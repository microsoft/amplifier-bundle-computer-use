"""Unit tests for the held-input ledger (`ledger.py`) - the day-one safety
mechanism behind docs/designs/remote-transport.md \u00a710.2.

No remote host, no subprocess, no real input injection - `release_fn` is a
plain recording callable. This is what proves "held-input ledger releases on
stream close" without touching a real desktop.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

from amplifier_module_tool_computer_use.ledger import HeldInputLedger


def test_hold_then_release_calls_release_fn_exactly_once():
    released: list[str] = []
    ledger = HeldInputLedger(deadman_seconds=100)
    ledger.hold("key", "ctrl", lambda: released.append("ctrl"))
    assert ledger.held_tokens == ["ctrl"]

    ledger.release("ctrl")
    assert released == ["ctrl"]
    assert ledger.held_tokens == []

    # Idempotent: releasing again is a no-op, not an error or a double-call.
    ledger.release("ctrl")
    assert released == ["ctrl"]


def test_release_all_releases_every_held_token_exactly_once():
    released: list[str] = []
    ledger = HeldInputLedger(deadman_seconds=100)
    ledger.hold("key", "ctrl", lambda: released.append("ctrl"))
    ledger.hold("mouse", "left", lambda: released.append("left"))

    result = ledger.release_all()

    assert sorted(result) == ["ctrl", "left"]
    assert sorted(released) == ["ctrl", "left"]
    assert ledger.held_tokens == []

    # A second release_all with nothing held releases nothing (and does not
    # re-call any release_fn).
    assert ledger.release_all() == []
    assert sorted(released) == ["ctrl", "left"]


def test_stop_releases_everything_and_disables_further_holds():
    released: list[str] = []
    ledger = HeldInputLedger(deadman_seconds=100)
    ledger.hold("key", "shift", lambda: released.append("shift"))

    ledger.stop()

    assert released == ["shift"]
    assert ledger.held_tokens == []

    # A hold requested after stop() must not silently vanish - it releases
    # immediately instead of being tracked forever.
    late_released: list[str] = []
    ledger.hold("key", "alt", lambda: late_released.append("alt"))
    assert late_released == ["alt"]
    assert ledger.held_tokens == []


def test_on_release_callback_fires_with_kind_and_token():
    calls: list[tuple[str, str]] = []
    ledger = HeldInputLedger(
        deadman_seconds=100, on_release=lambda kind, token: calls.append((kind, token))
    )
    ledger.hold("key", "ctrl", lambda: None)
    ledger.release_all()
    assert calls == [("key", "ctrl")]


def test_a_raising_release_fn_does_not_prevent_other_releases():
    released: list[str] = []

    def _boom():
        raise RuntimeError("synthetic input driver exploded")

    ledger = HeldInputLedger(deadman_seconds=100)
    ledger.hold("key", "broken", _boom)
    ledger.hold("key", "ok", lambda: released.append("ok"))

    result = ledger.release_all()

    # Failure is retained custody, never a fabricated RELEASED notification.
    assert result == ["ok"]
    assert released == ["ok"]
    assert ledger.held_tokens == ["broken"]
    ledger.stop()


def test_deadman_timer_releases_everything_with_no_activity():
    """The backstop for the half-open-connection case (\u00a710.2 item 2) -
    proven here with a fast timer instead of the real 5s default so the test
    suite stays quick."""
    released: list[str] = []
    ledger = HeldInputLedger(deadman_seconds=0.05)
    ledger.hold("key", "ctrl", lambda: released.append("ctrl"))

    time.sleep(0.3)

    assert released == ["ctrl"]
    assert ledger.held_tokens == []


def test_activity_resets_the_deadman_timer():
    """A second hold before the deadman fires must push the deadline out -
    otherwise a long but legitimately active session would spuriously release
    mid-use."""
    released: list[str] = []
    ledger = HeldInputLedger(deadman_seconds=0.15)
    ledger.hold("key", "ctrl", lambda: released.append("ctrl"))
    time.sleep(0.08)
    # Still under the deadman window - hold a second token, resetting the timer.
    ledger.hold("key", "shift", lambda: released.append("shift"))
    time.sleep(0.08)
    # Total elapsed since the FIRST hold now exceeds 0.15s, but only ~0.08s
    # since the timer was last reset - nothing should have fired yet.
    assert released == []

    time.sleep(0.2)
    assert sorted(released) == ["ctrl", "shift"]
