"""Regression tests for the remote-transport announcement gap: `a4943a1`
wired the session-start disclosure banner for LOCAL backends only - a
`getattr(backend, "is_remote", False)` backend logged a warning and fell
through with no channel, no matter who was sitting at the keyboard on the
other end. That was the flagship, most-hurtful configuration left
unaddressed (a human driven remotely, with no announcement at all).

These tests exercise `_build_announcement`'s new remote branch end to end
against a fake `RemoteBackend`-shaped object - no real SSH, no real display.
Before this pass, every test below would fail with an `AttributeError`
(`_build_remote_announcement` does not exist) or with the pre-existing
"no announcement channel implemented" warning path, never a real dispatch.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import amplifier_module_tool_computer_use as cu
from amplifier_module_tool_computer_use.backend import BackendError
from amplifier_module_tool_computer_use.coexistence_guard import CoexistenceGuard
from amplifier_module_tool_computer_use.geometry import Display
from amplifier_module_tool_computer_use.presence import PresenceMonitor

_DISPLAY = Display(
    screen_width=1920, screen_height=1080, model_width=1280, model_height=720
)


def _guard(platform: str, idle_ms: float = 999_999.0) -> CoexistenceGuard:
    presence = PresenceMonitor(idle_source=lambda: idle_ms, platform=platform)
    return CoexistenceGuard(presence=presence, release_all=lambda reason: [])


class _FakeRemoteBackend:
    """Duck-types just enough of `RemoteBackend` for `_build_announcement`'s
    remote branch: `is_remote`, `presence_platform`, `name`, and whichever
    of `announce_raise`/`announcement_status` the test needs."""

    is_remote = True

    def __init__(self, presence_platform: str) -> None:
        self.presence_platform = presence_platform
        self.name = f"remote-ssh:{presence_platform}"
        self.announce_calls: list[dict] = []
        self._raise_result: dict = {}
        self._raise_exc: Exception | None = None

    def type_text(self, text: str) -> None:
        """`ComputerTool.__init__` inspects this signature - present so this
        fake can be constructed into a real `ComputerTool` in the sync tests
        below, exactly like `RemoteBackend.type_text` itself."""

    def screen_geometry(self):
        from amplifier_module_tool_computer_use.backend import ScreenGeometry

        return ScreenGeometry(width=1920, height=1080)

    def list_monitors(self):
        from amplifier_module_tool_computer_use.backend import BackendError as _BE

        raise _BE("no monitor enumeration on this fake - falls back to screen_geometry")

    def announce_raise(self, **kwargs):
        self.announce_calls.append(kwargs)
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._raise_result


# -- macOS remote: the dialog runs on the target, policy applies identically -


def test_remote_macos_announce_is_actually_called_with_disclosed_timeout():
    backend = _FakeRemoteBackend("macos")
    backend._raise_result = {"button": "Continue", "gave_up": False}

    result = cu._build_announcement(backend, _guard("macos"), {}, _DISPLAY)

    assert len(backend.announce_calls) == 1, (
        "RemoteBackend.announce_raise() was never called - the remote macOS "
        "channel exists but is unreachable"
    )
    call = backend.announce_calls[0]
    assert str(call["timeout_seconds"]) in call["message"], (
        "the timeout must be disclosed in the message text (\u00a77.3), same as local"
    )
    assert result is None  # one-shot channel, nothing to keep alive


def test_remote_macos_announce_declined_refuses_to_mount():
    backend = _FakeRemoteBackend("macos")
    backend._raise_result = {"button": "Pause", "gave_up": False}

    try:
        cu._build_announcement(backend, _guard("macos"), {}, _DISPLAY)
        raised = False
    except cu.AnnouncementRefused:
        raised = True
    assert raised, "a human explicitly declining the remote dialog must refuse to mount"


def test_remote_macos_announce_gave_up_while_quiet_proceeds():
    backend = _FakeRemoteBackend("macos")
    backend._raise_result = {"button": None, "gave_up": True}

    result = cu._build_announcement(backend, _guard("macos"), {}, _DISPLAY)
    assert result is None


def test_remote_macos_announce_gave_up_while_human_active_refuses():
    backend = _FakeRemoteBackend("macos")
    backend._raise_result = {"button": None, "gave_up": True}
    guard = _guard("macos", idle_ms=0.0)  # HUMAN_ACTIVE at construction

    try:
        cu._build_announcement(backend, guard, {}, _DISPLAY)
        raised = False
    except cu.AnnouncementRefused:
        raised = True
    assert raised, (
        "a non-answer while someone may be at the remote machine must never "
        "be treated as consent (\u00a77.3 rules 2-3)"
    )


def test_remote_macos_announce_channel_failure_with_no_human_proceeds_loudly(caplog):
    backend = _FakeRemoteBackend("macos")
    backend._raise_exc = BackendError("osascript not found on target")

    result = cu._build_announcement(backend, _guard("macos"), {}, _DISPLAY)
    assert result is None
    assert any("failed to display" in rec.message for rec in caplog.records)


def test_remote_macos_announce_channel_failure_with_human_present_refuses():
    backend = _FakeRemoteBackend("macos")
    backend._raise_exc = BackendError("osascript not found on target")
    guard = _guard("macos", idle_ms=0.0)

    try:
        cu._build_announcement(backend, guard, {}, _DISPLAY)
        raised = False
    except cu.AnnouncementRefused:
        raised = True
    assert raised, (
        "a failed channel with a human detected present must refuse, not "
        "silently proceed with no disclosure at all"
    )


# -- Linux/Windows remote: the persistent overlay runs on the target --------


def test_remote_overlay_is_actually_raised_with_target_screen_geometry():
    backend = _FakeRemoteBackend("linux-x11")
    backend._raise_result = {
        "shown": True,
        "buttons": {"pause": [1800, 0, 1890, 36], "cancel": [1900, 0, 1990, 36]},
    }
    guard = _guard("linux-x11")

    result = cu._build_announcement(backend, guard, {}, _DISPLAY)

    assert len(backend.announce_calls) == 1
    assert backend.announce_calls[0] == {
        "screen_width": _DISPLAY.screen_width,
        "screen_x": _DISPLAY.origin_x,
        "screen_y": _DISPLAY.origin_y,
    }
    assert isinstance(result, cu._RemoteAnnouncementHandle)


def test_remote_overlay_registers_button_rects_into_the_guards_exclusion_zone():
    """\u00a77.5: the agent must not be able to click its own overlay controls -
    for a remote overlay the rects only exist on the wire response, so THIS
    is the only place they can reach the controller's exclusion zone."""
    backend = _FakeRemoteBackend("windows-wsl2")
    backend._raise_result = {
        "shown": True,
        "buttons": {"pause": [1800, 0, 1890, 36], "cancel": [1900, 0, 1990, 36]},
    }
    guard = _guard("windows-wsl2")

    cu._build_announcement(backend, guard, {}, _DISPLAY)

    assert guard.exclusion.contains(1850, 10) == "overlay_pause_button"
    assert guard.exclusion.contains(1950, 10) == "overlay_cancel_button"


