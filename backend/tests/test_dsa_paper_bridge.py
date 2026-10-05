"""DSA 报告点位 → 模拟盘自动下单桥测试 (迁移计划 §4.7-1 B1 桥侧语义)。

报告库经 settings.data_dir 指向 tmp 后走真实 dsa_analysis._STORE (每报告一文件,
与 test_dsa_watch 同款隔离); 模拟盘用真实 paper 域函数在 tmp 建账户。

覆盖: 入场单生成与金额槽位口径 / 幂等重跑 / day 单作废不重挂 / 新报告撤旧建新 /
持仓票止损 GTC 单 / 止损变化重建 / 缺止损槽位级保留 / 报告全删止损保留 /
卖出类建议 exit 单 (意图优先撤止损) / take_profit 不出单 / 空仓卖出建议不建入场 /
无账户静默待命。
"""
from __future__ import annotations

import threading
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from app import config as app_config
from app.custom import dsa_analysis, dsa_paper_bridge
from app.strategy import paper
from app.tickflow.repository import DataStore, KlineRepository

SYM = "600519.SH"
DAY = date(2026, 9, 24)
NEXT = DAY + timedelta(days=1)


@pytest.fixture
def data_dir(tmp_path, monkeypatch) -> Path:
    """settings.data_dir 指向 tmp (报告库/模拟盘全部隔离)。"""
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    return tmp_path


def _write_daily(tmp_path: Path, rows: list[tuple[date, float, float, float, float]]) -> None:
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


def _report(report_id: str, advice: str = "持有", points: dict | None = None,
            created: str = "2026-09-29T18:00:00") -> dict:
    return {
        "id": report_id,
        "symbol": SYM,
        "name": "贵州茅台",
        "created_at": created,
        "mode": "full",
        "operation_advice": advice,
        "points": points if points is not None else {
            "ideal_buy": None, "secondary_buy": None, "stop_loss": None, "take_profit": None,
        },
    }


def _save_report(**kwargs) -> dict:
    return dsa_analysis._STORE.save(_report(**kwargs))


def _buy_position(data_dir: Path, monkeypatch, qty: int = 1000, price: float = 9.5) -> None:
    """在 DAY 买入建仓 (次日起可卖)。"""
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    _write_daily(data_dir, [(DAY - timedelta(days=1), price, price + 0.2, price - 0.2, price)])
    paper.create_account(data_dir, 1_000_000)
    _order, err = paper.create_order(data_dir, SYM, "buy", qty=qty, ref_price=price)
    assert err is None
    assert len(paper.evaluate_intraday(data_dir, {SYM: price})) == 1


def _pending(data_dir: Path) -> list[dict]:
    return [o for o in paper.load_orders(data_dir) if o["status"] == "pending"]


# ── 入场单 (空仓票 + ideal_buy) ─────────────────────────
def test_bridge_entry_buy_created_for_empty_symbol(data_dir):
    paper.create_account(data_dir, 1_000_000)
    _save_report(report_id="r_20260929_600519_001", points={
        "ideal_buy": 10.0, "secondary_buy": 10.5, "stop_loss": 9.0, "take_profit": 12.0,
    })
    summary = dsa_paper_bridge.run_bridge(data_dir)
    assert summary == {"created": 1, "cancelled": 0, "skipped": 0}
    (order,) = _pending(data_dir)
    # 金额 = 权益/10 槽 = 10万, 按触发价折算整百 (amount 折算后订单只落 qty); 次日 day 条件买单
    assert order["side"] == "buy" and order["order_type"] == "conditional"
    assert order["qty"] == 10000 and order["ref_price"] == pytest.approx(10.0)
    assert order["trigger_price"] == pytest.approx(10.0)
    assert order["trigger_op"] == "<=" and order["expire"] == "day"
    assert order["source"] == "dsa:r_20260929_600519_001"


def test_bridge_entry_idempotent_and_expired_not_replaced(data_dir):
    paper.create_account(data_dir, 1_000_000)
    _save_report(report_id="r1", points={"ideal_buy": 10.0, "stop_loss": None,
                                         "secondary_buy": None, "take_profit": None})
    assert dsa_paper_bridge.run_bridge(data_dir)["created"] == 1
    # 幂等: 重跑不重建
    assert dsa_paper_bridge.run_bridge(data_dir) == {"created": 0, "cancelled": 0, "skipped": 0}
    # day 单作废 (未触发) → 同报告不重挂 (DSA「当根作废不顺延」)
    (order,) = paper.load_orders(data_dir)
    order["status"] = "expired"
    order["reason"] = "条件触发单当日未触发, 作废 (expire=day)"
    paper.save_order(data_dir, order)
    assert dsa_paper_bridge.run_bridge(data_dir) == {"created": 0, "cancelled": 0, "skipped": 0}


