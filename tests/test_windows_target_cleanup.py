"""Actual Windows cleanup methods with inert bridge/transport effects only.

These are synthetic controller/ledger regressions, not Windows qualification.
ComputerTool.execute -> WindowsBackend.raw is real; the remote variant adds the
real RemoteBackend, wire dispatcher and RemoteAgent mouse handlers. No process,
SSH connection, PowerShell, display, input device or deadman thread is started.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import amplifier_module_tool_computer_use as cu
from amplifier_module_tool_computer_use import ledger as ledger_module
from amplifier_module_tool_computer_use import presence, windows
from amplifier_module_tool_computer_use.coexistence_guard import (
    CoexistenceGuard,
    HaltedError,
)
from amplifier_module_tool_computer_use.ledger import HeldInputLedger
from amplifier_module_tool_computer_use.remote_agent import RemoteAgent
from amplifier_module_tool_computer_use.remote_backend import RemoteBackend
from amplifier_module_tool_computer_use.ssh_transport import SshConnectError
from amplifier_module_tool_computer_use.target_clock import (
    CURRENT_TARGET_CLOCK,
    TargetClockFailure,
    target_clock_exchange,
)
from amplifier_module_tool_computer_use.windows import WindowsBackend
from amplifier_module_tool_computer_use.wire import Request

TOKEN = "mouse:left"
EPOCH = "inert-windows-boot/session"
DOWN = {"action": "left_mouse_down", "coordinate": [10, 20]}
UP = {"action": "left_mouse_up", "coordinate": [110, 120]}


def run(coro):
    with asyncio.Runner(loop_factory=asyncio.new_event_loop) as runner:
        return runner.run(coro)


class InertTimer:
    """Replace OS scheduling, not the real ledger's registration/release logic."""

    def __init__(self, *args, **kwargs):
        self.daemon = False

    def start(self):
        pass

    def cancel(self):
        pass