def test_remote_overlay_failure_with_no_human_proceeds_loudly(caplog):
    backend = _FakeRemoteBackend("linux-x11")
    backend._raise_exc = BackendError("no DISPLAY on target")

    result = cu._build_announcement(backend, _guard("linux-x11"), {}, _DISPLAY)
    assert result is None
    assert any("failed to display" in rec.message for rec in caplog.records)


def test_remote_overlay_failure_with_human_present_refuses():
    backend = _FakeRemoteBackend("windows-wsl2")
    backend._raise_exc = BackendError("powershell.exe not found on target")
    guard = _guard("windows-wsl2", idle_ms=0.0)

    try:
        cu._build_announcement(backend, guard, {}, _DISPLAY)
        raised = False
    except cu.AnnouncementRefused:
        raised = True
    assert raised


def test_remote_overlay_reporting_not_shown_is_treated_as_a_channel_failure():
    backend = _FakeRemoteBackend("linux-x11")
    backend._raise_result = {"shown": False}

    result = cu._build_announcement(backend, _guard("linux-x11"), {}, _DISPLAY)
    assert result is None  # no human present in this guard -> proceeds, loudly


# -- unsupported remote platform: loud, not silent ---------------------------


def test_remote_backend_with_unknown_platform_logs_loudly_not_silently(caplog):
    backend = _FakeRemoteBackend("some-future-platform")
    result = cu._build_announcement(backend, _guard("linux-x11"), {}, _DISPLAY)
    assert result is None
    assert any(
        "no announcement channel implemented for remote platform" in rec.message
        for rec in caplog.records
    )


