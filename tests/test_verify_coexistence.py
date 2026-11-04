"""Headless policy/harness controls, NOT native X11 qualification.

The fake backend/child control time and emissions; classification and guard are
production objects. No display, process, network or environment is provisioned.
"""

from __future__ import annotations

import importlib.util
import io
import json
import math
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_coexistence", ROOT / "scripts" / "verify_coexistence.py"
)
assert SPEC is not None and SPEC.loader is not None
verify = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = verify
SPEC.loader.exec_module(verify)

from amplifier_module_tool_computer_use.coexistence_guard import (
    CoexistenceGuard,
    HaltedError,
)
from amplifier_module_tool_computer_use.presence import (
    Confidence,
    PresenceMonitor,
    PresenceState,
)


class Clock:
    def __init__(self):
        self.ns = 3_000_000_000

    def advance(self, seconds):
        self.ns += round(seconds * 1e9)


class Backend:
    def __init__(self, clock):
        self.clock = clock
        self.last = 0
        self.writes = []
        self.closed = False
        # >2ms separates symmetric counter intervals at this fake barrier.
        # Separate continuous and integer-tick controls retain ambiguity.
        self.unit_s = 0.0021
        self.first_delay_s = 0
        self.register = True

    def presence_idle_ms(self):
        return (self.clock.ns - self.last) / 1e6

    def type_text(self, text, guard=None):
        for _ in text:
            guard.before_event()
            if not self.writes:
                self.clock.advance(self.first_delay_s)
            self.writes.append(self.clock.ns)
            self.clock.advance(self.unit_s)
            if self.register:
                self.last = self.clock.ns
            guard.after_event()

    def probe(self):
        return SimpleNamespace(available=True)

    def close(self):
        self.closed = True


class IntegerTickBackend(Backend):
    """X-style difference of quantised timestamps, not rounded elapsed time."""

    query_s = 0.0

    def presence_idle_ms(self):
        idle = self.clock.ns // 1_000_000 - self.last // 1_000_000
        self.clock.advance(self.query_s)
        return float(idle)


class Child:
    def __init__(self, backend):
        self.backend = backend
        self.calls = 0
        self.cleanup = {}

    def fire(self):
        self.calls += 1
        before = self.backend.clock.ns
        self.backend.clock.advance(0.00001)
        self.backend.last = self.backend.clock.ns
        return {"send_before_ns": before, "sync_after_ns": self.backend.clock.ns}

    def ready(self):
        pass

    def close(self):
        self.cleanup = {"reaped": True, "returncode": 0, "forced": False}


