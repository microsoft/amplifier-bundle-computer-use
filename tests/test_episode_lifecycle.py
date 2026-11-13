"""Production lifecycle functions against inert backends, not native qualification."""

from __future__ import annotations

import asyncio
import io
import json
import threading
import time
import uuid
from functools import wraps

import amplifier_module_tool_computer_use as cu
import pytest
from amplifier_core.testing import MockCoordinator
from amplifier_module_tool_computer_use.episode import Episode, EpisodeError
from amplifier_module_tool_computer_use.ledger import HeldInputLedger
from amplifier_module_tool_computer_use.remote_agent import RemoteAgent
from amplifier_module_tool_computer_use.remote_backend import RemoteBackend
from amplifier_module_tool_computer_use.ssh_transport import SshTransport
from amplifier_module_tool_computer_use.wire import Request
from test_neutral_computer import Backend, batch
from test_remote_agent import _FakeBackend


class Indicator:
    def __init__(self):
        self.states = []
        self.healthy = True
        self.closed = False

    def start(self, state="viewing"):
        return self.update(state)

    def update(self, state):
        self.states.append(state)
        return self.status()

    def status(self):
        return {"available": True, "ready": True, "healthy": self.healthy}

    def close(self):
        self.closed = True
        return {
            "healthy": False,
            "closed": True,
            "surface_absent": True,
            "process_settled": True,
            "helper_exit": 0 if self.states[-1] == "released" else 2,
        }


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not observed"
        time.sleep(0.005)


def run_async(function):
    # A private loop leaves legacy sync tests' current loop untouched.
    @wraps(function)
    def run(*args, **kwargs):
        with asyncio.Runner(loop_factory=asyncio.new_event_loop) as runner:
            return runner.run(function(*args, **kwargs))

    return run


@pytest.fixture
def rig(monkeypatch, tmp_path):
    monkeypatch.setattr(cu, "SHOT_DIR", tmp_path)
    indicator = Indicator()
    tool = cu.ComputerTool(Backend(), {"_indicator_factory": lambda: indicator})
    tool._episode_enabled = True
    tool._announced = True
    tool.resolve_display()
    yield tool, indicator
    if tool._episode_identity is not None:
        tool.end_episode(interrupted=True)
    with cu._episode_registry_lock:
        cu._episode_registry.clear()


@run_async
async def test_capture_only_reasoning_gaps_batch_desktop_and_explicit_release(rig):
    tool, indicator = rig
    assert (await tool.execute({"action": "screenshot"})).success
    generation = tool.episode_status()["identity"]["generation"]
    assert indicator.states == ["viewing"]
    await asyncio.sleep(0.02)  # queue is empty, sponsor is still active
    assert tool.episode_status()["state"] == "viewing"
    assert not indicator.closed
    assert (
        await tool.execute(batch({"action": "key", "text": "A"}, capture=False))
    ).success
    assert "controlling" in indicator.states
    tool._backend.get_clipboard = lambda: "inert"
    assert (await cu.DesktopTool(tool).execute({"action": "get_clipboard"})).success
    result = await cu.DesktopTool(tool).execute(
        {"action": "end_episode", "generation": generation}
    )
    assert result.output["state"] == "released"
    assert result.output["release_confirmed"]
    assert indicator.closed and indicator.states[-1] == "released"
    assert not (await tool.execute({"action": "key", "text": "A"})).success