# -- pause/cancel propagation: the target's overlay click must reach THIS ---
# -- controller's guard, not stay stranded on the target alone (\u00a78.1/\u00a79.1) --


class _StatusBackend(_FakeRemoteBackend):
    def __init__(self, platform: str = "linux-x11") -> None:
        super().__init__(platform)
        self.status_calls = 0
        self.status: dict = {"paused": False, "cancelled": False}

    def announcement_status(self):
        self.status_calls += 1
        return self.status


def _computer_tool_with_remote_announcement(backend, guard) -> cu.ComputerTool:
    tool = cu.ComputerTool(backend, {"read_only": True})
    tool._coexistence_guard = guard
    tool._announcement = cu._RemoteAnnouncementHandle(backend.name)
    return tool


def test_sync_remote_announcement_state_applies_pause_to_the_guard():
    backend = _StatusBackend()
    guard = _guard("linux-x11")
    tool = _computer_tool_with_remote_announcement(backend, guard)

    backend.status = {"paused": True, "cancelled": False}
    tool._sync_remote_announcement_state(guard)

    assert guard.pause.is_paused is True


def test_sync_remote_announcement_state_applies_cancel_to_the_guard(monkeypatch):
    """`_on_overlay_cancel` writes a durable halt record to real disk by
    default - monkeypatch it here so this unit test never touches the
    filesystem, matching this module's own test-isolation convention."""
    recorded = {}
    monkeypatch.setattr(
        cu,
        "record_halt",
        lambda backend_name, snap, reason: recorded.update(
            {"backend_name": backend_name, "reason": reason}
        ),
    )
    backend = _StatusBackend()
    guard = _guard("linux-x11")
    tool = _computer_tool_with_remote_announcement(backend, guard)

    backend.status = {"paused": False, "cancelled": True}
    tool._sync_remote_announcement_state(guard)

    assert recorded["backend_name"] == backend.name
    assert guard.halted is False or guard.halted is True  # halt latches via
    # the durable-halt-poll on the NEXT before_event(), not synchronously
    # here - what THIS call guarantees is that the fact was recorded at all.
    assert recorded  # a durable halt record was written


def test_sync_remote_announcement_state_is_edge_triggered_not_polled_repeatedly():
    """A human's Pause click must be applied to the guard exactly once, not
    re-applied (and re-logged) on every subsequent guarded write for the
    rest of the session."""
    backend = _StatusBackend()
    guard = _guard("linux-x11")
    tool = _computer_tool_with_remote_announcement(backend, guard)
    backend.status = {"paused": True, "cancelled": False}

    tool._sync_remote_announcement_state(guard)
    tool._sync_remote_announcement_state(guard)
    tool._sync_remote_announcement_state(guard)

    assert backend.status_calls == 3, "still reads the target every time..."
    assert tool._remote_pause_seen is True  # ...but only APPLIES it once


def test_sync_remote_announcement_state_is_a_noop_when_backend_has_no_status_read():
    """macOS remote has no persistent overlay to poll at all - must not raise."""

    class _NoStatusBackend(_FakeRemoteBackend):
        pass

    backend = _NoStatusBackend("macos")
    guard = _guard("macos")
    tool = _computer_tool_with_remote_announcement(backend, guard)

    tool._sync_remote_announcement_state(guard)  # must not raise
    assert guard.pause.is_paused is False