@pytest.fixture
def make_rig(monkeypatch):
    rigs = []
    monkeypatch.setattr(
        ledger_module,
        "threading",
        SimpleNamespace(RLock=threading.RLock, Timer=InertTimer),
    )

    def make(
        *, remote=False, agent_owned=False, local_ledger=True, operator_override=False
    ):
        state = SimpleNamespace(
            now=10000,
            last=1000,
            fail_cleanup=False,
            reject_up=False,
            down_result="valid",
            foreground="inert-owned-window",
            focus_result="valid",
            focus_hook=lambda: None,
            write_foreground=None,
        )
        calls, emitted, wires, halts, releases, cleanup_runs, samples = (
            [] for _ in range(7)
        )
        ledgers = {}
        monkeypatch.setattr(
            presence,
            "time",
            SimpleNamespace(monotonic=lambda: state.now / 1000),
        )
        backend = WindowsBackend()
        backend._timeout = 1
        backend._ps = "inert-powershell"
        backend._ps_config = None

        def observation():
            return {
                "version": 1,
                "epoch": EPOCH,
                "now_ms": state.now,
                "last_input_ms": state.last,
                "foreground": state.foreground,
            }

        def custody():
            # During a release callback the ledger temporarily marks its entry
            # released to prevent recursion. Membership (not held_tokens) proves
            # the obligation still exists until the callback confirms success.
            return {
                name: (
                    TOKEN in ledger._held,
                    ledger._held[TOKEN].retain_on_failure
                    if TOKEN in ledger._held
                    else None,
                )
                for name, ledger in ledgers.items()
            }

        def bridge_run(argv, **kwargs):
            assert argv[0] == "inert-powershell"
            assert "-RequestFile" in argv
            payload = json.loads(Path(argv[-1]).read_text(encoding="utf-8"))
            op = payload["action"]
            state.now += 100  # outbound bridge/transport time on target ticks
            call = {
                "payload": deepcopy(payload),
                "context": CURRENT_TARGET_CLOCK.get(),
                "custody": custody(),
            }
            calls.append(call)
            if op == "presence_idle":
                result = {
                    "ok": True,
                    "idle_ms": state.now - state.last,
                    "observation": observation(),
                }
            elif op == "screen_info":
                result = {"ok": True, "width": 800, "height": 600, "screens": []}
            elif op == "focus_window":
                condition = payload["target_clock_condition"]
                assert condition["version"] == 3
                assert condition["operator_presence_target"] == state.foreground
                assert condition["focus_target"] == payload["handle"]
                call["source_binding"] = tool._coexistence_guard.binding.bound_target
                state.foreground = payload["handle"]
                emitted.append(op)
                before = state.now
                state.last = before
                result = {
                    "ok": True,
                    "focused": state.foreground,
                    "target_clock": {
                        "observation": observation(),
                        "injection": {
                            "epoch": EPOCH,
                            "before_ms": before,
                            "after_ms": before,
                            "count": 1,
                        },
                    },
                }
                mode = state.focus_result
                if mode in {"third", "zero", "source", "missing-foreground"}:
                    foreground = {
                        "third": "999",
                        "zero": "0",
                        "source": condition["operator_presence_target"],
                        "missing-foreground": None,
                    }[mode]
                    result["target_clock"]["observation"]["foreground"] = foreground
                elif mode == "missing-focused":
                    result.pop("focused")
                elif mode == "wrong-focused":
                    result["focused"] = "999"
                elif mode == "rejected":
                    result["ok"] = False  # desired focus alone is not success
                elif mode == "native-catch":
                    # Real PowerShell catch replaces, rather than mutates, out.
                    result = {
                        "ok": False,
                        "focus_stage": "alt_up",
                        "target_clock": result["target_clock"],
                    }
                elif mode == "missing":
                    result.pop("target_clock")
                elif mode == "missing-injection":
                    result["target_clock"]["injection"] = None
                elif mode == "count":
                    result["target_clock"]["injection"]["count"] = 0
                elif mode == "epoch":
                    result["target_clock"]["injection"]["epoch"] = "other-boot"
                elif mode == "tick":
                    result["target_clock"]["injection"]["after_ms"] = before + 1
                state.focus_hook()
                call["result"] = deepcopy(result)
                if mode == "lost":
                    raise subprocess.TimeoutExpired(argv, 1)
            else:
                assert op in {
                    "left_mouse_down",
                    "left_mouse_up",
                    "key",
                    "type",
                    "left_click",
                }, payload
                condition = payload.get("target_clock_condition")
                emergency = op == "left_mouse_up" and condition is None
                if emergency and state.fail_cleanup:
                    result = {"ok": False, "error": "inert safety-up unacknowledged"}
                elif op == "left_mouse_up" and state.reject_up and condition:
                    # Presence passed at the controller. External input arrives
                    # during request transport, before target-side emission.
                    assert samples[-1].state is presence.PresenceState.QUIET
                    assert state.last == condition["last_input_ms"]
                    state.reject_up = False
                    state.last = state.now
                    call["refused"] = True
                    result = {
                        "ok": False,
                        "error": "external input before emission",
                        "target_clock": {
                            "observation": observation(),
                            "injection": None,
                        },
                    }
                else:
                    before = state.now
                    state.last = before
                    emitted.append(op)
                    if state.write_foreground is not None:
                        state.foreground = state.write_foreground
                    if op == "left_mouse_down" and state.down_result == "human":
                        state.now += 50
                        state.last = state.now
                    result = {
                        "ok": True,
                        "target_clock": {
                            "observation": observation(),
                            "injection": {
                                "epoch": EPOCH,
                                "before_ms": before,
                                "after_ms": before,
                                "count": 1,
                            },
                        },
                    }
                    if op == "left_mouse_down":
                        if state.down_result == "missing":
                            result.pop("target_clock")
                        elif state.down_result == "corrupt":
                            result["target_clock"]["injection"]["count"] = 0
                        elif state.down_result == "unacknowledged":
                            # The target has emitted down but its checked
                            # bridge result refuses confirmation. This raises
                            # TargetClockFailure in the forwarding-only agent.
                            result["ok"] = False
                            result["error"] = "down emitted; bridge confirmation failed"
                call["result"] = deepcopy(result)
            return SimpleNamespace(stdout=json.dumps(result), stderr="", returncode=0)

        # Keep WindowsBackend.raw's payload construction and receipt parsing.
        # Only its OS transport and path translation are replaced.
        monkeypatch.setattr(
            windows,
            "subprocess",
            SimpleNamespace(
                run=bridge_run,
                DEVNULL=subprocess.DEVNULL,
                TimeoutExpired=subprocess.TimeoutExpired,
            ),
        )
        monkeypatch.setattr(windows, "_translate", lambda path, direction: path)
        agent = RemoteAgent(backend, read_only=False)
        ledgers["target"] = agent._ledger

        class Transport:
            user_host = "inert-target"
            port = None

            def send(self, line):
                req = json.loads(line)
                wires.append(deepcopy(req))
                # A separate target process does not inherit the controller's
                # ContextVar. _dispatch installs only the wire condition.
                token = CURRENT_TARGET_CLOCK.set(None)
                try:
                    response = agent._dispatch(
                        Request(id=req["id"], op=req["op"], args=req["args"])
                    )
                    if (
                        req["op"] == "focus_window"
                        and state.focus_result == "lost-remote"
                    ):
                        raise SshConnectError(
                            "inert applied focus; lost acknowledgement"
                        )
                    return response.encode()
                finally:
                    CURRENT_TARGET_CLOCK.reset(token)

        if remote:
            controller_backend = RemoteBackend(
                {"_host": "inert-target", "_transport": Transport()}
            )
            controller_backend._connected = True
            controller_backend.presence_platform = "windows-wsl2"
        else:
            controller_backend = backend
        tool = cu.ComputerTool(
            controller_backend, {"read_only": False, "gate_writes": False}
        )
        tool.resolve_display()
        # Disclosure is an OS effect outside these input-cleanup contracts.
        monkeypatch.setattr(tool, "_ensure_announced", lambda: None)
        if local_ledger:
            ledgers["controller"] = HeldInputLedger(
                on_release=lambda kind, token: releases.append((kind, token))
            )
            tool._ledger = ledgers["controller"]
        owner = "target" if agent_owned else "controller"

        def release_all(reason):
            ledger = ledgers.get(owner)
            confirmed = ledger.release_all(reason=reason) if ledger else []
            cleanup_runs.append((reason, list(confirmed)))
            return confirmed

        monitor = presence.PresenceMonitor(
            controller_backend.presence_idle_ms,
            platform="windows-wsl2",
            target_clock_source=controller_backend.presence_observation,
        )
        guard = CoexistenceGuard(
            monitor,
            release_all=release_all,
            on_presence_sample=samples.append,
            target_source=lambda: "inert-owned-window",
        )
        if operator_override:
            assert remote
            controller_backend.name = "remote-ssh:windows-wsl2"
            controller_backend.presence_platform = "windows-wsl2"
            monkeypatch.setattr(cu, "load_halt", lambda key: None)
            monkeypatch.setattr(cu, "_durable_halt_poll_for", lambda backend: None)
            monkeypatch.setattr(
                cu, "_get_channel_ledger", lambda key: ledgers["controller"]
            )
            guard = cu._build_coexistence_guard(
                controller_backend,
                {
                    "coexistence": {
                        "operator_presence_override": {
                            "channel": cu._halt_key(controller_backend),
                            "expires_at": time.time() + 600,
                            "reason": "synthetic offline policy",
                        }
                    },
                },
            )
            monitor = guard.presence
            guard.on_presence_sample = samples.append
        tool._coexistence_guard = guard
        monkeypatch.setattr(
            cu, "record_halt", lambda *args, **kwargs: halts.append((args, kwargs))
        )
        rig = SimpleNamespace(
            tool=tool,
            agent=agent,
            guard=guard,
            monitor=monitor,
            state=state,
            calls=calls,
            emitted=emitted,
            wires=wires,
            halts=halts,
            releases=releases,
            cleanup_runs=cleanup_runs,
            ledgers=ledgers,
            remote=remote,
        )
        rigs.append(rig)
        return rig

    yield make
    for rig in rigs:
        # Settle only our inert resources; never mask an assertion by sending
        # an OS input or leaving a live timer beyond the fixture lifetime.
        rig.state.fail_cleanup = False
        for ledger in rig.ledgers.values():
            ledger.stop()


