"""Journal durability, ordering and recovery."""

from __future__ import annotations

import json

import pytest

from atlas.bus.events import Event, EventKind
from atlas.bus.journal import Journal
from atlas.core.clock import FrozenClock
from atlas.core.decision import DecisionRecord, make_decision_id
from atlas.core.enums import DecisionOutcome


def test_seq_is_monotonic_and_gapless(tmp_path):
    with Journal(tmp_path, run_id="r1", clock=FrozenClock(1000)) as j:
        events = [j.append(EventKind.HEARTBEAT, {"i": i}) for i in range(50)]
    assert [e.seq for e in events] == list(range(1, 51))


def test_seq_recovers_across_reopen(tmp_path):
    with Journal(tmp_path, run_id="r1") as j:
        for i in range(5):
            j.append(EventKind.HEARTBEAT, {"i": i})
    with Journal(tmp_path, run_id="r2") as j2:
        e = j2.append(EventKind.HEARTBEAT, {"i": 99})
    assert e.seq == 6, "reopened journal must continue the sequence, not restart it"


def test_torn_final_line_is_tolerated(tmp_path):
    """A hard kill mid-write leaves a truncated line. Recovery must skip it, not crash."""
    with Journal(tmp_path, run_id="r1") as j:
        for i in range(3):
            j.append(EventKind.HEARTBEAT, {"i": i})
    path = tmp_path / "events.jsonl"
    with path.open("a") as fh:
        fh.write('{"seq": 4, "ts": 1, "kind": "engine.heart')  # torn write, no newline
    with Journal(tmp_path, run_id="r2") as j2:
        e = j2.append(EventKind.HEARTBEAT, {"i": 9})
        assert e.seq == 4
        # the torn line must not surface as an event
        assert all(x.seq != 4 or x.get("i") == 9 for x in j2.iter_jsonl())


def test_jsonl_is_source_of_truth_and_sqlite_rebuilds(tmp_path):
    with Journal(tmp_path, run_id="r1") as j:
        for i in range(20):
            j.append(EventKind.BAR_CLOSED, {"i": i}, stream="XAUUSD")
        conn = j.connection()
        conn.execute("DELETE FROM events")  # simulate a corrupted/deleted mirror
        conn.commit()
        assert j.count() == 0
        assert j.rebuild_sqlite() == 20
        assert j.count() == 20


def test_read_filters(tmp_path):
    with Journal(tmp_path, run_id="r1") as j:
        j.append(EventKind.BAR_CLOSED, {"a": 1}, stream="XAUUSD")
        j.append(EventKind.BAR_CLOSED, {"a": 2}, stream="EURUSD")
        j.append(EventKind.ORDER_FILLED, {"a": 3}, stream="XAUUSD")
        assert len(j.read(kinds=[EventKind.BAR_CLOSED])) == 2
        assert len(j.read(stream="XAUUSD")) == 2
        assert len(j.read(kinds=[EventKind.BAR_CLOSED], stream="EURUSD")) == 1
        assert j.read(kinds=[]) == []
        assert j.latest(EventKind.BAR_CLOSED).get("a") == 2


def test_pydantic_payloads_are_coerced(tmp_path):
    """Domain objects must be journallable without the caller hand-serialising them."""
    rec = DecisionRecord(
        decision_id=make_decision_id("XAUUSD", "s", 1),
        ts=1,
        symbol="XAUUSD",
        strategy="s",
        outcome=DecisionOutcome.NO_SETUP,
    )
    with Journal(tmp_path, run_id="r1") as j:
        e = j.append(EventKind.DECISION, {"record": rec})
        assert e.payload["record"]["outcome"] == "NO_SETUP"
    raw = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[0])
    assert raw["payload"]["record"]["decision_id"] == rec.decision_id


def test_listener_exception_cannot_break_append(tmp_path):
    """A broken dashboard subscriber must never take down the engine."""
    seen: list[Event] = []
    with Journal(tmp_path, run_id="r1") as j:
        j.add_listener(lambda e: (_ for _ in ()).throw(RuntimeError("boom")))
        j.add_listener(seen.append)
        j.append(EventKind.HEARTBEAT, {})
    assert len(seen) == 1


def test_batched_journal_flushes_on_close(tmp_path):
    j = Journal(tmp_path, run_id="r1", batch_size=100)
    for i in range(10):
        j.append(EventKind.DECISION, {"i": i})
    assert (tmp_path / "events.jsonl").stat().st_size == 0, "batch should still be buffered"
    j.close()
    assert (tmp_path / "events.jsonl").stat().st_size > 0


def test_closed_journal_rejects_writes(tmp_path):
    j = Journal(tmp_path, run_id="r1")
    j.close()
    with pytest.raises(RuntimeError):
        j.append(EventKind.HEARTBEAT, {})
