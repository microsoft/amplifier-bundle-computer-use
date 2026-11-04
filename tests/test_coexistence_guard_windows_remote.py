"""Unit tests: closes the coverage gap where `_build_coexistence_guard`
(`__init__.py`) built a guard only for backends exposing `presence_idle_ms()`
directly under a name already present in `presence.GUARD_MS` - which excluded
`WindowsBackend` (no `presence_idle_ms()` existed at all, until this pass) and
`RemoteBackend` (had `presence_idle_ms()` in principle, but its composite
`name` - `"remote-ssh:windows-wsl2"` - is never a `GUARD_MS` key, so the guard
silently failed to build even when the method existed).

No real PowerShell, no real SSH, no real display server - fake `Backend`-shaped
stand-ins with just enough surface for `_build_coexistence_guard` to exercise
its real decision logic (see `test_computer_tool_coexistence.py` for the same
no-real-backend approach used against the guard's before/after wiring).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import pytest
from amplifier_module_tool_computer_use import _build_coexistence_guard
from amplifier_module_tool_computer_use.coexistence_guard import CoexistenceGuard
from amplifier_module_tool_computer_use.presence import GUARD_MS


class _FakeWindowsBackend:
    """Stands in for `WindowsBackend` post-fix: has `presence_idle_ms()`,
    `name == "windows-wsl2"` (already a real `GUARD_MS` key), no
    `presence_platform` attribute (that's remote-only)."""

    name = "windows-wsl2"

    def __init__(self, idle_ms: float = 999_999.0) -> None:
        self._idle_ms = idle_ms
        self.calls = 0

    def presence_idle_ms(self) -> float:
        self.calls += 1
        return self._idle_ms

    def presence_observation(self):
        self.calls += 1
        return {
            "version": 1,
            "epoch": "synthetic-windows-boot-session",
            "now_ms": 2_000_000,
            "last_input_ms": 2_000_000 - int(self._idle_ms),
        }


class _FakeRemoteBackend:
    """Stands in for `RemoteBackend` post-fix: `name` is the COMPOSITE
    `"remote-ssh:<platform>"` identifier (never a `GUARD_MS` key by design -
    see `RemoteBackend.__init__`'s own docstring), while `presence_platform`
    carries the bare remote platform name the guard must actually resolve
    `GUARD_MS`/`PresenceMonitor` against."""

    def __init__(self, remote_platform: str, idle_ms: float = 999_999.0) -> None:
        self.name = f"remote-ssh:{remote_platform}"
        self.presence_platform = remote_platform
        self._idle_ms = idle_ms
        self.calls = 0

    def presence_idle_ms(self) -> float:
        self.calls += 1
        return self._idle_ms


class _FakeBackendNoPresence:
    """No `presence_idle_ms()` at all - the pre-existing "no guard" case,
    kept here as a control so the new resolution logic doesn't accidentally
    start building guards for backends that never claimed the capability."""

    name = "some-other-backend"


# -- WindowsBackend: a guard IS now built --------------------------------------


def test_windows_backend_gets_a_real_guard():
    backend = _FakeWindowsBackend()
    guard = _build_coexistence_guard(backend, {})

    assert guard is not None
    assert isinstance(guard, CoexistenceGuard)
    assert guard.presence.platform == "windows-wsl2"
    assert guard.presence.guard_ms == GUARD_MS["windows-wsl2"]


def test_windows_backend_guard_actually_calls_target_clock_observation():
    backend = _FakeWindowsBackend(idle_ms=999_999.0)
    guard = _build_coexistence_guard(backend, {})
    assert guard is not None

    guard.before_event()

    assert backend.calls >= 1


def test_windows_backend_guard_still_halts_on_a_detected_human():
    """The gap being closed is coverage, not the mechanism itself - a
    freshly-built Windows guard must halt exactly like Linux/macOS do."""
    backend = _FakeWindowsBackend(idle_ms=1.0)  # near-zero idle -> human_active
    guard = _build_coexistence_guard(backend, {})
    assert guard is not None
    # Anchor the injection to the LIVE monotonic clock, not an absolute literal.
    # A hardcoded 1000.0 is only "long ago" on a box whose uptime exceeds
    # 1000s. On a freshly-booted CI runner monotonic() is ~45, so 1000.0 is
    # the FUTURE - the margin arithmetic inverts, the sample reads as our own
    # injection, and the halt never fires. That is exactly how this test passed
    # locally (uptime 9,871,938s) while failing in CI with
    # "DID NOT RAISE HaltedError" on both 3.11 and 3.12. Reproduced by
    # monkeypatching monotonic to a 45s base. Test artifact, not a mechanism
    # defect - but never assume the absolute value of a monotonic clock.
    guard.presence.record_inject(at=time.monotonic() - 1000.0)

    from amplifier_module_tool_computer_use.coexistence_guard import HaltedError

    with pytest.raises(HaltedError):
        guard.before_event()


# -- RemoteBackend: a guard IS now built, using the REMOTE platform's band ----


@pytest.mark.parametrize(
    "remote_platform",
    ["linux-x11", "macos", "windows-wsl2"],
)
def test_remote_backend_gets_a_real_guard_for_every_supported_remote_platform(
    remote_platform,
):
    backend = _FakeRemoteBackend(remote_platform)
    guard = _build_coexistence_guard(backend, {})

    assert guard is not None, (
        f"no guard built for remote target running {remote_platform!r} - "
        "the primary remote-deployment scenario is unprotected"
    )
    assert guard.presence.platform == remote_platform
    assert guard.presence.guard_ms == GUARD_MS[remote_platform]


def test_remote_backend_composite_name_is_never_used_as_the_guard_ms_key():
    """The actual bug: `RemoteBackend.name` (`"remote-ssh:windows-wsl2"`) is
    not, and must never become, a `GUARD_MS` key - the platform key comes
    from `presence_platform` exclusively."""
    backend = _FakeRemoteBackend("windows-wsl2")
    assert backend.name not in GUARD_MS  # sanity: composite name is not a key

    guard = _build_coexistence_guard(backend, {})

    assert guard is not None
    assert guard.presence.platform != backend.name
    assert guard.presence.platform == "windows-wsl2"


def test_remote_backend_guard_actually_calls_presence_idle_ms():
    backend = _FakeRemoteBackend("linux-x11", idle_ms=999_999.0)
    guard = _build_coexistence_guard(backend, {})
    assert guard is not None

    guard.before_event()

    assert backend.calls >= 1


def test_remote_backend_with_unresolvable_platform_builds_no_guard_not_a_crash():
    """If a future/unknown remote platform reports something outside
    `GUARD_MS`, this must degrade to "no guard, logged" - the same fail-safe
    (never fail-open, never crash) shape the pre-existing `not in GUARD_MS`
    branch already had for local backends."""
    backend = _FakeRemoteBackend("some-future-os")

    guard = _build_coexistence_guard(backend, {})

    assert guard is None


# -- control: unchanged behavior for a backend with no presence capability ----


def test_backend_with_no_presence_idle_ms_still_gets_no_guard():
    backend = _FakeBackendNoPresence()
    assert _build_coexistence_guard(backend, {}) is None


# -- the halt invariant is unconditional by construction, regardless of which
#    backend built the guard (docs/designs/coexistence.md \u00a76.0) -------------


def test_halt_invariant_signature_has_no_disable_knob_for_any_backend():
    """Same assertion `test_halt_invariant.py` makes generically - repeated
    here so this file stands on its own evidence that Windows/Remote guards
    are not somehow constructed with a different, weaker `CoexistenceGuard`."""
    import inspect

    params = inspect.signature(CoexistenceGuard.__init__).parameters
    forbidden = {
        "disable_halt",
        "ignore_human",
        "force_continue",
        "bypass_halt",
        "no_halt",
    }
    assert not (set(params) & forbidden)
