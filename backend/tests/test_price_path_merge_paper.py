"""价格路径事件合并统一出口 + paper_auto 信息事件防御 + API 契约测试。

- merge_round_events 纯函数: 同 symbol 同 kind (接近/逼近/到价) 合并最强一条,
  不同 kind 保留, 同档 (reduce 多档) 取更深入方向, 非 path 事件透传。
- quote_service._evaluate_monitors 统一出口: 合并发生在 落盘/SSE/系统通知/
  Webhook 之前, 数据库记录与 SSE 同口径 (同一份合并后列表)。
- paper_auto.on_rule_events 真实下单边界: info_only 信息事件 (收复/反弹) 绝不
  下单; 合并后的单条触价事件最多一单 (不连下三单); 原方向触价事件仍可跟单
  (与旧行为兼容)。
- API: RuleModel 可选契约 + PUT 未传字段不吞掉托管模式。
"""
from __future__ import annotations

from datetime import datetime
from datetime import time as dt_time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.market_time import CN_TZ, cn_today
from app.services.quote_service import QuoteService
from app.strategy import monitor_rules, paper, paper_auto
from app.strategy.monitor import MonitorRuleEngine
from app.strategy.price_path import merge_round_events

SYM = "600519.SH"


def _ts(h: int, m: int, s: int) -> int:
    return int(datetime.combine(cn_today(), dt_time(h, m, s), tzinfo=CN_TZ).timestamp() * 1000)


def _rule(rid: str, kind: str, op: str, value: float, sym: str = SYM,
          severity: str = "warn") -> dict:
    return monitor_rules.normalize({
        "id": rid, "name": f"DSA·{sym[:6]}·{kind}", "type": "price",
        "scope": "symbols", "symbols": [sym], "asset_type": "stock",
        "conditions": [{"field": "close", "op": op, "value": value}],
        "severity": severity, "cooldown_seconds": 86400, "enabled": True,
        "price_path": True, "path_kind": kind, "message": f"DSA {kind} {value}",
    })


def _ev(rid: str, kind: str, value: float, op: str = "<=", *, info: bool = False,
        sym: str = SYM, etype: str = "price", severity: str | None = None) -> dict:
    return {
        "rule_id": rid, "rule_name": rid, "source": "price", "type": etype,
        "symbol": sym, "name": None, "price": value, "change_pct": 0.0,
        "signals": [], "severity": severity or ("critical" if not info else "info"),
        "conditions": [{"field": "close", "op": op, "value": value}],
        "logic": "and", "path_kind": kind, **({"info_only": True} if info else {}),
    }


# ================================================================
# merge_round_events 纯函数
# ================================================================

def test_merge_same_kind_keeps_main_over_ladder():
    plain = {"rule_id": "manual", "type": "price", "symbol": SYM, "price": 1.0,
             "conditions": [{"field": "close", "op": "<=", "value": 1.0}]}
    events = [
        _ev("dsa_n_add", "near_add", 32.32),
        _ev("dsa_add", "add", 32.0),
        _ev("dsa_m_add", "mid_add", 32.16),
        plain,  # 无 path_kind → 透传
    ]
    out = merge_round_events(events)
    ids = [e["rule_id"] for e in out]
    assert ids == ["dsa_add", "manual"]  # 主规则最强, near/mid 被合并
    assert out[0]["merged_count"] == 3
    assert "merged_count" not in out[1]


def test_merge_different_kinds_all_kept():
    events = [
        _ev("dsa_add", "near_add", 32.32),
        _ev("dsa_add2", "add", 32.0),
        _ev("dsa_stop", "stop_loss", 30.5),
        _ev("dsa_n_stop", "near_stop_loss", 30.81),
    ]
    out = merge_round_events(events)
    assert [e["rule_id"] for e in out] == ["dsa_add2", "dsa_stop"]  # 止损不被加仓吞


def test_merge_same_tier_reduce_keeps_deeper_line():
    events = [
        _ev("dsa_reduce_2", "mid_reduce", 32.0),  # rank2 逼近 (浅)
        _ev("dsa_reduce_1", "mid_reduce", 31.0),  # rank1 逼近 (深, <= 更小值更强)
    ]
    out = merge_round_events(events)
    assert [e["rule_id"] for e in out] == ["dsa_reduce_1"]


def test_merge_gte_tier_prefers_higher_and_info_events_mergeable():
    events = [
        _ev("dsa_tp", "take_profit", 40.0, ">="),
        _ev("dsa_n_tp", "near_take_profit", 39.6, ">="),
        # 收复信息事件与触价分别分组, 两种方向均保留
        _ev("dsa_n_tp", "near_take_profit", 39.6, ">=", info=True, etype="price_path_recovery"),
    ]
    out = merge_round_events(events)
    assert [e["rule_id"] for e in out] == ["dsa_tp", "dsa_n_tp"]
    assert out[0]["type"] == "price" and out[0].get("info_only") is None


