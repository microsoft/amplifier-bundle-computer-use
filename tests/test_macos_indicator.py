"""Headless protocol/adapter/native-shape fakes; NOT Mac pixel qualification."""

from __future__ import annotations

import builtins
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "modules/tool-computer-use/amplifier_module_tool_computer_use"
sys.path.insert(0, str(PACKAGE.parent))

from amplifier_module_tool_computer_use import macos_indicator as adapter
from amplifier_module_tool_computer_use import macos_indicator_helper as helper


def command(seq=1, op="start", state="viewing", **extra):
    return {"v": 1, "g": "a" * 32, "s": seq, "op": op, "state": state, **extra}


def started():
    p = helper.Protocol()
    p.accept(command())
    return p


class Surface:
    def __init__(self):
        self.states = []
        self.visible = True
        self.absent = True
        self.removals = 0

    def apply(self, state):
        self.states.append(state)

    def confirmed(self):
        return self.visible

    def remove(self):
        self.removals += 1
        return self.absent


def runtime():
    now, replies, surface = [10.0], [], Surface()
    run = helper.PanelRuntime(started(), surface, replies.append, clock=lambda: now[0])
    return run, now, replies, surface


@pytest.mark.parametrize(
    "data",
    [
        b"{}\n\n",
        b'{"v":1,"v":1}\n',
        b"[]\n",
        b"\xff\n",
        b"{}\x00\n",
        b"{}",
        helper.encode_frame(command()) + b"\n",
        b"x" * 513 + b"\n",
        b'{"x":NaN}\n',
    ],
)
def test_bad_frames(data):
    with pytest.raises(helper.ProtocolError):
        row = helper.decode_frame(data)
        helper.validate_command(row)


@pytest.mark.parametrize(
    "change",
    [
        {"v": True},
        {"s": True},
        {"s": 0},
        {"s": 2**54},
        {"s": 1.0},
        {"g": "../private"},
        {"g": "A" * 32},
        {"state": "typed secret"},
        {"state": []},
        {"op": {}},
        {"prompt": "private"},
        {"op": "status", "state": "viewing"},
        {"op": "arbitrary"},
    ],
)
def test_closed_command_schema(change):
    with pytest.raises(helper.ProtocolError, match="^protocol_error$"):
        helper.validate_command(command(**change))


@pytest.mark.parametrize(
    "key,value", [("reason", []), ("state", {}), ("window", 1), ("loop", True)]
)
def test_closed_reply_schema(key, value):
    row = started().reply("ready", window=True, loop=1)
    row[key] = value
    with pytest.raises(helper.ProtocolError):
        helper.validate_reply(row)


@pytest.mark.parametrize(
    "row",
    [
        command(),
        command(3, "status", None),
        command(2, "status", None, g="b" * 32),
        command(2, "start", "controlling"),
    ],
)
def test_reject_duplicate_skipped_stale_generation(row):
    with pytest.raises(helper.ProtocolError):
        started().accept(row)


def test_transition_and_terminal_rejection():
    p = started()
    for seq, state in enumerate(("controlling", "paused", "viewing", "stopping"), 2):
        p.accept(command(seq, "update", state))
    with pytest.raises(helper.ProtocolError):
        p.accept(command(6, "update", "viewing"))
    p.accept(command(6, "update", "released"))
    with pytest.raises(helper.ProtocolError):
        p.accept(command(7, "update", "controlling"))
    p.accept(command(7, "close", None))
    with pytest.raises(helper.ProtocolError):
        p.accept(command(8, "status", None))


@pytest.mark.parametrize(
    "op,state", [("status", None), ("close", None), ("update", "released")]
)
def test_helper_late_traffic_before_tick_cannot_renew_expired_lease(op, state):
    run, now, replies, surface = runtime()
    run.tick()
    run.receive(helper.encode_frame(command(2, "update", "released")))
    run.tick()
    # The loop stays alive throughout; the separate stalled-loop watchdog is
    # irrelevant. Expiry happens between the last tick and late command intake.
    now[0] = 13.95
    run.tick()
    now[0] = 14.01
    try:
        run.receive(helper.encode_frame(command(3, op, state)))
    except helper.ProtocolError:
        pass
    now[0] = 14.05
    run.tick()
    assert run.reason == "connection_lost"
    assert run.protocol.state == "interrupted" and not run.finished
    assert run.last_command == 10.0 and run.protocol.sequence == 2
    assert surface.states[-1] == "interrupted"
    assert not any(
        row["event"] == "terminal" and row["state"] == "released" for row in replies
    )