def emergency_calls(rig):
    return [
        call
        for call in rig.calls
        if call["payload"]["action"] == "left_mouse_up"
        and "target_clock_condition" not in call["payload"]
    ]


def assert_emergency_is_no_motion_and_unconditioned(rig, owner):
    emergency = emergency_calls(rig)
    assert emergency, "possibly held input requires a separate safety mouse-up"
    for call in emergency:
        assert set(call["payload"]) == {"action", "_input_marker"}
        assert call["payload"]["action"] == "left_mouse_up"
        down = next(c for c in rig.calls if c["payload"]["action"] == "left_mouse_down")
        assert call["payload"]["_input_marker"] == down["payload"]["_input_marker"]
        assert call["context"] is None
    assert any(call["custody"][owner] == (True, True) for call in emergency), (
        "Windows cleanup must own a retain-on-failure ledger entry before release"
    )
    assert rig.emitted.count("left_mouse_down") == 1, "never replay down"
    assert CURRENT_TARGET_CLOCK.get() is None


def assert_halted_public_result(rig, result, expected):
    assert not result.success
    assert result.error["type"] == "HaltedError", result.error
    assert rig.guard.halted
    assert len(rig.halts) == 1
    snapshot = rig.halts[0][0][1]
    assert snapshot.state is expected
    with pytest.raises(HaltedError) as later:
        rig.guard.before_event()
    assert later.value.snapshot is snapshot, "cleanup must not replace the actual halt"
    return snapshot