def test_merge_passes_through_non_path_and_bounce_events():
    plain = {"rule_id": "manual", "type": "price", "symbol": SYM, "price": 1.0}
    bounce = {"rule_id": "dsa_x", "type": "price_path_bounce", "symbol": SYM,
              "info_only": True, "price": 0.84}  # 反弹事件无 path_kind
    out = merge_round_events([plain, bounce, _ev("dsa_add", "add", 32.0)])
    assert [e["type"] for e in out] == ["price", "price_path_bounce", "price"]


# ================================================================
# quote_service 统一出口 (引擎 → 合并 → 落盘/SSE 同口径)
# ================================================================

class _Repo:
    def __init__(self, data_dir: Path) -> None:
        self.store = SimpleNamespace(data_dir=data_dir)

    def resolve_asset_type(self, symbol: str) -> str:
        return "stock"

    def get_name_map(self, symbols=None):
        return {SYM: "测试股"}


def _run_evaluate(data_dir: Path, rules: list[dict], ts: int, close: float):
    engine = MonitorRuleEngine()
    engine.set_data_dir(data_dir)
    engine.set_name_map({SYM: "测试股"})
    engine.set_rules(rules)
    df = pl.DataFrame({
        "symbol": [SYM], "close": [close], "raw_close": [close], "change_pct": [0.0], "quote_ts": [ts],
    })
    svc = QuoteService.__new__(QuoteService)
    svc._repo = _Repo(data_dir)
    svc._app_state = SimpleNamespace(monitor_engine=engine, repo=svc._repo)
    sse: list[dict] = []
    stored: list[list[dict]] = []
    webhooks: list[list[dict]] = []
    with (
        patch.object(QuoteService, "_is_continuous_trading", return_value=True),
        patch("app.strategy.monitor.time.time", return_value=ts / 1000 + 1),
        patch.object(QuoteService, "get_enriched_today", return_value=(df, cn_today())),
        patch.object(QuoteService, "_inject_intraday_signals", side_effect=lambda df, e, at: df),
        patch.object(QuoteService, "_enrich_alerts_ext", lambda self, alerts: None),
        patch.object(QuoteService, "_broadcast_alerts", lambda self, alerts: sse.extend(alerts)),
        patch.object(QuoteService, "_maybe_send_system_notifications", lambda self, alerts: None),
        patch.object(QuoteService, "_maybe_send_webhook",
                     lambda self, events, engine: webhooks.append(list(events))),
        patch("app.services.alert_store.append_many",
              lambda data_dir, events: stored.append(list(events))),
        patch("app.strategy.paper.list_account_ids", lambda data_dir: []),
    ):
        svc._evaluate_monitors(df, None)
    return engine, sse, stored, webhooks


def test_quote_service_exit_merges_before_store_sse_webhook(tmp_path):
    """跳空穿四线: 引擎 4 条 → 统一出口合并为 2 条, 落盘/SSE/Webhook 同口径。"""
    rules = [
        _rule("dsa_n_add", "near_add", "<=", 32.32, severity="info"),
        _rule("dsa_add", "add", "<=", 32.0, severity="warn"),
        _rule("dsa_n_stop", "near_stop_loss", "<=", 30.81, severity="info"),
        _rule("dsa_stop", "stop_loss", "<=", 30.5, severity="critical"),
    ]
    _engine, sse, stored, webhooks = _run_evaluate(tmp_path, rules, _ts(9, 36, 0), 30.4)
    # 独立引擎验证原始产出; 已经触发过的引擎必须保持去重。
    raw_engine = MonitorRuleEngine()
    raw_engine.set_rules(rules)
    with patch("app.strategy.monitor.time.time", return_value=_ts(9, 36, 0) / 1000 + 1):
        raw = raw_engine.evaluate(pl.DataFrame({
        "symbol": [SYM], "close": [30.4], "change_pct": [0.0], "quote_ts": [_ts(9, 36, 0)],
        }), asset_type="stock")
    assert len([e for e in raw if e["type"] == "price"]) == 4
    # 统一出口: 同 kind 合并最强 (add 32.0 + stop 30.5), 止损 critical 保留
    assert [e["rule_id"] for e in sse] == ["dsa_add", "dsa_stop"]
    assert [e["severity"] for e in sse] == ["warn", "critical"]
    assert [e["path_kind"] for e in sse] == ["add", "stop_loss"]
    assert len(stored) == 1 and [e["rule_id"] for e in stored[0]] == ["dsa_add", "dsa_stop"]
    assert [e["rule_id"] for e in webhooks[0]] == ["dsa_add", "dsa_stop"]  # Webhook 同口径


# ================================================================
# paper_auto 真实下单边界
# ================================================================

def _auto_rule(data_dir: Path, match_id: str) -> None:
    paper_auto.create_auto_rule(data_dir, {
        "name": "跟规则", "match_kind": "rule", "match_id": match_id,
        "side": "buy", "size_mode": "fixed_amount", "size_value": 5000,
        "order_type": "next_open", "cooldown_days": 0,
    })


