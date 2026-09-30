"""免费数据链(新浪/腾讯公开端点)内置数据源 provider。

方法签名对齐 custom.GenericHTTPProvider(service 分流点按这套签名调用),
声明 datasets = [realtime, daily, adj_factor, minute]; 财务/五档无免费源,
provider_has_dataset 为 False 自动回退 TickFlow。

实现数据集:
  - realtime     A股股票+ETF 快照。正常走新浪 hq.sinajs.cn(标的池来自本地
                 instruments parquet, 800 只/批, 全市场); 新浪对云服务器 IP 段
                 恒 403(2026-09-30 阿里云 ECS 实测) → 熔断 _SINA_BLOCK_S 秒并降级
                 腾讯 qt.gtimg 快照, 此时覆盖范围收窄为自选池(上限 _REALTIME_MAX_SYMBOLS
                 只), 生效源记在 provider.realtime_source。指数经 get_realtime_indices
                 补拉, 降级期同样切腾讯端点。
  - daily        日K不复权原始价(腾讯 fqkline, 股票+ETF 同端点, 800 根/页分段)
  - adj_factor   除权因子(腾讯 qfq/raw 推导, 事件级稀疏输出, 见下)
  - minute       分钟K(腾讯 mkline m1, 单请求上限 320 根, 浅历史, minute_history_days=5)

单位与口径 (CONTRIBUTING §3, 红线):
  - 新浪 volume 单位为股/份 → 手 = floor(/100)。指数例外(2026-09-30 实测 6 只指数):
    上交所指数(sh 前缀)行情量纲已是手, 原样透传; 深交所/北交所指数(sz/bj 前缀)
    为股, /100 折手 — 上证/沪深300/科创综指的 volume 与腾讯指数日K同值,
    深成指/创业板指为腾讯日K的 100 倍。折手后 realtime 与日K口径一致。
    腾讯日K/分钟 volume 已是手, 原样透传。
  - 新浪 amount 为元, 直接透传; 腾讯日K行内无成交额(第 7 列实测为分红注释 dict,
    出现数值时按万元 x10000 转元, 其余一律 None, 不伪造); 分钟 amount 恒 None。
  - change_pct 小数制 = change_amount / prev_close(新浪无现成涨跌幅字段, 由价格推导)。
  - 分钟 datetime 为北京墙钟 naive(标签 yyyymmddHHMM 即北京墙钟)。
  - 日K原始价不复权; 前复权一律由 indicators.pipeline 用本地因子计算。

除权因子推导(实测腾讯 qfq 为"减法+乘法"混合仿射口径, 2026-09-30 复核):
  段内 qfq 满足仿射关系 qfq = a·raw + b(a/b 段内恒定: 纯分红段 a=1、b=-ΣD;
  送转之后的段 a≠1, 实测残差 ≤0.001)。事件边界取自腾讯日K行自带的除权
  注释(dict, 含 cqr, 实测分红与送转除权日均标注); 对每一边界用相邻两段的
  仿射线求除权参考价 ref(连续性 a_b·raw_prev + b_b = a_a·ref + b_a),
  ex_factor = raw_prev / ref。纯分红时 a 相消, 退化为交易所公式
  raw_prev/(raw_prev - D), 与 fuyao/event-dump 语义一致(分红因子 > 1)。
  不做纯价格残差扫描(任务书原拟的 qfq/raw 比值扫描, 实测不可用):
  600519 503 个交易日逐日比值抖动 ~1e-3 >> 1e-4 容差, 扫描产出 336 个伪事件
  (真实 4 个); 且事件日 cum 上跳(qfq/raw → 1), 任务书公式 cum_prev/cum_t < 1,
  方向与 enriched 管道(pre/post 比值, 分红因子 > 1)相反 — 双重不可用。
  实测验证: 纯分红(600519 四次)段内 qfq-raw 恒定、事件步长恰为注释 fh_sh/10;
  派+转混合(sz300661 "10派2元转3股")段内斜率 = 1/1.3、边界因子与交易所公式
  一致(1e-6)。宁可漏不可错: 若腾讯存在未注释事件则该事件缺失(有界缺口,
  不产出错误因子), 推导失败的注释事件有告警日志。
"""

from __future__ import annotations

import calendar
import contextlib
import logging
import math
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import polars as pl

from app.data_providers.normalizer import normalize_daily
from app.plugins.cnfree import client as cnfree_client
from app.plugins.cnfree.client import (
    CnFreeClient,
    CnFreeError,
    CnFreeRateLimitedError,
)

logger = logging.getLogger(__name__)

# 只声明真实提供的数据集; 其余数据集 provider_has_dataset 返回 False → 回退 tickflow
_DATASETS = ("realtime", "daily", "adj_factor", "minute", "depth5")

_HIST_SYMBOL_BATCH = 50  # 日K/除权/分钟按标的分批, 每批回调一次 on_chunk_done
# 逐标的请求间隔。实测 2026-09-30: 0.1s(~10 req/s) 持续数千请求触发腾讯 WAF
# JS 挑战页(501), 降到 2 req/s; 全市场一晚 daily+adj ≈ 1.7 万请求 ≈ 2.5h, 可接受。
_SYM_INTERVAL_S = 0.5
_SYM_FAIL_BREAK = 20  # 连续 N 个标的疑似限流 → 熔断本轮, 不再空打剩余标的
_ADJ_BACKFILL_DAYS = 15  # 除权推导向前多取的日历日(保证窗口首日事件有前一日)
_ADJ_FETCH_HORIZON_DAYS = 365  # 推导跨度下限: 首装全量回填(见 get_adj_factors docstring)
_ADJ_MIN_ALIGNED_DAYS = 3  # 对齐日期数低于此无法推导
_ADJ_IDENTITY_EPS = 0.002  # 最新段单日 qfq≈raw 判定恒等线的容差
_ADJ_FACTOR_MIN = 0.25  # 因子合理界(超出视为推导失败, 剔除并告警)
_ADJ_FACTOR_MAX = 4.0
_MINUTE_MAX_PAGES = 40
_DEPTH_BATCH = 60  # qt.gtimg 单批代码数(88 字段/只, URL 长度取保守值)
_DEPTH_BATCH_INTERVAL_S = 0.3

