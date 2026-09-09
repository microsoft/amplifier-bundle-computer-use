"""The halt invariant (`docs/designs/coexistence.md` \u00a76.0):

> A detected human halts writes before the next one - every platform, every
> mode, and no configuration key disables it.

This test file exists to prove exactly that property, two ways:

1. **By construction** - inspect `CoexistenceGuard.__init__`'s signature and
   confirm there is no parameter whose name or documented purpose is "disable
   the halt". `drive_anyway` exists (\u00a77.6/D5) but is proven, functionally, to
   affect only whether driving may *begin* - never whether a detected human
   stops it.
2. **Functionally** - construct a guard with every combination of
   `drive_anyway`, detect a human mid-session, and confirm `before_event()`
   still raises `HaltedError` regardless.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import pytest
from amplifier_module_tool_computer_use.coexistence_guard import (
    CoexistenceGuard,
    HaltedError,
)
from amplifier_module_tool_computer_use.presence import PresenceMonitor


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.last_input_at = self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def touch(self) -> None:
        self.last_input_at = self.now

    def idle_ms(self) -> float:
        return (self.now - self.last_input_at) * 1000.0


def _detect_human(guard: CoexistenceGuard, clock: FakeClock) -> None:
    """Drive the guard's presence monitor to a genuine human_active
    detection, exactly the sequence `after_event()`/`before_event()` perform
    in production (record our own inject, advance, human touches it, sample
    on the next `before_event()` call)."""
    clock.touch()
    guard.presence.record_inject(at=clock.now)
    clock.advance(0.030)
    clock.touch()
    # `before_event()` samples presence itself - see below.


# -- 1. by construction: no disabling parameter exists ----------------------


def test_init_signature_has_no_halt_disabling_parameter():
    params = set(inspect.signature(CoexistenceGuard.__init__).parameters)
    # The only names ever allowed to exist here are the ones this test
    # enumerates and accepts BY NAME - anything else added later must be
    # justified against this same property, not slipped in silently.
    allowed = {
        "self",
        "presence",
        "release_all",
        "drive_anyway",
        "target_source",
        # Defect 2 fix: consulted every before_event() call for a durable,
        # cross-session halt record - can only ever ESCALATE via
        # seed_halted() (additive-only, never a clear), so it cannot
        # disable the halt invariant either - see coexistence_guard.py's
        # `_poll_durable_halt` and the field's own docstring.
        "durable_halt_poll",
        # Reporting-only: called after a successful sample inside a
        # try/except, so it cannot suppress classification or a halt.
        "on_presence_sample",
        "pause",
        "exclusion",
        "binding",
        "drag",
    }
    assert params <= allowed, (
        f"CoexistenceGuard.__init__ gained new parameter(s) {params - allowed} - "
        "verify none of them can disable the \u00a76.0 halt invariant before adding "
        "them to `allowed` here"
    )
    # None of the allowed names read as a halt disable/override.
    forbidden_substrings = ("disable_halt", "skip_halt", "ignore_human", "no_halt")
    for name in params:
        for bad in forbidden_substrings:
            assert bad not in name


# -- 2. functionally: drive_anyway never reaches before_event()'s halt -------


@pytest.mark.parametrize("drive_anyway", [False, True])
def test_halt_is_unconditional_regardless_of_drive_anyway(drive_anyway: bool):
    clock = FakeClock()
    monitor = PresenceMonitor(idle_source=clock.idle_ms, platform="linux-x11")
    released: list[str] = []
    guard = CoexistenceGuard(
        presence=monitor,
        release_all=lambda reason: (released.append(reason), [])[1],
        drive_anyway=drive_anyway,
    )

    monitor.record_inject(at=clock.now)
    clock.advance(0.030)
    clock.touch()  # a real, independent human input
    monitor.sample(now=clock.now)  # margin > guard -> HUMAN_ACTIVE, latches

    with pytest.raises(HaltedError):
        guard.before_event()
    assert guard.halted is True
    assert "halted" in released


def test_drive_anyway_only_affects_check_start_permission_not_before_event():
    """`drive_anyway=True` permits `check_start_permission()` to pass with a
    human already detected - but the moment a FRESH detection happens, the
    unconditional halt in `before_event()` still fires. This is the precise
    boundary \u00a77.6/D5 draws: drive_anyway governs beginning, never continuing.
    """
    clock = FakeClock()
    monitor = PresenceMonitor(idle_source=clock.idle_ms, platform="linux-x11")
    guard = CoexistenceGuard(
        presence=monitor,
        release_all=lambda reason: [],
        drive_anyway=True,
    )
    monitor.record_inject(at=clock.now)
    clock.advance(0.030)
    clock.touch()
    monitor.sample(now=clock.now)  # HUMAN_ACTIVE

    # drive_anyway=True: starting is permitted despite detection.
    guard.check_start_permission()

    # But before_event() - the actual per-write gate - still halts. This is
    # the property under test: drive_anyway does not leak into the
    # unconditional halt.
    with pytest.raises(HaltedError):
        guard.before_event()


def test_no_way_to_clear_a_latched_halt_from_the_guard_itself():
    """There is no `guard.clear_halt()`/`guard.reset()`/`guard.unhalt()`
    method - once `_halted` is set, the ONLY way it stops raising is a new
    `CoexistenceGuard` instance (a fresh driving session), matching \u00a713/D3:
    "resume is manual" - not a method call the agent or controller can
    invoke on itself."""
    public_methods = {
        name
        for name, _ in inspect.getmembers(CoexistenceGuard, predicate=callable)
        if not name.startswith("_")
    }
    forbidden = {"clear_halt", "reset", "unhalt", "resume", "force_resume"}
    assert public_methods.isdisjoint(forbidden), (
        f"CoexistenceGuard exposes {public_methods & forbidden} - "
        "the halt invariant must have no self-clear path"
    )
