"""Headless synthetic/source controls. No Windows/native input qualification."""

from __future__ import annotations

import ctypes
import io
import json
import sys
import tarfile
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

from amplifier_module_tool_computer_use import windows
from amplifier_module_tool_computer_use.target_clock import CURRENT_TARGET_CLOCK
from amplifier_module_tool_computer_use.windows_input_diagnostics import (
    PROBE_PS1,
    correlate_probe,
    event_category,
    prepare_probe,
)

EPOCH = "c" * 64
PACKAGE = PROBE_PS1.parent


def sample(now, tick):
    return {"observed_tick64": now, "last_input_tick_dword": tick}


def event(tick=105, *, injected=True, matches=True, observed=None, lower=False):
    return {
        "kind": "key-up",
        "callback_tick_dword": tick,
        "observed_tick64": tick if observed is None else observed,
        "injected": injected,
        "lower_il_injected": lower,
        "marker_matches": matches,
        "category": event_category(injected, lower, matches),
    }


def evidence():
    report = {
        "version": 1,
        "ready": True,
        "status": "completed",
        "epoch": EPOCH,
        "requested_duration_ms": 100,
        "event_cap": 256,
        "change_cap": 32,
        "keyboard_hook_installed": True,
        "mouse_hook_installed": True,
        "initial": sample(100, 10),
        "final": sample(200, 105),
        "counter_changes": [sample(150, 105)],
        "counter_change_count": 1,
        "counter_changes_dropped": 0,
        "sample_count": 3,
        "max_sample_gap_ms": 50,
        "events": [event()],
        "keyboard_callback_count": 1,
        "mouse_callback_count": 0,
        "events_dropped": 0,
        "callback_errors": 0,
        "deadline_stop_posted": True,
        "deadline_post_error": 0,
        "cleanup": dict.fromkeys(
            (
                "deadline_timer_disposed",
                "sample_timer_killed",
                "mouse_unhooked",
                "keyboard_unhooked",
                "input_desktop_handle_closed",
            ),
            True,
        ),
    }
    return [
        {"event": "probe_ready", "version": 1, "epoch": EPOCH, "observed_tick64": 100},
        {"event": "probe_result", "result": report},
        {
            "event": "probe_final",
            "status": "completed",
            "compiler_temp_files": [
                {
                    "existed_before_cleanup": True,
                    "exists_after_cleanup": False,
                    "error_type": None,
                }
            ],
            "input_calls": 0,
            "clipboard_access": 0,
            "gui_actions": 0,
            "halt_access": 0,
        },
    ], {"epoch": EPOCH, "before_ms": 104, "after_ms": 106, "count": 1}


@pytest.mark.parametrize(
    "injected,lower,matches,expected",
    [
        (True, False, True, "own_marker"),
        (True, True, True, "own_marker"),
        (True, False, False, "other_injected"),
        (False, False, False, "unattributed_noninjected"),
        (False, False, True, "inconsistent_marker"),
        (False, True, False, "inconsistent_flags"),
    ],
)
def test_category_is_correlation_not_author(injected, lower, matches, expected):
    assert event_category(injected, lower, matches) == expected


def test_unique_own_marker_in_receipt_correlates_without_mutating_inputs():
    frames, receipt = evidence()
    original = deepcopy((frames, receipt))
    result = correlate_probe(frames, receipt)
    assert result["status"] == "correlated"
    assert result["counter_changes"][0]["reason"] == "correlated_marker_only"
    assert result["events"][0]["receipt_relation"] == "within_receipt"
    assert (frames, receipt) == original
    assert "credit" not in result and "halt" not in result


