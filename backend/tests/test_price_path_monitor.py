"""价格路径告警 (price_path 模式) — MonitorRuleEngine 真实路径重放测试。

背景 (logs/glm-deepv/task.md): DSA 自动点位规则统一 cooldown_seconds=86400,
上午触发后下午的反弹/收复/再穿越整天被压。本文件用带 quote_ts 的合成快照
在 MonitorRuleEngine.evaluate 真实路径上重放深 V、镜像、午休、断流、重启等
场景; 未实现价格路径状态机前这些用例必须失败 (cooldown 压新周期 / 无收复 /
无反弹事件), 实现后通过。

时间口径: 固定回放日期与服务端时钟, 使用北京连续竞价秒差; 测试在任意时刻运行结果确定。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from datetime import time as dt_time

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.strategy import monitor_rules
from app.strategy.monitor import MonitorRuleEngine

SYM = "600000.SH"
SYM2 = "000001.SZ"
REPLAY_DAY = date(2026, 10, 9)
_replay_now = 0


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    from app.strategy import monitor, price_path

    global _replay_now
    _replay_now = _ts(9, 30, 0, REPLAY_DAY)
    monkeypatch.setattr(monitor, "cn_today", lambda: REPLAY_DAY)
    monkeypatch.setattr(price_path, "cn_today", lambda: REPLAY_DAY)
    monkeypatch.setattr(monitor.time, "time", lambda: _replay_now / 1000 + 1)


# ================================================================
# 构造工具
# ================================================================

def _ts(h: int, m: int, s: int, day: date | None = None) -> int:
    """北京墙上钟 → epoch ms (默认真实今天, 会话秒差与星期无关)。"""
    day = day or REPLAY_DAY
    return int(datetime.combine(day, dt_time(h, m, s), tzinfo=CN_TZ).timestamp() * 1000)


def _yesterday() -> date:
    from datetime import timedelta

    return REPLAY_DAY - timedelta(days=1)


def _df(rows: list[tuple[str, float, int | None]], *, with_ts: bool = True) -> pl.DataFrame:
    """rows: [(symbol, close, quote_ts_ms|None)] → 引擎评估用快照。"""
    global _replay_now
    today_times = [r[2] for r in rows if r[2] and datetime.fromtimestamp(r[2] / 1000, CN_TZ).date() == REPLAY_DAY]
    if today_times:
        _replay_now = max(today_times)
    data: dict[str, list] = {
        "symbol": [r[0] for r in rows],
        "close": [r[1] for r in rows],
        "change_pct": [0.0] * len(rows),
    }
    if with_ts:
        data["quote_ts"] = [r[2] for r in rows]
    return pl.DataFrame(data)


def _rule(
    rid: str, kind: str, op: str, value: float, sym: str = SYM,
    severity: str = "warn", asset_type: str = "stock",
) -> dict:
    label = {
        "stop_loss": "止损", "take_profit": "止盈", "add": "加仓",
        "reduce": "减仓", "entry": "介入",
    }.get(kind, kind)
    arrow = "≤" if op == "<=" else "≥"
    return monitor_rules.normalize({
        "id": rid,
        "name": f"DSA·{sym[:6]}·{kind}",
        "type": "price",
        "scope": "symbols",
        "symbols": [sym],
        "asset_type": asset_type,
        "conditions": [{"field": "close", "op": op, "value": value}],
        "severity": severity,
        "cooldown_seconds": 86400,
        "enabled": True,
        "price_path": True,
        "path_kind": kind,
        "message": f"DSA {label}提醒: 收盘价 {arrow} {value:.2f}",
    })


def _engine(tmp_path, rules: list[dict]) -> MonitorRuleEngine:
    eng = MonitorRuleEngine()
    eng.set_data_dir(tmp_path)
    eng.set_rules(rules)
    return eng


def _step(eng: MonitorRuleEngine, ts: int, close: float, sym: str = SYM, *,
          with_ts: bool = True, asset: str = "stock") -> list[dict]:
    return eng.evaluate(_df([(sym, close, ts)], with_ts=with_ts), asset_type=asset)


def _types(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


def _triggered(events: list[dict], rid: str) -> list[dict]:
    return [e for e in events if e["rule_id"] == rid and e["type"] == "price"]


def _info(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("info_only")]


def _force_flush(eng: MonitorRuleEngine) -> None:
    tracker = getattr(eng, "_price_path", None)
    if tracker is not None:
        tracker.flush(force=True)


def _state_files(tmp_path) -> list:
    d = tmp_path / "user_data" / "price_path"
    return sorted(d.glob("*.json")) if d.exists() else []


# ================================================================
# 矩阵 1: 深 V 主线重放
# 1.00 → 0.95(首次触价) → 0.90(独立止损) → 0.80(只记低点) →
# 0.83(尚未收复 0.95 也报反弹) → 确认收复 0.90/0.95 → 再跌破 0.95(再报)
# ================================================================

def test_deep_v_full_replay(tmp_path):
    add = _rule("dsa_a_add", "add", "<=", 0.95, severity="warn")
    stop = _rule("dsa_a_stop", "stop_loss", "<=", 0.90, severity="critical")
    eng = _engine(tmp_path, [add, stop])

    # 09:35 1.00: 两条线均未触发
    assert _step(eng, _ts(9, 35, 0), 1.00) == []
    # 09:40 0.95: 加仓线首次触价 (含等号), 立即报, 不等一分钟
    ev = _step(eng, _ts(9, 40, 0), 0.95)
    assert _types(ev) == ["price"] and ev[0]["rule_id"] == "dsa_a_add"
    assert ev[0]["price"] == 0.95 and ev[0]["severity"] == "warn"
    # 09:45 0.90: 独立止损事件 (风险不压加仓), 加仓线同区间不重复刷
    ev = _step(eng, _ts(9, 45, 0), 0.90)
    assert _types(ev) == ["price"] and ev[0]["rule_id"] == "dsa_a_stop"
    assert ev[0]["severity"] == "critical"
    # 09:50 0.80: 只记低点, 无事件
    assert _step(eng, _ts(9, 50, 0), 0.80) == []
    # 09:55 0.83: 反弹确认起点 (0.83 >= 0.80*1.03=0.824), 尚未满 60s
    assert _info(_step(eng, _ts(9, 55, 0), 0.83)) == []
    # 09:57 0.84: 反弹确认 (两条新鲜快照 + 120s 连续交易时间) — 未收复 0.90/0.95 也报
    ev = _step(eng, _ts(9, 57, 0), 0.84)
    bounce = [e for e in ev if e["type"] == "price_path_bounce"]
    assert len(bounce) == 1
    assert bounce[0]["info_only"] is True and bounce[0]["symbol"] == SYM
    assert "反弹" in bounce[0]["message"] and "非买入指令" in bounce[0]["message"]
    assert bounce[0]["price"] == 0.84
    # 10:10 0.96: 两条线同时进入收复确认起点 (0.96 = 0.95+0.01 缓冲线, 等号有效;
    # 0.90 线缓冲 0.91), 未满 60s 不报
    assert _step(eng, _ts(10, 10, 0), 0.96) == []
    # 10:12 0.97: 收复确认完成 → 两条线各一条信息事件
    ev = _step(eng, _ts(10, 12, 0), 0.97)
    rec = [e for e in ev if e["type"] == "price_path_recovery"]
    assert {e["rule_id"] for e in rec} == {"dsa_a_add", "dsa_a_stop"}
    assert all(e["info_only"] is True for e in rec)
    assert all("收复" in e["message"] for e in rec)
    # 10:20 0.949: 再次跌破 0.95 → 加仓线再报 (整天冷却不压新周期); 止损线未再破
    ev = _step(eng, _ts(10, 20, 0), 0.949)
    trig = _triggered(ev, "dsa_a_add")
    assert len(trig) == 1 and trig[0]["price"] == 0.949
    assert _triggered(ev, "dsa_a_stop") == []


def test_same_interval_no_repeat_and_dead_zone_quiet(tmp_path):
    """同区间持续下跌不重复; 阈值与缓冲线之间的死区不触发收复确认。"""
    add = _rule("dsa_a_add", "add", "<=", 0.95)
    eng = _engine(tmp_path, [add])
    assert _step(eng, _ts(9, 35, 0), 1.00) == []
    assert len(_step(eng, _ts(9, 36, 0), 0.95)) == 1
    # 同区间一路下跌 + 窄幅抖动 (0.95~0.955 死区往返) 全程无新事件
    for i, px in enumerate([0.94, 0.93, 0.955, 0.945, 0.955, 0.92, 0.91]):
        assert _step(eng, _ts(9, 37 + i, 0), px) == []
    # 长时间停在死区也不产生收复 (未达缓冲线 0.96)
    assert _step(eng, _ts(10, 0, 0), 0.955) == []
    assert _step(eng, _ts(10, 5, 0), 0.9555) == []


def test_bounce_unconfirmed_cancel_and_new_low_new_segment(tmp_path):
    """反弹不足退回取消; 有意义新低后允许第二段反弹; 横盘/连续新低不误报。"""
    stop = _rule("dsa_a_stop", "stop_loss", "<=", 0.90)
    eng = _engine(tmp_path, [stop])
    assert _step(eng, _ts(9, 35, 0), 1.00) == []
    assert len(_step(eng, _ts(9, 36, 0), 0.85)) == 1  # 首次触价, 低点 0.85
    # 反弹到 0.878 (>=0.8755) 起确认, 但回落 0.87 (<0.8755) → 取消
    assert _info(_step(eng, _ts(9, 40, 0), 0.878)) == []
    assert _info(_step(eng, _ts(9, 41, 0), 0.87)) == []   # 退回 → 取消确认
    assert _info(_step(eng, _ts(9, 50, 0), 0.876)) == []  # 重新起确认
    assert _info(_step(eng, _ts(9, 51, 0), 0.873)) == []  # 又退回 → 仍未确认
    # 横盘在低点附近 (不满足 +3%) 永不报
    for hh, mm in [(9, 55), (9, 58), (10, 0), (10, 3)]:
        assert _info(_step(eng, _ts(hh, mm, 0), 0.86)) == []
    # 有意义新低 0.80 (比 0.85 低 >0.5% 且 >=1tick): 低点更新 → 0.824/0.825 不再算反弹
    assert _step(eng, _ts(10, 10, 0), 0.80) == []
    assert _info(_step(eng, _ts(10, 12, 0), 0.824)) == []
    assert _info(_step(eng, _ts(10, 13, 0), 0.82)) == []  # 退回
    # 第二段反弹: 0.83 >= 0.80*1.03=0.824 起确认, 0.84 确认 → 第二次反弹事件
    assert _info(_step(eng, _ts(10, 20, 0), 0.83)) == []
    ev = _step(eng, _ts(10, 21, 30), 0.84)
    bounce = [e for e in ev if e["type"] == "price_path_bounce"]
    assert len(bounce) == 1
    # 未创新低前即使再反弹也不再报 (同一反弹段只报一次)
    # 0.75 <= 0.80-0.01=0.79 → 有意义新低, 段重开 (低点 0.75); 0.76 < 0.7725 未达
    assert _info(_step(eng, _ts(10, 25, 0), 0.75)) == []
    assert _info(_step(eng, _ts(10, 30, 0), 0.76)) == []
    assert _info(_step(eng, _ts(10, 32, 0), 0.78)) == []  # >=0.7725 起确认
    ev = _step(eng, _ts(10, 33, 30), 0.78)
    assert len([e for e in ev if e["type"] == "price_path_bounce"]) == 1


def test_bounce_requires_downside_interval_low(tmp_path):
    """只有曾在下行触发区间观测到低点才开启反弹观察; >= 规则不产生反弹。"""
    tp = _rule("dsa_a_tp", "take_profit", ">=", 40.0)
    eng = _engine(tmp_path, [tp])
    assert _step(eng, _ts(9, 35, 0), 39.9) == []
    assert len(_step(eng, _ts(9, 36, 0), 41.0)) == 1  # 首次触价 (>=)
    # 从 41 回落再反弹: 无任何下行区间 → 永不报低位反弹。
    # (回落侧单样本即被拉回/进死区, 不确认回落, 只验证反弹永不出现)
    for m, px in [(40, 39.5), (41, 41.5), (43, 39.7), (45, 39.85), (47, 41.0), (50, 43.0)]:
        assert _info(_step(eng, _ts(9, m, 0), px)) == []


# ================================================================
# 矩阵 3: >= 镜像、等号、stock/ETF tick
# ================================================================

def test_gte_mirror_trigger_recovery_retrigger(tmp_path):
    tp = _rule("dsa_a_tp", "take_profit", ">=", 40.0)
    eng = _engine(tmp_path, [tp])
    assert _step(eng, _ts(9, 35, 0), 39.9) == []
    # 等号触发
    ev = _step(eng, _ts(9, 36, 0), 40.0)
    assert _types(ev) == ["price"] and ev[0]["rule_id"] == "dsa_a_tp"
    # 区间内再涨不重复
    assert _step(eng, _ts(9, 40, 0), 41.0) == []
    # 回落到缓冲线下方: 40*0.995=39.8 → 取 min(39.8, 40-0.01)=39.8; 39.5 起确认
    assert _step(eng, _ts(9, 50, 0), 39.5) == []
    ev = _step(eng, _ts(9, 52, 0), 39.4)
    rec = [e for e in ev if e["type"] == "price_path_recovery"]
    assert len(rec) == 1 and "回落" in rec[0]["message"]
    # 再次上穿 ≥40 → 再报
    ev = _step(eng, _ts(10, 5, 0), 40.1)
    assert _types(ev) == ["price"] and ev[0]["rule_id"] == "dsa_a_tp"
    # 死区 (39.8, 40) 不触发收复也不触发再报
    assert _step(eng, _ts(10, 10, 0), 39.9) == []


def test_etf_tick_and_equality_boundaries(tmp_path):
    """ETF tick 0.001: 缓冲 = max(0.5%, 1tick); 缓冲线上的等号即收复侧。"""
    etf = _rule("dsa_e_add", "add", "<=", 0.500, sym="510300.SH", asset_type="etf")
    eng = _engine(tmp_path, [etf])
    sym = "510300.SH"
    assert _step(eng, _ts(9, 35, 0), 0.52, sym, asset="etf") == []
    assert len(_step(eng, _ts(9, 36, 0), 0.500, sym, asset="etf")) == 1  # 等号触发
    # 缓冲线 0.5 + max(0.0025, 0.001) = 0.5025; 恰好 0.5025 (等号) 起确认
    assert _step(eng, _ts(9, 40, 0), 0.5025, sym, asset="etf") == []
    ev = _step(eng, _ts(9, 41, 30), 0.503, sym, asset="etf")
    rec = [e for e in ev if e["type"] == "price_path_recovery"]
    assert len(rec) == 1
    # stock tick 0.01 主导: 0.95 缓冲线 0.96 (0.5%=0.00475 < 0.01)
    (tmp_path / "s").mkdir()
    st = _rule("dsa_s_add", "add", "<=", 0.95)
    eng2 = _engine(tmp_path / "s", [st])
    assert _step(eng2, _ts(9, 35, 0), 1.0) == []
    assert len(_step(eng2, _ts(9, 36, 0), 0.95)) == 1
    assert _step(eng2, _ts(9, 40, 0), 0.9559) == []  # 死区
    assert _step(eng2, _ts(9, 42, 0), 0.96) == []    # 等号起确认
    ev = _step(eng2, _ts(9, 43, 30), 0.961)
    assert len([e for e in ev if e["type"] == "price_path_recovery"]) == 1


def test_gap_through_multiple_lines_keeps_independent_kinds(tmp_path):
    """跳空穿多线: 引擎层各线独立触发 (合并在 quote_service 统一出口, 见
    test_price_path_merge_paper.py); 不同 kind (加仓/止损) 互不压制。"""
    add = _rule("dsa_a_add", "add", "<=", 32.0)
    stop = _rule("dsa_a_stop", "stop_loss", "<=", 30.5)
    eng = _engine(tmp_path, [add, stop])
    assert _step(eng, _ts(9, 35, 0), 33.0) == []
    ev = _step(eng, _ts(9, 36, 0), 30.4)  # 一步穿两线
    assert sorted(e["rule_id"] for e in _triggered(ev, "dsa_a_add") + _triggered(ev, "dsa_a_stop")) == \
        ["dsa_a_add", "dsa_a_stop"]
    # 加仓文案带风险提示: 现价 30.4 仍低于主止损线 30.5
    add_ev = _triggered(ev, "dsa_a_add")[0]
    assert "止损" in add_ev["message"]


# ================================================================
# 矩阵 4: 午休不计入确认时间
# ================================================================

def test_lunch_break_not_counted_in_confirmation(tmp_path):
    add = _rule("dsa_a_add", "add", "<=", 0.95)
    eng = _engine(tmp_path, [add])
    assert _step(eng, _ts(9, 35, 0), 1.0) == []
    assert len(_step(eng, _ts(9, 40, 0), 0.90)) == 1
    # 11:29:40 起收复确认 (0.96)
    assert _step(eng, _ts(11, 29, 40), 0.96) == []
    # 13:00:39: 会话内秒差 = 7239-7180 = 59s < 60s, 两条快照仍不确认
    assert _step(eng, _ts(13, 0, 39), 0.97) == []
    # 13:00:41: 61s ≥ 60s → 确认
    ev = _step(eng, _ts(13, 0, 41), 0.97)
    assert len([e for e in ev if e["type"] == "price_path_recovery"]) == 1


def test_bounce_confirmation_across_lunch(tmp_path):
    stop = _rule("dsa_a_stop", "stop_loss", "<=", 0.90)
    eng = _engine(tmp_path, [stop])
    assert _step(eng, _ts(9, 35, 0), 1.0) == []
    assert len(_step(eng, _ts(9, 36, 0), 0.80)) == 1
    assert _info(_step(eng, _ts(11, 29, 40), 0.83)) == []       # 0.824 起
    assert _info(_step(eng, _ts(13, 0, 39), 0.84)) == []        # 59s
    assert len(_info(_step(eng, _ts(13, 0, 41), 0.84))) == 1    # 61s


# ================================================================
# 矩阵 5: 脏快照 / 断流 / 重启 / 状态损坏 / reload / 变更 / 跨日
# ================================================================

def test_duplicate_reverse_stale_illegal_snapshots_ignored(tmp_path):
    add = _rule("dsa_a_add", "add", "<=", 0.95)
    eng = _engine(tmp_path, [add])
    assert _step(eng, _ts(9, 35, 0), 1.0) == []
    assert len(_step(eng, _ts(9, 36, 0), 0.90)) == 1
    # 重复同 ts: 不推进确认 (收复确认需要 ≥2 条时间递增快照)
    assert _step(eng, _ts(9, 40, 0), 0.96) == []
    for _ in range(5):
        assert _step(eng, _ts(9, 40, 0), 0.97) == []
    # 倒序 (更早 ts) 快照: 忽略
    assert _step(eng, _ts(9, 38, 0), 0.97) == []
    # 旧日 ts: 不推进行情状态, 并打断连续确认
    assert _step(eng, _ts(9, 40, 0, day=_yesterday()), 0.10) == []
    # 恢复有效行情后从零确认, 不把旧日快照充当连续观测
    assert _step(eng, _ts(9, 42, 0), 0.97) == []
    ev = _step(eng, _ts(9, 43, 0), 0.97)
    assert len([e for e in ev if e["type"] == "price_path_recovery"]) == 1


def test_illegal_prices_do_not_crash_or_update_state(tmp_path):
    add = _rule("dsa_a_add", "add", "<=", 0.95)
    eng = _engine(tmp_path, [add])
    assert _step(eng, _ts(9, 35, 0), 1.0) == []
    assert len(_step(eng, _ts(9, 36, 0), 0.90)) == 1
    for bad in (0.0, -1.0, float("nan"), float("inf"), float("-inf")):
        assert _step(eng, _ts(9, 40, 0), bad) == []
    # null close
    df = pl.DataFrame({"symbol": [SYM], "close": [None], "change_pct": [0.0], "quote_ts": [_ts(9, 41, 0)]})
    assert eng.evaluate(df, asset_type="stock") == []
    # 状态未被污染: 合法快照照常推进
    assert _step(eng, _ts(9, 42, 0), 0.96) == []
    ev = _step(eng, _ts(9, 44, 0), 0.97)
    assert len([e for e in ev if e["type"] == "price_path_recovery"]) == 1


def test_missing_timestamp_provider_compat(tmp_path):
    """provider 不给 timestamp: 首次触价照常 (兼容), 但收复/反弹无法确认。"""
    add = _rule("dsa_a_add", "add", "<=", 0.95)
    eng = _engine(tmp_path, [add])
    assert _step(eng, _ts(9, 35, 0), 1.0, with_ts=False) == []
    assert len(_step(eng, _ts(9, 36, 0), 0.90, with_ts=False)) == 1
    # 大量无 ts 快照在另一侧: 不确认收复 (不把服务端轮询时间当新行情)
    for m in range(40, 55):
        assert _step(eng, _ts(9, m, 0), 0.97, with_ts=False) == []
    # 有 ts 快照恢复后: 从零起确认 (不冒充全程)
    assert _step(eng, _ts(9, 56, 0), 0.97) == []
    ev = _step(eng, _ts(9, 58, 0), 0.97)
    assert len([e for e in ev if e["type"] == "price_path_recovery"]) == 1


def test_long_disconnection_resets_confirmation(tmp_path):
    add = _rule("dsa_a_add", "add", "<=", 0.95)
    eng = _engine(tmp_path, [add])
    assert _step(eng, _ts(9, 35, 0), 1.0) == []
    assert len(_step(eng, _ts(9, 36, 0), 0.90)) == 1
    assert _step(eng, _ts(10, 0, 0), 0.96) == []    # 确认起点
    # 30 分钟断流后恢复: 不能重连即冒充全程确认
    assert _step(eng, _ts(10, 30, 0), 0.97) == []
    # 恢复后重新起确认 → 第二条快照确认
    ev = _step(eng, _ts(10, 32, 0), 0.97)
    assert len([e for e in ev if e["type"] == "price_path_recovery"]) == 1


def test_restart_recovers_state_and_low(tmp_path):
    add = _rule("dsa_a_add", "add", "<=", 0.95)
    eng1 = _engine(tmp_path, [add])
    assert _step(eng1, _ts(9, 35, 0), 1.0) == []
    assert len(_step(eng1, _ts(9, 36, 0), 0.90)) == 1
    assert _step(eng1, _ts(9, 40, 0), 0.80) == []  # 低点 0.80
    assert _info(_step(eng1, _ts(9, 42, 0), 0.83)) == []  # 反弹确认起点
    _force_flush(eng1)
    # 重启: 新引擎同 data_dir + 同规则 reload
    eng2 = _engine(tmp_path, [add])
    ev = _step(eng2, _ts(9, 44, 0), 0.84)
    bounce = [e for e in ev if e["type"] == "price_path_bounce"]
    assert len(bounce) == 1  # 低点/确认进度来自持久化, 不虚构也不丢段
    # 已报警状态同样恢复: 同区间再跌不重复
    assert _step(eng2, _ts(9, 46, 0), 0.81) == []


def test_corrupt_state_file_degrades_to_fresh(tmp_path):
    add = _rule("dsa_a_add", "add", "<=", 0.95)
    eng1 = _engine(tmp_path, [add])
    assert _step(eng1, _ts(9, 35, 0), 1.0) == []
    assert len(_step(eng1, _ts(9, 36, 0), 0.90)) == 1
    _force_flush(eng1)
    files = _state_files(tmp_path)
    assert files, "实现应落盘路径状态文件"
    files[0].write_text("{ not json", encoding="utf-8")  # 损坏
    eng2 = _engine(tmp_path, [add])
    # 坏文件不崩: 保守降级为全新状态, 有效首轮触价照常报
    ev = _step(eng2, _ts(9, 40, 0), 0.90)
    assert _types(ev) == ["price"]


def test_same_config_reload_keeps_state(tmp_path):
    add = _rule("dsa_a_add", "add", "<=", 0.95)
    eng = _engine(tmp_path, [add])
    assert _step(eng, _ts(9, 35, 0), 1.0) == []
    assert len(_step(eng, _ts(9, 36, 0), 0.90)) == 1
    # 对账式 reload (同配置): 状态保留
    eng.set_rules([_rule("dsa_a_add", "add", "<=", 0.95)])
    assert _step(eng, _ts(9, 40, 0), 0.90) == []  # 同区间不重复
    # 反弹段不重复
    assert _step(eng, _ts(9, 42, 0), 0.80) == []
    assert _info(_step(eng, _ts(9, 44, 0), 0.83)) == []
    ev = _step(eng, _ts(9, 46, 0), 0.84)
    assert len([e for e in ev if e["type"] == "price_path_bounce"]) == 1


def test_threshold_change_resets_state(tmp_path):
    eng = _engine(tmp_path, [_rule("dsa_a_add", "add", "<=", 0.95)])
    assert _step(eng, _ts(9, 35, 0), 1.0) == []
    assert len(_step(eng, _ts(9, 36, 0), 0.90)) == 1
    # 改价: 签名变化 → 旧状态失效, 新周期立即触发
    eng.set_rules([_rule("dsa_a_add", "add", "<=", 0.85)])
    ev = _step(eng, _ts(9, 40, 0), 0.84)
    assert _types(ev) == ["price"] and ev[0]["rule_id"] == "dsa_a_add"


def test_disable_and_reenable_resets_state(tmp_path):
    rule = _rule("dsa_a_add", "add", "<=", 0.95)
    eng = _engine(tmp_path, [rule])
    assert _step(eng, _ts(9, 35, 0), 1.0) == []
    assert len(_step(eng, _ts(9, 36, 0), 0.90)) == 1
    # 关闭 (set_rules 只留启用规则) → 状态清除
    disabled = dict(rule, enabled=False)
    eng.set_rules([disabled])
    assert _step(eng, _ts(9, 40, 0), 0.90) == []
    # 再启用: 全新状态, 首轮触价照常报
    eng.set_rules([rule])
    ev = _step(eng, _ts(9, 42, 0), 0.90)
    assert _types(ev) == ["price"]


def test_delete_rule_drops_state(tmp_path):
    rule = _rule("dsa_a_add", "add", "<=", 0.95)
    eng = _engine(tmp_path, [rule])
    assert _step(eng, _ts(9, 35, 0), 1.0) == []
    assert len(_step(eng, _ts(9, 36, 0), 0.90)) == 1
    _force_flush(eng)
    eng.remove_rule("dsa_a_add")
    _force_flush(eng)
    # 重启后已删除规则不留状态; 重新加回 → 全新状态立即触发
    eng2 = _engine(tmp_path, [rule])
    ev = _step(eng2, _ts(9, 40, 0), 0.90)
    assert _types(ev) == ["price"]


def test_new_trading_day_resets_state(tmp_path):
    add = _rule("dsa_a_add", "add", "<=", 0.95)
    eng = _engine(tmp_path, [add])
    assert _step(eng, _ts(9, 35, 0), 1.0) == []
    assert len(_step(eng, _ts(9, 36, 0), 0.90)) == 1
    _force_flush(eng)
    # 状态文件日期改成昨天 → 视为跨日, 全部重置
    files = _state_files(tmp_path)
    assert files
    state = json.loads(files[0].read_text(encoding="utf-8"))
    state["date"] = "2000-01-01"
    files[0].write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    eng2 = _engine(tmp_path, [add])
    ev = _step(eng2, _ts(9, 40, 0), 0.90)
    assert _types(ev) == ["price"]


def test_first_upgrade_does_not_fabricate_low(tmp_path):
    """首次启用无旧状态: 价格已低于线, 只按上线后的新观测产生低点/反弹。"""
    add = _rule("dsa_a_add", "add", "<=", 0.95)
    eng = _engine(tmp_path, [add])
    # 上线即已在区间内: 首轮触价照常, 低点从 0.80 (而非日 low) 起算
    ev = _step(eng, _ts(10, 0, 0), 0.80)
    assert _types(ev) == ["price"]
    # 0.824 起确认 (低点=0.80, 非更早的日低)
    assert _info(_step(eng, _ts(10, 2, 0), 0.824)) == []
    assert len(_info(_step(eng, _ts(10, 3, 30), 0.83))) == 1


# ================================================================
# 矩阵 6: 未启用新模式的规则维持既有行为
# ================================================================

def test_plain_price_rule_keeps_legacy_cooldown_behavior(tmp_path):
    """人工 price 规则 (无 price_path): 旧 cooldown 语义不变 — 同日二次穿越不报。"""
    rule = monitor_rules.normalize({
        "id": "manual_px", "name": "手动价格提醒", "type": "price",
        "scope": "symbols", "symbols": [SYM], "asset_type": "stock",
        "conditions": [{"field": "close", "op": "<=", "value": 0.95}],
        "cooldown_seconds": 86400, "enabled": True,
    })
    eng = _engine(tmp_path, [rule])
    assert _step(eng, _ts(9, 35, 0), 1.0) == []
    assert len(_step(eng, _ts(9, 36, 0), 0.90)) == 1
    assert _step(eng, _ts(9, 38, 0), 0.97) == []   # 旧逻辑: 无收复事件
    assert _step(eng, _ts(9, 40, 0), 0.90) == []   # 冷却期内不再报
    assert _info(_step(eng, _ts(9, 42, 0), 0.96)) == []  # 无信息事件


def test_multiple_down_rules_single_bounce(tmp_path):
    """同 symbol 多条下行线: 反弹只按 symbol 报一次。"""
    add = _rule("dsa_a_add", "add", "<=", 0.95)
    stop = _rule("dsa_a_stop", "stop_loss", "<=", 0.90)
    eng = _engine(tmp_path, [add, stop])
    assert _step(eng, _ts(9, 35, 0), 1.0) == []
    assert len(_step(eng, _ts(9, 36, 0), 0.85)) == 2  # 两线同轮各自触发
    assert _info(_step(eng, _ts(9, 40, 0), 0.80)) == []   # 低点 0.80
    assert _info(_step(eng, _ts(9, 42, 0), 0.84)) == []   # 反弹确认起点
    ev = _step(eng, _ts(9, 44, 0), 0.85)
    bounce = [e for e in ev if e["type"] == "price_path_bounce"]
    assert len(bounce) == 1  # 只报一次, 不因多线重复


def test_two_symbols_independent_bounce(tmp_path):
    a = _rule("dsa_a_stop", "stop_loss", "<=", 0.90, sym=SYM)
    b = _rule("dsa_b_stop", "stop_loss", "<=", 9.0, sym=SYM2)
    eng = _engine(tmp_path, [a, b])
    df = _df([(SYM, 1.0, _ts(9, 35, 0)), (SYM2, 10.0, _ts(9, 35, 0))])
    assert eng.evaluate(df, asset_type="stock") == []
    df = _df([(SYM, 0.80, _ts(9, 36, 0)), (SYM2, 8.5, _ts(9, 36, 0))])
    assert len(eng.evaluate(df, asset_type="stock")) == 2
    # 两 symbol 各自起反弹确认; A 随后退回取消, B 确认 → 只有 B 报
    df = _df([(SYM, 0.84, _ts(9, 38, 0)), (SYM2, 9.5, _ts(9, 38, 0))])
    assert [e["type"] for e in eng.evaluate(df, asset_type="stock") if e.get("info_only")] == []
    df = _df([(SYM, 0.81, _ts(9, 40, 0)), (SYM2, 9.85, _ts(9, 40, 0))])
    ev = eng.evaluate(df, asset_type="stock")
    bounce = [e for e in ev if e["type"] == "price_path_bounce"]
    assert [e["symbol"] for e in bounce] == [SYM2]
    # A 重新起确认并完成 → A 也报 (各自独立)
    assert _info(_step(eng, _ts(9, 42, 0), 0.84)) == []
    ev = _step(eng, _ts(9, 44, 0), 0.85)
    assert [e["symbol"] for e in ev if e["type"] == "price_path_bounce"] == [SYM]