@pytest.mark.parametrize("remote", [False, True], ids=["local", "remote"])
@pytest.mark.parametrize(
    "fail_cleanup", [False, True], ids=["up-confirmed", "up-failed"]
)
def test_public_rejected_explicit_up_keeps_halt_and_cleanup_custody(
    make_rig, capsys, remote, fail_cleanup
):
    rig = make_rig(remote=remote)
    assert run(rig.tool.execute(DOWN)).success
    assert rig.ledgers["controller"].held_tokens == [TOKEN]
    if remote:
        assert rig.agent._ledger.held_tokens == [TOKEN]
    rig.state.reject_up = True
    rig.state.fail_cleanup = fail_cleanup

    result = run(rig.tool.execute(UP))

    refused = [call for call in rig.calls if call.get("refused")]
    assert len(refused) == 1
    assert refused[0]["payload"]["coordinate"] == UP["coordinate"]
    assert refused[0]["custody"]["controller"] == (True, True)
    if remote:
        assert refused[0]["custody"]["target"] == (True, True)
    assert_emergency_is_no_motion_and_unconditioned(
        rig, "target" if remote else "controller"
    )
    assert_halted_public_result(rig, result, presence.PresenceState.HUMAN_ACTIVE)
    assert rig.emitted.count("left_mouse_down") == 1
    assert rig.emitted.count("left_mouse_up") == (
        0 if fail_cleanup else len(emergency_calls(rig))
    )
    if fail_cleanup:
        assert rig.ledgers["controller"].held_tokens == [TOKEN]
        assert rig.releases == []
        assert all(confirmed == [] for _, confirmed in rig.cleanup_runs)
        assert f"RELEASED:{TOKEN}" not in capsys.readouterr().err
        if remote:
            assert rig.agent._ledger.held_tokens == [TOKEN]
        for ledger in rig.ledgers.values():
            assert ledger.release_all(reason="inert-failed-cleanup-check") == []
        rig.state.fail_cleanup = False
        assert rig.ledgers["controller"].release_all(reason="inert-recovery") == [TOKEN]
    else:
        assert rig.ledgers["controller"].held_tokens == []
        assert rig.releases == [("mouse", TOKEN)]
    if remote:
        assert rig.agent._ledger.held_tokens == []
        assert sum(req["op"] == "mouse_down" for req in rig.wires) == 1


@pytest.mark.parametrize("down_result", ["human", "missing", "corrupt"])
@pytest.mark.parametrize(
    "fail_cleanup", [False, True], ids=["up-confirmed", "up-failed"]
)
def test_local_down_receipt_failure_releases_before_normal_registration(
    make_rig, down_result, fail_cleanup
):
    rig = make_rig()
    rig.state.down_result = down_result
    rig.state.fail_cleanup = fail_cleanup

    result = run(rig.tool.execute(DOWN))

    down = [
        call for call in rig.calls if call["payload"]["action"] == "left_mouse_down"
    ]
    assert len(down) == 1
    assert down[0]["custody"]["controller"] == (False, None)
    assert_emergency_is_no_motion_and_unconditioned(rig, "controller")
    expected = (
        presence.PresenceState.HUMAN_ACTIVE
        if down_result == "human"
        else presence.PresenceState.UNKNOWN
    )
    assert_halted_public_result(rig, result, expected)
    ledger = rig.ledgers["controller"]
    if fail_cleanup:
        assert ledger.held_tokens == [TOKEN]
        assert rig.releases == []
        assert ledger.release_all(reason="inert-failed-cleanup-check") == []
        assert rig.releases == []
        rig.state.fail_cleanup = False
        assert ledger.release_all(reason="inert-recovery") == [TOKEN]
    assert ledger.held_tokens == []
    assert rig.releases == [("mouse", TOKEN)]
    assert rig.tool._mouse_pending == {}
    assert rig.emitted.count("left_mouse_down") == 1


