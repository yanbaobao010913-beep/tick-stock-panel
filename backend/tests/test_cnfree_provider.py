"""CnFreeProvider 契约与单位标准化测试。

不依赖真实网络: client 层用假 httpx.Client 返回实测格式的 GBK 快照/腾讯 K 线
payload, provider 层注入假 CnFreeClient。覆盖字段映射与单位(新浪股→手、
百分数→小数制推导、amount 元; 指数量纲分异: 沪指数已是手、深/北指数为股)、
尾部字段漂移定位(33/34/39 字段)、停牌空串跳过、新浪分批与合并、
软失败语义([]/None)、日K列序换序与不复权透传、adj 仿射推导(纯分红/送转/
派+转混合事件、方向、容差、窗口)、分钟 naive datetime 与 amount null、
能力声明与 loader 注册(CONTRIBUTING §3 / docs/plugin-development.md)。
"""

from __future__ import annotations

import calendar
import os
import time
from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from app.plugins.cnfree import client as cc
from app.plugins.cnfree import provider as cp
from app.plugins.cnfree.client import CnFreeError
from app.plugins.cnfree.provider import CnFreeProvider

# =====================================================================
# 实测格式的样例数据(2026-09-29 抓包形态)
# =====================================================================


def _sina_stock_fields(**over) -> list[str]:
    """34 字段股票/ETF 行: [0]名称 [1]开 [2]昨收 [3]最新 [4]高 [5]低
    [6]买一 [7]卖一 [8]量(股) [9]额(元) [10..29]盘口填充 [30]日期 [31]时间 [32..]尾部。"""
    fields = [
        "贵州茅台", "1244.600", "1243.880", "1235.580", "1245.870", "1230.880",
        "1235.580", "1235.700", "2636630", "3260057865.000",
        *["0"] * 20,
        "2026-09-29", "15:00:00", "00", "D|500|617790.00",
    ]
    return _override_fields(fields, over)


def _sina_etf_fields(**over) -> list[str]:
    fields = [
        "港股创新药ETF广发", "1.335", "1.318", "1.339", "1.359", "1.318",
        "1.339", "1.340", "5973968604", "7983641197.000",
        *["0"] * 20,
        "2026-09-29", "15:34:59", "00", "D|2269800|3039262.20",
    ]
    return _override_fields(fields, over)


def _sina_index_fields(**over) -> list[str]:
    """33 字段深指数行(如 sz399001): 尾部无 'D|...' 列, 日期/时间整体前移一位。"""
    fields = [
        "深证成指", "12839.611", "12858.753", "12901.947", "12955.254", "12831.873",
        "0.000", "0.000", "48269806213", "747493244834.227",
        *["0"] * 19,
        "2026-09-29", "15:00:03", "00",
    ]
    return _override_fields(fields, over)


def _sina_index_sh_fields(**over) -> list[str]:
    """34 字段沪指数行(如 sh000001): 实测 volume 量纲已是手(与腾讯指数日K同值)。"""
    fields = [
        "上证指数", "3816.1536", "3823.6206", "3830.4513", "3843.8375", "3810.8116",
        "0.000", "0.000", "399473391", "661704292801.000",
        *["0"] * 20,
        "2026-09-29", "15:35:28", "00", "D|500|617790.00",
    ]
    return _override_fields(fields, over)


def _sina_index_bj_fields(**over) -> list[str]:
    """39 字段北证50 行(实测): 字段数漂移更大, 日期/时间仍靠尾部定位。"""
    fields = [
        "北证50", "1398.42", "1395.10", "1402.55", "1408.17", "1394.62",
        "0.000", "0.000", "592546096", "12525613593.000",
        *["0"] * 20,
        "2026-09-29", "15:31:15", "00",
        *["EXT"] * 6,
    ]
    return _override_fields(fields, over)


def _override_fields(fields: list[str], over: dict) -> list[str]:
    out = list(fields)
    for idx, value in over.pop("by_index", {}).items():
        out[idx] = value
    return out


def _sina_line(code: str, fields: list[str]) -> str:
    return f'var hq_str_{code}="{",".join(fields)}";'


def _sina_body(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode("gbk")


def _tday(ds: str, o: str, c: str, h: str, low_: str, v: str, extra=None) -> list:
    """腾讯日K行: [日期, open, close, high, low, volume(手), (第7列: 注释dict/amount)]。"""
    row = [ds, o, c, h, low_, v]
    if extra is not None:
        row.append(extra)
    return row


def _mrow(label: str, o: str, c: str, h: str, low_: str, v: str) -> list:
    """腾讯 m1 行: [yyyymmddHHMM, open, close, high, low, volume(手), 任意, 任意]。"""
    return [label, o, c, h, low_, v, {}, "0.1144"]


def _expect_beijing_ms(y: int, mo: int, d: int, h: int, mi: int, s: int) -> int:
    """北京墙钟 → epoch 毫秒(timegm 视结构为 UTC, 北京 = UTC+8, 减 8h)。"""
    return (calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0)) - 28_800) * 1000


# =====================================================================
# 假传输层(client 层) 与假 client(provider 层)
# =====================================================================


class _Resp:
    def __init__(self, content: bytes = b"", status_code: int = 200, payload=None):
        self.content = content
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeSinaHttp:
    """按请求 URL 返回预置 GBK 报文, 记录调用。"""

    def __init__(self, body: bytes, status_code: int = 200, error: Exception | None = None):
        self.body = body
        self.status_code = status_code
        self.error = error
        self.calls: list[str] = []

    def get(self, url, params=None, **kw):
        self.calls.append(url)
        if self.error:
            raise self.error
        return _Resp(content=self.body, status_code=self.status_code)


class _FakeTencentHttp:
    """按 param 前缀(代码+fqt)返回预置 payload, 记录调用。

    responder(param) -> payload dict; 用可调用以支持按窗口/游标变化的分页响应。
    """

    def __init__(self, responder):
        self.responder = responder
        self.calls: list[str] = []

    def get(self, url, params=None):
        param = (params or {}).get("param", "")
        self.calls.append(param)
        return _Resp(payload=self.responder(param))


def _patch_client_cls(monkeypatch, fake):
    monkeypatch.setattr(cp, "cnfree_client", type("M", (), {"CnFreeClient": lambda **kw: fake}))


def _provider_with(monkeypatch, fake, universe=None) -> CnFreeProvider:
    _patch_client_cls(monkeypatch, fake)
    monkeypatch.setattr(cp, "_SYM_INTERVAL_S", 0.0)
    p = CnFreeProvider()
    if universe is not None:
        monkeypatch.setattr(p, "_load_universe", lambda kind: universe.get(kind, []))
    return p


class _FakeClient:
    """provider 层假客户端: 预置快照/日K/分钟数据, 记录调用供断言。

    error: sina_snapshot 抛出(整体软失败); daily_error_codes: 指定代码的日K
    请求抛出(单标的软失败); tencent_rows_error: 降级源整体失败;
    tencent_blocked: 降级源也被 WAF 限流(抛 CnFreeRateLimitedError)。
    """

    def __init__(
        self,
        snapshot=None,
        daily=None,
        minute=None,
        error=None,
        daily_error_codes=(),
        depth=None,
        depth_error=False,
        tencent_rows=None,
        tencent_snapshot_error=None,
        tencent_blocked=False,
    ):
        self.snapshot = snapshot or {}
        self.daily = daily or {}
        self.minute = minute or {}
        self.error = error
        self.daily_error_codes = set(daily_error_codes)
        self.depth = depth or {}
        self.depth_error = depth_error
        self.tencent_rows = tencent_rows or {}
        self.tencent_rows_error = tencent_snapshot_error
        self.tencent_blocked = tencent_blocked
        self.depth_calls: list[list[str]] = []
        self.sina_calls: list[list[str]] = []
        self.tencent_calls: list[list[str]] = []
        self.daily_calls: list[tuple] = []
        self.minute_calls: list[tuple] = []

    def tencent_depth_batch(self, sina_codes):
        self.depth_calls.append(list(sina_codes))
        if self.depth_error:
            raise CnFreeError("腾讯盘口 HTTP 501")
        out = {}
        for code in sina_codes:
            if code in self.depth:
                out[code] = self.depth[code]
        return out

    def tencent_snapshot(self, codes):
        self.tencent_calls.append(list(codes))
        if self.tencent_blocked:
            raise cc.CnFreeRateLimitedError("腾讯快照疑似被 WAF 限流 HTTP 501")
        if self.tencent_rows_error:
            raise self.tencent_rows_error
        wanted = set(codes)
        return {c: f for c, f in self.tencent_rows.items() if c in wanted}

    def sina_snapshot(self, codes):
        self.sina_calls.append(list(codes))
        if self.error:
            raise self.error
        wanted = set(codes)
        return {c: f for c, f in self.snapshot.items() if c in wanted}

    def tencent_daily(self, code, start_d, end_d, fqt=""):
        self.daily_calls.append((code, start_d, end_d, fqt))
        if self.error or code in self.daily_error_codes:
            raise CnFreeError(f"{code} 拉取失败")
        return list(self.daily.get((code, fqt), []))

    def tencent_minute(self, code, end_dt, max_pages):
        self.minute_calls.append((code, end_dt, max_pages))
        if self.error:
            raise self.error
        return list(self.minute.get(code, []))

    def close(self):
        pass


