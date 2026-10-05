"""DSA 报告点位 → 核心回测引擎 episode 端点 (迁移计划 §4.7-2 B2)。

POST /api/ext/dsa/backtest-report {report_id}: 取报告的 symbol/四点位/报告日,
构造报告日入场信号 (open_t+1: 次一交易日成交), 交给核心引擎按绝对点位跑单票
episode — 触及判定 (成交日 low ≤ ideal_buy, 未触及不建仓) + 成交价 min(当日
open, ideal_buy) (开盘更低按开盘, DSA 口径) + 绝对 stop_loss/take_profit 线 +
持有窗 max_hold_days=10 (DSA §4.6-2 口径)。撮合全部复用核心 engine
(MatcherConfig B2 绝对价支持), 本模块只做 报告→信号→结果 的桥接。

口径注记: 费用滑点用引擎默认 (DSA dsa_paper 是零费用口径, 刻意不同——B2 验收
「同号互证」只看方向一致); 涨跌停一字板依赖面板的 limit 信号列, 本端点不构造
该列 (原始 enriched 无此列), 故一字板拒单不在本 episode 口径内。卖出类建议
不参与 (核心引擎无 sell_signal 出场, 如实缺席)。

结果语义: 建仓/出场等数据性结果 200 + note 说明 (未触及建仓/数据不足不报错);
报告不存在 404、报告缺 ideal_buy 点位 400 (调用方错误)。
"""
from __future__ import annotations

import logging
import math
from datetime import date, timedelta
from typing import Annotated, Any

import polars as pl
from fastapi import APIRouter, Body, HTTPException, Request

from app.backtest.engine import BacktestEngine, MatcherConfig, TradeRecord
from app.custom import dsa_analysis
from app.enriched_generation import EnrichedGenerationUnavailableError
from app.extensions import (
    BACKEND_EXTENSION_API_VERSION,
    BackendExtensionRegistrar,
)

logger = logging.getLogger(__name__)

EXTENSION_ID = "dsa.backtest"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION

_HOLDING_WINDOW_BARS = 10   # 持有窗 (根), 对齐 DSA §4.6-2「持有满 10 根日K 到期」
_LOOKBACK_DAYS = 15         # 面板起点 = 报告日 - 15 自然日 (保住信号日当根)
_FORWARD_DAYS = 45          # 面板终点 = 报告日 + 45 自然日 (10 根持有窗 + 假期余量)

_EXIT_REASON_LABELS = {
    "stop_loss": "触发绝对止损线",
    "take_profit": "触发绝对止盈线",
    "trailing_stop": "触发移动止损",
    "trailing_take_profit": "触发回撤止盈",
    "signal": "卖出信号平仓",
    "max_hold": f"持有满 {_HOLDING_WINDOW_BARS} 根到期",
    "end": "数据末尾平仓",
}