@pytest.mark.parametrize("op", ["status", "close", "abort"])
def test_adapter_late_request_without_public_status_cannot_renew_lease(monkeypatch, op):
    indicator = inert_indicator(monkeypatch)
    try:
        assert indicator.start()["healthy"]
        with indicator._lock:
            indicator._last_reply = time.monotonic() - helper.WATCHDOG_SECONDS - 0.25
            seq = indicator._sequence
            result = indicator._request(op)
            assert result["state"] == "interrupted" and not result["healthy"]
            assert indicator._failed and indicator._sequence == seq
        indicator.close()
        assert indicator.status()["process_settled"]
    finally:
        indicator.abort()
        indicator._proc.stderr.close()


def test_helper_expiry_during_command_validation_does_not_refresh_or_show_release(
    monkeypatch,
):
    run, now, replies, surface = runtime()
    run.tick()
    now[0] = 13.99
    original = helper.validate_command

    def delayed_validation(row):
        original(row)
        now[0] = 14.01

    monkeypatch.setattr(helper, "validate_command", delayed_validation)
    run.receive(helper.encode_frame(command(2, "update", "released")))
    run.tick()
    assert run.reason == "connection_lost" and run.protocol.state == "interrupted"
    assert run.last_command == 10.0 and "released" not in surface.states
    assert replies[-1]["event"] == "unavailable"