# =====================================================================
# client 层: 新浪 headers / GBK / 空串跳过 / 分批
# =====================================================================


def test_client_sends_referer_and_ua(monkeypatch):
    captured: dict = {}

    def _factory(**kw):
        captured.update(kw)
        return _FakeSinaHttp(_sina_body())

    monkeypatch.setattr(cc.httpx, "Client", _factory)
    cc.CnFreeClient()
    assert captured["headers"]["Referer"] == "https://finance.sina.com.cn"
    assert "Mozilla/5.0" in captured["headers"]["User-Agent"]


def test_client_decodes_gbk_and_parses_lines(monkeypatch):
    body = _sina_body(_sina_line("sh600519", _sina_stock_fields()))
    monkeypatch.setattr(cc.httpx, "Client", lambda **kw: _FakeSinaHttp(body))
    out = cc.CnFreeClient().sina_snapshot(["sh600519"])
    assert list(out) == ["sh600519"]
    assert out["sh600519"][0] == "贵州茅台"  # GBK 解码正确
    assert len(out["sh600519"]) == 34


def test_client_skips_suspended_and_empty_lines(monkeypatch):
    body = _sina_body(
        _sina_line("sz000001", _sina_stock_fields(by_index={0: "平安银行"})),
        'var hq_str_sh600000="";',  # 停牌/无效代码
        'var hq_str_bj899050="";',
    )
    monkeypatch.setattr(cc.httpx, "Client", lambda **kw: _FakeSinaHttp(body))
    out = cc.CnFreeClient().sina_snapshot(["sz000001", "sh600000", "bj899050"])
    assert list(out) == ["sz000001"]