@pytest.mark.parametrize("down_result", ["human", "missing", "corrupt"])
@pytest.mark.parametrize(
    "fail_cleanup", [False, True], ids=["up-confirmed", "up-failed"]
)
def test_remote_agent_down_handler_retains_possible_hold_on_receipt_halt(
    make_rig, capsys, down_result, fail_cleanup
):
    # Handler-level discriminator: the forwarding-only dispatcher does not
    # classify HUMAN/UNKNOWN. Use the real guard exchange directly around the
    # real target handler to exercise its exception-before-register branch.
    rig = make_rig(agent_owned=True)
    rig.state.down_result = down_result
    rig.state.fail_cleanup = fail_cleanup
    rig.guard.before_event()
    with pytest.raises(HaltedError) as error, rig.guard.target_clock_event():
        rig.agent._op_mouse_down({"x": 10, "y": 20, "button": "left"})

    expected = (
        presence.PresenceState.HUMAN_ACTIVE
        if down_result == "human"
        else presence.PresenceState.UNKNOWN
    )
    assert error.value.snapshot.state is expected
    assert rig.guard.halted
    assert_emergency_is_no_motion_and_unconditioned(rig, "target")
    ledger = rig.agent._ledger
    if fail_cleanup:
        assert ledger.held_tokens == [TOKEN]
        assert ledger.release_all(reason="inert-failed-cleanup-check") == []
        assert f"RELEASED:{TOKEN}" not in capsys.readouterr().err
        rig.state.fail_cleanup = False
        assert ledger.release_all(reason="inert-recovery") == [TOKEN]
    assert ledger.held_tokens == []
    assert rig.agent._mouse_pending == {}
    assert capsys.readouterr().err.count(f"RELEASED:{TOKEN}") == 1
    assert rig.emitted.count("left_mouse_down") == 1


@pytest.mark.parametrize(
    "fail_cleanup", [False, True], ids=["up-confirmed", "up-failed"]
)
def test_remote_dispatch_uncertain_down_has_target_owned_cleanup_without_replay(
    make_rig, capsys, fail_cleanup
):
    rig = make_rig(remote=True)
    rig.state.down_result = "unacknowledged"
    rig.state.fail_cleanup = fail_cleanup

    result = run(rig.tool.execute(DOWN))

    assert_halted_public_result(rig, result, presence.PresenceState.UNKNOWN)
    assert_emergency_is_no_motion_and_unconditioned(rig, "target")
    assert sum(req["op"] == "mouse_down" for req in rig.wires) == 1
    ledger = rig.agent._ledger
    if fail_cleanup:
        assert ledger.held_tokens == [TOKEN]
        assert rig.tool._backend._call("release_all") == {"released": []}
        assert ledger.held_tokens == [TOKEN]
        assert f"RELEASED:{TOKEN}" not in capsys.readouterr().err
        rig.state.fail_cleanup = False
        assert rig.tool._backend._call("release_all") == {"released": [TOKEN]}
    assert ledger.held_tokens == []
    assert rig.agent._mouse_pending == {}
    assert capsys.readouterr().err.count(f"RELEASED:{TOKEN}") == 1
    assert rig.emitted.count("left_mouse_down") == 1


@pytest.mark.parametrize("down_result", ["human", "missing", "corrupt"])
def test_local_uncertain_down_still_sends_safety_up_without_ledger(
    make_rig, down_result
):
    rig = make_rig(local_ledger=False)
    rig.state.down_result = down_result

    result = run(rig.tool.execute(DOWN))

    assert not result.success and result.error["type"] == "HaltedError"
    assert rig.guard.halted
    emergency = emergency_calls(rig)
    assert len(emergency) == 1
    assert set(emergency[0]["payload"]) == {"action", "_input_marker"}
    assert emergency[0]["payload"]["action"] == "left_mouse_up"
    assert emergency[0]["context"] is None
    assert rig.emitted == ["left_mouse_down", "left_mouse_up"]
    assert CURRENT_TARGET_CLOCK.get() is None


def test_remote_operator_condition_reaches_actual_bridge_payload_and_retains_cleanup(
    make_rig,
):
    rig = make_rig(remote=True, operator_override=True)
    rig.state.down_result = "human"
    assert run(rig.tool.execute(DOWN)).success
    assert rig.monitor.last_snapshot.state is presence.PresenceState.HUMAN_ACTIVE
    assert not rig.guard.halted
    assert rig.ledgers["controller"].held_tokens == [TOKEN]
    assert rig.agent._ledger.held_tokens == [TOKEN]
    assert run(rig.tool.execute(UP)).success
    writes = [
        call
        for call in rig.calls
        if call["payload"]["action"].startswith("left_mouse_")
    ]
    assert len(writes) == 2
    for call in writes:
        condition = call["payload"]["target_clock_condition"]
        assert condition["version"] == 2
        assert condition["operator_presence_override"] is True
        assert condition["operator_presence_target"] == "inert-owned-window"
        assert (
            condition["operator_presence_channel"]
            == "remote-ssh:windows-wsl2:inert-target"
        )
        assert condition["operator_presence_expires_at"] > time.time()
    forwarded = [
        req["args"]["_target_clock_condition"]
        for req in rig.wires
        if req["op"] in {"mouse_down", "mouse_up"}
    ]
    assert forwarded == [call["payload"]["target_clock_condition"] for call in writes]
    assert not emergency_calls(rig) and not rig.guard.halted
    assert rig.ledgers["controller"].held_tokens == rig.agent._ledger.held_tokens == []