@pytest.mark.parametrize(
    "replacement,relation",
    [
        (event(matches=False), "within_receipt"),
        (event(injected=False, matches=False), "within_receipt"),
        (event(injected=False), "within_receipt"),
        (event(110), "late"),
        (event(105, observed=140), "late"),
        (event(101), "outside_receipt"),
    ],
)
def test_unrelated_noninjected_or_late_never_credit(replacement, relation):
    frames, receipt = evidence()
    report = frames[1]["result"]
    report["events"] = [replacement]
    report["counter_changes"] = [sample(150, replacement["callback_tick_dword"])]
    report["final"] = sample(200, replacement["callback_tick_dword"])
    result = correlate_probe(frames, receipt)
    assert result["status"] == "unresolved"
    assert result["events"][0]["receipt_relation"] == relation
    assert result["counter_changes"][0]["attribution"] == "unresolved"


def test_counter_change_without_event_remains_unresolved():
    frames, receipt = evidence()
    frames[1]["result"]["events"] = []
    frames[1]["result"]["keyboard_callback_count"] = 0
    result = correlate_probe(frames, receipt)
    assert result["counter_changes"][0]["reason"] == "no_matching_event"
    assert result["status"] == "unresolved"


@pytest.mark.parametrize(
    "competing", [event(), event(matches=False), event(injected=False, matches=False)]
)
def test_same_tick_multiple_events_are_ambiguous_even_if_all_ours(competing):
    frames, receipt = evidence()
    frames[1]["result"]["events"].append(competing)
    frames[1]["result"]["keyboard_callback_count"] = 2
    receipt["count"] = 2
    result = correlate_probe(frames, receipt)
    assert result["status"] == "unresolved"
    assert result["counter_changes"][0]["reason"] == "same_tick_ambiguous"


def test_competing_callback_after_sample_at_same_tick_not_hidden():
    frames, receipt = evidence()
    report = frames[1]["result"]
    report["events"].append(event(105, matches=False, observed=160))
    report["keyboard_callback_count"] = 2
    result = correlate_probe(frames, receipt)
    assert result["counter_changes"][0]["reason"] == "same_tick_ambiguous"


def test_multiple_events_between_counter_samples_do_not_prove_distinct_resets():
    frames, receipt = evidence()
    report = frames[1]["result"]
    report["events"] = [event(104), event(105)]
    report["keyboard_callback_count"] = 2
    receipt["count"] = 2
    result = correlate_probe(frames, receipt)
    assert result["status"] == "unresolved"
    assert result["counter_changes"][0]["attribution"] == "unresolved"


def test_extra_late_marker_without_counter_transition_does_not_make_window_ours():
    frames, receipt = evidence()
    frames[1]["result"]["events"].append(event(160))
    frames[1]["result"]["keyboard_callback_count"] = 2
    result = correlate_probe(frames, receipt)
    assert result["status"] == "unresolved"
    assert result["events"][1]["receipt_relation"] == "late"


@pytest.mark.parametrize(
    "fault",
    [
        "drop_event",
        "drop_counter",
        "callback_error",
        "no_readiness",
        "unhook_failed",
        "temp_retained",
        "epoch_mismatch",
        "receipt_count",
        "helper_failed",
        "no_counter",
        "stop_post_error",
    ],
)
def test_incomplete_coverage_never_correlates(fault):
    frames, receipt = evidence()
    report = frames[1]["result"]
    if fault == "drop_event":
        report["events_dropped"] = 1
        report["keyboard_callback_count"] += 1
    elif fault == "drop_counter":
        report["counter_changes_dropped"] = 1
        report["counter_change_count"] += 1
    elif fault == "callback_error":
        report["callback_errors"] = 1
    elif fault == "no_readiness":
        return_value = correlate_probe(frames[1:], receipt)
        assert return_value["status"] == "unresolved"
        return
    elif fault == "unhook_failed":
        report["cleanup"]["mouse_unhooked"] = False
    elif fault == "temp_retained":
        frames[2]["compiler_temp_files"][0]["exists_after_cleanup"] = True
    elif fault == "epoch_mismatch":
        receipt["epoch"] = "a" * 64
    elif fault == "receipt_count":
        receipt["count"] = 2
    elif fault == "helper_failed":
        report["status"] = "failed"
    elif fault == "no_counter":
        report["counter_changes"] = []
        report["counter_change_count"] = 0
        report["final"] = sample(200, 10)
    elif fault == "stop_post_error":
        report["deadline_post_error"] = 5
    assert correlate_probe(frames, receipt)["status"] == "unresolved"