def test_client_batches_requests_with_interval(monkeypatch):
    lines = [
        _sina_line("sh600519", _sina_stock_fields()),
        _sina_line("sz000001", _sina_stock_fields(by_index={0: "平安银行"})),
        _sina_line("sh513120", _sina_etf_fields()),
    ]
    monkeypatch.setattr(cc.httpx, "Client", lambda **kw: _FakeSinaHttp(_sina_body(*lines)))
    sleeps: list[float] = []
    monkeypatch.setattr(cc.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(cc, "_SINA_BATCH", 2)
    out = cc.CnFreeClient().sina_snapshot(["sh600519", "sz000001", "sh513120"])
    assert len(out) == 3  # 多批合并
    assert sleeps and sleeps[0] == cc._SINA_BATCH_INTERVAL_S  # 批间 0.1s


def test_client_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(
        cc.httpx, "Client", lambda **kw: _FakeSinaHttp(b"", status_code=462)
    )
    with pytest.raises(CnFreeError, match="462"):
        cc.CnFreeClient().sina_snapshot(["sh600519"])


# =====================================================================
# client 层: 腾讯日K分页(空页终止 / 不足整页终止 / 页数上限 / qfq 退化)
# =====================================================================


def _daily_responder(all_rows: list[list]):
    """模拟腾讯语义: 返回窗口内自末端向前 count 根(窗口 [start, end] 在 param 中)。"""

    def _respond(param: str) -> dict:
        code, _kind, start, end, count, fqt = param.split(",")
        rows = [r for r in all_rows if start <= r[0] <= end]
        rows = rows[-int(count) :]
        node = {"qfqday": rows} if fqt == "qfq" else {"day": rows}
        return {"code": 0, "data": {code: node}}

    return _respond


def test_client_daily_pagination_walks_backward_and_stops(monkeypatch):
    dates = [date(2026, 6, 1) + timedelta(days=i) for i in range(1500)]
    all_rows = [_tday(d.isoformat(), "1", "1", "1", "1", "100") for d in dates]
    http = _FakeTencentHttp(_daily_responder(all_rows))
    monkeypatch.setattr(cc.httpx, "Client", lambda **kw: http)
    rows = cc.CnFreeClient().tencent_daily("sh600519", dates[0], dates[-1], fqt="")
    assert [r[0] for r in rows] == [d.isoformat() for d in dates]  # 1500 根, 升序无重复
    assert len(http.calls) == 2  # 800 + 700(不足整页终止)
    # 第二段终点 = 第一页最早日期的前一天
    assert http.calls[1].split(",")[3] == (dates[1500 - 800] - timedelta(days=1)).isoformat()


def test_client_daily_empty_page_terminates(monkeypatch):
    http = _FakeTencentHttp(lambda param: {"code": 0, "data": {"sh600519": {"day": []}}})
    monkeypatch.setattr(cc.httpx, "Client", lambda **kw: http)
    rows = cc.CnFreeClient().tencent_daily("sh600519", date(2026, 1, 1), date(2026, 9, 29))
    assert rows == []
    assert len(http.calls) == 1


def test_client_daily_page_cap(monkeypatch):
    dates = [date(2020, 1, 1) + timedelta(days=i) for i in range(3000)]
    all_rows = [_tday(d.isoformat(), "1", "1", "1", "1", "100") for d in dates]
    http = _FakeTencentHttp(_daily_responder(all_rows))
    monkeypatch.setattr(cc.httpx, "Client", lambda **kw: http)
    monkeypatch.setattr(cc, "_KLINE_MAX_PAGES", 2)
    rows = cc.CnFreeClient().tencent_daily("sh600519", dates[0], dates[-1])
    assert len(rows) == 1600  # 2 页 x 800, 页数上限兜底
    assert len(http.calls) == 2


def test_client_daily_qfq_reads_qfqday_then_falls_back_to_day(monkeypatch):
    rows = [_tday("2026-09-29", "1", "1", "1", "1", "100")]

    # 正常: qfq 请求返回 qfqday
    def _with_qfqday(param: str) -> dict:
        return {"code": 0, "data": {"sh600519": {"qfqday": rows}}}

    monkeypatch.setattr(cc.httpx, "Client", lambda **kw: _FakeTencentHttp(_with_qfqday))
    assert cc.CnFreeClient().tencent_daily("sh600519", date(2026, 9, 1), date(2026, 9, 29), "qfq") == rows

    # 退化: qfq 请求只有 day
    def _day_only(param: str) -> dict:
        return {"code": 0, "data": {"sh600519": {"day": rows}}}

    monkeypatch.setattr(cc.httpx, "Client", lambda **kw: _FakeTencentHttp(_day_only))
    assert cc.CnFreeClient().tencent_daily("sh600519", date(2026, 9, 1), date(2026, 9, 29), "qfq") == rows


def test_client_daily_malformed_payload_returns_empty(monkeypatch):
    # 实测 count 超上限时 data.{code} 退化为 list → 视为空, 不抛异常
    monkeypatch.setattr(
        cc.httpx,
        "Client",
        lambda **kw: _FakeTencentHttp(lambda param: {"code": 0, "data": {"sh600519": ["bad"]}}),
    )
    assert cc.CnFreeClient().tencent_daily("sh600519", date(2026, 1, 1), date(2026, 9, 29)) == []


# =====================================================================
# client 层: 腾讯分钟分页
# =====================================================================


def test_client_minute_pages_backward(monkeypatch):
    def _respond(param: str) -> dict:
        code, _kind, cursor, count = param.split(",")
        end = int(cursor) if cursor else 202609291500
        n = int(count)
        # 与实测语义一致: 游标互斥, 返回游标之前紧邻的 n 根
        labels = range(end - n, end)
        return {"code": 0, "data": {code: {"m1": [[str(x), "1", "1", "1", "1", "10"] for x in labels]}}}

    http = _FakeTencentHttp(_respond)
    monkeypatch.setattr(cc.httpx, "Client", lambda **kw: http)
    monkeypatch.setattr(cc, "_MINUTE_PAGE", 3)
    rows = cc.CnFreeClient().tencent_minute("sh600519", None, max_pages=3)
    assert len(rows) == 9  # 3 页 x 3 根
    assert len(http.calls) == 3
    # 第二页游标 = 第一页最早标签(向过去推进)
    assert http.calls[1].split(",")[2] == str(202609291500 - 3)
    assert [r[0] for r in rows] == sorted(r[0] for r in rows)  # 升序
    assert len({r[0] for r in rows}) == 9  # 无重复


# =====================================================================
# provider 层: realtime 字段映射与单位
# =====================================================================


def test_realtime_units_and_field_mapping(monkeypatch):
    fake = _FakeClient(snapshot={"sh600519": _sina_stock_fields()})
    p = _provider_with(monkeypatch, fake, universe={"stock": ["600519.SH"], "etf": []})
    records = p.get_realtime()
    assert len(records) == 1
    r = records[0]
    assert r["symbol"] == "600519.SH"
    assert r["name"] == "贵州茅台"
    assert r["last_price"] == 1235.58
    assert r["prev_close"] == 1243.88
    assert r["open"] == 1244.60 and r["high"] == 1245.87 and r["low"] == 1230.88
    # 核心口径: change_pct 小数制 = (last - prev) / prev, 不乘 100
    assert r["change_pct"] == pytest.approx((1235.58 - 1243.88) / 1243.88)
    assert r["change_amount"] == pytest.approx(1235.58 - 1243.88)
    # 新浪 volume 单位股 → 手 floor(/100)
    assert r["volume"] == 26366
    # amount 元直接透传
    assert r["amount"] == 3260057865.0
    # 尾部日期+时间 → 北京 naive → epoch 毫秒
    assert r["timestamp"] == _expect_beijing_ms(2026, 9, 29, 15, 0, 0)
    # 未提供字段置 None, 不启发式伪造
    assert r["amplitude"] is None and r["turnover_rate"] is None and r["session"] is None


def test_realtime_etf_volume_shares_to_lots(monkeypatch):
    fake = _FakeClient(snapshot={"sh513120": _sina_etf_fields()})
    p = _provider_with(monkeypatch, fake, universe={"stock": [], "etf": ["513120.SH"]})
    r = p.get_realtime()[0]
    assert r["symbol"] == "513120.SH"
    assert r["name"] == "港股创新药ETF广发"
    assert r["volume"] == 59739686  # 5,973,968,604 份 → 手
    assert r["change_pct"] == pytest.approx((1.339 - 1.318) / 1.318)


def test_realtime_index_tail_field_drift(monkeypatch):
    """33 字段深指数行: 日期/时间不能按固定下标 30/31, 必须从尾部定位。"""
    fake = _FakeClient(snapshot={"sz399001": _sina_index_fields()})
    p = _provider_with(monkeypatch, fake, universe={"stock": [], "etf": []})
    records = p.get_realtime_indices(["399001.SZ"])
    assert len(records) == 1
    r = records[0]
    assert r["symbol"] == "399001.SZ"
    assert r["last_price"] == 12901.947
    assert r["timestamp"] == _expect_beijing_ms(2026, 9, 29, 15, 0, 3)
    # 深交所指数量纲为股 → /100 折手(实测为腾讯指数日K volume 的 100 倍)
    assert r["volume"] == 482698062
    assert r["amount"] == 747493244834.227
    assert r["change_pct"] == pytest.approx((12901.947 - 12858.753) / 12858.753)


def test_realtime_index_sh_volume_already_in_lots(monkeypatch):
    """上交所指数量纲已是手(实测与腾讯指数日K同值) → 原样透传, 不再 /100。"""
    fake = _FakeClient(snapshot={"sh000001": _sina_index_sh_fields()})
    p = _provider_with(monkeypatch, fake, universe={"stock": [], "etf": []})
    r = p.get_realtime_indices(["000001.SH"])[0]
    assert r["symbol"] == "000001.SH"
    assert r["volume"] == 399473391  # 34 字段行, vol 已是手
    assert r["timestamp"] == _expect_beijing_ms(2026, 9, 29, 15, 35, 28)


def test_realtime_index_bj_volume_and_39_fields(monkeypatch):
    """北证50 实测 39 字段(漂移更大): 尾部定位日期时间, bj 指数为股 → /100。"""
    fields = _sina_index_bj_fields()
    assert len(fields) == 39
    fake = _FakeClient(snapshot={"bj899050": fields})
    p = _provider_with(monkeypatch, fake, universe={"stock": [], "etf": []})
    r = p.get_realtime_indices(["899050.BJ"])[0]
    assert r["volume"] == 5925460  # 592,546,096 股 → 手
    assert r["timestamp"] == _expect_beijing_ms(2026, 9, 29, 15, 31, 15)


def test_realtime_stock_volume_always_shares_to_lots(monkeypatch):
    """股票/ETF 不受指数规则影响: 沪/深/北股票 Sina volume 均为股 → floor(/100)。"""
    fields_bj_stock = _sina_stock_fields(by_index={0: "万达轴承", 8: "2012360", 9: "105277106"})
    fake = _FakeClient(
        snapshot={
            "sh600519": _sina_stock_fields(),
            "sz000001": _sina_stock_fields(by_index={0: "平安银行", 8: "69097909", 9: "784632823"}),
            "bj920002": fields_bj_stock,
        }
    )
    p = _provider_with(
        monkeypatch,
        fake,
        universe={"stock": ["600519.SH", "000001.SZ", "920002.BJ"], "etf": []},
    )
    by_symbol = {r["symbol"]: r for r in p.get_realtime()}
    assert by_symbol["600519.SH"]["volume"] == 26366  # 2,636,630 股
    assert by_symbol["000001.SZ"]["volume"] == 690979  # 69,097,909 股
    assert by_symbol["920002.BJ"]["volume"] == 20123  # 2,012,360 股


def test_realtime_tail_drift_with_extra_trailing_fields(monkeypatch):
    """尾部再多一列漂移仍能定位日期/时间(固定下标方案会全部错位)。"""
    fields = [*_sina_stock_fields(), "EXT"]
    fake = _FakeClient(snapshot={"sh600519": fields})
    p = _provider_with(monkeypatch, fake, universe={"stock": ["600519.SH"], "etf": []})
    assert p.get_realtime()[0]["timestamp"] == _expect_beijing_ms(2026, 9, 29, 15, 0, 0)


def test_realtime_unparsable_timestamp_falls_back_to_local(monkeypatch):
    fields = _sina_stock_fields(by_index={30: "not-a-date", 31: "not-a-time"})
    fake = _FakeClient(snapshot={"sh600519": fields})
    p = _provider_with(monkeypatch, fake, universe={"stock": ["600519.SH"], "etf": []})
    assert p.get_realtime()[0]["timestamp"] > 0


def test_realtime_prev_close_zero_change_pct_none(monkeypatch):
    fields = _sina_stock_fields(by_index={2: "0"})
    fake = _FakeClient(snapshot={"sh600519": fields})
    p = _provider_with(monkeypatch, fake, universe={"stock": ["600519.SH"], "etf": []})
    r = p.get_realtime()[0]
    assert r["change_pct"] is None and r["prev_close"] == 0.0


def test_realtime_suspended_code_skipped_silently(monkeypatch):
    """停牌代码返回空串行 → client 跳过; provider 无记录且不告警不报错。"""
    fake = _FakeClient(snapshot={"sh600519": _sina_stock_fields()})
    p = _provider_with(
        monkeypatch,
        fake,
        universe={"stock": ["600519.SH", "600000.SH"], "etf": []},
    )
    records = p.get_realtime()
    assert [r["symbol"] for r in records] == ["600519.SH"]  # sh600000 空串被跳过
    assert fake.sina_calls == [["sh600519", "sh600000"]]


def test_realtime_all_rows_unparseable_warns_and_returns_empty(monkeypatch, caplog):
    fake = _FakeClient(snapshot={"sh600519": ["只有一列"]})
    p = _provider_with(monkeypatch, fake, universe={"stock": ["600519.SH"], "etf": []})
    with caplog.at_level("WARNING"):
        assert p.get_realtime() == []
    assert "结构变化" in caplog.text


def test_realtime_universe_missing_returns_empty_with_hint(monkeypatch, tmp_path, caplog):
    """标的池两个 parquet 都缺失 → 返回 [] 并提示先跑数据管道。"""
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    p = _provider_with(monkeypatch, _FakeClient())
    with caplog.at_level("WARNING"):
        assert p.get_realtime() == []
    assert "数据管道" in caplog.text


def test_universe_loads_symbols_with_mtime_cache(monkeypatch, tmp_path):
    """标的池懒加载 instruments.parquet + instruments_etf.parquet, mtime 缓存。"""
    from app.config import settings

    inst = tmp_path / "instruments"
    etf = tmp_path / "instruments_etf"
    inst.mkdir()
    etf.mkdir()
    pl.DataFrame({"symbol": ["600519.SH", "000001.SZ", "600519.SH"]}).write_parquet(
        inst / "instruments.parquet"
    )
    pl.DataFrame({"symbol": ["513120.SH", "159530.SZ"]}).write_parquet(
        etf / "instruments_etf.parquet"
    )
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    p = CnFreeProvider()
    assert p._load_universe("stock") == ["000001.SZ", "600519.SH"]  # 去重
    assert p._load_universe("etf") == ["159530.SZ", "513120.SH"]

    reads = {"n": 0}
    real_read = pl.read_parquet

    def _counting_read(*args, **kwargs):
        reads["n"] += 1
        return real_read(*args, **kwargs)

    monkeypatch.setattr(pl, "read_parquet", _counting_read)
    assert p._load_universe("stock") == ["000001.SZ", "600519.SH"]
    assert reads["n"] == 0  # mtime 未变 → 缓存命中, 不重读

    # mtime 变化 → 重新加载
    os.utime(inst / "instruments.parquet", (time.time() + 5, time.time() + 5))
    assert p._load_universe("stock") == ["000001.SZ", "600519.SH"]
    assert reads["n"] == 1


def test_realtime_soft_fail_returns_empty_list(monkeypatch, caplog):
    fake = _FakeClient(error=CnFreeError("网络请求失败"))
    p = _provider_with(monkeypatch, fake, universe={"stock": ["600519.SH"], "etf": []})
    with caplog.at_level("WARNING"):
        assert p.get_realtime() == []
    assert "失败" in caplog.text


def test_realtime_indices_error_returns_none(monkeypatch):
    """指数拉取失败 → None(上层保留上轮有效指数缓存)。"""
    fake = _FakeClient(error=CnFreeError("网络请求失败"))
    p = _provider_with(monkeypatch, fake)
    assert p.get_realtime_indices(["000001.SH"]) is None


def test_realtime_indices_success_but_empty(monkeypatch):
    fake = _FakeClient(snapshot={})
    p = _provider_with(monkeypatch, fake)
    assert p.get_realtime_indices(["000001.SH", "399001.SZ"]) == []
    assert fake.sina_calls == [["sh000001", "sz399001"]]


def test_realtime_skips_unmappable_symbols(monkeypatch):
    fake = _FakeClient(snapshot={"sh600519": _sina_stock_fields()})
    p = _provider_with(
        monkeypatch, fake, universe={"stock": ["600519.SH", "600519"], "etf": []}
    )
    records = p.get_realtime()
    assert [r["symbol"] for r in records] == ["600519.SH"]


# =====================================================================
# provider 层: daily 列序 / 单位 / 分批
# =====================================================================


def test_daily_column_order_and_raw_passthrough(monkeypatch):
    """腾讯行内列序 open,close,high,low → 输出 OHLC 标准序; volume 手透传; amount null。"""
    daily = {
        ("sh600519", ""): [
            _tday("2026-09-28", "1236.000", "1243.880", "1244.010", "1228.100", "28218.000"),
            _tday("2026-09-29", "1244.600", "1235.580", "1245.870", "1230.880", "26366.000"),
        ]
    }
    p = _provider_with(monkeypatch, _FakeClient(daily=daily))
    df = p.get_daily(["600519.SH"], datetime(2026, 9, 1), datetime(2026, 9, 29))
    assert df.columns == ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]
    assert df.schema["date"] == pl.Date
    assert df["date"].to_list() == [date(2026, 9, 28), date(2026, 9, 29)]
    # 换序正确: close 来自行内下标 2, high 来自下标 3, low 来自下标 4
    assert df["close"].to_list() == [1243.88, 1235.58]
    assert df["open"].to_list() == [1236.0, 1244.6]
    assert df["high"].to_list() == [1244.01, 1245.87]
    assert df["low"].to_list() == [1228.1, 1230.88]
    # 不复权原始价透传, volume 已是手, 不做 /100
    assert df["volume"].to_list() == [28218.0, 26366.0]
    assert df["amount"].to_list() == [None, None]


