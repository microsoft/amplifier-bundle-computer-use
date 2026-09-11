"""Headless tests for the coexistence ship gate's trial setup and baseline.

The real gate needs an X server and independent child processes. These tests fake only
those boundaries while retaining the production PresenceMonitor and CoexistenceGuard
inside `_run_one_trial`, so they run in CI with no display.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "verify_coexistence.py"
SPEC = importlib.util.spec_from_file_location("verify_coexistence", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
verify = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = verify
SPEC.loader.exec_module(verify)


@pytest.fixture(autouse=True)
def _isolate_gate_temp_directory(monkeypatch, tmp_path):
    def script_path(value):
        if str(value).startswith("/tmp/verify_coexistence_"):
            return tmp_path / "gate"
        return Path(value)

    monkeypatch.setattr(verify, "Path", script_path)


class _FakeProc:
    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


class _TrialBackend:
    def __init__(self, idle_values: list[float]) -> None:
        self._idle_values = iter(idle_values)
        self.keys: list[str] = []

    def presence_idle_ms(self) -> float:
        return next(self._idle_values)

    def type_text(self, text: str, guard=None) -> None:
        for ch in text:
            if guard is not None:
                guard.before_event()
            self.keys.append(ch)
            if guard is not None:
                guard.after_event()


def _fake_popen(*_args, **_kwargs):
    return _FakeProc()


@pytest.mark.parametrize(
    ("idle_ms", "expected"),
    [
        (1.0, "state=human_active"),
        (2000.0, "state=human_active"),
        (math.inf, "finite"),
        (math.nan, "finite"),
    ],
)
def test_invalid_fresh_baselines_fail_before_spawning_child(
    monkeypatch, tmp_path, idle_ms, expected
):
    backend = _TrialBackend([idle_ms])
    spawned = False

    def _popen(*args, **kwargs):
        nonlocal spawned
        spawned = True
        return _fake_popen(*args, **kwargs)

    monkeypatch.setattr(verify.subprocess, "Popen", _popen)

    with pytest.raises(verify.InvalidBaselineError, match=expected):
        verify._run_one_trial(0, backend, ":headless", tmp_path)

    assert spawned is False


def test_unreadable_baseline_fails_before_spawning_child(monkeypatch, tmp_path):
    backend = _TrialBackend([])
    monkeypatch.setattr(
        backend,
        "presence_idle_ms",
        lambda: (_ for _ in ()).throw(OSError("no idle counter")),
    )
    monkeypatch.setattr(
        verify.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("child must not be scheduled"),
    )

    with pytest.raises(verify.InvalidBaselineError, match="idle counter unreadable"):
        verify._run_one_trial(0, backend, ":headless", tmp_path)


def test_quiet_baseline_permits_child_scheduling(monkeypatch, tmp_path):
    backend = _TrialBackend([3000.0] * 200)
    calls = []
    monkeypatch.setattr(
        verify.subprocess,
        "Popen",
        lambda *a, **kw: calls.append((a, kw)) or _FakeProc(),
    )
    monkeypatch.setattr(verify.time, "sleep", lambda _seconds: None)

    result = verify._run_one_trial(0, backend, ":headless", tmp_path)

    assert len(calls) == 1
    assert result.baseline_idle_ms == 3000.0
    assert result.chars_typed > 0


def test_input_after_baseline_halts_before_first_key_and_releases(
    monkeypatch, tmp_path
):
    # Baseline is safely quiet; a new event arrives before `before_event()`.
    backend = _TrialBackend([3000.0, 1.0])
    monkeypatch.setattr(verify.subprocess, "Popen", _fake_popen)

    result = verify._run_one_trial(0, backend, ":headless", tmp_path)

    assert result.detected is True
    assert result.chars_typed == 0
    assert backend.keys == []
    assert result.released_on_halt is True


class _GateBackend:
    instances: list[_GateBackend] = []

    def __init__(self, _config) -> None:
        self.events: list[str] = []
        self.__class__.instances.append(self)

    def probe(self):
        self.events.append("probe")
        return SimpleNamespace(available=True, reason="")

    def type_text(self, text: str) -> None:
        self.events.append(f"warmup:{text!r}")


def _passing_result(index: int) -> verify.TrialResult:
    return verify.TrialResult(
        index=index,
        baseline_idle_ms=3000.0,
        human_delay_s=1.0,
        detected=True,
        false_positive_before_human=False,
        detection_latency_ms=1.0,
        margin_ms=10.0,
        guard_ms=5.0,
        chars_typed=1,
        error_repr=None,
        released_on_halt=True,
    )


def test_gate_warms_up_then_settles_before_trial_zero_and_later_trials(
    monkeypatch, capsys
):
    from amplifier_module_tool_computer_use import linux_x11

    _GateBackend.instances.clear()
    timeline = []

    class _TimelineBackend(_GateBackend):
        def __init__(self, config):
            super().__init__(config)
            self.events = timeline

    monkeypatch.setattr(linux_x11, "LinuxX11Backend", _TimelineBackend)
    monkeypatch.setattr(
        verify.time, "sleep", lambda seconds: timeline.append(("sleep", seconds))
    )

    def _run_trial(index, *_args):
        timeline.append(("trial", index))
        return _passing_result(index)

    monkeypatch.setattr(verify, "_run_one_trial", _run_trial)

    assert verify._run_gate(2, ":headless") == 0
    assert timeline == [
        "probe",
        "warmup:''",
        ("sleep", 2.5),
        ("trial", 0),
        ("sleep", 2.5),
        ("trial", 1),
    ]
    assert "baseline_idle_ms=" in capsys.readouterr().out


def test_warmup_failure_invalidates_gate_before_the_first_settle(monkeypatch, capsys):
    from amplifier_module_tool_computer_use import linux_x11

    class _WarmupFailureBackend(_GateBackend):
        def type_text(self, _text: str) -> None:
            raise RuntimeError("XTEST unavailable")

    sleeps: list[float] = []
    monkeypatch.setattr(linux_x11, "LinuxX11Backend", _WarmupFailureBackend)
    monkeypatch.setattr(verify.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        verify,
        "_run_one_trial",
        lambda *_args: pytest.fail("trial must not begin after warmup failure"),
    )

    assert verify._run_gate(1, ":headless") == 2
    assert sleeps == []
    assert "FAIL: setup warmup failed" in capsys.readouterr().out


def test_invalid_baseline_fails_the_whole_gate_without_retrying(monkeypatch, capsys):
    from amplifier_module_tool_computer_use import linux_x11

    attempts: list[int] = []
    monkeypatch.setattr(linux_x11, "LinuxX11Backend", _GateBackend)
    monkeypatch.setattr(verify.time, "sleep", lambda _seconds: None)

    def _invalid_trial(index, *_args):
        attempts.append(index)
        raise verify.InvalidBaselineError("trial 1: baseline is not safely quiet")

    monkeypatch.setattr(verify, "_run_one_trial", _invalid_trial)

    assert verify._run_gate(3, ":headless") == 2
    assert attempts == [0]
    assert "FAIL: invalid test environment" in capsys.readouterr().out


def test_gate_defaults_and_production_cadence_are_preserved(monkeypatch):
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        verify,
        "_run_gate",
        lambda trials, display: seen.update(trials=trials, display=display) or 0,
    )
    monkeypatch.setattr(sys, "argv", ["verify_coexistence.py"])
    monkeypatch.delenv("DISPLAY", raising=False)

    assert verify.main() == 0
    assert seen == {"trials": 100, "display": ":99"}
    assert verify.CADENCE_S == 0.060
    assert verify.WINDOW_S == 6.0
    assert verify.HUMAN_DELAY_MIN_S == verify.CADENCE_S * 4
    assert verify.HUMAN_DELAY_MAX_S == verify.WINDOW_S - verify.CADENCE_S * 4
