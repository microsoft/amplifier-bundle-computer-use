"""Unit tests for the §5.7 fix (`docs/designs/coexistence.md`): a remote
backend's `idle_source()` is not free (an SSH round trip - on Windows, a
`powershell.exe` spawn per read), so each snapshot reports its measured cost
without changing the platform guard or human-input classification.

No real SSH, no real PowerShell, no remote host - a plain `idle_source`
closure that sleeps real wall-clock time stands in for the transport, so the
actual bias this fix closes (and the measured-latency reporting layered on
top of it) is exercised deterministically, on the real clock, with no mocking
of `time.monotonic` itself.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

from amplifier_module_tool_computer_use.coexistence_guard import HaltedError
from amplifier_module_tool_computer_use.halt_state import (
    PERSISTED_BASIS,
    resolve_resume_command,
)
from amplifier_module_tool_computer_use.presence import (
    Confidence,
    PresenceMonitor,
    PresenceSnapshot,
    PresenceState,
)

# -- transport_latency_ms / effective_staleness_ms: measured, not invented ---


def test_local_style_idle_source_reports_negligible_transport_latency():
    """An in-process `idle_source` (Linux/macOS/local-Windows shape) costs
    microseconds - `transport_latency_ms` must stay near-zero and
    `effective_staleness_ms` must stay near `guard_ms`, unchanged from
    today's local behavior."""
    monitor = PresenceMonitor(idle_source=lambda: 5000.0, platform="linux-x11")
    snap = monitor.sample()
    assert snap.transport_latency_ms < 5.0  # real microsecond-scale call
    assert snap.effective_staleness_ms == monitor.guard_ms + snap.transport_latency_ms
    assert snap.effective_staleness_ms < monitor.guard_ms + 5.0


def test_remote_style_idle_source_reports_its_real_measured_cost():
    """A slow `idle_source` (standing in for `RemoteBackend.presence_idle_ms`,
    an SSH round trip) must have its ACTUAL wall-clock cost measured and
    reported - not a fixed/invented constant, not zero."""

    def slow_idle_source() -> float:
        time.sleep(0.12)  # stands in for an SSH round trip
        return 999_999.0

    monitor = PresenceMonitor(idle_source=slow_idle_source, platform="windows-wsl2")
    snap = monitor.sample()
    assert snap.transport_latency_ms >= 100.0  # real measured sleep, with margin
    assert snap.effective_staleness_ms == monitor.guard_ms + snap.transport_latency_ms
    # Distinguishable from GUARD_MS itself - never folded into it.
    assert snap.guard_ms == 20.0
    assert snap.effective_staleness_ms > snap.guard_ms


def test_idle_unreadable_path_still_reports_transport_latency():
    """Even the hard-failure path (§9.6) measures and reports the real cost
    of the failed `idle_source()` call - the declaration does not depend on
    the read succeeding."""

    def slow_boom() -> float:
        time.sleep(0.05)
        raise OSError("simulated remote read failure")

    monitor = PresenceMonitor(idle_source=slow_boom, platform="linux-x11")
    try:
        monitor.sample()
    except Exception:
        pass
    snap = monitor.last_snapshot
    assert snap is not None
    assert snap.transport_latency_ms >= 40.0


def test_to_dict_surfaces_both_new_fields():
    monitor = PresenceMonitor(idle_source=lambda: 5000.0, platform="linux-x11")
    snap = monitor.sample()
    d = snap.to_dict()
    assert "transport_latency_ms" in d
    assert "effective_staleness_ms" in d
    assert d["effective_staleness_ms"] == d["guard_ms"] + d["transport_latency_ms"]