def test_daily_amount_wan_to_yuan_and_annotation_dict_ignored(monkeypatch):
    daily = {
        ("sh600519", ""): [
            # 第 7 列为数值字符串 → 万元 x10000 转元
            _tday("2026-06-18", "1", "1", "1", "1", "100", "41262.000"),
            # 第 7 列为分红注释 dict(实测形态) → None, 不伪造
            _tday(
                "2026-06-19",
                "1",
                "1",
                "1",
                "1",
                "100",
                {"nd": "2023", "fh_sh": "308.76", "cqr": "2026-06-19"},
            ),
            # 第 7 列缺失 → None
            _tday("2026-06-20", "1", "1", "1", "1", "100"),
        ]
    }
    p = _provider_with(monkeypatch, _FakeClient(daily=daily))
    df = p.get_daily(["600519.SH"], datetime(2026, 6, 1), datetime(2026, 6, 30))
    assert df["amount"].to_list() == [412_620_000.0, None, None]


def test_daily_halt_rows_filtered(monkeypatch):
    """open=high=0 的停牌行由 normalize_daily 过滤, 不落库。"""
    daily = {
        ("sh600519", ""): [
            _tday("2026-09-28", "0", "1235.58", "0", "0", "0"),
            _tday("2026-09-29", "1244.600", "1235.580", "1245.870", "1230.880", "26366.000"),
        ]
    }
    p = _provider_with(monkeypatch, _FakeClient(daily=daily))
    df = p.get_daily(["600519.SH"], datetime(2026, 9, 1), datetime(2026, 9, 29))
    assert df["date"].to_list() == [date(2026, 9, 29)]


def test_daily_symbol_failure_skipped_not_fatal(monkeypatch):
    daily = {("sz000001", ""): [_tday("2026-09-29", "11.28", "11.35", "11.41", "11.28", "690978")]}
    fake = _FakeClient(daily=daily, daily_error_codes={"sh600519"})
    p = _provider_with(monkeypatch, fake)
    df = p.get_daily(["600519.SH", "000001.SZ"], datetime(2026, 9, 1), datetime(2026, 9, 29))
    # 第一个标的拉取失败被告警跳过, 第二个不受影响
    assert df["symbol"].unique().to_list() == ["000001.SZ"]
    # 失败标的仍走完调用(软失败语义: 异常在 client 层抛出, _daily_rows 捕获跳过)
    assert any(call[0] == "sh600519" for call in fake.daily_calls)


def test_daily_empty_symbols_returns_empty(monkeypatch):
    p = _provider_with(monkeypatch, _FakeClient())
    df = p.get_daily([], datetime(2026, 9, 1), datetime(2026, 9, 29))
    assert df.is_empty()


def test_daily_progress_callback_counts_empty_batches(monkeypatch):
    """每批(50 只)回调一次 on_chunk_done, 空批次也计数, 最终 cur == total。"""
    daily = {("sz000001", ""): [_tday("2026-09-29", "11.28", "11.35", "11.41", "11.28", "690978")]}
    p = _provider_with(monkeypatch, _FakeClient(daily=daily))
    monkeypatch.setattr(cp, "_HIST_SYMBOL_BATCH", 2)
    progress: list[tuple[int, int]] = []
    df = p.get_daily(
        ["600519.SH", "000001.SZ", "600000.SH"],
        datetime(2026, 9, 1),
        datetime(2026, 9, 29),
        on_chunk_done=lambda c, t: progress.append((c, t)),
    )
    assert progress == [(1, 2), (2, 2)]
    assert df["symbol"].unique().to_list() == ["000001.SZ"]


def test_daily_etf_same_endpoint(monkeypatch):
    daily = {("sz159530", ""): [_tday("2026-09-29", "1.260", "1.271", "1.277", "1.253", "3007022")]}
    p = _provider_with(monkeypatch, _FakeClient(daily=daily))
    df = p.get_daily(
        ["159530.SZ"], datetime(2026, 9, 1), datetime(2026, 9, 29), asset_type="etf"
    )
    assert df.height == 1 and df["close"][0] == 1.271


# =====================================================================
# provider 层: adj_factor 仿射推导
# =====================================================================


def _adj_fixture(
    raw_closes: list[str],
    event_idx: int,
    before=None,  # 事件前日期的 qfq 变换 callable(c)->float
    fh_sh: str = "3.0",
    annotate: bool = True,
):
    """构造 6 日 qfq/raw 夹具: 事件日在 event_idx。

    before(c) 为事件前日期的 qfq 变换(事件后恒为恒等); annotate 控制是否在
    事件日行内带腾讯除权注释 dict(实测: 分红/送转除权日均带)。
    返回 (days, raw_rows, qfq_rows, event_day)。
    """
    days = [date(2026, 8, 24) + timedelta(days=i) for i in range(len(raw_closes))]
    event_day = days[event_idx]
    raw_rows = [
        _tday(d.isoformat(), c, c, c, c, "1000")
        for d, c in zip(days, raw_closes, strict=True)
    ]
    if annotate:
        # 实测注释 dict 形态(键: nd/fh_sh/djr/cqr/FHcontent), 仅 cqr 参与判定
        raw_rows[event_idx].append(
            {
                "nd": str(event_day.year - 1),
                "fh_sh": fh_sh,
                "djr": (event_day - timedelta(days=1)).isoformat(),
                "cqr": event_day.isoformat(),
                "FHcontent": f"10派{fh_sh}元" if fh_sh else "10转3股",
            }
        )
    qfq_rows = []
    for d, c in zip(days, raw_closes, strict=True):
        q = before(float(c)) if (before is not None and d < event_day) else float(c)
        qfq_rows.append(_tday(d.isoformat(), f"{q}", f"{q}", f"{q}", f"{q}", "1000"))
    return days, raw_rows, qfq_rows, event_day


def test_adj_dividend_event_factor_direction(monkeypatch):
    """纯分红: qfq = raw - 0.30(事件前) → ex_factor = raw_prev/(raw_prev - D) > 1。"""
    _days, raw_rows, qfq_rows, event_day = _adj_fixture(
        ["10.00", "10.10", "9.80", "9.90", "10.05", "9.95"], 2, before=lambda c: c - 0.30
    )
    fake = _FakeClient(daily={("sh600519", ""): raw_rows, ("sh600519", "qfq"): qfq_rows})
    p = _provider_with(monkeypatch, fake)
    df = p.get_adj_factors(["600519.SH"], datetime(2026, 8, 24), datetime(2026, 8, 29))
    assert df.columns == ["symbol", "trade_date", "ex_factor"]
    assert df.height == 1
    row = df.row(0, named=True)
    assert row["symbol"] == "600519.SH"
    assert row["trade_date"] == event_day
    # 交易所公式: P=10.10(事件前收盘), D=0.30 → ref=9.80, factor=P/ref
    assert row["ex_factor"] == pytest.approx(10.10 / 9.80)
    assert row["ex_factor"] > 1.0  # 分红因子方向必 > 1