@pytest.fixture
def rig(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(verify.time, "monotonic", lambda: clock.ns / 1e9)
    monkeypatch.setattr(verify.time, "monotonic_ns", lambda: clock.ns)
    monkeypatch.setattr(verify.time, "sleep", clock.advance)
    backend = Backend(clock)
    return clock, backend, Child(backend)


@pytest.mark.parametrize(
    "entry_ms,end_ms,idle", [(100.960, 100.990, 0), (101.050, 101.100, 1)]
)
def test_integer_tick_counter_and_sample_intervals_cover_same_and_crossed_tick(
    rig, entry_ms, end_ms, idle
):
    clock, _, _ = rig
    backend = IntegerTickBackend(clock)
    backend.last = 100_920_000
    clock.ns = round(entry_ms * 1e6)
    backend.query_s = (end_ms - entry_ms) / 1000
    counter = {}
    raw_interval = verify._counter_read(backend, counter)
    sample_interval = verify._sample_counter_interval(
        {
            "sample_entry_ns": counter["entry_ns"],
            "sample_return_ns": counter["return_ns"],
            "snapshot": {"last_human_input_ago_ms": counter["idle_ms"]},
        }
    )
    assert counter["idle_ms"] == idle
    assert raw_interval == sample_interval
    assert raw_interval == (
        counter["entry_ns"] - (idle + 1) * 1e6,
        counter["return_ns"] - max(0, idle - 1) * 1e6,
    )
    assert raw_interval[0] <= backend.last <= raw_interval[1]
    if idle == 1:
        # The old one-sided upper bound was 100.100ms: before the valid event.
        assert counter["return_ns"] - idle * 1e6 < 100_900_000


@pytest.mark.parametrize("query_ms", [100.960, 101.050])
@pytest.mark.parametrize("kind", ["own", "child"])
def test_integer_tick_registration_uses_independent_event_bracket(rig, query_ms, kind):
    clock, _, _ = rig
    backend = IntegerTickBackend(clock)
    backend.last = 100_920_000
    clock.ns = round(query_ms * 1e6)
    backend.query_s = 0.00005
    prior = {
        "sample_entry_ns": 90_050_000,
        "sample_return_ns": 90_100_000,
        "snapshot": {"last_human_input_ago_ms": 40},
        "emit_before_ns": 100_900_000,
        "emit_after_ns": 100_950_000,
    }
    if kind == "own":
        result = {"timeline": [prior]}
        verify._confirm_own(backend, result)
        assert prior["own_counter"]["corroborated"]
        low, high = prior["own_counter"]["registration_interval_ns"]
        assert 100_900_000 <= low <= backend.last <= high <= 100_950_000
    else:
        result = {
            "timeline": [{}, {"emit_after_ns": 90_950_000}],
            "human": {"send_before_ns": 100_900_000, "sync_after_ns": 100_950_000},
        }
        verify._confirm_event(backend, result)
        assert result["event_counter"]["idle_ms"] == (0 if query_ms < 101 else 1)


@pytest.mark.parametrize(
    "case", ["quiet", "inside-2ms", "outside-8ms", "outside-15ms", "outside-55ms"]
)
def test_integer_tick_full_controls_include_registration_and_fresh_sample(rig, case):
    clock, _, _ = rig
    clock.ns += 900_000  # exercise a nonintegral phase, never rescue-delay a trial
    backend = IntegerTickBackend(clock)
    child = Child(backend)
    result = verify._run_case(case, backend, child)
    assert result["status"] == "PASS", result
    if case != "quiet":
        assert result["event_counter"]["idle_ms"].is_integer()
        sample = result["timeline"][2]
        assert sample["snapshot"]["last_human_input_ago_ms"].is_integer()
        low, high = verify._sample_counter_interval(sample)
        human = result["human"]
        assert low <= human["sync_after_ns"] and high >= human["send_before_ns"]


@pytest.mark.parametrize("unit_s", [0.00001, 0.0004])
def test_integer_tick_barrier_ambiguous_advancement_stays_invalid(rig, unit_s):
    clock, _, _ = rig
    backend = IntegerTickBackend(clock)
    backend.unit_s = unit_s
    child = Child(backend)
    result = verify._run_case("post-sample", backend, child)
    assert result["status"] == "INVALID"
    assert "own counter registration" in result["reason"]
    assert child.calls == 1


@pytest.mark.parametrize("case", ["quiet", "inside-2ms", "outside-15ms", "post-sample"])
def test_integer_tick_hooks_without_registration_cannot_qualify(rig, case):
    clock, _, _ = rig
    backend = IntegerTickBackend(clock)
    backend.register = False
    child = Child(backend)
    result = verify._run_case(case, backend, child)
    assert result["status"] == "INVALID"
    assert "own counter registration" in result["reason"]
    assert len(backend.writes) == 1 and child.calls == 0


def test_integer_tick_child_sync_without_registration_stays_invalid(rig, monkeypatch):
    clock, _, _ = rig
    backend = IntegerTickBackend(clock)
    child = Child(backend)

    def no_registration():
        child.calls += 1
        before = clock.ns
        clock.advance(0.00001)
        return {"send_before_ns": before, "sync_after_ns": clock.ns}

    monkeypatch.setattr(child, "fire", no_registration)
    result = verify._run_case("outside-15ms", backend, child)
    assert result["status"] == "INVALID"
    assert "counter" in result["reason"]
    assert child.calls == 1 and len(backend.writes) == 1


def test_integer_tick_barrier_ineffective_own_write_cannot_borrow_child_registration(
    rig, monkeypatch
):
    clock, _, _ = rig
    backend = IntegerTickBackend(clock)
    child = Child(backend)
    fire = child.fire

    def child_then_disable_own():
        response = fire()
        backend.register = False
        return response

    monkeypatch.setattr(child, "fire", child_then_disable_own)
    result = verify._run_case("post-sample", backend, child)
    assert result["status"] == "INVALID"
    assert "own counter registration" in result["reason"]
    assert child.calls == 1 and len(backend.writes) == 2


def test_symmetric_prior_child_upper_bound_retains_ambiguous_own_advance(rig):
    clock, _, _ = rig
    backend = IntegerTickBackend(clock)
    # A new own event is consistent with its bracket, but the raw interval
    # still overlaps the previous child's symmetric counter uncertainty.
    clock.ns, backend.last = 101_500_000, 101_400_000
    row = {
        "sample_entry_ns": 90_000_000,
        "sample_return_ns": 90_000_000,
        "snapshot": {"last_human_input_ago_ms": 40},
        "emit_before_ns": 101_350_000,
        "emit_after_ns": 101_450_000,
    }
    result = {
        "timeline": [row],
        "event_counter": {
            "entry_ns": 101_050_000,
            "return_ns": 101_100_000,
            "idle_ms": 1,
        },
    }
    with pytest.raises(verify.InvalidCase, match="did not advance"):
        verify._confirm_own(backend, result)


@pytest.mark.parametrize("offset,halts", [(-0.001, False), (0, False), (0.001, True)])
def test_exact_guard_boundary_uses_production_classifier_and_guard(
    monkeypatch, offset, halts
):
    # Exact arithmetic control, not a scheduler claim about equality at 5ms.
    margin = 5.0 + offset
    monitor = PresenceMonitor(idle_source=lambda: 0.0, platform="linux-x11")
    state, confidence = monitor._classify(margin, 0)
    assert (state is PresenceState.HUMAN_ACTIVE) == halts
    assert confidence is (Confidence.HIGH if halts else Confidence.LOW)
    monitor.record_inject(at=0)
    monkeypatch.setattr(verify.time, "monotonic", lambda: margin / 1000)
    releases = []
    guard = CoexistenceGuard(
        presence=monitor, release_all=lambda reason: releases.append(reason) or []
    )
    if halts:
        with pytest.raises(HaltedError):
            guard.before_event()
        # Quiet now, but guard and presence latches refuse a subsequent write.
        monitor.record_inject(at=margin / 1000)
        with pytest.raises(HaltedError):
            guard.before_event()
        assert releases == ["halted", "halted"]
    else:
        guard.before_event()
        assert releases == []


@pytest.mark.parametrize("case", verify.CASES)
def test_each_predeclared_control_once_with_production_objects(rig, case):
    _, backend, child = rig
    result = verify._run_case(case, backend, child)
    assert result["status"] == "PASS", result
    assert child.calls == (0 if case == "quiet" else 1)
    if case.startswith("outside"):
        assert result["placement"] == "OUTSIDE"
        assert result["detected"] is True
        assert result["latch_refused"] is True
        assert len(backend.writes) == 1
        assert len(result["releases"]) == 2
    elif case == "inside-2ms":
        assert result["placement"] == "INSIDE"
        assert result["detected"] is False
        assert len(backend.writes) == 2
    elif case == "post-sample":
        assert result["placement"] == "BARRIER"
        assert result["detected"] is False
        assert len(backend.writes) == 3
        assert "not a natural" in result["limitation"]
    else:
        assert len(backend.writes) == 101
        assert (
            result["timeline"][-1]["emit_after_ns"]
            - result["timeline"][1]["emit_after_ns"]
            >= 6_000_000_000
        )
    assert all(
        row["own_counter"]["corroborated"]
        for row in result["timeline"][1:]
        if "emit_after_ns" in row
    )


@pytest.mark.parametrize("idle", [1, 2000, math.nan, math.inf, -1])
def test_bad_baseline_invalid_without_child_or_input(rig, monkeypatch, idle):
    _, backend, child = rig
    monkeypatch.setattr(backend, "presence_idle_ms", lambda: idle)
    result = verify._run_case("outside-15ms", backend, child)
    assert result["status"] == "INVALID"
    assert child.calls == 0 and backend.writes == []


def test_unreadable_baseline_is_not_a_masking_miss(rig, monkeypatch):
    _, backend, child = rig
    monkeypatch.setattr(
        backend,
        "presence_idle_ms",
        lambda: (_ for _ in ()).throw(OSError("read failed")),
    )
    result = verify._run_case("outside-15ms", backend, child)
    assert result["status"] == "INVALID" and "read failed" in result["reason"]
    assert child.calls == 0


def test_inside_nominal_delay_is_not_placement_proof_and_no_retry(rig, monkeypatch):
    clock, backend, child = rig
    original = child.fire

    def late():
        clock.advance(0.007)
        return original()

    monkeypatch.setattr(child, "fire", late)
    result = verify._run_case("inside-2ms", backend, child)
    assert result["status"] == "INVALID"
    assert child.calls == 1
    assert "unplaceable" in result["reason"]


def test_sync_without_counter_registration_invalidates_control(rig, monkeypatch):
    _, backend, child = rig

    def no_event():
        return {"send_before_ns": backend.clock.ns, "sync_after_ns": backend.clock.ns}

    monkeypatch.setattr(child, "fire", no_event)
    result = verify._run_case("outside-15ms", backend, child)
    assert result["status"] == "INVALID"
    assert "counter" in result["reason"]
    assert len(backend.writes) == 1


def test_barrier_child_failure_contained_by_production_but_prevents_harness_write(
    rig, monkeypatch
):
    _, backend, child = rig
    monkeypatch.setattr(
        child, "fire", lambda: (_ for _ in ()).throw(verify.InvalidCase("lost ack"))
    )
    result = verify._run_case("post-sample", backend, child)
    assert result["status"] == "INVALID"
    assert "lost ack" in result["reason"]
    assert len(backend.writes) == 1


def test_quiet_false_positive_fails_not_invalid_or_miss(rig, monkeypatch):
    clock, backend, child = rig
    real_read = backend.presence_idle_ms
    reads = 0

    def noisy_read():
        nonlocal reads
        reads += 1
        if reads == 4:
            return 0
        return real_read()

    monkeypatch.setattr(backend, "presence_idle_ms", noisy_read)
    result = verify._run_case("quiet", backend, child)
    assert result["status"] == "FAIL"
    assert child.calls == 0
    assert len(backend.writes) == 1
    assert clock.ns >= 3_060_000_000


def test_outside_unexplained_miss_is_failure_not_reconditioned_away(rig, monkeypatch):
    _, backend, child = rig
    original = PresenceMonitor._classify

    def broken_classifier(self, margin, idle):
        # Fault injection: timing and returned counter still place the event,
        # but classification regresses. Must not condition this miss away.
        if margin is not None and margin > self.guard_ms:
            return PresenceState.QUIET, Confidence.LOW
        return original(self, margin, idle)

    monkeypatch.setattr(PresenceMonitor, "_classify", broken_classifier)
    result = verify._run_case("outside-15ms", backend, child)
    assert result["status"] == "FAIL"
    assert result["placement"] == "OUTSIDE"
    assert result["detected"] is False


def test_inside_detection_is_allowed_not_required_miss(rig, monkeypatch):
    _, backend, child = rig
    original = PresenceMonitor._classify

    def conservative_classifier(self, margin, idle):
        # Policy control only: emulate earlier attribution without changing the
        # counter/timing evidence. This is not native early-detection evidence.
        if margin is not None and margin > 0:
            return PresenceState.HUMAN_ACTIVE, Confidence.HIGH
        return original(self, margin, idle)

    monkeypatch.setattr(PresenceMonitor, "_classify", conservative_classifier)
    result = verify._run_case("inside-2ms", backend, child)
    assert result["status"] == "PASS"
    assert result["detected"] and result["latch_refused"]


@pytest.mark.parametrize("counter", ["older", "newer"])
def test_changed_counter_before_fresh_sample_is_invalid_not_a_masking_miss(
    rig, monkeypatch, counter
):
    _, backend, child = rig
    original = backend.presence_idle_ms
    reads = 0

    def contaminated():
        nonlocal reads
        reads += 1
        if reads == 5:
            return (
                (backend.clock.ns - backend.writes[0]) / 1e6
                if counter == "older"
                else 0
            )
        return original()

    monkeypatch.setattr(backend, "presence_idle_ms", contaminated)
    result = verify._run_case("outside-15ms", backend, child)
    assert result["status"] == "INVALID"
    assert "fresh guard counter" in result["reason"]
    assert child.calls == 1


def test_nonfinite_registration_counter_retains_json_safe_invalid_evidence(
    rig, monkeypatch
):
    _, backend, child = rig
    original = backend.presence_idle_ms
    reads = 0

    def broken():
        nonlocal reads
        reads += 1
        return math.nan if reads == 4 else original()

    monkeypatch.setattr(backend, "presence_idle_ms", broken)
    result = verify._run_case("outside-15ms", backend, child)
    assert result["status"] == "INVALID"
    assert result["event_counter"]["idle_ms"] is None
    json.dumps(result, allow_nan=False)


def test_outside_release_failure_never_qualifies(rig, monkeypatch):
    _, backend, child = rig
    original = verify._instrument

    def instrument(*args):
        presence, guard, tracked, arm = original(*args)
        guard.release_all = lambda reason: []
        return presence, guard, tracked, arm

    monkeypatch.setattr(verify, "_instrument", instrument)
    result = verify._run_case("outside-15ms", backend, child)
    assert result["status"] == "FAIL"


@pytest.mark.parametrize("case,active_sample", [("inside-2ms", 3), ("post-sample", 4)])
def test_activity_with_defective_guard_return_and_emission_fails_every_case(
    rig, monkeypatch, case, active_sample
):
    _, backend, child = rig
    classify = PresenceMonitor._classify
    before_event = CoexistenceGuard.before_event
    samples = 0

    def activity(self, margin, idle):
        nonlocal samples
        samples += 1
        if samples == active_sample:
            return PresenceState.HUMAN_ACTIVE, Confidence.HIGH
        return classify(self, margin, idle)

    def broken_return(self):
        try:
            before_event(self)
        except HaltedError:
            pass  # deliberately broken: activity/release, then permit emission

    monkeypatch.setattr(PresenceMonitor, "_classify", activity)
    monkeypatch.setattr(CoexistenceGuard, "before_event", broken_return)
    result = verify._run_case(case, backend, child)
    active = [
        row
        for row in result["timeline"][1:]
        if row["snapshot"]["state"] == "human_active"
    ]
    assert active and "emit_after_ns" in active[0] and not active[0].get("halted")
    assert result["status"] == "FAIL"
    assert "observation/halt invariant" in result["reason"]
    assert result["detected"] is True
    assert child.calls == 1


def test_retry_release_cannot_cover_omitted_initial_halt_release(rig, monkeypatch):
    _, backend, child = rig
    instrument = verify._instrument

    def missing_first(*args):
        presence, guard, tracked, arm = instrument(*args)
        release = guard.release_all
        calls = 0

        def retry_only(reason):
            nonlocal calls
            calls += 1
            return release(reason) if calls > 1 else []

        guard.release_all = retry_only
        return presence, guard, tracked, arm

    monkeypatch.setattr(verify, "_instrument", missing_first)
    result = verify._run_case("outside-15ms", backend, child)
    first_halt = result["timeline"][2]
    assert first_halt["halted"] and result["latch_refused"]
    assert len(result["releases"]) == 1
    assert result["releases"][0]["guard_call_id"] == result["latch_call_id"]
    assert result["releases"][0]["guard_call_id"] != first_halt["guard_call_id"]
    assert result["status"] == "FAIL"
    assert "observation/halt invariant" in result["reason"]


@pytest.mark.parametrize("alter", ["wrong-call", "before-entry", "after-return"])
def test_release_must_belong_to_and_finish_inside_each_guard_call(rig, alter):
    _, backend, child = rig
    result = verify._run_case("outside-15ms", backend, child)
    assert result["status"] == "PASS"
    row = result["timeline"][2]
    release = result["releases"][0]
    if alter == "wrong-call":
        release["guard_call_id"] = result["latch_call_id"]
    elif alter == "before-entry":
        release["at_ns"] = row["guard_entry_ns"] - 1
    else:
        release["at_ns"] = row["guard_return_ns"] + 1
    verify._assess(result, 5)
    assert result["status"] == "FAIL"


def test_quiet_first_emission_delay_does_not_shorten_acknowledged_span(rig):
    _, backend, child = rig
    backend.first_delay_s = 1
    result = verify._run_case("quiet", backend, child)
    assert result["status"] == "PASS"
    rows = result["timeline"][1:]
    assert rows[0]["emit_after_ns"] - rows[0]["emit_before_ns"] >= 1_000_000_000
    assert rows[-1]["emit_after_ns"] - rows[0]["emit_after_ns"] >= 6_000_000_000
    verify._quiet_timing(rows, complete=True)


@pytest.mark.parametrize("wake_adjustment", [-0.030, 0.100])
def test_quiet_early_or_late_scheduler_invalidates_without_catchup(
    rig, monkeypatch, wake_adjustment
):
    clock, backend, child = rig
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock.advance(max(0, seconds + wake_adjustment))

    monkeypatch.setattr(verify.time, "sleep", sleep)
    result = verify._run_case("quiet", backend, child)
    assert result["status"] == "INVALID"
    assert "cadence" in result["reason"]
    assert len(sleeps) == 1 and len(backend.writes) == 2
    assert child.calls == 0


def test_quiet_bounded_lateness_never_causes_a_catchup_burst(rig, monkeypatch):
    clock, backend, child = rig
    monkeypatch.setattr(
        verify.time, "sleep", lambda seconds: clock.advance(seconds + 0.010)
    )
    result = verify._run_case("quiet", backend, child)
    assert result["status"] == "PASS"
    rows = result["timeline"][1:]
    assert all(
        current["emit_before_ns"] - previous["emit_after_ns"] >= 70_000_000
        for previous, current in zip(rows, rows[1:], strict=False)
    )
    assert rows[-1]["emit_after_ns"] - rows[0]["emit_after_ns"] >= 7_000_000_000


def test_quiet_full_row_count_cannot_qualify_compressed_actual_span(rig):
    _, backend, child = rig
    result = verify._run_case("quiet", backend, child)
    assert result["status"] == "PASS"
    first = result["timeline"][1]["emit_after_ns"]
    for i, row in enumerate(result["timeline"][1:]):
        row["emit_after_ns"] = first + i * 50_000_000
        row["emit_before_ns"] = row["emit_after_ns"] - 1_100_000
    assert len(result["timeline"][1:]) == 101
    with pytest.raises(verify.InvalidCase, match="cadence|span"):
        verify._assess(result, 5)


@pytest.mark.parametrize("case", ["quiet", "inside-2ms", "outside-15ms", "post-sample"])
def test_hooks_without_any_own_counter_registration_never_qualify(rig, case):
    _, backend, child = rig
    backend.register = False
    result = verify._run_case(case, backend, child)
    assert result["status"] == "INVALID"
    assert "own counter registration" in result["reason"]
    assert len(backend.writes) == 1 and child.calls == 0


@pytest.mark.parametrize("stop_after", [1, 50, 100])
def test_quiet_counter_registration_is_required_throughout_span(
    rig, monkeypatch, stop_after
):
    _, backend, child = rig
    type_text = backend.type_text

    def stop_registering(text, guard=None):
        if len(backend.writes) >= stop_after:
            backend.register = False
        return type_text(text, guard)

    monkeypatch.setattr(backend, "type_text", stop_registering)
    result = verify._run_case("quiet", backend, child)
    assert result["status"] == "INVALID"
    assert "own counter registration" in result["reason"]
    assert len(backend.writes) == stop_after + 1


def test_own_counter_read_does_not_sample_monitor_and_remains_bounded(rig):
    _, backend, child = rig
    result = verify._run_case("quiet", backend, child)
    assert result["status"] == "PASS"
    assert len(result["timeline"]) == 102  # one baseline + 101 guarded units
    assert len([row for row in result["timeline"] if "own_counter" in row]) == 101
    assert child.calls == 0


def test_barrier_own_advance_unresolvable_at_counter_quantisation_is_invalid(rig):
    _, backend, child = rig
    backend.unit_s = 0.00001
    result = verify._run_case("post-sample", backend, child)
    assert result["status"] == "INVALID"
    assert "own counter registration" in result["reason"]
    assert child.calls == 1


@pytest.mark.parametrize(
    "change", ["missing", "straddle", "sample-overlap", "backwards", "wrong-side"]
)
def test_placement_requires_complete_intervals(change):
    prior = {"emit_after_ns": 1_000_000, "injection_anchor_ns": 1_000_010}
    sample = {"sample_entry_ns": 60_000_000, "sample_return_ns": 60_100_000}
    human = {"send_before_ns": 8_000_000, "sync_after_ns": 8_100_000}
    assert verify.placement("outside-8ms", prior, sample, human, 5) == "OUTSIDE"
    if change == "missing":
        human.pop("sync_after_ns")
    elif change == "straddle":
        human.update(send_before_ns=5_900_000, sync_after_ns=6_100_000)
    elif change == "sample-overlap":
        human["sync_after_ns"] = 60_050_000
    elif change == "backwards":
        human["send_before_ns"] = 9_000_000
    else:
        human.update(send_before_ns=2_000_000, sync_after_ns=2_100_000)
    assert verify.placement("outside-8ms", prior, sample, human, 5) == "INVALID"


def test_boundary_runner_owns_cleanup_and_has_no_retries(rig, monkeypatch):
    from amplifier_module_tool_computer_use import linux_x11

    _, backend, child = rig
    monkeypatch.setattr(linux_x11, "LinuxX11Backend", lambda config: backend)
    monkeypatch.setattr(verify, "EventChild", lambda display: child)
    report = {}
    verify._run_boundary(report, ":fake-owned")
    assert report["status"] == verify.MECHANISM_CONTROLS_PASS
    assert [r["case"] for r in report["cases"]] == list(verify.CASES)
    assert child.calls == 5 and backend.closed
    assert report["backend_close_returned"] and report["child_cleanup"]["reaped"]


def test_invalid_first_case_stops_run_and_closes_resources(rig, monkeypatch):
    from amplifier_module_tool_computer_use import linux_x11

    _, backend, child = rig
    monkeypatch.setattr(linux_x11, "LinuxX11Backend", lambda config: backend)
    monkeypatch.setattr(verify, "EventChild", lambda display: child)
    monkeypatch.setattr(
        verify, "_run_case", lambda case, *_args: {"case": case, "status": "INVALID"}
    )
    report = {}
    verify._run_boundary(report, ":fake-owned")
    assert report["status"] == "UNQUALIFIED"
    assert len(report["cases"]) == 1 and backend.closed
    assert child.cleanup["reaped"]


def test_ready_failure_closes_child_and_backend(rig, monkeypatch):
    from amplifier_module_tool_computer_use import linux_x11

    _, backend, child = rig
    monkeypatch.setattr(linux_x11, "LinuxX11Backend", lambda config: backend)
    monkeypatch.setattr(verify, "EventChild", lambda display: child)
    monkeypatch.setattr(
        child, "ready", lambda: (_ for _ in ()).throw(verify.InvalidCase("not ready"))
    )
    report = {}
    with pytest.raises(verify.InvalidCase, match="not ready"):
        verify._run_boundary(report, ":fake-owned")
    assert backend.closed and child.cleanup["reaped"]


def test_ipc_bounded_messages_eof_and_matching_ids(monkeypatch):
    read_fd, write_fd = os.pipe()
    try:
        verify._send_message(write_fd, {"id": 0, "kind": "ready"})
        assert verify._read_message(read_fd, verify.time.monotonic() + 1) == {
            "id": 0,
            "kind": "ready",
        }
        os.write(write_fd, b"x" * (verify.MAX_MESSAGE + 1))
        with pytest.raises(verify.InvalidCase, match="exceeds"):
            verify._read_message(read_fd, verify.time.monotonic() + 1)
    finally:
        os.close(write_fd)
        os.close(read_fd)
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    try:
        with pytest.raises(verify.InvalidCase, match="EOF"):
            verify._read_message(read_fd, verify.time.monotonic() + 1)
    finally:
        os.close(read_fd)
    child = object.__new__(verify.EventChild)
    child.sequence = 0
    child.proc = SimpleNamespace(stdin=SimpleNamespace(fileno=lambda: 0))
    monkeypatch.setattr(verify, "_send_message", lambda *_args: None)
    monkeypatch.setattr(child, "_receive", lambda: {"id": 2, "kind": "fired"})
    with pytest.raises(verify.InvalidCase, match="sequence"):
        child.fire()


def test_ipc_deadline_does_not_retry():
    read_fd, write_fd = os.pipe()
    try:
        with pytest.raises(verify.InvalidCase, match="deadline"):
            verify._read_message(read_fd, verify.time.monotonic() - 1)
    finally:
        os.close(write_fd)
        os.close(read_fd)


@pytest.mark.parametrize("fault", [None, "command-id", "sync"])
def test_event_child_protocol_owns_connection_and_one_event_per_command(
    monkeypatch, fault
):
    # No real Xlib/display/process: exercise the child protocol with fake OS
    # boundaries, including finally cleanup when command or connection fails.
    events, messages = [], []
    connection = SimpleNamespace(syncs=0, closed=False)

    def sync():
        connection.syncs += 1
        if fault == "sync" and connection.syncs == 2:
            raise OSError("sync failed")

    connection.sync = sync
    connection.close = lambda: setattr(connection, "closed", True)
    fake_xtest = SimpleNamespace(fake_input=lambda *a, **kw: events.append((a, kw)))
    fake_xlib = SimpleNamespace(
        X=SimpleNamespace(MotionNotify=6),
        display=SimpleNamespace(Display=lambda name: connection),
    )
    monkeypatch.setitem(sys.modules, "Xlib", fake_xlib)
    monkeypatch.setitem(sys.modules, "Xlib.ext", SimpleNamespace(xtest=fake_xtest))
    commands = iter({"id": i, "kind": "fire"} for i in range(1, 6))
    monkeypatch.setattr(
        verify,
        "_read_message",
        lambda *_args: (
            {"id": 9, "kind": "fire"} if fault == "command-id" else next(commands)
        ),
    )
    monkeypatch.setattr(
        verify, "_send_message", lambda _fd, message: messages.append(message)
    )
    if fault:
        with pytest.raises((verify.InvalidCase, OSError)):
            verify._event_child(":fake-only")
    else:
        verify._event_child(":fake-only")
        assert len(events) == 5 and connection.syncs == 6
        assert [m["id"] for m in messages] == list(range(6))
        assert all(m["send_before_ns"] <= m["sync_after_ns"] for m in messages[1:])
    assert messages[0] == {"id": 0, "kind": "ready"}
    assert connection.closed


def test_child_cleanup_reaps_owned_process_and_caps_stderr():
    class Proc:
        stdin = io.BytesIO()
        stdout = io.BytesIO()
        stderr = io.BytesIO(b"x" * 9000)
        returncode = None
        waits = 0
        terminated = False

        def wait(self, timeout):
            self.waits += 1
            if self.waits == 1:
                raise verify.subprocess.TimeoutExpired("owned", timeout)
            self.returncode = -15

        def terminate(self):
            self.terminated = True

    child = object.__new__(verify.EventChild)
    child.proc, child.errors = Proc(), bytearray()
    child.close()
    assert child.proc.terminated and child.proc.waits == 2
    assert child.cleanup["reaped"] and child.cleanup["forced"]
    assert len(child.cleanup["stderr_prefix"]) == 4096


def test_original_89_remains_failed_and_model_nonrejection_cannot_clear_it():
    assert not verify.legacy_policy_passes(100, 11, 0, True)
    assert verify.legacy_policy_passes(100, 3, 0, True)
    assert not verify.legacy_policy_passes(100, 3, 1, True)
    assert not verify.legacy_policy_passes(100, 3, 0, False)
    result = verify.random_model_report(100, 11)
    assert result["conditional_p_value"] == pytest.approx(0.210558032669744)
    assert result["conditional_model_result"] == "NOT_REJECTED"
    assert (
        result["status"] == "UNSUPPORTED" and result["qualification"] == "UNQUALIFIED"
    )


def test_exact_tail_fixed_alpha_size_and_power():
    n, q = 100, 5 / 60
    critical = next(k for k in range(101) if verify.binomial_tail(n, k, q) <= 0.05)
    assert verify.binomial_tail(n, critical - 1, q) > 0.05
    assert verify.binomial_tail(n, critical, q) <= 0.05
    assert 0.94 < verify.binomial_tail(n, critical, 0.20) < 0.97
    assert verify.binomial_tail(100, 0, q) == pytest.approx(1)
    assert verify.binomial_tail(100, 100, 0) == 0
    assert verify.binomial_tail(100, 100, 1) == 1
    assert (
        verify.random_model_report(100, 20)["conditional_model_result"]
        == "MODEL_REJECTED"
    )
    with pytest.raises(ValueError):
        verify.binomial_tail(100, 101, q)


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--output", "unused.json"],
        ["--mode", "random-model", "--output", "unused.json"],
        ["--mode", "random-model", "--misses", "101", "--output", "unused.json"],
    ],
)
def test_cli_invalid_arguments_never_begin_native_work(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["verify_coexistence.py", *argv])
    monkeypatch.setattr(
        verify, "_run_boundary", lambda *_args: pytest.fail("must not begin")
    )
    with pytest.raises(SystemExit) as exc:
        verify.main()
    assert exc.value.code == 2


