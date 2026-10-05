"""B2 绝对点位回测测试 (迁移计划 §4.7-2)。

覆盖: MatcherConfig 三绝对价字段 (默认 None 行为不变)、共享 _risk_exit_decision
纯函数口径 (绝对线取代百分比线、开盘已破按开盘价、盘中触及按线价、移动止损
共存取最高线)、入场触及判定 (成交日 low ≤ entry_price, 未触及拒单计数
buy_entry_not_touched)、成交价 min(当日 open, entry_price)、matrix/portfolio/
legacy 三类撮合器口径一致、strategy.py overrides 三键归一。

价格口径: 手工 panel 为不复权日K; 费用滑点置零以锁定断言数字。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from app.backtest.engine import BacktestEngine, MatcherConfig, _risk_exit_decision
from app.backtest.strategy import StrategyBacktestService

BASE = date(2024, 1, 1)


def _bar(o: float, h: float, low: float, c: float) -> dict:
    return {"o": o, "h": h, "l": low, "c": c}


def _panel(bars: list[dict]) -> pl.DataFrame:
    rows = [
        {
            "symbol": "600519.SH",
            "name": "贵州茅台",
            "date": BASE + timedelta(days=i),
            "open": bar["o"],
            "high": bar["h"],
            "low": bar["l"],
            "close": bar["c"],
            "volume": 100_000,
            "score": 1.0,
            "signal_limit_up": False,
            "signal_limit_down": False,
        }
        for i, bar in enumerate(bars)
    ]
    return pl.DataFrame(rows).sort(["symbol", "date"])


def _entry_mask(panel: pl.DataFrame, day_index: int) -> pl.Series:
    return pl.Series([i == day_index for i in range(panel.height)], dtype=pl.Boolean)


def _run(panel: pl.DataFrame, day_index: int, config: MatcherConfig, matcher: str = "independent") :
    engine = BacktestEngine(repo=None)
    entries = _entry_mask(panel, day_index)
    if matcher == "independent":
        return engine.simulate_independent_candidates(panel, entries, None, config)
    if matcher == "portfolio":
        return engine.simulate_portfolio(panel, entries, None, config)
    return engine.simulate_independent_candidates_legacy(panel, entries, None, config)


def _config(**overrides) -> MatcherConfig:
    params = {"matching": "open_t+1", "fees_pct": 0, "slippage_bps": 0}
    params.update(overrides)
    return MatcherConfig(**params)


# ── 共享决策纯函数 ──────────────────────────────────────
def _decision(**kw) -> tuple[str | None, float | None]:
    config = MatcherConfig(**kw.pop("config", {}))
    return _risk_exit_decision(config, **kw)


def test_decision_absolute_stop_touched_at_line():
    # 盘中跌破绝对止损线 → 按线价成交
    reason, price = _decision(
        config={"stop_price": 9.5}, entry_price=10.0, peak_price=10.2,
        open_price=10.1, low_price=9.4, high_price=10.2,
    )
    assert reason == "stop_loss" and price == pytest.approx(9.5)


def test_decision_absolute_stop_gap_open_at_open_price():
    # 开盘已破线 → 按开盘价成交 (DSA 跳空口径)
    reason, price = _decision(
        config={"stop_price": 9.5}, entry_price=10.0, peak_price=10.2,
        open_price=9.3, low_price=9.2, high_price=9.4,
    )
    assert reason == "stop_loss" and price == pytest.approx(9.3)


def test_decision_absolute_take_profit():
    reason, price = _decision(
        config={"take_profit_price": 10.8}, entry_price=10.0, peak_price=10.0,
        open_price=10.05, low_price=10.0, high_price=11.0,
    )
    assert reason == "take_profit" and price == pytest.approx(10.8)
    reason, price = _decision(
        config={"take_profit_price": 10.8}, entry_price=10.0, peak_price=10.0,
        open_price=11.6, low_price=11.5, high_price=11.7,
    )
    assert reason == "take_profit" and price == pytest.approx(11.6)


def test_decision_absolute_replaces_percentage_line():
    # stop_price 与 stop_loss_pct 同设: 绝对线取代百分比线 (9.8 线不生效)
    reason, price = _decision(
        config={"stop_price": 9.5, "stop_loss_pct": 0.02}, entry_price=10.0, peak_price=10.0,
        open_price=9.9, low_price=9.6, high_price=9.95,
    )
    assert reason is None and price is None  # 9.6 > 9.5 未破绝对线
    reason, price = _decision(
        config={"stop_price": 9.5, "stop_loss_pct": 0.02}, entry_price=10.0, peak_price=10.0,
        open_price=9.9, low_price=9.4, high_price=9.95,
    )
    assert reason == "stop_loss" and price == pytest.approx(9.5)


def test_decision_trailing_coexists_and_max_line_wins():
    # 移动止损与绝对止损共存: 取最高有效线
    reason, price = _decision(
        config={"stop_price": 9.5, "trailing_stop_pct": 0.1}, entry_price=10.0, peak_price=11.0,
        open_price=10.0, low_price=9.85, high_price=10.1,
    )
    assert reason == "trailing_stop" and price == pytest.approx(9.9)


def test_decision_no_lines_no_trigger():
    reason, price = _decision(
        config={}, entry_price=10.0, peak_price=10.0,
        open_price=9.9, low_price=9.8, high_price=10.0,
    )
    assert reason is None and price is None


# ── matrix 独立候选撮合器 (主路径) ──────────────────────
def test_entry_touched_fills_at_absolute_price():
    panel = _panel([
        _bar(10.0, 10.2, 9.8, 10.0),   # 报告日 (信号日)
        _bar(10.2, 10.4, 9.9, 10.3),   # 成交日: low 9.9 ≤ 10.0 触及, open 10.2 > 10 → 按触发价
        _bar(10.1, 10.2, 9.9, 10.0),
        _bar(10.0, 10.1, 9.8, 9.9),
    ])
    result = _run(panel, 0, _config(entry_price=10.0))
    assert len(result.trades) == 1
    assert result.trades[0].entry_price == pytest.approx(10.0)
    assert result.trades[0].entry_date == (BASE + timedelta(days=1)).isoformat()


def test_entry_gap_down_fills_at_open():
    panel = _panel([
        _bar(10.0, 10.2, 9.8, 10.0),
        _bar(9.8, 10.0, 9.7, 9.9),     # 开盘 9.8 已低于介入价 → 按开盘价 (更优)
        _bar(9.9, 10.0, 9.8, 9.9),
        _bar(9.9, 10.0, 9.8, 9.9),
    ])
    result = _run(panel, 0, _config(entry_price=10.0))
    assert result.trades[0].entry_price == pytest.approx(9.8)


def test_entry_not_touched_rejected():
    panel = _panel([
        _bar(10.0, 10.2, 9.8, 10.0),
        _bar(10.6, 10.8, 10.5, 10.7),  # low 10.5 > 10.0 全天未触及
        _bar(10.6, 10.8, 10.5, 10.7),
        _bar(10.6, 10.8, 10.5, 10.7),
    ])
    result = _run(panel, 0, _config(entry_price=10.0))
    assert result.trades == []
    assert result.stats["execution"]["buy_entry_not_touched"] == 1


# ── 点位未触及 x close_t 涨停拦截 组合 (合并自上游 close_t 修复) ──
def test_entry_not_touched_wins_over_close_t_limit_up():
    """同时命中: 点位未触及 + close_t 收盘封板 → 归因先执行的 buy_entry_not_touched。"""
    rows = [
        _bar(10.0, 10.2, 9.8, 10.0),
        # 收盘封涨停(非一字, prev_close 10.0 → limit 11.0): low 10.0 > 介入价 9.5
        {"o": 10.05, "h": 11.0, "l": 10.0, "c": 11.0},
        _bar(11.0, 11.2, 10.9, 11.1),
        _bar(11.0, 11.2, 10.9, 11.1),
    ]
    panel = _panel(rows)
    # 仅封板日声明涨停 (close_t: 信号日即成交日)
    panel = panel.with_columns(
        pl.when(pl.col("date") == BASE + timedelta(days=1))
        .then(pl.lit(True)).otherwise(pl.col("signal_limit_up"))
        .alias("signal_limit_up")
    )
    result = _run(panel, 1, _config(matching="close_t", entry_price=9.5))
    assert result.trades == []
    exec_stats = result.stats["execution"]
    assert exec_stats["buy_entry_not_touched"] == 1
    assert exec_stats.get("buy_limit_up", 0) == 0


def test_entry_touched_close_t_normal_fill():
    """两者都通过: close_t + 介入价触及 + 非涨停 → 正常成交 @ min(open, entry_price)。"""
    panel = _panel([
        _bar(10.0, 10.2, 9.8, 10.0),
        _bar(10.2, 10.4, 9.9, 10.3),   # low 9.9 ≤ 11.0 触及; 无涨停
        _bar(10.1, 10.2, 9.9, 10.0),
    ])
    result = _run(panel, 0, _config(matching="close_t", entry_price=11.0))
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_date == BASE.isoformat()  # close_t: 信号日即成交日
    assert trade.entry_price == pytest.approx(10.0)  # min(open 10.0, 11.0)


def test_absolute_stop_exit_matrix_matcher():
    panel = _panel([
        _bar(10.0, 10.2, 9.8, 10.0),
        _bar(10.2, 10.4, 9.9, 10.3),   # 建仓 @10.0
        _bar(10.1, 10.2, 9.4, 9.6),    # 盘中破 9.5 → 止损 @9.5
        _bar(9.5, 9.6, 9.3, 9.4),
    ])
    result = _run(panel, 0, _config(entry_price=10.0, stop_price=9.5))
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == "stop_loss"
    assert trade.exit_price == pytest.approx(9.5)
    assert trade.exit_date == (BASE + timedelta(days=2)).isoformat()


def test_absolute_take_profit_exit_matrix_matcher():
    panel = _panel([
        _bar(10.0, 10.2, 9.8, 10.0),
        _bar(10.2, 10.4, 9.9, 10.3),   # 建仓 @10.0
        _bar(10.4, 11.0, 10.3, 10.9),  # 盘中触 10.8 → 止盈 @10.8
    ])
    result = _run(panel, 0, _config(entry_price=10.0, take_profit_price=10.8))
    trade = result.trades[0]
    assert trade.exit_reason == "take_profit" and trade.exit_price == pytest.approx(10.8)


# ── portfolio matrix 撮合器 (同口径) ────────────────────
def test_absolute_points_portfolio_matcher():
    panel = _panel([
        _bar(10.0, 10.2, 9.8, 10.0),
        _bar(10.2, 10.4, 9.9, 10.3),   # 建仓 @10.0 (触及)
        _bar(10.1, 10.2, 9.4, 9.6),    # 止损 @9.5
    ])
    result = _run(panel, 0, _config(entry_price=10.0, stop_price=9.5, max_positions=1), matcher="portfolio")
    assert len(result.trades) == 1
    assert result.trades[0].entry_price == pytest.approx(10.0)
    assert result.trades[0].exit_reason == "stop_loss" and result.trades[0].exit_price == pytest.approx(9.5)


def test_entry_not_touched_portfolio_matcher():
    panel = _panel([
        _bar(10.0, 10.2, 9.8, 10.0),
        _bar(10.6, 10.8, 10.5, 10.7),
        _bar(10.6, 10.8, 10.5, 10.7),
    ])
    result = _run(panel, 0, _config(entry_price=10.0, max_positions=1), matcher="portfolio")
    assert result.trades == []
    assert result.stats["execution"]["buy_entry_not_touched"] == 1


# ── legacy panel 撮合器 (同口径) ────────────────────────
def test_absolute_points_legacy_matcher():
    panel = _panel([
        _bar(10.0, 10.2, 9.8, 10.0),
        _bar(10.2, 10.4, 9.9, 10.3),   # 建仓 @10.0
        _bar(10.1, 10.2, 9.4, 9.6),    # 止损 @9.5
        _bar(9.5, 9.6, 9.3, 9.4),
    ])
    result = _run(panel, 0, _config(entry_price=10.0, stop_price=9.5), matcher="legacy")
    assert len(result.trades) == 1
    assert result.trades[0].entry_price == pytest.approx(10.0)
    assert result.trades[0].exit_reason == "stop_loss" and result.trades[0].exit_price == pytest.approx(9.5)


def test_entry_not_touched_legacy_matcher():
    panel = _panel([
        _bar(10.0, 10.2, 9.8, 10.0),
        _bar(10.6, 10.8, 10.5, 10.7),
        _bar(10.6, 10.8, 10.5, 10.7),
    ])
    result = _run(panel, 0, _config(entry_price=10.0), matcher="legacy")
    assert result.trades == []
    assert result.stats["execution"]["buy_entry_not_touched"] == 1


# ── strategy.py overrides 三键归一 ──────────────────────
def test_normalize_abs_price():
    norm = StrategyBacktestService._normalize_abs_price
    assert norm(10.0) == 10.0
    assert norm("9.8") == 9.8
    assert norm(None) is None and norm("") is None
    assert norm("abc") is None
    assert norm(0) is None and norm(-1.0) is None
    assert norm(float("nan")) is None