def test_adj_bonus_event_affine_factor(monkeypatch):
    """送转(乘法成分): 事件前 qfq = 0.5·raw + 1.0 → ref = 0.5·raw_prev + 1.0。"""
    _days, raw_rows, qfq_rows, event_day = _adj_fixture(
        ["5.00", "5.10", "5.23", "4.68", "4.70", "4.75"],
        2,
        before=lambda c: c * 0.5 + 1.0,
        fh_sh="",  # 送转事件无现金注释
    )
    fake = _FakeClient(daily={("sz300176", ""): raw_rows, ("sz300176", "qfq"): qfq_rows})
    p = _provider_with(monkeypatch, fake)
    df = p.get_adj_factors(["300176.SZ"], datetime(2026, 8, 24), datetime(2026, 8, 29))
    assert df.height == 1
    assert df["trade_date"][0] == event_day
    assert df["ex_factor"][0] == pytest.approx(5.10 / (5.10 * 0.5 + 1.0))
    assert df["ex_factor"][0] > 1.0


def test_adj_combined_dividend_split_matches_exchange_formula(monkeypatch):
    """派+转混合事件(实测 sz300661 "10派2元转3股" 形态): 事件前 qfq = (raw-D)/1.3,
    ex_factor = raw_prev*1.3/(raw_prev-D) — 与交易所公式一致(实测 1.302895)。"""
    _days, raw_rows, qfq_rows, event_day = _adj_fixture(
        ["90.00", "90.01", "88.50", "89.00", "89.50", "89.30"],
        2,
        before=lambda c: (c - 0.2) / 1.3,
        fh_sh="2",
    )
    fake = _FakeClient(daily={("sz300661", ""): raw_rows, ("sz300661", "qfq"): qfq_rows})
    p = _provider_with(monkeypatch, fake)
    df = p.get_adj_factors(["300661.SZ"], datetime(2026, 8, 24), datetime(2026, 8, 29))
    assert df.height == 1
    assert df["trade_date"][0] == event_day
    assert df["ex_factor"][0] == pytest.approx(90.01 * 1.3 / (90.01 - 0.2))
    assert df["ex_factor"][0] > 1.0


def test_adj_no_event_returns_empty_with_schema(monkeypatch):
    """无任何事件与注释 → 空 df 但保留契约列。"""
    _days, raw_rows, qfq_rows, _event = _adj_fixture(
        ["10.00", "10.10", "9.80", "9.90", "10.05", "9.95"], 2, annotate=False
    )
    fake = _FakeClient(daily={("sh513120", ""): raw_rows, ("sh513120", "qfq"): qfq_rows})
    p = _provider_with(monkeypatch, fake)
    df = p.get_adj_factors(["513120.SH"], datetime(2026, 8, 24), datetime(2026, 8, 29))
    assert df.is_empty()
    assert df.columns == ["symbol", "trade_date", "ex_factor"]


def test_adj_no_price_impact_annotation_filtered_by_tolerance(monkeypatch):
    """注释存在但两侧同线(无实际除权影响) → 因子 ~1, 被容差过滤, 不产出伪事件。"""
    _days, raw_rows, qfq_rows, _event = _adj_fixture(
        ["10.00", "10.10", "9.80", "9.90", "10.05", "9.95"], 2, before=lambda c: c
    )
    fake = _FakeClient(daily={("sh600519", ""): raw_rows, ("sh600519", "qfq"): qfq_rows})
    p = _provider_with(monkeypatch, fake)
    df = p.get_adj_factors(["600519.SH"], datetime(2026, 8, 24), datetime(2026, 8, 29))
    assert df.is_empty()


def test_adj_narrow_window_backfills_and_multi_symbol(monkeypatch):
    """窄窗口(管道按天增量)也要回填推导跨度内的历史事件(首装语义, 幂等合并);
    多标的各自推导。回归: 旧实现按请求窗口过滤, 08-26 事件在 [08-27,08-29]
    窗口外被丢弃, 本地无 all.parquet 时永远回填不出历史。"""
    days, raw_rows, qfq_rows, _event = _adj_fixture(
        ["10.00", "10.10", "9.80", "9.90", "10.05", "9.95"], 2, before=lambda c: c - 0.30
    )
    closes_b = ["5.00", "5.05", "5.20", "5.10", "5.15", "5.12"]
    raw_b = [_tday(d.isoformat(), c, c, c, c, "1000") for d, c in zip(days, closes_b, strict=True)]
    # 第二只: 事件日 08-26, 事件前 qfq = raw - 0.30
    qfq_b = [
        _tday(
            d.isoformat(),
            f"{float(c) - 0.30}" if d < days[2] else c,
            f"{float(c) - 0.30}" if d < days[2] else c,
            f"{float(c) - 0.30}" if d < days[2] else c,
            f"{float(c) - 0.30}" if d < days[2] else c,
            "1000",
        )
        for d, c in zip(days, closes_b, strict=True)
    ]
    raw_b[2].append({"cqr": days[2].isoformat(), "fh_sh": "3.0"})
    fake = _FakeClient(
        daily={
            ("sh600519", ""): raw_rows,
            ("sh600519", "qfq"): qfq_rows,
            ("sz000001", ""): raw_b,
            ("sz000001", "qfq"): qfq_b,
        }
    )
    p = _provider_with(monkeypatch, fake)
    df = p.get_adj_factors(
        ["600519.SH", "000001.SZ"], datetime(2026, 8, 27), datetime(2026, 8, 29)
    )
    # 事件日 08-26 早于窗口起点, 但在推导跨度内 → 照样输出(首装回填)
    assert set(df["symbol"].to_list()) == {"600519.SH", "000001.SZ"}
    df2 = p.get_adj_factors(["600519.SH", "000001.SZ"], datetime(2026, 8, 24), datetime(2026, 8, 29))
    assert set(df2["symbol"].to_list()) == {"600519.SH", "000001.SZ"}
    assert df2.sort("symbol")["ex_factor"].to_list()[0] == pytest.approx(5.05 / 4.75)


def test_adj_progress_callback_per_batch(monkeypatch):
    _days, raw_rows, qfq_rows, _event = _adj_fixture(
        ["10.00", "10.10", "9.80", "9.90", "10.05", "9.95"], 2, before=lambda c: c - 0.30
    )
    fake = _FakeClient(daily={("sh600519", ""): raw_rows, ("sh600519", "qfq"): qfq_rows})
    p = _provider_with(monkeypatch, fake)
    monkeypatch.setattr(cp, "_HIST_SYMBOL_BATCH", 1)
    progress: list[tuple[int, int]] = []
    p.get_adj_factors(
        ["600519.SH", "600000.SH"],  # 第二只无数据 → 空批次也计数
        datetime(2026, 8, 24),
        datetime(2026, 8, 29),
        on_chunk_done=lambda c, t: progress.append((c, t)),
    )
    assert progress == [(1, 2), (2, 2)]


def test_waf_challenge_detection():
    """腾讯 WAF 挑战页识别: 403/429/501 或 200+HTML(实测 501 返回 JS 挑战页)。"""
    from app.plugins.cnfree.client import _is_waf_challenge

    html = '<!DOCTYPE html><html><head><script>var i=location.href;var v=window.btoa?window.'
    assert _is_waf_challenge(501, html)  # 实测样本
    assert _is_waf_challenge(501, "")
    assert _is_waf_challenge(403, "")
    assert _is_waf_challenge(429, "")
    assert _is_waf_challenge(200, html)  # 200 也可能回挑战页
    assert not _is_waf_challenge(200, '{"data":{}}')
    assert not _is_waf_challenge(500, '{"err":1}')  # 普通 5xx 不是限流


def test_adj_rate_limited_circuit_breaker(monkeypatch, caplog):
    """连续限流熔断(2026-09-30 实测: 全市场同步触发腾讯 WAF 挑战页):
    连续 _SYM_FAIL_BREAK 个标的后不再空打剩余标的, 进度回调仍满足 cur==total。"""
    from app.plugins.cnfree.client import CnFreeRateLimitedError

    class _RateLimitedClient:
        def __init__(self):
            self.calls = 0

        def tencent_daily(self, code, start_d, end_d, fqt=""):
            self.calls += 1
            raise CnFreeRateLimitedError("腾讯K线疑似被 WAF 限流 HTTP 501")

        def close(self):
            pass

    fake = _RateLimitedClient()
    p = _provider_with(monkeypatch, fake)
    syms = [f"600{i:03d}.SH" for i in range(30)]
    progress: list[tuple[int, int]] = []
    with caplog.at_level("ERROR"):
        df = p.get_adj_factors(
            syms,
            datetime(2026, 8, 24),
            datetime(2026, 8, 29),
            on_chunk_done=lambda c, t: progress.append((c, t)),
        )
    assert df.is_empty()
    assert fake.calls == 20  # 第 20 个标的熔断, 不打满 30
    assert progress and progress[-1] == (1, 1)  # cur==total 契约保持
    assert "熔断" in caplog.text


