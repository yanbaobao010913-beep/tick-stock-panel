"""DSA 报告点位 → TSP 核心模拟盘自动下单桥 (迁移计划 §4.7-1 B1)。

方案 B 拍板: 模拟盘在 TSP 核心上改 (核心 paper.py 已支持 conditional 条件触发
单, 撮合/费用/T+1/涨跌停校验全部复用 _fill_order), 本桥是扩展侧喂单器——把 DSA
分析报告的点位转成核心模拟盘 default 账户的挂单:

  - 空仓票 + 最新报告 ideal_buy → 次日 conditional 买单 (trigger_op="<=",
    expire="day", 金额 = 权益/槽位数; 槽数 10 与 DSA §4.6-3 口径一致)
  - 模拟盘持仓票 + 最新报告 stop_loss → GTC conditional 止损卖单
    (trigger_op="<=", 数量 = 当前可卖向下取整百); 报告更新 (新报告 id) → 撤旧建新
  - 持仓票 + 卖出类建议 (文本含 卖出/减仓/清仓, DSA §4.6-1 映射) → next_open
    卖单 (全量可卖整百); 卖出意图优先于止损线 (本拍撤全部止损单)
  - take_profit 是移动止盈启动价, TSP 模拟盘无回撤跟踪引擎, 不出单 (§4.7-1
    如实标注); 空仓票的卖出类建议不建入场单 (报告自相矛盾时以保守侧为准)

幂等/去重: 订单 source 标记来源报告 (dsa:{report_id}[:stop|:exit]); 同 source
订单在任何状态下存在即不重建——day 入场单未触发作废后不顺延重挂 (DSA 入场
口径「当根作废」), 报告未更新不反复重建。对账为纯 diff: pending 的 dsa 来源单
不在保留集即撤 (先撤后建)。

保护性保留: 持仓票最新报告缺 stop_loss、或报告库已无该票报告时, 既有 pending
止损单保留不撤 (对齐 §4.3-1「防 LLM 闪失清掉用户止损线」的槽位级保留语义);
空仓票的残留卖出单一律撤 (持仓已清, 止损/卖出单永无法成交)。

触发链: 不改其他 dsa_* 模块 (dsa_analysis 任务收尾钩子固定调 dsa_watch), 自起
daemon 线程 60s 轮询报告库对账; 对账全程持 paper.PAPER_LOCK, 全吞异常, 桥故障
不影响分析/盯盘/模拟盘主流程。

已知边界 (如实标注): 止损 GTC 单触发遇跌停拒单后, 默认账户 (queue 关) 过期不
自动重挂——账户开启 queue_limit_orders 可保持触发条件次日重试 (核心 _queue_or_expire
对 conditional 不转 next_open); 买单金额按权益均分但资金校验按账户现金, 现金
不足的槽位跳过留痕; 应用整体跨日宕机时 day 单可能晚一日成交 (作废的权威路径
是盘后结算, 宕机日无结算)。
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from app.custom import dsa_analysis, dsa_watch
from app.extensions import (
    BACKEND_EXTENSION_API_VERSION,
    BackendExtensionRegistrar,
    ExtensionContext,
)
from app.market_time import cn_today
from app.strategy import paper

logger = logging.getLogger(__name__)

EXTENSION_ID = "dsa.paper-bridge"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION

_POLL_SECONDS = 60.0
_SLOT_COUNT = 10                    # 买单槽位数 (DSA §4.6-3: 10 槽等额)
_ACCOUNT_ID = paper.DEFAULT_ACCOUNT_ID
_SOURCE_PREFIX = "dsa:"
_SELL_ADVICE_KEYWORDS = ("卖出", "减仓", "清仓")   # DSA §4.6-1 卖出类建议映射
_ENTRY_KIND = "entry"
_STOP_KIND = "stop"
_EXIT_KIND = "exit"

_RUNTIME: dict[str, Path | None] = {"data_dir": None}
_POLL_THREAD: threading.Thread | None = None
_POLL_THREAD_LOCK = threading.Lock()


# ================================================================
# 纯函数: 建议映射 / source 解析 / 期望订单规划
# ================================================================

def _is_sell_advice(advice) -> bool:
    """操作建议文本含卖出类关键词 → 卖出意图 (DSA §4.6-1 同款包含式映射)。"""
    text = str(advice or "")
    return any(keyword in text for keyword in _SELL_ADVICE_KEYWORDS)


def _lot_floor(qty: float) -> int:
    """向下取整到百股 (核心 create_order 只收整百; 不足一手为 0)。"""
    return int(qty // paper.LOT_SIZE) * paper.LOT_SIZE


def _parse_source(source: str) -> tuple[str, str] | None:
    """dsa:{rid}[:kind] → (rid, kind); kind 缺省 entry; 非 dsa 来源 → None。"""
    text = str(source or "")
    if not text.startswith(_SOURCE_PREFIX):
        return None
    rid, _, kind = text[len(_SOURCE_PREFIX):].partition(":")
    if not rid:
        return None
    return rid, (kind or _ENTRY_KIND)


def plan_orders(
    held: dict[str, tuple[str, int]],
    latest: dict[str, dict],
    orders: list[dict],
    slot_amount: float,
) -> tuple[list[dict], set[str]]:
    """期望订单规划 (纯函数): 返回 (待建参数列表, 应保留的 pending source 集)。

    held: {sym6: (模拟盘 symbol 后缀式, 可卖整百数量)}; latest: {sym6: 最新报告};
    orders: 账户全部订单 (任何状态, 供同 source 幂等去重)。每票至多一条期望单;
    期望单参数只由报告与持仓推导, 同 source 已存在 (含 filled/expired/cancelled)
    即不再建, 由调用方对 pending diff 撤旧。
    """
    existing = {
        str(o.get("source")) for o in orders
        if str(o.get("source", "")).startswith(_SOURCE_PREFIX)
    }
    pending_by_sym6: dict[str, list[dict]] = {}
    for o in orders:
        if o.get("status") != "pending" or not str(o.get("source", "")).startswith(_SOURCE_PREFIX):
            continue
        sym6 = dsa_watch._norm6(o.get("symbol"))
        if sym6 is not None:
            pending_by_sym6.setdefault(sym6, []).append(o)

    creates: list[dict] = []
    keeps: set[str] = set()

    for sym6, report in latest.items():
        report_id = str(report.get("id") or "")
        symbol = str(report.get("symbol") or "")
        if not report_id or not symbol:
            continue
        points = report.get("points") if isinstance(report.get("points"), dict) else {}
        group = pending_by_sym6.get(sym6, [])

        pos = held.get(sym6)
        if pos is None:
            # 空仓票: 最新报告 ideal_buy → 次日 conditional 买单 (§4.7-1);
            # 卖出类建议时不建 (报告自相矛盾, 不做多); 残留卖出单不保留 (撤)
            if _is_sell_advice(report.get("operation_advice")):
                continue
            ideal_buy = points.get("ideal_buy")
            if ideal_buy is None:
                continue
            source = f"{_SOURCE_PREFIX}{report_id}"
            keeps.add(source)
            if source not in existing:
                creates.append({
                    "symbol": symbol, "side": "buy", "order_type": "conditional",
                    "amount": float(slot_amount), "ref_price": float(ideal_buy),
                    "trigger_price": float(ideal_buy), "trigger_op": "<=", "expire": "day",
                    "source": source,
                })
            continue

        _paper_symbol, available = pos
        if _is_sell_advice(report.get("operation_advice")):
            # 卖出类建议 → 次日开盘全量卖出; 意图优先, 该票止损单全部撤
            source = f"{_SOURCE_PREFIX}{report_id}:{_EXIT_KIND}"
            keeps.add(source)
            if source not in existing and available > 0:
                creates.append({
                    "symbol": _paper_symbol, "side": "sell", "order_type": "next_open",
                    "qty": available, "source": source,
                })
            continue

        stop = points.get("stop_loss")
        if stop is not None:
            # 止损 GTC 单: 报告更新 (新 id) → 撤旧建新; 数量为当前可卖整百
            source = f"{_SOURCE_PREFIX}{report_id}:{_STOP_KIND}"
            keeps.add(source)
            if source not in existing and available > 0:
                creates.append({
                    "symbol": _paper_symbol, "side": "sell", "order_type": "conditional",
                    "qty": available, "ref_price": float(stop),
                    "trigger_price": float(stop), "trigger_op": "<=", "expire": "gtc",
                    "source": source,
                })
        else:
            # 最新报告缺 stop_loss: 既有止损单槽位级保留 (防 LLM 闪失清掉止损线)
            for o in group:
                parsed = _parse_source(str(o.get("source")))
                if parsed is not None and parsed[1] == _STOP_KIND:
                    keeps.add(str(o.get("source")))

    # 持仓票报告库已无报告: 止损单保护性保留, 卖出建议单撤 (建议已无依据)
    for sym6, group in pending_by_sym6.items():
        if sym6 in latest or sym6 not in held:
            continue
        for o in group:
            parsed = _parse_source(str(o.get("source")))
            if parsed is not None and parsed[1] == _STOP_KIND:
                keeps.add(str(o.get("source")))
    return creates, keeps


# ================================================================
# 对账主入口
# ================================================================

def _runtime_data_dir() -> Path:
    data_dir = _RUNTIME.get("data_dir")
    if data_dir is not None:
        return Path(data_dir)
    from app.config import settings

    return Path(settings.data_dir)


def _latest_reports() -> dict[str, dict]:
    """报告库按 sym6 取最新 (list_reports 按 created_at 倒序; 测试可换 _STORE)。"""
    latest: dict[str, dict] = {}
    for report in dsa_analysis._STORE.list_reports(None):
        sym6 = dsa_watch._norm6(report.get("symbol"))
        if sym6 and sym6 not in latest:
            latest[sym6] = report
    return latest


def _resolve_asset_type(repo, symbol: str) -> str | None:
    """repo 维表定资产类型; 失败回退 None (create_order 按代码段启发式兜底)。"""
    try:
        return repo.resolve_asset_type(symbol) if repo is not None else None
    except Exception as e:
        logger.warning("dsa paper bridge: resolve_asset_type(%s) 失败: %s", symbol, e)
        return None


def run_bridge(data_dir: Path | None = None, repo=None) -> dict:
    """对账: 报告库 → 期望集 → 撤不在保留集的 pending dsa 单 → 建缺失单。

    default 账户未初始化时静默待命 (不代建账户)。全程持 PAPER_LOCK (可重入,
    create_order/cancel_order 内部再加锁无害), 与盘中撮合/盘后结算互斥。
    """
    data_dir = Path(data_dir) if data_dir is not None else _runtime_data_dir()
    summary = {"created": 0, "cancelled": 0, "skipped": 0}
    acc = paper.get_account(data_dir, _ACCOUNT_ID)
    if acc is None:
        return summary
    with paper.PAPER_LOCK:
        positions = paper.load_positions(data_dir, _ACCOUNT_ID)
        today = cn_today().isoformat()
        held: dict[str, tuple[str, int]] = {}
        for symbol, pos in positions.items():
            if pos.get("qty", 0) <= 0:
                continue
            sym6 = dsa_watch._norm6(symbol)
            if sym6 is None:
                continue
            # 可卖按 T+1 现算 (物化文件跨日不更新), 整百向下取整
            held[sym6] = (symbol, _lot_floor(paper._available_of(pos, today)))
        orders = paper.load_orders(data_dir, _ACCOUNT_ID)
        # 权益 = 现金 + 持仓市值 (无实时价按成本, 18:00 后本无盘中价可依)
        equity = paper.overview(data_dir, account_id=_ACCOUNT_ID).get("total")
        if equity is None:
            equity = float(acc.get("cash") or 0)
        creates, keeps = plan_orders(held, _latest_reports(), orders, float(equity) / _SLOT_COUNT)

        for o in orders:
            source = str(o.get("source", ""))
            if o.get("status") != "pending" or not source.startswith(_SOURCE_PREFIX):
                continue
            if source in keeps:
                continue
            _, err = paper.cancel_order(data_dir, str(o["id"]), _ACCOUNT_ID)
            if err is None:
                summary["cancelled"] += 1
            else:
                logger.warning("dsa paper bridge: 撤单失败 %s: %s", o.get("id"), err)

        for params in creates:
            if params["side"] == "sell":
                asset_type = (positions.get(params["symbol"]) or {}).get("asset_type", "stock")
            else:
                asset_type = _resolve_asset_type(repo, params["symbol"])
            order, err = paper.create_order(
                data_dir, params["symbol"], params["side"], account_id=_ACCOUNT_ID,
                qty=params.get("qty"), amount=params.get("amount"),
                order_type=params["order_type"], asset_type=asset_type,
                ref_price=params.get("ref_price"),
                trigger_price=params.get("trigger_price"),
                trigger_op=params.get("trigger_op"),
                expire=params.get("expire", "day"),
                source=params["source"],
            )
            if order is None:
                summary["skipped"] += 1
                logger.warning(
                    "dsa paper bridge: 建单跳过 %s %s %s: %s",
                    params["source"], params["symbol"], params["side"], err,
                )
            else:
                summary["created"] += 1
                logger.info(
                    "dsa paper bridge: 建单 %s %s %s %s",
                    params["source"], params["symbol"], params["side"], params["order_type"],
                )
    if summary["created"] or summary["cancelled"]:
        logger.info("dsa paper bridge: 对账完成 %s", summary)
    return summary


# ================================================================
# 轮询线程与扩展挂接
# ================================================================

def _poll_loop() -> None:
    while True:
        time.sleep(_POLL_SECONDS)
        try:
            run_bridge(_runtime_data_dir())
        except Exception:
            logger.exception("dsa paper bridge: 轮询对账失败")


def setup(registrar: BackendExtensionRegistrar) -> None:
    """无路由无格式器: 本桥纯后台对账; 注册 EXTENSION_ID 让 startup 被调度。"""
    _ = registrar


def startup(context: ExtensionContext) -> None:
    """记录运行时并启动 daemon 轮询线程 (先睡一拍再跑, 不阻止进程退出)。"""
    global _POLL_THREAD
    _RUNTIME["data_dir"] = Path(context.data_dir)
    with _POLL_THREAD_LOCK:
        if _POLL_THREAD is not None and _POLL_THREAD.is_alive():
            return
        _POLL_THREAD = threading.Thread(target=_poll_loop, name="dsa-paper-bridge", daemon=True)
        _POLL_THREAD.start()