@pytest.mark.parametrize("fail_cleanup", [False, True])
def test_remote_operator_policy_still_halts_unknown_and_keeps_failed_cleanup(
    make_rig, fail_cleanup
):
    rig = make_rig(remote=True, operator_override=True)
    rig.state.down_result = "unacknowledged"
    rig.state.fail_cleanup = fail_cleanup
    result = run(rig.tool.execute(DOWN))
    assert_halted_public_result(rig, result, presence.PresenceState.UNKNOWN)
    assert_emergency_is_no_motion_and_unconditioned(rig, "target")
    ledger = rig.agent._ledger
    if fail_cleanup:
        assert ledger.held_tokens == [TOKEN]
        rig.state.fail_cleanup = False
        assert rig.tool._backend._call("release_all") == {"released": [TOKEN]}
    assert ledger.held_tokens == []


def test_operator_policy_does_not_authorize_read_only_input(make_rig):
    rig = make_rig(remote=True, operator_override=True)
    rig.tool._read_only = True
    result = run(rig.tool.execute(DOWN))
    assert not result.success
    assert "read_only" in result.error["message"]
    assert rig.emitted == []


def test_operator_policy_does_not_suppress_public_cancellation_or_schedule_next_input(
    make_rig,
):
    rig = make_rig(remote=True, operator_override=True)
    entered, release, settled = (threading.Event() for _ in range(3))
    original = rig.tool._backend._call

    def blocking(op, **args):
        value = original(op, **args)
        if op == "mouse_down":
            entered.set()
            assert release.wait(3)
        return value

    rig.tool._backend._call = blocking
    rig.tool._band_enter = lambda: "inert-band"
    rig.tool._band_exit = lambda token: settled.set()

    async def scenario():
        task = asyncio.create_task(
            rig.tool.execute(
                {
                    "version": 1,
                    "actions": [DOWN, UP],
                    "capture_after": False,
                }
            )
        )
        assert await asyncio.to_thread(entered.wait, 1)
        try:
            task.cancel()
            result = await task
            assert not result.success and result.error["type"] == "CancelledError"
            assert not settled.is_set()
        finally:
            release.set()
        assert await asyncio.to_thread(settled.wait, 1)

    run(scenario())
    ordinary = [
        call
        for call in rig.calls
        if call["payload"]["action"].startswith("left_mouse_")
        and "target_clock_condition" in call["payload"]
    ]
    assert [call["payload"]["action"] for call in ordinary] == ["left_mouse_down"]
    # Test cleanup owns the possibly completed hold; no normal UP was replayed.
    rig.tool._ledger.release_all(reason="inert-test-cleanup")


@pytest.mark.parametrize("already_focused", [False, True])
def test_public_explicit_focus_transfers_only_the_exact_approved_destination(
    make_rig, already_focused
):
    rig = make_rig(remote=True, operator_override=True)
    destination = "7209940"
    source = destination if already_focused else rig.state.foreground
    rig.state.foreground = source
    transfers = []
    original_bind = rig.guard.binding.bind

    def bind(target):
        transfers.append(target)
        original_bind(target)

    rig.guard.binding.bind = bind
    finished_conditions = []
    original_accept = rig.guard.accept_target_result

    def accepted(result, **kwargs):
        assert rig.guard.binding.bound_target == source
        original_accept(result, **kwargs)
        finished_conditions.append(rig.guard.target_condition())

    rig.guard.accept_target_result = accepted
    result = run(rig.tool.execute({"action": "focus_window", "handle": destination}))
    assert result.success, result.error
    assert transfers == [source, destination]
    assert finished_conditions[0]["operator_presence_target"] == destination
    assert finished_conditions[0]["version"] == 2
    assert "focus_target" not in finished_conditions[0]
    call = next(c for c in rig.calls if c["payload"]["action"] == "focus_window")
    assert call["source_binding"] == source
    wire = next(r for r in rig.wires if r["op"] == "focus_window")
    assert (
        wire["args"]["_target_clock_condition"]
        == call["payload"]["target_clock_condition"]
    )
    assert rig.guard.binding.status == "not_bound"
    assert CURRENT_TARGET_CLOCK.get() is None
    # Next ordinary call uses only v2 and the new fresh foreground; no focus grant.
    rig.guard.accept_target_result = original_accept
    assert run(rig.tool.execute(DOWN)).success
    condition = rig.calls[-1]["payload"]["target_clock_condition"]
    assert condition["version"] == 2 and "focus_target" not in condition
    assert condition["operator_presence_target"] == destination