_SINA_SUFFIX = {".SH": "sh", ".SZ": "sz", ".BJ": "bj"}
_SINA_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_SINA_TIME_RE = re.compile(r"\d{2}:\d{2}:\d{2}")

# 新浪被封禁后的熔断时长(秒)。实测 2026-09-30: 云服务器 IP 段 403 是持续性封禁,
# 每轮都重试只会白打一次请求并拖慢整轮快照; 熔断期内直接走腾讯。
_SINA_BLOCK_S = 900.0
# 降级到腾讯快照时的单轮标的上限。腾讯 qt.gtimg 实测 60 只/请求安全(见 client),
# 300 只 = 5 个请求 ≈ 1.5s 一轮; 全市场 5000+ 只按同粒度需 ~90 请求 ≈ 46s,
# 且实测连续高频请求会被 WAF 掐(东财同场景已实测断连), 故降级期只覆盖自选池。
_REALTIME_MAX_SYMBOLS = 300


def _naive_beijing(dt: datetime | None) -> datetime | None:
    """调用方 start/end 可能带时区(kline_sync 传 aware), 本插件输出与过滤统一
    北京墙钟 naive 口径 — 入口先归一, 避免 naive 与 aware 比较抛 TypeError。"""
    if dt is not None and dt.tzinfo is not None:
        from zoneinfo import ZoneInfo

        dt = dt.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    return dt

