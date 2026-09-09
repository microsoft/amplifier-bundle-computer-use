"""Unit tests for `CoexistenceGuard` (`coexistence_guard.py`) - the combined
per-event check from `docs/designs/coexistence.md` \u00a78.6: halt, pause,
geometric exclusion, and target binding in one call site, plus \u00a76.0's
release-on-abort behavior.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import pytest
from amplifier_module_tool_computer_use.coexistence_guard import (
    CoexistenceGuard,
    ExcludedCoordinateError,
    HaltedError,
)
from amplifier_module_tool_computer_use.exclusion import Rect
from amplifier_module_tool_computer_use.pause import PausedError
from amplifier_module_tool_computer_use.presence import (
    Confidence,
    PresenceMonitor,
    PresenceSnapshot,
    PresenceState,
)
from amplifier_module_tool_computer_use.target_binding import TargetChangedError


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        # Starts already-quiet (well past QUIET_FLOOR_SECONDS), not "touched
        # at the exact instant of construction" - a fresh guard's first-ever
        # sample has no injection history to reconcile against (defect 1,
        # `presence.py::_classify`), so an idle read this recent would now
        # correctly be reported HUMAN_ACTIVE. These tests are about pause/
        # exclusion/target-binding, not first-sample presence classification,
        # so the fixture models a machine nobody has touched recently rather
        # than accidentally exercising the defect-1 boundary on every test.
        self.last_input_at = self.now - 10.0

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def touch(self) -> None:
        self.last_input_at = self.now

    def idle_ms(self) -> float:
        return (self.now - self.last_input_at) * 1000.0


def make_guard(clock: FakeClock, **kwargs) -> tuple[CoexistenceGuard, list[str]]:
    released: list[str] = []
    monitor = PresenceMonitor(idle_source=clock.idle_ms, platform="linux-x11")
    guard = CoexistenceGuard(
        presence=monitor,
        release_all=lambda reason: (released.append(reason), [reason])[1],
        **kwargs,
    )
    return guard, released


# -- normal operation: nothing blocks a quiet session ------------------------


def test_before_event_passes_when_nothing_is_wrong():
    clock = FakeClock()
    guard, _released = make_guard(clock)
    guard.before_event()  # first sample ever - no injection recorded yet
    guard.after_event()
    clock.advance(0.030)  # well past QUIET_FLOOR with nothing new
    guard.before_event()  # still fine - quiet


# -- the halt invariant, end to end via the guard ----------------------------


def test_human_detected_mid_session_halts_and_releases(monkeypatch):
    """`before_event()`/`after_event()` call `PresenceMonitor.record_inject`/
    `.sample` with no explicit clock argument in production use (that is the
    whole point - callers never plumb a clock through), which means they
    fall back to real `time.monotonic()`. To drive that real path
    deterministically here, `presence.time.monotonic` is patched to read
    from the same `FakeClock` instead of real wall time.
    """
    import amplifier_module_tool_computer_use.presence as presence_module

    clock = FakeClock()
    monkeypatch.setattr(presence_module.time, "monotonic", lambda: clock.now)

    guard, released = make_guard(clock)
    guard.before_event()
    guard.after_event()  # records our own injection at clock.now
    clock.advance(0.030)
    clock.touch()  # independent human input, 30ms after our injection

    with pytest.raises(HaltedError):
        guard.before_event()
    assert guard.halted is True
    assert released == ["halted"]

    # Halt is sticky: every subsequent before_event() keeps raising, with no
    # further release_all calls needed (already released).
    with pytest.raises(HaltedError):
        guard.before_event()


def test_presence_observer_exception_cannot_bypass_human_halt_or_release(monkeypatch):
    """Remote transport reporting is optional: a broken observer must not
    weaken the existing detected-human path after our own injection."""
    import amplifier_module_tool_computer_use.presence as presence_module

    clock = FakeClock()
    monkeypatch.setattr(presence_module.time, "monotonic", lambda: clock.now)
    observed: list[PresenceSnapshot] = []

    def broken_observer(snapshot: PresenceSnapshot) -> None:
        observed.append(snapshot)
        raise RuntimeError("simulated reporting failure")

    guard, released = make_guard(clock, on_presence_sample=broken_observer)
    guard.before_event()
    guard.after_event()
    clock.advance(0.030)
    clock.touch()

    with pytest.raises(HaltedError):
        guard.before_event()

    assert len(observed) == 2
    assert guard.halted is True
    assert released == ["halted"]


# -- pause -------------------------------------------------------------------


def test_pause_blocks_before_event_and_releases():
    clock = FakeClock()
    guard, released = make_guard(clock)
    guard.pause.set("overlay_click", reason="human wants a break")
    with pytest.raises(PausedError):
        guard.before_event()
    assert released == ["paused"]


def test_clearing_pause_allows_before_event_again():
    clock = FakeClock()
    guard, _released = make_guard(clock)
    guard.pause.set("overlay_click")
    guard.pause.clear("overlay_click")
    guard.before_event()  # no longer raises


# -- geometric exclusion ------------------------------------------------------


def test_coordinate_inside_excluded_rect_is_refused():
    clock = FakeClock()
    guard, _released = make_guard(clock)
    guard.exclusion.register("pause_button", Rect(0, 0, 90, 36))
    with pytest.raises(ExcludedCoordinateError):
        guard.before_event(coord=(45, 18))


def test_coordinate_outside_excluded_rects_passes():
    clock = FakeClock()
    guard, _released = make_guard(clock)
    guard.exclusion.register("pause_button", Rect(0, 0, 90, 36))
    guard.before_event(coord=(500, 500))  # nowhere near the button


# -- target binding ------------------------------------------------------------


def test_target_change_aborts_and_releases():
    clock = FakeClock()
    targets = iter(["window-1", "window-1", "window-2"])
    guard, released = make_guard(clock, target_source=lambda: next(targets))
    guard.bind_target()  # reads "window-1"
    guard.before_event()  # reads "window-1" again - matches, passes
    with pytest.raises(TargetChangedError):
        guard.before_event()  # reads "window-2" - mismatch, aborts
    assert released == ["target_changed"]


def test_no_target_source_means_binding_is_never_enforced():
    clock = FakeClock()
    guard, _released = make_guard(clock)  # no target_source supplied
    guard.bind_target()
    assert guard.binding.status == "not_bound"
    guard.before_event()  # never raises on target binding


# -- seed_halted: cross-session durable halt seeding (defect 2) --------------


def test_seed_halted_makes_the_very_first_before_event_raise():
    """A guard seeded from a durable halt record (`halt_state.py`) must halt
    on its very FIRST `before_event()` call - before any live presence
    sample could possibly have detected anything - because the record is
    memory of a PRIOR session, not a live read."""
    clock = FakeClock()
    guard, released = make_guard(clock)
    snapshot = PresenceSnapshot(
        state=PresenceState.HUMAN_ACTIVE,
        confidence=Confidence.HIGH,
        basis="persisted_halt_from_prior_session",
        last_human_input_ago_ms=90_000.0,
        margin_ms=89_995.0,
        guard_ms=5.0,
        guard_measured=True,
        sample_interval_ms=None,
        latched_until_ms=None,
    )

    guard.seed_halted(snapshot)

    assert guard.halted is True
    with pytest.raises(HaltedError) as excinfo:
        guard.before_event()
    assert excinfo.value.snapshot is snapshot
    assert released == ["halted"]


def test_seed_halted_survives_even_a_long_quiet_idle_read():
    """The whole point: a fresh guard's OWN live idle read would say
    `quiet` (nothing has touched the machine in this new process's short
    life) - `seed_halted` must not be something a subsequent quiet sample
    can wash away. There is still no way to clear it from this class."""
    clock = FakeClock()
    guard, _released = make_guard(clock)
    snapshot = PresenceSnapshot(
        state=PresenceState.HUMAN_ACTIVE,
        confidence=Confidence.HIGH,
        basis="persisted_halt_from_prior_session",
        last_human_input_ago_ms=90_000.0,
        margin_ms=89_995.0,
        guard_ms=5.0,
        guard_measured=True,
        sample_interval_ms=None,
        latched_until_ms=None,
    )
    guard.seed_halted(snapshot)

    clock.advance(120.0)  # far past LATCH_DECAY_SECONDS; idle_ms() stays huge -> quiet
    with pytest.raises(HaltedError):
        guard.before_event()  # live sample says quiet; seeded halt still wins


# -- durable_halt_poll: cross-session halt escalation (defect 2 regression) --
# -- live safety defect, fixed 2026-08-02 -------------------------------------


def test_guard_mounted_before_a_durable_halt_exists_still_halts_on_next_event(
    tmp_path,
):
    """Reproduces the exact incident: a guard (standing in for the root
    session's `computer` tool) mounts while NO durable halt record exists
    for its backend yet - its own live presence stays quiet the whole time
    (`idle_source` always reports a huge idle, exactly like a session that
    never sees a human directly). A DIFFERENT session (standing in for the
    sub-agent) then detects a human and persists it via `record_halt` -
    something this guard's mount-time seed could never have seen, because
    it happened afterward. Pre-fix, this guard had no way to learn about
    that: `seed_halted` was consulted only once, at construction. Post-fix,
    `durable_halt_poll` (wired to `halt_state.make_durable_halt_poll` in
    production, `__init__.py::_build_coexistence_guard`) must escalate this
    guard to halted on its very next `before_event()` call - not the 10
    writes the real incident showed executing over the next ~55s."""
    from amplifier_module_tool_computer_use.halt_state import (
        make_durable_halt_poll,
        record_halt,
    )

    monitor = PresenceMonitor(idle_source=lambda: 999_999.0, platform="linux-x11")
    guard = CoexistenceGuard(
        presence=monitor,
        release_all=lambda reason: [],
        durable_halt_poll=make_durable_halt_poll("linux-x11", state_dir=tmp_path),
    )

    # Mount-time / early session: no durable record yet - writes proceed.
    guard.before_event()
    guard.after_event()
    assert guard.halted is False

    # The OTHER session's guard (not this one) detects a human and persists
    # the halt - this is the exact write `ComputerTool.execute()` performs
    # from its `except HaltedError` handler.
    persisted_snapshot = PresenceSnapshot(
        state=PresenceState.HUMAN_ACTIVE,
        confidence=Confidence.HIGH,
        basis="idle_reconciliation",
        last_human_input_ago_ms=12.0,
        margin_ms=30.0,
        guard_ms=5.0,
        guard_measured=True,
        sample_interval_ms=60.0,
        latched_until_ms=None,
    )
    record_halt(
        "linux-x11", persisted_snapshot, reason="halted: test", state_dir=tmp_path
    )

    # This guard's NEXT write must halt - no new mount, no new guard, and
    # its OWN live presence sample is still (falsely) quiet the whole time.
    with pytest.raises(HaltedError):
        guard.before_event()
    assert guard.halted is True

    # Sticky afterward, same as every other halt path.
    with pytest.raises(HaltedError):
        guard.before_event()


def test_durable_halt_poll_none_is_a_complete_no_op():
    """Every caller/test that predates defect 2's fix never supplies
    `durable_halt_poll` - confirm the default leaves behavior byte-for-byte
    unchanged: a guard with no durable-halt wiring at all never halts from
    this mechanism, no matter how many events pass."""
    clock = FakeClock()
    guard, _released = make_guard(clock)
    assert guard.durable_halt_poll is None
    for _ in range(5):
        guard.before_event()
        guard.after_event()
    assert guard.halted is False


# -- as_dict / presence envelope ----------------------------------------------


def test_as_dict_reports_halted_paused_and_binding_status():
    clock = FakeClock()
    guard, _released = make_guard(clock)
    out = guard.as_dict()
    assert out["halted"] is False
    assert out["paused"] is False
    assert out["target_binding"] == "not_bound"
