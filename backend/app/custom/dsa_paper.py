"""DSA 点位模拟 + 报告回测 (迁移计划 P5 §4.6)。

前向模拟盘: 每轮从报告库整段重放 (无增量状态, 改口径自动纠正全历史,
同日重跑幂等), 结果整表覆写 data/user_data/dsa_paper/
(state/trades/equity 三个 JSON, 原子写)。

语义权威 = DSA 实现 (daily_stock_analysis/src/core/trailing_stop.py +
src/services/paper_trading_service.py + backtest_engine.evaluate_position_tracking),
本模块是按契约 §4.6 的移植实现, 不 import DSA 代码:

- 入场: 报告日次一交易 bar 挂 ideal_buy 限价 (开盘更低按开盘, 盘中触及按
  ideal_buy, 未触及作废; 开盘即破止损放弃; 一字板不成交); secondary_buy 永不成交
- 出场状态机 (T+1, 建仓当根不检查): 止损 (跳空按开盘价) → 启动移动止盈
  (take_profit 是启动价, 本根不判线, peak 下一根生效) → 已启动出场线
  line = max(启动价, peakx(1-trail%)), trail% = clamp(ATR20%, 2%, 8%),
  ATR 取建仓锚点前 21 根现算、读不到回落 5%; 启动后止损失效; 一字板顺延;
  卖出类信号次日开盘平仓; 持有满 10 根当日收盘 window_expired; 末尾仍持仓记 open
- 账户: 10 万初始资金、10 槽位等额、零碎股份额、零费用零滑点 (与 DSA 可比口径);
  槽位满 skipped/no_slot, 行情缺失 skipped/no_bars (不伪造成交)
- 净值: 每日 cash + 持仓收盘 MTM; 基准沪深300 归一到初始资金
- 报告回测 (outcomes): 每报告 x horizon(1/3/5/10) 一条; 方向 up/not_down/not_up,
  ±2% neutral 带; up 走出场状态机; 命中率分母 = hit+miss
  (neutral/unable/无方向全部排除); 样本 <30 维度不下结论 (前端按 n 灰显)
"""
from __future__ import annotations

import itertools
import json
import logging
import math
import os
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from app.extensions import (
    BACKEND_EXTENSION_API_VERSION,
    BackendExtensionRegistrar,
    ExtensionContext,
)
from app.market_time import cn_now
from app.services.trading_day import is_trading_day

logger = logging.getLogger(__name__)

EXTENSION_ID = "dsa.paper"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION

INITIAL_CAPITAL = 100_000.0
MAX_SLOTS = 10
MAX_HOLDING_BARS = 10
BENCHMARK_SYMBOL = "000300.SH"
BENCHMARK_ASSET_TYPE = "index"

TRAIL_ATR_MULTIPLIER = 1.0
TRAIL_MIN_PCT = 2.0
TRAIL_MAX_PCT = 8.0
TRAIL_FALLBACK_PCT = 5.0

NEUTRAL_BAND_PCT = 2.0
HORIZONS = (1, 3, 5, 10)

_DATA_DIR_NAME = "dsa_paper"
_RUN_TIME = (15, 5)          # 交易日 15:05 后自动重放
_SCHED_TICK_SECONDS = 60.0
_HISTORY_BUFFER_DAYS = 100   # 建仓锚点前 21 根 K 线的日历缓冲

_BUY_WORDS = ("买入", "加仓")
_SELL_WORDS = ("卖出", "减仓", "清仓")
_HOLD_WORDS = ("持有",)
_AVOID_WORDS = ("规避",)

_RUNTIME: dict[str, Any] = {"repo": None, "data_dir": None}
_last_run_date: str | None = None  # 本进程已自动跑的日期 (防重复)


# ================================================================
# 存储 (整表覆写, 原子写)
# ================================================================

def _data_dir() -> Path:
    return Path(_RUNTIME["data_dir"]) / _DATA_DIR_NAME


def _store_path(name: str) -> Path:
    return _data_dir() / f"{name}.json"


