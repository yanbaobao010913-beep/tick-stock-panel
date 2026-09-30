"""dsa_paper 点位模拟 + 报告回测的单元测试 (P5 §4.6)。

全部用内存假 K 线 + 假报告库喂, 不依赖真实 repo/LLM:
- PaperStubRepo: 预置 symbol → bar 元组列表, get_daily_asset 走 polars DataFrame
- FakeReportStore: 顶替 dsa_analysis._STORE.list_reports
覆盖契约 §4.6-1/2/3/4 的入场/出场状态机/槽位/净值/命中率口径。
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import polars as pl

from app.custom import dsa_analysis, dsa_paper
from app.market_time import cn_now

# ================================================================
# 测试基建
# ================================================================

def _bdays(n: int, end: date | None = None) -> list[date]:
    """end (默认昨天) 往前数 n 个工作日, 升序。"""
    end = end or (cn_now().date() - timedelta(days=1))
    days: list[date] = []
    d = end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return sorted(days)


def _bar(d: date, o: float, h: float, lo: float, c: float) -> tuple:
    return (d, o, h, lo, c)


class PaperStubRepo:
    """内存 repo: bars_by_symbol = {symbol: [(date, o, h, l, c), ...]}。"""

    def __init__(self, bars_by_symbol: dict[str, list[tuple]]) -> None:
        self._bars = bars_by_symbol

    def resolve_asset_type(self, symbol: str) -> str:
        return "index" if symbol == "000300.SH" else "stock"

    def get_name_map(self, symbols=None) -> dict[str, str]:
        return {s: f"名称{s}" for s in (symbols or [])}

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None) -> pl.DataFrame:
        rows = [r for r in self._bars.get(symbol, []) if start <= r[0] <= end]
        return pl.DataFrame({
            "date": [r[0] for r in rows],
            "open": [r[1] for r in rows],
            "high": [r[2] for r in rows],
            "low": [r[3] for r in rows],
            "close": [r[4] for r in rows],
        })


class FakeReportStore:
    def __init__(self, reports: list[dict]) -> None:
        self._reports = reports

    def list_reports(self, symbol=None) -> list[dict]:
        return list(self._reports)


def _report(symbol: str, created: date, advice: str, points: dict, report_id: str) -> dict:
    return {
        "id": report_id,
        "symbol": symbol,
        "name": f"名称{symbol}",
        "created_at": created.isoformat() + "T18:00:00",
        "operation_advice": advice,
        "points": points,
    }


def _setup(
    tmp_path, monkeypatch, repo: PaperStubRepo, reports: list[dict], inception: date | None = None,
) -> None:
    monkeypatch.setattr(dsa_paper, "_RUNTIME", {"repo": repo, "data_dir": tmp_path})
    monkeypatch.setattr(dsa_analysis, "_STORE", FakeReportStore(reports))
    if inception is not None:
        # 前向账户语义: 首轮 inception=今天, 历史报告被过滤; 测试预置 state 把
        # inception 拨到报告日, 才能重放历史报告 (顺带覆盖 state 持久化路径)
        d = tmp_path / "dsa_paper"
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(json.dumps({
            "inception_date": inception.isoformat(),
            "initial_capital": 100000.0,
            "max_slots": 10,
        }), encoding="utf-8")


def _trades(tmp_path) -> list[dict]:
    return json.loads((tmp_path / "dsa_paper" / "trades.json").read_text(encoding="utf-8"))["items"]


_BENCH = {  # 沪深300: 自然日序列含今天, 保证 inception=今天也有基准行 (stub 不剔休市)
    "000300.SH": [
        _bar(d, 4000.0, 4001.0, 3999.0, 4000.0)
        for d in sorted(cn_now().date() - timedelta(days=i) for i in range(40))
    ],
}


# ================================================================
# §4.6-1 入场
# ================================================================

def test_entry_open_better_and_intraday_touch(tmp_path, monkeypatch):
    days = _bdays(6)
    d0, d1, d2 = days[0], days[1], days[2]
    repo = PaperStubRepo({
        **_BENCH,
        # A: 次日开盘 9.5 低于 ideal 10 → 按开盘成交
        "600001.SH": [_bar(d0, 10.2, 10.4, 10.1, 10.3), _bar(d1, 9.5, 10.2, 9.4, 10.0),
                      _bar(d2, 10.0, 10.2, 9.9, 10.1)],
        # B: 开盘 10.2 高于 ideal, 盘中最低 9.95 触及 → 按 ideal 成交
        "600002.SH": [_bar(d0, 10.2, 10.4, 10.1, 10.3), _bar(d1, 10.2, 10.3, 9.95, 10.1),
                      _bar(d2, 10.1, 10.3, 10.0, 10.2)],
    })
    reports = [
        _report("600001.SH", d0, "买入", {"ideal_buy": 10.0, "stop_loss": 9.0, "take_profit": 12.0}, "r_a"),
        _report("600002.SH", d0, "买入", {"ideal_buy": 10.0, "stop_loss": 9.0, "take_profit": 12.0}, "r_b"),
    ]
    _setup(tmp_path, monkeypatch, repo, reports, inception=d0)
    result = dsa_paper.run_paper()
    assert result["ok"] and result["episodes"] == 2
    trades = _trades(tmp_path)
    by_symbol = {t["symbol"]: t for t in trades}
    assert by_symbol["600001.SH"]["entry_price"] == 9.5      # 开盘更优按开盘
    assert by_symbol["600001.SH"]["entry_date"] == d1.isoformat()
    assert by_symbol["600002.SH"]["entry_price"] == 10.0     # 盘中触及按 ideal
    # 零碎股: slot_capital/entry_price, 不整手
    assert by_symbol["600001.SH"]["shares"] == round(10000.0 / 9.5, 6)


def test_entry_not_touched_no_position_and_bad_entries_rejected(tmp_path, monkeypatch):
    days = _bdays(6)
    d0, d1, d2 = days[0], days[1], days[2]
    repo = PaperStubRepo({
        **_BENCH,
        # A: 全天未触及 ideal (最低 10.1 > 10) → 不建仓
        "600001.SH": [_bar(d0, 10.2, 10.4, 10.1, 10.3), _bar(d1, 10.3, 10.4, 10.1, 10.2),
                      _bar(d2, 10.2, 10.4, 10.1, 10.3)],
        # B: 开盘 9.4 直接破止损 9.5 → 放弃建仓 (防假盈利)
        "600002.SH": [_bar(d0, 10.2, 10.4, 10.1, 10.3), _bar(d1, 9.4, 9.6, 9.3, 9.5),
                      _bar(d2, 9.5, 9.7, 9.4, 9.6)],
        # C: 一字板 (low==high) 买不进 (止损 9.0 不拦, 由一字板规则拦)
        "600003.SH": [_bar(d0, 10.2, 10.4, 10.1, 10.3), _bar(d1, 9.5, 9.5, 9.5, 9.5),
                      _bar(d2, 10.0, 10.2, 9.9, 10.1)],
    })
    reports = [
        _report("600001.SH", d0, "买入", {"ideal_buy": 10.0, "stop_loss": 9.5, "take_profit": 12.0}, "r_a"),
        _report("600002.SH", d0, "买入", {"ideal_buy": 10.0, "stop_loss": 9.5, "take_profit": 12.0}, "r_b"),
        _report("600003.SH", d0, "买入", {"ideal_buy": 10.0, "stop_loss": 9.0, "take_profit": 12.0}, "r_c"),
    ]
    _setup(tmp_path, monkeypatch, repo, reports, inception=d0)
    result = dsa_paper.run_paper()
    assert result["episodes"] == 0
    assert _trades(tmp_path) == []


def test_sell_signal_exits_next_open_and_buy_ignored_while_holding(tmp_path, monkeypatch):
    days = _bdays(8)
    d0, d1, d2, d3, d4 = days[0], days[1], days[2], days[3], days[4]
    repo = PaperStubRepo({
        **_BENCH,
        "600001.SH": [
            _bar(d0, 10.2, 10.4, 10.1, 10.3),
            _bar(d1, 10.05, 10.3, 9.9, 10.1),   # 建仓 10.0
            _bar(d2, 10.1, 10.3, 10.0, 10.2),
            _bar(d3, 10.2, 10.4, 10.1, 10.3),
            _bar(d4, 10.3, 10.5, 10.2, 10.4),   # 卖出信号次日开盘平
        ],
    })
    reports = [
        _report("600001.SH", d0, "买入", {"ideal_buy": 10.0, "stop_loss": 9.0, "take_profit": 12.0}, "r_in"),
        # 持仓期间的买入信号忽略
        _report("600001.SH", d2, "买入", {"ideal_buy": 9.5, "stop_loss": 9.0, "take_profit": 12.0}, "r_ignored"),
        _report("600001.SH", d3, "卖出", {"ideal_buy": None, "stop_loss": 9.0, "take_profit": 12.0}, "r_sell"),
    ]
    _setup(tmp_path, monkeypatch, repo, reports, inception=d0)
    result = dsa_paper.run_paper()
    assert result["episodes"] == 1
    trade = _trades(tmp_path)[0]
    assert trade["exit_reason"] == "sell_signal"
    assert trade["exit_date"] == d4.isoformat()
    assert trade["exit_price"] == 10.3


# ================================================================
# §4.6-2 出场状态机
# ================================================================

def test_stop_loss_touch_and_gap_open_fill(tmp_path, monkeypatch):
    days = _bdays(8)
    d0, d1, d2 = days[0], days[1], days[2]
    points = {"ideal_buy": 10.0, "stop_loss": 9.5, "take_profit": 12.0}
    repo = PaperStubRepo({
        **_BENCH,
        # A: 盘中触及止损 → 按止损价成交
        "600001.SH": [_bar(d0, 10.2, 10.4, 10.1, 10.3), _bar(d1, 10.2, 10.3, 9.9, 10.0),
                      _bar(d2, 9.9, 10.0, 9.4, 9.6)],
        # B: 跳空低开破线 → 按开盘价成交 (min(止损价, 开盘价))
        "600002.SH": [_bar(d0, 10.2, 10.4, 10.1, 10.3), _bar(d1, 10.2, 10.3, 9.9, 10.0),
                      _bar(d2, 8.9, 9.0, 8.8, 8.85)],
    })
    reports = [
        _report("600001.SH", d0, "买入", points, "r_a"),
        _report("600002.SH", d0, "买入", points, "r_b"),
    ]
    _setup(tmp_path, monkeypatch, repo, reports, inception=d0)
    result = dsa_paper.run_paper()
    assert result["closed"] == 2
    by_symbol = {t["symbol"]: t for t in _trades(tmp_path)}
    assert by_symbol["600001.SH"]["exit_reason"] == "stop_loss"
    assert by_symbol["600001.SH"]["exit_price"] == 9.5
    assert by_symbol["600001.SH"]["return_pct"] == -5.0
    assert by_symbol["600002.SH"]["exit_reason"] == "stop_loss"
    assert by_symbol["600002.SH"]["exit_price"] == 8.9    # 跳空按开盘, 不抹缺口


def test_trailing_arm_new_high_then_trail_hit(tmp_path, monkeypatch):
    """trail%=5% (锚点前不足21根 → 回落); 启动根不判线 (无未来函数)。"""
    days = _bdays(8)
    d0, d1, d2, d3, d4 = days[0], days[1], days[2], days[3], days[4]
    points = {"ideal_buy": 10.0, "stop_loss": 9.0, "take_profit": 12.0}
    common_head = [_bar(d0, 10.2, 10.4, 10.1, 10.3), _bar(d1, 10.05, 10.3, 9.9, 10.1)]
    repo = PaperStubRepo({
        **_BENCH,
        # A: 启动 → 新高 → 触线 (每根都独立推进)
        "600001.SH": [*common_head,
                      _bar(d2, 12.0, 12.5, 12.2, 12.3),    # 启动, peak=12.5, 本根不判线
                      _bar(d3, 12.7, 13.2, 12.6, 13.0),    # 新高 peak=13.2
                      _bar(d4, 12.8, 12.9, 12.4, 12.5)],    # line=13.2*0.95=12.54 → trail_hit
        # B: 启动根当日 low 深破"假想线"也不出场 (无未来函数), 后续同样 trail_hit
        "600002.SH": [*common_head,
                      _bar(d2, 11.5, 12.5, 11.0, 12.3),    # 启动; 11.0 ≤ 12.5*0.95 但本根不判
                      _bar(d3, 12.7, 13.2, 12.6, 13.0),    # 新高 peak=13.2
                      _bar(d4, 12.8, 12.9, 12.4, 12.5)],    # trail_hit 12.54
    })
    reports = [
        _report("600001.SH", d0, "买入", points, "r_a"),
        _report("600002.SH", d0, "买入", points, "r_b"),
    ]
    _setup(tmp_path, monkeypatch, repo, reports, inception=d0)
    result = dsa_paper.run_paper()
    assert result["closed"] == 2
    by_symbol = {t["symbol"]: t for t in _trades(tmp_path)}
    for trade in by_symbol.values():
        assert trade["exit_reason"] == "trail_hit"
        assert trade["exit_date"] == d4.isoformat()       # 不是启动根 d2
        assert trade["exit_price"] == 12.54               # max(12, 13.2x0.95)
        assert trade["return_pct"] == 25.4


def test_stop_loss_disabled_after_armed_gap_fill(tmp_path, monkeypatch):
    """启动后止损失效: 已启动 bar 跳空跌破止损也走 trail_hit (按开盘价)。"""
    days = _bdays(8)
    d0, d1, d2, d3 = days[0], days[1], days[2], days[3]
    repo = PaperStubRepo({
        **_BENCH,
        "600001.SH": [
            _bar(d0, 10.2, 10.4, 10.1, 10.3),
            _bar(d1, 10.05, 10.3, 9.9, 10.1),    # 建仓 10.0
            _bar(d2, 12.0, 12.5, 12.2, 12.3),    # 启动 peak=12.5, line=11.875
            _bar(d3, 8.5, 8.8, 8.5, 8.6),        # 破止损 9.0 但已启动 → trail_hit 按开盘 8.5
        ],
    })
    reports = [
        _report("600001.SH", d0, "买入", {"ideal_buy": 10.0, "stop_loss": 9.0, "take_profit": 12.0}, "r_a"),
    ]
    _setup(tmp_path, monkeypatch, repo, reports, inception=d0)
    dsa_paper.run_paper()
    trade = _trades(tmp_path)[0]
    assert trade["exit_reason"] == "trail_hit"   # 不是 stop_loss
    assert trade["exit_price"] == 8.5            # 跳空按开盘价
    assert trade["return_pct"] == -15.0


def test_t_plus_1_entry_bar_not_checked(tmp_path, monkeypatch):
    """建仓当根 (T+1) 即使最低价破止损也不检查; 次根起检查。"""
    days = _bdays(8)
    d0, d1, d2 = days[0], days[1], days[2]
    repo = PaperStubRepo({
        **_BENCH,
        "600001.SH": [
            _bar(d0, 10.2, 10.4, 10.1, 10.3),
            _bar(d1, 10.0, 10.1, 8.5, 9.8),      # 建仓根: low 8.5 破止损 9.0, 不检查
            _bar(d2, 9.9, 10.2, 9.5, 10.0),      # 次根 low 9.5 > 止损 → 不触发
        ],
    })
    reports = [
        _report("600001.SH", d0, "买入", {"ideal_buy": 10.0, "stop_loss": 9.0, "take_profit": 12.0}, "r_a"),
    ]
    _setup(tmp_path, monkeypatch, repo, reports, inception=d0)
    result = dsa_paper.run_paper()
    assert result["episodes"] == 1 and result["open"] == 1
    trade = _trades(tmp_path)[0]
    assert trade["status"] == "open" and trade["exit_reason"] is None
    assert trade["holding_bars"] == 2


def test_window_expired_forces_close_at_close(tmp_path, monkeypatch):
    """持有满 10 根日K (建仓根计 1) → 第 10 根当日收盘 window_expired。"""
    days = _bdays(12)
    d0 = days[0]
    entry_bar = _bar(days[1], 9.5, 9.6, 9.4, 9.5)   # 开盘 9.5 < 10 建仓
    # days[2..10] 共 9 根平稳 bar: 到 days[10] holding_bars=10 强平
    flat = [_bar(d, 10.0, 10.2, 9.8, 10.0) for d in days[2:11]]
    repo = PaperStubRepo({
        **_BENCH,
        "600001.SH": [_bar(d0, 10.2, 10.4, 10.1, 10.3), entry_bar, *flat],
    })
    reports = [
        _report("600001.SH", d0, "买入", {"ideal_buy": 10.0, "stop_loss": 5.0, "take_profit": 50.0}, "r_a"),
    ]
    _setup(tmp_path, monkeypatch, repo, reports, inception=d0)
    dsa_paper.run_paper()
    trade = _trades(tmp_path)[0]
    assert trade["exit_reason"] == "window_expired"
    assert trade["exit_date"] == days[10].isoformat()
    assert trade["exit_price"] == 10.0
    assert trade["holding_bars"] == 10


def test_limit_board_trigger_defers(tmp_path, monkeypatch):
    """一字板当天触发线卖不出 → 状态顺延, 下一根按新线/开盘价出场。"""
    days = _bdays(8)
    d0, d1, d2, d3 = days[0], days[1], days[2], days[3]
    repo = PaperStubRepo({
        **_BENCH,
        "600001.SH": [
            _bar(d0, 10.2, 10.4, 10.1, 10.3),
            _bar(d1, 10.05, 10.3, 9.9, 10.1),    # 建仓 10.0
            _bar(d2, 12.0, 12.5, 12.2, 12.3),    # 启动 peak=12.5, line=11.875
            _bar(d3, 11.8, 11.8, 11.8, 11.8),    # 一字 low 11.8 ≤ line 11.875 → 卖不出, 顺延
            _bar(days[4], 11.7, 11.75, 11.6, 11.65),  # 次根 low 11.6 ≤ line → trail_hit 11.7
        ],
    })
    reports = [
        _report("600001.SH", d0, "买入", {"ideal_buy": 10.0, "stop_loss": 9.0, "take_profit": 12.0}, "r_a"),
    ]
    _setup(tmp_path, monkeypatch, repo, reports, inception=d0)
    dsa_paper.run_paper()
    trade = _trades(tmp_path)[0]
    assert trade["exit_reason"] == "trail_hit"
    assert trade["exit_date"] == days[4].isoformat()   # 顺延到下一根, 不是一字板当天
    assert trade["exit_price"] == 11.7


# ================================================================
# §4.6-3 槽位 / 净值 / 幂等
# ================================================================

def test_slot_full_skipped_no_slot_and_no_bars(tmp_path, monkeypatch):
    days = _bdays(6)
    d0, d1 = days[0], days[1]
    bars = {
        "000300.SH": _BENCH["000300.SH"],
        # 11 只同 day 建仓, 10 槽位 → 1 只 no_slot
        **{
            f"6000{i:02d}.SH": [_bar(d0, 10.2, 10.4, 10.1, 10.3), _bar(d1, 9.5, 10.2, 9.4, 10.0)]
            for i in range(1, 12)
        },
    }
    repo = PaperStubRepo(bars)
    reports = [
        _report(f"6000{i:02d}.SH", d0, "买入", {"ideal_buy": 10.0, "stop_loss": 9.0, "take_profit": 12.0}, f"r{i}")
        for i in range(1, 12)
    ] + [
        # 无行情 → skipped/no_bars
        _report("600099.SH", d0, "买入", {"ideal_buy": 10.0, "stop_loss": 9.0, "take_profit": 12.0}, "r99"),
    ]
    _setup(tmp_path, monkeypatch, repo, reports, inception=d0)
    dsa_paper.run_paper()
    trades = _trades(tmp_path)
    open_trades = [t for t in trades if t["status"] == "open"]
    skipped = [t for t in trades if t["status"] == "skipped"]
    assert len(open_trades) == 10
    assert len(skipped) == 2
    reasons = sorted(t["skip_reason"] for t in skipped)
    assert reasons == ["no_bars", "no_slot"]


def test_equity_curve_marked_to_market_with_benchmark(tmp_path, monkeypatch):
    days = _bdays(6)
    d0, d1, d2 = days[0], days[1], days[2]
    repo = PaperStubRepo({
        "000300.SH": _BENCH["000300.SH"],
        "600001.SH": [_bar(d0, 10.2, 10.4, 10.1, 10.3), _bar(d1, 10.05, 10.2, 9.9, 10.0),
                      _bar(d2, 10.0, 11.0, 10.0, 11.0)],   # 建仓 10.0, +10% 收盘 MTM
    })
    reports = [
        _report("600001.SH", d0, "买入", {"ideal_buy": 10.0, "stop_loss": 9.0, "take_profit": 12.0}, "r_a"),
    ]
    _setup(tmp_path, monkeypatch, repo, reports, inception=d0)
    result = dsa_paper.run_paper()
    assert result["ok"]
    equity = json.loads((tmp_path / "dsa_paper" / "equity.json").read_text(encoding="utf-8"))["items"]
    assert len(equity) >= 2                       # 曲线从 inception(=d0) 起逐日
    assert equity[0]["date"] == d0.isoformat()
    assert equity[0]["equity"] == 100000.0        # 建仓前 (d1 才建仓)
    assert equity[0]["benchmark"] == 100000.0     # 基准首日归一
    row = equity[-1]
    assert row["equity"] == 101000.0              # 现金 9 万 + MTM 1.1 万 (11/10)
    assert row["benchmark"] == 100000.0           # 基准平走
    overview_state = json.loads((tmp_path / "dsa_paper" / "state.json").read_text(encoding="utf-8"))
    assert overview_state["initial_capital"] == 100000.0
    assert overview_state["max_slots"] == 10


def test_replay_is_idempotent(tmp_path, monkeypatch):
    days = _bdays(10)
    d0, d1, d2 = days[0], days[1], days[2]
    repo = PaperStubRepo({
        **_BENCH,
        "600001.SH": [_bar(d0, 10.2, 10.4, 10.1, 10.3), _bar(d1, 9.5, 10.2, 9.4, 10.0),
                      _bar(d2, 9.9, 10.0, 9.4, 9.6)],
        "600002.SH": [_bar(d0, 10.2, 10.4, 10.1, 10.3), _bar(d1, 10.2, 10.3, 9.95, 10.1),
                      _bar(d2, 10.1, 10.3, 10.0, 10.2)],
    })
    reports = [
        _report("600001.SH", d0, "买入", {"ideal_buy": 10.0, "stop_loss": 9.0, "take_profit": 12.0}, "r_a"),
        _report("600002.SH", d0, "买入", {"ideal_buy": 10.0, "stop_loss": 9.0, "take_profit": 12.0}, "r_b"),
    ]
    _setup(tmp_path, monkeypatch, repo, reports, inception=d0)
    first = dsa_paper.run_paper()
    assert first["episodes"] == 2
    trades_first = (tmp_path / "dsa_paper" / "trades.json").read_text(encoding="utf-8")
    equity_first = (tmp_path / "dsa_paper" / "equity.json").read_text(encoding="utf-8")
    second = dsa_paper.run_paper()
    assert second["ok"] and second["episodes"] == first["episodes"]
    assert (tmp_path / "dsa_paper" / "trades.json").read_text(encoding="utf-8") == trades_first
    assert (tmp_path / "dsa_paper" / "equity.json").read_text(encoding="utf-8") == equity_first


# ================================================================
# §4.6-4 报告回测 (outcomes) 分母口径
# ================================================================

def test_outcomes_direction_mapping_and_denominator(tmp_path, monkeypatch):
    """hit/miss/neutral/unable 混合: 命中率分母 = hit+miss, neutral/unable 全排除。"""
    days = _bdays(20)
    d0 = days[0]
    fwd = [_bar(d, 10.4, 10.6, 10.2, 10.5) for d in days[1:11]]      # +5%
    fwd_down = [_bar(d, 9.8, 9.9, 9.7, 9.8) for d in days[1:11]]      # -2%
    fwd_neutral = [_bar(d, 10.09, 10.1, 10.08, 10.1) for d in days[1:11]]  # +1% 死区
    repo = PaperStubRepo({
        **_BENCH,
        "600001.SH": [_bar(d0, 10.0, 10.1, 9.9, 10.0), *fwd],           # up → hit x4
        "600002.SH": [_bar(d0, 10.0, 10.1, 9.9, 10.0), *fwd],           # 持有 not_down → hit x4
        "600003.SH": [_bar(d0, 10.0, 10.1, 9.9, 10.0), *fwd_down],      # 卖出 not_up → hit x4
        "600004.SH": [_bar(d0, 10.0, 10.1, 9.9, 10.0), *fwd_neutral],   # up → neutral x4 (排除)
        "600005.SH": [_bar(d0, 10.0, 10.1, 9.9, 10.0), *fwd],           # 观望 → unable x4 (排除)
        "600006.SH": [_bar(d0, 10.0, 10.1, 9.9, 10.0),
                      *[_bar(d, 9.5, 9.6, 9.4, 9.5) for d in days[1:11]]],     # up → miss x4
        # 前向不足 10 根: h1/h3 可判, h5/h10 unable
        "600007.SH": [_bar(d0, 10.0, 10.1, 9.9, 10.0), *fwd[:3]],
    })
    pts = {"ideal_buy": 9.0, "stop_loss": 8.0, "take_profit": 20.0}
    reports = [
        _report("600001.SH", d0, "买入", pts, "r1"),
        _report("600002.SH", d0, "持有", pts, "r2"),
        _report("600003.SH", d0, "卖出", pts, "r3"),
        _report("600004.SH", d0, "买入", pts, "r4"),
        _report("600005.SH", d0, "观望", pts, "r5"),
        _report("600006.SH", d0, "买入", pts, "r6"),
        _report("600007.SH", d0, "买入", pts, "r7"),
    ]
    _setup(tmp_path, monkeypatch, repo, reports, inception=d0)
    result = dsa_paper.compute_outcomes()
    items = result["items"]
    assert len(items) == 7 * 4

    def _of(report_id: str) -> list[dict]:
        return sorted([i for i in items if i["report_id"] == report_id], key=lambda i: i["horizon"])

    assert [i["outcome"] for i in _of("r1")] == ["hit"] * 4
    assert [i["outcome"] for i in _of("r2")] == ["hit"] * 4       # not_down 死区内算命中
    assert [i["outcome"] for i in _of("r3")] == ["hit"] * 4       # not_up
    assert [i["outcome"] for i in _of("r4")] == ["neutral"] * 4   # ±2% 死区
    assert [i["outcome"] for i in _of("r5")] == ["unable"] * 4    # 观望不进分母
    assert [i["outcome"] for i in _of("r6")] == ["miss"] * 4
    # 前向不足: h1/h3 判得出, h5/h10 unable
    assert [i["outcome"] for i in _of("r7")] == ["hit", "hit", "unable", "unable"]
    # up 走出场状态机: exit_reason ∈ stop_loss/trail_hit/window_end
    assert all(i["exit_reason"] in ("stop_loss", "trail_hit", "window_end") for i in _of("r1"))
    assert all(i["exit_reason"] is None for i in _of("r3"))       # not_up 纯窗口

    stats = result["stats"]
    # 分母 = hit+miss = 4*3 + 4 + 2 = 18; hits = 12+2 = 14
    assert stats["n"] == 18
    assert stats["hits"] == 14 and stats["misses"] == 4
    assert stats["hit_rate"] == 77.8
    # 分 horizon: h1 = (4 hit + 1 miss) → 80.0; h5 缺 r7 → 3/4 → 75.0
    assert stats["by_horizon"]["1"]["n"] == 5
    assert stats["by_horizon"]["1"]["hit_rate"] == 80.0
    assert stats["by_horizon"]["5"]["n"] == 4
    assert stats["by_horizon"]["5"]["hit_rate"] == 75.0


def test_outcomes_up_direction_uses_state_machine(tmp_path, monkeypatch):
    """up 的 return_pct 按出场状态机 (止损截断), 不是裸窗口收益。"""
    days = _bdays(20)
    d0 = days[0]
    # 前 1 根大涨后连续跌停: 裸窗口看 h10 可能为正, 状态机先止损出场为负
    bars = [_bar(d0, 10.0, 10.1, 9.9, 10.0)]
    bars += [_bar(days[1], 10.2, 10.8, 10.2, 10.8)]                 # +8% (无启动价参与)
    bars += [_bar(d, 9.0, 9.1, 8.9, 9.0) for d in days[2:12]]       # 连续破止损 9.5
    repo = PaperStubRepo({**_BENCH, "600001.SH": bars})
    reports = [
        _report("600001.SH", d0, "买入", {"ideal_buy": 9.0, "stop_loss": 9.5, "take_profit": 20.0}, "r1"),
    ]
    _setup(tmp_path, monkeypatch, repo, reports, inception=d0)
    result = dsa_paper.compute_outcomes()
    by_h = {i["horizon"]: i for i in result["items"]}
    assert by_h[1]["outcome"] == "hit"            # 止损前窗口 +8% ≥ 2%
    assert by_h[1]["exit_reason"] == "window_end"  # h1 仅含 d1 (high 10.8 < 20 未启动)
    assert by_h[1]["return_pct"] == 8.0
    assert by_h[3]["outcome"] == "miss"           # d2 起破止损 9.5 → 状态机 -10%
    assert by_h[3]["exit_reason"] == "stop_loss"
    assert by_h[10]["outcome"] == "miss"          # 状态机止损 -10% ≤ -2% (裸窗口 h10 为 -10% 同向)
    assert by_h[10]["return_pct"] == -10.0
    assert by_h[10]["exit_reason"] == "stop_loss"


# ================================================================
# API 端点 (契约 §4.6-5)
# ================================================================

def test_api_endpoints_shape(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.extensions import (
        BACKEND_EXTENSION_API_VERSION,
        BackendExtensionRegistrar,
    )

    days = _bdays(6)
    d0, d1, d2 = days[0], days[1], days[2]
    repo = PaperStubRepo({
        **_BENCH,
        "600001.SH": [_bar(d0, 10.2, 10.4, 10.1, 10.3), _bar(d1, 9.6, 10.2, 9.4, 10.0),
                      _bar(d2, 9.9, 10.0, 9.4, 9.6)],
    })
    reports = [
        _report("600001.SH", d0, "买入", {"ideal_buy": 10.0, "stop_loss": 9.5, "take_profit": 12.0}, "r_a"),
    ]
    _setup(tmp_path, monkeypatch, repo, reports, inception=d0)

    app = FastAPI()
    registrar = BackendExtensionRegistrar(
        dsa_paper.EXTENSION_ID, api_version=BACKEND_EXTENSION_API_VERSION,
    )
    dsa_paper.setup(registrar)
    for router in registrar.routers:
        app.include_router(router)
    client = TestClient(app)

    run_resp = client.post("/api/ext/dsa/paper/run")
    assert run_resp.status_code == 200
    body = run_resp.json()
    assert body["ok"] is True and body["episodes"] == 1 and body["ran_at"]

    overview = client.get("/api/ext/dsa/paper/overview").json()
    assert overview["state"]["initial_capital"] == 100000.0
    assert overview["state"]["max_slots"] == 10
    assert overview["state"]["inception_date"]
    assert overview["summary"]["closed"] == 1
    assert overview["summary"]["win_rate"] == 0.0      # -5% 一笔
    assert overview["equity"][0]["date"]

    trades = client.get("/api/ext/dsa/paper/trades").json()
    item = trades["items"][0]
    for key in ("symbol", "name", "entry_date", "entry_price", "shares",
                "exit_date", "exit_price", "exit_reason", "return_pct", "status"):
        assert key in item
    assert item["exit_reason"] == "stop_loss"

    outcomes = client.get("/api/ext/dsa/paper/outcomes").json()
    assert outcomes["stats"]["n"] >= 1
    assert all("horizon" in i for i in outcomes["items"])