@pytest.mark.parametrize(
    "place", ["report", "event", "sample", "cleanup", "final", "receipt"]
)
def test_closed_schema_rejects_payload_without_echo(place):
    frames, receipt = evidence()
    report = frames[1]["result"]
    target = {
        "report": report,
        "event": report["events"][0],
        "sample": report["initial"],
        "cleanup": report["cleanup"],
        "final": frames[2],
        "receipt": receipt,
    }[place]
    target["private-key-data"] = "secret-path-or-keystroke"
    with pytest.raises(ValueError, match="Malformed bounded input diagnostic") as error:
        correlate_probe(frames, receipt)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "mutation", ["bool_tick", "bad_category", "cap", "backwards", "count"]
)
def test_malformed_evidence_is_not_inferred(mutation):
    frames, receipt = evidence()
    report = frames[1]["result"]
    if mutation == "bool_tick":
        report["events"][0]["callback_tick_dword"] = True
    elif mutation == "bad_category":
        report["events"][0]["category"] = "human"
    elif mutation == "cap":
        report["events"] *= 257
    elif mutation == "backwards":
        receipt["before_ms"] = 107
    elif mutation == "count":
        report["keyboard_callback_count"] = 0
    with pytest.raises(ValueError):
        correlate_probe(frames, receipt)


def test_dword_wrap_is_unwrapped_on_target_clock_only():
    frames, receipt = evidence()
    wrap = 1 << 32
    report = frames[1]["result"]
    report["initial"] = sample(wrap - 10, wrap - 20)
    report["final"] = sample(wrap + 100, 5)
    report["counter_changes"] = [sample(wrap + 50, 5)]
    report["events"] = [event(5, observed=wrap + 5)]
    frames[0]["observed_tick64"] = wrap - 10
    receipt.update(before_ms=wrap + 4, after_ms=wrap + 6)
    assert correlate_probe(frames, receipt)["status"] == "correlated"


