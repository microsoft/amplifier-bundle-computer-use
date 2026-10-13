"""Desktop gate checks use real public execute and a public ApprovalSystem double.

The former pre-grant/ask_user assertions are deliberately replaced by observed
executor approval and backend effects, preserving deny/opt-in/non-sticky promises.
"""

from __future__ import annotations

import pytest
from amplifier_module_tool_computer_use.backend import BackendError, ScreenGeometry
from test_gate_hook import Decisions
from test_neutral_computer import Coordinator, cu, run
from test_neutral_computer import hook as hook_mod


class _FakeDesktopBackend:
    name = "remote-ssh:windows"
    is_remote = True

    def __init__(self):
        self.focus_calls = []
        self._clipboard = ""

    def screen_geometry(self):
        return ScreenGeometry(4, 4, 0, 0)

    def list_monitors(self):
        raise BackendError("fake has no monitor enumeration")

    def list_windows(self):
        return []

    def focus_window(self, handle):
        self.focus_calls.append(handle)

    def get_clipboard(self):
        return self._clipboard

    def set_clipboard(self, text):
        self._clipboard = text

    def close(self):
        pass


def _make_desktop(cfg=None):
    backend = _FakeDesktopBackend()
    computer = cu.ComputerTool(backend, cfg or {})
    computer.resolve_display()
    return cu.DesktopTool(computer), computer, backend


def _install(computer, desktop, *, interactive=False, unattended=False):
    coord = Coordinator(computer, interactive)
    coord.tools.update(computer=computer, desktop=desktop)
    coord.approval_system = Decisions()
    handler = hook_mod._make_gate_handler(coord, unattended_writes_ok=unattended)
    assert run(handler("tool:pre", {"tool_name": "desktop"})).action == "continue"
    return coord, handler


def test_gate_writes_blocks_focus_window_by_default_on_a_remote_target():
    desktop, computer, backend = _make_desktop({"read_only": False})
    assert computer._gate_writes is True
    result = run(desktop.execute({"action": "focus_window", "handle": "abc"}))
    assert result.success is False
    assert "gate_writes" in result.error["message"]
    assert backend.focus_calls == []


def test_unattended_writes_ok_satisfies_gate_writes_for_focus_window():
    desktop, computer, backend = _make_desktop({"read_only": False})
    _install(computer, desktop, unattended=True)
    assert run(desktop.execute({"action": "focus_window", "handle": "abc"})).success
    assert backend.focus_calls == ["abc"]


def test_unattended_writes_ok_satisfies_gate_writes_for_set_clipboard():
    desktop, computer, backend = _make_desktop({"read_only": False})
    _install(computer, desktop, unattended=True)
    assert run(desktop.execute({"action": "set_clipboard", "text": "hello"})).success
    assert backend._clipboard == "hello"


def test_unattended_writes_ok_defaults_false_never_a_silent_escape_hatch():
    desktop, computer, backend = _make_desktop({"read_only": False})
    assert computer._unattended_writes_ok is False
    assert not run(
        desktop.execute({"action": "set_clipboard", "text": "hello"})
    ).success
    assert backend._clipboard == ""


def test_explicit_gate_writes_false_still_works_unaffected():
    desktop, computer, backend = _make_desktop(
        {"read_only": False, "gate_writes": False}
    )
    assert computer._gate_writes is False
    assert run(desktop.execute({"action": "focus_window", "handle": "abc"})).success
    assert backend.focus_calls == ["abc"]


def test_refusal_message_names_every_concrete_remedy():
    desktop, _, _ = _make_desktop({"read_only": False})
    result = run(desktop.execute({"action": "focus_window", "handle": "abc"}))
    message = result.error["message"]
    assert all(
        word in message
        for word in (
            "unattended_writes_ok",
            "gate_writes: false",
            "hook-computer-use",
            "remote-transport.md",
        )
    )


def test_gate_hook_syncs_unattended_writes_ok_onto_the_computer_tool():
    desktop, computer, _ = _make_desktop({"read_only": False})
    assert computer._unattended_writes_ok is False
    _install(computer, desktop, unattended=True)
    assert computer._unattended_writes_ok is True
    assert computer._write_approval is not None


