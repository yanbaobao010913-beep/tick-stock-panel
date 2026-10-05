"""DSA 点位回测策略测试 (迁移计划 §4.7-2b)。

三层覆盖:
1. 物化器纯逻辑: 报告 → 信号行 (建议映射/次日入场格/逐格 ideal_buy/stop/take、
   同日多报告取更晚、窗口外跳过);
2. 内建策略文件: loader 装载 (source=builtin, 出现在策略列表);
3. 策略级 e2e (StrategyBacktestService.run): 真实走 matrix_native 特判分支 →
   仓位模拟/全量模拟两撮合器, 断言 入场价=ideal_buy 或更优开盘、逐仓位止损线
   各自生效、卖出建议次日开盘离场、未触及不建仓、持有窗到期。

费用口径: fees_pct=0 + slippage 0 锁定数字。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from app import config as app_config
from app.backtest.engine import BacktestEngine
from app.backtest.matrix import build_market_data_matrix
from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService
from app.custom import dsa_analysis, dsa_points
from app.strategy.engine import StrategyEngine

SYM_A = "600519.SH"
SYM_B = "000001.SZ"
BASE = date(2024, 1, 1)

BUILTIN_DIR = Path(__file__).resolve().parents[2] / "app" / "strategy" / "builtin"


def _panel(bars: dict[str, list[tuple[float, float, float, float]]]) -> pl.DataFrame:
    """{symbol: [(open, high, low, close)]} 从 BASE 起逐日对齐。"""
    rows = []
    length = max(len(bars_) for bars_ in bars.values())
    for sym, bars_ in bars.items():
        for i in range(length):
            o, h, low, c = bars_[i] if i < len(bars_) else bars_[-1]
            rows.append({
                "symbol": sym, "name": sym,
                "date": BASE + timedelta(days=i),
                "open": o, "high": h, "low": low, "close": c,
                "volume": 100_000,
            })
    return pl.DataFrame(rows).sort(["symbol", "date"])


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch) -> Path:
    """settings.data_dir 指向 tmp (报告库隔离)。"""
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    return tmp_path


def _save_report(
    report_id: str, symbol: str, advice: str, points: dict,
    created: str = "2024-01-01T18:00:00",
) -> dict:
    return dsa_analysis._STORE.save({
        "id": report_id, "symbol": symbol, "name": None,
        "created_at": created, "mode": "full", "operation_advice": advice,
        "points": points,
    })


def _points(ideal_buy=None, stop_loss=None, take_profit=None, secondary_buy=None) -> dict:
    return {
        "ideal_buy": ideal_buy, "secondary_buy": secondary_buy,
        "stop_loss": stop_loss, "take_profit": take_profit,
    }


# ── 1. 物化器 ───────────────────────────────────────────
def _matrix(panel: pl.DataFrame):
    return build_market_data_matrix(panel)


def test_materialize_entry_row_next_day_with_prices(data_dir):
    market = _matrix(_panel({SYM_A: [(10.0, 10.2, 9.8, 10.0), (10.2, 10.4, 9.9, 10.3)]}))
    _save_report("r1", SYM_A, "逢低买入", _points(ideal_buy=10.0, stop_loss=9.5, take_profit=10.8))
    signals = dsa_points.materialize_dsa_signal_rows(market, BASE, BASE)
    assert signals.entry.sum() == 1
    # 入场格 = 报告日次一交易日 (t=1), 逐格介入/止损/止盈价同行注入
    assert signals.entry[0, 0] == 0 and signals.entry[1, 0] == 1
    assert float(signals.entry_price[1, 0]) == pytest.approx(10.0)
    assert float(signals.stop_price[1, 0]) == pytest.approx(9.5)
    assert float(signals.take_profit_price[1, 0]) == pytest.approx(10.8)
    assert float(signals.entry_price[0, 0]) != float(signals.entry_price[0, 0])  # NaN


def test_materialize_sell_advice_exit_at_report_day(data_dir):
    market = _matrix(_panel({SYM_A: [(10.0, 10.2, 9.8, 10.0), (10.2, 10.4, 9.9, 10.3)]}))
    _save_report("r1", SYM_A, "清仓离场", _points())
    signals = dsa_points.materialize_dsa_signal_rows(market, BASE, BASE)
    # 离场行在报告日 (引擎 exit_delay=1 → 次日开盘成交), 不产生入场
    assert signals.exit[0, 0] == 1 and signals.entry.sum() == 0


def test_materialize_hold_and_missing_ideal_buy_skip(data_dir):
    market = _matrix(_panel({SYM_A: [(10.0, 10.2, 9.8, 10.0), (10.2, 10.4, 9.9, 10.3)]}))
    _save_report("r1", SYM_A, "持有观望", _points(ideal_buy=10.0))
    _save_report("r2", SYM_A, "回调买入", _points(ideal_buy=None), created="2024-01-02T18:00:00")
    signals = dsa_points.materialize_dsa_signal_rows(market, BASE, BASE)
    assert signals.entry.sum() == 0 and signals.exit.sum() == 0


def test_materialize_same_day_later_report_wins(data_dir):
    market = _matrix(_panel({SYM_A: [(10.0, 10.2, 9.8, 10.0), (10.2, 10.4, 9.9, 10.3)]}))
    _save_report("r1", SYM_A, "买入", _points(ideal_buy=10.0), created="2024-01-01T18:00:00")
    _save_report("r2", SYM_A, "加仓", _points(ideal_buy=9.8), created="2024-01-01T20:00:00")
    signals = dsa_points.materialize_dsa_signal_rows(market, BASE, BASE)
    assert signals.entry.sum() == 1
    assert float(signals.entry_price[1, 0]) == pytest.approx(9.8)  # 更晚创建的报告生效


def test_materialize_window_and_symbol_bounds(data_dir):
    market = _matrix(_panel({SYM_A: [(10.0, 10.2, 9.8, 10.0), (10.2, 10.4, 9.9, 10.3)]}))
    _save_report("r1", SYM_A, "买入", _points(ideal_buy=10.0), created="2023-12-25T18:00:00")
    _save_report("r2", "999999.SZ", "买入", _points(ideal_buy=10.0), created="2024-01-01T18:00:00")
    signals = dsa_points.materialize_dsa_signal_rows(market, BASE, BASE + timedelta(days=1))
    assert signals.entry.sum() == 0  # 窗口外报告 + 市场外标的均跳过


# ── 2. 内建策略文件装载 ─────────────────────────────────
def test_builtin_strategy_registered():
    engine = StrategyEngine(strategy_dirs=[BUILTIN_DIR])
    assert engine.has("dsa_points")
    listed = {s["id"]: s for s in engine.list_strategies()}
    assert listed["dsa_points"]["name"] == "DSA 点位"
    assert listed["dsa_points"]["source"] == "builtin"
    # 选股器空跑安全: compute_signals 返回合法空信号
    market = build_market_data_matrix(_panel({SYM_A: [(10.0, 10.2, 9.8, 10.0)]}))
    signals = engine.get("dsa_points").matrix_strategy.compute_signals(market, {})
    assert signals.entry.sum() == 0


# ── 3. 策略级 e2e (仓位模拟 = portfolio matrix 撮合器) ──
def _service(panel: pl.DataFrame) -> StrategyBacktestService:
    engine = StrategyEngine(strategy_dirs=[BUILTIN_DIR])
    bt_engine = BacktestEngine(repo=None)
    bt_engine.load_market_data_matrix_for_backtest = (  # type: ignore[method-assign]
        lambda *args, **kwargs: build_market_data_matrix(panel)
    )
    return StrategyBacktestService(engine=bt_engine, strategy_engine=engine)


def _config(start: date, end: date, mode: str = "position", **kw) -> StrategyBacktestConfig:
    defaults = dict(
        strategy_id="dsa_points",
        symbols=[SYM_A, SYM_B],
        start=start, end=end,
        matching="open_t+1",   # 请求默认口径; dsa_points 应钉死为 close_t/open_t+1
        fees_pct=0, slippage_bps=0,
        max_positions=10,
        mode=mode,
        holding_days=5,
    )
    defaults.update(kw)
    return StrategyBacktestConfig(**defaults)


def test_e2e_position_mode_entry_touch_and_per_position_stop(data_dir):
    """两票各自报告线各自生效: A 止损 9.5 / B 止损 9.0; 入场价=触及口径。"""
    panel = _panel({
        SYM_A: [
            (10.0, 10.2, 9.8, 10.0),   # d0 报告日
            (10.2, 10.4, 9.9, 10.3),   # d1 触及 10.0 → 建仓 @10.0
            (10.1, 10.2, 9.4, 9.6),    # d2 破 A 线 9.5 → 止损 @9.5
            (9.6, 9.7, 9.3, 9.4),
        ],
        SYM_B: [
            (10.0, 10.2, 9.8, 10.0),
            (10.2, 10.4, 9.9, 10.3),   # 建仓 @10.0
            (10.1, 10.2, 9.2, 9.3),    # 破 A 线也破 B 线 9.0 → B 止损 @9.0 (逐仓位)
            (9.2, 9.3, 8.9, 9.0),
        ],
    })
    _save_report("ra", SYM_A, "逢低买入", _points(ideal_buy=10.0, stop_loss=9.5))
    _save_report("rb", SYM_B, "逢低买入", _points(ideal_buy=10.0, stop_loss=9.0))
    result = _service(panel).run(_config(BASE, BASE + timedelta(days=3)))
    assert result.error is None
    by_symbol = {t["symbol"]: t for t in result.trades}
    assert by_symbol[SYM_A]["entry_price"] == pytest.approx(10.0)
    assert by_symbol[SYM_A]["entry_date"] == "2024-01-02"
    assert by_symbol[SYM_A]["exit_reason"] == "stop_loss"
    assert by_symbol[SYM_A]["exit_price"] == pytest.approx(9.5)
    assert by_symbol[SYM_B]["exit_reason"] == "stop_loss"
    assert by_symbol[SYM_B]["exit_price"] == pytest.approx(9.0)  # 各自报告线, 非同一百分比


def test_e2e_gap_down_fills_at_open_and_take_profit(data_dir):
    panel = _panel({
        SYM_A: [
            (10.0, 10.2, 9.8, 10.0),
            (9.7, 10.0, 9.6, 9.9),     # 开盘 9.7 < 介入价 10 → 按开盘价 (更优)
            (10.4, 11.0, 10.3, 10.9),  # 触 10.8 → 止盈 @10.8
            (10.9, 11.0, 10.8, 10.9),
        ],
    })
    _save_report("ra", SYM_A, "买入", _points(ideal_buy=10.0, take_profit=10.8))
    result = _service(panel).run(_config(BASE, BASE + timedelta(days=3), symbols=[SYM_A]))
    assert result.error is None
    (trade,) = result.trades
    assert trade["entry_price"] == pytest.approx(9.7)
    assert trade["exit_reason"] == "take_profit" and trade["exit_price"] == pytest.approx(10.8)


def test_e2e_entry_not_touched_no_position(data_dir):
    panel = _panel({
        SYM_A: [
            (10.0, 10.2, 9.8, 10.0),
            (10.6, 10.8, 10.5, 10.7),  # 次日全天未触及 10.0 → 不建仓
            (10.6, 10.8, 10.5, 10.7),
        ],
    })
    _save_report("ra", SYM_A, "买入", _points(ideal_buy=10.0, stop_loss=9.5))
    result = _service(panel).run(_config(BASE, BASE + timedelta(days=2), symbols=[SYM_A]))
    assert result.error is None
    assert result.trades == []


def test_e2e_sell_advice_exits_next_open(data_dir):
    """先买入建仓, 后续报告给出清仓建议 → 次日开盘离场。"""
    panel = _panel({
        SYM_A: [
            (10.0, 10.2, 9.8, 10.0),   # d0 买入报告 → d1 建仓 @10.0
            (10.2, 10.4, 9.9, 10.3),
            (10.5, 10.6, 10.2, 10.4),  # d2 清仓报告 → d3 开盘 10.4 离场
            (10.4, 10.5, 10.1, 10.2),
        ],
    })
    _save_report("r1", SYM_A, "买入", _points(ideal_buy=10.0, stop_loss=9.0), created="2024-01-01T18:00:00")
    _save_report("r2", SYM_A, "清仓离场", _points(), created="2024-01-03T18:00:00")
    result = _service(panel).run(_config(BASE, BASE + timedelta(days=3), symbols=[SYM_A]))
    assert result.error is None
    (trade,) = result.trades
    assert trade["entry_date"] == "2024-01-02"
    assert trade["exit_reason"] == "signal"
    assert trade["exit_date"] == "2024-01-04"
    assert trade["exit_price"] == pytest.approx(10.4)  # d3 开盘价 (exit_fill=open_t+1)


def test_e2e_full_mode_max_hold_window(data_dir):
    """全量模拟 (independent 撮合器): 无线无卖出建议 → 持有 10 根到期。"""
    flat = [(10.0, 10.05, 9.95, 10.0)] * 13
    panel = _panel({SYM_A: flat})
    _save_report("ra", SYM_A, "买入", _points(ideal_buy=10.0))
    result = _service(panel).run(_config(BASE, BASE + timedelta(days=5), mode="full", symbols=[SYM_A]))
    assert result.error is None
    (trade,) = result.trades
    assert trade["entry_price"] == pytest.approx(10.0)  # 平价触及 (low 9.95 ≤ 10)
    assert trade["exit_reason"] == "max_hold"
    assert trade["duration"] == 10