def _atomic_write_json(path: Path, payload: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


# ================================================================
# 行情与指标工具
# ================================================================

def _bar_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _fetch_bars(repo, asset_type: str, symbol: str, start: date, end: date) -> list[dict]:
    """repo 日K → 升序 bar dict 列表; 取数失败/为空返回 [] (宁缺不编)。"""
    try:
        df = repo.get_daily_asset(
            asset_type, symbol, start, end, columns=["date", "open", "high", "low", "close"],
        )
    except Exception as e:
        logger.info("dsa paper: 取日K失败 %s: %s", symbol, e)
        return []
    if df is None or df.is_empty():
        return []
    bars = []
    for row in df.to_dicts():
        d = _bar_date(row.get("date"))
        if d is None:
            continue
        bars.append({
            "date": d,
            "open": _finite(row.get("open")),
            "high": _finite(row.get("high")),
            "low": _finite(row.get("low")),
            "close": _finite(row.get("close")),
        })
    bars.sort(key=lambda b: b["date"])
    return bars


def _atr_20_pct(prior_bars: list[dict]) -> float | None:
    """ATR20% = mean(近 20 根 TR) / 最新收盘 x 100; 不足 21 根/非法返回 None。

    TR = max(high-low, |high-prev_close|, |low-prev_close|) —— 与 dsa_watch 同公式。
    """
    window = prior_bars[-21:]
    if len(window) < 21:
        return None
    trs = []
    for prev, cur in itertools.pairwise(window):
        h, lo, pc = cur["high"], cur["low"], prev["close"]
        if None in (h, lo, pc) or pc == 0:
            return None
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    last_close = window[-1]["close"]
    if not trs or not last_close:
        return None
    return (sum(trs) / len(trs)) / last_close * 100


def _trail_pct_for_atr(atr_20_pct: float | None) -> float:
    """clamp(1.0xATR20%, 2%, 8%); ATR 缺失/非正 → 回落 5%。"""
    if atr_20_pct is not None and atr_20_pct > 0:
        return min(max(atr_20_pct * TRAIL_ATR_MULTIPLIER, TRAIL_MIN_PCT), TRAIL_MAX_PCT)
    return TRAIL_FALLBACK_PCT


# ================================================================
# 出场状态机 (移植 trailing_step, touch 口径) + 评估窗口模拟
# ================================================================

def _trailing_step(
    *, armed: bool, peak: float | None, activation: float | None,
    stop_loss: float | None, trail_pct: float,
    high: float | None, low: float | None, bar_open: float | None,
) -> tuple[bool, float | None, float | None, str | None]:
    """单根日K状态转移: 返回 (armed, peak, exit_price, exit_reason)。

    优先级: 一字板顺延 (low==high 且触线) → 止损 (未启动, 跳空按开盘价) →
    本根启动 (不判线, 无未来函数, peak=max(启动价,当日高)) → 触线 trail_hit →
    幸存根抬 peak (只影响下一根)。启动后止损失效由 armed 条件保证。
    """
    limit_locked = low is not None and high is not None and low == high
    stop_hit = (not armed) and stop_loss is not None and low is not None and low <= stop_loss
    activation_touch = (
        (not armed) and activation is not None and high is not None and high >= activation
    )
    line = (
        max(activation, (peak if peak is not None else activation) * (1.0 - trail_pct / 100.0))
        if armed and activation is not None
        else None
    )
    line_hit = armed and line is not None and low is not None and low <= line

    if limit_locked and (stop_hit or line_hit):
        return armed, peak, None, None
    if stop_hit:
        exit_price = min(stop_loss, bar_open) if bar_open is not None else stop_loss
        return armed, peak, exit_price, "stop_loss"
    if activation_touch:
        return True, max(peak if peak is not None else activation, high), None, None
    if line_hit:
        exit_price = line if (bar_open is None or bar_open >= line) else bar_open
        return armed, peak, exit_price, "trail_hit"
    if armed and activation is not None and high is not None and (peak is None or high > peak):
        peak = high
    return armed, peak, None, None


def _simulate_window(
    *, start_price: float, activation: float | None, stop_loss: float | None,
    trail_pct: float, window_bars: list[dict],
) -> tuple[float | None, str | None]:
    """评估窗口逐根模拟 (outcomes 用, 语义 = DSA simulate_activation_trailing)。

    窗口是评估窗口: 一字板顺延撞到最后仍不出场 → window_end 按最后收盘。
    返回 (return_pct, exit_reason)。
    """
    armed = False
    peak = activation
    exit_price = None
    exit_reason = None
    for bar in window_bars:
        armed, peak, exit_price, exit_reason = _trailing_step(
            armed=armed, peak=peak, activation=activation, stop_loss=stop_loss,
            trail_pct=trail_pct, high=bar["high"], low=bar["low"], bar_open=bar["open"],
        )
        if exit_reason is not None:
            break
    if exit_reason is None:
        exit_reason = "window_end"
        exit_price = window_bars[-1]["close"] if window_bars else None
    if exit_price is None:
        return None, None
    return round((exit_price / start_price - 1.0) * 100, 2), exit_reason


def _validate_targets(
    start_price: float, stop_loss: float | None, take_profit: float | None,
) -> tuple[float | None, float | None]:
    """丢弃与锚定价方向矛盾的目标价 (多头: 止损须低于介入价, 启动价须高于介入价)。"""
    stop = stop_loss if (stop_loss is not None and 0 < stop_loss < start_price) else None
    target = take_profit if (take_profit is not None and take_profit > start_price) else None
    return stop, target


# ================================================================
# 信号分类 (契约 §4.6-1/§4.6-4: 卖出类优先, 其次买入类, 再持有)
# ================================================================

def _signal_kind(advice: Any) -> str:
    """operation_advice → buy/sell/hold/neutral (观望/无法识别 = neutral)。"""
    text = str(advice or "")
    if not text:
        return "neutral"
    if any(w in text for w in _SELL_WORDS) or any(w in text for w in _AVOID_WORDS):
        return "sell"
    if any(w in text for w in _BUY_WORDS):
        return "buy"
    if any(w in text for w in _HOLD_WORDS):
        return "hold"
    return "neutral"


def _direction_for_kind(kind: str) -> str | None:
    """信号 kind → outcome 方向 (契约 §4.6-4); neutral 无方向。"""
    return {"buy": "up", "hold": "not_down", "sell": "not_up"}.get(kind)


# ================================================================
# 每票 episode 重放 (持仓跟踪语义, 与 DSA evaluate_position_tracking 对齐)
# ================================================================

def _anchor_and_prior(
    bars: list[dict], report_day: date,
) -> tuple[dict | None, list[dict]]:
    """报告日锚定: 第一根 date >= 报告日的 bar 及其之前的全部 bar。"""
    for idx, bar in enumerate(bars):
        if bar["date"] >= report_day:
            return bar, bars[:idx]
    return None, []


def _replay_symbol(
    symbol: str, name: str | None, bars: list[dict], reports: list[dict],
) -> tuple[list[dict], dict[date, float], list[dict]]:
    """单票重放: 返回 (episodes, 收盘映射, skipped 行)。

    同日多报告取最新一条 (与 DSA 引擎 signals dict 同语义); 限价单次日不成交即作废。
    """
    [b["date"] for b in bars]
    latest_by_day: dict[date, dict] = {}
    skipped: list[dict] = []
    for report in reports:
        report_day = _bar_date(str(report.get("created_at") or "")[:10])
        anchor, _ = _anchor_and_prior(bars, report_day) if report_day else (None, [])
        if report_day is None or anchor is None:
            skipped.append(_skipped_row(symbol, name, report, "no_bars"))
            continue
        latest_by_day[anchor["date"]] = report

    episodes: list[dict] = []
    closes = {b["date"]: b["close"] for b in bars if b["close"] is not None}
    position: dict | None = None
    pending_entry: dict | None = None
    pending_exit = False

    def _close_episode(exit_date: date, exit_price: float, reason: str) -> None:
        entry_price = position["entry_price"]
        episodes.append({
            "report_id": position["report_id"],
            "signal_date": position["signal_date"].isoformat(),
            "entry_date": position["entry_date"].isoformat(),
            "entry_price": round(entry_price, 4),
            "exit_date": exit_date.isoformat(),
            "exit_price": round(exit_price, 4),
            "exit_reason": reason,
            "holding_bars": position["holding_bars"],
            "return_pct": round((exit_price / entry_price - 1.0) * 100, 2),
            "status": "closed",
        })

    for bar in bars:
        bar_open = bar["open"] if bar["open"] is not None else bar["close"]
        # 1) 执行上一日遗留: 限价建仓 (开盘更优按开盘, 未触及作废) / 卖出信号开盘平
        if position is None and pending_entry is not None:
            target = pending_entry["ideal_buy"]
            stop = pending_entry["stop_loss"]
            entry_price = None
            if target is not None and bar_open is not None:
                candidate = None
                if bar_open < target:
                    candidate = bar_open
                elif bar["low"] is not None and bar["low"] <= target:
                    candidate = target
                # 开盘即破止损: 放弃该买点 (防同一根K线按高于开盘的止损价出场记假盈利)
                if candidate is not None and not (stop is not None and candidate <= stop):
                    entry_price = candidate
                # 一字板买不进
                if entry_price is not None and bar["low"] is not None and bar["high"] is not None \
                        and bar["low"] == bar["high"]:
                    entry_price = None
            if entry_price:
                position = {
                    "report_id": pending_entry["report_id"],
                    "signal_date": pending_entry["signal_date"],
                    "entry_date": bar["date"],
                    "entry_price": float(entry_price),
                    "stop_loss": stop,
                    "take_profit": pending_entry["take_profit"],
                    "trail_pct": pending_entry["trail_pct"],
                    "armed": False,
                    "peak": None,
                    # 建仓 bar 计入持仓窗口, 当根 (T+1) 不触发止损/止盈
                    "holding_bars": 1,
                }
            pending_entry = None
        elif position is not None and pending_exit:
            if bar_open:
                _close_episode(bar["date"], float(bar_open), "sell_signal")
                position = None
            pending_exit = False

        # 2) 持仓中逐根检查 (T+1: 建仓当根不检查); 窗口兜底在状态机无出场时推进
        if position is not None and position["entry_date"] != bar["date"]:
            armed, peak, exit_price, exit_reason = _trailing_step(
                armed=position["armed"], peak=position["peak"],
                activation=position["take_profit"], stop_loss=position["stop_loss"],
                trail_pct=position["trail_pct"],
                high=bar["high"], low=bar["low"], bar_open=bar_open,
            )
            position["armed"], position["peak"] = armed, peak
            if exit_reason is None:
                position["holding_bars"] += 1
                if position["holding_bars"] >= MAX_HOLDING_BARS and bar["close"]:
                    exit_price, exit_reason = bar["close"], "window_expired"
            if exit_reason is not None:
                _close_episode(bar["date"], float(exit_price), exit_reason)
                position = None

        # 3) 当日报告信号 (次日生效); 持仓中买入类忽略, 卖出类挂次日开盘平
        report = latest_by_day.get(bar["date"])
        if report is not None:
            kind = _signal_kind(report.get("operation_advice"))
            points = report.get("points") if isinstance(report.get("points"), dict) else {}
            if position is None:
                ideal_buy = _finite(points.get("ideal_buy"))
                # 入场只用 ideal_buy (§4.6-1); 无 ideal_buy 的买入信号不动作
                if pending_entry is None and kind == "buy" and ideal_buy is not None:
                    _, prior = _anchor_and_prior(bars, bar["date"])
                    pending_entry = {
                        "report_id": report.get("id"),
                        "signal_date": bar["date"],
                        "ideal_buy": ideal_buy,
                        "stop_loss": _finite(points.get("stop_loss")),
                        "take_profit": _finite(points.get("take_profit")),
                        "trail_pct": _trail_pct_for_atr(_atr_20_pct(prior)),
                    }
            elif not pending_exit and kind == "sell":
                pending_exit = True

    # 数据末尾仍持仓 → open (浮动按最新收盘 MTM)
    if position is not None and bars:
        last_close = bars[-1]["close"]
        if last_close:
            entry_price = position["entry_price"]
            episodes.append({
                "report_id": position["report_id"],
                "signal_date": position["signal_date"].isoformat(),
                "entry_date": position["entry_date"].isoformat(),
                "entry_price": round(entry_price, 4),
                "exit_date": None,
                "exit_price": None,
                "exit_reason": None,
                "holding_bars": position["holding_bars"],
                "return_pct": round((float(last_close) / entry_price - 1.0) * 100, 2),
                "status": "open",
            })
    return episodes, closes, skipped


def _skipped_row(symbol: str, name: str | None, report: dict, reason: str) -> dict:
    report_day = _bar_date(str(report.get("created_at") or "")[:10])
    return {
        "report_id": report.get("id"),
        "symbol": symbol,
        "name": name or symbol,
        "signal_date": report_day.isoformat() if report_day else None,
        "status": "skipped",
        "skip_reason": reason,
    }


# ================================================================
# 槽位分配 + 净值曲线 (口径 = DSA paper_trading_service)
# ================================================================

def _allocate_slots(episodes: list[dict], capital: float, max_slots: int) -> list[dict]:
    """按 entry_date 先到先得占格 (exit>=entry 当日仍占格); 零碎股份额, 满槽 no_slot。"""
    slot_capital = capital / max_slots
    accepted: list[dict] = []
    rows: list[dict] = []
    for ep in sorted(episodes, key=lambda e: (e["entry_date"], e["symbol"])):
        entry_date = date.fromisoformat(ep["entry_date"])
        exit_date = date.fromisoformat(ep["exit_date"]) if ep.get("exit_date") else None
        occupied = sum(1 for a in accepted if a["_exit"] is None or a["_exit"] >= entry_date)
        if occupied >= max_slots:
            rows.append({
                "report_id": ep.get("report_id"), "symbol": ep["symbol"], "name": ep["name"],
                "signal_date": ep["signal_date"], "status": "skipped", "skip_reason": "no_slot",
            })
            continue
        accepted.append({**ep, "_exit": exit_date})
        entry_price = ep.get("entry_price")
        rows.append({
            "report_id": ep.get("report_id"),
            "symbol": ep["symbol"],
            "name": ep["name"],
            "signal_date": ep["signal_date"],
            "entry_date": ep["entry_date"],
            "entry_price": entry_price,
            "shares": round(slot_capital / entry_price, 6) if entry_price else None,
            "exit_date": ep.get("exit_date"),
            "exit_price": ep.get("exit_price"),
            "exit_reason": ep.get("exit_reason"),
            "holding_bars": ep.get("holding_bars"),
            "return_pct": ep.get("return_pct"),
            "status": ep.get("status"),
        })
    return rows


def _build_equity(
    trades: list[dict], closes_by_symbol: dict[str, dict[date, float]],
    benchmark_closes: dict[date, float], inception: date, today: date, capital: float,
) -> list[dict]:
    """每日 cash + 持仓收盘 MTM; 基准沪深300 归一到初始资金 (anchor = 首日)。"""
    filled = [
        t for t in trades
        if t["status"] in ("open", "closed") and t.get("entry_date")
    ]
    calendar = sorted(benchmark_closes)
    if not calendar:
        calendar = sorted(
            {inception, today}
            | {date.fromisoformat(t["entry_date"]) for t in filled}
            | {date.fromisoformat(t["exit_date"]) for t in filled if t.get("exit_date")}
        )
    slot_capital = capital / MAX_SLOTS
    rows = []
    for d in calendar:
        cash = capital
        market_value = 0.0
        for t in filled:
            entry_date = date.fromisoformat(t["entry_date"])
            if entry_date > d:
                continue
            exit_date = date.fromisoformat(t["exit_date"]) if t.get("exit_date") else None
            entry_price = t.get("entry_price")
            if exit_date is not None and exit_date <= d:
                cash += slot_capital * (t["exit_price"] / entry_price if entry_price else 1.0)
                continue
            closes = closes_by_symbol.get(t["symbol"], {})
            close_px = closes.get(d)
            if close_px is None:  # 停牌/缺行: 按最近已知收盘 mark (无则按成本价)
                prior = [c_day for c_day in closes if c_day <= d]
                close_px = closes[max(prior)] if prior else entry_price
            market_value += slot_capital * (close_px / entry_price if entry_price else 1.0)
        cash -= sum(slot_capital for t in filled if date.fromisoformat(t["entry_date"]) <= d)
        bench_close = benchmark_closes.get(d)
        anchor_close = benchmark_closes[calendar[0]] if benchmark_closes else None
        benchmark = (
            capital * bench_close / anchor_close
            if bench_close is not None and anchor_close else None
        )
        rows.append({
            "date": d.isoformat(),
            "equity": round(cash + market_value, 2),
            "benchmark": round(benchmark, 2) if benchmark is not None else None,
        })
    return rows


def _summary(trades: list[dict], equity: list[dict]) -> dict:
    closed = [t for t in trades if t["status"] == "closed"]
    wins = [t for t in closed if (t.get("return_pct") or 0) > 0]
    summary: dict[str, Any] = {
        "closed": len(closed),
        "open": sum(1 for t in trades if t["status"] == "open"),
        "skipped": sum(1 for t in trades if t["status"] == "skipped"),
    }
    equities = [row["equity"] for row in equity if row.get("equity")]
    if len(equities) >= 2 and equities[0] > 0:
        summary["total_return_pct"] = round((equities[-1] / equities[0] - 1) * 100, 2)
        peak, max_dd = equities[0], 0.0
        for v in equities:
            peak = max(peak, v)
            max_dd = min(max_dd, v / peak - 1)
        summary["max_drawdown_pct"] = round(max_dd * 100, 2)
    if closed:
        summary["win_rate"] = round(len(wins) / len(closed) * 100, 1)
    return summary


# ================================================================
# 整段重放
# ================================================================

def _load_reports() -> list[dict]:
    """P1 报告库全量 (升序); 存储不可用返回 []。"""
    try:
        from app.custom import dsa_analysis  # 函数级导入防循环

        reports = dsa_analysis._STORE.list_reports(None)
    except Exception as e:
        logger.warning("dsa paper: 读取报告库失败: %s", e)
        return []
    return sorted(reports, key=lambda r: str(r.get("created_at") or ""))


def run_paper() -> dict:
    """整段重放并整表覆写。返回 {ok, episodes, ran_at, ...}。"""
    repo = _RUNTIME.get("repo")
    if repo is None:
        return {"ok": False, "reason": "repository_unavailable"}
    now = cn_now().replace(tzinfo=None)
    today = now.date()
    reports = _load_reports()

    d = _data_dir()
    d.mkdir(parents=True, exist_ok=True)
    state_path = _store_path("state")
    trades_path = _store_path("trades")
    prior_state = _read_json(state_path, {})
    prior_trades = _read_json(trades_path, {}).get("items") or []
    # 防覆写守卫: 报告查空但库里有存量交易 → 大概率读取异常, 中止不清历史
    if not reports and prior_trades:
        logger.warning("dsa paper: 报告为空但存在存量交易, 中止重放 (防误清历史)")
        return {"ok": False, "aborted": True, "reason": "empty_reports_with_existing_trades"}

    state = prior_state if isinstance(prior_state, dict) else {}
    inception = _bar_date(state.get("inception_date")) or today
    inception = inception if inception <= today else today

    episodes: list[dict] = []
    skipped_rows: list[dict] = []
    closes_by_symbol: dict[str, dict[date, float]] = {}
    by_symbol: dict[str, list[dict]] = {}
    for report in reports:
        report_day = _bar_date(str(report.get("created_at") or "")[:10])
        if report_day is None or report_day < inception:
            continue
        by_symbol.setdefault(str(report.get("symbol") or ""), []).append(report)

    names: dict[str, str] = {}
    try:
        names = repo.get_name_map(list(by_symbol)) or {}
    except Exception as e:
        logger.info("dsa paper: 读取名称映射失败: %s", e)

    start = inception - timedelta(days=_HISTORY_BUFFER_DAYS)
    for symbol, symbol_reports in sorted(by_symbol.items()):
        try:
            asset_type = repo.resolve_asset_type(symbol)
        except Exception:
            asset_type = "stock"
        bars = _fetch_bars(repo, asset_type, symbol, start, today)
        if not bars:
            logger.info("dsa paper: %s 无日K, %d 条报告跳过 (不伪造成交)", symbol, len(symbol_reports))
            name = names.get(symbol)
            skipped_rows.extend(_skipped_row(symbol, name, r, "no_bars") for r in symbol_reports)
            continue
        name = names.get(symbol)
        eps, closes, skipped = _replay_symbol(symbol, name, bars, symbol_reports)
        for ep in eps:
            ep["symbol"] = symbol
            ep["name"] = name or symbol
        episodes.extend(eps)
        closes_by_symbol[symbol] = closes
        skipped_rows.extend(skipped)

    benchmark_closes = {
        b["date"]: b["close"]
        for b in _fetch_bars(repo, BENCHMARK_ASSET_TYPE, BENCHMARK_SYMBOL, inception, today)
        if b["close"] is not None
    }

    trades = _allocate_slots(episodes, INITIAL_CAPITAL, MAX_SLOTS) + skipped_rows
    equity = _build_equity(trades, closes_by_symbol, benchmark_closes, inception, today, INITIAL_CAPITAL)

    state_payload = {
        "inception_date": inception.isoformat(),
        "initial_capital": INITIAL_CAPITAL,
        "max_slots": MAX_SLOTS,
        "last_run_at": now.isoformat(timespec="seconds"),
    }
    _atomic_write_json(state_path, state_payload)
    _atomic_write_json(trades_path, {"items": trades})
    _atomic_write_json(_store_path("equity"), {"items": equity})

    closed = sum(1 for t in trades if t["status"] == "closed")
    skipped = sum(1 for t in trades if t["status"] == "skipped")
    stats = {
        "reports": len(reports),
        "episodes": len(episodes),
        "closed": closed,
        "open": len(episodes) - closed,
        "skipped": skipped,
        "curve_days": len(equity),
    }
    logger.info("dsa paper: replayed %s", stats)
    return {"ok": True, "episodes": len(episodes), "ran_at": state_payload["last_run_at"], **stats}


# ================================================================
# 报告回测 (outcomes, 契约 §4.6-4)
# ================================================================

def _classify_outcome(direction: str, return_pct: float) -> str:
    """±2% neutral 带 (up 三元; not_down/not_up 二元, 死区内算命中 —— DSA 同口径)。"""
    band = abs(NEUTRAL_BAND_PCT)
    if direction == "up":
        if return_pct >= band:
            return "hit"
        if return_pct <= -band:
            return "miss"
        return "neutral"
    if direction == "not_down":
        return "hit" if return_pct >= -band else "miss"
    if direction == "not_up":
        return "hit" if return_pct <= band else "miss"
    return "unable"


def compute_outcomes() -> dict:
    """每份报告 x horizon(1/3/5/10) 一条 outcome; 命中率分母 = hit+miss。"""
    repo = _RUNTIME.get("repo")
    if repo is None:
        return {"items": [], "stats": {"hit_rate": None, "n": 0, "by_horizon": {}}}
    today = cn_now().date()
    reports = _load_reports()
    items: list[dict] = []
    by_symbol: dict[str, list[dict]] = {}
    for report in reports:
        by_symbol.setdefault(str(report.get("symbol") or ""), []).append(report)

    try:
        repo.get_name_map(list(by_symbol)) or {}
    except Exception as e:
        logger.info("dsa paper: outcomes 读取名称映射失败: %s", e)

    for symbol, symbol_reports in sorted(by_symbol.items()):
        try:
            asset_type = repo.resolve_asset_type(symbol)
        except Exception:
            asset_type = "stock"
        report_days = [
            _bar_date(str(r.get("created_at") or "")[:10]) for r in symbol_reports
        ]
        earliest = min((d for d in report_days if d), default=today)
        bars = _fetch_bars(repo, asset_type, symbol, earliest - timedelta(days=_HISTORY_BUFFER_DAYS), today)
        if not bars:
            for report in symbol_reports:
                for horizon in HORIZONS:
                    items.append(_outcome_item(report, None, horizon, "unable", None, None))
            continue
        for report in symbol_reports:
            report_day = _bar_date(str(report.get("created_at") or "")[:10])
            anchor, prior = _anchor_and_prior(bars, report_day) if report_day else (None, [])
            kind = _signal_kind(report.get("operation_advice"))
            direction = _direction_for_kind(kind)
            points = report.get("points") if isinstance(report.get("points"), dict) else {}
            evaluable = (
                anchor is not None and anchor["close"] is not None
                and direction is not None and bool(points)
            )
            if not evaluable:
                # 观望/无方向/无点位/无锚定 → 每 horizon 一条 unable (不进命中率分母)
                for horizon in HORIZONS:
                    items.append(_outcome_item(report, direction, horizon, "unable", None, None))
                continue
            start_price = float(anchor["close"])
            stop, target = _validate_targets(
                start_price, _finite(points.get("stop_loss")), _finite(points.get("take_profit")),
            )
            trail_pct = _trail_pct_for_atr(_atr_20_pct(prior))
            forward = [b for b in bars if b["date"] > anchor["date"]]
            for horizon in HORIZONS:
                if len(forward) < horizon:
                    items.append(_outcome_item(report, direction, horizon, "unable", None, None))
                    continue
                window = forward[:horizon]
                if direction == "up":
                    return_pct, exit_reason = _simulate_window(
                        start_price=start_price, activation=target, stop_loss=stop,
                        trail_pct=trail_pct, window_bars=window,
                    )
                else:
                    end_close = window[-1]["close"]
                    if end_close is None:
                        items.append(_outcome_item(report, direction, horizon, "unable", None, None))
                        continue
                    return_pct = round((float(end_close) / start_price - 1.0) * 100, 2)
                    exit_reason = None  # not_down/not_up 是纯窗口收益
                if return_pct is None:
                    items.append(_outcome_item(report, direction, horizon, "unable", None, None))
                    continue
                outcome = _classify_outcome(direction, return_pct)
                items.append(_outcome_item(report, direction, horizon, outcome, exit_reason, return_pct))
    return {"items": items, "stats": _outcome_stats(items)}


def _outcome_item(
    report: dict, direction: str | None, horizon: int | None,
    outcome: str | None, exit_reason: str | None, return_pct: float | None,
) -> dict:
    return {
        "report_id": report.get("id"),
        "symbol": str(report.get("symbol") or ""),
        "name": report.get("name"),
        "created_at": report.get("created_at"),
        "direction": direction,
        "horizon": horizon,
        "outcome": outcome,
        "exit_reason": exit_reason,
        "return_pct": return_pct,
    }


def _outcome_stats(items: list[dict]) -> dict:
    def _aggregate(rows: list[dict]) -> dict:
        hits = sum(1 for r in rows if r["outcome"] == "hit")
        misses = sum(1 for r in rows if r["outcome"] == "miss")
        n = hits + misses  # 分母硬规则: neutral/unable/无方向全部排除
        return {
            "hit_rate": round(hits / n * 100, 1) if n else None,
            "n": n,
            "hits": hits,
            "misses": misses,
        }

    stats = _aggregate(items)
    stats["by_horizon"] = {
        str(h): _aggregate([r for r in items if r["horizon"] == h]) for h in HORIZONS
    }
    return stats


# ================================================================
# 调度: 交易日 15:05 后自动整段重放一次/日
# ================================================================

def _scheduler_loop() -> None:
    global _last_run_date
    while True:
        try:
            now = cn_now()
            day = now.date().isoformat()
            if (
                is_trading_day(now)
                and (now.hour, now.minute) >= _RUN_TIME
                and _last_run_date != day
            ):
                result = run_paper()
                if result.get("ok"):
                    _last_run_date = day
        except Exception as e:
            logger.warning("dsa paper 调度异常: %s", e)
        time.sleep(_SCHED_TICK_SECONDS)


# ================================================================
# HTTP 路由 (契约 §4.6-5)
# ================================================================

def setup(registrar: BackendExtensionRegistrar) -> None:
    router = APIRouter(prefix="/api/ext/dsa", tags=["dsa-paper"])

    @router.post("/paper/run")
    def run_paper_now() -> dict:
        return run_paper()

    @router.get("/paper/overview")
    def paper_overview() -> dict:
        state = _read_json(_store_path("state"), {})
        equity = (_read_json(_store_path("equity"), {}) or {}).get("items") or []
        trades = (_read_json(_store_path("trades"), {}) or {}).get("items") or []
        return {
            "state": state if isinstance(state, dict) and state else None,
            "summary": _summary(trades, equity),
            "equity": equity,
        }

    @router.get("/paper/trades")
    def paper_trades() -> dict:
        items = (_read_json(_store_path("trades"), {}) or {}).get("items") or []
        # 最近成交在前; skipped (无 entry_date) 沉底
        items.sort(key=lambda t: (t.get("entry_date") or "0000-00-00",), reverse=True)
        return {"items": items}

    @router.get("/paper/outcomes")
    def paper_outcomes() -> dict:
        return compute_outcomes()

    registrar.include_router(router)


def startup(context: ExtensionContext) -> None:
    _RUNTIME["repo"] = context.repository
    _RUNTIME["data_dir"] = Path(context.data_dir)
    threading.Thread(target=_scheduler_loop, name="dsa-paper-scheduler", daemon=True).start()
