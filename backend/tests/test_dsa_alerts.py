"""dsa_alerts 回放审计的单元测试 (P4 §4.5-3)。"""
from __future__ import annotations

import json

from app.custom import dsa_alerts
from app.services import alert_store


def _setup_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(dsa_alerts, "_RUNTIME", {"repo": None, "data_dir": tmp_path})
    dsa_alerts._AUDIT_DONE_DATE = None


def _add_alert(data_dir, ts_ms, rule_id, symbol, price, op, value):
    alert_store.append(data_dir, {
        "ts": ts_ms, "rule_id": rule_id, "source": "price", "type": "price",
        "symbol": symbol, "name": f"DSA·{symbol}·stop_loss", "message": "x",
        "price": price, "severity": "critical",
        "conditions": [{"field": "close", "op": op, "value": value}],
    })


def test_run_audit_filters_dsa_and_writes_markdown(tmp_path, monkeypatch):
    _setup_runtime(tmp_path, monkeypatch)
    # 当天两条: 一条 DSA· 一条人工规则 (不进审计)
    day_start_ms = int(dsa_alerts.cn_now().replace(hour=10, minute=0, second=0).timestamp() * 1000)
    _add_alert(tmp_path, day_start_ms, "dsa_600519sh_stop_loss", "600519.SH", 30.2, "<=", 30.5)
    alert_store.append(tmp_path, {
        "ts": day_start_ms, "rule_id": "manual_1", "source": "price", "type": "price",
        "symbol": "000001.SZ", "name": "我的手工规则", "message": "y", "price": 1.0,
        "severity": "info", "conditions": [],
    })

    result = dsa_alerts.run_audit()
    today = dsa_alerts.cn_now().date().isoformat()
    assert result["date"] == today and result["total"] == 1
    text = (tmp_path / "dsa_alert_audit" / f"{today}.md").read_text(encoding="utf-8")
    assert "DSA·600519.SH·stop_loss" in text
    assert "我的手工规则" not in text
    # repo=None → 收盘缺失, 判定宁缺不编
    assert "| — |" in text


def test_run_audit_empty_day_still_writes(tmp_path, monkeypatch):
    _setup_runtime(tmp_path, monkeypatch)
    result = dsa_alerts.run_audit()
    assert result["total"] == 0
    assert "共 0 条" in (tmp_path / "dsa_alert_audit" / f"{result['date']}.md").read_text(encoding="utf-8")


def test_verdict_semantics():
    assert dsa_alerts._verdict("<=", 30.5, 30.2) == "收盘仍在线下"
    assert dsa_alerts._verdict("<=", 30.5, 31.0) == "收盘收回线上"
    assert dsa_alerts._verdict(">=", 40.0, 41.0) == "收盘站稳线上"
    assert dsa_alerts._verdict(">=", 40.0, 39.0) == "收盘回落线下"
    assert dsa_alerts._verdict(None, 1.0, 1.0) == "—"
    assert dsa_alerts._verdict("<=", None, 1.0) == "—"
    assert dsa_alerts._verdict("<=", 30.5, None) == "—"