def test_paper_auto_skips_info_only_events(tmp_path):
    """收复/反弹信息事件带 symbol+price 也不下单 (入口防御, 不只靠文案)。"""
    paper.create_account(tmp_path, 1_000_000)
    _auto_rule(tmp_path, "dsa_add")
    created = paper_auto.on_rule_events(tmp_path, [
        {**_ev("dsa_add", "add", 0.97), "type": "price_path_recovery", "info_only": True},
        {**_ev("dsa_add", "add", 0.84), "type": "price_path_bounce", "info_only": True},
    ])
    assert created == []


def test_paper_auto_merged_trigger_places_at_most_one_order(tmp_path):
    """合并后的同 kind 一条事件: 最多一单 (不因接近/逼近/到价同轮连下三单)。"""
    paper.create_account(tmp_path, 1_000_000)
    _auto_rule(tmp_path, "dsa_stop")
    events = [
        _ev("dsa_n_stop", "near_stop_loss", 30.81, severity="info"),
        _ev("dsa_m_stop", "mid_stop_loss", 30.65),
        _ev("dsa_stop", "stop_loss", 30.5, severity="critical"),
    ]
    merged = merge_round_events(events)
    assert len(merged) == 1 and merged[0]["rule_id"] == "dsa_stop"
    created = paper_auto.on_rule_events(tmp_path, merged)
    assert len(created) == 1
    assert created[0]["symbol"] == SYM and created[0]["source"].startswith("auto:")


def test_paper_auto_trigger_event_still_places_order(tmp_path):
    """原方向触价事件 (无 info_only) 与旧 price 事件同构: 跟单行为不变。"""
    paper.create_account(tmp_path, 1_000_000)
    _auto_rule(tmp_path, "dsa_stop")
    created = paper_auto.on_rule_events(tmp_path, [
        _ev("dsa_stop", "stop_loss", 30.5, severity="critical"),
    ])
    assert len(created) == 1 and created[0]["symbol"] == SYM


# ================================================================
# API 契约: 可选字段 + PUT 不吞模式
# ================================================================

def _client(tmp_path: Path) -> TestClient:
    from app.api.monitor_rules import router

    app = FastAPI()
    app.include_router(router)
    app.state.repo = _Repo(tmp_path)
    return TestClient(app)


def test_api_accepts_price_path_and_put_preserves_it(tmp_path):
    client = _client(tmp_path)
    body = {
        "id": "dsa_path_1", "name": "DSA·600519·stop_loss", "type": "price",
        "scope": "symbols", "symbols": [SYM], "asset_type": "stock",
        "conditions": [{"field": "close", "op": "<=", "value": 30.5}],
        "severity": "critical", "cooldown_seconds": 86400,
        "price_path": True, "path_kind": "stop_loss",
    }
    resp = client.post("/api/monitor-rules", json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["rule"]["price_path"] is True
    # PUT 未传 price_path (旧表单): 不吞掉模式
    body2 = {k: v for k, v in body.items() if k not in ("price_path", "path_kind")}
    resp2 = client.post("/api/monitor-rules", json=body2)
    assert resp2.status_code == 200, resp2.text
    rule2 = resp2.json()["rule"]
    assert rule2["price_path"] is True and rule2["path_kind"] == "stop_loss"
    # 显式 False 可关闭 (托管方/用户意图)
    resp3 = client.post("/api/monitor-rules", json={**body, "price_path": False, "path_kind": None})
    assert resp3.status_code == 200
    assert resp3.json()["rule"]["price_path"] is False


def test_api_rejects_unsupported_price_path_shape(tmp_path):
    """不支持本模式的条件 (多标的/多条件/非 close) 明确 400, 不退化。"""
    client = _client(tmp_path)
    bad_multi_symbol = {
        "id": "bad1", "name": "多标的", "type": "price", "scope": "symbols",
        "symbols": [SYM, "000001.SZ"], "asset_type": "stock",
        "conditions": [{"field": "close", "op": "<=", "value": 30.5}],
        "price_path": True,
    }
    assert client.post("/api/monitor-rules", json=bad_multi_symbol).status_code == 400
    bad_field = {
        "id": "bad2", "name": "非close", "type": "price", "scope": "symbols",
        "symbols": [SYM], "asset_type": "stock",
        "conditions": [{"field": "change_pct", "op": "<=", "value": -0.05}],
        "price_path": True,
    }
    assert client.post("/api/monitor-rules", json=bad_field).status_code == 400
    bad_type = {
        "id": "bad3", "name": "signal型", "type": "signal", "scope": "symbols",
        "symbols": [SYM], "asset_type": "stock",
        "conditions": [{"field": "close", "op": "<=", "value": 30.5}],
        "price_path": True,
    }
    assert client.post("/api/monitor-rules", json=bad_type).status_code == 400


def test_old_rule_json_without_fields_loads_as_plain(tmp_path):
    """旧 JSON 缺字段: normalize 补 False/None, 行为与人工规则一致。"""
    rule = monitor_rules.normalize({
        "id": "legacy", "name": "旧规则", "type": "price", "scope": "symbols",
        "symbols": [SYM], "conditions": [{"field": "close", "op": "<=", "value": 1.0}],
    })
    assert rule["price_path"] is False and rule["path_kind"] is None
