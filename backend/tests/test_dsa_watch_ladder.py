"""§4.5-1 接近/逼近阶梯规则的单元测试 (dsa_watch 增量, 不动 GLM 既有测试文件)。"""
from __future__ import annotations

from app.custom.dsa_watch import (
    _base_kind,
    _ladder_price,
    _near_buffer,
    _rule_id,
    _rule_name,
    derive_expected_rules,
    diff_rules,
    with_ladder_rules,
    parse_dsa_rule,
    ExpectedSpec,
)

HOLDING_REPORT = {
    "symbol": "600519.SH",
    "created_at": "2026-09-30T08:00:00",
    "points": {"ideal_buy": None, "secondary_buy": 32.0, "stop_loss": 30.5, "take_profit": 40.0},
    "phase_decision": {"risk_conditions": []},
}


def _by_kind(rules):
    return {r.kind: r for r in rules}


def test_ladder_prices_match_dsa_reference_case():
    """DSA 任务书算例: 止损 30.5 + ATR2% → 接近 30.81 / 逼近 30.65。"""
    rules = _by_kind(with_ladder_rules(derive_expected_rules("600519", "600519.SH", True, HOLDING_REPORT), 2.0))
    assert rules["stop_loss"].price == 30.5 and rules["stop_loss"].severity == "critical"
    near = rules["near_stop_loss"]
    assert near.price == 30.81 and near.op == "<=" and near.severity == "info"
    mid = rules["mid_stop_loss"]
    assert mid.price == 30.65 and mid.severity == "warn"
    # op>= 方向镜像: 止盈 40 → 接近 39.6 / 逼近 39.8
    assert rules["near_take_profit"].price == 39.6
    assert rules["mid_take_profit"].price == 39.8
    # add (<=): 32.0 → 接近 32.32 / 逼近 32.16
    assert rules["near_add"].price == 32.32
    assert rules["mid_add"].price == 32.16


def test_ladder_buffer_clamps_and_fallback():
    assert _near_buffer(None) == 0.01                      # 回落默认 1%
    assert _near_buffer(0.2) == 0.003                      # 下限 0.3%
    assert _near_buffer(100.0) == 0.03                     # 上限 3%
    assert _ladder_price(30.5, "<=", "near", 4.0) == 31.11  # ATR4% → 带宽 2%
    # mid: b2 = max(0.3%/2, 0.15%) = 0.15% → 30.5×1.0015 = 30.54575 → 30.55
    assert _ladder_price(30.5, "<=", "mid", 0.2) == 30.55


def test_entry_gets_no_ladder():
    report = {"points": {"ideal_buy": 10.0}, "phase_decision": {}}
    rules = with_ladder_rules(derive_expected_rules("000001", "000001.SZ", False, report), 2.0)
    assert [r.kind for r in rules] == ["entry"]


def test_reduce_ladder_keeps_rank():
    report = {
        "points": {"stop_loss": 30.5, "take_profit": None, "secondary_buy": None},
        "phase_decision": {"risk_conditions": [
            {"kind": "reduce", "text": "减仓1", "price": 31.0},
            {"kind": "reduce", "text": "减仓2", "price": 32.0},
        ]},
    }
    rules = _by_kind(with_ladder_rules(derive_expected_rules("600519", "600519.SH", True, report), 2.0))
    # near_reduce 有两条 (rank 1/2), 用 list 查
    rules_list = with_ladder_rules(derive_expected_rules("600519", "600519.SH", True, report), 2.0)
    near_reduce = [r for r in rules_list if r.kind == "near_reduce"]
    assert len(near_reduce) == 2
    assert {r.rank for r in near_reduce} == {1, 2}
    assert _rule_id("600519.SH", "near_reduce", 2) == "dsa_600519sh_near_reduce_2"
    assert _rule_name("600519", "mid_reduce", 1) == "DSA·600519·mid_reduce·1"
    del rules  # 重名 kind 不进 dict, 仅确认不炸


def test_parse_ladder_rule_names():
    rule = {
        "id": "dsa_600519sh_near_stop_loss", "name": "DSA·600519·near_stop_loss",
        "symbols": ["600519.SH"], "severity": "info", "enabled": True,
        "conditions": [{"field": "close", "op": "<=", "value": 30.81}],
    }
    parsed = parse_dsa_rule(rule)
    assert parsed is not None and parsed.kind == "near_stop_loss" and parsed.rank == 1
    # near_entry 不在阶梯白名单 → 不托管
    bad = dict(rule, name="DSA·600519·near_entry")
    assert parse_dsa_rule(bad) is None
    # 乱仿命名 → 不托管
    assert parse_dsa_rule(dict(rule, name="DSA·600519·near_xxx")) is None


def test_base_kind_helper():
    assert _base_kind("near_stop_loss") == "stop_loss"
    assert _base_kind("mid_reduce") == "reduce"
    assert _base_kind("entry") == "entry"


def test_preserve_main_kind_keeps_its_ladder():
    """槽位级保留 (§4.3-1) 按 base kind 生效: 报告缺 stop_loss 时其阶梯一并保留。"""
    # 报告只给 take_profit → stop_loss 槽位 (含阶梯) 保留
    report = {
        "points": {"stop_loss": None, "take_profit": 40.0, "secondary_buy": None},
        "phase_decision": {"risk_conditions": []},
    }
    spec = ExpectedSpec(
        sym6="600519", suffixed="600519.SH", holding=True,
        rules=with_ladder_rules(derive_expected_rules("600519", "600519.SH", True, report), 2.0),
        preserve_kinds=frozenset({"stop_loss"}),
    )
    existing = [
        {"id": "dsa_600519sh_stop_loss", "name": "DSA·600519·stop_loss",
         "symbols": ["600519.SH"], "severity": "critical", "enabled": True,
         "conditions": [{"field": "close", "op": "<=", "value": 30.5}]},
        {"id": "dsa_600519sh_near_stop_loss", "name": "DSA·600519·near_stop_loss",
         "symbols": ["600519.SH"], "severity": "info", "enabled": True,
         "conditions": [{"field": "close", "op": "<=", "value": 30.81}]},
    ]
    result = diff_rules([spec], existing)
    assert result.removes == []
    assert result.orphan_deletes == []
    # 止盈主规则缺失 → 应创建 (连同其阶梯)
    created_kinds = {er.kind for _, er in result.creates}
    assert {"take_profit", "near_take_profit", "mid_take_profit"} <= created_kinds