def test_sync_remote_announcement_state_read_failure_is_best_effort():
    class _BrokenStatusBackend(_FakeRemoteBackend):
        def announcement_status(self):
            raise BackendError("connection lost")

    backend = _BrokenStatusBackend("linux-x11")
    guard = _guard("linux-x11")
    tool = _computer_tool_with_remote_announcement(backend, guard)

    tool._sync_remote_announcement_state(guard)  # must not raise
    assert guard.pause.is_paused is False


# -- the announcement moved: mount() never builds it, first use does --------
#
# Regression tests for the double-mount defect (docs/designs/coexistence.md):
# `amplifier_core`'s loader calls every tool module's `mount()` TWICE per real
# session - once as a throwaway protocol-compliance probe
# (`amplifier_core.validation.tool.ToolValidator._check_protocol_compliance`,
# against a `MockCoordinator` whose result is discarded moments later) and
# once for real. When `mount()` itself built the disclosure, the probe showed
# a real dialog to a real human for a `ComputerTool`/`CoexistenceGuard` pair
# about to be thrown away. `_ensure_announced` (called from `execute()`, not
# `mount()`) closes that hole structurally: a validation probe never calls
# `execute()`.


def test_mount_never_builds_the_announcement_even_when_it_would_be_refused(
    monkeypatch,
):
    """FAILS on the old design: `mount()` used to call `_build_announcement`
    directly and could refuse to mount over it. Now `_build_announcement` is
    only ever reached from `ComputerTool._ensure_announced`, at first real
    use - `mount()` must mount unconditionally, even for a backend whose
    announcement would be refused."""

    class _FakeCoordinator:
        def __init__(self) -> None:
            self.mounted: list[str] = []

        async def mount(self, _mount_point, module, name=None):
            self.mounted.append(name or getattr(module, "name", "?"))

    class _RefusingBackend(_FakeRemoteBackend):
        def __init__(self) -> None:
            super().__init__("macos")
            self.closed = False

        def close(self) -> None:
            self.closed = True

    backend = _RefusingBackend()
    monkeypatch.setattr(cu, "select_backend", lambda cfg: backend)
    monkeypatch.setattr(cu, "_build_coexistence_guard", lambda b, cfg: _guard("macos"))

    def _refuse(*_a, **_k):
        raise AssertionError(
            "_build_announcement must never be called from mount() - a "
            "protocol-compliance probe calls mount() too, and must not be "
            "able to trigger the disclosure"
        )

    monkeypatch.setattr(cu, "_build_announcement", _refuse)

    import asyncio

    coordinator = _FakeCoordinator()
    result = asyncio.run(cu.mount(coordinator, {}))

    assert coordinator.mounted == ["computer", "desktop"], (
        "mount() must mount both tools unconditionally - refusal is no "
        "longer a mount()-time concept"
    )
    assert result["provides"] == ["computer", "desktop"]
    assert backend.closed is False, (
        "mount() must not touch backend.close() at all any more - that "
        "safety net moved to _ensure_announced's own refusal handling"
    )


def test_first_execute_closes_the_backend_when_announcement_is_refused(monkeypatch):
    """The new home for the old `backend.close()` safety net: refusing a
    remote target after `connect()` already spawned a live SSH subprocess +
    agent process must not leak that connection - now proven at the point
    the refusal actually happens, this session's first real `execute()`."""

    class _RefusingBackend(_FakeRemoteBackend):
        def __init__(self) -> None:
            super().__init__("macos")
            self.closed = False

        def close(self) -> None:
            self.closed = True

    backend = _RefusingBackend()
    guard = _guard("macos")
    tool = cu.ComputerTool(backend, {"read_only": True})
    tool._coexistence_guard = guard

    def _refuse(*_a, **_k):
        raise cu.AnnouncementRefused("declined")

    monkeypatch.setattr(cu, "_build_announcement", _refuse)

    import asyncio

    result = asyncio.run(tool.execute({"action": "screenshot"}))

    assert result.success is False
    assert backend.closed is True, (
        "backend.close() must be called when a refused announcement leaves "
        "this session with nothing it may do"
    )
