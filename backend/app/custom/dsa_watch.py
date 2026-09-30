"""DSA 次日盯盘 (迁移计划 §4 P2): watch-plan 聚合 + 点位→monitor-rules 对账 + 盘前自检。

三个端点 (契约 §4.1):
  GET  /watch-plan       盯盘清单: 持仓(DSA 桥只读) + 自选、最新报告、已同步规则
  POST /watch-sync/run   对账: 把最新报告点位落成 DSA· 前缀的 price 规则 (只碰前缀规则)
  GET  /premarket/status 盘前自检结果 (调度线程每交易日 09:10-11:30 跑一次, GET 纯读)

盯盘集合 (契约 §4.1 "持仓股在前, 其余按自选顺序"): 持仓 symbol + 自选, 持仓在
前、其余按自选序、去重; 各票 symbol 统一经 dsa_analysis._canonical_symbol
规范化为 `代码.交易所` 后缀式 (DSA 库内是裸 6 位或 SZ 前缀式) 再进清单与规则。

对账硬规则 (契约 §4.2):
  - 同步规则 name 前缀「DSA·」是唯一托管标记 (TSP 规则无 source 字段); 人工规则
    一律不碰; 外来 DSA· 命名 (解析不出 sym6+五 kind) 也不托管、不比对、不删。
  - 持仓股: stop_loss→close<=X critical / take_profit→close>=X critical /
    secondary_buy→close<=X warn(kind=add) / risk_conditions kind=reduce 且带价→
    close<=X warn(kind=reduce)。空仓: ideal_buy→close<=X info(kind=entry)。
  - 删除触发器只有两个票级事件: 移出自选、清仓。持仓票报告缺点位/缺报告时
    跳过点位派生 diff、保留既有规则 (防 LLM 围栏解析闪失当晚清掉用户止损线);
    清仓票则删其全部同步规则 (清仓后止损线已无意义)。
  - 触发时机: 分析任务收尾 (dsa_analysis 钩子 on_task_done) + 调度线程 30min
    兜底 (scheduler_tick) + 手动 POST /watch-sync/run, 三入口经 _SYNC_LOCK 串行。

规则写入与引擎生效: 写盘 ≠ 生效 (引擎无周期重载), 对账落盘后镜像
api/monitor_rules._sync_engine 调 engine.set_rules; engine 引用只能在请求
handler 捕获 (ExtensionContext 不带 engine)。有变更而 engine 未捕获时置
_ENGINE_FLUSH_PENDING, 任一 dsa_watch/分析路由随后捕获到 engine 即补 flush,
不让规则停留只在磁盘。

持仓读取 (P3 切换, 迁移计划 §4.4.3 过渡策略, load_positions): 本账
(dsa_portfolio 流水账) 非空 → FIFO 重放为持仓, positions_source="dsa_portfolio";
本账为空 → 回落 dsa_bridge 只读 DSA 库 portfolio_positions, positions_source=
"dsa_bridge"——过渡期连续性: 用户未从 DSA 导入/记账前持仓视图不回归;
positions_source 如实上报实际来源。本账读取故障不回落桥 (导入后桥是过时旧账,
按旧账对账会误删规则), 与桥不可用同抛 BridgeUnavailableError。桥读法 (仅回落
时): quantity>0; 同持仓 avg/fifo 各存一行 (UNIQUE(account_id, symbol, market,
currency, cost_method)), 混读会重复计数——任一行带 fifo 则整库只取 fifo 行
(实测 fifo 11 行是 avg 的超集, 账户 3/4 仅 fifo 行, 只取 avg 会丢持仓), 否则
只取 avg 行, 两法绝不混读; 跨账户 quantity 求和、avg_cost 按数量加权。不按
portfolio_accounts.is_active 过滤 (实测 12/17 行挂非活跃账户, 过滤会丢大半
持仓)。桥不可用 (文件缺失/锁定/无表) 时 watch-plan holding=false 全量按空仓
处理, 绝不 5xx; 对账则中止零写入 (防误删规则)。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.custom import dsa_analysis, dsa_bridge, dsa_portfolio
from app.extensions import (
    BACKEND_EXTENSION_API_VERSION,
    BackendExtensionRegistrar,
    ExtensionContext,
)
from app.market_time import cn_now
from app.services import watchlist
from app.services.fs_utils import atomic_write_text
from app.services.trading_day import is_trading_day
from app.strategy import monitor_rules

logger = logging.getLogger(__name__)

EXTENSION_ID = "dsa.watch"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION

NAME_PREFIX = "DSA·"
RULE_COOLDOWN_SECONDS = 86400
_WATCH_FALLBACK_INTERVAL_SECONDS = 1800.0
_PREMARKET_WINDOW = ("09:10", "11:30")
_PREMARKET_STATE_DIR = "dsa_watch"
_MAX_DETAIL_LEN = 120

# 五类规则: kind → (条件方向, severity)。name/id 模板见 _rule_name/_rule_id。
_RULE_OP_BY_KIND = {
    "stop_loss": "<=", "take_profit": ">=", "add": "<=", "reduce": "<=", "entry": "<=",
}
_RULE_SEVERITY_BY_KIND = {
    "stop_loss": "critical", "take_profit": "critical",
    "add": "warn", "reduce": "warn", "entry": "info",
}
_KINDS = tuple(_RULE_OP_BY_KIND)

# 告警 message 的触发语义 (引擎 message 非空即原文下发, 不再拼默认条件摘要,
# 故必须自述清楚"触发的是什么"); 方向符号与 _RULE_OP_BY_KIND 一一对应。
_RULE_KIND_LABELS = {
    "stop_loss": "止损", "take_profit": "止盈", "add": "加仓",
    "reduce": "减仓", "entry": "介入",
}
_RULE_OP_LABELS = {"<=": "≤", ">=": "≥"}

# ===== §4.5-1 接近/逼近阶梯 (ATR 自适应, 移植自 DSA alert_rule_sync_service) =====
# 每条主规则 (stop_loss/take_profit/add/reduce; entry 不配) 配两条同向预警:
# 接近(near, info) / 逼近(mid, warn), 每日各响一次 (cooldown 与主规则同 86400),
# 把"到价才响一声"拆成接近→逼近→到价三段。带宽随 ATR 自适应。
_LADDER_BASE_KINDS = ("stop_loss", "take_profit", "add", "reduce")
_LADDER_TIER_SEVERITY = {"near": "info", "mid": "warn"}
_NEAR_BUFFER_MIN = 0.003      # 0.3%
_NEAR_BUFFER_MAX = 0.03       # 3%
_NEAR_BUFFER_DEFAULT = 0.01   # ATR 读不到回落 1.0%
_MID_BUFFER_MIN = 0.0015      # 0.15%


def _base_kind(kind: str) -> str:
    """near_/mid_ 阶梯 kind → 主规则 kind; 非阶梯原样返回。"""
    for tier in ("near_", "mid_"):
        if kind.startswith(tier):
            return kind[len(tier):]
    return kind


def _kind_label(kind: str) -> str:
    """kind → 消息文案标签; 阶梯 kind 加 接近/逼近 前缀。"""
    base = _base_kind(kind)
    label = _RULE_KIND_LABELS.get(base, base)
    if kind.startswith("near_"):
        return "接近" + label
    if kind.startswith("mid_"):
        return "逼近" + label
    return label


def _near_buffer(atr_20_pct: float | None) -> float:
    """接近带宽(小数) = clamp(0.5×ATR20%, 0.3%, 3%); ATR 缺读回落 1.0%。"""
    if atr_20_pct is not None and atr_20_pct > 0:
        return min(max(0.005 * float(atr_20_pct), _NEAR_BUFFER_MIN), _NEAR_BUFFER_MAX)
    return _NEAR_BUFFER_DEFAULT


def _ladder_price(price: float, op: str, tier: str, atr_20_pct: float | None) -> float:
    """接近/逼近带阈值: 主阈值向"更早触发"方向外扩 (op<= 上加, op>= 下减), 四舍五入到分。

    Decimal + ROUND_HALF_UP (对齐 DSA 任务书算例 30.5→30.81; float round 会因
    二进制表示把 30.805 收成 30.8)。
    """
    b = _near_buffer(atr_20_pct)
    if tier == "mid":
        b = max(b / 2.0, _MID_BUFFER_MIN)
    factor = Decimal("1") + Decimal(str(b)) if op == "<=" else Decimal("1") - Decimal(str(b))
    return float((Decimal(str(price)) * factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _atr_20_pct(repo, suffixed: str, asset_type: str) -> float | None:
    """ATR20% = mean(近 20 根 TR) / 最新收盘 × 100; 任一环节失败回落 None (带宽取默认)。

    TR = max(high-low, |high-prev_close|, |low-prev_close|)。对账与 sync_state
    判定共用同一 _build_specs 路径, 两侧口径永远一致。
    """
    if repo is None:
        return None
    try:
        end = cn_now().date()
        start = end - timedelta(days=70)
        df = repo.get_daily_asset(asset_type, suffixed, start, end, columns=["date", "high", "low", "close"])
        if df is None or df.height < 21:
            return None
        df = df.sort("date").tail(21)
        highs = df["high"].to_list()
        lows = df["low"].to_list()
        closes = df["close"].to_list()
        trs = [
            max(h - l, abs(h - pc), abs(l - pc))
            for h, l, pc in zip(highs[1:], lows[1:], closes[:-1])
            if None not in (h, l, pc)
        ]
        last_close = closes[-1]
        if not trs or not last_close:
            return None
        return (sum(trs) / len(trs)) / last_close * 100
    except Exception as e:  # noqa: BLE001
        logger.info("dsa watch: ATR 计算失败 %s, 带宽取默认: %s", suffixed, e)
        return None

_RUNTIME: dict[str, Any] = {"repo": None, "data_dir": None, "engine": None}
_SYNC_LOCK = threading.Lock()
_ENGINE_FLUSH_PENDING = False  # 有规则变更但 engine 未捕获: 置位待捕获后补 flush
_last_watch_sync = 0.0  # time.monotonic() 时钟
_last_premarket_date: date | None = None


class BridgeUnavailableError(RuntimeError):
    """DSA 旧库不可用 (文件缺失/锁定/无表/查询失败)。"""


class SyncError(RuntimeError):
    """对账前置读取失败, 零写入中止 (手动同步映射 400)。"""


# ================================================================
# symbol 工具 (纯函数)
# ================================================================

_SYM6_RE = re.compile(r"(?:SZ|SH|BJ)?(\d{6})")


def _norm6(raw: Any) -> str | None:
    """任意形态 symbol → 6 位数字码; 非法返回 None。

    兼容 "600460.SH" / "SZ000636" / "000636" 三种历史形态 (DSA 桥行是裸码或
    SZ 前缀, TSP 自选带后缀)。
    """
    text = str(raw or "").strip().upper()
    if not text:
        return None
    if "." in text:
        text = text.split(".", 1)[0]
    m = _SYM6_RE.fullmatch(text)
    return m.group(1) if m else None


def _symkey(suffixed: str) -> str:
    """带后缀 symbol → id 片段 (ID_RE 只收小写字母数字下划线, 点号必须清洗)。"""
    return suffixed.replace(".", "").lower()


def _rule_id(suffixed: str, kind: str, rank: int) -> str:
    """确定性 id (天然 upsert 幂等): dsa_{symkey}_{kind}[_{rank}]。"""
    base = f"dsa_{_symkey(suffixed)}_{kind}"
    return f"{base}_{rank}" if _base_kind(kind) == "reduce" else base


def _rule_name(sym6: str, kind: str, rank: int) -> str:
    name = f"{NAME_PREFIX}{sym6}·{kind}"
    return f"{name}·{rank}" if _base_kind(kind) == "reduce" else name


def _rule_message(expected: ExpectedRule) -> str:
    """告警 message: 自述触发语义与阈值 (现价/涨跌幅由引擎推送时追加)。

    由 (kind, op, price) 确定性生成——_content_differs 纳入 message 比较,
    老版本模板同步出的规则会在下次对账自动改写为当前文案。
    """
    kind = _kind_label(expected.kind)
    op = _RULE_OP_LABELS.get(expected.op, expected.op)
    return f"DSA {kind}提醒: 收盘价 {op} {expected.price:.2f}"


# ================================================================
# 持仓读取 (DSA 桥只读)
# ================================================================

def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _clean_position_row(row: Any) -> dict | None:
    """单行清洗: 数量/成本不是有限数值 (TEXT/NULL 脏行) → None 跳过并 warning。

    SQLite 无列类型约束, WHERE quantity>0 放得过 TEXT 行 (类型序 TEXT 恒大于
    数值); 脏行若直通 merge 会抛 TypeError 打穿「桥不可用 200 不 5xx」降级,
    故在采集层统一容错。
    """
    try:
        sym6 = _norm6(row.get("symbol"))
        quantity = float(row.get("quantity"))
        avg_cost = float(row.get("avg_cost"))
    except (TypeError, ValueError):
        logger.warning("dsa watch: 持仓脏行跳过 (非数值字段): %r", row)
        return None
    if sym6 is None or quantity <= 0 or avg_cost < 0:
        logger.warning("dsa watch: 持仓脏行跳过 (码非法或数量/成本越界): %r", row)
        return None
    return {
        "sym6": sym6,
        "account_id": row.get("account_id"),
        "cost_method": row.get("cost_method"),
        "quantity": quantity,
        "avg_cost": avg_cost,
    }


def merge_position_rows(rows: list[dict]) -> dict[str, dict]:
    """持仓行合并 (纯函数): fifo/avg 二选一 → 同 (票,账户) 去重 → 跨账户加权合并。

    实测旧库 UNIQUE(account_id, symbol, market, currency, cost_method) 使同持仓
    avg+fifo 各一行, 混读会重复计数。选取规则 (整库二选一, 绝不混读): 任一行带
    fifo 则只取 fifo 行 (实测 fifo 11 行是 avg 的超集, 账户 3/4 仅有 fifo 行,
    只取 avg 会丢这些持仓), 否则只取 avg 行。跨账户才是真加仓。
    """
    method = "fifo" if any(row.get("cost_method") == "fifo" for row in rows) else "avg"
    picked: dict[tuple[str, Any], dict] = {}
    for row in rows:
        if row.get("cost_method") != method:
            continue
        picked.setdefault((row["sym6"], row.get("account_id")), row)
    totals: dict[str, list[float]] = {}
    for row in picked.values():
        acc = totals.setdefault(row["sym6"], [0.0, 0.0])
        acc[0] += row["quantity"]
        acc[1] += row["quantity"] * row["avg_cost"]
    return {
        sym6: {"quantity": round(q, 4), "avg_cost": round(cost_sum / q, 4)}
        for sym6, (q, cost_sum) in totals.items()
    }


def _fetch_position_rows() -> list[dict]:
    """只读 portfolio_positions 原始行 (quantity>0); 绝不写入 (URI mode=ro)。"""
    sql = (
        "SELECT account_id, symbol, cost_method, quantity, avg_cost "
        "FROM portfolio_positions WHERE quantity > 0"
    )
    try:
        with closing(dsa_bridge._connect_legacy_ro()) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(sql).fetchall()]
    except (sqlite3.Error, OSError) as e:
        raise BridgeUnavailableError(f"DSA 旧库不可用: {e}") from None


def load_merged_positions() -> dict[str, dict]:
    """DSA 桥持仓 → {sym6: {quantity, avg_cost}}; 桥不可用抛 BridgeUnavailableError。"""
    raw = _fetch_position_rows()
    cleaned = [r for r in (_clean_position_row(row) for row in raw) if r is not None]
    return merge_position_rows(cleaned)


def load_positions() -> tuple[dict[str, dict], str]:
    """持仓来源切换 (§4.4.3 过渡策略, 见模块 docstring「持仓读取」)。

    本账 (dsa_portfolio) 流水非空 → FIFO 重放, source="dsa_portfolio"; 本账为空
    → 回落 DSA 桥, source="dsa_bridge" (用户未导入前持仓视图不回归)。返回
    ({sym6: {quantity, avg_cost}}, source), source 如实上报实际来源; 本账按
    sym6 归并 (同码跨市场撞码并票与桥口径一致)。本账读取/重放故障不回落桥
    (导入后桥是过时旧账), 抛 BridgeUnavailableError 走既有降级。
    """
    try:
        trades = dsa_portfolio._STORE.list_trades()
        replayed = dsa_portfolio.replay_positions(trades) if trades else {}
    except Exception as e:
        raise BridgeUnavailableError(f"本账持仓流水读取失败, 无法确定持仓: {e}") from None
    if trades:
        positions: dict[str, dict] = {}
        for symbol, pos in replayed.items():
            sym6 = _norm6(symbol)
            if sym6 is not None:
                positions[sym6] = {"quantity": pos["quantity"], "avg_cost": pos["avg_cost"]}
        return positions, "dsa_portfolio"
    try:
        merged = load_merged_positions()
    except BridgeUnavailableError as e:
        raise BridgeUnavailableError(f"DSA 桥不可用, 无法确定持仓: {e}") from None
    return merged, "dsa_bridge"


# ================================================================
# 期望规则推导 (纯函数, 对账与 sync_state 判定的共同输入)
# ================================================================

@dataclass(frozen=True)
class ExpectedRule:
    kind: str   # stop_loss | take_profit | add | reduce | entry
    rank: int   # 同 kind 多条时按价格升序 1..n (跨报告稳定映射)
    op: str
    price: float
    severity: str


@dataclass
class ExpectedSpec:
    sym6: str
    suffixed: str
    holding: bool
    rules: list[ExpectedRule] = field(default_factory=list)
    skip_diff: bool = False   # True=保留既有规则不比对 (无报告/点位全空/指数票)
    empty_slots: int = 0      # 点位为空的期望槽位数 (skipped 计数)
    preserve_kinds: frozenset[str] = field(default_factory=frozenset)
    # 报告缺点位的点位列 kind (§4.3-1): 这些槽位的既有规则保留不删, 报告
    # 给出的点位照常比对——LLM 单次缺某个点位不清掉用户在用的挂线
    asset_type: str = "stock"
    report_created_at: str | None = None


def _report_points(report: dict | None) -> dict:
    if isinstance(report, dict) and isinstance(report.get("points"), dict):
        return report["points"]
    return {}


def _risk_conditions(report: dict | None) -> list:
    phase = report.get("phase_decision") if isinstance(report, dict) else None
    risks = phase.get("risk_conditions") if isinstance(phase, dict) else None
    return risks if isinstance(risks, list) else []


def _count_empty_point_slots(holding: bool, report: dict | None) -> int:
    """报告存在但点位为空的期望槽位数 (skipped 计数); 无报告票无槽位不计。"""
    if not isinstance(report, dict):
        return 0
    points = _report_points(report)
    if holding:
        slots = sum(1 for k in ("stop_loss", "take_profit", "secondary_buy") if points.get(k) is None)
        slots += sum(
            1 for item in _risk_conditions(report)
            if isinstance(item, dict) and item.get("kind") == "reduce" and item.get("price") is None
        )
        return slots
    return 1 if points.get("ideal_buy") is None else 0


def _empty_point_kinds(holding: bool, report: dict | None) -> frozenset[str]:
    """报告缺点位的点位列 kind 集 (§4.3-1 槽位级保留的输入)。

    持仓票: stop_loss/take_profit/secondary_buy(add); 空仓票: ideal_buy(entry)。
    risk_conditions 的 reduce 属列表语义 (报告更新即整列替换), 不在保留范围。
    """
    if not isinstance(report, dict):
        return frozenset()
    points = _report_points(report)
    if holding:
        return frozenset(
            kind for key, kind in (
                ("stop_loss", "stop_loss"), ("take_profit", "take_profit"),
                ("secondary_buy", "add"),
            ) if points.get(key) is None
        )
    return frozenset({"entry"}) if points.get("ideal_buy") is None else frozenset()


def derive_expected_rules(
    sym6: str, suffixed: str, holding: bool, report: dict | None,
    atr_20_pct: float | None = None,
) -> list[ExpectedRule]:
    """最新报告 → 该票期望规则集 (契约 §4.2 推导表 + §4.5-1 阶梯; 点位为空的项不建规则)。

    sym6/suffixed 参数保留 (便于测试直调与后续按票特化), 当前推导只依赖
    holding 与 report。主规则 (除 entry) 各配 near/mid 两条 ATR 阶梯预警。
    """
    del sym6, suffixed
    if not isinstance(report, dict):
        return []
    points = _report_points(report)
    rules: list[ExpectedRule] = []
    if holding:
        for key, kind in (("stop_loss", "stop_loss"), ("take_profit", "take_profit"),
                          ("secondary_buy", "add")):
            price = points.get(key)
            if price is not None:
                rules.append(ExpectedRule(kind, 1, _RULE_OP_BY_KIND[kind], float(price),
                                          _RULE_SEVERITY_BY_KIND[kind]))
        prices = sorted({
            round(float(item["price"]), 2)
            for item in _risk_conditions(report)
            if isinstance(item, dict) and item.get("kind") == "reduce" and item.get("price") is not None
        })
        # 同价去重 (round 到分): LLM 重复条目否则会建同条件双规则、同 tick 双告警
        rules.extend(
            ExpectedRule("reduce", i + 1, "<=", price, "warn")
            for i, price in enumerate(prices)
        )
    else:
        price = points.get("ideal_buy")
        if price is not None:
            rules.append(ExpectedRule("entry", 1, "<=", float(price), "info"))
    return rules


def with_ladder_rules(rules: list[ExpectedRule], atr_20_pct: float | None) -> list[ExpectedRule]:
    """§4.5-1: 主规则 (除 entry) 各配接近/逼近阶梯, 返回 主规则 + 阶梯。

    与主规则同生同灭同对账 (阶梯在期望集内, diff/保留/删除语义随 base kind)。
    derive_expected_rules 保持只产主规则 (纯报告推导), 阶梯由本函数叠加。
    """
    ladder: list[ExpectedRule] = []
    for r in rules:
        if r.kind not in _LADDER_BASE_KINDS:
            continue
        for tier in ("near", "mid"):
            ladder.append(ExpectedRule(
                f"{tier}_{r.kind}", r.rank, r.op,
                _ladder_price(r.price, r.op, tier, atr_20_pct),
                _LADDER_TIER_SEVERITY[tier],
            ))
    return rules + ladder


@dataclass(frozen=True)
class _ParsedRule:
    sym6: str
    kind: str
    rank: int
    op: str | None
    price: float | None
    severity: str | None
    rule_id: str
    enabled: bool


def parse_dsa_rule(rule: dict) -> _ParsedRule | None:
    """解析 DSA· 前缀规则的身份与内容; 解析不出 → None (不托管不比对不删)。

    sym6 以 symbols[0] 归一为准, kind/rank 从 name 尾段解析
    (DSA·{sym6}·{kind} 或 DSA·{sym6}·reduce·{rank}); 五 kind 之外、symbols
    缺失、price 非数值的仿命名规则一律 None, 防误删用户仿命名规则。
    """
    name = rule.get("name")
    if not isinstance(name, str) or not name.startswith(NAME_PREFIX):
        return None
    parts = name.split("·")
    kind = parts[2] if len(parts) >= 3 else None
    if kind not in _KINDS:
        # §4.5-1 阶梯命名 near_/mid_ 前缀: base kind 须可解析且在阶梯白名单
        base = _base_kind(kind) if kind else None
        if base is None or base == kind or base not in _LADDER_BASE_KINDS:
            return None
    rank = 1
    if len(parts) >= 4:
        try:
            rank = int(parts[3])
        except ValueError:
            return None
        if rank < 1 or len(parts) > 4:
            return None
    symbols = rule.get("symbols")
    sym6 = _norm6(symbols[0]) if isinstance(symbols, list) and symbols else None
    if sym6 is None:
        return None
    conds = rule.get("conditions")
    first = conds[0] if isinstance(conds, list) and conds and isinstance(conds[0], dict) else {}
    price = first.get("value")
    if not _is_number(price):
        return None
    severity = rule.get("severity")
    return _ParsedRule(
        sym6=sym6, kind=kind, rank=rank, op=first.get("op"), price=float(price),
        severity=severity if severity in ("info", "warn", "critical") else None,
        rule_id=str(rule.get("id")), enabled=rule.get("enabled") is not False,
    )


def _content_differs(rule: dict, expected: ExpectedRule) -> bool:
    """现存规则内容与期望是否不一致 (op/price/severity/message; enabled 不参与比较)。"""
    conds = rule.get("conditions")
    first = conds[0] if isinstance(conds, list) and conds and isinstance(conds[0], dict) else {}
    price = first.get("value")
    return (
        first.get("op") != expected.op
        or not _is_number(price)
        or abs(float(price) - expected.price) > 1e-9
        or rule.get("severity") != expected.severity
        or rule.get("message") != _rule_message(expected)
    )


@dataclass
class _DiffResult:
    creates: list[tuple[ExpectedSpec, ExpectedRule]]
    updates: list[tuple[dict, ExpectedRule]]
    removes: list[str]          # 自选票多余键 (reduce 降档、清仓换 entry 等)
    orphan_deletes: list[str]   # 移出自选票的全部规则
    skipped: int                # 点位为空的期望槽位数


def diff_rules(specs: list[ExpectedSpec], existing_rules: list[dict]) -> _DiffResult:
    """期望集 vs 现存集 diff (纯函数, 不碰 IO)。

    skip_diff 票 (持仓票无报告/点位全空、指数票) 的既有规则全部保留; 部分缺点位
    票缺位槽位的既有规则按 preserve_kinds 保留, 给出的点位照常比对; 删除只发生
    在移出自选与清仓两个票级触发器 + 键级多余 (reduce 从 2 条变 1 条等列表语义)。
    """
    managed: dict[tuple[str, str, int], dict] = {}
    for rule in existing_rules:
        if not isinstance(rule, dict) or not str(rule.get("name", "")).startswith(NAME_PREFIX):
            continue  # 人工规则, 不进任何写路径
        parsed = parse_dsa_rule(rule)
        if parsed is None:
            logger.warning(
                "dsa watch: 外来 DSA· 命名规则不托管 (id=%s name=%r)", rule.get("id"), rule.get("name"),
            )
            continue
        key = (parsed.sym6, parsed.kind, parsed.rank)
        cur = managed.get(key)
        # 同键脏数据: 优先保留自家确定性 id 的规则
        if cur is None or (
            str(rule.get("id", "")).startswith("dsa_")
            and not str(cur.get("id", "")).startswith("dsa_")
        ):
            managed[key] = rule
    creates: list[tuple[ExpectedSpec, ExpectedRule]] = []
    updates: list[tuple[dict, ExpectedRule]] = []
    removes: list[str] = []
    skipped = 0
    spec_by_sym = {spec.sym6: spec for spec in specs}
    for spec in specs:
        skipped += spec.empty_slots
        if spec.skip_diff:
            continue
        expected = {(r.kind, r.rank): r for r in spec.rules}
        actual = {(k[1], k[2]): r for k, r in managed.items() if k[0] == spec.sym6}
        for key, er in expected.items():
            cur = actual.get(key)
            if cur is None:
                creates.append((spec, er))
            elif _content_differs(cur, er):
                updates.append((cur, er))
        removes.extend(
            r["id"] for k, r in actual.items()
            if k not in expected and _base_kind(k[0]) not in spec.preserve_kinds
        )
    orphan_deletes = [
        r["id"] for (sym6, _kind, _rank), r in managed.items() if sym6 not in spec_by_sym
    ]
    return _DiffResult(creates, updates, removes, orphan_deletes, skipped)


# ================================================================
# 规则读写 (save_one 是裸写, 调用方必须自调 normalize+validate)
# ================================================================

def _build_rule(spec: ExpectedSpec, expected: ExpectedRule) -> dict:
    rule = {
        "id": _rule_id(spec.suffixed, expected.kind, expected.rank),
        "name": _rule_name(spec.sym6, expected.kind, expected.rank),
        "type": "price",
        "scope": "symbols",
        "symbols": [spec.suffixed],
        "asset_type": spec.asset_type,
        "conditions": [{"field": "close", "op": expected.op, "value": expected.price}],
        "severity": expected.severity,
        "cooldown_seconds": RULE_COOLDOWN_SECONDS,
        "logic": "and",
        "enabled": True,
        "message": _rule_message(expected),
    }
    return monitor_rules.normalize(rule)


def _latest_reports() -> dict[str, dict]:
    """报告库按 sym6 取最新 (list_reports 已按 created_at 倒序)。"""
    latest: dict[str, dict] = {}
    for report in dsa_analysis._STORE.list_reports(None):
        sym6 = _norm6(report.get("symbol"))
        if sym6 and sym6 not in latest:
            latest[sym6] = report
    return latest


def _resolve_asset_type(repo, suffixed: str) -> str:
    try:
        return repo.resolve_asset_type(suffixed) if repo is not None else "stock"
    except Exception as e:
        logger.warning("dsa watch: resolve_asset_type(%s) 失败, 回退 stock: %s", suffixed, e)
        return "stock"


def _watch_symbols(entries: list[dict], positions: dict[str, dict], repo) -> list[str]:
    """盯盘集合 (契约 §4.1): 持仓在前、其余按自选顺序, 按 sym6 去重。

    各 symbol 经 dsa_analysis._canonical_symbol 规范化为后缀点分式 (DSA 库内
    裸 6 位/SZ 前缀式 → 000636.SZ 形态; 已是后缀式幂等)。无法判定交易所的码
    原样保留 (不编造, 自然走"暂无日K")。
    """
    ordered: list[str] = []
    seen: set[str] = set()

    def _push(raw: Any) -> None:
        text = str(raw or "").strip()
        if not text:
            return
        symbol = dsa_analysis._canonical_symbol(repo, text)
        sym6 = _norm6(symbol)
        if sym6 is None or sym6 in seen:
            return
        seen.add(sym6)
        ordered.append(symbol)

    for sym6 in sorted(positions):  # 持仓在前 (sym6 字典序, 输出确定性)
        _push(sym6)
    for entry in entries:
        _push(entry.get("symbol"))
    return ordered


def _build_specs(
    symbols: list[str], positions: dict[str, dict], latest: dict[str, dict], repo,
) -> list[ExpectedSpec]:
    """自选票 (保序去重) → 期望 spec 列表。symbols 为带后缀原形。"""
    specs: list[ExpectedSpec] = []
    seen: set[str] = set()
    for suffixed in symbols:
        sym6 = _norm6(suffixed)
        if sym6 is None or sym6 in seen:
            continue
        seen.add(sym6)
        holding = sym6 in positions
        report = latest.get(sym6)
        asset_type = _resolve_asset_type(repo, suffixed)
        rules = derive_expected_rules(sym6, suffixed, holding, report)
        if any(r.kind in _LADDER_BASE_KINDS for r in rules):
            # §4.5-1: 有主规则才值得扫 K 线算 ATR; 读不到则阶梯带宽取默认 1.0%
            atr = _atr_20_pct(repo, suffixed, asset_type)
            rules = with_ladder_rules(rules, atr)
        empty_slots = _count_empty_point_slots(holding, report)
        preserve = _empty_point_kinds(holding, report)
        skip = False
        if asset_type == "index":
            # 指数票 (如 000001.SH 撞码上证指数) 无盯盘语义: 不建规则, 既有
            # 规则保留不动 (边缘防护; 当前自选无指数码)
            logger.info("dsa watch: 指数 %s 跳过 DSA 规则对账", suffixed)
            rules, skip, empty_slots = [], True, 0
        elif holding and (report is None or not rules):
            # 持仓票无报告/点位全空: 票级保留既有规则 (防 LLM 围栏解析闪失
            # 当晚清掉用户止损线); 部分缺点位走 preserve_kinds 槽位级保留;
            # 清仓票不 skip → 删全部
            skip = True
        specs.append(ExpectedSpec(
            sym6=sym6, suffixed=suffixed, holding=holding, rules=rules, skip_diff=skip,
            empty_slots=empty_slots, preserve_kinds=preserve, asset_type=asset_type,
            report_created_at=str(report.get("created_at")) if report else None,
        ))
    return specs


def _reload_engine(engine, repo, data_dir: Path, changed: bool) -> None:
    """镜像 api/monitor_rules._sync_engine: 写盘后把最新规则集喂给引擎。

    engine 未捕获时: 若本轮确有规则变更则置 _ENGINE_FLUSH_PENDING, 待任一路由
    捕获到 engine 后由 capture_engine 补 flush; flush 成功才清位, 失败保留重试。
    """
    global _ENGINE_FLUSH_PENDING
    if engine is None:
        if changed:
            _ENGINE_FLUSH_PENDING = True
            logger.warning(
                "dsa watch: 规则已落盘, monitor 引擎未捕获 (engine 引用未捕获), "
                "已置 pending 待下次捕获后 flush",
            )
        else:
            logger.warning(
                "dsa watch: 规则已落盘, monitor 引擎未重载 (engine 引用未捕获, "
                "将在下次 dsa_watch/monitor API 请求或重启后生效)",
            )
        return
    try:
        from app.api.monitor_rules import _reconcile_index_asset_type

        engine.set_rules([
            _reconcile_index_asset_type(r, repo) for r in monitor_rules.load_all(data_dir)
        ])
        _ENGINE_FLUSH_PENDING = False
    except Exception as e:
        logger.warning("dsa watch: monitor 引擎重载失败: %s", e)


def run_reconcile(data_dir: Path | None, repo, engine=None) -> dict:
    """对账主入口: 前置读取 → 纯 diff → 应用 → 引擎重载。任一前置失败零写入。

    三入口 (任务收尾/调度兜底/手动 POST) 经 _SYNC_LOCK 串行; 成功后刷新
    _last_watch_sync (30min 兜底门控时钟)。报告库统一走 dsa_analysis._STORE
    单例 (其目录每调用懒解析 settings.data_dir, 测试经 data_dir fixture 隔离)。
    """
    global _last_watch_sync
    with _SYNC_LOCK:
        data_dir = Path(data_dir) if data_dir is not None else _runtime_data_dir()
        # —— 前置读取 (顺序固定; 异常≠空值: 空自选是合法用户态, 异常是故障) ——
        try:
            positions, _positions_source = load_positions()
        except BridgeUnavailableError as e:
            # load_positions 消息已自述来源 (本账/桥), 直接透传, 零写入中止
            raise SyncError(str(e)) from None
        try:
            entries = watchlist.list_symbols()
        except Exception as e:
            raise SyncError(f"自选读取失败: {e}") from None
        symbols = _watch_symbols(entries, positions, repo)
        latest = _latest_reports()
        try:
            existing = monitor_rules.load_all(data_dir)
        except Exception as e:
            raise SyncError(f"规则存储不可用: {e}") from None

        specs = _build_specs(symbols, positions, latest, repo)
        diff = diff_rules(specs, existing)

        # —— 应用 (仅 DSA· 托管集; create 前查 id 占用防覆写用户改名规则) ——
        created = updated = removed = 0
        id_conflicts = 0
        for spec, er in diff.creates:
            rid = _rule_id(spec.suffixed, er.kind, er.rank)
            if monitor_rules.load_one(data_dir, rid) is not None:
                # id 被占用但已挤出托管集 (用户在监控 UI 改过名): 不静默覆写
                logger.warning("dsa watch: 规则 id %s 已被占用且不在托管集, 跳过创建", rid)
                id_conflicts += 1
                continue
            rule = _build_rule(spec, er)
            try:
                monitor_rules.validate(rule)
            except ValueError as e:
                logger.warning("dsa watch: 规则 %s 校验失败, 跳过: %s", rid, e)
                id_conflicts += 1
                continue
            try:
                monitor_rules.save_one(data_dir, rule)
            except OSError as e:
                logger.warning("dsa watch: 规则 %s 落盘失败, 跳过: %s", rid, e)
                id_conflicts += 1
                continue
            created += 1
        for cur, er in diff.updates:
            rule = dict(cur)
            rule["conditions"] = [{"field": "close", "op": er.op, "value": er.price}]
            rule["severity"] = er.severity  # enabled/created_at/name/symbols 等全保留
            rule["message"] = _rule_message(er)
            try:
                monitor_rules.validate(rule)
            except ValueError as e:
                logger.warning("dsa watch: 规则 %s 更新校验失败, 跳过: %s", rule.get("id"), e)
                id_conflicts += 1
                continue
            try:
                monitor_rules.save_one(data_dir, rule)
            except OSError as e:
                logger.warning("dsa watch: 规则 %s 落盘失败, 跳过: %s", rule.get("id"), e)
                id_conflicts += 1
                continue
            updated += 1
        for rid in (*diff.removes, *diff.orphan_deletes):
            try:
                if monitor_rules.delete_one(data_dir, rid):
                    removed += 1
            except OSError as e:
                # 单票落盘异常记日志继续, 不拖垮整轮 (其余票照常对账)
                logger.warning("dsa watch: 规则 %s 删除失败, 跳过: %s", rid, e)

        _RUNTIME["data_dir"] = data_dir  # pending flush 按刚对账过的目录读规则
        _reload_engine(engine, repo, data_dir, bool(created or updated or removed))
        _last_watch_sync = time.monotonic()
        result = {
            "created": created, "updated": updated, "removed": removed,
            "skipped": diff.skipped + id_conflicts,
        }
        if created or updated or removed:
            logger.info("dsa watch: 对账完成 %s", result)
        return result


# ================================================================
# watch-plan 聚合
# ================================================================

def _project_report(report: dict) -> dict:
    """报告 → 契约六字段投影 (points/phase_decision 原样, 前端按空值隐藏)。"""
    return {
        "id": report.get("id"),
        "created_at": report.get("created_at"),
        "operation_advice": report.get("operation_advice"),
        "sentiment_score": report.get("sentiment_score"),
        "points": report.get("points"),
        "phase_decision": report.get("phase_decision"),
    }


def _judge_sync_state(
    spec: ExpectedSpec, actual: dict[tuple[str, int], _ParsedRule], rules_unavailable: bool,
) -> str:
    """sync_state 纯判定: synced/stale/no_points/no_report (契约 §4.1 注释)。

    enabled 不参与 synced 比较 (用户手动停用仍算 synced, 规则内容与报告一致
    只是被静音); 规则读失败降级为 stale (点位存在但状态未知, 保守提示同步)。
    """
    if not spec.rules:
        return "no_report" if spec.report_created_at is None else "no_points"
    if rules_unavailable:
        return "stale"
    expected = {(r.kind, r.rank): (r.op, round(r.price, 6), r.severity) for r in spec.rules}
    actual_map = {
        (p.kind, p.rank): (p.op, round(p.price, 6), p.severity)
        for p in actual.values() if _base_kind(p.kind) not in spec.preserve_kinds
    }
    return "synced" if expected == actual_map else "stale"


def build_watch_plan(repo, data_dir: Path | None) -> dict:
    """GET /watch-plan 主体。恒读磁盘现算不缓存; 永不抛 (桥不可用=全空仓 200)。"""
    data_dir = Path(data_dir) if data_dir is not None else _runtime_data_dir()
    try:
        entries = watchlist.list_symbols()
    except Exception as e:
        logger.warning("dsa watch: 自选读取失败, watch-plan 返回空清单: %s", e)
        entries = []
    try:
        positions, positions_source = load_positions()
    except BridgeUnavailableError as e:
        logger.warning("dsa watch: 持仓读取降级, 全量按空仓处理: %s", e)
        # 降级态保持 P2 契约值 (来源链末端是桥; 无任何来源成功时无从如实上报)
        positions, positions_source = {}, "dsa_bridge"
    latest = _latest_reports()
    try:
        existing = monitor_rules.load_all(data_dir)
        rules_unavailable = False
    except Exception as e:
        logger.warning("dsa watch: 规则读取失败, rules 降级为空: %s", e)
        existing, rules_unavailable = [], True

    parsed_by_sym: dict[str, dict[tuple[str, int], _ParsedRule]] = {}
    rules_by_sym: dict[str, list[dict]] = {}
    for rule in existing:
        if not isinstance(rule, dict) or not str(rule.get("name", "")).startswith(NAME_PREFIX):
            continue
        parsed = parse_dsa_rule(rule)
        if parsed is None:
            continue  # 外来 DSA· 命名不展示 (防 price 异常打爆前端渲染)
        parsed_by_sym.setdefault(parsed.sym6, {})[(parsed.kind, parsed.rank)] = parsed
        rules_by_sym.setdefault(parsed.sym6, []).append({
            "rule_id": parsed.rule_id,
            "kind": parsed.kind,
            "price": parsed.price,
            "severity": parsed.severity,
            "enabled": parsed.enabled,
        })

    specs = _build_specs(
        _watch_symbols(entries, positions, repo),
        positions, latest, repo,
    )
    names: dict[str, str] = {}
    if specs and repo is not None:
        try:
            names = repo.get_name_map([s.suffixed for s in specs]) or {}
        except Exception as e:
            logger.warning("dsa watch: 名称解析失败: %s", e)

    items: list[dict] = []
    for spec in specs:
        pos = positions.get(spec.sym6)
        report = latest.get(spec.sym6)
        items.append({
            "symbol": spec.suffixed,  # 契约 §3.1 注记: 对外 symbol 统一后缀点分式
            "name": names.get(spec.suffixed),
            "holding": spec.holding,
            "quantity": pos["quantity"] if pos else None,
            "avg_cost": pos["avg_cost"] if pos else None,
            "report": _project_report(report) if report else None,
            "rules": rules_by_sym.get(spec.sym6, []),
            "sync_state": _judge_sync_state(
                spec, parsed_by_sym.get(spec.sym6, {}), rules_unavailable,
            ),
        })
    items.sort(key=lambda item: not item["holding"])  # 稳定排序: 持仓在前, 其余按自选序
    return {
        "generated_at": dsa_analysis._now_iso(),
        "positions_source": positions_source,
        "items": items,
    }


# ================================================================
# 盘前自检 (调度线程每交易日 09:10-11:30 跑一次; GET 纯读状态文件)
# ================================================================

def _premarket_path(data_dir: Path) -> Path:
    return Path(data_dir) / "user_data" / _PREMARKET_STATE_DIR / "premarket.json"


def _read_premarket_state(data_dir: Path) -> dict | None:
    """状态文件 → dict; 缺失/损坏/缺 date 视为未跑并记日志。"""
    try:
        state = json.loads(_premarket_path(data_dir).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning("dsa watch: premarket 状态文件读取失败, 视为未跑: %s", e)
        return None
    if not isinstance(state, dict) or not state.get("date"):
        logger.warning("dsa watch: premarket 状态文件损坏, 视为未跑")
        return None
    return state


def _write_premarket_state(data_dir: Path, state: dict) -> None:
    p = _premarket_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(state, ensure_ascii=False, indent=2))


def _prev_trading_date(now: datetime) -> date | None:
    """上一交易日; 从昨天向前回溯, is_trading_day 未知 (None) 视为交易日。"""
    day = now.date() - timedelta(days=1)
    for _ in range(15):
        probe = datetime(day.year, day.month, day.day, 12, 0)
        if is_trading_day(probe) is not False:
            return day
        day -= timedelta(days=1)
    return None


def _last_close_str(df) -> str | None:
    """DataFrame 最后一行 date 的 ISO 前 10 位 (date 列缺失/空表 → None)。"""
    if df.height == 0 or "date" not in df.columns:
        return None
    return str(df.tail(1)["date"][0])[:10]


def _check_data_source(repo, symbols: list[str]) -> dict:
    if not symbols:
        return {"name": "数据源", "status": "ok", "detail": "自选为空, 跳过探针"}
    symbol = symbols[0]
    df = dsa_analysis._load_kline(repo, symbol)
    if df.is_empty():
        return {"name": "数据源", "status": "fail", "detail": f"首只自选 {symbol} K线读取为空"}
    last_date = _last_close_str(df)
    prev = _prev_trading_date(cn_now())
    if last_date is None:
        return {"name": "数据源", "status": "warn", "detail": f"{symbol} K线缺 date 列, 无法核新鲜度"}
    if prev and last_date < prev.isoformat():
        return {
            "name": "数据源", "status": "warn",
            "detail": f"最新K线 {last_date} 落后于上一交易日 {prev.isoformat()}",
        }
    return {"name": "数据源", "status": "ok", "detail": f"首只自选 {symbol} 最新K线 {last_date}"}


def _check_kline_coverage(repo, symbols: list[str]) -> dict:
    prev = _prev_trading_date(cn_now())
    if not symbols:
        return {"name": "自选K线覆盖", "status": "ok", "detail": "自选为空"}
    if prev is None:
        return {"name": "自选K线覆盖", "status": "warn", "detail": "上一交易日不可判定, 跳过覆盖检查"}
    today = cn_now().date()
    missing: list[str] = []
    for suffixed in symbols:
        asset_type = _resolve_asset_type(repo, suffixed)
        try:
            df = repo.get_daily_asset(asset_type, suffixed, start=prev, end=today)
        except Exception as e:
            return {
                "name": "自选K线覆盖", "status": "fail",
                "detail": f"{suffixed} K线读取失败: {e}"[:_MAX_DETAIL_LEN],
            }
        last_date = _last_close_str(df)
        if last_date is None or last_date < prev.isoformat():
            missing.append(suffixed)
    if missing:
        preview = ", ".join(missing[:5]) + ("…" if len(missing) > 5 else "")
        return {
            "name": "自选K线覆盖", "status": "warn",
            "detail": f"{len(missing)} 只缺 {prev.isoformat()} K线: {preview}",
        }
    return {"name": "自选K线覆盖", "status": "ok", "detail": f"{len(symbols)} 只自选 {prev.isoformat()} K线齐备"}


def _check_sync_rules(data_dir: Path, repo, positions: dict[str, dict]) -> dict:
    existing = monitor_rules.load_all(data_dir)
    managed = [r for r in existing if isinstance(r, dict) and str(r.get("name", "")).startswith(NAME_PREFIX)]
    enabled = sum(1 for r in managed if r.get("enabled") is not False)
    plan = build_watch_plan(repo, data_dir)
    stale = sum(1 for item in plan["items"] if item["sync_state"] == "stale")
    try:
        # 盯盘集合是持仓+自选, "持仓不在自选"需直接对照自选表 (提醒补自选)
        watch6 = {
            _norm6(str(e.get("symbol") or "").strip())
            for e in watchlist.list_symbols()
        } - {None}
    except Exception:
        watch6 = set()
    unlisted = len(set(positions) - watch6)
    detail = f"同步规则 {len(managed)} 条 (启用 {enabled}/停用 {len(managed) - enabled})"
    if unlisted:
        detail += f", 持仓不在自选 {unlisted} 只"
    if stale:
        return {"name": "同步规则启用", "status": "warn", "detail": f"{stale} 只票规则待同步 ({detail})"}
    return {"name": "同步规则启用", "status": "ok", "detail": detail}


def run_premarket_checks(data_dir: Path, repo) -> list[dict]:
    """四检查项 (各独立容错, 异常→该项 fail + 截断 detail, 互不拖垮)。"""
    checks: list[dict] = []
    data_dir = Path(data_dir)
    try:
        entries = watchlist.list_symbols()
        symbols = [str(e.get("symbol")).strip() for e in entries if e.get("symbol")]
        watchlist_error = None
    except Exception as e:
        symbols, watchlist_error = [], str(e)
    try:
        raw_rows = _fetch_position_rows()
        bridge_positions = merge_position_rows(
            [r for r in (_clean_position_row(row) for row in raw_rows) if r is not None],
        )
        bridge_error = None
    except BridgeUnavailableError as e:
        raw_rows, bridge_positions, bridge_error = [], {}, str(e)
    try:
        # "持仓不在自选"提示按实际持仓来源 (§4.4.3 本账优先); 失败按空仓不阻塞
        positions, _positions_source = load_positions()
    except BridgeUnavailableError:
        positions = {}

    def _safe(name: str, fn) -> None:
        try:
            check = fn()
        except Exception as e:
            logger.warning("dsa watch: premarket 检查 %s 异常: %s", name, e)
            check = {"name": name, "status": "fail", "detail": str(e)}
        checks.append({**check, "detail": str(check.get("detail") or "")[:_MAX_DETAIL_LEN]})

    if watchlist_error is not None:
        detail = f"自选读取失败: {watchlist_error}"[:_MAX_DETAIL_LEN]
        checks.append({"name": "数据源", "status": "fail", "detail": detail})
        checks.append({"name": "自选K线覆盖", "status": "fail", "detail": detail})
    else:
        _safe("数据源", lambda: _check_data_source(repo, symbols))
        _safe("自选K线覆盖", lambda: _check_kline_coverage(repo, symbols))
    _safe("同步规则启用", lambda: _check_sync_rules(data_dir, repo, positions))
    if bridge_error is not None:
        checks.append({
            "name": "DSA桥可用", "status": "fail",
            "detail": f"旧库不可用, 持仓按空仓处理 ({bridge_error})"[:_MAX_DETAIL_LEN],
        })
    else:
        _safe("DSA桥可用", lambda: {
            "name": "DSA桥可用", "status": "ok",
            "detail": f"{len(bridge_positions)} 只持仓 ({len(raw_rows)} 行)",
        })
    return checks


def _decide_premarket_run(
    *, now: datetime, last_date: date | None, trading_day: bool | None,
) -> tuple[bool, str]:
    """盘前自检判定纯函数: 交易日 → 09:10-11:30 窗口 → 当日一次。"""
    if trading_day is None:
        logger.info("dsa premarket: is_trading_day 未知, 退化为周一~五判断 (%s)", now.date())
        is_open = now.weekday() < 5
    else:
        is_open = trading_day
    if not is_open:
        return False, "not_trading_day"
    start_h, start_m = (int(x) for x in _PREMARKET_WINDOW[0].split(":"))
    end_h, end_m = (int(x) for x in _PREMARKET_WINDOW[1].split(":"))
    if not (start_h * 60 + start_m <= now.hour * 60 + now.minute <= end_h * 60 + end_m):
        return False, "out_of_window"
    if last_date == now.date():
        return False, "already_ran"
    return True, "trigger"


# ================================================================
# 挂接点: 任务收尾钩子 / 调度兜底 tick / 运行时捕获
# ================================================================

def _runtime_data_dir() -> Path:
    data_dir = _RUNTIME.get("data_dir")
    if data_dir is not None:
        return Path(data_dir)
    from app.config import settings

    return Path(settings.data_dir)


def on_task_done(task: dict) -> None:
    """分析任务收尾对账 (dsa_analysis._process_task 置 done 后调用)。

    全吞异常: 对账失败绝不影响任务终态 (item 全成功的任务不能被冤枉成 failed)。
    """
    try:
        repo = task.get("_repo")
        data_dir = task.get("_data_dir") or _runtime_data_dir()
        result = run_reconcile(data_dir, repo, _RUNTIME.get("engine"))
        logger.info("dsa watch: 任务收尾对账完成 %s", result)
    except Exception as e:
        logger.warning("dsa watch: 任务收尾对账跳过: %s", e)


def _watch_fallback_tick() -> None:
    """30min 门控兜底对账 (跑完/尝试完都刷新时钟)。"""
    global _last_watch_sync
    now_mono = time.monotonic()
    if _last_watch_sync and now_mono - _last_watch_sync < _WATCH_FALLBACK_INTERVAL_SECONDS:
        return
    _last_watch_sync = now_mono
    try:
        result = run_reconcile(_runtime_data_dir(), _RUNTIME.get("repo"), _RUNTIME.get("engine"))
        logger.info("dsa watch: 兜底对账完成 %s", result)
    except Exception as e:
        logger.warning("dsa watch: 兜底对账跳过: %s", e)


def _premarket_tick(now: datetime) -> None:
    """盘前自检门控 (每交易日窗口内一次, 结果原子落盘)。"""
    global _last_premarket_date
    try:
        try:
            trading_day = is_trading_day(now)
        except Exception:
            trading_day = None
        data_dir = _runtime_data_dir()
        run, reason = _decide_premarket_run(
            now=now, last_date=_last_premarket_date, trading_day=trading_day,
        )
        if not run:
            if reason not in ("out_of_window", "not_trading_day"):
                logger.info("dsa premarket: 到窗但跳过 (%s)", reason)
            return
        checks = run_premarket_checks(data_dir, _RUNTIME.get("repo"))
        _write_premarket_state(data_dir, {
            "date": now.date().isoformat(), "ran_at": dsa_analysis._now_iso(), "checks": checks,
        })
        _last_premarket_date = now.date()
        logger.info("dsa premarket: 自检完成 (%d 项)", len(checks))
    except Exception as e:
        logger.warning("dsa watch: 盘前自检跳过: %s", e)


def scheduler_tick(now: datetime | None = None) -> None:
    """调度周期兜底 (dsa_analysis._schedule_loop 30s 一拍调用): 兜底对账 + 盘前自检。"""
    now = now or cn_now()
    try:
        _watch_fallback_tick()
    except Exception:
        logger.exception("dsa watch fallback tick failed")
    try:
        _premarket_tick(now)
    except Exception:
        logger.exception("dsa watch premarket tick failed")


def capture_engine(engine) -> None:
    """捕获 monitor 引擎引用 (进程级单例, 持引用安全); None 不覆盖已有引用。

    有挂起中的规则变更 (对账时 engine 未捕获) 则立即补 flush——后台路径
    (任务收尾/调度兜底) 写盘后引擎未就绪的规则由此进入引擎内存。
    """
    if engine is None:
        return
    _RUNTIME["engine"] = engine
    if _ENGINE_FLUSH_PENDING:
        try:
            _reload_engine(engine, _RUNTIME.get("repo"), _runtime_data_dir(), changed=True)
        except Exception as e:
            logger.warning("dsa watch: pending flush 失败: %s", e)


def _capture_runtime(request: Request) -> None:
    """handler 首行捕获 repo/data_dir/engine (用户点过分析按钮/打开过盯盘页即捕获)。"""
    repo = getattr(request.app.state, "repo", None)
    if repo is not None:
        _RUNTIME["repo"] = repo
    data_dir = getattr(getattr(repo, "store", None), "data_dir", None)
    if data_dir is None:
        data_dir = _RUNTIME.get("data_dir")
    if data_dir is None:
        from app.config import settings

        data_dir = settings.data_dir
    _RUNTIME["data_dir"] = Path(data_dir)
    capture_engine(getattr(request.app.state, "monitor_engine", None))


def _resolve_runtime(request: Request) -> tuple[Any, Path]:
    """镜像 dsa_analysis._resolve_runtime: repo.store.data_dir → _RUNTIME → settings。"""
    return dsa_analysis._resolve_runtime(request)


def _read_premarket_public(data_dir: Path) -> dict:
    """GET /premarket/status: 纯读状态文件, date≠今日 → 今日空态, 永不 5xx。"""
    today = cn_now().date().isoformat()
    try:
        state = _read_premarket_state(data_dir)
    except Exception as e:
        logger.warning("dsa watch: premarket 状态读取失败: %s", e)
        state = None
    if not state or state.get("date") != today:
        return {"date": today, "ran_at": None, "checks": []}
    checks = state.get("checks") if isinstance(state.get("checks"), list) else []
    return {"date": state.get("date"), "ran_at": state.get("ran_at"), "checks": checks}


# ================================================================
# HTTP 路由 (契约 §4.1; 三条新路径与既有路由零冲突)
# ================================================================

def setup(registrar: BackendExtensionRegistrar) -> None:
    router = APIRouter(prefix="/api/ext/dsa", tags=["dsa-watch"])

    @router.get("/watch-plan")
    def get_watch_plan(request: Request) -> dict:
        _capture_runtime(request)
        repo, data_dir = _resolve_runtime(request)
        return build_watch_plan(repo, data_dir)

    @router.post("/watch-sync/run")
    def run_watch_sync(request: Request) -> dict:
        _capture_runtime(request)
        repo, data_dir = _resolve_runtime(request)
        try:
            return run_reconcile(data_dir, repo, _RUNTIME.get("engine"))
        except SyncError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None

    @router.get("/premarket/status")
    def get_premarket_status(request: Request) -> dict:
        _capture_runtime(request)
        _repo, data_dir = _resolve_runtime(request)
        return _read_premarket_public(data_dir)

    registrar.include_router(router)


def startup(context: ExtensionContext) -> None:
    """只填运行时 (repo/data_dir), 不自起线程 — 兜底与盘前 tick 复用
    dsa_analysis 调度循环 (单线程单 30s 睡眠; loader 按 sorted 次序 startup,
    dsa_watch 排在 dsa_analysis 之后, 调度首拍前运行时已就绪)。
    """
    _RUNTIME["repo"] = context.repository
    _RUNTIME["data_dir"] = Path(context.data_dir)