def _positive_price(value: Any) -> float | None:
    """点位 → 有限正数; 缺失/非法返回 None (= 不启用该线)。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if f > 0 and math.isfinite(f) else None


def _signal_day(report: dict) -> date | None:
    """报告 created_at 前 10 位 → 信号日; 缺失/非法返回 None。"""
    text = str(report.get("created_at") or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _resolve_asset_type(repo, symbol: str) -> str:
    try:
        return repo.resolve_asset_type(symbol) if repo is not None else "stock"
    except Exception as e:
        logger.warning("dsa backtest: resolve_asset_type(%s) 失败, 回退 stock: %s", symbol, e)
        return "stock"


def _no_trade(symbol: str, note: str) -> dict:
    return {
        "symbol": symbol, "entry": None, "exit": None,
        "return_pct": None, "holding_bars": 0, "note": note,
    }


def _no_trade_note(stats: dict, ideal_buy: float) -> str:
    """撮合执行统计 → 未建仓原因 (契约: 未触及/数据不足走 note 不报错)。"""
    if stats.get("buy_entry_not_touched"):
        return f"成交日未触及介入价 {ideal_buy:g}, 未建仓 (DSA 口径: 不追)"
    if stats.get("buy_limit_up"):
        return "成交日一字涨停, 买不进, 未建仓"
    if stats.get("buy_suspended"):
        return "成交日停牌, 未建仓"
    if stats.get("sell_no_future") or stats.get("buy_no_next_bar"):
        return "信号日后无交易日数据, 无法建仓"
    return "信号未成交"


def _trade_note(trade: TradeRecord) -> str:
    label = _EXIT_REASON_LABELS.get(trade.exit_reason, trade.exit_reason)
    return f"按介入价建仓, {label} (持有 {trade.duration} 根)"


def _engine_for(request: Request) -> BacktestEngine:
    """核心回测引擎单例 (镜像 api/backtest._get_engine, PanelCache 跨请求复用)。"""
    from app.api.backtest import _get_engine

    return _get_engine(request)


def run_report_backtest(request: Request, report: dict) -> dict:
    """报告 → 核心 engine 单票 episode。引擎/面板异常向上抛 (真实故障可见),
    数据性结果 (无行情/未触及) 走 note 200。"""
    symbol = str(report.get("symbol") or "").strip()
    points = report.get("points") if isinstance(report.get("points"), dict) else {}
    ideal_buy = _positive_price(points.get("ideal_buy"))
    if ideal_buy is None:
        raise HTTPException(status_code=400, detail="报告缺少 ideal_buy 点位, 无法回测")
    stop_price = _positive_price(points.get("stop_loss"))
    take_profit_price = _positive_price(points.get("take_profit"))

    signal_day = _signal_day(report)
    if not symbol or signal_day is None:
        return _no_trade(symbol, note="报告缺少标的代码或报告日期, 无法构造入场信号")

    engine = _engine_for(request)
    repo = getattr(request.app.state, "repo", None)
    asset_type = _resolve_asset_type(repo, symbol)
    try:
        panel = engine.load_panel(
            [symbol],
            signal_day - timedelta(days=_LOOKBACK_DAYS),
            signal_day + timedelta(days=_FORWARD_DAYS),
            columns=["open", "high", "low", "close", "volume", "name"],
            asset_type=asset_type,
        )
    except EnrichedGenerationUnavailableError:
        return _no_trade(symbol, note=f"本地无 {symbol} 的 enriched 日K数据, 数据不足")
    if panel.is_empty() or not panel.select(pl.col("date") == signal_day).to_series().any():
        return _no_trade(symbol, note=f"本地无 {signal_day.isoformat()} 前后的日K数据, 数据不足")

    entries = panel.select((pl.col("date") == signal_day).alias("entry")).to_series()
    config = MatcherConfig(
        matching="open_t+1",          # 次一交易日成交: 触及判定/成交价都在成交日
        exit_fill="close_t",          # 无卖出信号; max_hold/end 按当日收盘 (DSA 窗口到期口径)
        entry_price=float(ideal_buy),
        stop_price=stop_price,
        take_profit_price=take_profit_price,
        max_hold_days=_HOLDING_WINDOW_BARS,
        asset_type=asset_type,
    )
    result = engine.simulate_independent_candidates(panel, entries, None, config)

    trades = list(result.trades or [])
    if not trades:
        stats = result.stats.get("execution") if isinstance(result.stats, dict) else None
        return _no_trade(symbol, _no_trade_note(stats if isinstance(stats, dict) else {}, float(ideal_buy)))
    trade = trades[0]
    return {
        "symbol": symbol,
        "entry": {"date": str(trade.entry_date), "price": round(float(trade.entry_price), 4)},
        "exit": {
            "date": str(trade.exit_date),
            "price": round(float(trade.exit_price), 4),
            "reason": trade.exit_reason,
        },
        "return_pct": round(float(trade.pnl_pct) * 100, 2),
        "holding_bars": int(trade.duration),
        "note": _trade_note(trade),
    }


def setup(registrar: BackendExtensionRegistrar) -> None:
    router = APIRouter(prefix="/api/ext/dsa", tags=["dsa-backtest"])

    @router.post("/backtest-report")
    def backtest_report(request: Request, payload: Annotated[dict, Body()]) -> dict:
        report_id = payload.get("report_id")
        if not isinstance(report_id, str) or not report_id.strip():
            raise HTTPException(status_code=400, detail="report_id 不能为空")
        report = dsa_analysis._STORE.get(report_id.strip())
        if report is None:
            raise HTTPException(status_code=404, detail=f"报告不存在: {report_id}")
        return run_report_backtest(request, report)

    registrar.include_router(router)
