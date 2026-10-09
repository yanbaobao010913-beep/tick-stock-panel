"""Independent review regressions for stale data and path lifecycle."""
from unittest.mock import patch

import test_price_path_monitor as replay

from app.strategy import price_path

_fixed_clock = replay._fixed_clock


def test_old_day_cannot_fire_first_observation(tmp_path):
    eng = replay._engine(tmp_path, [replay._rule("add", "add", "<=", 0.95)])
    assert replay._step(eng, replay._ts(9, 35, 0, replay._yesterday()), 0.80) == []


def test_same_day_stale_first_quote_cannot_fire(tmp_path):
    eng = replay._engine(tmp_path, [replay._rule("add", "add", "<=", 0.95)])
    df = replay._df([(replay.SYM, 0.80, replay._ts(9, 35, 0))])
    with patch("app.strategy.monitor.time.time", return_value=replay._ts(13, 0, 0) / 1000):
        assert eng.evaluate(df) == []


def test_missing_data_interrupts_confirmation(tmp_path):
    eng = replay._engine(tmp_path, [replay._rule("add", "add", "<=", 0.95)])
    replay._step(eng, replay._ts(9, 35, 0), 0.90)
    replay._step(eng, replay._ts(9, 36, 0), 0.97)
    replay._step(eng, replay._ts(9, 36, 30), float("nan"))
    assert replay._step(eng, replay._ts(9, 37, 0), 0.97) == []
    assert any(e["type"] == "price_path_recovery" for e in replay._step(eng, replay._ts(9, 38, 0), 0.97))


def test_rule_replacement_does_not_borrow_previous_low(tmp_path):
    eng = replay._engine(tmp_path, [replay._rule("add", "add", "<=", 0.95)])
    replay._step(eng, replay._ts(9, 35, 0), 0.80)
    eng.set_rules([])
    eng.set_rules([replay._rule("add", "add", "<=", 0.95)])
    replay._step(eng, replay._ts(9, 36, 0), 0.84)
    assert not any(e["type"] == "price_path_bounce" for e in replay._step(eng, replay._ts(9, 37, 0), 0.84))


def test_stale_rule_snapshot_cannot_resurrect_deleted_state(tmp_path):
    rule = replay._rule("add", "add", "<=", 0.95)
    eng = replay._engine(tmp_path, [rule])
    eng.set_rules([])
    tracker = eng._price_path
    tracker.begin_round()
    assert tracker.evaluate_rule(replay._df([(replay.SYM, 0.80, replay._ts(9, 35, 0))]), rule, replay._ts(9, 35, 0) / 1000) == []
    assert tracker._rules == {}


def test_malformed_state_root_does_not_break_reload(tmp_path):
    target = tmp_path / price_path.STATE_DIR / price_path.STATE_FILE
    target.parent.mkdir(parents=True)
    target.write_text("[]", encoding="utf-8")
    eng = replay._engine(tmp_path, [replay._rule("add", "add", "<=", 0.95)])
    assert len(replay._step(eng, replay._ts(9, 35, 0), 0.80)) == 1


def test_different_event_directions_do_not_merge():
    from test_price_path_merge_paper import _ev

    events = [_ev("main", "add", 0.95), _ev("near", "near_add", 0.96, info=True, etype="price_path_recovery")]
    assert len(price_path.merge_round_events(events)) == 2


def test_paper_rejects_information_type_even_without_flag(tmp_path):
    from test_price_path_merge_paper import _auto_rule, _ev

    from app.strategy import paper, paper_auto

    paper.create_account(tmp_path, 1_000_000)
    _auto_rule(tmp_path, "add")
    assert paper_auto.on_rule_events(tmp_path, [_ev("add", "add", 30.0, etype="price_path_recovery")]) == []


def test_missing_timestamp_interrupts_confirmation(tmp_path):
    eng = replay._engine(tmp_path, [replay._rule("add", "add", "<=", 0.95)])
    replay._step(eng, replay._ts(9, 35, 0), 0.90)
    replay._step(eng, replay._ts(9, 36, 0), 0.97)
    replay._step(eng, None, 0.97)
    assert replay._step(eng, replay._ts(9, 37, 0), 0.97) == []
    assert any(e["type"] == "price_path_recovery" for e in replay._step(eng, replay._ts(9, 38, 0), 0.97))


def test_empty_snapshot_interrupts_confirmation(tmp_path):
    import polars as pl

    eng = replay._engine(tmp_path, [replay._rule("add", "add", "<=", 0.95)])
    replay._step(eng, replay._ts(9, 35, 0), 0.90)
    replay._step(eng, replay._ts(9, 36, 0), 0.97)
    assert eng.evaluate(pl.DataFrame()) == []
    assert replay._step(eng, replay._ts(9, 37, 0), 0.97) == []
    assert any(e["type"] == "price_path_recovery" for e in replay._step(eng, replay._ts(9, 38, 0), 0.97))


def test_disk_write_does_not_hold_quote_state_lock(tmp_path, monkeypatch):
    import threading

    eng = replay._engine(tmp_path, [replay._rule("add", "add", "<=", 0.95)])
    entered, release, evaluated = threading.Event(), threading.Event(), threading.Event()
    original = price_path.atomic_write_text

    def delayed(path, content):
        if path == tmp_path / price_path.STATE_DIR / price_path.STATE_FILE:
            entered.set()
            release.wait(5)
        original(path, content)

    monkeypatch.setattr(price_path, "atomic_write_text", delayed)
    try:
        replay._step(eng, replay._ts(9, 35, 0), 0.90)
        assert entered.wait(2)

        def evaluate():
            replay._step(eng, replay._ts(9, 36, 0), 0.80)
            evaluated.set()

        worker = threading.Thread(target=evaluate)
        worker.start()
        assert evaluated.wait(2)
    finally:
        release.set()
        eng._price_path.flush(force=True)


def test_dsa_old_rules_are_migrated_without_replacing_preferences():
    from test_dsa_watch import _mk_expected, _mk_spec

    from app.custom import dsa_watch

    expected = _mk_expected("stop_loss", 1, "<=", 31.0, "critical")
    spec = _mk_spec("600460", [expected])
    rule = dsa_watch._build_rule(spec, expected)
    rule.pop("price_path")
    rule.pop("path_kind")
    rule.update(enabled=False, webhook_channels=["channel1"])
    diff = dsa_watch.diff_rules([spec], [rule])
    assert len(diff.updates) == 1
    assert diff.updates[0][0]["enabled"] is False
    assert diff.updates[0][0]["webhook_channels"] == ["channel1"]
