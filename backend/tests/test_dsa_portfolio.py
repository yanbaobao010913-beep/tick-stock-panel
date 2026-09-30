"""DSA 持仓账测试 (迁移计划 §4.4 P3)。

覆盖: FIFO 重放纯函数 (两买一卖精确数量/成本/浮盈、加权成本、同刻入账序
tiebreak、超卖中文 ValueError、费用不资本化、脏行跳过)、TradeStore (追加/列表/
损坏行跳过/删除原子重写/删除破坏 FIFO 拒绝且零落盘/导入事务幂等)、路由契约
(手动记账裸代码→后缀式存储、严格校验 400、超卖 422 不落盘、traded_at 各形态
含时区换算、trades 倒序同刻稳定/limit/规范化过滤、删除 404)、positions
(raw_close 优先/缺列退化 close/无数据 null 连带 totals null/精确市值浮盈/14 天
窗口)、mini sqlite 导入 (fee+tax 合并、symbol 三形态规范化、坏 side/坏数量/
无法定交易所/超卖行 skip 计数且后续行继续、库缺失/无表 400、非空 409 幂等)、
P2 切换 (load_positions 本账优先/桥回落 + watch-plan positions_source 集成:
先 bridge 后记账再查 watch-plan)、loader 注册。

测试数据一律 tmp_path 隔离 (settings.data_dir + store root + mini sqlite,
monkeypatch DSA_DB_PATH), 依赖全 mock (StubRepo), 绝不碰真实 data/ 与真实
DSA 旧库。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config as app_config
from app.custom import dsa_bridge, dsa_portfolio, dsa_watch
from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar
from app.extensions.loader import configure_backend_extensions
from app.market_time import cn_now
from app.services import watchlist

POSITIONS_URL = "/api/ext/dsa/portfolio/positions"
TRADES_URL = "/api/ext/dsa/portfolio/trades"
IMPORT_URL = "/api/ext/dsa/portfolio/import-from-dsa"
WATCH_PLAN_URL = "/api/ext/dsa/watch-plan"


# ================================================================
# fixtures 与桩
# ================================================================

@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """settings.data_dir 指向 tmp (流水账/自选/规则全部隔离)。"""
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """DSA 旧库路径指向 tmp 不存在文件 (任何用例都不真实读旧库); watch 模块态复位。"""
    monkeypatch.setattr(dsa_portfolio, "DSA_DB_PATH", tmp_path / "no-such-legacy.db")
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", tmp_path / "no-such-legacy.db")
    dsa_watch._RUNTIME.update({"repo": None, "data_dir": None, "engine": None})
    dsa_watch._ENGINE_FLUSH_PENDING = False
    dsa_watch._last_watch_sync = 0.0
    dsa_watch._last_premarket_date = None
    yield
    dsa_watch._RUNTIME.update({"repo": None, "data_dir": None, "engine": None})
    dsa_watch._ENGINE_FLUSH_PENDING = False
    dsa_watch._last_watch_sync = 0.0
    dsa_watch._last_premarket_date = None


class PortfolioStubRepo:
    """repo 桩: 可控名称映射与日K列 (raw_close 优先/缺列退化断言), 记录日K入参。"""

    def __init__(self, name_map: dict[str, str] | None = None, klines: dict | None = None):
        self.name_map = dict(name_map or {})
        self.klines = klines or {}  # symbol → pl.DataFrame (缺省空 → last_price null)
        self.calls: list[tuple] = []

    def resolve_asset_type(self, symbol: str) -> str:
        return "stock"

    def get_name_map(self, symbols=None) -> dict[str, str]:
        if symbols is None:
            return dict(self.name_map)
        wanted = set(symbols)
        return {s: n for s, n in self.name_map.items() if s in wanted}

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None) -> pl.DataFrame:
        self.calls.append((asset_type, symbol, start, end, columns))
        return self.klines.get(symbol, pl.DataFrame())


def _make_client(repo=None) -> TestClient:
    app = FastAPI()
    registrar = BackendExtensionRegistrar(
        dsa_portfolio.EXTENSION_ID, api_version=BACKEND_EXTENSION_API_VERSION,
    )
    dsa_portfolio.setup(registrar)
    for router in registrar.routers:
        app.include_router(router)
    if repo is not None:
        app.state.repo = repo
    return TestClient(app)


def _make_full_client(repo=None) -> TestClient:
    """watch + portfolio 双扩展 (P2 切换集成用)。"""
    app = FastAPI()
    for module in (dsa_watch, dsa_portfolio):
        registrar = BackendExtensionRegistrar(
            module.EXTENSION_ID, api_version=BACKEND_EXTENSION_API_VERSION,
        )
        module.setup(registrar)
        for router in registrar.routers:
            app.include_router(router)
    if repo is not None:
        app.state.repo = repo
    return TestClient(app)


def _trade(symbol, side, qty, price, traded_at, fee=None, note=None, tid=None):
    return {
        "id": tid or f"tr_test_{symbol}_{side}_{qty}",
        "symbol": symbol,
        "side": side,
        "quantity": qty,
        "price": price,
        "fee": fee,
        "traded_at": traded_at,
        "note": note,
    }


def _seed(trades) -> None:
    for trade in trades:
        dsa_portfolio._STORE.append_checked(trade)


# ================================================================
# mini DSA 旧库 (侦查的 portfolio_trades 15 列 / portfolio_positions 14 列结构)
# ================================================================

_TRADES_DDL = """
CREATE TABLE portfolio_trades (
    id INTEGER PRIMARY KEY,
    account_id INTEGER,
    trade_uid VARCHAR(128),
    symbol VARCHAR(16),
    market VARCHAR(8),
    currency VARCHAR(8),
    trade_date DATE,
    side VARCHAR(8),
    quantity FLOAT,
    price FLOAT,
    fee FLOAT,
    tax FLOAT,
    note VARCHAR(255),
    dedup_hash VARCHAR(64),
    created_at DATETIME
)
"""

# (id, symbol, trade_date, side, quantity, price, fee, tax, note)
_TRADE_ROWS = [
    (1, "600519", "2026-08-18", "buy", 100.0, 1500.0, 5.0, 1.0, None),
    (2, "SZ000636", "2026-08-19", "buy", 200.0, 58.5, 0.0, 0.0, "老库备注"),
    (3, "000636.SZ", "2026-08-20", "sell", 50.0, 60.0, None, None, None),
    (4, "600105", "2026-08-21", "hold", 100.0, 42.0, 0.0, 0.0, None),      # side 非法 → skip
    (5, "600519", "2026-08-22", "sell", 99999.0, 1600.0, 0.0, 0.0, None),   # 重放超卖 → skip
    (6, "123456", "2026-08-22", "buy", 100.0, 10.0, 0.0, 0.0, None),        # 无法定交易所 → skip
    (7, "600105", "2026-08-23", "buy", "abc", 42.0, 0.0, 0.0, None),        # TEXT 数量 → skip
    (8, "600519", "2026-08-24", "buy", 10.0, 1550.0, 0.0, 0.0, None),       # 超卖行之后仍继续导入
]

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


def _create_trades_db(path, rows=None):
    conn = sqlite3.connect(path)
    try:
        conn.execute(_TRADES_DDL)
        for rid, symbol, trade_date, side, qty, price, fee, tax, note in (
            rows if rows is not None else _TRADE_ROWS
        ):
            conn.execute(
                "INSERT INTO portfolio_trades (id, account_id, trade_uid, symbol, market,"
                " currency, trade_date, side, quantity, price, fee, tax, note, dedup_hash,"
                " created_at) VALUES (?, 2, ?, ?, 'cn', 'CNY', ?, ?, ?, ?, ?, ?, ?, 'h',"
                " '2026-08-18 10:00:00')",
                (rid, f"u{rid}", symbol, trade_date, side, qty, price, fee, tax, note),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def _create_positions_db(path):
    conn = sqlite3.connect(path)
    try:
        conn.execute(_POS_DDL)
        conn.execute(
            "INSERT INTO portfolio_positions (id, account_id, cost_method, symbol, market,"
            " currency, quantity, avg_cost, total_cost, last_price, market_value_base,"
            " unrealized_pnl_base, valuation_currency, updated_at)"
            " VALUES (1, 2, 'fifo', '600460', 'cn', 'CNY', 300.0, 34.648, 10394.4, 0, 0, 0,"
            " 'CNY', '2026-09-29')"
        )
        conn.commit()
    finally:
        conn.close()
    return path


# ================================================================
# A. FIFO 重放纯函数
# ================================================================

def test_replay_two_buys_one_sell_exact_fifo():
    """契约验收: 两买一卖 → 数量/加权成本/总成本精确; 卖价不进成本。"""
    trades = [
        _trade("600519.SH", "buy", 100, 10.0, "2026-08-18T10:00:00"),
        _trade("600519.SH", "buy", 200, 12.0, "2026-08-19T10:00:00"),
        _trade("600519.SH", "sell", 150, 13.5, "2026-08-20T10:00:00"),
    ]
    assert dsa_portfolio.replay_positions(trades)["600519.SH"] == {
        "quantity": 150.0, "avg_cost": 12.0, "total_cost": 1800.0,
    }


def test_replay_weighted_avg_cost():
    trades = [
        _trade("600460.SH", "buy", 300, 34.648, "2026-08-18T10:00:00"),
        _trade("600460.SH", "buy", 100, 35.0, "2026-08-19T10:00:00"),
    ]
    pos = dsa_portfolio.replay_positions(trades)["600460.SH"]
    assert pos["quantity"] == 400.0
    assert pos["avg_cost"] == pytest.approx(34.736)
    assert pos["total_cost"] == pytest.approx(13894.4)


def test_replay_same_timestamp_uses_entry_order():
    """同刻多笔按入账顺序消费 (先记的批次先被卖): 剩 50@20 而非 50@10。"""
    trades = [
        _trade("600519.SH", "buy", 100, 10.0, "2026-08-18T10:00:00"),
        _trade("600519.SH", "buy", 100, 20.0, "2026-08-18T10:00:00"),
        _trade("600519.SH", "sell", 150, 30.0, "2026-08-18T10:00:00"),
    ]
    pos = dsa_portfolio.replay_positions(trades)["600519.SH"]
    assert pos["quantity"] == 50.0
    assert pos["avg_cost"] == 20.0
    assert pos["total_cost"] == 1000.0


def test_replay_oversell_raises_chinese_value_error():
    trades = [
        _trade("600519.SH", "buy", 100, 10.0, "2026-08-18T10:00:00"),
        _trade("600519.SH", "sell", 200, 11.0, "2026-08-19T10:00:00"),
    ]
    with pytest.raises(ValueError, match="超卖") as exc_info:
        dsa_portfolio.replay_positions(trades)
    assert "600519.SH" in str(exc_info.value)  # 中文 ValueError 子类, 消息带 symbol


def test_replay_fee_not_capitalized():
    """费用只记流水: 批次成本 = price*quantity, fee 不摊入 avg_cost。"""
    trades = [_trade("600519.SH", "buy", 100, 10.0, "2026-08-18T10:00:00", fee=5.0)]
    pos = dsa_portfolio.replay_positions(trades)["600519.SH"]
    assert pos["avg_cost"] == 10.0 and pos["total_cost"] == 1000.0


def test_replay_skips_invalid_rows_with_warning(caplog):
    trades = [
        _trade("600519.SH", "buy", 100, 10.0, "2026-08-18T10:00:00"),
        _trade("600519.SH", "buy", "abc", 10.0, "2026-08-18T10:01:00"),   # 非数值数量
        _trade("600519.SH", "hold", 100, 10.0, "2026-08-18T10:02:00"),    # side 非法
        _trade("600519.SH", "buy", 100, 0, "2026-08-18T10:03:00"),        # 非正价格
    ]
    with caplog.at_level(logging.WARNING, logger="app.custom.dsa_portfolio"):
        pos = dsa_portfolio.replay_positions(trades)
    assert pos["600519.SH"]["quantity"] == 100.0
    assert len([r for r in caplog.records if "重放跳过" in r.message]) == 3


def test_replay_sell_to_zero_not_in_output():
    trades = [
        _trade("600519.SH", "buy", 100, 10.0, "2026-08-18T10:00:00"),
        _trade("600519.SH", "sell", 100, 12.0, "2026-08-19T10:00:00"),
    ]
    assert dsa_portfolio.replay_positions(trades) == {}
    assert dsa_portfolio.replay_positions([]) == {}


# ================================================================
# B. TradeStore
# ================================================================

def test_store_append_list_delete_roundtrip(tmp_path):
    store = dsa_portfolio.TradeStore(root=tmp_path)
    t1 = _trade("600519.SH", "buy", 100, 10.0, "2026-08-18T10:00:00", tid="t1")
    t2 = _trade("000636.SZ", "buy", 200, 5.0, "2026-08-19T10:00:00", tid="t2")
    assert store.append_checked(t1) == t1
    store.append_checked(t2)
    assert store.list_trades() == [t1, t2]  # 文件序=入账顺序
    lines = (tmp_path / "trades.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["id"] == "t1"
    assert store.delete("t1") == (True, [t2])  # 全量原子重写
    assert [t["id"] for t in store.list_trades()] == ["t2"]
    assert store.delete("no-such") == (False, None)


def test_store_corrupt_lines_skipped_with_warning(tmp_path, caplog):
    store = dsa_portfolio.TradeStore(root=tmp_path)
    t1 = _trade("600519.SH", "buy", 100, 10.0, "2026-08-18T10:00:00", tid="t1")
    path = tmp_path / "trades.jsonl"
    path.write_text(
        json.dumps(t1, ensure_ascii=False) + "\nnot-json\n[1, 2]\n\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="app.custom.dsa_portfolio"):
        assert store.list_trades() == [t1]  # 损坏行/非对象行跳过, 空行忽略
    assert len([r for r in caplog.records if "跳过" in r.message]) == 2


def test_store_delete_breaking_fifo_rejected_file_unchanged(tmp_path):
    store = dsa_portfolio.TradeStore(root=tmp_path)
    t1 = _trade("600519.SH", "buy", 100, 10.0, "2026-08-18T10:00:00", tid="t1")
    t2 = _trade("600519.SH", "sell", 100, 12.0, "2026-08-19T10:00:00", tid="t2")
    store.append_checked(t1)
    store.append_checked(t2)
    path = tmp_path / "trades.jsonl"
    before = path.read_bytes()
    with pytest.raises(dsa_portfolio.OversellError, match="超卖"):
        store.delete("t1")  # 删买入 → 剩余卖出无批次可消费
    assert path.read_bytes() == before  # fail-closed: 零落盘


def test_store_import_all_requires_empty_ledger(tmp_path):
    store = dsa_portfolio.TradeStore(root=tmp_path)
    t1 = _trade("600519.SH", "buy", 100, 10.0, "2026-08-18T10:00:00", tid="t1")
    t2 = _trade("000636.SZ", "buy", 200, 5.0, "2026-08-19T10:00:00", tid="t2")
    assert store.import_all([t1, t2]) == 2
    assert store.import_all([t1]) is None          # 非空拒绝 (幂等)
    assert [t["id"] for t in store.list_trades()] == ["t1", "t2"]


def test_store_import_empty_staged_does_not_touch_disk(tmp_path):
    store = dsa_portfolio.TradeStore(root=tmp_path)
    assert store.import_all([]) == 0
    assert not (tmp_path / "trades.jsonl").exists()  # 空导入不落盘, 账本仍为空可重试


# ================================================================
# C. 路由: 手动记账 / 流水列表 / 删除
# ================================================================

def test_post_trade_normalizes_bare_code_and_returns_full_trade(data_dir):
    repo = PortfolioStubRepo(name_map={"600519.SH": "贵州茅台"})
    client = _make_client(repo)
    resp = client.post(TRADES_URL, json={
        "symbol": "600519", "side": "buy", "quantity": 100, "price": 1500.0,
        "fee": 5.0, "traded_at": "2026-09-29T15:00:00", "note": "首笔",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"id", "symbol", "side", "quantity", "price", "fee", "traded_at", "note"}
    assert re.fullmatch(r"tr_\d{8}_\d{6}_[0-9a-f]{6}", body["id"])  # 手动记账 id 口径
    assert body["symbol"] == "600519.SH"  # 裸 6 位 → 维表规范化为后缀式存储
    assert (body["side"], body["quantity"], body["price"], body["fee"]) == ("buy", 100.0, 1500.0, 5.0)
    assert body["traded_at"] == "2026-09-29T15:00:00" and body["note"] == "首笔"
    listed = client.get(TRADES_URL).json()["items"]
    assert [t["id"] for t in listed] == [body["id"]]


@pytest.mark.parametrize(
    "payload",
    [
        {"symbol": "ABCDEF", "side": "buy", "quantity": 100, "price": 10.0},   # 非代码
        {"side": "buy", "quantity": 100, "price": 10.0},                        # 缺 symbol
        {"symbol": "60051", "side": "buy", "quantity": 100, "price": 10.0},     # 5 位
        {"symbol": "600519.SH", "side": "hold", "quantity": 100, "price": 10.0},
        {"symbol": "600519.SH", "side": "buy", "quantity": "100", "price": 10.0},  # 字符串数量
        {"symbol": "600519.SH", "side": "buy", "quantity": 0, "price": 10.0},
        {"symbol": "600519.SH", "side": "buy", "quantity": 100, "price": -1.0},
        {"symbol": "600519.SH", "side": "buy", "quantity": 100, "price": 10.0, "fee": -0.5},
        {"symbol": "600519.SH", "side": "buy", "quantity": 100, "price": 10.0,
         "traded_at": "08/18/2026"},
        {"symbol": "600519.SH", "side": "buy", "quantity": 100, "price": 10.0, "note": 3},
    ],
)
def test_post_trade_validation_400(data_dir, payload):
    client = _make_client(PortfolioStubRepo(name_map={"600519.SH": "贵州茅台"}))
    resp = client.post(TRADES_URL, json=payload)
    assert resp.status_code == 400
    assert "detail" in resp.json()


def test_post_trade_bad_symbol_detail_exact(data_dir):
    client = _make_client(PortfolioStubRepo(name_map={"600519.SH": "贵州茅台"}))
    resp = client.post(TRADES_URL, json={
        "symbol": "ABCDEF", "side": "buy", "quantity": 100, "price": 10.0,
    })
    assert resp.status_code == 400 and resp.json()["detail"] == "代码格式不对"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-08-18", "2026-08-18T00:00:00"),                # 纯日期可
        ("2026-08-18T10:30:00", "2026-08-18T10:30:00"),
        ("2026-08-17T20:00:00-04:00", "2026-08-18T08:00:00"),  # 带时区 → 北京墙钟换算
    ],
)
def test_post_trade_traded_at_variants(data_dir, raw, expected):
    client = _make_client(PortfolioStubRepo())
    resp = client.post(TRADES_URL, json={
        "symbol": "600519.SH", "side": "buy", "quantity": 100, "price": 10.0,
        "traded_at": raw,
    })
    assert resp.status_code == 200
    assert resp.json()["traded_at"] == expected


def test_post_trade_traded_at_defaults_to_cn_now(data_dir):
    client = _make_client(PortfolioStubRepo())
    resp = client.post(TRADES_URL, json={
        "symbol": "600519.SH", "side": "buy", "quantity": 100, "price": 10.0,
    })
    traded_at = resp.json()["traded_at"]
    parsed = datetime.fromisoformat(traded_at)
    assert len(traded_at) == 19  # naive ISO 秒精度
    delta = parsed - cn_now().replace(tzinfo=None, microsecond=0)
    assert abs(delta.total_seconds()) < 10


def test_post_trade_oversell_422_not_persisted(data_dir):
    _seed([_trade("600519.SH", "buy", 100, 10.0, "2026-08-18T10:00:00", tid="t1")])
    client = _make_client(PortfolioStubRepo())
    resp = client.post(TRADES_URL, json={
        "symbol": "600519.SH", "side": "sell", "quantity": 200, "price": 11.0,
        "traded_at": "2026-08-19T10:00:00",
    })
    assert resp.status_code == 422
    assert "超卖" in resp.json()["detail"]  # 中文说明
    assert [t["id"] for t in client.get(TRADES_URL).json()["items"]] == ["t1"]  # 卖单未落盘


def test_get_trades_desc_order_stable_and_filter_and_limit(data_dir):
    _seed([
        _trade("600519.SH", "buy", 100, 10.0, "2026-08-18T10:00:00", tid="t1"),
        _trade("000636.SZ", "buy", 200, 5.0, "2026-08-19T10:00:00", tid="t2"),
        _trade("600519.SH", "sell", 50, 11.0, "2026-08-18T11:00:00", tid="t3"),
    ])
    client = _make_client(PortfolioStubRepo())
    # traded_at 倒序; 同刻按入账序倒序 (后记的在前), 顺序确定
    assert [t["id"] for t in client.get(TRADES_URL).json()["items"]] == ["t2", "t3", "t1"]
    # symbol 过滤先规范化: 裸 600519 命中 600519.SH
    assert [t["id"] for t in client.get(TRADES_URL, params={"symbol": "600519"}).json()["items"]] == ["t3", "t1"]
    assert [t["id"] for t in client.get(TRADES_URL, params={"limit": 2}).json()["items"]] == ["t2", "t3"]
    assert client.get(TRADES_URL, params={"limit": 0}).status_code == 400


def test_get_trades_same_timestamp_deterministic(data_dir):
    _seed([
        _trade("600519.SH", "buy", 100, 10.0, "2026-08-18T10:00:00", tid="first"),
        _trade("000636.SZ", "buy", 200, 5.0, "2026-08-18T10:00:00", tid="second"),
    ])
    client = _make_client(PortfolioStubRepo())
    items = client.get(TRADES_URL).json()["items"]
    assert [t["id"] for t in items] == ["second", "first"]  # 同刻: 后入账在前 (稳定)


def test_delete_trade_ok_then_404(data_dir):
    _seed([
        _trade("600519.SH", "buy", 100, 10.0, "2026-08-18T10:00:00", tid="t1"),
        _trade("000636.SZ", "buy", 200, 5.0, "2026-08-19T10:00:00", tid="t2"),
    ])
    client = _make_client(PortfolioStubRepo())
    assert client.delete(f"{TRADES_URL}/t1").json() == {"ok": True}
    positions = client.get(POSITIONS_URL).json()
    assert [i["symbol"] for i in positions["items"]] == ["000636.SZ"]
    resp = client.delete(f"{TRADES_URL}/t1")  # 已删再删
    assert resp.status_code == 404 and resp.json()["detail"] == "流水不存在"


def test_delete_buy_breaking_fifo_422(data_dir):
    _seed([
        _trade("600519.SH", "buy", 100, 10.0, "2026-08-18T10:00:00", tid="t1"),
        _trade("600519.SH", "sell", 100, 12.0, "2026-08-19T10:00:00", tid="t2"),
    ])
    client = _make_client(PortfolioStubRepo())
    resp = client.delete(f"{TRADES_URL}/t1")
    assert resp.status_code == 422 and "超卖" in resp.json()["detail"]
    # 未删: GET 倒序 (t2 08-19 在前)
    assert [t["id"] for t in client.get(TRADES_URL).json()["items"]] == ["t2", "t1"]


# ================================================================
# D. 持仓视图
# ================================================================

def test_positions_exact_numbers_raw_close_priority_and_14d_window(data_dir):
    repo = PortfolioStubRepo(
        name_map={"600519.SH": "贵州茅台"},
        klines={"600519.SH": pl.DataFrame({
            "date": ["2026-09-28", "2026-09-29"],
            "raw_close": [14.0, 15.0],
            "close": [13.0, 14.5],
        })},
    )
    _seed([
        _trade("600519.SH", "buy", 100, 10.0, "2026-08-18T10:00:00", tid="t1"),
        _trade("600519.SH", "buy", 200, 12.0, "2026-08-19T10:00:00", tid="t2"),
        _trade("600519.SH", "sell", 150, 13.0, "2026-08-20T10:00:00", tid="t3"),
    ])
    body = _make_client(repo).get(POSITIONS_URL).json()
    assert body["items"] == [{
        "symbol": "600519.SH",
        "name": "贵州茅台",
        "quantity": 150.0,
        "avg_cost": 12.0,
        "total_cost": 1800.0,
        "last_price": 15.0,        # raw_close 优先 (不复权, 与成本同口径)
        "market_value": 2250.0,
        "unrealized_pnl": 450.0,
        "unrealized_pnl_pct": 25.0,
    }]
    assert body["totals"] == {"total_cost": 1800.0, "market_value": 2250.0, "unrealized_pnl": 450.0}
    asset_type, symbol, start, end, columns = repo.calls[0]
    assert (asset_type, symbol) == ("stock", "600519.SH")
    assert columns == ["date", "raw_close", "close"]
    assert (cn_now().date() - start).days == 14 and end == cn_now().date()  # 今天-14天, 今天


def test_positions_last_price_null_cascades_to_totals(data_dir):
    repo = PortfolioStubRepo()  # 无任何日K → last_price null, 不编造
    _seed([_trade("600519.SH", "buy", 100, 10.0, "2026-08-18T10:00:00", tid="t1")])
    body = _make_client(repo).get(POSITIONS_URL).json()
    item = body["items"][0]
    assert item["name"] is None
    assert item["last_price"] is None
    assert item["market_value"] is None
    assert item["unrealized_pnl"] is None
    assert item["unrealized_pnl_pct"] is None
    assert item["quantity"] == 100.0 and item["avg_cost"] == 10.0 and item["total_cost"] == 1000.0
    # 任一持仓缺价 → totals 市值/浮盈 null (部分和不冒充总数), 成本恒可加
    assert body["totals"] == {"total_cost": 1000.0, "market_value": None, "unrealized_pnl": None}


def test_positions_close_fallback_when_raw_close_missing(data_dir):
    """缺 raw_close 列/值 → 退化 close (前复权, docstring 已声明除权口径误差)。"""
    repo = PortfolioStubRepo(klines={
        "600519.SH": pl.DataFrame({"date": ["2026-09-29"], "close": [14.5]}),
        "000636.SZ": pl.DataFrame({"date": ["2026-09-29"], "raw_close": [None], "close": [13.2]}),
    })
    _seed([
        _trade("600519.SH", "buy", 100, 10.0, "2026-08-18T10:00:00", tid="t1"),
        _trade("000636.SZ", "buy", 100, 5.0, "2026-08-18T10:00:00", tid="t2"),
    ])
    items = _make_client(repo).get(POSITIONS_URL).json()["items"]
    by_sym = {i["symbol"]: i for i in items}
    assert by_sym["600519.SH"]["last_price"] == 14.5   # 缺列
    assert by_sym["000636.SZ"]["last_price"] == 13.2   # 列在但值 null


def test_positions_empty_ledger(data_dir):
    body = _make_client(PortfolioStubRepo()).get(POSITIONS_URL).json()
    assert body["items"] == []
    assert body["totals"]["total_cost"] == 0


def test_positions_corrupt_oversell_file_is_loud(data_dir):
    """手工改坏账本 (超卖) → 读路径 fail-closed 抛错, 不静默装空仓。"""
    path = data_dir / "user_data" / "dsa_portfolio" / "trades.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        _trade("600519.SH", "buy", 100, 10.0, "2026-08-18T10:00:00", tid="t1"),
        _trade("600519.SH", "sell", 200, 11.0, "2026-08-19T10:00:00", tid="t2"),
    ]
    path.write_text(
        "".join(json.dumps(t, ensure_ascii=False) + "\n" for t in rows), encoding="utf-8",
    )
    with pytest.raises(dsa_portfolio.OversellError):
        _make_client(PortfolioStubRepo()).get(POSITIONS_URL)


# ================================================================
# E. DSA 旧库一次性导入
# ================================================================

def test_import_from_dsa_happy(data_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(
        dsa_portfolio, "DSA_DB_PATH", _create_trades_db(tmp_path / "stock_analysis.db"),
    )
    repo = PortfolioStubRepo(name_map={"600519.SH": "贵州茅台", "000636.SZ": "风华高科"})
    client = _make_client(repo)
    resp = client.post(IMPORT_URL)
    assert resp.status_code == 200
    assert resp.json() == {"imported": 4, "skipped": 4}

    trades = {t["id"]: t for t in client.get(TRADES_URL).json()["items"]}
    assert set(trades) == {"dsa_1", "dsa_2", "dsa_3", "dsa_8"}  # 导入行 id 留溯源
    assert trades["dsa_1"]["symbol"] == "600519.SH"   # 裸码 → 后缀式
    assert trades["dsa_1"]["fee"] == 6.0              # fee + tax 合并 (总费用)
    assert trades["dsa_1"]["traded_at"] == "2026-08-18T00:00:00"  # trade_date 仅日期
    assert trades["dsa_2"]["symbol"] == "000636.SZ"   # SZ 前缀式 → 后缀式
    assert trades["dsa_2"]["note"] == "老库备注"
    assert trades["dsa_3"]["fee"] is None             # fee/tax 全缺 → null
    assert trades["dsa_8"]["quantity"] == 10.0        # 超卖行之后的行仍被导入

    body = client.get(POSITIONS_URL).json()
    by_sym = {i["symbol"]: i for i in body["items"]}
    assert by_sym["600519.SH"]["quantity"] == 110.0
    assert by_sym["600519.SH"]["avg_cost"] == pytest.approx(1504.545455)
    assert by_sym["000636.SZ"]["quantity"] == 150.0 and by_sym["000636.SZ"]["avg_cost"] == 58.5

    resp = client.post(IMPORT_URL)  # 幂等: 已有流水拒绝
    assert resp.status_code == 409 and "已有流水" in resp.json()["detail"]


def test_import_rejects_nonempty_ledger_409(data_dir, tmp_path, monkeypatch):
    _seed([_trade("600519.SH", "buy", 100, 10.0, "2026-08-18T10:00:00", tid="manual1")])
    monkeypatch.setattr(
        dsa_portfolio, "DSA_DB_PATH", _create_trades_db(tmp_path / "stock_analysis.db"),
    )
    client = _make_client(PortfolioStubRepo())
    resp = client.post(IMPORT_URL)
    assert resp.status_code == 409
    assert [t["id"] for t in client.get(TRADES_URL).json()["items"]] == ["manual1"]  # 未动


def test_import_db_missing_400(data_dir):
    client = _make_client(PortfolioStubRepo())
    resp = client.post(IMPORT_URL)  # autouse 已把 DSA_DB_PATH 指向不存在文件
    assert resp.status_code == 400 and resp.json()["detail"] == "DSA 旧库不可用"


def test_import_db_no_table_400(data_dir, tmp_path, monkeypatch):
    empty = tmp_path / "stock_analysis.db"
    conn = sqlite3.connect(empty)
    conn.execute("CREATE TABLE other(x)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(dsa_portfolio, "DSA_DB_PATH", empty)
    client = _make_client(PortfolioStubRepo())
    resp = client.post(IMPORT_URL)
    assert resp.status_code == 400 and resp.json()["detail"] == "DSA 旧库不可用"


def test_import_oversell_row_skipped_replay_continues(data_dir, tmp_path, monkeypatch):
    rows = [
        (1, "600519", "2026-08-18", "buy", 100.0, 10.0, 0.0, 0.0, None),
        (2, "600519", "2026-08-19", "sell", 50.0, 11.0, 0.0, 0.0, None),
        (3, "600519", "2026-08-20", "sell", 80.0, 11.0, 0.0, 0.0, None),  # 持 50 卖 80 → skip
        (4, "600519", "2026-08-21", "buy", 10.0, 12.0, 0.0, 0.0, None),
    ]
    monkeypatch.setattr(
        dsa_portfolio, "DSA_DB_PATH", _create_trades_db(tmp_path / "stock_analysis.db", rows),
    )
    client = _make_client(PortfolioStubRepo(name_map={"600519.SH": "贵州茅台"}))
    assert client.post(IMPORT_URL).json() == {"imported": 3, "skipped": 1}
    body = client.get(POSITIONS_URL).json()
    assert body["items"][0]["quantity"] == 60.0  # 100 - 50 + 10


# ================================================================
# F. P2 切换 (§4.4.3): 本账优先, 桥回落
# ================================================================

def test_load_positions_local_first_then_bridge_fallback(data_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", _create_positions_db(tmp_path / "legacy.db"))
    # 本账为空 → 回落 DSA 桥 (过渡期连续性)
    positions, source = dsa_watch.load_positions()
    assert source == "dsa_bridge"
    assert positions["600460"] == {"quantity": 300.0, "avg_cost": 34.648}
    # 本账非空 → 完全以本账为准 (桥持仓不再出现)
    _seed([_trade("600519.SH", "buy", 100, 1500.0, "2026-09-29T15:00:00", tid="t1")])
    positions, source = dsa_watch.load_positions()
    assert source == "dsa_portfolio"
    assert positions == {"600519": {"quantity": 100.0, "avg_cost": 1500.0}}


def test_watch_plan_switches_bridge_to_local_ledger(data_dir, monkeypatch):
    """集成: 先 bridge (watch-plan 用桥持仓) → 记账 → watch-plan 切本账持仓。"""
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", _create_positions_db(data_dir / "legacy.db"))
    watchlist.add_batch(["600460.SH"])
    client = _make_full_client(PortfolioStubRepo(name_map={"600519.SH": "贵州茅台"}))

    plan = client.get(WATCH_PLAN_URL).json()
    assert plan["positions_source"] == "dsa_bridge"
    by_sym = {i["symbol"]: i for i in plan["items"]}
    assert by_sym["600460.SH"]["holding"] is True and by_sym["600460.SH"]["quantity"] == 300.0
    assert "600519.SH" not in by_sym  # 未记账且不在自选: 盯盘集合尚无此票

    resp = client.post(TRADES_URL, json={
        "symbol": "600519", "side": "buy", "quantity": 100, "price": 1500.0,
        "traded_at": "2026-09-29T15:00:00",
    })
    assert resp.status_code == 200

    plan = client.get(WATCH_PLAN_URL).json()
    assert plan["positions_source"] == "dsa_portfolio"  # 如实上报实际来源
    by_sym = {i["symbol"]: i for i in plan["items"]}
    assert by_sym["600519.SH"]["holding"] is True
    assert by_sym["600519.SH"]["quantity"] == 100.0 and by_sym["600519.SH"]["avg_cost"] == 1500.0
    # 本账非空后完全以本账为准: 桥里的 600460 不再视为持仓
    assert by_sym["600460.SH"]["holding"] is False and by_sym["600460.SH"]["quantity"] is None


# ================================================================
# G. loader 集成
# ================================================================

def test_extension_registers_via_loader():
    app = FastAPI()
    registry, errors = configure_backend_extensions(app)
    assert "dsa.portfolio" in registry.extension_ids()
    assert errors == ()
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/api/ext/dsa/portfolio/positions" in paths
    assert "/api/ext/dsa/portfolio/trades" in paths
    assert "/api/ext/dsa/portfolio/import-from-dsa" in paths


def test_extension_id_and_version():
    assert re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", dsa_portfolio.EXTENSION_ID)
    assert dsa_portfolio.EXTENSION_API_VERSION == BACKEND_EXTENSION_API_VERSION
