"""Offline regressions for the bounded native `key` repeat field."""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import amplifier_module_tool_computer_use as tool_mod
from amplifier_module_tool_computer_use import ComputerTool
from amplifier_module_tool_computer_use.backend import BackendError, ScreenGeometry
from amplifier_module_tool_computer_use.coexistence_guard import HaltedError
from amplifier_module_tool_computer_use.presence import (
    Confidence,
    PresenceSnapshot,
    PresenceState,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _FakeBackend:
    name = "linux-x11"

    def __init__(self, on_key=None) -> None:
        self.calls: list[str] = []
        self._on_key = on_key

    def screen_geometry(self) -> ScreenGeometry:
        return ScreenGeometry(800, 600, 0, 0)

    def list_monitors(self):
        raise BackendError("no monitor enumeration on this fake")

    def key(self, combo: str) -> None:
        self.calls.append(combo)
        if self._on_key is not None:
            self._on_key()

    def type_text(self, _text: str, guard=None) -> None:
        del guard

    def close(self) -> None:
        pass


def _computer(backend: _FakeBackend, config: dict | None = None) -> ComputerTool:
    computer = ComputerTool(backend, config)
    computer.resolve_display()
    return computer


def _halt_snapshot() -> PresenceSnapshot:
    return PresenceSnapshot(
        state=PresenceState.HUMAN_ACTIVE,
        confidence=Confidence.HIGH,
        basis="test",
        last_human_input_ago_ms=1.0,
        margin_ms=1.0,
        guard_ms=5.0,
        guard_measured=True,
        sample_interval_ms=60.0,
        latched_until_ms=None,
    )


class _HaltOnPress:
    def __init__(self, press: int) -> None:
        self.press = press
        self.before_calls = 0

    def check_start_permission(self) -> None:
        pass

    def bind_target(self) -> None:
        pass

    def before_event(self, *, coord=None) -> None:
        assert coord is None
        self.before_calls += 1
        if self.before_calls == self.press:
            raise HaltedError(_halt_snapshot())

    def after_event(self) -> None:
        pass

    def release_target(self) -> None:
        pass


def test_key_repeat_defaults_to_one_and_preserves_input():
    backend = _FakeBackend()
    computer = _computer(backend)
    payload = {"action": "key", "text": "ctrl+s"}

    result = _run(computer.execute(payload))

    assert result.success is True, result.error
    assert result.output == "pressed ctrl+s"
    assert backend.calls == ["ctrl+s"]
    assert payload == {"action": "key", "text": "ctrl+s"}


def test_key_repeat_dispatches_each_press_and_reports_count():
    backend = _FakeBackend()
    computer = _computer(backend)

    result = _run(computer.execute({"action": "key", "text": "down", "repeat": 3}))

    assert result.success is True, result.error
    assert result.output == "pressed down (3 times)"
    assert backend.calls == ["down", "down", "down"]


def test_key_repeat_accepts_the_bounded_maximum():
    backend = _FakeBackend()
    computer = _computer(backend)

    result = _run(computer.execute({"action": "key", "text": "down", "repeat": 100}))

    assert result.success is True, result.error
    assert len(backend.calls) == 100


@pytest.mark.parametrize("repeat", [0, -1, 1.5, "2", None, True, 101])
def test_invalid_key_repeat_fails_before_disclosure_or_backend_side_effect(repeat):
    backend = _FakeBackend()
    computer = _computer(backend)
    payload = {"action": "key", "text": "down", "repeat": repeat}
    announced: list[bool] = []
    computer._ensure_announced = lambda: announced.append(True)  # type: ignore[method-assign]

    result = _run(computer.execute(payload))

    assert result.success is False
    assert result.error["type"] == "ValueError"
    assert "repeat must be an integer from 1 to 100" in result.error["message"]
    assert announced == []
    assert backend.calls == []
    assert payload == {"action": "key", "text": "down", "repeat": repeat}


def test_repeat_does_not_broaden_other_actions_parameter_validation():
    backend = _FakeBackend()
    computer = _computer(backend)

    result = _run(
        computer.execute({"action": "wait", "duration": 0, "repeat": "ignored"})
    )

    assert result.success is True, result.error
    assert backend.calls == []


def test_read_only_blocks_the_whole_repeat_before_any_keypress():
    backend = _FakeBackend()
    computer = _computer(backend, {"read_only": True})

    result = _run(computer.execute({"action": "key", "text": "down", "repeat": 3}))

    assert result.success is False
    assert "read_only" in result.error["message"]
    assert backend.calls == []


def test_halt_on_nth_repeat_press_stops_every_later_press(monkeypatch):
    monkeypatch.setattr(tool_mod, "record_halt", lambda *args, **kwargs: None)
    backend = _FakeBackend()
    computer = _computer(backend)
    guard = _HaltOnPress(press=3)
    computer._coexistence_guard = guard  # type: ignore[assignment]

    result = _run(computer.execute({"action": "key", "text": "down", "repeat": 5}))

    assert result.success is False
    assert result.error["type"] == "HaltedError"
    assert backend.calls == ["down", "down"]
    assert guard.before_calls == 3


def test_retarget_during_repeat_stops_without_using_the_new_backend():
    new_backend = _FakeBackend()
    computer: ComputerTool

    def retarget_after_first_press() -> None:
        computer._backend = new_backend

    old_backend = _FakeBackend(on_key=retarget_after_first_press)
    computer = _computer(old_backend)

    result = _run(computer.execute({"action": "key", "text": "down", "repeat": 3}))

    assert result.success is False
    assert result.error["type"] == "BackendError"
    assert "target or policy changed" in result.error["message"]
    assert old_backend.calls == ["down"]
    assert new_backend.calls == []


class _BlockingBackend(_FakeBackend):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def key(self, combo: str) -> None:
        self.calls.append(combo)
        self.started.set()
        self.release.wait(timeout=5)
        self.finished.set()


def test_cancelling_repeat_does_not_issue_a_later_keypress():
    backend = _BlockingBackend()
    computer = _computer(backend)

    async def cancel_repeat() -> None:
        task = asyncio.create_task(
            computer.execute({"action": "key", "text": "down", "repeat": 3})
        )
        assert await asyncio.to_thread(backend.started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        backend.release.set()
        assert await asyncio.to_thread(backend.finished.wait, 1)

    _run(cancel_repeat())

    assert backend.calls == ["down"]


def test_read_only_tightening_waits_for_current_press_then_stops_the_repeat():
    backend = _BlockingBackend()
    computer = _computer(backend)
    binding = computer._binding
    first_press: list[BaseException] = []
    policy_attempted = threading.Event()
    policy_applied = threading.Event()

    def press_once() -> None:
        try:
            computer._run_key_press({"text": "down"}, binding, 0, 2)
        except BaseException as exc:  # pragma: no cover - asserted below
            first_press.append(exc)

    def tighten_policy() -> None:
        policy_attempted.set()
        computer._read_only = True
        policy_applied.set()

    press_thread = threading.Thread(target=press_once)
    press_thread.start()
    assert backend.started.wait(1)

    policy_thread = threading.Thread(target=tighten_policy)
    policy_thread.start()
    assert policy_attempted.wait(1)
    assert not policy_applied.wait(0.05)

    backend.release.set()
    press_thread.join(timeout=1)
    policy_thread.join(timeout=1)
    assert not press_thread.is_alive()
    assert not policy_thread.is_alive()
    assert first_press == []
    assert policy_applied.is_set()

    with pytest.raises(BackendError, match="target or policy changed"):
        computer._run_key_press({"text": "down"}, binding, 1, 2)
    assert backend.calls == ["down"]


def test_input_schema_describes_repeat_only_as_a_key_count():
    schema = _computer(_FakeBackend()).input_schema

    assert schema["properties"]["repeat"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 100,
        "description": "Number of times to press a key combo; valid only for action 'key' (default: 1).",
    }