def test_adj_client_error_returns_empty_schema(monkeypatch):
    fake = _FakeClient(error=CnFreeError("网络失败"))
    p = _provider_with(monkeypatch, fake)
    df = p.get_adj_factors(["600519.SH"], None, None)
    assert df.is_empty() and df.columns == ["symbol", "trade_date", "ex_factor"]


def test_adj_output_sorted_and_deduped(monkeypatch):
    _days, raw_rows, qfq_rows, _event = _adj_fixture(
        ["10.00", "10.10", "9.80", "9.90", "10.05", "9.95"], 2, before=lambda c: c - 0.30
    )
    fake = _FakeClient(daily={("sh600519", ""): raw_rows, ("sh600519", "qfq"): qfq_rows})
    p = _provider_with(monkeypatch, fake)
    df = p.get_adj_factors(["600519.SH"], None, None)
    assert df.height == 1  # 唯一事件


# =====================================================================
# provider 层: minute
# =====================================================================


def test_minute_mapping_naive_beijing_and_amount_null(monkeypatch):
    minute = {
        "sh600519": [
            _mrow("202609290931", "1244.00", "1243.50", "1244.20", "1243.00", "143.00"),
            _mrow("202609291500", "1236.50", "1235.58", "1236.50", "1235.58", "439.00"),
            _mrow("202609281500", "1240.00", "1241.00", "1242.00", "1239.00", "300.00"),
        ]
    }
    p = _provider_with(monkeypatch, _FakeClient(minute=minute))
    df = p.get_minute(["600519.SH"], datetime(2026, 9, 29), datetime(2026, 9, 29, 23, 59))
    assert df.columns == ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]
    dtype = df.schema["datetime"]
    assert isinstance(dtype, pl.Datetime) and dtype.time_zone is None  # 北京墙钟 naive
    assert df["datetime"].to_list() == [
        datetime(2026, 9, 29, 9, 31),
        datetime(2026, 9, 29, 15, 0),
    ]  # 09-28 的行被窗口过滤
    # 腾讯行内列序 open,close,high,low → 输出换序
    assert df["close"].to_list() == [1243.5, 1235.58]
    assert df["high"].to_list() == [1244.2, 1236.5]
    # volume 手透传; amount 恒 null(端点不提供, 不伪造)
    assert df["volume"].to_list() == [143.0, 439.0]
    assert df["amount"].to_list() == [None, None]


def test_minute_accepts_tz_aware_window(monkeypatch):
    """回归: kline_sync 实测传 aware start/end, 与 naive 行内时间比较曾抛
    TypeError(can't compare offset-naive and offset-aware datetimes) —
    入口归一为北京墙钟 naive 后, 过滤与分页游标都应正常。"""
    from datetime import timedelta as _td
    from datetime import timezone

    minute = {
        "sh600519": [
            _mrow("202609290931", "1244.00", "1243.50", "1244.20", "1243.00", "143.00"),
            _mrow("202609291500", "1236.50", "1235.58", "1236.50", "1235.58", "439.00"),
            _mrow("202609281500", "1240.00", "1241.00", "1242.00", "1239.00", "300.00"),
        ]
    }
    p = _provider_with(monkeypatch, _FakeClient(minute=minute))
    tz = timezone(_td(hours=8))
    df = p.get_minute(
        ["600519.SH"],
        datetime(2026, 9, 29, 0, 0, tzinfo=tz),
        datetime(2026, 9, 29, 23, 59, tzinfo=tz),
    )
    assert df["datetime"].to_list() == [
        datetime(2026, 9, 29, 9, 31),
        datetime(2026, 9, 29, 15, 0),
    ]
    # UTC aware 入参: 归一为北京墙钟后窗口仍应覆盖当日
    utc = UTC
    df2 = p.get_minute(
        ["600519.SH"],
        datetime(2026, 9, 28, 16, 0, tzinfo=utc),
        datetime(2026, 9, 29, 16, 0, tzinfo=utc),
    )
    assert df2.height == 2


def test_minute_freq_guard(monkeypatch, caplog):
    p = _provider_with(monkeypatch, _FakeClient())
    with caplog.at_level("WARNING"):
        df = p.get_minute(
            ["600519.SH"], datetime(2026, 9, 29), datetime(2026, 9, 29), freq="5m"
        )
    assert df.is_empty()
    assert "1m" in caplog.text


def test_minute_empty_symbols_returns_empty(monkeypatch):
    p = _provider_with(monkeypatch, _FakeClient())
    assert p.get_minute([], datetime(2026, 9, 29), datetime(2026, 9, 29)).is_empty()


def test_minute_passes_window_and_batching(monkeypatch):
    minute = {"sh600519": [_mrow("202609291500", "1", "1", "1", "1", "10")]}
    fake = _FakeClient(minute=minute)
    p = _provider_with(monkeypatch, fake)
    monkeypatch.setattr(cp, "_HIST_SYMBOL_BATCH", 1)
    progress: list[tuple[int, int]] = []
    p.get_minute(
        ["600519.SH", "000001.SZ"],
        datetime(2026, 9, 29),
        datetime(2026, 9, 29),
        on_chunk_done=lambda c, t: progress.append((c, t)),
    )
    assert progress == [(1, 2), (2, 2)]
    # 端时刻与页数估算随窗口传入
    assert fake.minute_calls[0][0] == "sh600519"
    assert fake.minute_calls[0][2] >= 1


# =====================================================================
# 能力声明 / availability / loader 集成
# =====================================================================


def test_datasets_declaration():
    config = CnFreeProvider().config
    assert set(config.datasets) == {"realtime", "daily", "adj_factor", "minute", "depth5"}
    # 财务/全量分钟无免费源 → False, 自动回退 TickFlow
    for absent in ("financial", "full_minute"):
        assert absent not in config.datasets


# =====================================================================
# provider 层: depth5 (腾讯 qt.gtimg 五档盘口)
# =====================================================================


def _depth_fields(
    bid_prices=("10.01", "10.00", "9.99", "9.98", "9.97"),
    bid_volumes=("100", "200", "300", "400", "500"),
    ask_prices=("10.02", "10.03", "10.04", "10.05", "10.06"),
    ask_volumes=("11", "22", "33", "44", "55"),
    ts="20260930114558",
    n=88,
) -> list[str]:
    """构造 qt.gtimg 原始字段行: 其余字段填占位值。"""
    f = ["x"] * max(n, 31)
    for k in range(5):
        f[9 + k * 2] = bid_prices[k]
        f[10 + k * 2] = bid_volumes[k]
        f[19 + k * 2] = ask_prices[k]
        f[20 + k * 2] = ask_volumes[k]
    f[30] = ts
    return f


def test_depth_mapping_fields_and_units(monkeypatch):
    """价量按契约排列(买1→买5/卖1→卖5), 量原生为手不换算, 时间戳转毫秒。"""
    fake = _FakeClient(depth={"sh600519": _depth_fields()})
    p = _provider_with(monkeypatch, fake)
    out = p.get_depth_batch(["600519.SH"])
    assert set(out) == {"600519.SH"}
    d = out["600519.SH"]
    assert d["bid_prices"] == [10.01, 10.00, 9.99, 9.98, 9.97]
    assert d["bid_volumes"] == [100.0, 200.0, 300.0, 400.0, 500.0]  # 手, 原样
    assert d["ask_prices"] == [10.02, 10.03, 10.04, 10.05, 10.06]
    assert d["ask_volumes"] == [11.0, 22.0, 33.0, 44.0, 55.0]
    ts = d["timestamp"]
    assert isinstance(ts, int) and ts > 1_700_000_000_000  # 毫秒量级
    # 北京 2026-09-30 11:45:58 → UTC epoch
    from datetime import datetime, timezone
    from datetime import timedelta as _td
    expect = int(datetime(2026, 9, 30, 11, 45, 58, tzinfo=timezone(_td(hours=8))).timestamp() * 1000)
    assert ts == expect


def test_depth_suspended_and_short_lines_skipped(monkeypatch):
    """停牌行(五档全 0)与短行(<29 字段)跳过, 不产出伪造盘口。"""
    fake = _FakeClient(
        depth={
            "sh600000": _depth_fields(bid_prices=("0",) * 5, ask_prices=("0",) * 5),
            "sh600001": ["x"] * 10,  # 短行
        }
    )
    p = _provider_with(monkeypatch, fake)
    out = p.get_depth_batch(["600000.SH", "600001.SH"])
    assert out == {}


def test_depth_unknown_code_skipped_and_batching(monkeypatch):
    """无法识别的代码跳过; 超过 _DEPTH_BATCH 分批请求。"""
    fake = _FakeClient(depth={"sh600519": _depth_fields()})
    p = _provider_with(monkeypatch, fake)
    syms = ["600519.SH", "BADCODE", "000001.SZ"] * 25  # 75 只 → 2 批
    out = p.get_depth_batch(syms)
    assert set(out) == {"600519.SH"}
    assert len(fake.depth_calls) == 2
    assert all(len(c) <= 60 for c in fake.depth_calls)
    # BADCODE 不进入任何批次请求
    assert all("badcode" not in c for c in fake.depth_calls)