def test_bridge_new_report_replaces_entry_order(data_dir):
    paper.create_account(data_dir, 1_000_000)
    _save_report(report_id="r1", points={"ideal_buy": 10.0, "stop_loss": None,
                                         "secondary_buy": None, "take_profit": None})
    dsa_paper_bridge.run_bridge(data_dir)
    _save_report(report_id="r2", created="2026-09-30T18:00:00",
                 points={"ideal_buy": 9.5, "stop_loss": None, "secondary_buy": None, "take_profit": None})
    summary = dsa_paper_bridge.run_bridge(data_dir)
    assert summary["created"] == 1 and summary["cancelled"] == 1
    (pending,) = _pending(data_dir)
    assert pending["source"] == "dsa:r2" and pending["trigger_price"] == pytest.approx(9.5)
    (old,) = [o for o in paper.load_orders(data_dir) if o["id"] != pending["id"]]
    assert old["status"] == "cancelled"


def test_bridge_sell_advice_on_empty_symbol_no_entry(data_dir):
    """空仓票报告含卖出类建议: 即便有 ideal_buy 也不建入场单 (报告自相矛盾取保守)。"""
    paper.create_account(data_dir, 1_000_000)
    _save_report(report_id="r1", advice="观望,跌破支撑减仓",
                 points={"ideal_buy": 10.0, "stop_loss": None, "secondary_buy": None, "take_profit": None})
    assert dsa_paper_bridge.run_bridge(data_dir) == {"created": 0, "cancelled": 0, "skipped": 0}
    assert _pending(data_dir) == []


# ── 止损单 (持仓票 + stop_loss) ─────────────────────────
def test_bridge_stop_sell_gtc_for_held_symbol(data_dir, monkeypatch):
    _buy_position(data_dir, monkeypatch)
    monkeypatch.setattr(paper, "cn_today", lambda: NEXT)
    monkeypatch.setattr(dsa_paper_bridge, "cn_today", lambda: NEXT)
    _save_report(report_id="r1", points={
        "ideal_buy": None, "secondary_buy": None, "stop_loss": 9.0, "take_profit": 12.0,
    })
    summary = dsa_paper_bridge.run_bridge(data_dir)
    assert summary == {"created": 1, "cancelled": 0, "skipped": 0}
    (order,) = _pending(data_dir)
    # 全量可卖整百 GTC 条件卖单; take_profit 是移动止盈启动价, 不出单
    assert order["side"] == "sell" and order["order_type"] == "conditional"
    assert order["qty"] == 1000
    assert order["trigger_price"] == pytest.approx(9.0)
    assert order["trigger_op"] == "<=" and order["expire"] == "gtc"
    assert order["source"] == "dsa:r1:stop"
    assert dsa_paper_bridge.run_bridge(data_dir) == {"created": 0, "cancelled": 0, "skipped": 0}


def test_bridge_stop_change_replaces_order(data_dir, monkeypatch):
    _buy_position(data_dir, monkeypatch)
    monkeypatch.setattr(paper, "cn_today", lambda: NEXT)
    monkeypatch.setattr(dsa_paper_bridge, "cn_today", lambda: NEXT)
    _save_report(report_id="r1", points={
        "ideal_buy": None, "secondary_buy": None, "stop_loss": 9.0, "take_profit": None,
    })
    dsa_paper_bridge.run_bridge(data_dir)
    _save_report(report_id="r2", created="2026-09-30T18:00:00", points={
        "ideal_buy": None, "secondary_buy": None, "stop_loss": 8.8, "take_profit": None,
    })
    summary = dsa_paper_bridge.run_bridge(data_dir)
    assert summary["created"] == 1 and summary["cancelled"] == 1
    (pending,) = _pending(data_dir)
    assert pending["source"] == "dsa:r2:stop" and pending["trigger_price"] == pytest.approx(8.8)


def test_bridge_missing_stop_keeps_existing_order(data_dir, monkeypatch):
    """最新报告缺 stop_loss: 既有止损单槽位级保留 (防 LLM 闪失清掉止损线)。"""
    _buy_position(data_dir, monkeypatch)
    monkeypatch.setattr(paper, "cn_today", lambda: NEXT)
    monkeypatch.setattr(dsa_paper_bridge, "cn_today", lambda: NEXT)
    _save_report(report_id="r1", points={
        "ideal_buy": None, "secondary_buy": None, "stop_loss": 9.0, "take_profit": None,
    })
    dsa_paper_bridge.run_bridge(data_dir)
    _save_report(report_id="r2", created="2026-09-30T18:00:00", points={
        "ideal_buy": None, "secondary_buy": 9.2, "stop_loss": None, "take_profit": None,
    })
    summary = dsa_paper_bridge.run_bridge(data_dir)
    assert summary == {"created": 0, "cancelled": 0, "skipped": 0}
    (order,) = _pending(data_dir)
    assert order["source"] == "dsa:r1:stop" and order["status"] == "pending"