@pytest.mark.parametrize(
    "failure",
    [
        "third",
        "zero",
        "source",
        "missing-foreground",
        "missing-focused",
        "wrong-focused",
        "rejected",
        "missing",
        "missing-injection",
        "count",
        "epoch",
        "tick",
        "lost",
        "lost-remote",
    ],
)
def test_public_focus_failure_never_transfers_or_replays(make_rig, failure):
    rig = make_rig(remote=True, operator_override=True)
    source = rig.state.foreground
    transfers = []
    original_bind = rig.guard.binding.bind

    def bind(target):
        transfers.append(target)
        original_bind(target)

    rig.guard.binding.bind = bind
    rig.state.focus_result = failure
    result = run(rig.tool.execute({"action": "focus_window", "handle": "7209940"}))
    assert_halted_public_result(rig, result, presence.PresenceState.UNKNOWN)
    assert transfers == [source]
    assert rig.guard.binding.status == "not_bound"
    assert rig.emitted == ["focus_window"]
    assert CURRENT_TARGET_CLOCK.get() is None


@pytest.mark.parametrize(
    "control", ["stop", "durable", "pause", "expiry", "revocation"]
)
def test_focus_receipt_cannot_transfer_after_independent_control(make_rig, control):
    rig = make_rig(remote=True, operator_override=True)
    transfers = []
    bind = rig.guard.binding.bind
    rig.guard.binding.bind = lambda target: (transfers.append(target), bind(target))[-1]
    policy = rig.guard.operator_presence_override
    controlled = {"wall": time.time(), "config": policy.config()}
    rig.guard.operator_presence_override = replace(
        policy,
        wall_clock=lambda: controlled["wall"],
        config_source=lambda: controlled["config"],
    )

    def interrupt():
        if control == "stop":
            rig.guard.seed_halted(rig.monitor.last_snapshot)
        elif control == "durable":
            rig.guard.durable_halt_poll = lambda: rig.monitor.last_snapshot
        elif control == "pause":
            rig.guard.pause.set("inert-focus-pause")
        elif control == "expiry":
            controlled["wall"] = policy.expires_at
        else:
            controlled["config"] = None

    rig.state.focus_hook = interrupt
    result = run(rig.tool.execute({"action": "focus_window", "handle": "7209940"}))
    assert not result.success, result.output
    assert transfers == ["inert-owned-window"]
    assert rig.emitted == ["focus_window"]
    assert CURRENT_TARGET_CLOCK.get() is None


def test_public_focus_uses_approved_copy_not_mutable_request_or_approval_params(
    make_rig,
):
    rig = make_rig(remote=True, operator_override=True)
    rig.tool._binding = replace(rig.tool._binding, gate_writes=True)
    request = {"action": "focus_window", "handle": "7209940"}
    approvals = []

    async def approve(params, binding):
        approvals.append(deepcopy(params))
        params["handle"] = request["handle"] = "999"
        return True

    rig.tool._write_approval = approve
    result = run(rig.tool.execute(request))
    assert result.success, result.error
    assert approvals == [{"action": "focus_window", "handle": "7209940"}]
    focus = next(
        c["payload"] for c in rig.calls if c["payload"]["action"] == "focus_window"
    )
    assert (
        focus["handle"] == focus["target_clock_condition"]["focus_target"] == "7209940"
    )


@pytest.mark.parametrize("action", ["type", "key", "left_click"])
def test_public_ordinary_writes_cannot_select_focus_authority(make_rig, action):
    rig = make_rig(remote=True, operator_override=True)
    rig.state.write_foreground = "7209940"
    result = run(
        rig.tool.execute(
            {
                "action": action,
                "text": "a",
                "coordinate": [10, 20],
                "focus_target": "7209940",  # ignored flat extra, never authority
            }
        )
    )
    assert_halted_public_result(rig, result, presence.PresenceState.UNKNOWN)
    assert rig.guard._halt_snapshot.basis == "operator_presence_target_changed"
    for call in rig.calls:
        condition = call["payload"].get("target_clock_condition")
        if condition:
            assert condition["version"] == 2 and "focus_target" not in condition