@pytest.mark.parametrize("action", ["get_clipboard", "set_clipboard", "focus_window"])
@run_async
async def test_cancelled_desktop_worker_not_released_and_queued_call_refused(
    rig, action
):
    tool, indicator = rig
    entered, proceed, settled = threading.Event(), threading.Event(), threading.Event()

    def blocked(*args):
        entered.set()
        assert proceed.wait(5)
        settled.set()
        return "inert"

    setattr(tool._backend, action, blocked)
    desktop = cu.DesktopTool(tool)
    task = asyncio.create_task(
        desktop.execute({"action": action, "text": "x", "handle": "1"})
    )
    assert await asyncio.to_thread(entered.wait, 2)
    generation = tool.episode_status()["identity"]["generation"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    controller = cu.EpisodeController(tool)
    status = controller.fence(generation)
    assert status["workers"] == 1 and not status["release_confirmed"]
    assert "released" not in indicator.states
    queued = await tool.execute({"action": "key", "text": "A"})
    assert not queued.success
    proceed.set()
    assert await asyncio.to_thread(settled.wait, 2)
    result = await controller.end(generation, interrupted=True)
    assert result["workers"] == 0 and result["state"] == "interrupted"
    assert "released" not in indicator.states


def test_failed_release_keeps_custody_and_never_issues_success_notification(rig):
    tool, indicator = rig
    notifications = []
    ledger = HeldInputLedger(
        deadman_seconds=100, on_release=lambda *args: notifications.append(args)
    )
    tool._ledger = ledger
    tool.begin_episode()

    def fail():
        raise RuntimeError("inert release failure")

    # Even legacy callers explicitly passing false must retain failed custody.
    ledger.hold("key", "owned", fail, retain_on_failure=False)
    assert ledger.release_all() == []
    assert ledger.held_tokens == ["owned"] and notifications == []
    first = tool.end_episode()
    second = tool.end_episode()
    assert first["state"] == second["state"] == "interrupted"
    assert not first["release_confirmed"] and not second["release_confirmed"]
    assert "released" not in indicator.states
    ledger.stop()


@run_async
async def test_second_owner_and_stale_scope_cannot_touch_new_generation(rig):
    tool, _ = rig
    controller = cu.EpisodeController(tool)
    first = await controller.begin()
    old = first["identity"]["generation"]
    sibling = cu.ComputerTool(tool._backend, {"_indicator_factory": Indicator})
    sibling._episode_enabled = True
    sibling._announced = True
    with pytest.raises(cu.BackendError, match="another controller"):
        sibling.begin_episode()
    with pytest.raises(cu.BackendError, match="stale"):
        tool.end_episode("not-the-owner")
    assert (await controller.end(old))["state"] == "released"
    second = await controller.begin()
    assert second["identity"]["generation"] != old
    with pytest.raises(cu.BackendError, match="stale"):
        controller.fence(old)
    # Existing descendant context cannot silently attach to a new sponsor.
    token = cu._episode_scope.set((tool, old))
    try:
        result = await tool.execute({"action": "key", "text": "A"})
        assert not result.success and "stale" in result.error["message"]
    finally:
        cu._episode_scope.reset(token)


@pytest.mark.parametrize("loss", ["expiry", "helper"])
def test_watchdog_fences_without_held_input_or_next_call(loss):
    indicator = Indicator()
    now = [1.0]
    episode = Episode(
        "target",
        lambda: indicator,
        lambda: True,
        clock=lambda: now[0],
        lease_seconds=0.1,
    )
    identity = {"owner": "owner", "target": "target", "generation": "generation"}
    episode.begin(identity)
    if loss == "expiry":
        now[0] = 2.0
    else:
        indicator.healthy = False
    wait_for(lambda: episode.status()["state"] == "interrupted")
    with pytest.raises(EpisodeError):
        episode.heartbeat(identity)
    with pytest.raises(EpisodeError), episode.admit(identity):
        pytest.fail("fenced episode admitted")
    assert "released" not in indicator.states


def test_remote_revocation_not_queued_behind_native_dispatch_and_no_replay():
    backend, indicator = _FakeBackend(), Indicator()
    agent = RemoteAgent(
        backend,
        read_only=False,
        require_episode=True,
        indicator_factory=lambda: indicator,
    )
    identity = {
        "owner": "owner",
        "target": agent._episode_target,
        "generation": uuid.uuid4().hex,
    }
    assert agent._dispatch(Request(1, "episode_begin", {"identity": identity})).ok
    entered, proceed = threading.Event(), threading.Event()
    calls = []

    def blocked(combo):
        entered.set()
        assert proceed.wait(3)
        calls.append(combo)

    backend.key = blocked
    worker = threading.Thread(
        target=agent._dispatch,
        args=(Request(2, "key", {"combo": "A", "_episode": identity}),),
    )
    worker.start()
    assert entered.wait(2)
    # Exactly the reader-side production notice path, while dispatch is blocked.
    assert agent._episode_notice(Request(3, "episode_revoke", {"identity": identity}))
    assert not agent._dispatch(
        Request(4, "key", {"combo": "B", "_episode": identity})
    ).ok
    assert agent._episode.status()["workers"] == 1
    assert "released" not in indicator.states
    proceed.set()
    worker.join(2)
    end = agent._dispatch(Request(5, "episode_end", {"identity": identity}))
    assert end.ok and end.result["state"] == "released"
    assert calls == ["A"]  # no input retry after an unknown/lost reply


def test_remote_notify_uses_write_lock_not_response_lock():
    transport = SshTransport.__new__(SshTransport)
    transport._lock = threading.Lock()
    transport._write_lock = threading.Lock()

    class Process:
        stdin = io.BytesIO()

    transport._proc = Process()
    with transport._lock:
        transport.notify(Request(1, "episode_revoke", {"identity": {}}).encode())
    assert json.loads(transport._proc.stdin.getvalue())["op"] == "episode_revoke"


@run_async
async def test_production_mount_exports_trusted_controller_and_cleanup(
    rig, monkeypatch
):
    tool, _ = rig
    monkeypatch.setattr(cu, "_select_and_build", lambda cfg: tool)
    coordinator = MockCoordinator()
    mounted = await cu.mount(coordinator)
    controller = coordinator.get_capability("computer_use.episodes")
    status = await controller.begin()
    generation = status["identity"]["generation"]
    with controller.bind(generation):
        assert (await tool.execute({"action": "screenshot"})).success
    await mounted()
    assert not tool.episode_status()["admission_open"]
    assert coordinator.get_capability("computer_use.episodes") is None


def test_lost_begin_reply_reconciles_status_without_replaying_begin():
    backend = RemoteBackend()
    identity = {"owner": "owner", "target": "target", "generation": "generation"}
    calls = []

    def call(op, **kwargs):
        calls.append(op)
        if op == "episode_begin":
            raise cu.BackendError("lost reply")
        return {"identity": identity, "state": "viewing", "workers": 0}

    backend._call = call
    with pytest.raises(cu.BackendError):
        backend.begin_episode(identity)
    assert backend.episode_status(identity)["identity"] == identity
    assert calls == ["episode_begin", "episode_status"]
