"""免费数据链(新浪/腾讯) HTTP 客户端。

职责: 全部 HTTP 细节 — 公共 headers、新浪 GBK 解码与行解析、腾讯 K 线信封解包、
分页(日K 800 根/页、分钟 320 根/页, 均从窗口末端向过去推进)与请求间隔限速。
不知道 provider / services 层, 不做字段映射与单位换算。

实测契约(2026-09-29):
- 新浪 hq.sinajs.cn 必须带 Referer finance.sina.com.cn 与浏览器 UA, 响应 GBK;
  行格式 `var hq_str_<code>="<f0>,<f1>,..."`, 停牌/无效代码返回空串;
  股票/ETF 34 字段、部分指数 33 字段, 尾部有漂移(日期/时间定位交给 provider)。
- 新浪对云服务器 IP 段直接 403(实测 2026-09-30 阿里云杭州 ECS: 带 Referer、
  走 http/https、换 hq2.sinajs.cn 与 rn= 路径全部 403, 本机家宽同请求正常)。
  这是 IP 段封禁而非限流退避, 重试无意义 → sina_snapshot 抛
  CnFreeRateLimitedError, 由 provider 熔断并降级腾讯快照。
- 腾讯 qt.gtimg 快照(q=): 与盘口同端点同布局, 88 字段/只; [1]名称 [3]最新
  [4]昨收 [5]今开 [6]成交量(手) [30]时间 yyyyMMddHHMMSS [33]最高 [34]最低
  [35]"价/量/成交额(元)"。量额量纲与 tushare daily 交叉验证一致(600519 同日:
  腾讯 [6]=38331 手 / [35]尾段 4797246636 元, tushare vol=38330.98 手 /
  amount=4797246.636 千元)。
- 腾讯 fqkline: param={code},day,{start},{end},{count},{fqt}; fqt 留空 = 不复权原始价
  (payload.data.{code}.day), fqt=qfq 取 .qfqday(退化读 .day); count 根自窗口末端
  向前取(count=800 长窗口实测返回最新 800 根)。实测 count=2000 仍正常返回,
  3000 时 data.{code} 退化为 list; 分页保守按 800 根推进, 退化结构视为空终止。
- 腾讯 mkline: param={code},m1,{end_yyyymmddHHMM},{count}; 端 datetime 空串 = 最新,
  非空 = 向该时刻之前取(实测可向后翻页), 单请求上限实测 320 根(count=2000 仍返 320)。
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, timedelta

import httpx

logger = logging.getLogger(__name__)

SINA_SNAPSHOT_URL = "https://hq.sinajs.cn/list="
TENCENT_SNAPSHOT_URL = "https://qt.gtimg.cn/q="
TENCENT_FQKLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
TENCENT_MKLINE_URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"

DEFAULT_HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0",
}
TENCENT_HEADERS = {
    "Referer": "https://finance.qq.com",
    "User-Agent": "Mozilla/5.0",
}

_SINA_LINE_RE = re.compile(r'var\s+hq_str_(?P<code>[a-z0-9]+)="(?P<fields>[^"]*)"')

_SINA_BATCH = 800  # 新浪单批代码数(实测 306 只单请求全回, 800 只参数长度仍安全)
_SINA_BATCH_INTERVAL_S = 0.1  # 批间隔, 降低触发风控概率
# qt.gtimg 快照/盘口单批代码数(88 字段/只, URL 长度取保守值)。实测 2026-09-30:
# 60 只/请求稳定, 全市场 5562 只需 ~93 请求/轮(≈46s), 因此快照只用于小标的池。
_SNAPSHOT_BATCH = 60
_KLINE_PAGE = 800  # 腾讯日K分页粒度(实测 2000 仍可用, 保守取 800)
_KLINE_MAX_PAGES = 40  # 页数上限(防御 count 异常导致死循环)
_MINUTE_PAGE = 320  # 腾讯 m1 单请求上限(实测 count=2000 仍返回 320)
_MINUTE_MAX_PAGES = 40
# 分页请求间隔。实测 2026-09-30: ~10 req/s 持续数千请求后 web.ifzq.gtimg.cn
# 返回 501 + JS 挑战页(WAF 反爬), 降速到 ~3 req/s。
_REQ_INTERVAL_S = 0.3


class CnFreeError(Exception):
    """免费数据链接口错误(网络失败 / HTTP 非 200 / 响应非 JSON)。"""


class CnFreeRateLimitedError(CnFreeError):
    """腾讯 WAF 限流(JS 挑战页/403/429/501)。上游需熔断, 不要继续打请求。"""


def _is_waf_challenge(status_code: int, text: str) -> bool:
    """识别腾讯 WAF 挑战页: 501/403/429 或返回 HTML(正常应为 JSON)。"""
    if status_code in (403, 429, 501):
        return True
    head = text.lstrip()[:200].lower()
    return head.startswith("<!doctype") or head.startswith("<html")


class CnFreeClient:
    """新浪/腾讯公开端点客户端 (线程安全: httpx.Client 可并发复用)。"""

    def __init__(self, timeout: float = 15.0) -> None:
        self._http = httpx.Client(headers=DEFAULT_HEADERS, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    # ---- 新浪实时快照 ----
    def sina_snapshot(self, codes: list[str]) -> dict[str, list[str]]:
        """批量新浪实时快照。返回 {新浪代码: 字段列表}。

        每批 _SINA_BATCH 只、批间 _SINA_BATCH_INTERVAL_S。停牌/无效代码(空串行)
        不进结果。任一批请求失败抛 CnFreeError(调用方按软失败语义处理)。
        """
        out: dict[str, list[str]] = {}
        for i in range(0, len(codes), _SINA_BATCH):
            batch = codes[i : i + _SINA_BATCH]
            if i:
                time.sleep(_SINA_BATCH_INTERVAL_S)
            try:
                resp = self._http.get(SINA_SNAPSHOT_URL + ",".join(batch))
            except httpx.HTTPError as e:
                raise CnFreeError(f"新浪快照请求失败: {e}") from e
            if resp.status_code != 200:
                # 403/429/501 属"该来源不再可用"(云服务器 IP 段被封或风控),
                # 抛限流类异常让 provider 熔断, 避免每轮都空打一次请求。
                if _is_waf_challenge(resp.status_code, ""):
                    raise CnFreeRateLimitedError(
                        f"新浪快照疑似被封禁或限流 HTTP {resp.status_code}"
                    )
                raise CnFreeError(f"新浪快照 HTTP {resp.status_code}")
            # 新浪响应为 GBK 编码, 必须按 bytes 解码(resp.text 会用错误的默认编码)
            text = resp.content.decode("gbk", errors="replace")
            for m in _SINA_LINE_RE.finditer(text):
                fields = m.group("fields").split(",")
                # 空串 = 停牌/无效代码, 跳过且不告警
                if not fields or fields[0] == "":
                    continue
                out[m.group("code")] = fields
        return out

    # ---- 腾讯实时快照 ----
    def tencent_snapshot(self, codes: list[str]) -> dict[str, list[str]]:
        """批量腾讯实时快照 → {腾讯代码: 字段列表}。

        与 tencent_depth_batch 同一端点同一布局(88 字段/只), 但按实时所需的
        [35]"价/量/额" 段做长度守卫: 短行(<36 字段, 停牌/无效代码)直接跳过。
        每批 _SNAPSHOT_BATCH 只、批间 _REQ_INTERVAL_S; 疑似 WAF/封禁抛
        CnFreeRateLimitedError, 其余非 200 抛 CnFreeError。
        """
        out: dict[str, list[str]] = {}
        for i in range(0, len(codes), _SNAPSHOT_BATCH):
            batch = codes[i : i + _SNAPSHOT_BATCH]
            if i:
                time.sleep(_REQ_INTERVAL_S)
            try:
                resp = self._http.get(
                    TENCENT_SNAPSHOT_URL + ",".join(batch), headers=TENCENT_HEADERS
                )
            except httpx.HTTPError as e:
                raise CnFreeError(f"腾讯快照请求失败: {e}") from e
            body = getattr(resp, "text", "") or ""
            if _is_waf_challenge(resp.status_code, body):
                raise CnFreeRateLimitedError(
                    f"腾讯快照疑似被 WAF 限流 HTTP {resp.status_code}"
                )
            if resp.status_code != 200:
                raise CnFreeError(f"腾讯快照 HTTP {resp.status_code}")
            text = resp.content.decode("gbk", errors="replace")
            for line in text.strip().split("\n"):
                if '="' not in line:
                    continue
                var = line.split("=", 1)[0].rsplit("_", 1)[-1].strip()
                fields = line.split('="', 1)[1].rstrip('";').split("~")
                if var and len(fields) >= 36:
                    out[var] = fields
        return out

    # ---- 腾讯日K ----
    def tencent_daily(
        self, sina_code: str, start_d: date, end_d: date, fqt: str = ""
    ) -> list[list]:
        """日K原始行(未做单位换算), 按日期升序返回, 不含请求窗口之外的行。

        fqt: "" = 不复权原始价(payload.data.{code}.day); "qfq" = 前复权
        (.qfqday, 退化读 .day)。分页粒度 _KLINE_PAGE 根(实测服务端 2000 仍可用,
        保守取 800), count 根自窗口末端向前取 → 分页从 end_d 起向过去推进
        (下一段终点 = 本页最早日期的前一天), 空页 / 不足整页 / 覆盖到 start_d 终止。
        """
        out: list[list] = []
        seen: set[str] = set()
        cursor = end_d
        for page in range(_KLINE_MAX_PAGES):
            if page:
                time.sleep(_REQ_INTERVAL_S)
            param = f"{sina_code},day,{start_d.isoformat()},{cursor.isoformat()},{_KLINE_PAGE},{fqt}"
            node = self._tencent_payload(TENCENT_FQKLINE_URL, param)
            key = "qfqday" if fqt == "qfq" else "day"
            rows = node.get(key) if isinstance(node, dict) else None
            if not rows and fqt == "qfq":
                rows = node.get("day") if isinstance(node, dict) else None  # qfq 退化读 day
            if not rows:
                break
            fresh = 0
            first_date_str = ""
            for row in rows:
                if not isinstance(row, list) or not row:
                    continue
                date_str = str(row[0])
                if date_str in seen:
                    continue
                seen.add(date_str)
                out.append(row)
                fresh += 1
                if not first_date_str or date_str < first_date_str:
                    first_date_str = date_str
            if fresh == 0:
                break
            if len(rows) < _KLINE_PAGE or first_date_str <= start_d.isoformat():
                break
            first_d = date.fromisoformat(first_date_str)
            cursor = min(cursor, first_d) - timedelta(days=1)
        out.sort(key=lambda r: str(r[0]))
        return [r for r in out if start_d.isoformat() <= str(r[0]) <= end_d.isoformat()]

    # ---- 腾讯分钟K ----
    def tencent_minute(
        self, sina_code: str, end_dt: datetime | None, max_pages: int
    ) -> list[list]:
        """m1 分钟K原始行(未做单位换算), 按时间升序返回。

        end_dt 为 None 时取最新; 否则取该时刻(yyyymmddHHMM)之前的若干页。
        每页 _MINUTE_PAGE 根, 最多 max_pages 页(由调用方按窗口深度估算)。
        """
        out: list[list] = []
        seen: set[str] = set()
        cursor = end_dt.strftime("%Y%m%d%H%M") if end_dt is not None else ""
        pages = max(1, min(int(max_pages), _MINUTE_MAX_PAGES))
        for page in range(pages):
            if page:
                time.sleep(_REQ_INTERVAL_S)
            param = f"{sina_code},m1,{cursor},{_MINUTE_PAGE}"
            node = self._tencent_payload(TENCENT_MKLINE_URL, param)
            rows = node.get("m1") if isinstance(node, dict) else None
            if not rows:
                break
            fresh = 0
            first_label = ""
            for row in rows:
                if not isinstance(row, list) or not row:
                    continue
                label = str(row[0])
                if label in seen:
                    continue
                seen.add(label)
                out.append(row)
                fresh += 1
                if not first_label or label < first_label:
                    first_label = label
            if fresh == 0 or len(rows) < _MINUTE_PAGE or not first_label:
                break
            cursor = first_label
        out.sort(key=lambda r: str(r[0]))
        return out

    def tencent_depth_batch(self, sina_codes: list[str]) -> dict[str, list[str]]:
        """qt.gtimg 五档盘口原始行 → {sina_code: 字段列表}。

        单批约 60 只(实测 88 字段/只, URL 长度与上游限制取保守值);
        停牌/无效代码返回短行或空串行, 由调用方按字段数过滤。
        字段布局(0 起): [3]最新 [4]昨收 [9]买一价 [10]买一量 [11]买二价 [12]买二量
        ... [17]买五价 [18]买五量 [19]卖一价 [20]卖一量 ... [27]卖五价 [28]卖五量
        [30]时间 yyyyMMddHHMMSS; 价量单位: 价=元, 量=手(与深度契约一致, 不换算)。
        """
        out: dict[str, list[str]] = {}
        resp = self._http.get(
            TENCENT_SNAPSHOT_URL + ",".join(sina_codes), headers=TENCENT_HEADERS
        )
        if _is_waf_challenge(resp.status_code, getattr(resp, "text", "") or ""):
            raise CnFreeRateLimitedError(
                f"腾讯盘口疑似被 WAF 限流 HTTP {resp.status_code}"
            )
        if resp.status_code != 200:
            raise CnFreeError(f"腾讯盘口 HTTP {resp.status_code}")
        text = resp.content.decode("gbk", errors="replace")
        for line in text.strip().split("\n"):
            if '="' not in line:
                continue
            var = line.split("=", 1)[0].rsplit("_", 1)[-1].strip()
            fields = line.split('="', 1)[1].rstrip('";').split("~")
            if var and len(fields) >= 29:
                out[var] = fields
        return out

    # ---- 内部 ----
    def _tencent_payload(self, url: str, param: str) -> dict:
        """GET 腾讯 K 线端点并解出 payload.data.{code} 节点; 无数据返回 {}。

        实测: count 超上限时 data.{code} 会退化为 list 等非预期结构 → 视为空,
        由分页循环终止, 不抛异常(空响应终止是分页的正常结束条件)。
        """
        try:
            resp = self._http.get(url, params={"param": param})
        except httpx.HTTPError as e:
            raise CnFreeError(f"腾讯K线请求失败 ({param[:60]}): {e}") from e
        body = getattr(resp, "text", "") or ""
        if _is_waf_challenge(resp.status_code, body):
            raise CnFreeRateLimitedError(
                f"腾讯K线疑似被 WAF 限流 HTTP {resp.status_code} ({param[:60]})"
            )
        if resp.status_code != 200:
            raise CnFreeError(f"腾讯K线 HTTP {resp.status_code} ({param[:60]})")
        try:
            payload = resp.json()
        except ValueError as e:
            if _is_waf_challenge(resp.status_code, body):
                raise CnFreeRateLimitedError(
                    f"腾讯K线返回挑战页而非 JSON (疑似限流) ({param[:60]})"
                ) from e
            raise CnFreeError(f"腾讯K线响应不是 JSON ({param[:60]})") from e
        if not isinstance(payload, dict):
            return {}
        data = payload.get("data")
        if not isinstance(data, dict):
            return {}
        code = param.split(",", 1)[0]
        node = data.get(code)
        return node if isinstance(node, dict) else {}