def test_depth_soft_failure_on_client_error(monkeypatch):
    """批次请求失败 → 软处理(该批跳过), 返回空 dict 而非抛异常。"""
    fake = _FakeClient(depth_error=True)
    p = _provider_with(monkeypatch, fake)
    out = p.get_depth_batch(["600519.SH"])
    assert out == {}


def test_depth_rate_limit_circuit_breaker(monkeypatch, caplog):
    """连续限流熔断: 达到 _SYM_FAIL_BREAK 后不再打剩余批次。"""
    from app.plugins.cnfree.client import CnFreeRateLimitedError

    class _RateLimitedDepth:
        def __init__(self):
            self.calls = 0

        def tencent_depth_batch(self, codes):
            self.calls += 1
            raise CnFreeRateLimitedError("WAF")

        def close(self):
            pass

    fake = _RateLimitedDepth()
    p = _provider_with(monkeypatch, fake)
    syms = [f"{600000 + i}.SH" for i in range(60 * 25)]  # 25 批
    with caplog.at_level("ERROR"):
        out = p.get_depth_batch(syms)
    assert out == {}
    assert fake.calls == 20  # 熔断
    assert "熔断" in caplog.text


def test_provider_has_dataset_via_registry():
    from app.data_providers.custom import loader

    loader._register_one_plugin(loader.plugin_manifest("cnfree"))
    assert loader.provider_has_dataset("cnfree", "realtime") is True
    assert loader.provider_has_dataset("cnfree", "minute") is True
    assert loader.provider_has_dataset("cnfree", "depth5") is True
    assert loader.provider_has_dataset("cnfree", "financial") is False


def test_availability_is_true_without_key():
    assert cp.availability() == (True, "ok")


def test_manifest_declares_contract():
    from app.data_providers.custom import loader

    manifest = loader.plugin_manifest("cnfree")
    assert manifest is not None
    assert manifest["name"] == "cnfree"
    assert manifest["entry"] == "app.plugins.cnfree.provider:CnFreeProvider"
    assert manifest["check"] == "app.plugins.cnfree.provider:availability"
    assert manifest["runtime"] == "none"
    assert "api_key_env" not in manifest  # 无 Key 依赖
    assert {"realtime", "daily", "adj_factor", "minute"} <= set(manifest["datasets"])
    assert "TickFlow" in manifest["description"]  # 覆盖范围说明含回退语义


def test_loader_registers_cnfree_plugin():
    from app.data_providers.custom import loader

    manifest = loader.plugin_manifest("cnfree")
    loader._register_one_plugin(manifest)
    assert "cnfree" in loader._PLUGIN_STATUS
    assert loader._PLUGIN_STATUS["cnfree"]["available"] is True
    assert "cnfree" in loader._PROVIDERS
    provider = loader._PROVIDERS["cnfree"]
    assert provider.name == "cnfree"
    assert provider.builtin is True
    assert provider.minute_history_days == 5


# =====================================================================
# 设置页试拉
# =====================================================================


def test_test_dataset_realtime_preview(monkeypatch):
    fake = _FakeClient(
        snapshot={
            "sh600519": _sina_stock_fields(),
            "sz000001": _sina_stock_fields(by_index={0: "平安银行"}),
        }
    )
    p = _provider_with(monkeypatch, fake)
    out = p.test_dataset("realtime", ["600519.SH", "000001.SZ"])
    assert out["provider"] == "cnfree" and out["dataset"] == "realtime"
    assert out["rows"] == 2
    assert out["preview"][0]["symbol"] == "600519.SH"
    assert out["preview"][0]["change_pct"] == pytest.approx((1235.58 - 1243.88) / 1243.88)


def test_test_dataset_daily_preview_serializes_dates(monkeypatch):
    daily = {("sh600519", ""): [_tday("2026-09-29", "1244.6", "1235.58", "1245.87", "1230.88", "26366")]}
    p = _provider_with(monkeypatch, _FakeClient(daily=daily))
    out = p.test_dataset("daily", ["600519.SH"])
    assert out["rows"] == 1
    assert out["preview"][0]["date"] == "2026-09-29"  # date → ISO 字符串
    assert out["columns"][0] == "symbol"


def test_test_dataset_minute_preview_serializes_datetime(monkeypatch):
    minute = {"sh600519": [_mrow("202609291500", "1", "1", "1", "1", "10")]}
    p = _provider_with(monkeypatch, _FakeClient(minute=minute))
    out = p.test_dataset("minute", ["600519.SH"])
    assert out["rows"] == 1
    assert out["preview"][0]["datetime"].startswith("2026-09-29T15:00")


def test_test_dataset_adj_factor_preview(monkeypatch):
    _days, raw_rows, qfq_rows, event_day = _adj_fixture(
        ["10.00", "10.10", "9.80", "9.90", "10.05", "9.95"], 2, before=lambda c: c - 0.30
    )
    p = _provider_with(monkeypatch, _FakeClient(daily={("sh600519", ""): raw_rows, ("sh600519", "qfq"): qfq_rows}))
    out = p.test_dataset("adj_factor", ["600519.SH"])
    assert out["rows"] == 1
    assert out["preview"][0]["trade_date"] == event_day.isoformat()
    assert out["preview"][0]["ex_factor"] == pytest.approx(10.10 / 9.80)


def test_test_dataset_unsupported_reports_fallback(monkeypatch):
    p = _provider_with(monkeypatch, _FakeClient())
    for dataset in ("financial", "full_minute"):
        out = p.test_dataset(dataset)
        assert "error" in out and "回退" in out["error"]
    # depth5 已接入: 空客户端下无盘口数据, 但不再是"未接入"错误
    out = p.test_dataset("depth5")
    assert "error" not in out


def test_close_is_idempotent(monkeypatch):
    fake = _FakeClient()
    p = _provider_with(monkeypatch, fake)
    p.close()
    p.close()  # 二次调用不抛异常
    assert p._client is None
# =====================================================================
# 腾讯快照降级源: 样例数据 / client 层 / 映射口径
# =====================================================================


def _tencent_fields(**over) -> list[str]:
    """88 字段腾讯快照行(实测 2026-09-30 sh600519 收盘态, 与 tushare daily 交叉核对)。

    [1]名称 [3]最新 [4]昨收 [5]今开 [6]成交量(手) [9..28]五档价量 [30]时间
    [31]涨跌额 [32]涨跌%(2 位小数) [33]高 [34]低 [35]"价/量/成交额(元)" [37]额(万元)。
    """
    fields = [""] * 88
    fields[0] = "1"
    fields[1] = "贵州茅台"
    fields[2] = "600519"
    fields[3] = "1258.62"
    fields[4] = "1235.58"
    fields[5] = "1239.53"
    fields[6] = "38331"
    fields[7] = "21633"
    fields[8] = "16698"
    for k in range(5):
        fields[9 + k * 2] = f"{1258.0 + k:.2f}"
        fields[10 + k * 2] = str(1 + k)
        fields[19 + k * 2] = f"{1258.5 + k:.2f}"
        fields[20 + k * 2] = str(2 + k)
    fields[30] = "20260930161458"
    fields[31] = "23.04"
    fields[32] = "1.86"
    fields[33] = "1268.00"
    fields[34] = "1236.05"
    fields[35] = "1258.62/38331/4797246636"
    fields[36] = "38331"
    fields[37] = "479725"
    fields[38] = "0.31"
    fields[39] = "19.32"
    return _override_fields(fields, over)


def _tencent_line(code: str, fields: list[str]) -> str:
    return f'v_{code}="{"~".join(fields)}";'