def test_bridge_all_reports_deleted_keeps_stop(data_dir, monkeypatch):
    """报告库无该票报告: 止损单保护性保留。"""
    _buy_position(data_dir, monkeypatch)
    monkeypatch.setattr(paper, "cn_today", lambda: NEXT)
    monkeypatch.setattr(dsa_paper_bridge, "cn_today", lambda: NEXT)
    report = _save_report(report_id="r1", points={
        "ideal_buy": None, "secondary_buy": None, "stop_loss": 9.0, "take_profit": None,
    })
    dsa_paper_bridge.run_bridge(data_dir)
    assert dsa_analysis._STORE.delete(report["id"])
    assert dsa_paper_bridge.run_bridge(data_dir) == {"created": 0, "cancelled": 0, "skipped": 0}
    (order,) = _pending(data_dir)
    assert order["source"] == "dsa:r1:stop"


# ── 卖出类建议 → 次日开盘卖单 ───────────────────────────
def test_bridge_sell_advice_exit_replaces_stop(data_dir, monkeypatch):
    _buy_position(data_dir, monkeypatch)
    monkeypatch.setattr(paper, "cn_today", lambda: NEXT)
    monkeypatch.setattr(dsa_paper_bridge, "cn_today", lambda: NEXT)
    _save_report(report_id="r1", points={
        "ideal_buy": None, "secondary_buy": None, "stop_loss": 9.0, "take_profit": None,
    })
    dsa_paper_bridge.run_bridge(data_dir)
    _save_report(report_id="r2", created="2026-09-30T18:00:00", advice="清仓离场",
                 points={"ideal_buy": None, "secondary_buy": None, "stop_loss": None, "take_profit": None})
    summary = dsa_paper_bridge.run_bridge(data_dir)
    assert summary["created"] == 1 and summary["cancelled"] == 1
    (order,) = _pending(data_dir)
    # 卖出意图优先于止损线: 次日开盘全量卖出, 旧止损单撤销
    assert order["side"] == "sell" and order["order_type"] == "next_open"
    assert order["qty"] == 1000
    assert order["source"] == "dsa:r2:exit"


def test_bridge_take_profit_alone_no_order(data_dir, monkeypatch):
    """take_profit 是移动止盈启动价, TSP 模拟盘无回撤跟踪引擎, 不出单。"""
    paper.create_account(data_dir, 1_000_000)
    _save_report(report_id="r1", points={
        "ideal_buy": None, "secondary_buy": None, "stop_loss": None, "take_profit": 12.0,
    })
    assert dsa_paper_bridge.run_bridge(data_dir) == {"created": 0, "cancelled": 0, "skipped": 0}


# ── 降级 ────────────────────────────────────────────────
def test_bridge_no_account_stands_by(data_dir):
    _save_report(report_id="r1", points={"ideal_buy": 10.0, "stop_loss": None,
                                         "secondary_buy": None, "take_profit": None})
    # 模拟盘未初始化: 静默待命, 不代建账户不下单
    assert dsa_paper_bridge.run_bridge(data_dir) == {"created": 0, "cancelled": 0, "skipped": 0}
    assert paper.get_account(data_dir) is None
    assert _pending(data_dir) == []


def test_bridge_no_reports_no_orders(data_dir):
    paper.create_account(data_dir, 1_000_000)
    assert dsa_paper_bridge.run_bridge(data_dir) == {"created": 0, "cancelled": 0, "skipped": 0}
    assert _pending(data_dir) == []


def test_bridge_extension_registers_and_starts(data_dir, monkeypatch):
    """loader 契约: EXTENSION_ID 合法、setup 可调、startup 幂等启动轮询线程。

    _poll_loop 替换为永久等待桩: 断言线程被拉起且 daemon, 不真跑对账 (不碰
    测试结束即删除的 tmp 目录)。
    """
    from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar

    assert dsa_paper_bridge.EXTENSION_API_VERSION == BACKEND_EXTENSION_API_VERSION
    registrar = BackendExtensionRegistrar(dsa_paper_bridge.EXTENSION_ID, api_version=BACKEND_EXTENSION_API_VERSION)
    dsa_paper_bridge.setup(registrar)
    assert registrar.routers == []  # 纯后台桥, 无路由

    monkeypatch.setattr(
        dsa_paper_bridge, "_poll_loop", lambda: threading.Event().wait(), raising=True,
    )
    from app.extensions.contracts import ExtensionContext
    context = ExtensionContext(
        api_version=BACKEND_EXTENSION_API_VERSION, data_dir=data_dir, repository=None,
    )
    dsa_paper_bridge.startup(context)
    thread = dsa_paper_bridge._POLL_THREAD
    assert thread is not None and thread.is_alive() and thread.daemon
    dsa_paper_bridge.startup(context)  # 幂等: 不重复起线程
    assert dsa_paper_bridge._POLL_THREAD is thread
    assert dsa_paper_bridge._RUNTIME["data_dir"] == data_dir
    # 复位模块态, 不污染其他测试
    dsa_paper_bridge._POLL_THREAD = None
    dsa_paper_bridge._RUNTIME["data_dir"] = None