def test_gate_hook_syncs_false_too_never_stale_true_from_a_prior_call():
    desktop, computer, backend = _make_desktop({"read_only": False})
    _install(computer, desktop, unattended=True)
    assert run(desktop.execute({"action": "focus_window", "handle": "abc"})).success
    _install(computer, desktop)
    assert computer._unattended_writes_ok is False
    assert not run(desktop.execute({"action": "focus_window", "handle": "xyz"})).success
    assert backend.focus_calls == ["abc"]


def test_interactive_write_approved_satisfies_gate_writes_for_focus_window():
    desktop, computer, backend = _make_desktop({"read_only": False})
    coord, _ = _install(computer, desktop, interactive=True)
    assert not coord.approval_system.requests
    assert run(desktop.execute({"action": "focus_window", "handle": "abc"})).success
    assert backend.focus_calls == ["abc"]
    assert len(coord.approval_system.requests) == 1


def test_interactive_write_approved_defaults_false_still_denies():
    test_gate_writes_blocks_focus_window_by_default_on_a_remote_target()


def test_gate_hook_sets_interactive_write_approved_before_ask_user():
    # The old interface pre-authorized input before anyone answered. The corrected
    # interface must install policy but not request or inject anything at pre.
    desktop, computer, backend = _make_desktop({"read_only": False})
    coord, _ = _install(computer, desktop, interactive=True)
    assert not coord.approval_system.requests
    assert not backend.focus_calls
    assert not hasattr(computer, "_interactive_write_approved")


def test_gate_hook_leaves_interactive_write_approved_false_on_the_deny_path():
    desktop, computer, backend = _make_desktop({"read_only": False})
    coord, _ = _install(computer, desktop)
    assert not run(desktop.execute({"action": "focus_window", "handle": "abc"})).success
    assert not backend.focus_calls and not coord.approval_system.requests


def test_gate_hook_leaves_interactive_write_approved_false_on_unattended_continue():
    desktop, computer, backend = _make_desktop({"read_only": False})
    coord, _ = _install(computer, desktop, unattended=True)
    assert computer._unattended_writes_ok is True
    assert run(desktop.execute({"action": "focus_window", "handle": "abc"})).success
    assert backend.focus_calls == ["abc"]
    assert not coord.approval_system.requests
    assert not hasattr(computer, "_interactive_write_approved")


def test_previous_interactive_approval_cannot_authorize_a_later_call():
    desktop, computer, backend = _make_desktop({"read_only": False})
    coord, _ = _install(computer, desktop, interactive=True)
    assert run(desktop.execute({"action": "focus_window", "handle": "abc"})).success
    coord.approval_system.selected = "Deny"
    # Direct execute without another pre still needs its own decision.
    assert not run(desktop.execute({"action": "focus_window", "handle": "xyz"})).success
    assert backend.focus_calls == ["abc"]
    assert len(coord.approval_system.requests) == 2


@pytest.mark.parametrize("action", ["focus_window", "set_clipboard"])
@pytest.mark.parametrize("selected", ["Allow", "Deny"])
def test_desktop_writes_require_their_own_real_decision(action, selected):
    desktop, computer, backend = _make_desktop({"read_only": False})
    coord, _ = _install(computer, desktop, interactive=True)
    coord.approval_system.selected = selected
    payload = {"action": action, "handle": "abc", "text": "hello"}
    result = run(desktop.execute(payload))
    assert result.success is (selected == "Allow")
    assert backend.focus_calls == (
        ["abc"] if action == "focus_window" and selected == "Allow" else []
    )
    assert backend._clipboard == (
        "hello" if action == "set_clipboard" and selected == "Allow" else ""
    )


@pytest.mark.parametrize(
    "payload", [{"action": "focus_window"}, {"action": "set_clipboard", "text": 4}]
)
def test_malformed_desktop_write_never_prompts_or_discloses(payload):
    desktop, computer, backend = _make_desktop({"read_only": False})
    coord, _ = _install(computer, desktop, interactive=True)
    disclosed = []
    computer._ensure_announced = lambda: disclosed.append(True)
    assert not run(desktop.execute(payload)).success
    assert not coord.approval_system.requests and not disclosed
    assert not backend.focus_calls and backend._clipboard == ""
