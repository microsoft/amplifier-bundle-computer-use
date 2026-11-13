"""Production lifecycle functions against inert backends, not native qualification."""

from __future__ import annotations

import asyncio
import io
import json
import queue
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


@pytest.mark.parametrize("loss", ["expiry", "helper"])
def test_end_and_cached_status_check_deadline_before_watchdog_tick(loss):
    indicator = Indicator()
    clock = [0.0]
    episode = Episode("t", lambda: indicator, lambda: True, clock=lambda: clock[0])
    identity = {"owner": "o", "target": "t", "generation": "g"}
    episode.begin(identity)
    if loss == "expiry":
        clock[0] = 10.01
    else:
        indicator.healthy = False
    result = episode.end(identity)
    assert result["state"] == "interrupted"
    assert "released" not in indicator.states
    assert not episode.status()["admission_open"]


def test_indicator_upgrade_exception_is_sticky():
    indicator = Indicator()
    update = indicator.update

    def fail(state):
        if state == "controlling":
            raise RuntimeError("unknown indicator acknowledgement")
        return update(state)

    indicator.update = fail
    episode = Episode("t", lambda: indicator, lambda: True)
    identity = {"owner": "o", "target": "t", "generation": "g"}
    episode.begin(identity)
    with pytest.raises(EpisodeError), episode.admit(identity, controlling=True):
        pytest.fail("admitted without indicator")
    indicator.update = update
    with pytest.raises(EpisodeError):
        episode.heartbeat(identity)
    assert episode.end(identity)["state"] == "interrupted"


@pytest.mark.parametrize("surface", ["computer", "desktop"])
@run_async
async def test_call_queued_at_band_cannot_attach_replacement_generation(
    rig, monkeypatch, surface
):
    tool, _ = rig
    entered, proceed = asyncio.Event(), asyncio.Event()
    acquire = tool._acquire_batch_band

    async def gate():
        entered.set()
        await proceed.wait()
        return await acquire()

    monkeypatch.setattr(tool, "_acquire_batch_band", gate)
    tool._backend.get_clipboard = lambda: "inert"
    target = tool if surface == "computer" else cu.DesktopTool(tool)
    payload = (
        {"action": "key", "text": "A"}
        if surface == "computer"
        else {"action": "get_clipboard"}
    )
    queued = asyncio.create_task(target.execute(payload))
    await asyncio.wait_for(entered.wait(), 2)
    old = tool.episode_status()["identity"]["generation"]
    assert (await asyncio.to_thread(tool.end_episode, old))["state"] == "released"
    tool.begin_episode(explicit=True)
    proceed.set()
    assert not (await queued).success
    assert tool._backend.calls == []


def test_reader_revoke_overtaking_queued_begin_remains_fenced():
    indicator = Indicator()
    agent = RemoteAgent(
        _FakeBackend(), require_episode=True, indicator_factory=lambda: indicator
    )
    identity = {"owner": "o", "target": agent._episode_target, "generation": "g"}
    lines = queue.Queue()
    entered, proceed, revoked = threading.Event(), threading.Event(), threading.Event()
    dispatch, notice = agent._dispatch, agent._episode_notice

    def gated(req):
        if req.op == "episode_begin":
            entered.set()
            assert proceed.wait(3)
        return dispatch(req)

    def observed(req):
        result = notice(req)
        if req.op == "episode_revoke":
            revoked.set()
        return result

    agent._dispatch, agent._episode_notice = gated, observed

    class Input:
        def __iter__(self):
            while (line := lines.get()) is not None:
                yield line

    stdout = io.StringIO()
    worker = threading.Thread(target=agent.run, args=(Input(), stdout))
    worker.start()
    lines.put(Request(1, "episode_begin", {"identity": identity}).encode())
    assert entered.wait(2)
    lines.put(Request(2, "episode_revoke", {"identity": identity}).encode())
    assert revoked.wait(2)
    lines.put(Request(3, "key", {"combo": "A", "_episode": identity}).encode())
    proceed.set()
    lines.put(Request(4, "bye").encode())
    lines.put(None)
    worker.join(3)
    assert not worker.is_alive()
    replies = {
        row["id"]: row for row in map(json.loads, stdout.getvalue().splitlines())
    }
    assert replies[1]["ok"] is False and replies[3]["ok"] is False
    assert 2 not in replies  # one-way notice never disturbs response framing


