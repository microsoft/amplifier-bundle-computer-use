"""Inert actual-method regressions: no SSH, PowerShell, display or OS input.

The remote fixture uses the real controller -> RemoteBackend wire call ->
RemoteAgent typing handler -> WindowsBackend method. Only transport and the
bridge's clock/input effects are doubles. Timing is illustrative, not hardware
qualification or attribution of any historical live event.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

from amplifier_module_tool_computer_use import presence
from amplifier_module_tool_computer_use.backend import BackendError, ScreenGeometry
from amplifier_module_tool_computer_use.coexistence_guard import CoexistenceGuard
from amplifier_module_tool_computer_use.remote_agent import RemoteAgent
from amplifier_module_tool_computer_use.remote_backend import RemoteBackend
from amplifier_module_tool_computer_use.ssh_transport import SshConnectError
from amplifier_module_tool_computer_use.target_clock import CURRENT_TARGET_CLOCK
from amplifier_module_tool_computer_use.windows import WindowsBackend
from test_neutral_computer import Backend as NeutralBackend
from test_neutral_computer import cu, run

TEXT = "powershell.exe -NoLogo -NoProfile"


class Clock:
    def __init__(self):
        self.now = 100.0
        self.last_input = 0.0
        self.external_events = 0
        self.after_sleep = None
        self.after_inject = None

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        if self.after_sleep:
            self.after_sleep()

    def external(self):
        self.now += 0.050
        self.last_input = self.now
        self.external_events += 1


@pytest.fixture(params=["remote", "windows"])
def rig(request, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(presence, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(
        cu,
        "time",
        SimpleNamespace(
            monotonic=clock.monotonic, sleep=clock.sleep, time=clock.monotonic
        ),
    )
    injections, wires, halts, released = [], [], [], []
    windows = WindowsBackend.__new__(WindowsBackend)
    windows._timeout = 1

    def raw(operation, **kwargs):
        # A bridge call costs time even on the local WSL path. Idle is read
        # fresh; synthetic input updates the same last-input counter as a user.
        clock.now += 0.120

        def observation():
            return {
                "version": 1,
                "epoch": "synthetic-windows-boot-session",
                "now_ms": round((clock.now + 10000) * 1000),
                "last_input_ms": round((clock.last_input + 10000) * 1000),
            }

        if operation == "presence_idle":
            return {
                "ok": True,
                "idle_ms": (clock.now - clock.last_input) * 1000,
                "observation": observation(),
            }
        assert operation in {"type", "key"}
        exchange = CURRENT_TARGET_CLOCK.get()
        if exchange is not None:
            condition = exchange.condition
            fresh = observation()
            baseline = condition["inject_before_ms"]
            active = (
                (fresh["last_input_ms"] != condition["last_input_ms"])
                if baseline is None
                else (fresh["last_input_ms"] - baseline > condition["guard_ms"])
            )
            if active:
                result = {
                    "ok": False,
                    "error": "external input before emission",
                    "target_clock": {"observation": fresh, "injection": None},
                }
                exchange.finish(result, require_injection=True)
                return result
        injected_at = observation()["now_ms"]
        clock.last_input = clock.now
        injections.append((operation, kwargs["text"]))
        if clock.after_inject:
            clock.after_inject()
        result = {
            "ok": True,
            "target_clock": {
                "observation": observation(),
                "injection": {
                    "epoch": "synthetic-windows-boot-session",
                    "before_ms": injected_at,
                    "after_ms": injected_at,
                    "count": 2,
                },
            },
        }
        if exchange is not None:
            exchange.finish(result, require_injection=True)
        return result

    windows.raw = raw
    agent = SimpleNamespace(
        backend=windows, read_only=False, _HANDLERS=RemoteAgent._HANDLERS
    )

    class Transport:
        fail_type = False
        lose_ack = False
        input_tail = 0.005
        presence_tail = 0.005
        external_at = None
        corrupt_type_response = None

        def send(self, line, **kwargs):
            data = json.loads(line)
            op, args = data["op"], data["args"]
            wires.append((op, args))
            clock.now += 0.020
            if self.external_at == "outbound" and op == "type_text":
                self.external_at = None
                clock.external()
            if self.fail_type and op == "type_text":
                raise SshConnectError("Lost response; input outcome unknown")
            from amplifier_module_tool_computer_use.wire import Request

            response = RemoteAgent._dispatch(
                agent, Request(id=data["id"], op=op, args=args)
            )
            if self.lose_ack and op == "type_text":
                raise SshConnectError("Input applied; acknowledgement lost")
            clock.now += (
                self.presence_tail if op == "presence_idle" else self.input_tail
            )
            if self.external_at == "presence-tail" and op == "presence_idle":
                self.external_at = None
                clock.external()
            if self.corrupt_type_response is not None and op == "type_text":
                return json.dumps(
                    {"id": data["id"], "ok": False, "error": self.corrupt_type_response}
                ).encode()
            return response.encode()

    transport = Transport()
    if request.param == "remote":
        backend = RemoteBackend({"_host": "synthetic-target", "_transport": transport})
        backend._connected = True
        backend.presence_platform = "windows-wsl2"
    else:
        backend = windows
    backend.screen_geometry = lambda: ScreenGeometry(800, 600, 0, 0)

    def no_monitors():
        raise BackendError("Synthetic backend has no monitor enumeration")

    backend.list_monitors = no_monitors
    tool = cu.ComputerTool(backend, {"read_only": False, "gate_writes": False})
    tool.resolve_display()
    # Disclosure rendering is covered separately; never create an OS overlay
    # in this actual-method input/guard fixture.
    monkeypatch.setattr(tool, "_ensure_announced", lambda: None)
    monkeypatch.setattr(tool, "_sync_remote_announcement_state", lambda guard: None)
    monitor = presence.PresenceMonitor(
        backend.presence_idle_ms,
        platform="windows-wsl2",
        target_clock_source=backend.presence_observation,
    )
    target = ["original-window"]
    guard = CoexistenceGuard(
        monitor,
        release_all=lambda reason: released.append(reason) or [],
        target_source=lambda: target[0],
    )
    tool._coexistence_guard = guard
    monkeypatch.setattr(cu, "record_halt", lambda *a, **k: halts.append((a, k)))
    after = guard.after_event
    timestamps = []

    def accounted():
        timestamps.append(clock.monotonic())
        after()

    guard.after_event = accounted
    return SimpleNamespace(
        tool=tool,
        backend=backend,
        windows=windows,
        transport=transport,
        clock=clock,
        guard=guard,
        monitor=monitor,
        injections=injections,
        wires=wires,
        timestamps=timestamps,
        halts=halts,
        released=released,
        target=target,
    )


def test_own_33_character_typing_then_enter_is_not_human_activity(rig):
    assert len(TEXT) == 33
    assert run(rig.tool.execute({"action": "type", "text": TEXT})).success
    assert rig.injections == [("type", char) for char in TEXT]
    assert len(rig.timestamps) == 33
    assert rig.monitor._last_inject_monotonic is None
    assert rig.monitor.target_condition()["inject_before_ms"] is not None
    rig.clock.now += 15.844  # a delayed next action, not reconstructed live timing
    assert run(rig.tool.execute({"action": "key", "text": "ENTER"})).success
    assert rig.injections[-1] == ("key", "ENTER")
    assert rig.clock.external_events == 0
    assert rig.monitor.last_snapshot.state is not presence.PresenceState.HUMAN_ACTIVE
    assert rig.monitor.guard_ms == presence.GUARD_MS["windows-wsl2"]
    assert len(rig.timestamps) == 34


def test_own_input_does_not_become_external_with_asymmetric_response_tails(rig):
    if not isinstance(rig.backend, RemoteBackend):
        pytest.skip("Remote response-tail discriminator")
    rig.transport.input_tail = 0.010
    rig.transport.presence_tail = 0.046518420995
    result = run(rig.tool.execute({"action": "type", "text": TEXT}))
    assert result.success, result.error
    assert rig.clock.external_events == 0
    assert rig.injections == [("type", char) for char in TEXT]


def test_external_input_is_not_hidden_by_a_long_write_response_tail(rig):
    if not isinstance(rig.backend, RemoteBackend):
        pytest.skip("Remote response-tail discriminator")
    rig.transport.input_tail = 0.200
    rig.transport.presence_tail = 0.005

    def external_during_write_response():
        # Target input occurs after injection, before its delayed acknowledgement.
        # Controller receipt-time bookkeeping must not credit that event as ours.
        rig.clock.external()

    rig.clock.after_inject = external_during_write_response
    result = run(rig.tool.execute({"action": "type", "text": TEXT}))
    assert not result.success
    assert result.error["type"] == "HaltedError"
    assert rig.injections == [("type", TEXT[0])]


@pytest.mark.parametrize("phase", ["presence-tail", "outbound"])
def test_external_input_during_transport_refuses_before_next_emission(rig, phase):
    if not isinstance(rig.backend, RemoteBackend):
        pytest.skip("Remote transport-only contract")
    # Establish a target-clock baseline before testing the next invocation.
    assert run(rig.tool.execute({"action": "key", "text": "CTRL+A"})).success
    previous = list(rig.injections)
    rig.transport.external_at = phase
    result = run(rig.tool.execute({"action": "type", "text": TEXT}))
    assert not result.success
    assert result.error["type"] == "HaltedError"
    assert rig.injections == previous
    assert rig.guard.halted
    assert len(rig.halts) == 1


def test_malformed_error_after_possible_input_latches_unknown_without_retry(rig):
    if not isinstance(rig.backend, RemoteBackend):
        pytest.skip("Remote wire-only contract")
    rig.transport.corrupt_type_response = "corrupt"
    result = run(rig.tool.execute({"action": "type", "text": TEXT}))
    assert not result.success
    assert result.error["type"] == "HaltedError"
    assert rig.guard.halted
    assert rig.guard._halt_snapshot.state is presence.PresenceState.UNKNOWN
    assert len(rig.halts) == 1
    assert rig.injections == [("type", TEXT[0])]
    rig.clock.now += 100
    assert not run(rig.tool.execute({"action": "key", "text": "ENTER"})).success
    assert rig.injections == [("type", TEXT[0])]


def test_signature_without_capability_cannot_skip_controller_callbacks(rig):
    # Model the old shape: accepts guard but discards it. Absence of the
    # explicit capability forces controller ownership, despite that signature.
    rig.backend.type_text_guarded = False
    real_type = rig.backend.type_text
    received_guards = []

    def legacy(text, guard=None):
        received_guards.append(guard)
        return real_type(text)

    rig.backend.type_text = legacy
    rig.tool._run("type", {"text": TEXT})
    rig.tool._run("key", {"text": "ENTER"})
    assert received_guards == [None] * 33
    assert len(rig.timestamps) == 34 and rig.clock.external_events == 0


@pytest.mark.parametrize("stop", ["external", "pause", "target", "durable"])
def test_mid_typing_stop_refuses_remaining_characters_and_enter(rig, stop):
    def stop_after_first():
        rig.clock.after_sleep = None
        if stop == "external":
            rig.clock.external()
        elif stop == "pause":
            rig.guard.pause.set("manual_test")
        elif stop == "target":
            rig.target[0] = "different-window"
        else:
            snap = replace(
                rig.monitor.last_snapshot, state=presence.PresenceState.HUMAN_ACTIVE
            )
            rig.guard.durable_halt_poll = lambda: snap

    rig.clock.after_sleep = stop_after_first
    result = run(rig.tool.execute({"action": "type", "text": TEXT}))
    assert not result.success
    assert (
        result.error["type"]
        == {
            "external": "HaltedError",
            "pause": "PausedError",
            "target": "TargetChangedError",
            "durable": "HaltedError",
        }[stop]
    )
    assert rig.injections == [("type", TEXT[0])]
    assert len(rig.timestamps) == 1
    assert rig.released
    if stop in {"external", "durable"}:
        assert len(rig.halts) == 1
        assert not run(rig.tool.execute({"action": "key", "text": "ENTER"})).success
        assert rig.injections == [("type", TEXT[0])]


@pytest.mark.parametrize("pacing", [None, 0])
@pytest.mark.parametrize("stop", ["latched", "durable", "paused"])
def test_preexisting_halt_or_pause_refuses_all_typing(rig, pacing, stop):
    rig.tool._type_pacing_ms = pacing
    snap = replace(rig.monitor.sample(), state=presence.PresenceState.HUMAN_ACTIVE)
    if stop == "latched":
        rig.guard.seed_halted(snap)
    elif stop == "durable":
        rig.guard.durable_halt_poll = lambda: snap
    else:
        rig.guard.pause.set("manual_test")
    assert not run(rig.tool.execute({"action": "type", "text": TEXT})).success
    assert not rig.injections and not rig.timestamps


def test_zero_pacing_keeps_per_character_checks_and_accounting(rig):
    rig.tool._type_pacing_ms = 0
    rig.tool._run("type", {"text": TEXT})
    assert rig.injections == [("type", char) for char in TEXT]
    assert len(rig.timestamps) == 33
    rig.tool._run("key", {"text": "ENTER"})
    assert len(rig.timestamps) == 34


@pytest.mark.parametrize("stop", ["external", "durable", "pause", "target"])
def test_zero_pacing_still_refuses_remaining_characters(rig, stop):
    rig.tool._type_pacing_ms = 0
    original = rig.guard.after_event

    def stop_after_first():
        original()
        if stop == "external":
            rig.clock.external()
        elif stop == "pause":
            rig.guard.pause.set("manual_test")
        elif stop == "target":
            rig.target[0] = "different-window"
        else:
            snap = replace(
                rig.monitor.last_snapshot, state=presence.PresenceState.HUMAN_ACTIVE
            )
            rig.guard.durable_halt_poll = lambda: snap

    rig.guard.after_event = stop_after_first
    result = run(rig.tool.execute({"action": "type", "text": TEXT}))
    assert not result.success
    assert (
        result.error["type"]
        == {
            "external": "HaltedError",
            "durable": "HaltedError",
            "pause": "PausedError",
            "target": "TargetChangedError",
        }[stop]
    )
    assert rig.injections == [("type", TEXT[0])]
    assert len(rig.timestamps) == 1


def test_failed_bridge_input_does_not_record_success(rig):
    def fail(*a, **kw):
        return {"ok": False, "error": "Synthetic bridge failure"}

    rig.windows.raw = fail
    assert not run(rig.tool.execute({"action": "type", "text": TEXT})).success
    assert not rig.timestamps and not rig.injections


def test_lost_remote_response_is_never_retried_or_accounted_as_success(rig):
    if not isinstance(rig.backend, RemoteBackend):
        pytest.skip("Remote wire-only contract")
    rig.transport.fail_type = True
    result = run(rig.tool.execute({"action": "type", "text": TEXT}))
    assert not result.success
    assert len([op for op, _ in rig.wires if op == "type_text"]) == 1
    assert not rig.timestamps


def test_lost_ack_after_input_does_not_replay_the_character(rig):
    if not isinstance(rig.backend, RemoteBackend):
        pytest.skip("Remote wire-only contract")
    rig.transport.lose_ack = True
    result = run(rig.tool.execute({"action": "type", "text": TEXT}))
    assert not result.success
    assert rig.injections == [("type", TEXT[0])]
    assert len([op for op, _ in rig.wires if op == "type_text"]) == 1
    assert not rig.timestamps


def test_cancellation_stops_after_in_flight_character_without_replay(rig, monkeypatch):
    entered, release, settled = threading.Event(), threading.Event(), threading.Event()
    exits = []
    real_type = rig.backend.type_text

    def blocked(text, guard=None):
        entered.set()
        assert release.wait(5)
        return real_type(text, guard=guard)

    rig.backend.type_text = blocked
    monkeypatch.setattr(rig.tool, "_band_enter", lambda: "owned-band")
    monkeypatch.setattr(
        rig.tool, "_band_exit", lambda token: (exits.append(token), settled.set())
    )

    async def scenario():
        task = asyncio.create_task(rig.tool.execute({"action": "type", "text": TEXT}))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not exits
        finally:
            release.set()
        assert await asyncio.to_thread(settled.wait, 5)

    run(scenario())
    assert rig.injections == [("type", TEXT[0])]
    assert len(rig.timestamps) == 1
    assert exits == ["owned-band"]


def test_read_only_still_refuses_before_transport_input(rig):
    rig.tool._read_only = True
    assert not run(rig.tool.execute({"action": "type", "text": TEXT})).success
    assert not rig.injections


def test_disk_halt_clear_does_not_clear_cached_guard_and_fresh_guard_can_resume(rig):
    snap = replace(rig.monitor.sample(), state=presence.PresenceState.HUMAN_ACTIVE)
    disk = [snap]
    rig.guard.durable_halt_poll = lambda: disk[0]
    assert not run(rig.tool.execute({"action": "type", "text": TEXT})).success
    disk[0] = None  # inert model of an explicit reset, never a real halt file
    assert not run(rig.tool.execute({"action": "type", "text": TEXT})).success
    assert not rig.injections
    rig.tool._coexistence_guard = CoexistenceGuard(
        presence.PresenceMonitor(
            rig.backend.presence_idle_ms,
            platform="windows-wsl2",
            target_clock_source=rig.backend.presence_observation,
        ),
        release_all=lambda reason: [],
        durable_halt_poll=lambda: disk[0],
    )
    assert run(rig.tool.execute({"action": "type", "text": TEXT})).success


def test_cancelled_typing_keeps_disclosure_and_pinned_halt_until_worker_settles(
    rig, monkeypatch
):
    entered, release, settled = threading.Event(), threading.Event(), threading.Event()
    exits = []
    monkeypatch.setattr(rig.tool, "_band_enter", lambda: "owned-band")
    monkeypatch.setattr(
        rig.tool, "_band_exit", lambda token: (exits.append(token), settled.set())
    )
    real_type = rig.backend.type_text

    def blocked(text, guard=None):
        entered.set()
        assert release.wait(5)
        return real_type(text, guard=guard)

    rig.backend.type_text = blocked

    async def scenario():
        task = asyncio.create_task(rig.tool.execute({"action": "type", "text": TEXT}))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not exits
            # The operation remains on its original binding. A late durable
            # halt must still be recorded exactly once by its executing worker.
            snap = replace(
                rig.monitor.sample(), state=presence.PresenceState.HUMAN_ACTIVE
            )
            rig.guard.seed_halted(snap)
        finally:
            release.set()
        assert await asyncio.to_thread(settled.wait, 5)

    run(scenario())
    assert exits == ["owned-band"]
    assert len(rig.halts) == 1
    assert not rig.injections and not rig.timestamps


def test_queued_ungated_type_rechecks_policy_before_dispatch(rig, monkeypatch):
    before, release = threading.Event(), threading.Event()
    original = rig.tool._run_flat_step

    def queued(*args):
        before.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(rig.tool, "_run_flat_step", queued)

    async def scenario():
        task = asyncio.create_task(rig.tool.execute({"action": "type", "text": TEXT}))
        try:
            assert await asyncio.to_thread(before.wait, 5)
            rig.tool._read_only = True
        finally:
            release.set()
        return await task

    assert not run(scenario()).success
    assert not rig.injections and not rig.timestamps


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "left_click", "coordinate": [20, 30]},
        {"action": "key", "text": "ENTER"},
        {"action": "key", "text": "ENTER", "repeat": 3},
    ],
    ids=["click", "key", "key-repeat"],
)
def test_cancelled_lock_waiter_never_dispatches_and_cleans_up_once(
    payload, monkeypatch
):
    backend = NeutralBackend()
    tool = cu.ComputerTool(backend, {})
    tool.resolve_display()
    entered, proceed, waiting, settled = (threading.Event() for _ in range(4))
    worker = []
    exits = []
    original_lock = tool._announce_lock
    method = "_run_key_press" if payload["action"] == "key" else "_run_flat_step"
    original_step = getattr(tool, method)

    def queued(*args):
        worker.append(threading.get_ident())
        entered.set()
        assert proceed.wait(5)
        return original_step(*args)

    class ObservedLock:
        # Observe contention on the REAL operation lock, not a replacement
        # dispatch implementation. Other preflight lock users are unchanged.
        def __enter__(self):
            if worker and threading.get_ident() == worker[0]:
                waiting.set()
            return original_lock.__enter__()

        def __exit__(self, *args):
            return original_lock.__exit__(*args)

    monkeypatch.setattr(tool, method, queued)
    monkeypatch.setattr(tool, "_announce_lock", ObservedLock())
    monkeypatch.setattr(tool, "_ensure_announced", lambda: None)
    monkeypatch.setattr(tool, "_band_enter", lambda: "owned-band")
    monkeypatch.setattr(
        tool, "_band_exit", lambda token: (exits.append(token), settled.set())
    )

    async def scenario():
        task = asyncio.create_task(tool.execute(payload))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            original_lock.acquire()
            try:
                proceed.set()
                assert await asyncio.to_thread(waiting.wait, 5)
                assert not backend.calls
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert not backend.calls
                assert not exits
            finally:
                original_lock.release()
            assert await asyncio.to_thread(settled.wait, 5)
        finally:
            proceed.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    run(scenario())
    assert not backend.calls
    assert exits == ["owned-band"]