def _tencent_body(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode("gbk")


def test_client_sina_403_raises_rate_limited(monkeypatch):
    """云服务器 IP 段被新浪 403 封禁: 必须抛限流类异常, 让 provider 熔断降级。"""
    monkeypatch.setattr(
        cc.httpx, "Client", lambda **kw: _FakeSinaHttp(b"Forbidden", status_code=403)
    )
    with pytest.raises(cc.CnFreeRateLimitedError):
        cc.CnFreeClient().sina_snapshot(["sh600519"])


def test_client_sina_500_stays_plain_error(monkeypatch):
    """非封禁类 HTTP 错误仍是 CnFreeError(软失败语义, 不该触发整段熔断)。"""
    monkeypatch.setattr(cc.httpx, "Client", lambda **kw: _FakeSinaHttp(b"", status_code=500))
    with pytest.raises(CnFreeError) as ei:
        cc.CnFreeClient().sina_snapshot(["sh600519"])
    assert not isinstance(ei.value, cc.CnFreeRateLimitedError)


def test_client_tencent_snapshot_decodes_gbk_and_drops_short_rows(monkeypatch):
    body = _tencent_body(
        _tencent_line("sh600519", _tencent_fields()),
        'v_sz000001="1~平安银行~000001";',  # 停牌/无效短行 → 必须丢弃
    )
    monkeypatch.setattr(cc.httpx, "Client", lambda **kw: _FakeSinaHttp(body))
    out = cc.CnFreeClient().tencent_snapshot(["sh600519", "sz000001"])
    assert set(out) == {"sh600519"}
    assert out["sh600519"][3] == "1258.62"


def test_client_tencent_snapshot_batches_by_60(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(cc, "_SNAPSHOT_BATCH", 2)
    monkeypatch.setattr(cc.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(cc.httpx, "Client", lambda **kw: _FakeSinaHttp(b""))
    cc.CnFreeClient().tencent_snapshot(["sh1", "sh2", "sh3", "sh4", "sh5"])
    assert len(sleeps) == 2  # 5 只 / 每批 2 → 3 批, 批间 sleep 2 次


def test_client_tencent_snapshot_waf_raises_rate_limited(monkeypatch):
    monkeypatch.setattr(
        cc.httpx,
        "Client",
        lambda **kw: _FakeSinaHttp(b"<html>challenge</html>", status_code=501),
    )
    with pytest.raises(cc.CnFreeRateLimitedError):
        cc.CnFreeClient().tencent_snapshot(["sh600519"])


def test_tencent_mapping_units_and_precision():
    """量纲: volume 原生手不再 /100; amount 取 [35] 尾段(元); change_pct 现算小数制。"""
    rec = cp._map_tencent_fields("sh600519", "600519.SH", _tencent_fields())
    assert rec["symbol"] == "600519.SH" and rec["name"] == "贵州茅台"
    assert rec["last_price"] == 1258.62 and rec["prev_close"] == 1235.58
    assert rec["open"] == 1239.53 and rec["high"] == 1268.00 and rec["low"] == 1236.05
    assert rec["volume"] == 38331.0  # 手, 与 tushare daily vol=38330.98 手同源
    assert rec["amount"] == 4797246636.0  # 元, = tushare amount 4797246.636 千元
    # 腾讯 [32] 只有 1.86(2 位), 现算更准 → 断言用的是 最新/昨收
    assert rec["change_pct"] == pytest.approx((1258.62 - 1235.58) / 1235.58)
    assert rec["change_amount"] == pytest.approx(23.04)
    assert rec["timestamp"] == _expect_beijing_ms(2026, 9, 30, 16, 14, 58)
    assert rec["amplitude"] is None and rec["turnover_rate"] is None


def test_tencent_mapping_rejects_short_row():
    assert cp._map_tencent_fields("sh600519", "600519.SH", ["1", "x"] * 17) is None
    bad = _tencent_fields(by_index={3: ""})  # 最新价缺失 → 不伪造
    assert cp._map_tencent_fields("sh600519", "600519.SH", bad) is None


# =====================================================================
# 新浪封禁 → 腾讯自选池降级(provider 层熔断与覆盖范围)
# =====================================================================


def _fallback_provider(monkeypatch, universe, watch, tencent_rows=None, sina_error=None):
    """构造"新浪被封"场景的 provider: sina 抛限流, 腾讯可返回预置行。"""
    fake = _FakeClient(
        snapshot={},
        error=sina_error
        or cc.CnFreeRateLimitedError("新浪快照疑似被封禁或限流 HTTP 403"),
        tencent_rows={"sh600519": _tencent_fields()} if tencent_rows is None else tencent_rows,
    )
    p = _provider_with(monkeypatch, fake, universe={"stock": universe, "etf": []})
    monkeypatch.setattr(p, "_load_watchlist", lambda: watch)
    return p, fake


def test_realtime_sina_blocked_falls_back_to_tencent(monkeypatch):
    p, fake = _fallback_provider(monkeypatch, ["600519.SH"], ["600519.SH"])
    records = p.get_realtime()
    assert [r["symbol"] for r in records] == ["600519.SH"]
    assert p.realtime_source == "tencent"
    assert len(fake.sina_calls) == 1 and len(fake.tencent_calls) == 1


def test_realtime_stays_blocked_within_cooldown(monkeypatch):
    """熔断期内不再打新浪 — 省掉每轮一次必然 403 的请求。"""
    p, fake = _fallback_provider(monkeypatch, ["600519.SH"], ["600519.SH"])
    p.get_realtime()
    fake.sina_calls.clear()
    records = p.get_realtime()
    assert records and fake.sina_calls == []
    assert len(fake.tencent_calls) == 2


def test_realtime_scopes_to_watchlist_not_full_market(monkeypatch):
    """降级覆盖范围: 只拉自选 ∩ universe(腾讯 60 只/请求撑不起全市场轮询)。"""
    p, fake = _fallback_provider(
        monkeypatch,
        ["600519.SH", "000001.SZ", "300750.SZ"],
        ["600519.SH"],
        tencent_rows={
            "sh600519": _tencent_fields(),
            "sz000001": _tencent_fields(by_index={1: "平安银行", 2: "000001"}),
        },
    )
    records = p.get_realtime()
    assert fake.tencent_calls[0] == ["sh600519"]
    assert [r["symbol"] for r in records] == ["600519.SH"]


def test_realtime_fallback_with_empty_watchlist_returns_empty(monkeypatch, caplog):
    """自选为空 → 明确不返回数据(不静默给全市场假快照), 且给出可读告警。"""
    p, fake = _fallback_provider(monkeypatch, ["600519.SH"], [])
    with caplog.at_level("WARNING"):
        assert p.get_realtime() == []
    assert fake.tencent_calls == []
    assert "自选为空" in caplog.text


def test_realtime_fallback_caps_symbols_per_round(monkeypatch):
    monkeypatch.setattr(cp, "_REALTIME_MAX_SYMBOLS", 2)
    p, fake = _fallback_provider(
        monkeypatch,
        ["600519.SH", "000001.SZ", "300750.SZ"],
        ["600519.SH", "000001.SZ", "300750.SZ"],
        tencent_rows={},
    )
    p.get_realtime()
    assert len(fake.tencent_calls[0]) == 2


def test_realtime_both_sources_blocked_soft_returns_empty(monkeypatch):
    """腾讯也被 WAF 掐 → 软返回 [], 不抛异常打断 quote_service 轮询线程。"""
    p, _ = _fallback_provider(monkeypatch, ["600519.SH"], ["600519.SH"])
    p._sina_blocked_until = time.time() + 100
    p._get_client().tencent_blocked = True
    assert p.get_realtime() == []


def test_realtime_recovers_sina_after_cooldown(monkeypatch):
    """熔断到期后自动回新浪(全市场), 不需要重启进程。"""
    p, fake = _fallback_provider(monkeypatch, ["600519.SH"], ["600519.SH"])
    p._sina_blocked_until = time.time() - 1  # 已过期
    fake.error = None
    fake.snapshot = {"sh600519": _sina_stock_fields()}
    records = p.get_realtime()
    assert p.realtime_source == "sina"
    assert [r["symbol"] for r in records] == ["600519.SH"]
    assert fake.tencent_calls == []


def test_indices_fall_back_to_tencent_when_blocked(monkeypatch):
    p, fake = _fallback_provider(monkeypatch, [], [])
    p._sina_blocked_until = time.time() + 100
    p._get_client().tencent_rows = {
        "sh000001": _tencent_fields(by_index={1: "上证指数", 3: "3842.19"})
    }
    recs = p.get_realtime_indices(["000001.SH"])
    assert recs is not None and recs[0]["last_price"] == 3842.19
    assert fake.sina_calls == [] and len(fake.tencent_calls) == 1


def test_indices_tencent_failure_returns_none(monkeypatch):
    """降级仍失败 → None(上层保留上轮指数缓存), 不能返回 [] 把缓存清空。"""
    p, _ = _fallback_provider(monkeypatch, [], [])
    p._sina_blocked_until = time.time() + 100
    p._get_client().tencent_rows_error = CnFreeError("腾讯快照 HTTP 500")
    assert p.get_realtime_indices(["000001.SH"]) is None


def test_indices_sina_block_then_tencent_retry(monkeypatch):
    """首轮新浪 403: 当场降级腾讯出数据, 不空转一轮。"""
    p, fake = _fallback_provider(monkeypatch, [], [])
    p._get_client().tencent_rows = {"sh000001": _tencent_fields(by_index={1: "上证指数"})}
    recs = p.get_realtime_indices(["000001.SH"])
    assert recs and recs[0]["symbol"] == "000001.SH"
    assert p.realtime_source == "tencent"
    assert len(fake.sina_calls) == 1 and len(fake.tencent_calls) == 1


def test_watchlist_reader_missing_file_returns_empty(monkeypatch, tmp_path):
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    assert CnFreeProvider()._load_watchlist() == []


def test_watchlist_reader_symbols_sorted_unique(monkeypatch, tmp_path):
    from app.config import settings

    wd = tmp_path / "user_data"
    wd.mkdir(parents=True)
    pl.DataFrame({"symbol": ["300750.SZ", "600519.SH", "600519.SH"]}).write_parquet(
        wd / "watchlist.parquet"
    )
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    assert CnFreeProvider()._load_watchlist() == ["300750.SZ", "600519.SH"]


def test_test_dataset_reports_active_realtime_source(monkeypatch):
    p, _ = _fallback_provider(monkeypatch, ["600519.SH"], ["600519.SH"])
    out = p.test_dataset("realtime", ["600519.SH"])
    assert out["realtime_source"] == "tencent" and out["rows"] == 1
