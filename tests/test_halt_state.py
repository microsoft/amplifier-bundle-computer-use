"""Unit tests for `halt_state.py` - the durable, cross-session halt memory
that closes defect 2 (`docs/designs/coexistence.md` \u00a713 D3): resume after a
human-detected halt must require an explicit signal, not the mere passage
of time.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

from amplifier_module_tool_computer_use.halt_state import (
    PERSISTED_BASIS,
    PersistedHalt,
    clear_halt,
    list_halted_platforms,
    load_halt,
    make_durable_halt_poll,
    record_halt,
    resolve_resume_command,
)
from amplifier_module_tool_computer_use.presence import (
    Confidence,
    PresenceSnapshot,
    PresenceState,
)


def _snapshot() -> PresenceSnapshot:
    return PresenceSnapshot(
        state=PresenceState.HUMAN_ACTIVE,
        confidence=Confidence.HIGH,
        basis="idle_reconciliation",
        last_human_input_ago_ms=12.0,
        margin_ms=30.0,
        guard_ms=5.0,
        guard_measured=True,
        sample_interval_ms=60.0,
        latched_until_ms=1234.0,
    )


# -- no record: the common, unslowed case ------------------------------------


def test_no_record_means_no_halt(tmp_path: Path) -> None:
    """A machine nobody has ever been detected on pays nothing - `load_halt`
    returns `None`, not a default record."""
    assert load_halt("linux-x11", state_dir=tmp_path) is None
    assert list_halted_platforms(state_dir=tmp_path) == []


# -- record / load round trip ------------------------------------------------


def test_record_then_load_round_trips(tmp_path: Path) -> None:
    record_halt(
        "linux-x11", _snapshot(), reason="halted: test reason", state_dir=tmp_path
    )
    loaded = load_halt("linux-x11", state_dir=tmp_path)
    assert loaded is not None
    assert loaded.platform == "linux-x11"
    assert loaded.reason == "halted: test reason"
    assert loaded.margin_ms == 30.0
    assert loaded.guard_ms == 5.0
    assert loaded.guard_measured is True
    assert loaded.last_human_input_ago_ms == 12.0
    assert list_halted_platforms(state_dir=tmp_path) == ["linux-x11"]


def test_persisted_halt_to_snapshot_is_honestly_labelled(tmp_path: Path) -> None:
    """The rebuilt snapshot must never claim to be a fresh live sample -
    `basis` distinguishes a durable memory from `idle_reconciliation`."""
    record_halt("linux-x11", _snapshot(), reason="r", state_dir=tmp_path)
    loaded = load_halt("linux-x11", state_dir=tmp_path)
    assert loaded is not None
    snap = loaded.to_snapshot()
    assert snap.state is PresenceState.HUMAN_ACTIVE
    assert snap.confidence is Confidence.HIGH
    assert snap.basis == PERSISTED_BASIS
    assert snap.basis != "idle_reconciliation"
    assert snap.margin_ms == 30.0
    assert snap.guard_ms == 5.0


@pytest.mark.parametrize(
    "basis", ["operator_presence_target_changed", "focus_transition_unverified"]
)
def test_focus_failure_persists_unknown_not_human_authorship(tmp_path, basis):
    snapshot = replace(
        _snapshot(),
        state=PresenceState.UNKNOWN,
        confidence=Confidence.LOW,
        basis=basis,
        margin_ms=None,
        last_human_input_ago_ms=None,
    )
    record_halt(
        "remote-ssh:windows-wsl2:owned", snapshot, reason="focus", state_dir=tmp_path
    )
    loaded = load_halt("remote-ssh:windows-wsl2:owned", state_dir=tmp_path)
    assert loaded is not None
    assert loaded.basis == basis
    assert loaded.to_snapshot().state is PresenceState.UNKNOWN
    assert loaded.to_snapshot().basis == f"persisted_target_clock_failure:{basis}"


# -- separate platforms are isolated -----------------------------------------


def test_different_platforms_do_not_collide(tmp_path: Path) -> None:
    record_halt("linux-x11", _snapshot(), reason="linux halt", state_dir=tmp_path)
    assert load_halt("macos", state_dir=tmp_path) is None
    record_halt("macos", _snapshot(), reason="macos halt", state_dir=tmp_path)
    linux = load_halt("linux-x11", state_dir=tmp_path)
    macos = load_halt("macos", state_dir=tmp_path)
    assert linux is not None and linux.reason == "linux halt"
    assert macos is not None and macos.reason == "macos halt"
    assert sorted(list_halted_platforms(state_dir=tmp_path)) == ["linux-x11", "macos"]


# -- clear_halt: the ONLY resume path ----------------------------------------


def test_clear_halt_removes_the_record(tmp_path: Path) -> None:
    record_halt("linux-x11", _snapshot(), reason="r", state_dir=tmp_path)
    assert load_halt("linux-x11", state_dir=tmp_path) is not None

    cleared = clear_halt("linux-x11", state_dir=tmp_path)

    assert cleared is True
    assert load_halt("linux-x11", state_dir=tmp_path) is None
    assert list_halted_platforms(state_dir=tmp_path) == []


def test_clear_halt_on_nonexistent_record_returns_false(tmp_path: Path) -> None:
    assert clear_halt("linux-x11", state_dir=tmp_path) is False


# -- passage of time alone never clears it -----------------------------------


def test_record_halt_has_no_expiry_parameter() -> None:
    """`record_halt`/`load_halt` must not accept anything shaped like a TTL
    or expiry - the whole point of this module is that ONLY `clear_halt`
    (an explicit human action) removes a record, never elapsed wall time."""
    import inspect

    for fn in (record_halt, load_halt):
        params = set(inspect.signature(fn).parameters)
        for forbidden in ("ttl", "expiry", "expires", "max_age", "decay"):
            assert not any(forbidden in p.lower() for p in params), (
                f"{fn.__name__} exposes a time-based expiry parameter "
                f"({params}) - resume must require an explicit signal, not "
                "the mere passage of time"
            )


def test_repeated_record_halt_does_not_expire_or_reset(tmp_path: Path) -> None:
    """Calling `record_halt` again (e.g. a second `HaltedError` in the same
    still-halted session) must not somehow start a decay clock - the record
    stays exactly as latched as it was the first time."""
    record_halt("linux-x11", _snapshot(), reason="first", state_dir=tmp_path)
    record_halt("linux-x11", _snapshot(), reason="second", state_dir=tmp_path)
    loaded = load_halt("linux-x11", state_dir=tmp_path)
    assert loaded is not None
    assert loaded.reason == "second"  # latest fact, but still latched
    # Still requires an explicit clear - re-recording is not a clear.
    assert list_halted_platforms(state_dir=tmp_path) == ["linux-x11"]


# -- corrupt record: fail safe (still latched), never silently "no halt" ----


def test_corrupt_record_fails_safe_not_silent(tmp_path: Path) -> None:
    state_dir = tmp_path
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "linux-x11.json").write_text("{not valid json", encoding="utf-8")

    loaded = load_halt("linux-x11", state_dir=state_dir)

    assert loaded is not None  # never silently treated as "no halt occurred"
    assert "corrupt" in loaded.reason.lower()


# -- make_durable_halt_poll: the defect 2 per-event poll --------------------


def test_durable_halt_poll_returns_none_when_no_record_exists(tmp_path: Path) -> None:
    """The common case - nobody has ever been detected on this backend -
    costs one `Path.stat()` (`FileNotFoundError`), never a read or parse."""
    poll = make_durable_halt_poll("linux-x11", state_dir=tmp_path)
    assert poll() is None
    assert poll() is None  # repeatable; still no record


def test_durable_halt_poll_returns_a_snapshot_once_a_record_appears(
    tmp_path: Path,
) -> None:
    """Once a record is written (e.g. by a DIFFERENT session's guard, same
    backend), the poll must report it - honestly labelled `PERSISTED_BASIS`,
    never as a fresh live sample."""
    poll = make_durable_halt_poll("linux-x11", state_dir=tmp_path)
    assert poll() is None

    record_halt("linux-x11", _snapshot(), reason="halted: test", state_dir=tmp_path)

    snap = poll()
    assert snap is not None
    assert snap.state == PresenceState.HUMAN_ACTIVE
    assert snap.basis == PERSISTED_BASIS
    assert snap.margin_ms == 30.0


def test_durable_halt_poll_is_isolated_per_platform(tmp_path: Path) -> None:
    linux_poll = make_durable_halt_poll("linux-x11", state_dir=tmp_path)
    macos_poll = make_durable_halt_poll("macos", state_dir=tmp_path)
    record_halt("linux-x11", _snapshot(), reason="linux halt", state_dir=tmp_path)
    assert linux_poll() is not None
    assert macos_poll() is None


# -- resolve_resume_command(): the "command not found" defect ---------------
# -- (bare `amplifier-computer-use-resume` is not on a normal user's PATH). --


def test_resolve_resume_command_uses_installed_console_script_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the console script `pip`/`uv` would have installed exists next
    to `sys.executable` (as it does whenever this module was actually
    installed into the running environment), return its full, absolute
    path - not a bare name the operator has to hope is on PATH."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3"
    fake_python.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_script = fake_bin / "amplifier-computer-use-resume"
    fake_script.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(fake_python))
    monkeypatch.setattr(sys, "platform", "linux")

    command = resolve_resume_command()

    assert command == str(fake_script.resolve())
    assert Path(command).exists()


def test_resolve_resume_command_falls_back_when_console_script_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the console script is NOT next to `sys.executable` (partial
    install, a source checkout with no `pip install` step, a venv that
    never installed this module) - fall back to a `-m` invocation of THIS
    exact interpreter, which can always import `resume_cli` because it is
    a sibling module in the same package as this running code. Never emit
    a path that doesn't exist."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3"
    fake_python.write_text("#!/bin/sh\n", encoding="utf-8")
    # Deliberately do NOT create amplifier-computer-use-resume here.
    monkeypatch.setattr(sys, "executable", str(fake_python))
    monkeypatch.setattr(sys, "platform", "linux")

    command = resolve_resume_command()

    assert command == f"{fake_python} -m amplifier_module_tool_computer_use.resume_cli"
    assert not (fake_bin / "amplifier-computer-use-resume").exists()


def test_resolve_resume_command_checks_exe_suffix_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Console scripts on Windows are `.exe` launcher stubs, not bare
    files - the lookup must account for that or it will always report
    'absent' on Windows even when the script IS installed."""
    fake_bin = tmp_path / "Scripts"
    fake_bin.mkdir()
    fake_python = fake_bin / "python.exe"
    fake_python.write_text("", encoding="utf-8")
    fake_script = fake_bin / "amplifier-computer-use-resume.exe"
    fake_script.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(fake_python))
    monkeypatch.setattr(sys, "platform", "win32")

    command = resolve_resume_command()

    assert command == str(fake_script.resolve())


def test_resolve_resume_command_in_the_real_current_environment_is_runnable() -> None:
    """No mocking: whatever `resolve_resume_command()` returns RIGHT NOW, in
    THIS test run's actual environment, must resolve to something real -
    the specific check whose absence let the original defect ship (the
    bare command name was never checked against the filesystem at all)."""
    command = resolve_resume_command()
    if " -m " in command:
        executable, _, _module = command.partition(" -m ")
        assert Path(executable).exists()
    else:
        assert Path(command).exists()


def test_persisted_halt_round_trip_dict() -> None:
    original = PersistedHalt(
        platform="linux-x11",
        detected_at=1000.0,
        reason="halted: r",
        last_human_input_ago_ms=12.0,
        margin_ms=30.0,
        guard_ms=5.0,
        guard_measured=True,
    )
    restored = PersistedHalt.from_dict(original.to_dict())
    assert restored == original