@pytest.mark.parametrize("op", ["status", "close"])
@pytest.mark.parametrize("boundary", ["before_write", "before_reply_renewal"])
def test_adapter_expiry_during_exchange_cannot_be_refreshed(monkeypatch, op, boundary):
    indicator = adapter.MacOSIndicator()
    indicator._snapshot.update(
        available=True, ready=True, healthy=True, state="released"
    )
    indicator._last_reply, indicator._last_loop = 10.0, 1
    clock, writes = [13.99], []
    indicator._proc = SimpleNamespace(
        poll=lambda: None,
        stdin=SimpleNamespace(fileno=lambda: 101),
        stdout=SimpleNamespace(fileno=lambda: 102),
    )
    reply = {
        "v": 1,
        "g": indicator._generation,
        "s": 1,
        "event": "ack" if op == "status" else "terminal",
        "state": "released",
        "reason": "ok",
        "window": op == "status",
        "loop": 2,
    }

    def select_ready(readers, writers, errors, timeout):
        if writers and boundary == "before_write":
            clock[0] = 14.01
        return readers, writers, []

    def read_reply(_fd, _size):
        clock[0] = 14.01
        return helper.encode_frame(reply)

    monkeypatch.setattr(adapter.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(adapter.select, "select", select_ready)
    monkeypatch.setattr(
        adapter.os, "write", lambda fd, data: writes.append(data) or len(data)
    )
    monkeypatch.setattr(adapter.os, "read", read_reply)
    result = indicator._request(op)
    assert result["state"] == "interrupted" and result["reason"] == "connection_lost"
    assert not result["healthy"] and not result["surface_absent"]
    assert indicator._last_reply == 10.0 and indicator._failed
    assert len(writes) == (boundary == "before_reply_renewal")
    assert not indicator._request(op)["healthy"]
    assert len(writes) == (boundary == "before_reply_renewal")  # No replay.


def test_close_expired_without_intervening_public_status_never_sends_handoff(
    monkeypatch,
):
    indicator = inert_indicator(monkeypatch)
    try:
        assert indicator.start()["healthy"]
        assert indicator.update("released")["state"] == "released"
        indicator._stop.set()
        indicator._monitor.join(2)
        assert not indicator._monitor.is_alive()
        indicator._last_reply = time.monotonic() - helper.WATCHDOG_SECONDS - 0.25
        seq = indicator._sequence
        result = indicator.close()
        assert result["state"] == "interrupted" and result["process_settled"]
        assert indicator._failed and indicator._sequence == seq
        trace = indicator._proc.stderr.read()
        assert b'"op":"close"' not in trace
        assert b"final:interrupted" in trace
    finally:
        indicator.abort()
        indicator._proc.stderr.close()


def test_readiness_on_later_loop_and_persistent_reasoning_gap():
    run, now, replies, surface = runtime()
    assert replies == []
    surface.visible = False
    run.tick()
    assert replies == []
    now[0] += 0.1
    surface.visible = True
    run.tick()
    assert replies[-1]["event"] == "ready"
    assert replies[-1]["window"] and replies[-1]["loop"] == 2
    for seq in range(2, 8):
        now[0] += 0.5
        run.receive(helper.encode_frame(command(seq, "status", None)))
        run.tick()
    assert run.protocol.state == "viewing"
    assert surface.removals == 0
    assert surface.states == ["viewing"]


def test_released_is_explicit_and_stays_until_close_then_absence_ack():
    run, now, replies, surface = runtime()
    run.tick()
    run.receive(helper.encode_frame(command(2, "update", "released")))
    run.tick()
    assert replies[-1]["state"] == "released"
    # Long observation dwell needs no test-only production configuration.
    for seq in range(3, 65):
        now[0] += 0.5
        run.receive(helper.encode_frame(command(seq, "status", None)))
        run.tick()
    assert not run.finished and surface.removals == 0
    run.receive(helper.encode_frame(command(65, "close", None)))
    assert replies[-1]["event"] == "ack"  # No acknowledgement merely on receive.
    run.tick()
    assert run.finished and replies[-1]["event"] == "terminal"
    assert replies[-1]["state"] == "released" and not replies[-1]["window"]


@pytest.mark.parametrize("op", ["close", "abort"])
def test_active_teardown_is_interrupted_minimum_dwell(op):
    run, now, replies, surface = runtime()
    run.tick()
    run.receive(helper.encode_frame(command(2, op, None)))
    run.tick()
    assert surface.states[-1] == "interrupted" and not run.finished
    now[0] += helper.TERMINAL_DWELL_SECONDS
    run.tick()
    assert run.finished and replies[-1]["state"] == "interrupted"


@pytest.mark.parametrize("fault", ["watchdog", "parent", "surface", "eof"])
def test_loss_overrides_release_never_infers_done(monkeypatch, fault):
    run, now, replies, surface = runtime()
    run.tick()
    run.receive(helper.encode_frame(command(2, "update", "released")))
    run.tick()
    if fault == "watchdog":
        now[0] += helper.WATCHDOG_SECONDS + 0.01
    elif fault == "parent":
        monkeypatch.setattr(helper.os, "getppid", lambda: run.parent_pid + 1)
    elif fault == "surface":
        surface.visible = False
    else:
        run.interrupt()
    run.tick()
    assert run.protocol.state == "interrupted"
    assert run.protocol.closing
    assert surface.states[-1] == "interrupted"
    now[0] += helper.TERMINAL_DWELL_SECONDS + 0.01
    run.tick()
    assert run.finished


def test_failed_surface_removal_cannot_ack_release():
    run, now, replies, surface = runtime()
    run.tick()
    run.receive(helper.encode_frame(command(2, "update", "released")))
    run.tick()
    run.receive(helper.encode_frame(command(3, "close", None)))
    surface.absent = False
    now[0] += helper.TERMINAL_DWELL_SECONDS
    run.tick()
    assert not run.finished
    now[0] += 0.5
    run.tick()
    assert run.finished
    assert replies[-1]["reason"] == "surface_unconfirmed"
    assert replies[-1]["state"] == "interrupted"


def test_console_mismatch_and_no_console_are_closed(monkeypatch):
    monkeypatch.setattr(helper.sys, "platform", "darwin")
    monkeypatch.setattr(helper.os, "geteuid", lambda: 501)
    monkeypatch.setattr(helper.os, "stat", lambda _: SimpleNamespace(st_uid=502))
    assert helper.console_reason() == "console_user_mismatch"
    monkeypatch.setattr(helper.os, "stat", lambda _: SimpleNamespace(st_uid=0))
    assert helper.console_reason() == "console_unavailable"


def test_missing_cocoa_is_lazy_fixed_reason(monkeypatch):
    monkeypatch.setattr(helper, "console_reason", lambda: "ok")
    original = builtins.__import__

    def no_cocoa(name, *args, **kwargs):
        if name == "AppKit":
            raise ImportError("sensitive local information")
        return original(name, *args, **kwargs)

    rows = []
    monkeypatch.setattr(builtins, "__import__", no_cocoa)
    monkeypatch.setattr(helper, "_emit", rows.append)
    assert helper._native_main(started()) == 2
    assert rows[0]["reason"] == "cocoa_unavailable"
    assert "sensitive" not in json.dumps(rows)


# This separate test-only subprocess exercises the real framing + runtime over
# anonymous pipes. The production helper has NO inert/fake backend switch.
INERT_DRIVER = r"""
import importlib.util, os, select, sys, time
spec=importlib.util.spec_from_file_location("panel_protocol", sys.argv[1])
h=importlib.util.module_from_spec(spec); spec.loader.exec_module(h)
h.TERMINAL_DWELL_SECONDS=0.03
mode=sys.argv[2]
p=h._read_start()
if mode in ("console_unavailable","console_user_mismatch","cocoa_unavailable"):
    h._emit(p.reply("unavailable",mode)); sys.exit(2)
if mode=="die": sys.exit(2)
if mode=="timeout": time.sleep(10); sys.exit(2)
class Surface:
    def apply(self,state): pass
    def confirmed(self): return True
    def remove(self): return True
def emit(row):
    if mode=="droprelease" and row["state"]=="released": return
    if mode=="dieonstatus" and row["event"]=="ack": sys.exit(2)
    if mode=="stale": row["s"]-=1
    if mode=="wronggen": row["g"]="b"*32
    if mode=="extra": row["payload"]="forbidden"
    if mode=="oversize": os.write(1,b"x"*513); return
    if mode=="wrongloop": row["loop"]=0
    if mode=="wrongstate": row["state"]="released"
    h._emit(row)
r=h.PanelRuntime(p,Surface(),emit)
os.set_blocking(0,False); os.set_blocking(1,False)
buf=b""
while not r.finished:
    ready,_,_=select.select([0],[],[],0.01)
    if ready:
        data=os.read(0,513)
        if not data: r.interrupt()
        else:
            buf+=data
            if b"\n" in buf:
                line,buf=buf.split(b"\n",1)
                os.write(2,line+b"\n")
                try: r.receive(line+b"\n")
                except h.ProtocolError: r.interrupt("protocol_error")
    r.tick()
os.write(2,("final:"+p.state+"\n").encode())
if mode=="terminalhung": time.sleep(10)
sys.exit(0 if p.state=="released" and r.reason=="ok" else 2)
"""


def inert_indicator(monkeypatch, mode="ok"):
    indicator = adapter.MacOSIndicator()
    monkeypatch.setattr(adapter.sys, "platform", "darwin")

    def launch():
        proc = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-c",
                INERT_DRIVER,
                str(PACKAGE / "macos_indicator_helper.py"),
                mode,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        os.set_blocking(proc.stdin.fileno(), False)
        os.set_blocking(proc.stdout.fileno(), False)
        return proc

    monkeypatch.setattr(indicator, "_launch", launch)
    return indicator


def test_inert_adapter_entire_lifecycle_and_background_heartbeat(monkeypatch):
    indicator = inert_indicator(monkeypatch)
    try:
        result = indicator.start("viewing")
        assert result["available"] and result["ready"] and result["healthy"]
        seq = result["sequence"]
        deadline = time.monotonic() + 2
        while indicator.status()["sequence"] == seq and time.monotonic() < deadline:
            time.sleep(0.01)
        assert indicator.status()["sequence"] > seq  # No model status polling needed.
        for state in ("controlling", "paused", "viewing", "stopping", "released"):
            assert indicator.update(state)["state"] == state
        assert not indicator.status()["pixels_verified"]
        result = indicator.close()
        assert result["state"] == "released"
        assert (
            result["closed"] and result["surface_absent"] and result["process_settled"]
        )
        assert result["helper_exit"] == 0 and not result["healthy"]
        assert indicator.close() == result
    finally:
        indicator.abort()
        if indicator._proc.stderr:
            indicator._proc.stderr.close()


@pytest.mark.parametrize(
    "mode", ["die", "stale", "wronggen", "extra", "oversize", "wrongloop", "wrongstate"]
)
def test_inert_faults_fail_closed_and_settle(monkeypatch, mode):
    indicator = inert_indicator(monkeypatch, mode)
    result = indicator.start()
    assert not result["healthy"] and not result["ready"]
    assert result["state"] == "interrupted"
    assert result["process_settled"] and not result["surface_absent"]
    assert result["fault_settled"] and result["settlement_basis"] == "helper_reaped"
    assert not indicator.start()["healthy"]  # Never restart unknown generation.
    indicator.close()
    indicator._proc.stderr.close()


@pytest.mark.parametrize(
    "reason", ["console_unavailable", "console_user_mismatch", "cocoa_unavailable"]
)
def test_startup_unavailable_helper_reaped_is_failed_resource_settlement(
    monkeypatch, reason
):
    indicator = inert_indicator(monkeypatch, reason)
    try:
        result = indicator.start()
        assert result["state"] == "interrupted" and result["reason"] == reason
        assert not result["healthy"] and not result["ready"]
        assert result["process_settled"] and result["fault_settled"]
        assert result["settlement_basis"] == "helper_reaped"
        assert not result["surface_absent"]  # No invented native metadata ack.
        assert indicator.close()["fault_settled"]
    finally:
        indicator.abort()
        indicator._proc.stderr.close()


def test_never_launched_failure_has_explicit_resource_settlement(monkeypatch):
    indicator = adapter.MacOSIndicator()
    monkeypatch.setattr(adapter.sys, "platform", "darwin")
    monkeypatch.setattr(
        indicator, "_launch", lambda: (_ for _ in ()).throw(OSError("private"))
    )
    result = indicator.start()
    assert result["state"] == "interrupted" and result["reason"] == "helper_failed"
    assert result["fault_settled"] and result["process_settled"]
    assert (
        result["settlement_basis"] == "never_launched" and result["helper_exit"] is None
    )
    assert not result["surface_absent"] and not result["healthy"]
    assert indicator.close()["fault_settled"]


class SettlementProcess:
    """Inert wait/escalation controls, never an actual OS PID."""

    def __init__(self, outcome):
        self.returncode = None
        self.outcome = outcome
        self.signals = []
        self.waits = 0
        self.stdin = SimpleNamespace(closed=True, fileno=lambda: 101)
        self.stdout = SimpleNamespace(closed=True, fileno=lambda: 102)

    def wait(self, timeout):
        self.waits += 1
        if self.outcome == "wait_error":
            raise OSError("private")
        if self.outcome == "unknown" or (self.outcome == "killed" and self.waits < 3):
            raise subprocess.TimeoutExpired("private", timeout)
        if self.outcome != "no_returncode":
            self.returncode = -9 if self.outcome == "killed" else 2
        return self.returncode

    def terminate(self):
        self.signals.append("terminate")

    def kill(self):
        self.signals.append("kill")


@pytest.mark.parametrize("outcome", ["unknown", "no_returncode", "wait_error"])
def test_unknown_process_outcome_never_claims_fault_settlement(outcome):
    indicator = adapter.MacOSIndicator()
    indicator._proc = SettlementProcess(outcome)
    indicator._fail("reply_timeout")
    result = indicator.close()
    assert result["state"] == "interrupted" and not result["healthy"]
    assert not result["process_settled"] and not result["fault_settled"]
    assert not result["surface_absent"] and result["settlement_basis"] == "not_settled"
    assert result["helper_exit"] is None
    # Retained cleanup custody can settle later once exit really is observed.
    indicator._proc.outcome = "exit"
    recovered = indicator.close()
    assert recovered["fault_settled"] and recovered["process_settled"]
    assert recovered["state"] == "interrupted" and not recovered["healthy"]


def test_confirmed_kill_exit_settles_resource_but_never_becomes_release():
    indicator = adapter.MacOSIndicator()
    indicator._proc = SettlementProcess("killed")
    indicator._fail("reply_timeout")
    result = indicator.close()
    assert indicator._proc.signals == ["terminate", "kill"]
    assert result["state"] == "interrupted" and result["reason"] == "reply_timeout"
    assert result["process_settled"] and result["fault_settled"]
    assert result["helper_exit"] == -9 and not result["surface_absent"]
    assert result["settlement_basis"] == "helper_reaped" and not result["healthy"]


def test_pipe_setup_failure_retains_spawned_helper_custody(monkeypatch):
    indicator = adapter.MacOSIndicator()
    proc = SettlementProcess("exit")
    monkeypatch.setattr(adapter.sys, "platform", "darwin")
    monkeypatch.setattr(adapter.subprocess, "Popen", lambda *a, **kw: proc)
    monkeypatch.setattr(
        adapter.os, "set_blocking", lambda *_: (_ for _ in ()).throw(OSError("private"))
    )
    result = indicator.start()
    assert indicator._proc is proc and proc.waits == 1
    assert result["fault_settled"] and result["process_settled"]
    assert result["settlement_basis"] == "helper_reaped"  # Not never_launched.
    assert result["state"] == "interrupted" and not result["healthy"]


def test_unsupported_platform_proves_no_helper_created(monkeypatch):
    indicator = adapter.MacOSIndicator()
    monkeypatch.setattr(adapter.sys, "platform", "linux")
    monkeypatch.setattr(
        adapter.subprocess, "Popen", lambda *_a, **_k: pytest.fail("must not spawn")
    )
    result = indicator.start()
    assert result["fault_settled"] and result["process_settled"]
    assert result["settlement_basis"] == "never_launched"
    assert not result["healthy"] and not result["surface_absent"]


def test_health_loss_irreversible_nonblocking_and_no_mutation_replay(monkeypatch):
    indicator = inert_indicator(monkeypatch)
    try:
        assert indicator.start()["healthy"]
        with indicator._lock:
            indicator._last_reply = time.monotonic() - 20
            before = time.monotonic()
            assert indicator.status()["state"] == "interrupted"
            assert time.monotonic() - before < 0.1
        seq = indicator._sequence
        assert not indicator.update("released")["healthy"]
        assert indicator._sequence == seq
        assert indicator.close()["state"] == "interrupted"
        assert indicator.status()["process_settled"]
    finally:
        indicator.abort()
        indicator._proc.stderr.close()


def test_timeout_not_process_death_or_release(monkeypatch):
    indicator = inert_indicator(monkeypatch)
    try:
        assert indicator.start()["healthy"]
        monkeypatch.setattr(
            indicator, "_exchange", lambda *_: (_ for _ in ()).throw(TimeoutError())
        )
        result = indicator.update("released")
        assert result["reason"] == "reply_timeout"
        assert result["state"] == "interrupted"
        assert not result["process_settled"]
        assert not result["fault_settled"]
        indicator.close()
        assert indicator.status()["process_settled"]
        assert indicator.status()["fault_settled"]
    finally:
        indicator.abort()
        indicator._proc.stderr.close()


def test_real_lost_mutation_reply_is_not_replayed(monkeypatch):
    indicator = inert_indicator(monkeypatch, "droprelease")
    try:
        assert indicator.start()["healthy"]
        result = indicator.update("released")
        assert result["reason"] == "reply_timeout" and result["state"] == "interrupted"
        assert not result["process_settled"]
        indicator.close()
        trace = indicator._proc.stderr.read()
        assert trace.count(b'"op":"update"') == 1
        assert b'"op":"close"' not in trace and b'"op":"abort"' not in trace
        assert b"final:interrupted" in trace
        assert indicator.status()["process_settled"]
        assert indicator.status()["fault_settled"]
    finally:
        indicator.abort()
        indicator._proc.stderr.close()


def test_background_detects_helper_death_without_model_poll(monkeypatch):
    indicator = inert_indicator(monkeypatch, "dieonstatus")
    try:
        assert indicator.start()["healthy"]
        assert indicator._stop.wait(3)
        assert indicator.status()["state"] == "interrupted"
        assert not indicator.status()["healthy"]
        indicator.close()
        assert indicator.status()["process_settled"]
        assert indicator.status()["fault_settled"]
    finally:
        indicator.abort()
        indicator._proc.stderr.close()


def test_terminal_ack_is_not_process_settlement(monkeypatch):
    indicator = inert_indicator(monkeypatch, "terminalhung")
    try:
        assert indicator.start()["healthy"]
        assert indicator.update("released")["state"] == "released"
        result = indicator.close()
        assert result["surface_absent"]
        assert result["process_settled"] and result["helper_exit"] != 0
        assert result["state"] == "interrupted" and result["reason"] == "helper_failed"
    finally:
        indicator.abort()
        indicator._proc.stderr.close()


def test_terminal_publication_cannot_overwrite_concurrent_sticky_loss(monkeypatch):
    entered, resume = threading.Event(), threading.Event()

    class BarrierIndicator(adapter.MacOSIndicator):
        armed = False

        def __setattr__(self, name, value):
            if name == "_snapshot" and self.armed and value.get("closed"):
                self.armed = False
                entered.set()
                assert resume.wait(3)
            super().__setattr__(name, value)

    original = adapter.MacOSIndicator
    monkeypatch.setattr(adapter, "MacOSIndicator", BarrierIndicator)
    indicator = inert_indicator(monkeypatch)
    monkeypatch.setattr(adapter, "MacOSIndicator", original)
    results = []
    try:
        assert indicator.start()["healthy"]
        assert indicator.update("released")["state"] == "released"
        indicator.armed = True
        worker = threading.Thread(target=lambda: results.append(indicator.close()))
        worker.start()
        assert entered.wait(3)
        indicator._last_reply = time.monotonic() - 20
        assert indicator.status()["state"] == "interrupted"
        resume.set()
        worker.join(3)
        assert not worker.is_alive()
        assert results[0]["state"] == indicator.status()["state"] == "interrupted"
        assert results[0]["surface_absent"] and results[0]["process_settled"]
    finally:
        resume.set()
        indicator.abort()
        indicator._proc.stderr.close()


def test_signal_request_mid_command_never_reapplies_released(monkeypatch):
    run, now, replies, surface = runtime()
    run.tick()
    validate = helper.validate_command

    def request_during_validation(row):
        validate(row)
        run.request_interrupt()
        assert surface.states == ["viewing"]  # Handler is bookkeeping only.
        assert run.reason == "ok"

    monkeypatch.setattr(helper, "validate_command", request_during_validation)
    run.receive(helper.encode_frame(command(2, "update", "released")))
    run.tick()
    assert run.reason == "connection_lost" and run.protocol.state == "interrupted"
    assert "released" not in surface.states
    assert replies[-1]["event"] == "unavailable"
    now[0] += helper.TERMINAL_DWELL_SECONDS + 0.01
    run.tick()
    assert run.finished


def test_private_stop_wakes_only_own_application_queue():
    calls = []
    event = object()
    app = SimpleNamespace(
        stop_=lambda sender: calls.append(("stop", sender)),
        postEvent_atStart_=lambda value, front: calls.append(("post", value, front)),
    )
    toolkit = SimpleNamespace(
        NSEventTypeApplicationDefined=15,
        NSEvent=SimpleNamespace(
            otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_=lambda *args: (
                calls.append(("event", args)) or event
            )
        ),
    )
    helper._stop_application(app, toolkit)
    assert calls == [
        ("stop", None),
        ("event", (15, (0, 0), 0, 0, 0, None, 0, 0, 0)),
        ("post", event, False),
    ]


def test_real_helper_selftest_and_nonmac_unavailable_no_ui():
    path = PACKAGE / "macos_indicator_helper.py"
    result = subprocess.run(
        [sys.executable, "-I", str(path), "--self-test"], capture_output=True, timeout=5
    )
    assert result.returncode == 0 and b'"native_ui_exercised":false' in result.stdout
    if sys.platform != "darwin":
        result = subprocess.run(
            [sys.executable, "-I", str(path)],
            input=helper.encode_frame(command()),
            capture_output=True,
            timeout=5,
        )
        row = helper.decode_frame(result.stdout)
        assert result.returncode == 2 and row["reason"] == "unsupported_platform"
        assert result.stderr == b""


def test_constructor_import_and_launch_argv_are_inert(monkeypatch):
    calls = []
    fake = SimpleNamespace(
        stdin=SimpleNamespace(fileno=lambda: 101),
        stdout=SimpleNamespace(fileno=lambda: 102),
    )
    monkeypatch.setattr(
        adapter.subprocess, "Popen", lambda *a, **k: calls.append((a, k)) or fake
    )
    monkeypatch.setattr(adapter.os, "set_blocking", lambda *_: None)
    indicator = adapter.MacOSIndicator()
    assert calls == [] and indicator.status()["reason"] == "not_started"
    indicator._launch()
    argv, kwargs = calls[0][0][0], calls[0][1]
    assert argv == [sys.executable, "-I", str(PACKAGE / "macos_indicator_helper.py")]
    assert kwargs["close_fds"] and kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["stdin"] == kwargs["stdout"] == subprocess.PIPE
    assert "shell" not in kwargs and "env" not in kwargs


def test_packaging_dependency_marker_and_source_resources():
    project = tomllib.loads((PACKAGE.parent / "pyproject.toml").read_text())
    assert (
        "pyobjc-framework-Cocoa>=10.0; sys_platform == 'darwin'"
        in project["project"]["dependencies"]
    )
    for name in ("macos_indicator.py", "macos_indicator_helper.py"):
        path = PACKAGE / name
        assert importlib.util.find_spec(
            f"amplifier_module_tool_computer_use.{path.stem}"
        ).origin == str(path)
        assert len(hashlib.sha256(path.read_bytes()).hexdigest()) == 64


def test_fake_appkit_fixed_panel_no_focus_and_original_window_absence(monkeypatch):
    """Exercise real surface builder, not grep. Does not prove native semantics."""
    calls, panels, labels = [], [], []
    entries = [{"owner": os.getpid(), "number": 42}]

    class Object:
        def __getattr__(self, name):
            if name.startswith("set"):
                return lambda value: calls.append((name, value))
            raise AttributeError(name)

    class Panel(Object):
        @classmethod
        def alloc(cls):
            panel = cls()
            panel.closed = False
            panels.append(panel)
            return panel

        def initWithContentRect_styleMask_backing_defer_(self, *args):
            calls.append(("init", args))
            return self

        def contentView(self):
            return SimpleNamespace(
                addSubview_=lambda label: calls.append(("label", label))
            )

        def orderFront_(self, sender):
            calls.append(("orderFront", sender))

        def displayIfNeeded(self):
            pass

        def windowNumber(self):
            return -1 if self.closed else 42

        def isVisible(self):
            return not self.closed

        def isKeyWindow(self):
            return False

        def isMainWindow(self):
            return False

        def ignoresMouseEvents(self):
            return True

        def orderOut_(self, sender):
            calls.append(("orderOut", sender))

        def close(self):
            self.closed = True

    class Field(Object):
        @classmethod
        def labelWithString_(cls, text):
            label = cls()
            labels.append(label)
            return label

    app = SimpleNamespace(activationPolicy=lambda: 7, isActive=lambda: False)
    appkit = SimpleNamespace(
        NSPanel=Panel,
        NSScreen=SimpleNamespace(
            mainScreen=lambda: SimpleNamespace(
                visibleFrame=lambda: SimpleNamespace(
                    origin=SimpleNamespace(x=0, y=0),
                    size=SimpleNamespace(width=1200, height=800),
                )
            )
        ),
        NSWindowStyleMaskBorderless=0,
        NSWindowStyleMaskNonactivatingPanel=1,
        NSBackingStoreBuffered=2,
        NSStatusWindowLevel=3,
        NSWindowCollectionBehaviorCanJoinAllSpaces=1,
        NSWindowCollectionBehaviorStationary=2,
        NSWindowCollectionBehaviorFullScreenAuxiliary=4,
        NSColor=SimpleNamespace(
            colorWithCalibratedRed_green_blue_alpha_=lambda *args: args,
            blackColor=lambda: "black",
        ),
        NSTextField=Field,
        NSFont=SimpleNamespace(boldSystemFontOfSize_=lambda size: size),
        NSTextAlignmentCenter=2,
        NSApplication=SimpleNamespace(sharedApplication=lambda: app),
        NSApplicationActivationPolicyAccessory=7,
    )
    quartz = SimpleNamespace(
        CGWindowListCopyWindowInfo=lambda *_: entries,
        kCGWindowListOptionOnScreenOnly=1,
        kCGWindowListExcludeDesktopElements=2,
        kCGNullWindowID=0,
        kCGWindowOwnerPID="owner",
        kCGWindowNumber="number",
    )
    monkeypatch.setattr(helper, "console_reason", lambda: "ok")
    surface = helper._create_surface(appkit, quartz)
    assert len(panels) == len(labels) == 1
    assert surface.confirmed()
    assert not panels[0].canBecomeKeyWindow() and not panels[0].canBecomeMainWindow()
    assert ("setIgnoresMouseEvents_", True) in calls
    assert ("setCollectionBehavior_", 7) in calls
    assert calls[0] == ("init", (((16, 720), (480, 64)), 1, 2, False))
    for state in helper.STATES:
        surface.apply(state)
        assert ("setStringValue_", helper.LABELS[state]) in calls
    # WindowServer can retain the original window briefly after close.
    assert not surface.remove()
    entries.clear()
    assert surface.remove()  # Prove original ID absent, never the invalidated -1.
    assert not surface.confirmed()