ADJ_SCHEMA = {"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64}
MINUTE_SCHEMA = {
    "symbol": pl.String,
    "datetime": pl.Datetime("us"),
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
}


def availability() -> tuple[bool, str]:
    """loader 启动自检: 纯公开端点, 无 Key 依赖, 恒可用。不做联网探测。不抛异常。"""
    return True, "ok"


@dataclass
class _CnFreeConfig:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset 能识别本 provider。"""

    name: str = "cnfree"
    display_name: str = "免费数据链(新浪/腾讯)"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _parse_depth_fields(fields: list[str]) -> dict | None:
    """qt.gtimg 原始字段 → 标准盘口字典; 布局不符返回 None。

    [9]买一价 [10]买一量 ... [17]买五价 [18]买五量 [19]卖一价 [20]卖一量
    ... [27]卖五价 [28]卖五量, 量原生为手; [30]时间 yyyyMMddHHMMSS(北京墙钟)。
    停牌/异常行五档价全 0 → 视为无盘口返回 None, 不伪造。
    """
    if len(fields) < 29:
        return None
    bid_prices = [_to_float(fields[9 + k * 2]) for k in range(5)]
    bid_volumes = [_to_float(fields[10 + k * 2]) for k in range(5)]
    ask_prices = [_to_float(fields[19 + k * 2]) for k in range(5)]
    ask_volumes = [_to_float(fields[20 + k * 2]) for k in range(5)]
    if any(p is None for p in bid_prices + ask_prices):
        return None
    if not any(p > 0 for p in bid_prices + ask_prices):
        return None  # 停牌/无盘口: 五档全 0
    timestamp: int | None = None
    if len(fields) > 30:
        with contextlib.suppress(ValueError):
            ts = datetime.strptime(fields[30], "%Y%m%d%H%M%S")
            timestamp = int(
                ts.replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000
            )
    return {
        "bid_prices": bid_prices,
        "bid_volumes": bid_volumes,
        "ask_prices": ask_prices,
        "ask_volumes": ask_volumes,
        "timestamp": timestamp,
    }


def _sina_code(symbol: str) -> str | None:
    """600519.SH → sh600519; 未知后缀/裸代码返回 None(调用方跳过, 不猜交易所)。"""
    text = str(symbol or "").strip().upper()
    for suffix, prefix in _SINA_SUFFIX.items():
        if text.endswith(suffix):
            return prefix + text[: -len(suffix)]
    return None


def _beijing_naive_to_ms(dt: datetime) -> int:
    """北京墙钟 naive datetime → epoch 毫秒(与本机时区无关)。"""
    return (calendar.timegm(dt.timetuple()) - 28_800) * 1000


def _tail_datetime(fields: list[str]) -> tuple[str, str] | None:
    """从行尾向前定位日期/时间(股票/ETF 34 字段、部分指数 33 字段, 尾部字段有漂移)。"""
    for i in range(len(fields) - 2, 0, -1):
        if _SINA_DATE_RE.fullmatch(fields[i]) and _SINA_TIME_RE.fullmatch(fields[i + 1]):
            return fields[i], fields[i + 1]
    return None


def _map_sina_fields(sina_code: str, symbol: str, fields: list[str], is_index: bool = False) -> dict | None:
    """新浪快照字段 → 内部 realtime record。缺失按口径推导, 不启发式伪造。

    字段: [0]名称 [1]开 [2]昨收 [3]最新 [4]高 [5]低 [6]买一 [7]卖一
          [8]成交量 [9]成交额(元) ... 尾部含日期与时间。
    成交量量纲: 股票/ETF 为股/份(/100 折手); 上交所指数(sh 前缀)已是手,
    深交所/北交所指数(sz/bj 前缀)为股(/100) — 均为实测口径, 非启发式。
    """
    if len(fields) < 10:
        return None
    last = _to_float(fields[3])
    if last is None:
        return None
    prev = _to_float(fields[2])
    change_amount = last - prev if prev is not None else None
    # 契约: change_pct 小数制, prev 为 0/缺失时 None
    change_pct = (
        change_amount / prev if change_amount is not None and prev not in (None, 0) else None
    )
    volume = _to_float(fields[8])  # 股/份; 上交所指数行情已是手
    if volume is None:
        volume_lots = None
    elif is_index and sina_code.startswith("sh"):
        volume_lots = volume  # 上交所指数量纲已是手, 不再 /100
    else:
        volume_lots = math.floor(volume / 100.0)  # 股/份 → 手
    tail = _tail_datetime(fields)
    fetched_ms = int(time.time() * 1000)
    if tail is not None:
        with contextlib.suppress(ValueError):
            # 尾部时间解析失败退本地时间
            fetched_ms = _beijing_naive_to_ms(
                datetime.strptime(f"{tail[0]} {tail[1]}", "%Y-%m-%d %H:%M:%S")
            )
    return {
        "symbol": symbol,
        "name": fields[0] or None,
        "last_price": last,
        "prev_close": prev,
        "open": _to_float(fields[1]),
        "high": _to_float(fields[4]),
        "low": _to_float(fields[5]),
        "volume": volume_lots,
        "amount": _to_float(fields[9]),  # 元
        "change_pct": change_pct,
        "change_amount": change_amount,
        "amplitude": None,  # 未提供, 不启发式计算
        "turnover_rate": None,  # 需股本口径 (§3.4), 交给 enriched 管道
        "timestamp": fetched_ms,
        "session": None,
    }


def _map_tencent_fields(
    tencent_code: str, symbol: str, fields: list[str], is_index: bool = False
) -> dict | None:
    """腾讯快照字段 → 内部 realtime record(新浪被封禁时的降级源)。

    字段: [1]名称 [3]最新 [4]昨收 [5]今开 [6]成交量(手) [30]时间 yyyyMMddHHMMSS
          [33]高 [34]低 [35]"价/量/成交额(元)"。
    量纲为实测(见 client 顶部): 成交量原生"手"不再 /100, 成交额原生"元";
    指数 [6] 与股票同一字段位, 按手透传(与新浪 sh 指数口径一致)。
    change_pct 一律由 最新/昨收 现算(腾讯 [32] 只保留 2 位小数, 精度低于现算),
    amplitude / turnover_rate 留 None — 换手率需历史股本口径(§3.4), 交 enriched 管道。
    """
    if len(fields) < 36:
        return None
    last = _to_float(fields[3])
    if last is None:
        return None
    prev = _to_float(fields[4])
    change_amount = last - prev if prev is not None else None
    change_pct = (
        change_amount / prev if change_amount is not None and prev not in (None, 0) else None
    )
    amount: float | None = None
    parts = str(fields[35]).split("/")
    if len(parts) >= 3:
        amount = _to_float(parts[2])
    timestamp = int(time.time() * 1000)
    with contextlib.suppress(ValueError):
        ts = datetime.strptime(fields[30], "%Y%m%d%H%M%S")
        timestamp = int(ts.replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)
    return {
        "symbol": symbol,
        "name": fields[1] or None,
        "last_price": last,
        "prev_close": prev,
        "open": _to_float(fields[5]),
        "high": _to_float(fields[33]),
        "low": _to_float(fields[34]),
        "volume": _to_float(fields[6]),  # 手, 原生量纲
        "amount": amount,  # 元
        "change_pct": change_pct,
        "change_amount": change_amount,
        "amplitude": None,
        "turnover_rate": None,
        "timestamp": timestamp,
        "session": None,
    }


def _daily_close(row: list) -> tuple[date | None, float | None]:
    """腾讯日K行 [日期, open, close, high, low, ...] → (date, close)。"""
    if not isinstance(row, list) or len(row) < 3:
        return None, None
    try:
        d = date.fromisoformat(str(row[0]))
    except ValueError:
        return None, None
    return d, _to_float(row[2])


def _daily_amount(row: list) -> float | None:
    """腾讯日K成交额: 行内第 7 列。实测该列多为分红注释 dict → None;
    为数值(或数值字符串)时按万元 x10000 转元; 其余一律 None, 不伪造。"""
    if len(row) < 7:
        return None
    value = row[6]
    if isinstance(value, (bool, dict)):
        return None
    amount = _to_float(value)
    return amount * 10_000.0 if amount is not None else None


def _has_event_annotation(row: list) -> bool:
    """日K行是否带分红/除权注释(第 7 列为含 cqr 的 dict, 实测仅除权日行携带)。"""
    return len(row) >= 7 and isinstance(row[6], dict) and "cqr" in row[6]


def _lsq_fit(pts: list[tuple[float, float]]) -> tuple[float, float] | None:
    """最小二乘拟合 qfq = a·raw + b; raw 无方差(全相同)时返回 None。"""
    n = len(pts)
    sx = sum(x for x, _ in pts)
    sy = sum(y for _, y in pts)
    sxx = sum(x * x for x, _ in pts)
    sxy = sum(x * y for x, y in pts)
    det = n * sxx - sx * sx
    if det <= 1e-9:
        return None
    a = (n * sxy - sx * sy) / det
    return a, (sy - a * sx) / n


def _segment_starts(days: list[date], annotated: set[date]) -> list[int]:
    """把对齐日期序列按注释除权日切成"复权变换恒定"的段, 返回各段起点下标(含 0)。

    事件边界只信任腾讯自带的除权注释(实测除权日行携带含 cqr 的 dict, 分红与
    送转均标注)。不做纯价格残差扫描: 无事件先验时两点拟合的斜率噪声会在
    平盘段放大, 产生看似合理的伪事件 — 宁可漏(有明确日志)不可错。
    """
    pos = {d: i for i, d in enumerate(days)}
    return sorted({0, *{pos[d] for d in annotated if d in pos}})


class CnFreeProvider:
    """免费数据链数据源。

    realtime = 股票+ETF 快照: 新浪全市场优先, 新浪被封(IP 段 403)时熔断并降级
    腾讯自选池模式; 其余数据集(daily/adj_factor/minute/depth5)恒走腾讯。
    """

    name = "cnfree"
    builtin = True
    # 腾讯 m1 实测单请求上限 320 根(约 1.3 个交易日), 向后翻页实测可覆盖 ≥11 个
    # 交易日; 按任务书保守声明 5 个交易日, 个股分时档位据此收窄。
    minute_history_days = 5

    def __init__(self) -> None:
        self.config = _CnFreeConfig()
        self._client: CnFreeClient | None = None
        self._universe_cache: dict[str, tuple[float, list[str]]] = {}
        # 新浪封禁熔断到期时间戳(秒)与当前实时生效源, 供日志/试拉展示。
        self._sina_blocked_until = 0.0
        self.realtime_source = "sina"

    # ---- 新浪封禁熔断 ----
    def _sina_blocked(self) -> bool:
        return time.time() < self._sina_blocked_until

    def _block_sina(self, reason: str) -> None:
        """标记新浪不可用并在熔断期内不再请求, 避免每轮白打一次。"""
        self._sina_blocked_until = time.time() + _SINA_BLOCK_S
        self.realtime_source = "tencent"
        logger.warning(
            "cnfree 新浪实时源不可用(%s), 熔断 %.0fs 内改走腾讯快照; "
            "降级期只覆盖自选池(上限 %d 只)",
            reason,
            _SINA_BLOCK_S,
            _REALTIME_MAX_SYMBOLS,
        )

    def close(self) -> None:  # loader.load_all 重建注册表时会对每个 provider 调 close
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
            self._client = None
        self._universe_cache.clear()

    def _get_client(self) -> CnFreeClient:
        if self._client is None:
            self._client = cnfree_client.CnFreeClient()
        return self._client

    # ---- 标的池 ----
    def _load_universe(self, kind: str) -> list[str]:
        """kind: stock | etf → 本地标的维表的 symbol 列, 按 mtime 缓存避免每轮重读。"""
        from app.config import settings

        subdir = "instruments" if kind == "stock" else "instruments_etf"
        filename = "instruments.parquet" if kind == "stock" else "instruments_etf.parquet"
        path = settings.data_dir / subdir / filename
        if not path.exists():
            return []
        try:
            mtime = path.stat().st_mtime
            cached = self._universe_cache.get(kind)
            if cached is not None and cached[0] == mtime:
                return cached[1]
            df = pl.read_parquet(path, columns=["symbol"])
            symbols = sorted(set(df["symbol"].drop_nulls().cast(pl.String).to_list()))
        except Exception as e:
            logger.warning("cnfree 标的维表读取失败 (%s): %s", path, e)
            return []
        self._universe_cache[kind] = (mtime, symbols)
        return symbols

    def _load_watchlist(self) -> list[str]:
        """自选池 symbol 列表(data/user_data/watchlist.parquet, 按 mtime 缓存)。

        与 _load_universe 同一读取方式: 只依赖已挂载的 data 目录, 不反向 import
        services(自选读写会拉起 tickflow client, 插件不该依赖它)。
        """
        from app.config import settings

        path = settings.data_dir / "user_data" / "watchlist.parquet"
        if not path.exists():
            return []
        try:
            mtime = path.stat().st_mtime
            cached = self._universe_cache.get("watchlist")
            if cached is not None and cached[0] == mtime:
                return cached[1]
            df = pl.read_parquet(path, columns=["symbol"])
            symbols = sorted(set(df["symbol"].drop_nulls().cast(pl.String).to_list()))
        except Exception as e:
            logger.warning("cnfree 自选池读取失败 (%s): %s", path, e)
            return []
        self._universe_cache["watchlist"] = (mtime, symbols)
        return symbols

    def _realtime_symbols(self, universe: list[str]) -> list[str]:
        """降级模式的标的池: 自选池 ∩ universe, 超出上限截断并告警。"""
        watch = set(self._load_watchlist())
        picked = [s for s in universe if s in watch]
        if not picked:
            logger.warning(
                "cnfree 降级模式依赖自选池, 但自选为空或未与本地维表交集为空 — "
                "本轮不返回快照(不静默给全市场假数据); 请配置自选或修复新浪访问"
            )
            return []
        if len(picked) > _REALTIME_MAX_SYMBOLS:
            logger.warning(
                "cnfree 降级模式自选 %d 只, 超过单轮上限 %d, 只拉前 %d 只",
                len(picked),
                _REALTIME_MAX_SYMBOLS,
                _REALTIME_MAX_SYMBOLS,
            )
            picked = picked[:_REALTIME_MAX_SYMBOLS]
        return picked

    # ---- realtime ----
    def get_realtime(self) -> list[dict]:
        """实时快照(股票+ETF): 新浪全市场优先, 新浪被封则熔断并降级腾讯自选池。

        实测 2026-09-30: hq.sinajs.cn 对阿里云 ECS IP 段恒 403(带 Referer/备用域
        均无效), 而腾讯快照单请求只安全到 60 只 → 降级期覆盖范围从"全市场"收窄为
        "自选池"(见 _realtime_symbols)。失败软返回 [](不阻断轮询)。
        """
        symbols = self._load_universe("stock") + self._load_universe("etf")
        if not symbols:
            logger.warning(
                "cnfree 标的池为空: 缺少 instruments/instruments.parquet 与 "
                "instruments_etf/instruments_etf.parquet, 请先运行数据管道同步标的维表"
            )
            return []
        if self._sina_blocked():
            return self._fetch_tencent_records(self._realtime_symbols(symbols))
        try:
            records = self._fetch_sina_records(symbols)
        except CnFreeRateLimitedError as e:
            self._block_sina(str(e))
            return self._fetch_tencent_records(self._realtime_symbols(symbols))
        self.realtime_source = "sina"
        return records

    def get_realtime_indices(self, symbols: list[str]) -> list[dict] | None:
        """指数实时快照(可选插件协议, quote_service 鸭子类型调用)。

        股票/指数在新浪与腾讯都是同一端点同格式(买一卖一为 0, 量按手), 因此
        熔断期内同样降级腾讯。失败返回 None(上层保留上轮有效指数缓存);
        成功但无数据返回 []。
        """
        codes: list[str] = []
        sym_by_code: dict[str, str] = {}
        for symbol in symbols or []:
            code = _sina_code(symbol)
            if code is None:
                logger.warning("cnfree 指数代码格式无法识别: %s, 跳过", symbol)
                continue
            codes.append(code)
            sym_by_code[code] = symbol
        if not codes:
            return []
        blocked = self._sina_blocked()
        try:
            snap = (
                self._get_client().tencent_snapshot(codes)
                if blocked
                else self._get_client().sina_snapshot(codes)
            )
        except CnFreeRateLimitedError as e:
            if blocked:
                logger.warning("cnfree 指数行情(腾讯降级)拉取失败: %s", e)
                return None
            self._block_sina(str(e))
            try:
                snap = self._get_client().tencent_snapshot(codes)
            except CnFreeError as e2:
                logger.warning("cnfree 指数行情降级仍失败: %s", e2)
                return None
        except CnFreeError as e:
            logger.warning("cnfree 指数行情拉取失败: %s", e)
            return None
        records = []
        for code, fields in snap.items():
            symbol = sym_by_code.get(code)
            if symbol is None:
                continue
            rec = (
                _map_tencent_fields(code, symbol, fields, is_index=True)
                if blocked
                else _map_sina_fields(code, symbol, fields, is_index=True)
            )
            if rec is not None:
                records.append(rec)
        logger.info(
            "cnfree 指数行情拉取完成(%s): %d 条(请求 %d 只)",
            "腾讯降级" if blocked else "新浪",
            len(records),
            len(codes),
        )
        return records

    def _fetch_tencent_records(self, symbols: list[str]) -> list[dict]:
        """腾讯快照降级拉取(代码格式与新浪一致, 复用 _sina_code)。软失败返回 []。"""
        if not symbols:
            return []
        codes: list[str] = []
        sym_by_code: dict[str, str] = {}
        for symbol in symbols:
            code = _sina_code(symbol)
            if code is not None:
                codes.append(code)
                sym_by_code[code] = symbol
        if not codes:
            return []
        try:
            snap = self._get_client().tencent_snapshot(codes)
        except CnFreeError as e:
            logger.warning("cnfree 腾讯快照降级拉取失败: %s", e)
            return []
        records: list[dict] = []
        for code, fields in snap.items():
            symbol = sym_by_code.get(code)
            if symbol is None:
                continue
            rec = _map_tencent_fields(code, symbol, fields)
            if rec is not None:
                records.append(rec)
        if snap and not records:
            logger.warning("cnfree 腾讯快照 %d 行全部无法解析, 疑似接口结构变化", len(snap))
            return []
        logger.info("cnfree 实时行情(腾讯降级)拉取完成: %d 条(请求 %d 只)", len(records), len(codes))
        return records

    def _fetch_sina_records(self, symbols: list[str]) -> list[dict]:
        """按标的池拉新浪快照并映射; 整批失败 / 结构不可识别时软返回 []。

        封禁/限流类失败(CnFreeRateLimitedError)向上抛, 由 get_realtime 熔断降级。
        """
        codes: list[str] = []
        sym_by_code: dict[str, str] = {}
        skipped = 0
        for symbol in symbols:
            code = _sina_code(symbol)
            if code is None:
                skipped += 1
                continue
            codes.append(code)
            sym_by_code[code] = symbol
        if skipped:
            logger.warning("cnfree 有 %d 个标的后缀无法映射新浪代码, 已跳过", skipped)
        try:
            snap = self._get_client().sina_snapshot(codes)
        except CnFreeRateLimitedError:
            raise
        except CnFreeError as e:
            logger.warning("cnfree 实时行情拉取失败: %s", e)
            return []

        records: list[dict] = []
        unparseable = 0
        for code, fields in snap.items():
            symbol = sym_by_code.get(code)
            if symbol is None:
                continue
            rec = _map_sina_fields(code, symbol, fields)
            if rec is None:
                unparseable += 1
            else:
                records.append(rec)
        if snap and not records:
            # 响应非空但一行都映射不出 → 大概率接口结构变化, 明确告警而非静默空数据
            logger.warning("cnfree 新浪快照 %d 行全部无法解析, 疑似接口结构变化", len(snap))
            return []
        logger.info("cnfree 实时行情拉取完成: %d 条(请求 %d 只)", len(records), len(codes))
        return records

    # ---- daily ----
    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable | None = None,
    ) -> pl.DataFrame:
        """日K不复权原始价(腾讯, 股票+ETF 同端点) → 内部契约。

        输出列 [symbol, date, open, high, low, close, volume, amount](腾讯行内
        列序为 open,close,high,low, 此处换序为 OHLC 标准序); volume 已是手,
        原样透传; amount 无则 None。asset_type 接受但同一实现。
        """
        chunks = [
            df
            for df in self.iter_daily(
                symbols,
                start_time=start_time,
                end_time=end_time,
                asset_type=asset_type,
                on_chunk_done=on_chunk_done,
            )
            if not df.is_empty()
        ]
        return pl.concat(chunks, how="diagonal_relaxed") if chunks else pl.DataFrame()

    def iter_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable | None = None,
    ) -> Iterator[pl.DataFrame]:
        """分批产出日K(每 _HIST_SYMBOL_BATCH 只一批), 供历史同步逐批落盘。"""
        if not symbols:
            return
        end_dt = _naive_beijing(end_time) or datetime.now()
        start_dt = _naive_beijing(start_time) or (end_dt - timedelta(days=365))
        start_d, end_d = start_dt.date(), end_dt.date()
        batches = [
            symbols[i : i + _HIST_SYMBOL_BATCH] for i in range(0, len(symbols), _HIST_SYMBOL_BATCH)
        ]
        client = self._get_client()
        consec_fail = 0
        for done, batch in enumerate(batches, start=1):
            rows: list[dict] = []
            stop = False
            for j, symbol in enumerate(batch):
                if j:
                    time.sleep(_SYM_INTERVAL_S)
                try:
                    rows.extend(self._daily_rows(client, symbol, start_d, end_d))
                    consec_fail = 0
                except CnFreeRateLimitedError as e:
                    consec_fail += 1
                    logger.warning("cnfree 日K疑似被限流 %s: %s (连续 %d)", symbol, e, consec_fail)
                    if consec_fail >= _SYM_FAIL_BREAK:
                        logger.error(
                            "cnfree 日K连续 %d 个标的疑似被腾讯限流, 熔断本轮(已同步数据保留)", consec_fail
                        )
                        stop = True
                        break
            df = normalize_daily(rows, source=self.name) if rows else pl.DataFrame()
            # 空批次也回调计数, 保证最终 cur == total
            if on_chunk_done:
                on_chunk_done(done, len(batches))
            if not df.is_empty():
                yield df
            if stop:
                # 剩余批次不再拉取, 但补齐进度回调(cur==total 契约)
                if on_chunk_done:
                    for rest in range(done + 1, len(batches) + 1):
                        on_chunk_done(rest, len(batches))
                return

    def _daily_rows(
        self, client: CnFreeClient, symbol: str, start_d: date, end_d: date
    ) -> list[dict]:
        """单标的日K原始行 → 内部行(OHLC 换序, volume 手透传, amount 万元→元)。"""
        code = _sina_code(symbol)
        if code is None:
            logger.warning("cnfree 日K: 代码格式无法识别 %s, 跳过", symbol)
            return []
        try:
            raw = client.tencent_daily(code, start_d, end_d, fqt="")
        except CnFreeRateLimitedError:
            raise
        except CnFreeError as e:
            logger.warning("cnfree 日K拉取失败 %s: %s", symbol, e)
            return []
        out: list[dict] = []
        for row in raw:
            d, close = _daily_close(row)
            if d is None or close is None:
                continue
            out.append(
                {
                    "symbol": symbol,
                    "date": d,
                    # 腾讯行内列序 open,close,high,low → 输出 OHLC 标准序
                    "open": _to_float(row[1]),
                    "high": _to_float(row[3]),
                    "low": _to_float(row[4]),
                    "close": close,
                    "volume": _to_float(row[5]),  # 手, 原样透传
                    "amount": _daily_amount(row),
                }
            )
        return out

    # ---- adj_factor ----
    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable | None = None,
    ) -> pl.DataFrame:
        """除权因子(事件级稀疏) → [symbol, trade_date, ex_factor]。

        算法见模块 docstring: qfq/raw 仿射段边界法。只输出事件行, 无事件返回
        空 DataFrame。推导失败的标的告警跳过, 不拖垮整批。

        推导跨度取「请求窗口」与「end 起 365 天」的较大者, 且输出推导跨度内
        全部事件(不只请求窗口内的): 盘后管道的 adj 阶段按天传增量窗口, 若按
        窗口过滤, 首装(本地无 all.parquet)时永远回填不出历史事件; 全量输出由
        调用方按 (symbol, trade_date) 幂等合并。腾讯单请求 800 根 > 365 交易日,
        拉宽跨度不增加请求次数。
        """
        if not symbols:
            return pl.DataFrame(schema=ADJ_SCHEMA)
        end_dt = _naive_beijing(end_time) or datetime.now()
        start_dt = _naive_beijing(start_time) or (end_dt - timedelta(days=365))
        horizon_start = (end_dt - timedelta(days=_ADJ_FETCH_HORIZON_DAYS)).date()
        fetch_start = min((start_dt - timedelta(days=_ADJ_BACKFILL_DAYS)).date(), horizon_start)
        end_d = end_dt.date()
        batches = [
            symbols[i : i + _HIST_SYMBOL_BATCH] for i in range(0, len(symbols), _HIST_SYMBOL_BATCH)
        ]
        client = self._get_client()
        rows_out: list[dict] = []
        consec_fail = 0
        stop = False
        for done, batch in enumerate(batches, start=1):
            for j, symbol in enumerate(batch):
                if j:
                    time.sleep(_SYM_INTERVAL_S)
                try:
                    rows_out.extend(
                        self._adj_rows(client, symbol, fetch_start, end_d, fetch_start, end_d)
                    )
                    consec_fail = 0
                except CnFreeRateLimitedError as e:
                    consec_fail += 1
                    logger.warning("cnfree 除权因子疑似被限流 %s: %s (连续 %d)", symbol, e, consec_fail)
                    if consec_fail >= _SYM_FAIL_BREAK:
                        logger.error(
                            "cnfree 除权因子连续 %d 个标的疑似被腾讯限流, 熔断本轮(已推导事件保留)", consec_fail
                        )
                        stop = True
                        break
            if on_chunk_done:  # 空批次也回调计数
                on_chunk_done(done, len(batches))
            if stop:
                if on_chunk_done:
                    for rest in range(done + 1, len(batches) + 1):
                        on_chunk_done(rest, len(batches))
                break
        if not rows_out:
            return pl.DataFrame(schema=ADJ_SCHEMA)
        return (
            pl.DataFrame(rows_out, schema=ADJ_SCHEMA)
            .unique(subset=["symbol", "trade_date"], keep="last")
            .sort(["symbol", "trade_date"])
        )

    def _adj_rows(
        self,
        client: CnFreeClient,
        symbol: str,
        fetch_start: date,
        fetch_end: date,
        out_start: date,
        out_end: date,
    ) -> list[dict]:
        """单标的除权推导: qfq/raw 两次请求 → 仿射分段 → 边界因子。"""
        code = _sina_code(symbol)
        if code is None:
            logger.warning("cnfree 除权因子: 代码格式无法识别 %s, 跳过", symbol)
            return []
        try:
            raw_rows = client.tencent_daily(code, fetch_start, fetch_end, fqt="")
            qfq_rows = client.tencent_daily(code, fetch_start, fetch_end, fqt="qfq")
        except CnFreeRateLimitedError:
            raise
        except CnFreeError as e:
            logger.warning("cnfree 除权因子拉取失败 %s: %s", symbol, e)
            return []

        raw_map: dict[date, float] = {}
        annotated: set[date] = set()
        for row in raw_rows:
            d, close = _daily_close(row)
            if d is None:
                continue
            if close and close > 0:
                raw_map[d] = close
            if _has_event_annotation(row):
                annotated.add(d)
        qfq_map: dict[date, float] = {}
        for row in qfq_rows:
            d, close = _daily_close(row)
            if d is not None and close and close > 0:
                qfq_map[d] = close

        days = sorted(set(raw_map) & set(qfq_map))
        if len(days) < _ADJ_MIN_ALIGNED_DAYS:
            return []  # 数据不足(如 ETF 无事件、qfq 深度不足), 不伪造

        starts = _segment_starts(days, annotated)
        seg_bounds = [
            (starts[k], starts[k + 1] if k + 1 < len(starts) else len(days))
            for k in range(len(starts))
        ]
        events: list[dict] = []
        for k in range(1, len(seg_bounds)):
            below_start, below_end = seg_bounds[k - 1]
            above_start, above_end = seg_bounds[k]
            event = self._boundary_factor(
                symbol, days, raw_map, qfq_map, below_start, below_end, above_start, above_end
            )
            if event is None:
                continue
            ex_day, factor = event
            if not (out_start <= ex_day <= out_end):
                continue
            events.append({"symbol": symbol, "trade_date": ex_day, "ex_factor": factor})
        return events

    @staticmethod
    def _boundary_factor(
        symbol: str,
        days: list[date],
        raw_map: dict[date, float],
        qfq_map: dict[date, float],
        below_start: int,
        below_end: int,
        above_start: int,
        above_end: int,
    ) -> tuple[date, float] | None:
        """段边界 → (除权日, ex_factor); 无法推导返回 None。

        除权日 = 上段首日; 连续性 a_b·raw_prev + b_b = a_a·ref + b_a →
        ref = (a_b*raw_prev + b_b - b_a)/a_a, ex_factor = raw_prev / ref
        (纯分红时 a 相消, 退化为 raw_prev/(raw_prev - D), 因子 > 1)。
        """
        if above_start - 1 < below_start:
            logger.warning(
                "cnfree 除权因子: %s 注释事件 %s 位于窗口首日, 无前一日可推导, 跳过",
                symbol,
                days[above_start],
            )
            return None
        raw_prev = raw_map[days[above_start - 1]]
        below_pts = [(raw_map[days[i]], qfq_map[days[i]]) for i in range(below_start, below_end)]
        above_pts = [(raw_map[days[i]], qfq_map[days[i]]) for i in range(above_start, above_end)]
        fit_below = _lsq_fit(below_pts) if len(below_pts) >= 2 else None
        fit_above = _lsq_fit(above_pts) if len(above_pts) >= 2 else None
        if fit_above is None and above_end == len(days) and len(above_pts) == 1:
            # 最新段(前复权锚定现价)且仅剩除权日当天: qfq≈raw 时取恒等线
            d = days[above_start]
            if abs(qfq_map[d] - raw_map[d]) <= _ADJ_IDENTITY_EPS:
                fit_above = (1.0, 0.0)
        if fit_below is None or fit_above is None:
            logger.warning(
                "cnfree 除权因子: %s 注释事件 %s 段内数据不足无法推导, 跳过(宁可漏不错)",
                symbol,
                days[above_start],
            )
            return None
        a_b, b_b = fit_below
        a_a, b_a = fit_above
        ref = (a_b * raw_prev + b_b - b_a) / a_a
        if ref <= 0:
            logger.warning("cnfree 除权因子: %s 参考价非正 (%s), 跳过", days[above_start], ref)
            return None
        factor = raw_prev / ref
        if not (_ADJ_FACTOR_MIN <= factor <= _ADJ_FACTOR_MAX):
            logger.warning(
                "cnfree 除权因子: %s 推导因子 %s 超出合理界, 剔除", days[above_start], factor
            )
            return None
        if abs(factor - 1.0) <= 1e-4:  # 容差内不视为事件(如纯噪声切分)
            return None
        return days[above_start], factor

    # ---- minute ----
    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable | None = None,
        freq: str = "1m",
    ) -> pl.DataFrame:
        """分钟K(腾讯 m1) → [symbol, datetime(北京墙钟 naive), open, high, low,
        close, volume(手), amount(None)]。仅支持 1m; 深度受 minute_history_days 限制,
        超出深度部分自然缺数据(端点只保留浅历史), 不伪造。
        """
        if freq != "1m":
            logger.warning("cnfree 分钟K仅支持 1m, 收到 %s, 返回空(该数据集回退 TickFlow)", freq)
            return pl.DataFrame(schema=MINUTE_SCHEMA)
        if not symbols:
            return pl.DataFrame(schema=MINUTE_SCHEMA)
        end_dt = _naive_beijing(end_time) or datetime.now()
        start_dt = _naive_beijing(start_time) or (end_dt - timedelta(days=1))
        days_span = max(1, (end_dt.date() - start_dt.date()).days + 1)
        max_pages = min(days_span + 1, _MINUTE_MAX_PAGES)
        batches = [
            symbols[i : i + _HIST_SYMBOL_BATCH] for i in range(0, len(symbols), _HIST_SYMBOL_BATCH)
        ]
        client = self._get_client()
        chunks: list[pl.DataFrame] = []
        consec_fail = 0
        stop = False
        for done, batch in enumerate(batches, start=1):
            rows: list[dict] = []
            for j, symbol in enumerate(batch):
                if j:
                    time.sleep(_SYM_INTERVAL_S)
                try:
                    rows.extend(self._minute_rows(client, symbol, start_dt, end_dt, max_pages))
                    consec_fail = 0
                except CnFreeRateLimitedError as e:
                    consec_fail += 1
                    logger.warning("cnfree 分钟K疑似被限流 %s: %s (连续 %d)", symbol, e, consec_fail)
                    if consec_fail >= _SYM_FAIL_BREAK:
                        logger.error(
                            "cnfree 分钟K连续 %d 个标的疑似被腾讯限流, 熔断本轮(已拉数据保留)", consec_fail
                        )
                        stop = True
                        break
            if on_chunk_done:  # 空批次也回调计数
                on_chunk_done(done, len(batches))
            chunks.append(
                pl.DataFrame(rows, schema=MINUTE_SCHEMA)
                if rows
                else pl.DataFrame(schema=MINUTE_SCHEMA)
            )
            if stop:
                if on_chunk_done:
                    for rest in range(done + 1, len(batches) + 1):
                        on_chunk_done(rest, len(batches))
                break
        chunks = [df for df in chunks if not df.is_empty()]
        return pl.concat(chunks, how="vertical_relaxed") if chunks else pl.DataFrame(
            schema=MINUTE_SCHEMA
        )

    def _minute_rows(
        self,
        client: CnFreeClient,
        symbol: str,
        start_dt: datetime,
        end_dt: datetime,
        max_pages: int,
    ) -> list[dict]:
        code = _sina_code(symbol)
        if code is None:
            logger.warning("cnfree 分钟K: 代码格式无法识别 %s, 跳过", symbol)
            return []
        try:
            raw = client.tencent_minute(code, end_dt, max_pages)
        except CnFreeRateLimitedError:
            raise
        except CnFreeError as e:
            logger.warning("cnfree 分钟K拉取失败 %s: %s", symbol, e)
            return []
        out: list[dict] = []
        unparseable = 0
        total_rows = 0
        for row in raw:
            if not isinstance(row, list) or len(row) < 6:
                continue
            total_rows += 1
            try:
                dt = datetime.strptime(str(row[0]), "%Y%m%d%H%M")  # 北京墙钟 naive
            except ValueError:
                unparseable += 1
                continue
            if not (start_dt <= dt <= end_dt):
                continue
            out.append(
                {
                    "symbol": symbol,
                    "datetime": dt,
                    # 腾讯行内列序 open,close,high,low → 输出 OHLC 标准序
                    "open": _to_float(row[1]),
                    "high": _to_float(row[3]),
                    "low": _to_float(row[4]),
                    "close": _to_float(row[2]),
                    "volume": _to_float(row[5]),  # 手, 原样透传
                    "amount": None,  # 端点不提供, 不伪造
                }
            )
        if total_rows and unparseable >= total_rows and not out:
            # 有行但全部时间标签无法解析 → 结构变化告警, 不静默空数据
            logger.warning(
                "cnfree 分钟K %s 的 %d 行时间标签全部无法解析, 疑似接口结构变化",
                symbol,
                unparseable,
            )
        return out

    # ---- depth5 (五档盘口) ----
    def get_depth_batch(self, symbols: list[str]) -> dict[str, dict]:
        """五档盘口(腾讯 qt.gtimg) → {symbol: 标准盘口字典}。

        契约(docs/plugin-development.md): bid/ask 价量各 5 档按一档到五档排列,
        量单位为"手"(qt.gtimg 原生即手, 不换算), timestamp 毫秒。
        单批失败软处理: 该批标的跳过并告警, 不拖垮其他批次, 不跨源回退。
        """
        out: dict[str, dict] = {}
        batches = [
            symbols[i : i + _DEPTH_BATCH] for i in range(0, len(symbols), _DEPTH_BATCH)
        ]
        client = self._get_client()
        consec_fail = 0
        for bi, batch in enumerate(batches):
            if bi:
                time.sleep(_DEPTH_BATCH_INTERVAL_S)
            codes = {sym: _sina_code(sym) for sym in batch}
            valid = {sym: code for sym, code in codes.items() if code}
            bad = [sym for sym, code in codes.items() if code is None]
            for sym in bad:
                logger.warning("cnfree 盘口: 代码格式无法识别 %s, 跳过", sym)
            if not valid:
                continue
            try:
                raw = client.tencent_depth_batch(list(valid.values()))
                consec_fail = 0
            except CnFreeRateLimitedError as e:
                consec_fail += 1
                logger.warning("cnfree 盘口疑似被限流: %s (连续 %d 批)", e, consec_fail)
                if consec_fail >= _SYM_FAIL_BREAK:
                    logger.error("cnfree 盘口连续 %d 批疑似被腾讯限流, 熔断本轮", consec_fail)
                    break
                continue
            except CnFreeError as e:
                logger.warning("cnfree 盘口批次拉取失败: %s", e)
                continue
            reverse = {code: sym for sym, code in valid.items()}
            for code, fields in raw.items():
                sym = reverse.get(code)
                if sym is None:
                    continue
                parsed = _parse_depth_fields(fields)
                if parsed is not None:
                    out[sym] = parsed
        return out

    # ---- 测试(设置页试拉) ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        syms = [s for s in (symbols or [])][:2]
        now = datetime.now()
        try:
            if dataset == "realtime":
                targets = syms or ["600519.SH", "000001.SZ"]
                if self._sina_blocked():
                    records = self._fetch_tencent_records(targets)
                else:
                    try:
                        records = self._fetch_sina_records(targets)
                    except CnFreeRateLimitedError as e:
                        self._block_sina(str(e))
                        records = self._fetch_tencent_records(targets)
                return {
                    "provider": self.name,
                    "dataset": dataset,
                    "rows": len(records),
                    "realtime_source": self.realtime_source,
                    "columns": list(records[0].keys()) if records else [],
                    "preview": records[:5],
                }
            if dataset == "depth5":
                depth = self.get_depth_batch(syms or ["600519.SH", "000001.SZ"])
                return {
                    "provider": self.name,
                    "dataset": dataset,
                    "rows": len(depth),
                    "columns": [
                        "symbol",
                        "bid_prices",
                        "bid_volumes",
                        "ask_prices",
                        "ask_volumes",
                        "timestamp",
                    ],
                    "preview": [
                        {"symbol": sym, **fields} for sym, fields in list(depth.items())[:5]
                    ],
                }
            if dataset == "daily":
                df = self.get_daily(syms or ["600519.SH"], now - timedelta(days=30), now)
            elif dataset == "adj_factor":
                df = self.get_adj_factors(syms or ["600519.SH"], now - timedelta(days=365), now)
            elif dataset == "minute":
                df = self.get_minute(syms or ["600519.SH"], now - timedelta(days=2), now)
            else:
                return {
                    "provider": self.name,
                    "dataset": dataset,
                    "rows": 0,
                    "error": f"cnfree 插件未接入 {dataset} 数据集(自动回退 TickFlow)",
                }
        except CnFreeError as e:
            return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
        head = df.head(5).to_dicts()
        for row in head:  # date/datetime → ISO 字符串, 保证 JSON 可序列化
            for k, v in list(row.items()):
                if isinstance(v, (date, datetime)):
                    row[k] = v.isoformat()
        return {
            "provider": self.name,
            "dataset": dataset,
            "rows": df.height,
            "columns": df.columns,
            "preview": head,
        }