@pytest.mark.parametrize(
    "action,params", [("key", {"text": "alt"}), ("focus_window", {"handle": "999"})]
)
def test_windows_forwarding_refuses_focus_scope_on_another_call(
    make_rig, action, params
):
    rig = make_rig(remote=True, operator_override=True)
    rig.guard.bind_target()
    rig.guard.before_event()
    condition = {
        **rig.guard.target_condition(),
        "version": 3,
        "focus_target": "7209940",
    }
    before = len(rig.calls)
    with target_clock_exchange(condition), pytest.raises(TargetClockFailure):
        rig.agent.backend.raw(action, **params)
    assert len(rig.calls) == before  # refused before any fake/native bridge
    assert CURRENT_TARGET_CLOCK.get() is None


@pytest.mark.parametrize("fail_cleanup", [False, True])
def test_focus_lost_ack_retains_failed_existing_cleanup_and_never_replays(
    make_rig, fail_cleanup
):
    rig = make_rig(remote=True, operator_override=True)
    assert run(rig.tool.execute(DOWN)).success
    rig.state.focus_result = "lost"
    rig.state.fail_cleanup = fail_cleanup
    result = run(rig.tool.execute({"action": "focus_window", "handle": "7209940"}))
    assert_halted_public_result(rig, result, presence.PresenceState.UNKNOWN)
    assert rig.emitted.count("focus_window") == 1
    assert emergency_calls(rig)
    for call in emergency_calls(rig):
        assert call["context"] is None
    assert rig.ledgers["controller"].held_tokens == ([TOKEN] if fail_cleanup else [])
    assert rig.agent._ledger.held_tokens == ([TOKEN] if fail_cleanup else [])


def test_public_cancel_during_focus_never_transfers_or_schedules_next_action(make_rig):
    rig = make_rig(remote=True, operator_override=True)
    entered, release, settled = (threading.Event() for _ in range(3))
    transfers = []
    bind = rig.guard.binding.bind
    rig.guard.binding.bind = lambda target: (transfers.append(target), bind(target))[-1]

    def in_flight():
        entered.set()
        assert release.wait(3)

    rig.state.focus_hook = in_flight
    rig.tool._band_enter = lambda: "inert-focus-band"
    rig.tool._band_exit = lambda token: settled.set()

    async def scenario():
        task = asyncio.create_task(
            rig.tool.execute(
                {
                    "version": 1,
                    "actions": [{"action": "focus_window", "handle": "7209940"}, DOWN],
                    "capture_after": False,
                }
            )
        )
        assert await asyncio.to_thread(entered.wait, 1)
        try:
            task.cancel()
            result = await task
            assert not result.success and result.error["type"] == "CancelledError"
            assert not settled.is_set()
        finally:
            release.set()
        assert await asyncio.to_thread(settled.wait, 1)

    run(scenario())
    assert rig.emitted == ["focus_window"]
    assert transfers == ["inert-owned-window"]
    assert rig.guard.halted and len(rig.halts) == 1
    assert CURRENT_TARGET_CLOCK.get() is None


def test_desktop_focus_halt_uses_shared_durable_recording(make_rig):
    rig = make_rig(remote=True, operator_override=True)
    rig.state.focus_result = "third"
    desktop = cu.DesktopTool(rig.tool)
    result = run(desktop.execute({"action": "focus_window", "handle": "7209940"}))
    assert not result.success and result.error["type"] == "HaltedError"
    assert rig.guard.halted
    assert len(rig.halts) == 1
    assert len(rig.tool.halt_notices) == 1
    assert rig.tool.halt_notices[0]["action"] == "focus_window"
    assert rig.guard.binding.status == "not_bound"
    assert CURRENT_TARGET_CLOCK.get() is None


def test_native_focus_catch_shape_retains_bounded_discriminator_and_halts(make_rig):
    rig = make_rig(remote=True, operator_override=True)
    rig.state.focus_result = "native-catch"
    result = run(
        cu.DesktopTool(rig.tool).execute(
            {"action": "focus_window", "handle": "7209940"}
        )
    )
    assert not result.success and rig.guard.halted
    assert len(rig.halts) == 1
    assert rig.guard.binding.status == "not_bound"
    assert rig.emitted == ["focus_window"]
    assert rig.guard.as_dict()["focus_transition_evidence"] == {
        "bridge_ok": False,
        "focused_present": False,
        "focused_is_string": False,
        "focused_matches": False,
        "observed_present": True,
        "observed_matches": True,
        "bridge_stage": "alt_up",
    }
    assert CURRENT_TARGET_CLOCK.get() is None