def test_backend_marker_stable_distinct_private_and_preparation_is_inert(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("probe preparation must not contact anything")

    monkeypatch.setattr(windows.subprocess, "run", forbidden)
    first, second = windows.WindowsBackend(), windows.WindowsBackend()
    one = first.prepare_input_probe(123)
    again = first.prepare_input_probe(123)
    other = second.prepare_input_probe(123)
    marker = json.loads(one.stdin)["marker"]
    assert one == again
    assert marker != json.loads(other.stdin)["marker"]
    assert type(marker) is int and 0 < marker <= (1 << 31) - 1
    assert str(marker) not in repr(one) and str(marker) not in repr(first)
    assert one.script == PROBE_PS1
    for bad in (0, -1, 35001, True, "100"):
        with pytest.raises(ValueError):
            first.prepare_input_probe(bad)


def test_private_marker_validation():
    for bad in (None, True, 0, -1, "123", 1 << 31, 1 << 64):
        with pytest.raises(ValueError):
            prepare_probe(bad)


@pytest.mark.parametrize("native_bits", [32, 64])
def test_mixed_width_max_marker_tags_input_and_unconditioned_release(
    monkeypatch, native_bits
):
    # Model a 64-bit controller's maximum RNG result, not a lucky small draw.
    # The target's pointer width is independent of the WSL Python interpreter.
    monkeypatch.setattr(windows.secrets, "randbelow", lambda upper: upper - 1)
    backend = windows.WindowsBackend({"powershell_path": "inert-32-or-64-bit"})
    backend._ps = "inert-powershell"
    prepared = json.loads(backend.prepare_input_probe().stdin)
    seen = []
    finished = []

    def bridge(argv, **kwargs):
        payload = json.loads(Path(argv[-1]).read_text())
        marker = payload["_input_marker"]
        assert 0 < marker <= (1 << (native_bits - 1)) - 1
        seen.append(payload)
        return SimpleNamespace(stdout='{"ok":true}', stderr="", returncode=0)

    monkeypatch.setattr(windows, "_translate", lambda path, flag: path)
    monkeypatch.setattr(windows.subprocess, "run", bridge)
    exchange = SimpleNamespace(
        condition={"unchanged": True}, finish=lambda *a, **k: finished.append((a, k))
    )
    token = CURRENT_TARGET_CLOCK.set(exchange)
    try:
        assert backend.raw("key", key="shift") == {"ok": True}
    finally:
        CURRENT_TARGET_CLOCK.reset(token)
    # Safety release stays unconditioned, but uses the exact same marker.
    assert backend.raw("left_mouse_up") == {"ok": True}
    assert prepared["marker"] == seen[0]["_input_marker"] == seen[1]["_input_marker"]
    assert prepared["marker"] == (1 << 31) - 1
    assert seen[0]["target_clock_condition"] == exchange.condition
    assert "target_clock_condition" not in seen[1]
    assert len(finished) == 1


def test_backend_private_payload_keeps_exchange_and_public_result_unchanged(
    monkeypatch,
):
    calls = []
    result = {"ok": True, "target_clock": {"unchanged": "receipt"}}
    finishes = []
    backend = windows.WindowsBackend()
    backend._ps = "inert-powershell"

    def bridge(argv, **kwargs):
        payload = json.loads(Path(argv[-1]).read_text())
        calls.append(payload)
        assert Path(argv[-1]).stat().st_mode & 0o077 == 0
        return SimpleNamespace(stdout=json.dumps(result), stderr="", returncode=0)

    monkeypatch.setattr(windows, "_translate", lambda path, flag: path)
    monkeypatch.setattr(windows.subprocess, "run", bridge)
    exchange = SimpleNamespace(
        condition={"original": True}, finish=lambda *a, **k: finishes.append((a, k))
    )
    token = CURRENT_TARGET_CLOCK.set(exchange)
    try:
        assert backend.raw("key", text="x", _input_marker=99) == result
        assert backend.raw("left_mouse_up") == result
        assert backend.raw("presence_idle", _input_marker=99) == result
    finally:
        CURRENT_TARGET_CLOCK.reset(token)
    assert (
        calls[0]["_input_marker"] == calls[1]["_input_marker"] == backend._input_marker
    )
    assert calls[0]["_input_marker"] != 99
    assert (
        calls[0]["target_clock_condition"] is not exchange.condition
    )  # JSON round trip
    assert calls[0]["target_clock_condition"] == exchange.condition
    assert "_input_marker" not in calls[2] and "target_clock_condition" not in calls[2]
    assert len(finishes) == 2 and all(args == (result,) for args, _ in finishes)
    assert all(kwargs == {"require_injection": True} for _, kwargs in finishes)


def test_bridge_stamps_every_actual_sendinput_including_emergency_release():
    source = (PACKAGE / "bridge.ps1").read_text()
    assert source.count("uint count = SendInput(") == 2
    emit = source.split("public static void Emit(INPUT[] inputs) {")[1].split(
        "public static void Release"
    )[0]
    release = source.split("public static void Release(INPUT[] inputs) {")[1].split(
        "public delegate"
    )[0]
    assert (
        emit.index("Stamp(inputs);") < emit.index("Check();") < emit.index("SendInput(")
    )
    assert release.index("Stamp(inputs);") < release.index("SendInput(")
    assert "Check();" not in release and "Injection =" not in release
    assert "inputs[i].ki.dwExtraInfo = inputMarker" in source
    assert "inputs[i].mi.dwExtraInfo = inputMarker" in source
    assert "if (inputMarker == IntPtr.Zero) return;" in source
    assert "marker <= 0" in source and "marker > Int32.MaxValue" in source
    dispatch = source[source.index("$req = Get-Content") :]
    assert dispatch.index("ConfigureMarker") < dispatch.index("switch ($action)")
    assert "$req._input_marker -isnot [int]" in source
    assert "$req._input_marker -isnot [long]" in source
    assert "$out._input_marker" not in source


def test_probe_readiness_cleanup_bounds_privacy_and_no_input_source_contract():
    source = PROBE_PS1.read_text()
    run = source[source.index("public static object Run") :]
    assert run.index("keyboardHook = SetWindowsHookEx") < run.index("probe_ready")
    assert run.index("mouseHook = SetWindowsHookEx") < run.index("probe_ready")
    assert run.index("Poll();") < run.index("probe_ready")
    assert run.index("deadline = new Timer") < run.index("probe_ready")
    assert "Console.Out.Flush();" in source
    assert "durationMs > 35000" in source
    assert "EventCap = 256, ChangeCap = 32" in source
    assert (
        "events.Count >= EventCap" in source and "changes.Count < ChangeCap" in source
    )
    assert source.count("return CallNextHookEx(") == 2
    assert source.count("catch { callbackErrors++; }") == 2
    assert "Marshal.PtrToStructure" not in source
    record = source.split("static void Record(")[1].split("static IntPtr Keyboard")[0]
    assert record.count("Marshal.ReadInt32(") == 2
    assert record.count("Marshal.ReadIntPtr(") == 1
    for field in ("flags", "time", "extra"):
        assert f'Offset(keyboard, "{field}")' in record
    for forbidden in (
        "SendInput",
        "keybd_event",
        "mouse_event",
        "SetCursorPos",
        "GetClipboard",
        "SetClipboard",
        "native_pid",
        "foreground_hwnd",
        "basename=",
        "error=$_.Exception.Message",
    ):
        assert forbidden not in source
    for required in (
        "UnhookWindowsHookEx(mouseHook)",
        "UnhookWindowsHookEx(keyboardHook)",
        "KillTimer(",
        "deadline.Dispose()",
        "CloseDesktop(inputDesktop)",
        "TempFileCollection",
        "$provider.Dispose()",
        "$tempFiles.Dispose()",
        "[System.IO.File]::Delete($ownedPath)",
    ):
        assert required in source


def test_hook_layout_offsets_match_x64_contract_without_reading_payload():
    class Keyboard(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint32) for n in ("vk", "scan", "flags", "time")] + [
            ("extra", ctypes.c_void_p)
        ]

    class Mouse(ctypes.Structure):
        _fields_ = [
            (n, ctypes.c_uint32) for n in ("x", "y", "data", "flags", "time")
        ] + [("extra", ctypes.c_void_p)]

    assert (Keyboard.flags.offset, Keyboard.time.offset, Keyboard.extra.offset) == (
        8,
        12,
        16,
    )
    assert (Mouse.flags.offset, Mouse.time.offset) == (12, 16)
    assert Mouse.extra.offset == (24 if ctypes.sizeof(ctypes.c_void_p) == 8 else 20)
    assert "Marshal.OffsetOf(" in PROBE_PS1.read_text()


def test_ssh_payload_contains_exact_target_owner_diagnostic_siblings():
    from amplifier_module_tool_computer_use.ssh_transport import _build_payload

    with tarfile.open(fileobj=io.BytesIO(_build_payload(PACKAGE)), mode="r:gz") as tf:
        for name in ("windows_input_diagnostics.py", "input_probe.ps1"):
            member = tf.extractfile(f"amplifier_cu_agent/{name}")
            assert member is not None
            assert member.read() == (PACKAGE / name).read_bytes()