def test_offline_receipt_identity_and_refuses_replacement(monkeypatch, tmp_path):
    output = tmp_path / "model.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_coexistence.py",
            "--mode",
            "random-model",
            "--misses",
            "11",
            "--output",
            str(output),
        ],
    )
    monkeypatch.setattr(
        verify,
        "_run_boundary",
        lambda *_args: pytest.fail("offline must not run native"),
    )
    assert verify.main() == 2
    first = output.read_bytes()
    report = json.loads(first)
    assert report["schema"] == verify.SCHEMA and report["policy"] == verify.POLICY
    assert report["run_id"] and len(report["source_sha256"]) == 5
    assert report["status"] == "UNSUPPORTED"
    with pytest.raises(FileExistsError):
        verify.main()
    assert output.read_bytes() == first


def test_boundary_default_and_setup_failure_receipt(monkeypatch, tmp_path):
    output = tmp_path / "boundary.json"
    seen = []

    def failed_setup(report, display):
        seen.append((report["mode"], display))
        assert json.loads(output.read_text())["status"] == "INCOMPLETE"
        raise verify.InvalidCase("backend unavailable")

    monkeypatch.setattr(verify, "_run_boundary", failed_setup)
    monkeypatch.setattr(
        sys,
        "argv",
        ["verify_coexistence.py", "--display", ":owned", "--output", str(output)],
    )
    assert verify.main() == 2
    assert seen == [("boundary", ":owned")]
    report = json.loads(output.read_text())
    assert (
        report["status"] == "UNQUALIFIED" and "backend unavailable" in report["error"]
    )


@pytest.mark.parametrize(
    "status,exit_code",
    [(verify.MECHANISM_CONTROLS_PASS, 0), ("PASS", 2), ("UNQUALIFIED", 2)],
)
def test_cli_zero_exit_only_for_scoped_mechanism_verdict(
    monkeypatch, tmp_path, status, exit_code
):
    output = tmp_path / "conditional.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["verify_coexistence.py", "--display", ":fake-only", "--output", str(output)],
    )
    monkeypatch.setattr(
        verify, "_run_boundary", lambda report, _display: report.update(status=status)
    )
    assert verify.main() == exit_code
    assert json.loads(output.read_text())["status"] == status
