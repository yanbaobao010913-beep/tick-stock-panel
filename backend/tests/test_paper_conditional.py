"""模拟盘 conditional 条件触发单测试 (迁移计划 §4.7-1 B1 核心口径)。

覆盖验收矩阵: 触发成交 (盘中按现价) / 跳空按开盘价 / 盘中穿越按触发价 /
未触发作废 (expire=day) / GTC 持续 / 下单校验 / 费用滑点 T+1 涨跌停校验不被
绕过 (复用 _fill_order) / 排队重试保持触发语义 / API 契约透传。

价格全程不复权 raw 价; 撮合复用既有 _fill_order, 账务一致性口径与
test_paper_trading.py 相同。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.paper import router as paper_router
from app.market_time import CN_TZ
from app.strategy import paper
from app.tickflow.repository import DataStore, KlineRepository

SYM = "600519.SH"
DAY = date(2026, 9, 24)
NEXT = DAY + timedelta(days=1)


def _write_daily(tmp_path: Path, rows: list[tuple[date, float, float, float, float]]) -> None:
    """写 kline_daily 分区: [(day, open, high, low, close)] (不复权 raw 价)。"""
    repo = KlineRepository(DataStore(tmp_path))
    repo.append_daily(pl.DataFrame(
        {
            "symbol": [SYM] * len(rows),
            "date": [r[0] for r in rows],
            "open": [r[1] for r in rows],
            "high": [r[2] for r in rows],
            "low": [r[3] for r in rows],
            "close": [r[4] for r in rows],
            "volume": [10000.0] * len(rows),
            "amount": [r[4] * 10000.0 for r in rows],
        },
    ))


def _freeze(monkeypatch, day: date, hhmm: time) -> None:
    """把域模块的北京时钟钉在某交易日某时刻 (created_at 由此产生)。"""
    now = datetime.combine(day, hhmm, tzinfo=CN_TZ)
    monkeypatch.setattr(paper, "cn_now", lambda: now)
    monkeypatch.setattr(paper, "cn_today", lambda: now.date())


def _cond_order(tmp_path: Path, **overrides) -> tuple[dict, str | None]:
    params = {
        "symbol": SYM, "side": "buy", "qty": 100,
        "order_type": "conditional", "trigger_price": 10.0, "trigger_op": "<=",
        "ref_price": 10.0,
    }
    params.update(overrides)
    return paper.create_order(
        tmp_path, params.pop("symbol"), params.pop("side"),
        order_type=params.pop("order_type"),
        trigger_price=params.pop("trigger_price", None),
        trigger_op=params.pop("trigger_op", None),
        expire=params.pop("expire", "day"),
        **params,
    )


# ── 下单校验 ────────────────────────────────────────────
def test_create_conditional_requires_account(tmp_path):
    with pytest.raises(ValueError, match="尚未创建"):
        _cond_order(tmp_path)


def test_create_conditional_validation(tmp_path):
    paper.create_account(tmp_path, 1_000_000)
    _, err = _cond_order(tmp_path, trigger_price=None)
    assert "trigger_price" in err
    _, err = _cond_order(tmp_path, trigger_price=0)
    assert "trigger_price" in err
    _, err = _cond_order(tmp_path, trigger_op="<")
    assert "trigger_op" in err
    _, err = _cond_order(tmp_path, expire="week")
    assert "expire" in err
    # 触发字段用在非 conditional 单上 → 拒 (不静默忽略错误语义)
    _, err = _cond_order(tmp_path, order_type="market", trigger_price=10.0, trigger_op="<=")
    assert "仅 conditional" in err

    order, err = _cond_order(tmp_path)
    assert err is None
    assert order["order_type"] == "conditional"
    assert order["trigger_price"] == pytest.approx(10.0)
    assert order["trigger_op"] == "<="
    assert order["expire"] == "day"


def test_create_conditional_amount_mode_sizes_at_trigger(tmp_path):
    paper.create_account(tmp_path, 1_000_000)
    order, err = _cond_order(tmp_path, qty=None, amount=20_000.0, trigger_price=100.0, ref_price=100.0)
    assert err is None
    assert order["qty"] == 200  # 2万 / 触发价 100 → 200 股 (整百)


# ── 盘中触发 (成交价 = 现价) ────────────────────────────
def test_conditional_intraday_fill_at_snapshot_price(tmp_path, monkeypatch):
    _freeze(monkeypatch, DAY, time(10, 0))
    _write_daily(tmp_path, [(DAY - timedelta(days=1), 10.0, 10.2, 9.9, 10.0)])
    paper.create_account(tmp_path, 1_000_000)
    order, _ = _cond_order(tmp_path, qty=1000, trigger_price=10.0, trigger_op="<=")

    # 未触发保持挂单
    assert paper.evaluate_intraday(tmp_path, {SYM: 10.5}) == []
    assert paper.get_order(tmp_path, order["id"])["status"] == "pending"
    # 向下触发: 成交价 = 现价 (含滑点), 费用照收
    assert len(paper.evaluate_intraday(tmp_path, {SYM: 9.9})) == 1
    got = paper.get_order(tmp_path, order["id"])
    assert got["status"] == "filled"
    assert got["fill_price"] == pytest.approx(paper.apply_slippage(9.9, "buy", 5.0))
    fee = paper.buy_fee(1000, got["fill_price"], paper.DEFAULT_COMMISSION_PCT)
    assert got["fees"] == pytest.approx(fee)
    _, cash = paper.replay_positions(tmp_path)
    assert cash == pytest.approx(1_000_000 - 1000 * got["fill_price"] - fee)


def test_conditional_intraday_ge_direction(tmp_path, monkeypatch):
    _freeze(monkeypatch, DAY, time(10, 0))
    _write_daily(tmp_path, [(DAY - timedelta(days=1), 10.0, 10.2, 9.9, 10.0)])
    paper.create_account(tmp_path, 1_000_000)
    order, _ = _cond_order(tmp_path, qty=100, trigger_price=10.5, trigger_op=">=")
    assert paper.evaluate_intraday(tmp_path, {SYM: 10.4}) == []
    assert paper.get_order(tmp_path, order["id"])["status"] == "pending"
    assert len(paper.evaluate_intraday(tmp_path, {SYM: 10.6})) == 1
    assert paper.get_order(tmp_path, order["id"])["fill_price"] == pytest.approx(paper.apply_slippage(10.6, "buy", 5.0))


def test_conditional_buy_rejected_at_limit_up(tmp_path, monkeypatch):
    """触发成交仍走 _fill_order 涨跌停校验: 触及涨停的买单拒单过期留痕。"""
    _freeze(monkeypatch, DAY, time(10, 0))
    _write_daily(tmp_path, [(DAY - timedelta(days=1), 10.0, 10.2, 9.9, 10.0)])
    paper.create_account(tmp_path, 1_000_000)
    order, _ = _cond_order(tmp_path, qty=100, trigger_price=11.0, trigger_op=">=")
    # 主板涨停 11.0: 快照 11.0 + 滑点 ≥ 涨停 → 拒单
    assert paper.evaluate_intraday(tmp_path, {SYM: 11.0}) == []
    got = paper.get_order(tmp_path, order["id"])
    assert got["status"] == "expired" and "涨停" in got["reason"]


def test_conditional_sell_requires_t1_available(tmp_path, monkeypatch):
    day = DAY
    monkeypatch.setattr(paper, "cn_today", lambda: day)
    _write_daily(tmp_path, [(day - timedelta(days=1), 10.0, 10.2, 9.9, 10.0)])
    paper.create_account(tmp_path, 1_000_000)
    # 无持仓不能挂条件卖单
    _, err = _cond_order(tmp_path, side="sell", qty=100, trigger_price=9.0, trigger_op="<=")
    assert "不能卖出" in err
    # 当日买入 T+1 锁定: 当日不可挂全量条件卖单
    buy, _ = _cond_order(tmp_path, qty=1000, trigger_price=10.0, trigger_op="<=")
    assert len(paper.evaluate_intraday(tmp_path, {SYM: 10.0})) == 1
    _, err = _cond_order(tmp_path, side="sell", qty=1000, trigger_price=9.0, trigger_op="<=")
    assert "可卖数量不足" in err
    assert paper.get_order(tmp_path, buy["id"])["status"] == "filled"


# ── 盘后结算: 穿越判定与跳空口径 ────────────────────────
def test_conditional_settle_cross_fills_at_trigger(tmp_path, monkeypatch):
    """盘中最低价穿越触发价、开盘未破 → 按触发价成交。"""
    _freeze(monkeypatch, DAY, time(8, 0))  # 开盘前创建, 当日可结算
    paper.create_account(tmp_path, 1_000_000)
    _write_daily(tmp_path, [
        (DAY - timedelta(days=1), 10.0, 10.2, 9.9, 10.0),
        (DAY, 10.5, 10.6, 9.8, 10.2),  # low 9.8 <= 10.0 触发; open 10.5 > 10.0
    ])
    order, _ = _cond_order(tmp_path, qty=100, trigger_price=10.0, trigger_op="<=")
    summary = paper.settle_day(tmp_path, DAY.isoformat())
    assert summary["filled"] == 1
    got = paper.get_order(tmp_path, order["id"])
    assert got["status"] == "filled"
    assert got["fill_price"] == pytest.approx(paper.apply_slippage(10.0, "buy", 5.0))


def test_conditional_settle_gap_fills_at_open(tmp_path, monkeypatch):
    """跳空低开破触发价 → 按开盘价成交 (更优, DSA 口径 min(open, trigger))。"""
    _freeze(monkeypatch, DAY, time(8, 0))
    paper.create_account(tmp_path, 1_000_000)
    _write_daily(tmp_path, [
        (DAY - timedelta(days=1), 10.0, 10.2, 9.9, 10.0),
        (DAY, 9.5, 10.1, 9.4, 9.9),  # 开盘 9.5 已低于触发 10.0
    ])
    order, _ = _cond_order(tmp_path, qty=100, trigger_price=10.0, trigger_op="<=")
    assert paper.settle_day(tmp_path, DAY.isoformat())["filled"] == 1
    got = paper.get_order(tmp_path, order["id"])
    assert got["fill_price"] == pytest.approx(paper.apply_slippage(9.5, "buy", 5.0))


def test_conditional_settle_ge_fills_at_trigger_and_gap(tmp_path, monkeypatch):
    """>= 向: high 穿越按触发价, 跳空高开按开盘价 (max(open, trigger))。"""
    _freeze(monkeypatch, DAY, time(8, 0))
    paper.create_account(tmp_path, 1_000_000)
    _write_daily(tmp_path, [
        (DAY - timedelta(days=1), 11.0, 11.2, 10.9, 11.0),
        (DAY, 10.5, 11.4, 10.4, 11.2),  # high 11.4 >= 11.0; open 10.5 < 11.0
    ])
    order, _ = _cond_order(tmp_path, qty=100, trigger_price=11.0, trigger_op=">=", ref_price=11.0)
    assert paper.settle_day(tmp_path, DAY.isoformat())["filled"] == 1
    assert paper.get_order(tmp_path, order["id"])["fill_price"] == pytest.approx(paper.apply_slippage(11.0, "buy", 5.0))

    # 跳空高开: 开盘已 >= 触发 → 按开盘价
    _freeze(monkeypatch, NEXT, time(8, 0))
    order2, _ = _cond_order(tmp_path, qty=100, trigger_price=11.0, trigger_op=">=", ref_price=11.0)
    _write_daily(tmp_path, [(NEXT, 11.3, 11.5, 10.9, 11.4)])  # open 11.3 >= 11.0
    assert paper.settle_day(tmp_path, NEXT.isoformat())["filled"] == 1
    assert paper.get_order(tmp_path, order2["id"])["fill_price"] == pytest.approx(paper.apply_slippage(11.3, "buy", 5.0))


def test_conditional_day_expire_and_gtc_persist(tmp_path, monkeypatch):
    """expire=day 当日未触发作废; expire=gtc 保持挂单次日继续判定。"""
    _freeze(monkeypatch, DAY, time(8, 0))
    paper.create_account(tmp_path, 1_000_000)
    _write_daily(tmp_path, [
        (DAY - timedelta(days=1), 10.0, 10.2, 9.9, 10.0),
        (DAY, 10.5, 10.6, 9.5, 10.0),  # low 9.5 > 9.0 未触发
    ])
    day_order, _ = _cond_order(tmp_path, qty=100, trigger_price=9.0, trigger_op="<=", expire="day")
    gtc_order, _ = _cond_order(tmp_path, qty=100, trigger_price=9.0, trigger_op="<=", expire="gtc", ref_price=10.0)
    summary = paper.settle_day(tmp_path, DAY.isoformat())
    assert summary["expired"] == 1
    got_day = paper.get_order(tmp_path, day_order["id"])
    assert got_day["status"] == "expired" and "作废" in got_day["reason"]
    assert paper.get_order(tmp_path, gtc_order["id"])["status"] == "pending"

    # gtc 次日穿越 → 成交
    _write_daily(tmp_path, [(NEXT, 9.6, 9.7, 8.8, 9.0)])  # low 8.8 <= 9.0
    assert paper.settle_day(tmp_path, NEXT.isoformat())["filled"] == 1
    assert paper.get_order(tmp_path, gtc_order["id"])["status"] == "filled"


def test_conditional_created_in_session_settles_next_day(tmp_path, monkeypatch):
    """盘中创建的 conditional 单当日结算跳过 (开盘后区间不可回填), 次日评估。"""
    _freeze(monkeypatch, DAY, time(10, 0))
    paper.create_account(tmp_path, 1_000_000)
    _write_daily(tmp_path, [
        (DAY - timedelta(days=1), 10.0, 10.2, 9.9, 10.0),
        (DAY, 10.5, 10.6, 9.5, 10.0),   # 当日 low 9.5 > 9.0 (即便判定也不该算)
        (NEXT, 10.2, 10.3, 9.8, 10.0),  # 次日 low 9.8 > 9.0
    ])
    order, _ = _cond_order(tmp_path, qty=100, trigger_price=9.0, trigger_op="<=", expire="day")
    summary = paper.settle_day(tmp_path, DAY.isoformat())
    assert summary["filled"] == 0 and summary["expired"] == 0
    assert paper.get_order(tmp_path, order["id"])["status"] == "pending"
    # 次日结算评估: 未触发 → day 单作废 (其「当日」= 首个可评估交易日)
    summary = paper.settle_day(tmp_path, NEXT.isoformat())
    assert summary["expired"] == 1
    assert paper.get_order(tmp_path, order["id"])["status"] == "expired"


def test_conditional_settle_missing_low_high_postpones(tmp_path, monkeypatch):
    """条件单缺 low/high 视同缺行情顺延, 不误判未触发。"""
    _freeze(monkeypatch, DAY, time(8, 0))
    paper.create_account(tmp_path, 1_000_000)
    repo = KlineRepository(DataStore(tmp_path))
    repo.append_daily(pl.DataFrame(
        {
            "symbol": [SYM, SYM],
            "date": [DAY - timedelta(days=1), DAY],
            "open": [10.0, 10.5],
            "high": [10.2, None],  # 当日缺 high/low
            "low": [9.9, None],
            "close": [10.0, 10.2],
            "volume": [10000.0, 10000.0],
            "amount": [100000.0, 102000.0],
        },
    ))
    order, _ = _cond_order(tmp_path, qty=100, trigger_price=9.0, trigger_op="<=")
    paper.settle_day(tmp_path, DAY.isoformat())
    got = paper.get_order(tmp_path, order["id"])
    assert got["status"] == "pending" and got["postponed"] == 1


def test_conditional_queue_retry_keeps_trigger_semantics(tmp_path, monkeypatch):
    """排队开启: 条件单触发遇涨停拒单 → 保持触发条件次日重判 (不转 next_open)。"""
    _freeze(monkeypatch, DAY, time(10, 0))
    paper.create_account(tmp_path, 1_000_000, queue_limit_orders=True)
    _write_daily(tmp_path, [
        (DAY - timedelta(days=1), 10.0, 10.2, 9.9, 10.0),
        (DAY, 10.5, 11.0, 10.4, 11.0),
        (NEXT, 10.4, 10.6, 10.2, 10.5),
    ])
    order, _ = _cond_order(tmp_path, qty=100, trigger_price=11.0, trigger_op=">=")
    assert paper.evaluate_intraday(tmp_path, {SYM: 11.0}) == []  # 涨停 11.0 → 排队
    got = paper.get_order(tmp_path, order["id"])
    assert got["status"] == "pending" and got["order_type"] == "conditional"
    assert got["postponed"] == 1 and got["trigger_price"] == pytest.approx(11.0)

    # 次日不再触线 → 条件单保持挂单 (未转 next_open, 不按开盘价成交)
    assert paper.settle_day(tmp_path, DAY.isoformat())["filled"] == 0
    assert paper.evaluate_intraday(tmp_path, {SYM: 10.5}) == []
    assert paper.get_order(tmp_path, order["id"])["status"] == "pending"
    assert paper.get_order(tmp_path, order["id"])["order_type"] == "conditional"


# ── API 契约 ────────────────────────────────────────────
@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(paper_router)
    app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        resolve_asset_type=lambda s: "stock",
    )
    return TestClient(app)


def test_api_conditional_order_roundtrip(client: TestClient):
    client.post("/api/paper/account", json={"initial_cash": 1000000})
    r = client.post("/api/paper/orders", json={
        "symbol": SYM, "side": "buy", "qty": 100,
        "order_type": "conditional", "trigger_price": 10.0, "trigger_op": "<=", "expire": "gtc",
    })
    assert r.status_code == 200
    order = r.json()["order"]
    assert order["order_type"] == "conditional"
    assert order["trigger_price"] == pytest.approx(10.0)
    assert order["trigger_op"] == "<="
    assert order["expire"] == "gtc"
    assert order["status"] == "pending"


def test_api_conditional_amount_mode_uses_trigger_as_ref(client: TestClient):
    """ref_price 缺省时 conditional 单按触发价折算金额 (预期成交价上界)。"""
    client.post("/api/paper/account", json={"initial_cash": 1000000})
    r = client.post("/api/paper/orders", json={
        "symbol": SYM, "side": "buy", "amount": 20000,
        "order_type": "conditional", "trigger_price": 100.0, "trigger_op": "<=",
    })
    assert r.status_code == 200
    assert r.json()["order"]["qty"] == 200


def test_api_conditional_validation_errors(client: TestClient):
    client.post("/api/paper/account", json={"initial_cash": 1000000})
    base = {"symbol": SYM, "side": "buy", "qty": 100, "order_type": "conditional",
            "trigger_price": 10.0}
    r = client.post("/api/paper/orders", json={**base, "trigger_op": "<"})
    assert r.status_code == 400 and "trigger_op" in r.json()["detail"]
    r = client.post("/api/paper/orders", json={**base, "trigger_op": "<=", "expire": "week"})
    assert r.status_code == 400 and "expire" in r.json()["detail"]
    # market 单带触发字段 → 400
    r = client.post("/api/paper/orders", json={
        "symbol": SYM, "side": "buy", "qty": 100,
        "order_type": "market", "trigger_price": 10.0, "trigger_op": "<=",
    })
    assert r.status_code == 400 and "仅 conditional" in r.json()["detail"]