def remote_rig():
    target = _FakeBackend()
    agent = RemoteAgent(
        target, read_only=False, require_episode=True, indicator_factory=Indicator
    )

    class Transport:
        user_host = "inert"
        port = None

        def send(self, line):
            req = json.loads(line)
            return agent._dispatch(Request(**req)).encode()

        def notify(self, line):
            agent._episode_notice(Request(**json.loads(line)))

    backend = RemoteBackend({"_transport": Transport()})
    backend._connected = True
    backend.handshake = {"episode_target": agent._episode_target}
    tool = cu.ComputerTool(backend, {"read_only": False, "gate_writes": False})
    tool._episode_enabled = True
    tool._announced = True
    tool.resolve_display()
    tool._ledger = HeldInputLedger(deadman_seconds=100)
    return tool, agent, target


@run_async
async def test_remote_target_release_reconciles_controller_without_replaying_up():
    tool, agent, target = remote_rig()
    assert (
        await tool.execute({"action": "left_mouse_down", "coordinate": [1, 1]})
    ).success
    assert tool._mouse_pending
    result = await asyncio.to_thread(tool.end_episode)
    assert result["state"] == "released"
    assert not tool._mouse_pending and tool._ledger.held_tokens == []
    assert not agent._ledger.held_tokens and len(target.mouse_ups) == 1
    tool._ledger.release_all()
    assert len(target.mouse_ups) == 1
    tool._ledger.stop()


def test_public_end_fences_before_waiting_sponsor_status_lock():
    tool, agent, _ = remote_rig()
    tool.begin_episode()
    entered, proceed, ending = threading.Event(), threading.Event(), threading.Event()
    status = tool._episode.episode_status

    def blocked(identity):
        entered.set()
        assert proceed.wait(3)
        return status(identity)

    tool._episode.episode_status = blocked
    begin_worker = threading.Thread(target=lambda: _ignore_error(tool.begin_episode))
    begin_worker.start()
    assert entered.wait(2)

    def end():
        ending.set()
        tool.end_episode()

    end_worker = threading.Thread(target=end)
    end_worker.start()
    assert ending.wait(1)
    wait_for(lambda: tool._episode._episode_fenced)
    wait_for(lambda: not agent._episode.status()["admission_open"])
    proceed.set()
    begin_worker.join(3)
    end_worker.join(3)
    assert not end_worker.is_alive()
    tool._ledger.stop()


def _ignore_error(function):
    try:
        function()
    except Exception:
        pass


@run_async
async def test_cleanup_owns_pending_start_and_no_active_publish_after_close(
    rig, monkeypatch
):
    tool, indicator = rig
    entered, proceed = threading.Event(), threading.Event()
    start = indicator.start

    def blocked(state="viewing"):
        entered.set()
        assert proceed.wait(3)
        return start(state)

    indicator.start = blocked
    backend_closes = []
    tool._backend.close = lambda: backend_closes.append(True)
    monkeypatch.setattr(cu, "_select_and_build", lambda cfg: tool)
    coordinator = MockCoordinator()
    mounted = await cu.mount(coordinator)
    controller = coordinator.get_capability("computer_use.episodes")
    beginning = asyncio.create_task(controller.begin())
    assert await asyncio.to_thread(entered.wait, 2)
    cleaning = asyncio.create_task(mounted())
    await asyncio.sleep(0.02)
    assert not cleaning.done() and not backend_closes
    assert controller.cached_status()["admission_open"] is False
    proceed.set()
    with pytest.raises((EpisodeError, cu.BackendError)):
        await beginning
    await cleaning
    assert indicator.closed and backend_closes == [True]
    assert not tool.episode_status()["admission_open"]
    assert coordinator.get_capability("computer_use.episodes") is None


@run_async
async def test_cleanup_retains_capability_and_backend_until_indicator_settles(
    rig, monkeypatch
):
    tool, indicator = rig
    backend_closes = []
    tool._backend.close = lambda: backend_closes.append(True)
    real_close = indicator.close
    indicator.close = lambda: {
        "closed": False,
        "surface_absent": False,
        "process_settled": False,
        "helper_exit": None,
    }
    monkeypatch.setattr(cu, "_select_and_build", lambda cfg: tool)
    coordinator = MockCoordinator()
    mounted = await cu.mount(coordinator)
    controller = coordinator.get_capability("computer_use.episodes")
    await controller.begin()
    with pytest.raises(RuntimeError, match="indicator settlement"):
        await mounted()
    status = controller.cached_status()
    assert status["release_confirmed"] is True
    assert status["indicator_settled"] is False
    assert status["state"] == "interrupted"
    assert not backend_closes
    assert coordinator.get_capability("computer_use.episodes") is controller
    indicator.close = real_close
    await mounted()
    assert backend_closes == [True]
    assert coordinator.get_capability("computer_use.episodes") is None