def test_existing_presence_snapshot_construction_sites_are_unaffected():
    """`transport_latency_ms` defaults to 0.0 - every pre-existing
    `PresenceSnapshot(...)` construction site (durable halt records, other
    test fixtures) that does not know about this field keeps working
    unchanged."""
    snap = PresenceSnapshot(
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
    assert snap.transport_latency_ms == 0.0
    assert snap.effective_staleness_ms == 5.0


# -- the actual bug: a human event masked by transport delay, and the fix ----


def test_ordering_fix_recovers_a_true_margin_across_a_slow_transport():
    """Reproduces the exact class of false negative §5.7 fixes: a human
    touches the remote machine 50ms after the agent's own last injection
    (comfortably outside `linux-x11`'s 5ms guard_ms) - but the presence read
    itself takes 300ms of simulated transport time to come back, dwarfing
    that margin by 6x.

    Before the ordering fix (`now` captured BEFORE issuing the slow
    `idle_source()` call), this same scenario computes a margin around
    -250ms - deep in `QUIET` territory - because the stale pre-call `now` is
    compared against an `idle_ms` that is only valid ~300ms later. Worked
    through by hand in this test's own comments so the arithmetic is
    checkable, not just asserted.

    After the fix (`now` captured AFTER `idle_source()` returns), the true
    50ms margin survives essentially intact and the human is correctly
    detected - proving the fix, not just describing it.
    """
    inject_time = time.monotonic()
    human_touch_time = inject_time + 0.050  # human acts 50ms after our own write

    def remote_idle_source() -> float:
        # Stands in for RemoteBackend.presence_idle_ms(): real wall-clock
        # transport cost BEFORE the "freshest possible" idle reading is
        # available, exactly like an SSH round trip / powershell.exe spawn.
        time.sleep(0.30)
        read_time = time.monotonic()
        return (read_time - human_touch_time) * 1000.0

    monitor = PresenceMonitor(idle_source=remote_idle_source, platform="linux-x11")
    monitor.record_inject(at=inject_time)

    # Mirrors real usage: the NEXT before_event() call happens some time
    # after the human's touch, not instantaneously.
    time.sleep(0.06)
    snap = monitor.sample()  # real clock - the production, non-test path

    assert snap.transport_latency_ms >= 250.0  # the simulated 300ms round trip
    assert snap.margin_ms is not None
    # The true margin (~50ms) survives, not the ~-250ms a stale `now` would
    # have produced - generous bounds around 50ms to absorb real scheduling
    # jitter from the two real `time.sleep()` calls above.
    assert 0.0 < snap.margin_ms < 200.0
    assert snap.margin_ms > monitor.guard_ms
    assert snap.state is PresenceState.HUMAN_ACTIVE


def test_quiet_session_over_a_slow_transport_stays_quiet():
    """Control for the fix above: when NOTHING but the agent's own injection
    has touched the machine, a slow transport must not manufacture a false
    HUMAN_ACTIVE out of thin air - `idle_ms` correctly tracks our own last
    injection even after the ordering change."""
    inject_time = time.monotonic()

    def remote_idle_source() -> float:
        time.sleep(0.15)
        read_time = time.monotonic()
        # Nothing touched the machine since our own injection.
        return (read_time - inject_time) * 1000.0

    monitor = PresenceMonitor(idle_source=remote_idle_source, platform="linux-x11")
    monitor.record_inject(at=inject_time)
    snap = monitor.sample()

    assert snap.transport_latency_ms >= 130.0
    assert snap.state is not PresenceState.HUMAN_ACTIVE


# -- HaltedError message declares transport cost when it is non-trivial -----


def test_halted_error_message_declares_transport_latency_when_significant():
    snap = PresenceSnapshot(
        state=PresenceState.HUMAN_ACTIVE,
        confidence=Confidence.HIGH,
        basis="idle_reconciliation",
        last_human_input_ago_ms=12.0,
        margin_ms=30.0,
        guard_ms=5.0,
        guard_measured=True,
        sample_interval_ms=60.0,
        latched_until_ms=None,
        transport_latency_ms=812.4,
    )
    message = str(HaltedError(snap))
    assert "transport" in message.lower()
    assert "812.4" in message
    assert "effective_staleness" in message.lower()


def test_halted_error_message_is_unchanged_for_a_local_zero_latency_snapshot():
    """The declaration is additive and silent in the common (local) case -
    no new clause for a snapshot with negligible transport_latency_ms."""
    snap = PresenceSnapshot(
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
    message = str(HaltedError(snap))
    assert "transport" not in message.lower()


# -- HaltedError message distinguishes a persisted (disk) halt from a live --
# -- sample: the "someone's there right now" reading is wrong for a memory --
# -- of a PRIOR session and must not be implied here.                     --


def test_halted_error_message_for_a_persisted_record_does_not_claim_present_tense():
    """A snapshot rebuilt from `halt_state.PersistedHalt.to_snapshot()` (basis
    `PERSISTED_BASIS`) is a MEMORY of a prior session's detection, not a live
    sample - the six-weeks-later reader must not conclude "someone's there
    right now, I'll wait", because waiting can never clear this halt."""
    snap = PresenceSnapshot(
        state=PresenceState.HUMAN_ACTIVE,
        confidence=Confidence.HIGH,
        basis=PERSISTED_BASIS,
        last_human_input_ago_ms=12.0,
        margin_ms=30.0,
        guard_ms=5.0,
        guard_measured=True,
        sample_interval_ms=None,
        latched_until_ms=None,
    )
    message = str(HaltedError(snap))
    lowered = message.lower()
    # Names the mechanism as a memory, not a live reading.
    assert "prior session" in lowered
    assert "memory" in lowered
    # Explicitly refuses the wrong present-tense reading.
    assert "does not mean someone is at the keyboard right now" in lowered
    # Explicitly refuses the "waiting will fix it" reading - the whole point.
    assert "will not clear itself" in lowered or "not clear itself" in lowered
    # Names an actual recovery path - resolved against THIS process, not a
    # bare command name assumed (wrongly) to be on the user's shell PATH.
    # This is the specific regression check for the "command not found"
    # defect: the message must contain the SAME resolved command
    # `resolve_resume_command()` produces, not a hardcoded literal that
    # could drift from what the code actually emits.
    assert resolve_resume_command() in message
    # Must NOT reuse the live-sample present-tense phrasing.
    assert "produced input" not in lowered


def test_halted_error_resume_command_is_actually_runnable_on_this_machine():
    """The specific check whose ABSENCE let the defect ship: assert the
    command embedded in the message is not merely a string, but something
    that actually resolves on disk in the current environment - an
    executable interpreter for the `-m` form, or an existing file for the
    installed-console-script form. A bare, unresolved command name (the
    pre-fix behavior) fails this: `Path("amplifier-computer-use-resume")`
    does not exist anywhere on a normal machine.
    """
    snap = PresenceSnapshot(
        state=PresenceState.HUMAN_ACTIVE,
        confidence=Confidence.HIGH,
        basis=PERSISTED_BASIS,
        last_human_input_ago_ms=12.0,
        margin_ms=30.0,
        guard_ms=5.0,
        guard_measured=True,
        sample_interval_ms=None,
        latched_until_ms=None,
    )
    message = str(HaltedError(snap))
    command = resolve_resume_command()
    assert command in message

    if " -m " in command:
        executable, _, _module = command.partition(" -m ")
        assert Path(executable).exists(), (
            f"resume command names an interpreter that doesn't exist: {executable!r}"
        )
    else:
        assert Path(command).exists(), (
            f"resume command names a console script that doesn't exist on disk: {command!r}"
        )


def test_halted_error_message_for_a_live_sample_is_unchanged_by_the_persisted_case():
    """Guard against the persisted-case branch leaking into the ordinary,
    live-sample path (`basis="idle_reconciliation"`, the common case)."""
    snap = PresenceSnapshot(
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
    message = str(HaltedError(snap))
    assert "transport" not in message.lower()
