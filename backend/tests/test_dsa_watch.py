"""DSA 次日盯盘测试 (迁移计划 §4 P2)。

覆盖: symbol 归一/持仓合并纯函数 (fifo/avg 整库二选一不混读不翻倍、跨账户加权、
SZ 前缀归一、脏行容错)、盯盘集合 (持仓+自选、持仓在前、canonical 后缀式)、
期望规则推导与 diff (含 no_points/no_report 保留既有规则、外来 DSA· 命名保护、
id 占用跳过、reduce 同价去重)、对账服务 (happy path/幂等/改价保 enabled/清仓/
移出自选且未持仓才删/人工规则不动/自选异常零写入/桥不可用中止)、引擎热加载
(无 engine 置 pending → 捕获即补 flush)、三个端点契约形状 (watch-plan 桥不可用
200 全空仓不 5xx / watch-sync 400 detail / premarket 纯读)、盘前自检四项独立
容错与窗口门控、任务收尾钩子全吞异常、30min 兜底门控、10 线程并发守恒、
loader 集成 (三新路径注册 + dsa_bridge health 契约原样)。

mini sqlite 按侦查的 14 列结构建在 tmp_path (放宽 NOT NULL 以注入脏行),
monkeypatch 注入 dsa_bridge.DSA_DB_PATH, 绝不碰真实旧库 (autouse fixture
兜底把桥路径指向 tmp 不存在文件)。
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import date, datetime

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config as app_config
from app.custom import dsa_analysis, dsa_bridge, dsa_watch
from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar
from app.extensions.loader import configure_backend_extensions
from app.market_time import cn_now
from app.strategy import monitor_rules
from app.strategy.monitor import MonitorRuleEngine

WATCH_PLAN_URL = "/api/ext/dsa/watch-plan"
WATCH_SYNC_URL = "/api/ext/dsa/watch-sync/run"
PREMARKET_URL = "/api/ext/dsa/premarket/status"


# ================================================================
# fixtures 与桩
# ================================================================

@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """settings.data_dir 指向 tmp (报告/自选/规则/盘前状态全部隔离)。"""
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _reset_watch_state(tmp_path, monkeypatch):
    """dsa_watch 模块态逐测试复位; 桥路径兜底指向 tmp 不存在文件 (个别用例
    未自带 legacy_db fixture 时也绝不碰真实旧库)。"""
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", tmp_path / "no-such-legacy.db")
    with dsa_analysis._task_lock:
        dsa_analysis._tasks.clear()
    dsa_analysis._last_run_date = None
    dsa_analysis._RUNTIME.update({"repo": None, "data_dir": None})
    _reset_watch_runtime(tmp_path)
    yield
    _reset_watch_runtime(tmp_path)
    with dsa_analysis._task_lock:
        dsa_analysis._tasks.clear()
    dsa_analysis._last_run_date = None
    dsa_analysis._RUNTIME.update({"repo": None, "data_dir": None})


def _reset_watch_runtime(tmp_path):
    dsa_watch._RUNTIME.update({"repo": None, "data_dir": None, "engine": None})
    dsa_watch._ENGINE_FLUSH_PENDING = False
    dsa_watch._last_watch_sync = 0.0
    dsa_watch._last_premarket_date = None


class WatchStubRepo:
    """最小 repo 桩: resolve_asset_type 可配置 (ETF 票断言), get_daily_asset 恒有上一交易日行。

    get_name_map() 不带参 (dsa_analysis._canonical_symbol 的维表反查入口) 时
    返回 588200.SH 样本, 验证裸码持仓经维表归一为后缀式; 未命中走代码段规则。
    """

    def __init__(self, asset_types: dict[str, str] | None = None, missing_kline: tuple = ()):
        self._types = asset_types or {}
        self._missing = set(missing_kline)

    def resolve_asset_type(self, symbol: str) -> str:
        return self._types.get(symbol, "stock")

    def get_name_map(self, symbols=None) -> dict[str, str]:
        if symbols is None:
            return {"588200.SH": "科创50ETF"}
        return {s: f"名称{s}" for s in symbols}

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None) -> pl.DataFrame:
        if symbol in self._missing:
            return pl.DataFrame()
        return pl.DataFrame({"date": [str(start)]})


def _make_client(repo=None) -> TestClient:
    app = FastAPI()
    registrar = BackendExtensionRegistrar(
        dsa_watch.EXTENSION_ID, api_version=BACKEND_EXTENSION_API_VERSION,
    )
    dsa_watch.setup(registrar)
    for router in registrar.routers:
        app.include_router(router)
    if repo is not None:
        app.state.repo = repo
    app.state.monitor_engine = MonitorRuleEngine()
    return TestClient(app)


# ================================================================
# mini 持仓库: 侦查 14 列结构 (放宽 NOT NULL 以注入脏行)
# ================================================================

_POS_DDL = """
CREATE TABLE portfolio_positions (
    id INTEGER PRIMARY KEY,
    account_id INTEGER,
    cost_method VARCHAR(8),
    symbol VARCHAR(16),
    market VARCHAR(8),
    currency VARCHAR(8),
    quantity FLOAT,
    avg_cost FLOAT,
    total_cost FLOAT,
    last_price FLOAT,
    market_value_base FLOAT,
    unrealized_pnl_base FLOAT,
    valuation_currency VARCHAR(8),
    updated_at DATETIME
)
"""

# (id, account_id, cost_method, symbol, quantity, avg_cost)
# 实测旧库形态: 账户 2 同持仓 avg+fifo 双记 (数值相同), 账户 3/4 仅 fifo 行——
# 整库只取 fifo 行才不丢账户 3/4 持仓 (取 avg 会只剩 002837 等)。
_POS_ROWS = [
    (1, 2, "avg", "002837", 200.0, 58.126),   # 同账户 avg+fifo 双记 → 只取 fifo, 200 不翻倍
    (2, 2, "fifo", "002837", 200.0, 58.126),
    (3, 2, "avg", "600460", 300.0, 34.648),   # avg 行被整库 fifo 选择排除
    (11, 2, "fifo", "600460", 300.0, 34.648),
    (4, 4, "fifo", "600460", 100.0, 35.0),    # 跨账户 → 400@加权 34.736
    (5, 4, "fifo", "SZ000636", 100.0, 58.5),  # SZ 前缀 → 000636
    (6, 4, "fifo", "588200", 8000.0, 1.19),   # 跨账户大额 → 12000@1.1967
    (7, 2, "fifo", "588200", 4000.0, 1.21),
    (8, 2, "avg", "300750", 0.0, 10.0),       # 清仓置 0 → 不出现
    (9, 2, "fifo", "601318", 500.0, None),    # NULL 成本脏行 → 跳过
    (10, 2, "fifo", "600105", "abc", 42.0),   # TEXT 数量脏行 → 跳过
]


def _create_positions_db(path, rows=None):
    conn = sqlite3.connect(path)
    try:
        conn.execute(_POS_DDL)
        for rid, acc, method, sym, qty, cost in (rows if rows is not None else _POS_ROWS):
            total = qty * cost if isinstance(qty, (int, float)) and isinstance(cost, (int, float)) else None
            conn.execute(
                "INSERT INTO portfolio_positions (id, account_id, cost_method, symbol, market,"
                " currency, quantity, avg_cost, total_cost, last_price, market_value_base,"
                " unrealized_pnl_base, valuation_currency, updated_at)"
                " VALUES (?, ?, ?, ?, 'cn', 'CNY', ?, ?, ?, 0, 0, 0, 'CNY', '2026-09-29 18:00:00')",
                (rid, acc, method, sym, qty, cost, total),
            )
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def legacy_db(tmp_path):
    return _create_positions_db(tmp_path / "stock_analysis.db")


@pytest.fixture
def client(data_dir, legacy_db, monkeypatch):
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", legacy_db)
    return _make_client(WatchStubRepo({"159530.SZ": "etf"}))


# ================================================================
# 报告/自选/规则 seed
# ================================================================

def _seed_watchlist(symbols):
    from app.services import watchlist as wl

    wl.add_batch(list(symbols))


def _seed_report(symbol, created_at="2026-09-29T18:00:00", points=None, risks=None):
    return dsa_analysis._STORE.save({
        "symbol": symbol,
        "created_at": created_at,
        "markdown": "# 报告",
        "operation_advice": "持有",
        "sentiment_score": 65,
        "points": points if points is not None else {
            "ideal_buy": None, "secondary_buy": None, "stop_loss": None, "take_profit": None,
        },
        "phase_decision": (
            {"risk_conditions": risks} if risks is not None
            else {"watch_conditions": ["放量"], "risk_conditions": []}
        ),
    })


_FULL_HOLDING_POINTS = {
    "ideal_buy": None, "secondary_buy": 33.5, "stop_loss": 31.0, "take_profit": 38.0,
}


def _seed_holding_scenario(data_dir):
    """自选 600460.SH(持仓,点位齐) + 159530.SZ(空仓,ideal_buy) + 600519.SH(无报告)。"""
    _seed_report("600460.SH", points=dict(_FULL_HOLDING_POINTS), risks=[
        {"kind": "reduce", "text": "跌破 34 减半", "price": 34.0},
        {"kind": "reduce", "text": "涨破 36 减半", "price": 36.0},
    ])
    _seed_report("159530.SZ", points={
        "ideal_buy": 1.05, "secondary_buy": None, "stop_loss": None, "take_profit": None,
    })
    _seed_watchlist(["600460.SH", "159530.SZ", "600519.SH"])


# ================================================================
# A. 纯函数
# ================================================================

def test_norm6_and_symkey():
    assert dsa_watch._norm6("600460.SH") == "600460"
    assert dsa_watch._norm6("SZ000636") == "000636"
    assert dsa_watch._norm6("000636") == "000636"
    assert dsa_watch._norm6(" sh600519 ") == "600519"
    # 码段 6 位数字即接受 (后缀剥除); 7 位/含字母/纯字母非法
    assert dsa_watch._norm6("600460.S") == "600460"
    for bad in ("ABC", "", None, "6004606", "12AB56", "SH", ".SH"):
        assert dsa_watch._norm6(bad) is None
    assert dsa_watch._symkey("600460.SH") == "600460sh"
    assert dsa_watch._rule_id("600460.SH", "stop_loss", 1) == "dsa_600460sh_stop_loss"
    assert dsa_watch._rule_id("600460.SH", "reduce", 2) == "dsa_600460sh_reduce_2"
    assert dsa_watch._rule_name("600460", "reduce", 2) == "DSA·600460·reduce·2"
    assert dsa_watch._rule_name("600460", "stop_loss", 1) == "DSA·600460·stop_loss"


def test_merge_position_rows_dedup_and_weighted_avg():
    cleaned = [r for r in (dsa_watch._clean_position_row({
        "account_id": acc, "symbol": sym, "cost_method": m, "quantity": q, "avg_cost": c,
    }) for acc, m, sym, q, c in [
        (2, "avg", "002837", 200.0, 58.126),
        (2, "fifo", "002837", 200.0, 58.126),
        (2, "avg", "600460", 300.0, 34.648),
        (2, "fifo", "600460", 300.0, 34.648),
        (4, "fifo", "600460", 100.0, 35.0),
        (4, "fifo", "SZ000636", 100.0, 58.5),
    ]) if r is not None]
    merged = dsa_watch.merge_position_rows(cleaned)
    assert merged["002837"] == {"quantity": 200.0, "avg_cost": 58.126}
    assert merged["600460"]["quantity"] == 400.0
    assert merged["600460"]["avg_cost"] == pytest.approx(34.736)
    assert merged["000636"] == {"quantity": 100.0, "avg_cost": 58.5}
    assert dsa_watch.merge_position_rows([]) == {}


def test_merge_position_rows_fifo_wins_over_avg_and_avg_only_fallback():
    """成本方法整库二选一: 任一行带 fifo 只取 fifo (值不同时不取 avg、不翻倍);
    全库无 fifo 行才取 avg。"""
    def row(acc, method, sym, q, c):
        return dsa_watch._clean_position_row({
            "account_id": acc, "symbol": sym, "cost_method": method,
            "quantity": q, "avg_cost": c,
        })

    both = [r for r in (row(2, "avg", "600519", 100.0, 1600.0),
                        row(2, "fifo", "600519", 100.0, 1650.0)) if r]
    assert dsa_watch.merge_position_rows(both)["600519"] == {
        "quantity": 100.0, "avg_cost": 1650.0,
    }
    avg_only = [r for r in (row(2, "avg", "600519", 100.0, 1600.0),) if r]
    assert dsa_watch.merge_position_rows(avg_only)["600519"] == {
        "quantity": 100.0, "avg_cost": 1600.0,
    }


def test_clean_position_row_rejects_dirty_rows():
    good = {"account_id": 2, "symbol": "600460", "cost_method": "avg", "quantity": 300.0, "avg_cost": 34.6}
    assert dsa_watch._clean_position_row(good)["sym6"] == "600460"
    assert dsa_watch._clean_position_row({**good, "avg_cost": None}) is None       # NULL 成本
    assert dsa_watch._clean_position_row({**good, "quantity": "abc"}) is None      # TEXT 数量
    assert dsa_watch._clean_position_row({**good, "quantity": 0}) is None          # 清仓行
    assert dsa_watch._clean_position_row({**good, "symbol": "ABCDEF"}) is None     # 码非法


def test_watch_symbols_positions_first_union_canonical_and_dedup():
    """盯盘集合 = 持仓 + 自选: 持仓在前 (sym6 序)、其余按自选序; SZ 前缀/裸码
    经 _canonical_symbol 归一为后缀点分式; 同票去重 (持仓形态优先保留)。"""
    positions = {"600460": {"quantity": 100.0, "avg_cost": 1.0}, "000636": {"quantity": 1.0, "avg_cost": 1.0}}
    entries = [{"symbol": "600519.SH"}, {"symbol": "SZ000636"}, {"symbol": "600460.SH"}]
    symbols = dsa_watch._watch_symbols(entries, positions, WatchStubRepo())
    assert symbols == ["000636.SZ", "600460.SH", "600519.SH"]
    # 自选为空 → 纯持仓集; 持仓为空 → 纯自选序
    assert dsa_watch._watch_symbols([], positions, WatchStubRepo()) == ["000636.SZ", "600460.SH"]
    assert dsa_watch._watch_symbols(entries, {}, WatchStubRepo()) == ["600519.SH", "000636.SZ", "600460.SH"]


def test_derive_expected_rules_holding_and_flat():
    report = {
        "points": dict(_FULL_HOLDING_POINTS),
        "phase_decision": {"risk_conditions": [
            {"kind": "reduce", "text": "a", "price": 36.0},
            {"kind": "reduce", "text": "b", "price": 34.0},
        ]},
    }
    rules = dsa_watch.derive_expected_rules("600460", "600460.SH", True, report)
    assert [(r.kind, r.rank, r.op, r.price, r.severity) for r in rules] == [
        ("stop_loss", 1, "<=", 31.0, "critical"),
        ("take_profit", 1, ">=", 38.0, "critical"),
        ("add", 1, "<=", 33.5, "warn"),
        ("reduce", 1, "<=", 34.0, "warn"),
        ("reduce", 2, "<=", 36.0, "warn"),
    ]


def test_derive_expected_rules_empty_points_and_flat_mode():
    holding_only_ideal = {"points": {"ideal_buy": 10.0, "secondary_buy": None,
                                     "stop_loss": None, "take_profit": None}, "phase_decision": None}
    assert dsa_watch.derive_expected_rules("x", "x.SH", True, holding_only_ideal) == []
    flat = {"points": {"ideal_buy": 10.0, "secondary_buy": None,
                       "stop_loss": 9.0, "take_profit": 12.0}, "phase_decision": None}
    rules = dsa_watch.derive_expected_rules("x", "x.SH", False, flat)
    assert [(r.kind, r.op, r.price, r.severity) for r in rules] == [("entry", "<=", 10.0, "info")]
    assert dsa_watch.derive_expected_rules("x", "x.SH", True, None) == []


def test_derive_expected_rules_reduce_without_price_or_kind_ignored():
    report = {"points": {"ideal_buy": None, "secondary_buy": None,
                         "stop_loss": None, "take_profit": None},
              "phase_decision": {"risk_conditions": [
                  {"kind": "reduce", "text": "无价", "price": None},
                  {"kind": "reduce", "text": "重复价", "price": 5.0},
                  {"kind": "reduce", "text": "重复价", "price": 5.0},
                  {"kind": "watch", "text": "非 reduce", "price": 4.0},
              ]}}
    rules = dsa_watch.derive_expected_rules("x", "x.SH", True, report)
    assert [(r.kind, r.rank, r.price) for r in rules] == [("reduce", 1, 5.0)]  # 同价去重+无价跳过


def test_count_empty_point_slots():
    full = {"points": dict(_FULL_HOLDING_POINTS), "phase_decision": {"risk_conditions": [
        {"kind": "reduce", "text": "a", "price": None},
        {"kind": "reduce", "text": "b", "price": 3.0},
    ]}}
    assert dsa_watch._count_empty_point_slots(True, full) == 1  # 仅 reduce 无价一条
    assert dsa_watch._count_empty_point_slots(
        True, {"points": {k: None for k in dsa_analysis._POINT_KEYS}, "phase_decision": None}) == 3
    assert dsa_watch._count_empty_point_slots(
        False, {"points": {"ideal_buy": None}, "phase_decision": None}) == 1
    assert dsa_watch._count_empty_point_slots(False, None) == 0


def _mk_expected(kind, rank, op, price, severity):
    return dsa_watch.ExpectedRule(kind, rank, op, price, severity)


def _mk_spec(sym6, rules, skip=False, holding=True, empty_slots=0, suffixed=None, created_at=None,
             preserve=frozenset()):
    return dsa_watch.ExpectedSpec(
        sym6=sym6, suffixed=suffixed or f"{sym6}.SH", holding=holding, rules=list(rules),
        skip_diff=skip, empty_slots=empty_slots, report_created_at=created_at,
        preserve_kinds=preserve,
    )


def test_empty_point_kinds_slots():
    full = {"points": dict(_FULL_HOLDING_POINTS), "phase_decision": None}
    assert dsa_watch._empty_point_kinds(True, full) == frozenset()
    assert dsa_watch._empty_point_kinds(
        True, {"points": {"ideal_buy": None, "secondary_buy": None,
                          "stop_loss": None, "take_profit": 38.0}, "phase_decision": None},
    ) == frozenset({"stop_loss", "add"})
    assert dsa_watch._empty_point_kinds(
        False, {"points": {"ideal_buy": None}, "phase_decision": None}) == frozenset({"entry"})
    assert dsa_watch._empty_point_kinds(False, {"points": {"ideal_buy": 1.0}}) == frozenset()
    assert dsa_watch._empty_point_kinds(True, None) == frozenset()


def test_diff_rules_partial_points_preserves_missing_slot_rules():
    """§4.3-1 槽位级保留: 缺位点位的既有规则不进 removes, 给出的点位照常比对;
    reduce 属列表语义仍按键级删除。"""
    spec = _mk_spec("600460", [_mk_expected("take_profit", 1, ">=", 38.0, "critical")],
                    preserve=frozenset({"stop_loss", "add"}))
    existing = [
        {"id": "a1", "name": "DSA·600460·stop_loss", "type": "price", "scope": "symbols",
         "symbols": ["600460.SH"],
         "conditions": [{"field": "close", "op": "<=", "value": 31.0}], "severity": "critical"},
        {"id": "a2", "name": "DSA·600460·take_profit", "type": "price", "scope": "symbols",
         "symbols": ["600460.SH"],
         "conditions": [{"field": "close", "op": ">=", "value": 30.0}], "severity": "critical"},
        {"id": "a3", "name": "DSA·600460·reduce·1", "type": "price", "scope": "symbols",
         "symbols": ["600460.SH"],
         "conditions": [{"field": "close", "op": "<=", "value": 34.0}], "severity": "warn"},
    ]
    diff = dsa_watch.diff_rules([spec], existing)
    assert diff.removes == ["a3"]                     # reduce 列表多余仍删
    assert [(cur["id"], er.price) for cur, er in diff.updates] == [("a2", 38.0)]
    assert diff.creates == [] and diff.orphan_deletes == []


def test_judge_sync_state_ignores_preserved_kinds():
    """被保留的缺位槽位不参与 synced 比较 (否则部分缺点位票永远显示待对账)。"""
    def parsed(kind, rank, op, price, severity, rid):
        return dsa_watch._ParsedRule("600460", kind, rank, op, price, severity, rid, True)

    actual = {
        ("stop_loss", 1): parsed("stop_loss", 1, "<=", 31.0, "critical", "r1"),
        ("take_profit", 1): parsed("take_profit", 1, ">=", 38.0, "critical", "r2"),
    }
    base = [_mk_expected("take_profit", 1, ">=", 38.0, "critical")]
    spec = _mk_spec("600460", base, preserve=frozenset({"stop_loss"}))
    assert dsa_watch._judge_sync_state(spec, actual, False) == "synced"
    # 不在保留集的多余规则 (如待删 reduce) 仍判 stale
    extra = dict(actual)
    extra[("reduce", 1)] = parsed("reduce", 1, "<=", 34.0, "warn", "r3")
    assert dsa_watch._judge_sync_state(spec, extra, False) == "stale"
    assert dsa_watch._judge_sync_state(_mk_spec("600460", base), actual, False) == "stale"


def test_diff_rules_creates_updates_removes_and_orphans():
    existing = [
        {"id": "dsa_600460sh_stop_loss", "name": "DSA·600460·stop_loss", "symbols": ["600460.SH"],
         "conditions": [{"field": "close", "op": "<=", "value": 30.0}], "severity": "critical"},
        {"id": "dsa_600460sh_reduce_2", "name": "DSA·600460·reduce·2", "symbols": ["600460.SH"],
         "conditions": [{"field": "close", "op": "<=", "value": 36.0}], "severity": "warn"},
        {"id": "dsa_000001sz_entry", "name": "DSA·000001·entry", "symbols": ["000001.SZ"],
         "conditions": [{"field": "close", "op": "<=", "value": 9.0}], "severity": "info"},
        {"id": "human_rule", "name": "我的手工规则", "symbols": ["600460.SH"],
         "conditions": [{"field": "close", "op": "<=", "value": 1.0}], "severity": "info"},
    ]
    spec = _mk_spec("600460", [
        _mk_expected("stop_loss", 1, "<=", 31.0, "critical"),   # 改价 30→31
        _mk_expected("reduce", 1, "<=", 34.0, "warn"),          # 新建
    ], empty_slots=2)
    spec2 = _mk_spec("000001", [], skip=True, holding=False)    # 无报告票: 保留
    diff = dsa_watch.diff_rules([spec, spec2], existing)
    assert [(s.sym6, e.kind, e.rank) for s, e in diff.creates] == [("600460", "reduce", 1)]
    assert [(r["id"], e.kind) for r, e in diff.updates] == [("dsa_600460sh_stop_loss", "stop_loss")]
    assert diff.removes == ["dsa_600460sh_reduce_2"]            # 多余 rank
    assert diff.orphan_deletes == []                            # 000001 仍在自选 (skip 保留)
    assert diff.skipped == 2


def test_diff_rules_removed_from_watchlist_becomes_orphan():
    spec = _mk_spec("000001", [_mk_expected("entry", 1, "<=", 9.0, "info")], holding=False)
    existing = [
        {"id": "dsa_600460sh_stop_loss", "name": "DSA·600460·stop_loss", "symbols": ["600460.SH"],
         "conditions": [{"field": "close", "op": "<=", "value": 30.0}], "severity": "critical"},
    ]
    diff = dsa_watch.diff_rules([spec], existing)
    assert diff.orphan_deletes == ["dsa_600460sh_stop_loss"]
    assert diff.removes == []


def test_diff_rules_price_tolerance_and_foreign_prefix_rule():
    existing = [
        # 1e-9 容差内的价差不算变更
        {"id": "dsa_600460sh_stop_loss", "name": "DSA·600460·stop_loss", "symbols": ["600460.SH"],
         "conditions": [{"field": "close", "op": "<=", "value": 31.0000000005}], "severity": "critical",
         "message": "DSA 止损提醒: 收盘价 ≤ 31.00"},
        # 外来 DSA· 命名 (kind 段非五类): 不托管不比对不删
        {"id": "foreign_dsa", "name": "DSA·手工·自定义", "symbols": ["600460.SH"],
         "conditions": [{"field": "close", "op": "<=", "value": 1.0}], "severity": "info"},
    ]
    spec = _mk_spec("600460", [_mk_expected("stop_loss", 1, "<=", 31.0, "critical")])
    diff = dsa_watch.diff_rules([spec], existing)
    assert diff.creates == [] and diff.updates == [] and diff.removes == []


def test_diff_rules_stale_message_flags_update():
    """message 参与内容比对: 老模板规则 (op/price/severity 全同但文案旧) 判为更新。"""
    existing = [
        {"id": "dsa_600460sh_stop_loss", "name": "DSA·600460·stop_loss", "symbols": ["600460.SH"],
         "conditions": [{"field": "close", "op": "<=", "value": 31.0}], "severity": "critical",
         "message": "DSA 盯盘规则(报告 2026-09-29)"},
    ]
    spec = _mk_spec("600460", [_mk_expected("stop_loss", 1, "<=", 31.0, "critical")])
    diff = dsa_watch.diff_rules([spec], existing)
    assert [cur["id"] for cur, _ in diff.updates] == ["dsa_600460sh_stop_loss"]
    # 对账应用后 message 重写为当前模板 (现价由引擎推送时追加, message 只含阈值)
    er = diff.updates[0][1]
    assert dsa_watch._rule_message(er) == "DSA 止损提醒: 收盘价 ≤ 31.00"


def test_parse_dsa_rule_variants():
    ok = {"id": "dsa_600460sh_reduce_2", "name": "DSA·600460·reduce·2", "symbols": ["600460.SH"],
          "conditions": [{"field": "close", "op": "<=", "value": 34.0}], "severity": "warn",
          "enabled": False}
    parsed = dsa_watch.parse_dsa_rule(ok)
    assert parsed.sym6 == "600460" and parsed.kind == "reduce" and parsed.rank == 2
    assert parsed.price == 34.0 and parsed.severity == "warn" and parsed.enabled is False
    plain = dict(ok, id="dsa_600460sh_stop_loss", name="DSA·600460·stop_loss",
                 conditions=[{"field": "close", "op": ">=", "value": 38.0}])
    p2 = dsa_watch.parse_dsa_rule(plain)
    assert p2.kind == "stop_loss" and p2.rank == 1 and p2.op == ">="
    for bad in [
        dict(ok, name="我的手工规则"),                       # 缺前缀
        dict(ok, name="DSA·600460·custom_kind"),             # kind 非五类
        dict(ok, name="DSA·600460·reduce·x"),                # rank 非数字
        dict(ok, name="DSA·600460·reduce·0"),                # rank <1
        dict(ok, name="DSA·600460·stop_loss·extra"),         # 5 段
        dict(ok, symbols=[]),                                # symbols 空
        dict(ok, symbols=["BAD"]),
        dict(ok, conditions=[{"field": "close", "op": "<=", "value": None}]),  # price 非数值
        dict(ok, conditions=[]),
    ]:
        assert dsa_watch.parse_dsa_rule(bad) is None, bad["name"]


def test_judge_sync_state_four_states():
    er = _mk_expected("stop_loss", 1, "<=", 31.0, "critical")
    synced_rule = dsa_watch.parse_dsa_rule({
        "id": "dsa_600460sh_stop_loss", "name": "DSA·600460·stop_loss", "symbols": ["600460.SH"],
        "conditions": [{"field": "close", "op": "<=", "value": 31.0}], "severity": "critical",
        "enabled": False,   # enabled 不参与比较: 手动停用仍 synced
    })
    spec = _mk_spec("600460", [er])
    assert dsa_watch._judge_sync_state(spec, {("stop_loss", 1): synced_rule}, False) == "synced"
    assert dsa_watch._judge_sync_state(spec, {}, False) == "stale"                # 缺条
    stale_price = dsa_watch.parse_dsa_rule({
        "id": "x", "name": "DSA·600460·stop_loss", "symbols": ["600460.SH"],
        "conditions": [{"field": "close", "op": "<=", "value": 30.0}], "severity": "critical",
    })
    assert dsa_watch._judge_sync_state(spec, {("stop_loss", 1): stale_price}, False) == "stale"
    assert dsa_watch._judge_sync_state(spec, {}, True) == "stale"                 # 规则读失败降级
    assert dsa_watch._judge_sync_state(
        _mk_spec("600460", [], created_at="2026-09-29T18:00:00"), {}, False) == "no_points"
    assert dsa_watch._judge_sync_state(_mk_spec("600460", []), {}, False) == "no_report"


def test_decide_premarket_run_gating():
    now = datetime(2026, 9, 30, 9, 15)
    assert dsa_watch._decide_premarket_run(now=now, last_date=None, trading_day=True) == (True, "trigger")
    assert dsa_watch._decide_premarket_run(
        now=now, last_date=date(2026, 9, 30), trading_day=True) == (False, "already_ran")
    assert dsa_watch._decide_premarket_run(
        now=datetime(2026, 9, 30, 9, 9), last_date=None, trading_day=True) == (False, "out_of_window")
    assert dsa_watch._decide_premarket_run(
        now=datetime(2026, 9, 30, 11, 31), last_date=None, trading_day=True) == (False, "out_of_window")
    assert dsa_watch._decide_premarket_run(now=now, last_date=None, trading_day=False) == (False, "not_trading_day")
    # 未知 → 退化周一~五: 2026-09-30 是周三
    assert dsa_watch._decide_premarket_run(now=now, last_date=None, trading_day=None)[0] is True
    assert dsa_watch._decide_premarket_run(
        now=datetime(2026, 10, 3, 9, 15), last_date=None, trading_day=None) == (False, "not_trading_day")


# ================================================================
# B. 对账服务 (tmp_path 全真文件)
# ================================================================

def test_reconcile_happy_path_creates_rules(data_dir, legacy_db, monkeypatch):
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", legacy_db)
    _seed_holding_scenario(data_dir)
    repo = WatchStubRepo({"159530.SZ": "etf"})
    engine = MonitorRuleEngine()
    result = dsa_watch.run_reconcile(data_dir, repo, engine)
    assert result == {"created": 16, "updated": 0, "removed": 0, "skipped": 0}

    rule = monitor_rules.load_one(data_dir, "dsa_600460sh_stop_loss")
    assert rule["type"] == "price" and rule["scope"] == "symbols"
    assert rule["symbols"] == ["600460.SH"] and rule["cooldown_seconds"] == 86400
    assert rule["severity"] == "critical" and rule["enabled"] is True
    assert rule["conditions"] == [{"field": "close", "op": "<=", "value": 31.0}]
    assert rule["name"] == "DSA·600460·stop_loss" and rule["asset_type"] == "stock"
    assert rule["message"] == "DSA 止损提醒: 收盘价 ≤ 31.00"
    assert monitor_rules.load_one(data_dir, "dsa_600460sh_take_profit")["conditions"][0]["op"] == ">="
    add_rule = monitor_rules.load_one(data_dir, "dsa_600460sh_add")
    assert add_rule["severity"] == "warn" and add_rule["conditions"][0]["value"] == 33.5
    r1 = monitor_rules.load_one(data_dir, "dsa_600460sh_reduce_1")
    r2 = monitor_rules.load_one(data_dir, "dsa_600460sh_reduce_2")
    assert (r1["name"], r1["conditions"][0]["value"]) == ("DSA·600460·reduce·1", 34.0)
    assert (r2["name"], r2["conditions"][0]["value"]) == ("DSA·600460·reduce·2", 36.0)
    entry = monitor_rules.load_one(data_dir, "dsa_159530sz_entry")
    assert entry["asset_type"] == "etf" and entry["severity"] == "info"   # ETF asset_type 由 repo 纠正
    assert entry["symbols"] == ["159530.SZ"]

    assert set(engine._rules) == {
        "dsa_600460sh_stop_loss", "dsa_600460sh_take_profit", "dsa_600460sh_add",
        "dsa_600460sh_reduce_1", "dsa_600460sh_reduce_2", "dsa_159530sz_entry",
        "dsa_600460sh_near_stop_loss", "dsa_600460sh_mid_stop_loss",
        "dsa_600460sh_near_take_profit", "dsa_600460sh_mid_take_profit",
        "dsa_600460sh_near_add", "dsa_600460sh_mid_add",
        "dsa_600460sh_near_reduce_1", "dsa_600460sh_mid_reduce_1",
        "dsa_600460sh_near_reduce_2", "dsa_600460sh_mid_reduce_2",
    }


def test_reconcile_idempotent_second_run(data_dir, legacy_db, monkeypatch):
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", legacy_db)
    _seed_holding_scenario(data_dir)
    repo = WatchStubRepo()
    assert dsa_watch.run_reconcile(data_dir, repo, None)["created"] == 16
    assert dsa_watch.run_reconcile(data_dir, repo, None) == {
        "created": 0, "updated": 0, "removed": 0, "skipped": 0,
    }


def test_reconcile_price_update_keeps_enabled_and_created_at(data_dir, legacy_db, monkeypatch):
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", legacy_db)
    _seed_holding_scenario(data_dir)
    monitor_rules.save_one(data_dir, monitor_rules.normalize({
        "id": "dsa_600460sh_stop_loss", "name": "DSA·600460·stop_loss", "type": "price",
        "scope": "symbols", "symbols": ["600460.SH"],
        "conditions": [{"field": "close", "op": "<=", "value": 30.0}],
        "severity": "warn", "enabled": False, "created_at": "2026-09-28T18:00:00",
    }))
    result = dsa_watch.run_reconcile(data_dir, WatchStubRepo(), None)
    assert result["created"] == 15 and result["updated"] == 1 and result["removed"] == 0
    rule = monitor_rules.load_one(data_dir, "dsa_600460sh_stop_loss")
    assert rule["conditions"][0]["value"] == 31.0 and rule["severity"] == "critical"
    assert rule["enabled"] is False                       # 手动停用不被对账复活
    assert rule["created_at"] == "2026-09-28T18:00:00"    # 保留原 created_at


def test_reconcile_flat_position_switches_to_entry(data_dir, tmp_path, monkeypatch):
    # 只含 002837 的持仓库 → 600460 已清仓 (在自选、无持仓)
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH",
                        _create_positions_db(tmp_path / "flat.db", rows=_POS_ROWS[:2]))
    _seed_report("600460.SH", points={
        "ideal_buy": 1650.0, "secondary_buy": None, "stop_loss": None, "take_profit": None,
    })
    _seed_watchlist(["600460.SH"])
    monitor_rules.save_one(data_dir, monitor_rules.normalize({
        "id": "dsa_600460sh_stop_loss", "name": "DSA·600460·stop_loss", "type": "price",
        "scope": "symbols", "symbols": ["600460.SH"],
        "conditions": [{"field": "close", "op": "<=", "value": 1600.0}], "severity": "critical",
    }))
    result = dsa_watch.run_reconcile(data_dir, WatchStubRepo(), None)
    assert result["created"] == 1 and result["removed"] == 1
    assert monitor_rules.load_one(data_dir, "dsa_600460sh_stop_loss") is None
    entry = monitor_rules.load_one(data_dir, "dsa_600460sh_entry")
    assert entry["conditions"] == [{"field": "close", "op": "<=", "value": 1650.0}]


def test_reconcile_removed_from_watchlist_unheld_deletes_but_held_keeps(data_dir, legacy_db, monkeypatch):
    """移出自选: 未持仓票 (000300) 规则全删; 仍持仓票 (600460, 盯盘集合=持仓+自选)
    规则保留; 人工规则与 DSA 规则混放时逐字节未动。"""
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", legacy_db)
    for rid, name, sym in (
        ("dsa_000300sh_entry", "DSA·000300·entry", "000300.SH"),          # 移出自选且未持仓 → 删
        ("dsa_600460sh_stop_loss", "DSA·600460·stop_loss", "600460.SH"),  # 移出自选但仍持仓 → 留
    ):
        monitor_rules.save_one(data_dir, monitor_rules.normalize({
            "id": rid, "name": name, "type": "price", "scope": "symbols",
            "symbols": [sym],
            "conditions": [{"field": "close", "op": "<=", "value": 1.0}], "severity": "info",
        }))
    monitor_rules.save_one(data_dir, monitor_rules.normalize({
        "id": "human_rule", "name": "我的手工规则", "type": "price", "scope": "symbols",
        "symbols": ["000300.SH"],
        "conditions": [{"field": "close", "op": "<=", "value": 9.9}], "severity": "warn",
    }))
    human_path = data_dir / "user_data" / "monitor_rules" / "human_rule.json"
    human_before = human_path.read_bytes()
    _seed_watchlist(["600460.SH"])  # 000300 移出自选
    result = dsa_watch.run_reconcile(data_dir, WatchStubRepo(), None)
    assert result["removed"] == 1
    rules_dir = data_dir / "user_data" / "monitor_rules"
    assert not (rules_dir / "dsa_000300sh_entry.json").exists()
    assert monitor_rules.load_one(data_dir, "dsa_600460sh_stop_loss") is not None
    assert human_path.read_bytes() == human_before  # 人工规则逐字节未变


def test_reconcile_empty_watchlist_keeps_held_deletes_unheld(data_dir, legacy_db, monkeypatch):
    """清空自选: 未持仓票的 DSA 规则删除; 持仓票 (集合=持仓+自选) 规则保留。"""
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", legacy_db)
    _seed_watchlist([])
    for rid, name, sym in (
        ("dsa_000300sh_entry", "DSA·000300·entry", "000300.SH"),
        ("dsa_600460sh_stop_loss", "DSA·600460·stop_loss", "600460.SH"),
    ):
        monitor_rules.save_one(data_dir, monitor_rules.normalize({
            "id": rid, "name": name, "type": "price", "scope": "symbols",
            "symbols": [sym],
            "conditions": [{"field": "close", "op": "<=", "value": 1.0}], "severity": "critical",
        }))
    result = dsa_watch.run_reconcile(data_dir, WatchStubRepo(), None)
    assert result["removed"] == 1
    assert monitor_rules.load_one(data_dir, "dsa_000300sh_entry") is None
    assert monitor_rules.load_one(data_dir, "dsa_600460sh_stop_loss") is not None


def test_reconcile_watchlist_error_aborts_without_write(data_dir, legacy_db, monkeypatch):
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", legacy_db)
    _seed_watchlist(["600460.SH"])
    monitor_rules.save_one(data_dir, monitor_rules.normalize({
        "id": "dsa_600460sh_stop_loss", "name": "DSA·600460·stop_loss", "type": "price",
        "scope": "symbols", "symbols": ["600460.SH"],
        "conditions": [{"field": "close", "op": "<=", "value": 1.0}], "severity": "critical",
    }))
    rule_path = data_dir / "user_data" / "monitor_rules" / "dsa_600460sh_stop_loss.json"
    before = rule_path.read_bytes()

    def boom():
        raise RuntimeError("自选读取炸了")

    monkeypatch.setattr(dsa_watch.watchlist, "list_symbols", boom)
    with pytest.raises(dsa_watch.SyncError, match="自选读取失败"):
        dsa_watch.run_reconcile(data_dir, WatchStubRepo(), None)
    assert rule_path.read_bytes() == before  # 异常≠空自选: 中止防误删


def test_reconcile_bridge_unavailable_aborts(data_dir, monkeypatch):
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", data_dir / "no-such.db")
    _seed_watchlist(["600460.SH"])
    monitor_rules.save_one(data_dir, monitor_rules.normalize({
        "id": "dsa_600460sh_stop_loss", "name": "DSA·600460·stop_loss", "type": "price",
        "scope": "symbols", "symbols": ["600460.SH"],
        "conditions": [{"field": "close", "op": "<=", "value": 1.0}], "severity": "critical",
    }))
    rule_path = data_dir / "user_data" / "monitor_rules" / "dsa_600460sh_stop_loss.json"
    before = rule_path.read_bytes()
    with pytest.raises(dsa_watch.SyncError, match="DSA 桥不可用"):
        dsa_watch.run_reconcile(data_dir, WatchStubRepo(), None)
    assert rule_path.read_bytes() == before  # 零写入


def test_reconcile_no_points_and_no_report_keep_rules(data_dir, legacy_db, monkeypatch):
    """评审固化: 持仓票报告缺点位/缺报告 → 保留既有规则 (防 LLM 闪失清线)。"""
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", legacy_db)
    _seed_watchlist(["600460.SH"])
    monitor_rules.save_one(data_dir, monitor_rules.normalize({
        "id": "dsa_600460sh_stop_loss", "name": "DSA·600460·stop_loss", "type": "price",
        "scope": "symbols", "symbols": ["600460.SH"],
        "conditions": [{"field": "close", "op": "<=", "value": 31.0}], "severity": "critical",
    }))
    # 场景 1: 报告存在但 points 全空 (LLM 围栏解析失败的落盘形态)
    _seed_report("600460.SH", points={k: None for k in dsa_analysis._POINT_KEYS})
    result = dsa_watch.run_reconcile(data_dir, WatchStubRepo(), None)
    assert result["removed"] == 0 and result["skipped"] == 3
    assert monitor_rules.load_one(data_dir, "dsa_600460sh_stop_loss") is not None
    # 场景 2: 报告全删
    report = dsa_analysis._STORE.list_reports("600460.SH")[0]
    dsa_analysis._STORE.delete(report["id"])
    result = dsa_watch.run_reconcile(data_dir, WatchStubRepo(), None)
    assert result["removed"] == 0
    assert monitor_rules.load_one(data_dir, "dsa_600460sh_stop_loss") is not None


def test_reconcile_partial_points_preserves_missing_slot_rules(data_dir, legacy_db, monkeypatch):
    """§4.3-1 拍板落地 (槽位级, 2026-09-30): 持仓票报告部分缺点位 → 缺位槽位
    的既有规则原样保留 (防单次 LLM 缺点位清掉在用止损线), 给出的点位照常改价;
    reduce 走列表语义, 多余删除。"""
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", legacy_db)
    _seed_holding_scenario(data_dir)
    assert dsa_watch.run_reconcile(data_dir, WatchStubRepo(), None)["created"] == 16
    # 新报告只给 take_profit 且改价; stop_loss/secondary_buy 缺位, 无 reduce 条目
    _seed_report("600460.SH", created_at="2026-09-30T18:00:00", points={
        "ideal_buy": None, "secondary_buy": None, "stop_loss": None, "take_profit": 39.5,
    })
    result = dsa_watch.run_reconcile(data_dir, WatchStubRepo(), None)
    assert result["updated"] == 3 and result["removed"] == 6   # take_profit 改价带阶梯 2 条; reduce 主 2 + 阶梯 4 列表多余
    assert result["created"] == 0 and result["skipped"] == 2   # stop_loss/add 两个空槽位
    keep = monitor_rules.load_one(data_dir, "dsa_600460sh_stop_loss")
    assert keep is not None and keep["conditions"][0]["value"] == 31.0  # 原价原样保留
    assert monitor_rules.load_one(data_dir, "dsa_600460sh_near_stop_loss") is not None  # 阶梯随主规则保留
    assert monitor_rules.load_one(data_dir, "dsa_600460sh_add") is not None
    tp = monitor_rules.load_one(data_dir, "dsa_600460sh_take_profit")
    assert tp["conditions"][0]["value"] == 39.5                 # 给出的照常改价
    assert monitor_rules.load_one(data_dir, "dsa_600460sh_reduce_1") is None
    assert monitor_rules.load_one(data_dir, "dsa_600460sh_near_reduce_1") is None


def test_reconcile_flat_missing_ideal_buy_keeps_entry_rule(data_dir, legacy_db, monkeypatch):
    """槽位级保留的空仓侧: 新报告 ideal_buy 缺位 → 既有 entry 规则保留。"""
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", legacy_db)
    _seed_holding_scenario(data_dir)
    dsa_watch.run_reconcile(data_dir, WatchStubRepo(), None)
    _seed_report("159530.SZ", created_at="2026-09-30T18:00:00", points={
        "ideal_buy": None, "secondary_buy": None, "stop_loss": None, "take_profit": None,
    })
    result = dsa_watch.run_reconcile(data_dir, WatchStubRepo(), None)
    assert result["removed"] == 0 and result["skipped"] == 1
    entry = monitor_rules.load_one(data_dir, "dsa_159530sz_entry")
    assert entry is not None and entry["conditions"][0]["value"] == 1.05


def test_reconcile_skips_create_when_id_occupied_by_renamed_rule(data_dir, legacy_db, monkeypatch):
    """评审固化: 确定性 id 被挤出托管集 (用户改名) → 跳过创建, 不静默覆写。"""
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", legacy_db)
    _seed_holding_scenario(data_dir)
    monitor_rules.save_one(data_dir, monitor_rules.normalize({
        "id": "dsa_600460sh_stop_loss", "name": "我的自定义止损", "type": "price",
        "scope": "symbols", "symbols": ["600460.SH"],
        "conditions": [{"field": "close", "op": "<=", "value": 1.0}],
        "severity": "warn", "enabled": False,
    }))
    result = dsa_watch.run_reconcile(data_dir, WatchStubRepo(), None)
    assert result["created"] == 15 and result["skipped"] == 1
    rule = monitor_rules.load_one(data_dir, "dsa_600460sh_stop_loss")
    assert rule["name"] == "我的自定义止损" and rule["enabled"] is False  # 原样未动


def test_reconcile_index_watchlist_symbol_skips_rules(data_dir, legacy_db, monkeypatch):
    """评审固化: resolve_asset_type=index 的自选 (撞码指数) 不建规则。"""
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", legacy_db)
    _seed_report("000001.SH", points={
        "ideal_buy": None, "secondary_buy": None, "stop_loss": 3100.0, "take_profit": None,
    })
    _seed_watchlist(["000001.SH"])
    result = dsa_watch.run_reconcile(
        data_dir, WatchStubRepo({"000001.SH": "index"}), None)
    assert result == {"created": 0, "updated": 0, "removed": 0, "skipped": 0}
    assert monitor_rules.load_one(data_dir, "dsa_000001sh_stop_loss") is None


# ================================================================
# C. 持仓桥
# ================================================================

def test_load_merged_positions_end_to_end(legacy_db, monkeypatch):
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", legacy_db)
    merged = dsa_watch.load_merged_positions()
    assert set(merged) == {"002837", "600460", "000636", "588200"}  # 脏行/清仓行剔除
    assert merged["002837"] == {"quantity": 200.0, "avg_cost": 58.126}
    assert merged["600460"] == {"quantity": 400.0, "avg_cost": pytest.approx(34.736)}
    assert merged["000636"] == {"quantity": 100.0, "avg_cost": 58.5}
    assert merged["588200"]["quantity"] == 12000.0
    assert merged["588200"]["avg_cost"] == pytest.approx(1.1967)


def test_load_merged_positions_missing_db(data_dir, monkeypatch):
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", data_dir / "no-such.db")
    with pytest.raises(dsa_watch.BridgeUnavailableError):
        dsa_watch.load_merged_positions()


def test_load_merged_positions_no_table(tmp_path, monkeypatch):
    empty = tmp_path / "empty.db"
    conn = sqlite3.connect(empty)
    conn.execute("CREATE TABLE other(x)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", empty)
    with pytest.raises(dsa_watch.BridgeUnavailableError):
        dsa_watch.load_merged_positions()


def test_positions_access_is_read_only(legacy_db, monkeypatch):
    """全程零写入断言: 读取前后库文件逐字节一致。"""
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", legacy_db)
    before = legacy_db.read_bytes()
    dsa_watch.load_merged_positions()
    assert legacy_db.read_bytes() == before


# ================================================================
# D. 端点
# ================================================================

def test_watch_plan_full_shape(client):
    _seed_holding_scenario(_data_dir_of(client))
    body = client.get(WATCH_PLAN_URL)
    assert body.status_code == 200
    body = body.json()
    datetime.fromisoformat(body["generated_at"])  # JS Date 可解析的 ISO
    assert body["positions_source"] == "dsa_bridge"
    items = body["items"]
    # 盯盘集合 = 持仓 + 自选 (契约 §4.1): 持仓在前 (sym6 序), 其余按自选序
    # (add_batch 后插前排 → 自选文件序为 600519, 159530, 600460, 600460 已在持仓去重);
    # symbol 统一后缀点分式 (裸码 588200 经维表归一, SZ000636 前缀式归 000636.SZ)
    assert [i["symbol"] for i in items] == [
        "000636.SZ", "002837.SZ", "588200.SH", "600460.SH", "600519.SH", "159530.SZ",
    ]
    assert len({i["symbol"] for i in items}) == 6
    # 持仓 + 自选: 不在自选的持仓股也有条目 (holding=True, 无报告)
    unwatched = items[:3]
    assert all(i["holding"] is True and i["report"] is None and i["sync_state"] == "no_report"
               for i in unwatched)
    assert [i["symbol"] for i in unwatched] == ["000636.SZ", "002837.SZ", "588200.SH"]
    holding = next(i for i in items if i["symbol"] == "600460.SH")
    assert holding["holding"] is True
    assert holding["quantity"] == 400.0 and holding["avg_cost"] == pytest.approx(34.736)
    assert holding["name"] == "名称600460.SH"
    assert set(holding["report"]) == {"id", "created_at", "operation_advice",
                                      "sentiment_score", "points", "phase_decision"}
    assert holding["report"]["id"] == dsa_analysis._STORE.list_reports("600460.SH")[0]["id"]
    assert holding["sync_state"] == "stale"  # 尚未对账
    flat = next(i for i in items if i["symbol"] == "159530.SZ")
    assert flat["holding"] is False and flat["quantity"] is None and flat["avg_cost"] is None
    assert flat["report"]["points"]["ideal_buy"] == 1.05
    assert flat["sync_state"] == "stale"
    no_report = next(i for i in items if i["symbol"] == "600519.SH")
    assert no_report["report"] is None and no_report["sync_state"] == "no_report"
    for item in items:
        for rule in item["rules"]:
            assert rule["kind"] in ("stop_loss", "take_profit", "add", "reduce", "entry")
            assert isinstance(rule["price"], (int, float)) and rule["price"] is not None
            assert rule["severity"] in ("info", "warn", "critical")
    assert all(i["sync_state"] in ("synced", "stale", "no_points", "no_report") for i in items)


def _data_dir_of(client):
    from app.config import settings

    return settings.data_dir


def test_watch_plan_after_sync_is_synced(client):
    _seed_holding_scenario(_data_dir_of(client))
    assert client.post(WATCH_SYNC_URL, content="{}").status_code == 200
    items = client.get(WATCH_PLAN_URL).json()["items"]
    by_sym = {i["symbol"]: i for i in items}
    assert by_sym["600460.SH"]["sync_state"] == "synced"
    # rules[] 按规则文件序 (glob 字典序), 顺序契约未规定, 用多重集断言
    assert sorted(r["kind"] for r in by_sym["600460.SH"]["rules"]) == [
        "add", "mid_add", "mid_reduce", "mid_reduce", "mid_stop_loss", "mid_take_profit",
        "near_add", "near_reduce", "near_reduce", "near_stop_loss", "near_take_profit",
        "reduce", "reduce", "stop_loss", "take_profit",
    ]
    assert by_sym["159530.SZ"]["sync_state"] == "synced"    # 空仓 entry
    assert by_sym["600519.SH"]["sync_state"] == "no_report"  # 无报告
    assert all(by_sym[s]["sync_state"] == "no_report"
               for s in ("000636.SZ", "002837.SZ", "588200.SH"))  # 持仓无报告


def test_watch_plan_bridge_unavailable_200_all_flat(data_dir, monkeypatch):
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", data_dir / "no-such.db")
    _seed_watchlist(["600460.SH"])
    client = _make_client(WatchStubRepo())
    r = client.get(WATCH_PLAN_URL)
    assert r.status_code == 200  # 契约硬要求: 不 5xx
    body = r.json()
    assert body["positions_source"] == "dsa_bridge"
    assert all(
        i["holding"] is False and i["quantity"] is None and i["avg_cost"] is None
        for i in body["items"]
    )


def test_watch_plan_rules_read_failure_degrades(data_dir, legacy_db, monkeypatch):
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", legacy_db)
    _seed_holding_scenario(data_dir)

    def boom(_data_dir):
        raise RuntimeError("规则目录炸了")

    monkeypatch.setattr(monitor_rules, "load_all", boom)
    client = _make_client(WatchStubRepo())
    body = client.get(WATCH_PLAN_URL).json()
    by_sym = {i["symbol"]: i for i in body["items"]}
    assert by_sym["600460.SH"]["rules"] == [] and by_sym["600460.SH"]["sync_state"] == "stale"
    assert by_sym["159530.SZ"]["sync_state"] == "stale"   # 有点位但状态未知 → 保守 stale
    assert by_sym["600519.SH"]["sync_state"] == "no_report"  # 无报告不受影响


def test_watch_plan_watchlist_error_falls_back_to_positions(data_dir, legacy_db, monkeypatch):
    """自选读取失败 ≠ 空清单: 持仓仍可达 → 集合退化为持仓, 绝不 5xx。"""
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", legacy_db)

    def boom():
        raise RuntimeError("自选炸了")

    monkeypatch.setattr(dsa_watch.watchlist, "list_symbols", boom)
    client = _make_client(WatchStubRepo())
    assert client.get(WATCH_PLAN_URL).status_code == 200
    items = client.get(WATCH_PLAN_URL).json()["items"]
    assert [i["symbol"] for i in items] == ["000636.SZ", "002837.SZ", "588200.SH", "600460.SH"]
    assert all(i["holding"] is True for i in items)


def test_watch_sync_run_endpoint_counts(client):
    _seed_holding_scenario(_data_dir_of(client))
    r = client.post(WATCH_SYNC_URL, content="{}", headers={"Content-Type": "application/json"})
    assert r.status_code == 200
    assert r.json() == {"created": 16, "updated": 0, "removed": 0, "skipped": 0}
    again = client.post(WATCH_SYNC_URL, content="{}")
    assert again.json() == {"created": 0, "updated": 0, "removed": 0, "skipped": 0}


def test_watch_sync_bridge_unavailable_400_detail(data_dir, monkeypatch):
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", data_dir / "no-such.db")
    _seed_watchlist(["600460.SH"])
    client = _make_client(WatchStubRepo())
    r = client.post(WATCH_SYNC_URL, content="{}")
    assert r.status_code == 400  # 非 5xx
    assert "DSA 桥不可用" in r.json()["detail"]


def test_watch_plan_multi_account_merge_endpoint(data_dir, legacy_db, monkeypatch):
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", legacy_db)
    _seed_watchlist(["588200.SH"])
    client = _make_client(WatchStubRepo())
    items = client.get(WATCH_PLAN_URL).json()["items"]
    item = next(i for i in items if i["symbol"] == "588200.SH")  # 裸码经维表归一为后缀式
    assert item["holding"] is True
    assert item["quantity"] == 12000.0 and item["avg_cost"] == pytest.approx(1.1967)


# ================================================================
# D/E. premarket 状态与检查项
# ================================================================

def test_premarket_status_never_run(data_dir):
    client = _make_client(None)
    body = client.get(PREMARKET_URL).json()
    assert body == {"date": cn_now().date().isoformat(), "ran_at": None, "checks": []}


def test_premarket_status_after_run_roundtrip(data_dir):
    today = cn_now().date().isoformat()
    dsa_watch._write_premarket_state(data_dir, {
        "date": today, "ran_at": "2026-09-30T09:15:00",
        "checks": [{"name": "数据源", "status": "ok", "detail": "x"}],
    })
    client = _make_client(None)
    body = client.get(PREMARKET_URL).json()
    assert body == {"date": today, "ran_at": "2026-09-30T09:15:00",
                    "checks": [{"name": "数据源", "status": "ok", "detail": "x"}]}


def test_premarket_status_stale_date_returns_today_empty(data_dir):
    dsa_watch._write_premarket_state(data_dir, {
        "date": "2026-09-01", "ran_at": "2026-09-01T09:15:00",
        "checks": [{"name": "数据源", "status": "ok", "detail": "旧"}],
    })
    client = _make_client(None)
    assert client.get(PREMARKET_URL).json() == {
        "date": cn_now().date().isoformat(), "ran_at": None, "checks": [],
    }


def test_premarket_status_corrupt_file_no_crash(data_dir):
    p = dsa_watch._premarket_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    client = _make_client(None)
    assert client.get(PREMARKET_URL).status_code == 200
    assert client.get(PREMARKET_URL).json()["checks"] == []


def _patch_premarket_env(monkeypatch, data_dir, legacy_db, *, kline_boom=False):
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", legacy_db)
    monkeypatch.setattr(dsa_watch, "is_trading_day", lambda now=None: True)
    if kline_boom:
        def boom(repo, symbol):
            raise RuntimeError("K线探针炸了")
        monkeypatch.setattr(dsa_analysis, "_load_kline", boom)
    else:
        # 数据源探针桩: 恒返回最新日期行 (stub get_daily_asset 的 start 是远期历史,
        # 真实 _load_kline 会拿到旧日期行误报 warn)
        monkeypatch.setattr(dsa_analysis, "_load_kline",
                            lambda repo, symbol: pl.DataFrame({"date": [cn_now().date().isoformat()]}))
    return WatchStubRepo()


def test_premarket_checks_four_items_ok(data_dir, legacy_db, monkeypatch):
    _patch_premarket_env(monkeypatch, data_dir, legacy_db)
    _seed_holding_scenario(data_dir)
    repo = WatchStubRepo({"159530.SZ": "etf"})
    dsa_watch.run_reconcile(data_dir, repo, None)  # 先同步 → 无 stale
    checks = dsa_watch.run_premarket_checks(data_dir, repo)
    by_name = {c["name"]: c for c in checks}
    assert set(by_name) == {"数据源", "自选K线覆盖", "同步规则启用", "DSA桥可用"}
    assert all(c["status"] == "ok" for c in checks), checks
    assert "4 只持仓" in by_name["DSA桥可用"]["detail"]
    assert "同步规则 16 条" in by_name["同步规则启用"]["detail"]


def test_premarket_checks_isolated_failure(data_dir, legacy_db, monkeypatch):
    _patch_premarket_env(monkeypatch, data_dir, legacy_db, kline_boom=True)
    _seed_holding_scenario(data_dir)
    checks = dsa_watch.run_premarket_checks(data_dir, WatchStubRepo())
    by_name = {c["name"]: c for c in checks}
    assert by_name["数据源"]["status"] == "fail"
    assert "K线探针炸了" in by_name["数据源"]["detail"]
    assert by_name["自选K线覆盖"]["status"] == "ok"
    assert by_name["DSA桥可用"]["status"] == "ok"


def test_premarket_bridge_fail_detail(data_dir, monkeypatch):
    monkeypatch.setattr(dsa_watch, "is_trading_day", lambda now=None: True)
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", data_dir / "no-such.db")
    _seed_watchlist(["600460.SH"])
    checks = dsa_watch.run_premarket_checks(data_dir, WatchStubRepo())
    bridge = next(c for c in checks if c["name"] == "DSA桥可用")
    assert bridge["status"] == "fail"
    assert "旧库不可用" in bridge["detail"] and "空仓" in bridge["detail"]


def test_premarket_bridge_unused_when_portfolio_supplies(data_dir, monkeypatch):
    """本账供仓 → 旧桥不参与持仓, 不探旧库也不报异常 (云端容器无 DSA 部署的常态)。"""
    monkeypatch.setattr(dsa_watch, "is_trading_day", lambda now=None: True)
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", data_dir / "no-such.db")
    _seed_watchlist(["600460.SH"])
    trade = {"id": "t1", "symbol": "600460.SH", "side": "buy", "quantity": 100.0,
             "price": 31.5, "fee": None, "traded_at": "2026-08-25T09:30:00", "note": None}
    path = data_dir / "user_data" / "dsa_portfolio" / "trades.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(trade, ensure_ascii=False) + "\n", encoding="utf-8")

    def _never_probe():
        raise AssertionError("本账供仓时不应再开旧库")

    monkeypatch.setattr(dsa_watch, "_fetch_position_rows", _never_probe)
    checks = dsa_watch.run_premarket_checks(data_dir, WatchStubRepo())
    bridge = next(c for c in checks if c["name"] == "DSA桥可用")
    assert bridge["status"] == "ok" and "本账" in bridge["detail"]


def test_premarket_stale_rules_warn(data_dir, legacy_db, monkeypatch):
    _patch_premarket_env(monkeypatch, data_dir, legacy_db)
    _seed_holding_scenario(data_dir)  # 有报告有点位但不建规则 → stale
    checks = dsa_watch.run_premarket_checks(data_dir, WatchStubRepo())
    sync_check = next(c for c in checks if c["name"] == "同步规则启用")
    assert sync_check["status"] == "warn" and "待同步" in sync_check["detail"]


def test_premarket_kline_coverage_warn(data_dir, legacy_db, monkeypatch):
    _patch_premarket_env(monkeypatch, data_dir, legacy_db)
    _seed_watchlist(["600460.SH"])
    repo = WatchStubRepo(missing_kline=("600460.SH",))
    checks = dsa_watch.run_premarket_checks(data_dir, repo)
    coverage = next(c for c in checks if c["name"] == "自选K线覆盖")
    assert coverage["status"] == "warn" and "缺" in coverage["detail"]


# ================================================================
# F. 挂接与并发
# ================================================================

def test_on_task_done_reconciles(data_dir, legacy_db, monkeypatch):
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", legacy_db)
    _seed_holding_scenario(data_dir)
    dsa_watch.on_task_done({"_repo": WatchStubRepo(), "_data_dir": data_dir})
    assert monitor_rules.load_one(data_dir, "dsa_600460sh_stop_loss") is not None


def test_on_task_done_swallows_reconcile_errors(data_dir, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("对账炸了")

    monkeypatch.setattr(dsa_watch, "run_reconcile", boom)
    dsa_watch.on_task_done({"_repo": None, "_data_dir": None})  # 不外抛, 任务终态不受影响


def test_on_task_done_hook_wired_into_process_task(data_dir, legacy_db, monkeypatch):
    """挂接点 1 集成: _process_task 终态后真实触发一次对账 (mini 库端到端)。"""
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", legacy_db)
    _seed_holding_scenario(data_dir)
    with dsa_analysis._task_lock:
        dsa_analysis._tasks["t_hook"] = {
            "task_id": "t_hook", "status": "running", "mode": "full", "total": 0,
            "_repo": WatchStubRepo(), "_data_dir": data_dir, "items": [],
        }
    dsa_analysis._process_task("t_hook")
    with dsa_analysis._task_lock:
        assert dsa_analysis._tasks["t_hook"]["status"] == "done"
    assert monitor_rules.load_one(data_dir, "dsa_600460sh_stop_loss") is not None


def test_scheduler_tick_fallback_gating(data_dir, monkeypatch):
    calls = []

    def fake_reconcile(*args, **kwargs):
        calls.append(1)
        return {"created": 0, "updated": 0, "removed": 0, "skipped": 0}

    monkeypatch.setattr(dsa_watch, "run_reconcile", fake_reconcile)
    monkeypatch.setattr(dsa_watch, "is_trading_day", lambda now=None: False)
    dsa_watch.scheduler_tick(now=datetime(2026, 9, 30, 8, 0))
    assert len(calls) == 1                                   # 首拍跑兜底
    dsa_watch.scheduler_tick(now=datetime(2026, 9, 30, 8, 5))
    assert len(calls) == 1                                   # 30min 内二拍跳过
    dsa_watch._last_watch_sync -= dsa_watch._WATCH_FALLBACK_INTERVAL_SECONDS + 1.0
    dsa_watch.scheduler_tick(now=datetime(2026, 9, 30, 8, 10))
    assert len(calls) == 2                                   # 超门控再跑


def test_scheduler_tick_premarket_once_per_day(data_dir, legacy_db, monkeypatch):
    _patch_premarket_env(monkeypatch, data_dir, legacy_db)
    runs = []
    monkeypatch.setattr(dsa_watch, "run_premarket_checks",
                        lambda dd, repo: runs.append(1) or [{"name": "数据源", "status": "ok", "detail": ""}])
    dsa_watch.scheduler_tick(now=datetime(2026, 9, 30, 9, 15))
    assert len(runs) == 1
    state = dsa_watch._read_premarket_state(data_dir)
    assert state["date"] == "2026-09-30" and state["ran_at"]
    dsa_watch.scheduler_tick(now=datetime(2026, 9, 30, 10, 0))
    assert len(runs) == 1                                    # 当日只一次
    assert dsa_watch._last_premarket_date == date(2026, 9, 30)


def test_scheduler_tick_premarket_out_of_window_or_holiday(data_dir, legacy_db, monkeypatch):
    _patch_premarket_env(monkeypatch, data_dir, legacy_db)
    runs = []
    monkeypatch.setattr(dsa_watch, "run_premarket_checks",
                        lambda dd, repo: runs.append(1) or [])
    dsa_watch.scheduler_tick(now=datetime(2026, 9, 30, 8, 0))    # 窗口外
    dsa_watch.scheduler_tick(now=datetime(2026, 9, 30, 12, 0))   # 窗口外
    assert runs == [] and dsa_watch._last_premarket_date is None
    assert not dsa_watch._premarket_path(data_dir).exists()
    monkeypatch.setattr(dsa_watch, "is_trading_day", lambda now=None: False)
    dsa_watch.scheduler_tick(now=datetime(2026, 9, 30, 9, 15))   # 非交易日
    assert runs == []


def test_reconcile_concurrent_threads_conserved(data_dir, legacy_db, monkeypatch):
    """10 线程同调 run_reconcile: _SYNC_LOCK 串行 → created 守恒、无交叉写。"""
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", legacy_db)
    _seed_holding_scenario(data_dir)
    results: list[dict] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(10)

    def worker():
        barrier.wait()
        try:
            results.append(dsa_watch.run_reconcile(data_dir, WatchStubRepo(), None))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sum(r["created"] for r in results) == 16  # 只有持锁的首轮建齐
    assert sum(r["removed"] for r in results) == 0
    assert monitor_rules.load_one(data_dir, "dsa_600460sh_reduce_2") is not None


def test_capture_engine_none_keeps_existing():
    dsa_watch._RUNTIME["engine"] = "existing"
    dsa_watch.capture_engine(None)
    assert dsa_watch._RUNTIME["engine"] == "existing"
    dsa_watch.capture_engine("new")
    assert dsa_watch._RUNTIME["engine"] == "new"


def test_reconcile_without_engine_pends_then_capture_flushes(data_dir, legacy_db, monkeypatch):
    """契约: 对账写盘后引擎内存必须刷新或挂起待捕获后 flush。后台路径 (任务收尾/
    调度兜底) 常无 engine 引用 → 置 pending; 任一路由随后捕获 engine 即补 flush。"""
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", legacy_db)
    _seed_holding_scenario(data_dir)
    assert dsa_watch.run_reconcile(data_dir, WatchStubRepo(), None)["created"] == 16
    assert dsa_watch._ENGINE_FLUSH_PENDING is True        # 有变更且 engine 未捕获 → 挂起
    engine = MonitorRuleEngine()
    dsa_watch.capture_engine(engine)
    assert dsa_watch._ENGINE_FLUSH_PENDING is False       # 捕获即补 flush 并清位
    assert set(engine._rules) == {
        "dsa_600460sh_stop_loss", "dsa_600460sh_take_profit", "dsa_600460sh_add",
        "dsa_600460sh_reduce_1", "dsa_600460sh_reduce_2", "dsa_159530sz_entry",
        "dsa_600460sh_near_stop_loss", "dsa_600460sh_mid_stop_loss",
        "dsa_600460sh_near_take_profit", "dsa_600460sh_mid_take_profit",
        "dsa_600460sh_near_add", "dsa_600460sh_mid_add",
        "dsa_600460sh_near_reduce_1", "dsa_600460sh_mid_reduce_1",
        "dsa_600460sh_near_reduce_2", "dsa_600460sh_mid_reduce_2",
    }


def test_capture_engine_without_pending_does_not_reload():
    engine = MonitorRuleEngine()
    dsa_watch.capture_engine(engine)  # 无挂起变更: 只存引用, 不重载规则
    assert engine._rules == {}
    assert dsa_watch._RUNTIME["engine"] is engine


def test_reconcile_with_engine_flushes_immediately_and_clears_pending(data_dir, legacy_db, monkeypatch):
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", legacy_db)
    _seed_holding_scenario(data_dir)
    engine = MonitorRuleEngine()
    dsa_watch._ENGINE_FLUSH_PENDING = True  # 模拟历史挂起
    assert dsa_watch.run_reconcile(data_dir, WatchStubRepo(), engine)["created"] == 16
    assert dsa_watch._ENGINE_FLUSH_PENDING is False       # 本轮 flush 成功即清位
    assert "dsa_600460sh_stop_loss" in engine._rules


def test_endpoint_capture_runtime_and_engine(client):
    """handler 首行捕获: 请求后 _RUNTIME 带上 repo/data_dir/engine。"""
    _seed_holding_scenario(_data_dir_of(client))
    engine = client.app.state.monitor_engine
    client.get(WATCH_PLAN_URL)
    assert dsa_watch._RUNTIME["engine"] is engine
    assert dsa_watch._RUNTIME["repo"] is client.app.state.repo
    assert dsa_watch._RUNTIME["data_dir"] == _data_dir_of(client)


# ================================================================
# G. loader 集成
# ================================================================

def test_extension_registers_via_loader_and_keeps_bridge_paths():
    app = FastAPI()
    registry, errors = configure_backend_extensions(app)
    assert "dsa.watch" in registry.extension_ids()
    assert errors == ()
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/api/ext/dsa/watch-plan" in paths
    assert "/api/ext/dsa/watch-sync/run" in paths
    assert "/api/ext/dsa/premarket/status" in paths
    assert "/api/ext/dsa/health" in paths  # dsa_bridge 契约原样保留


def test_extension_id_matches_registry_rules():
    assert re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", dsa_watch.EXTENSION_ID)
    assert dsa_watch.EXTENSION_API_VERSION == BACKEND_EXTENSION_API_VERSION
