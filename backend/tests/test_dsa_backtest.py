"""DSA 报告点位回测端点测试 (迁移计划 §4.7-2 B2 扩展侧)。

报告库经 settings.data_dir 指向 tmp 走真实 dsa_analysis._STORE; 引擎为真实
BacktestEngine(repo=None), load_panel 打桩注入手工日K面板 (撮合语义由
tests/backtest/test_absolute_points.py 锁定, 本文件锁端点桥接与契约形状)。

覆盖: 触及建仓+绝对止损出场全流程 (含费用口径 return_pct)、跳空低开按开盘价、
未触及建仓走 note (200)、数据不足走 note、报告缺失 404、缺 ideal_buy 400、
持有窗到期 max_hold、loader 契约 (路由注册)。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config as app_config
from app.backtest.engine import BacktestEngine
from app.custom import dsa_analysis, dsa_backtest

BASE = date(2024, 1, 1)


def _panel(bars: list[tuple[float, float, float, float]]) -> pl.DataFrame:
    """[(open, high, low, close)] 从 BASE 起逐日。"""
    rows = [
        {
            "symbol": "600519.SH", "name": "贵州茅台",
            "date": BASE + timedelta(days=i),
            "open": o, "high": h, "low": low, "close": c,
            "volume": 100_000,
        }
        for i, (o, h, low, c) in enumerate(bars)
    ]
    return pl.DataFrame(rows).sort(["symbol", "date"])


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)  # 报告库隔离
    app = FastAPI()
    app.include_router(_build_router())
    app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        resolve_asset_type=lambda s: "stock",
    )
    engine = BacktestEngine(repo=None)
    app.state.backtest_engine = engine
    return TestClient(app), engine, monkeypatch


def _build_router():
    """独立 FastAPI app 直接挂 setup() 注册的路由 (镜像扩展装载)。"""
    from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar

    registrar = BackendExtensionRegistrar(dsa_backtest.EXTENSION_ID, api_version=BACKEND_EXTENSION_API_VERSION)
    dsa_backtest.setup(registrar)
    assert len(registrar.routers) == 1
    return registrar.routers[0]


def _set_panel(engine: BacktestEngine, monkeypatch, panel: pl.DataFrame) -> None:
    monkeypatch.setattr(engine, "load_panel", lambda *args, **kwargs: panel)


def _save_report(report_id: str, points: dict, created: str = "2024-01-01T18:00:00") -> dict:
    return dsa_analysis._STORE.save({
        "id": report_id, "symbol": "600519.SH", "name": "贵州茅台",
        "created_at": created, "mode": "full", "operation_advice": "买入",
        "points": points,
    })


URL = "/api/ext/dsa/backtest-report"


def test_backtest_report_full_episode_with_stop(client):
    tc, engine, monkeypatch = client
    _set_panel(engine, monkeypatch, _panel([
        (10.0, 10.2, 9.8, 10.0),   # 报告日 (信号)
        (10.2, 10.4, 9.9, 10.3),   # 成交日: 触及 → 建仓 @10.0
        (10.1, 10.2, 9.4, 9.6),    # 盘中破 9.5 → 止损 @9.5
        (9.5, 9.6, 9.3, 9.4),
    ]))
    _save_report("r1", {"ideal_buy": 10.0, "secondary_buy": 10.5, "stop_loss": 9.5, "take_profit": None})
    resp = tc.post(URL, json={"report_id": "r1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "600519.SH"
    assert body["entry"] == {"date": "2024-01-02", "price": 10.0}
    assert body["exit"] == {"date": "2024-01-03", "price": 9.5, "reason": "stop_loss"}
    # holding_bars = 引擎原生 duration (成交日之后的持有根数, 与 DSA 逐根计数一致)
    assert body["holding_bars"] == 1
    assert "止损" in body["note"]
    # return_pct 百分数口径 (与 dsa_paper 互证同号): 含默认费用为负
    expected = ((9.5 * (1 - 0.0007)) / (10.0 * (1 + 0.0007)) - 1) * 100
    assert body["return_pct"] == pytest.approx(expected, abs=0.01)
    assert body["return_pct"] < 0


def test_backtest_report_gap_down_fills_at_open(client):
    tc, engine, monkeypatch = client
    _set_panel(engine, monkeypatch, _panel([
        (10.0, 10.2, 9.8, 10.0),
        (9.8, 10.0, 9.7, 9.9),     # 开盘 9.8 < 介入价 10 → 按开盘价 (更优)
        (9.9, 10.0, 9.8, 9.9),
        (9.9, 10.0, 9.8, 9.9),
    ]))
    _save_report("r1", {"ideal_buy": 10.0, "stop_loss": None, "take_profit": None})
    body = tc.post(URL, json={"report_id": "r1"}).json()
    assert body["entry"]["price"] == 9.8
    assert body["entry"]["date"] == "2024-01-02"


def test_backtest_report_take_profit_exit(client):
    tc, engine, monkeypatch = client
    _set_panel(engine, monkeypatch, _panel([
        (10.0, 10.2, 9.8, 10.0),
        (10.2, 10.4, 9.9, 10.3),
        (10.4, 11.0, 10.3, 10.9),  # 触 10.8 → 止盈
    ]))
    _save_report("r1", {"ideal_buy": 10.0, "stop_loss": None, "take_profit": 10.8})
    body = tc.post(URL, json={"report_id": "r1"}).json()
    assert body["exit"]["reason"] == "take_profit"
    assert body["exit"]["price"] == 10.8
    assert body["return_pct"] > 0


def test_backtest_report_not_touched_note_not_error(client):
    tc, engine, monkeypatch = client
    _set_panel(engine, monkeypatch, _panel([
        (10.0, 10.2, 9.8, 10.0),
        (10.6, 10.8, 10.5, 10.7),  # 全天未触及 10.0
        (10.6, 10.8, 10.5, 10.7),
    ]))
    _save_report("r1", {"ideal_buy": 10.0, "stop_loss": None, "take_profit": None})
    resp = tc.post(URL, json={"report_id": "r1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["entry"] is None and body["exit"] is None and body["return_pct"] is None
    assert body["holding_bars"] == 0
    assert "未触及" in body["note"]


def test_backtest_report_max_hold_window(client):
    tc, engine, monkeypatch = client
    _set_panel(engine, monkeypatch, _panel([(10.0, 10.05, 9.95, 10.0)] * 13))
    _save_report("r1", {"ideal_buy": 10.0, "stop_loss": None, "take_profit": None})
    body = tc.post(URL, json={"report_id": "r1"}).json()
    assert body["exit"]["reason"] == "max_hold"
    assert body["holding_bars"] == 10


def test_backtest_report_no_market_data_note(client):
    tc, engine, monkeypatch = client
    _set_panel(engine, monkeypatch, _panel([(10.0, 10.2, 9.8, 10.0)]))  # 只有报告日一根, 无法成交
    _save_report("r1", {"ideal_buy": 10.0, "stop_loss": None, "take_profit": None})
    body = tc.post(URL, json={"report_id": "r1"}).json()
    assert body["entry"] is None and "note" in body


def test_backtest_report_missing_report_404(client):
    tc, _engine, _monkeypatch = client
    assert tc.post(URL, json={"report_id": "nope"}).status_code == 404
    assert tc.post(URL, json={}).status_code == 400


def test_backtest_report_without_ideal_buy_400(client):
    tc, engine, monkeypatch = client
    _set_panel(engine, monkeypatch, _panel([(10.0, 10.2, 9.8, 10.0)]))
    _save_report("r1", {"ideal_buy": None, "stop_loss": 9.0, "take_profit": None})
    resp = tc.post(URL, json={"report_id": "r1"})
    assert resp.status_code == 400 and "ideal_buy" in resp.json()["detail"]


def test_extension_registers_route():
    """loader 契约: EXTENSION_ID 合法, setup 注册 /api/ext/dsa/backtest-report。"""
    from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar

    registrar = BackendExtensionRegistrar(dsa_backtest.EXTENSION_ID, api_version=BACKEND_EXTENSION_API_VERSION)
    dsa_backtest.setup(registrar)
    paths = {route.path for router in registrar.routers for route in router.routes}
    assert "/api/ext/dsa/backtest-report" in paths
