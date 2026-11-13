"""Headless lifecycle checks; no platform, installed-wheel or GUI qualification.

Use real public Core Session/Coordinator cleanup with a test-only loader and inert
backends. The installed consumer smoke remains a separate parent's check.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "hook-computer-use"))

import amplifier_module_hook_computer_use as hook
import amplifier_module_tool_computer_use as cu
from amplifier_core import AmplifierSession, HookResult
from amplifier_core.testing import MockCoordinator
from amplifier_module_tool_computer_use import overlay_linux
from amplifier_module_tool_computer_use.exclusion import ExclusionZone
from amplifier_module_tool_computer_use.geometry import Display
from amplifier_module_tool_computer_use.linux_x11 import LinuxX11Backend
from amplifier_module_tool_computer_use.shared_transport import acquire_shared_transport


class Backend:
    name = "inert-cleanup"
    is_remote = False

    def __init__(self, close=None):
        self.close = Mock(side_effect=close)


class ReopeningBackend(LinuxX11Backend):
    """No OS calls: model Linux's lazy reconnect if a closed backend is used."""

    def __init__(self):
        super().__init__({"display": ":inert-dispatch"})
        self._display = SimpleNamespace(close=Mock())
        self.calls = []
        self.reconnections = 0
        for name in (
            "key",
            "capture",
            "get_clipboard",
            "set_clipboard",
            "list_monitors",
            "screen_geometry",
            "list_windows",
            "focus_window",
        ):
            setattr(self, name, self.operation(name))

    def operation(self, name):
        def call(*args, **kwargs):
            self.calls.append(name)
            self._ensure_connected()
            raise cu.BackendError("unexpected backend invocation")

        call.__name__ = name
        return call

    def _ensure_connected(self):
        if self._display is None:
            self.reconnections += 1
            self._display = SimpleNamespace(close=Mock())


@pytest.fixture
def dispatch_computer(monkeypatch):
    computer = cu.ComputerTool(ReopeningBackend())
    computer._display = Display(
        screen_width=400, screen_height=300, model_width=400, model_height=300
    )
    computer._announced = True
    monkeypatch.setattr(cu, "_select_and_build", lambda cfg: computer)
    return computer


class Provider:
    def request_budget(self, request, **kwargs):
        return request

    async def complete(self, request, **kwargs):
        return request


class Loader:
    """Only module loading is synthetic; initialization/cleanup are public Core."""

    async def load(self, module_id, config=None, **kwargs):
        async def bound(coordinator):
            if module_id == "tool-computer-use":
                return await cu.mount(coordinator, config)
            if module_id == "hook-computer-use":
                return await hook.mount(coordinator, config)
            return None

        return bound

    def get_on_session_ready_queue(self):
        return []

    def clear_on_session_ready_queue(self):
        pass


@pytest.fixture
def computer(monkeypatch):
    tool = cu.ComputerTool(Backend(), config={})
    monkeypatch.setattr(cu, "_select_and_build", lambda cfg: tool)
    return tool


@pytest.mark.asyncio
async def test_public_session_initialization_registers_and_awaits_mount_returns(
    computer,
):
    session = AmplifierSession(
        {
            "session": {
                "orchestrator": "test-orchestrator",
                "context": "test-context",
            },
            "tools": [{"module": "tool-computer-use"}],
            "hooks": [{"module": "hook-computer-use"}],
        },
    )
    session.coordinator.loader = Loader()
    await session.initialize()
    coordinator = session.coordinator
    assert coordinator.get("tools", "computer") is computer
    assert coordinator.hooks.list_handlers(hook.TOOL_PRE)[hook.TOOL_PRE]
    await session.cleanup()
    await session.cleanup()
    computer._backend.close.assert_called_once_with()
    assert not coordinator.hooks.list_handlers(hook.TOOL_PRE).get(hook.TOOL_PRE)


@pytest.mark.asyncio
async def test_direct_metadata_and_concurrent_cleanup_are_preserved(computer):
    coordinator = MockCoordinator()
    result = await cu.mount(coordinator)
    assert isinstance(result, dict) and callable(result)
    assert result["provides"] == ["computer", "desktop"]
    assert coordinator.get("tools", "desktop")._computer is computer
    # No eager registration: Core registers this one return value, not two.
    await coordinator.cleanup()
    computer._backend.close.assert_not_called()
    await asyncio.gather(result(), result())
    computer._backend.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_retarget_cleanup_closes_final_binding_not_initial_backend(
    computer, monkeypatch
):
    coordinator = MockCoordinator()
    result = await cu.mount(coordinator)
    original = computer._backend
    final = Backend()
    final.name = "inert-cleanup-retargeted"
    monkeypatch.setattr(cu, "select_backend", lambda cfg: final)
    monkeypatch.setattr(cu, "_build_coexistence_guard", lambda *args: object())
    monkeypatch.setattr(cu, "_build_announcement", lambda *args: None)
    monkeypatch.setattr(
        cu, "_get_channel_ledger", lambda *args: SimpleNamespace(held_tokens=[])
    )
    monkeypatch.setattr(
        cu, "_get_channel_band_state", lambda *args: cu._ChannelBandState()
    )
    monkeypatch.setattr(
        computer, "_resolve_display_for_target", lambda *args, **kwargs: (None, None)
    )
    assert computer.retarget("local").success
    original.close.assert_called_once_with()
    final.close.assert_not_called()
    await result()
    await result()
    original.close.assert_called_once_with()
    final.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_unavailable_activation_transfers_custody_to_original_return(monkeypatch):
    def unavailable(cfg):
        raise cu.NoBackendAvailable([("inert", "no display")])

    monkeypatch.setattr(cu, "_select_and_build", unavailable)
    coordinator = MockCoordinator()
    result = await cu.mount(coordinator)
    assert callable(result)
    assert result["provides"] == ["computer_use_unavailable"]
    stub = coordinator.get("tools", "computer_use_unavailable")
    computer = cu.ComputerTool(Backend(), config={})
    monkeypatch.setattr(cu, "_select_and_build", lambda cfg: computer)
    activated = await stub.execute({"action": "activate", "target": "local"})
    assert activated.success, activated.error
    assert coordinator.get("tools", "computer") is computer
    assert coordinator.get("tools", stub.name) is None
    await result()
    await result()
    computer._backend.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_unavailable_without_activation_is_cleanup_callable(monkeypatch):
    def unavailable(cfg):
        raise cu.NoBackendAvailable([("inert", "no display")])

    monkeypatch.setattr(cu, "_select_and_build", unavailable)
    result = await cu.mount(MockCoordinator())
    await result()
    await result()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "depth,held,lowered", [(0, [], True), (1, [], False), (0, ["sibling"], False)]
)
async def test_shared_band_and_ledger_are_not_torn_down_by_one_session(
    computer, depth, held, lowered
):
    coordinator = MockCoordinator()
    computer._announcement = SimpleNamespace(show=Mock(), hide=Mock())
    computer._band_state = cu._ChannelBandState(depth=depth)
    computer._ledger = SimpleNamespace(
        held_tokens=held, stop=Mock(), release_all=Mock()
    )
    guard = object()
    computer._coexistence_guard = guard
    computer.halt_notices.append({"message": "retained halt"})
    result = await cu.mount(coordinator)
    await result()
    assert computer._announcement.hide.called is lowered
    computer._ledger.stop.assert_not_called()
    computer._ledger.release_all.assert_not_called()
    assert computer._coexistence_guard is guard
    assert computer.halt_notices == [{"message": "retained halt"}]
    computer._backend.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_unresolved_owned_hold_retains_backend_and_deadman(computer):
    computer._mouse_pending["mouse:left"] = {"up_confirmed": False}
    computer._ledger = SimpleNamespace(held_tokens=["mouse:left"], stop=Mock())
    result = await cu.mount(MockCoordinator())
    with pytest.raises(RuntimeError, match="cleanup deferred"):
        await result()
    computer._backend.close.assert_not_called()
    computer._ledger.stop.assert_not_called()
    assert "mouse:left" in computer._mouse_pending
    # Model a separately confirmed release; cleanup must not replay any input.
    computer._mouse_pending.clear()
    computer._ledger.held_tokens.clear()
    await result()
    await result()
    computer._backend.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_shared_transport_last_owner_alone_closes_transport(monkeypatch):
    transport = SimpleNamespace(close=Mock())
    key = ("cleanup-test", "inert-never-connected", None)
    first = acquire_shared_transport(key, lambda: transport)
    second = acquire_shared_transport(key, lambda: transport)
    computers = [
        cu.ComputerTool(Backend(handle.close), config={}) for handle in (first, second)
    ]
    results = []
    for computer in computers:
        monkeypatch.setattr(cu, "_select_and_build", lambda cfg, tool=computer: tool)
        results.append(await cu.mount(MockCoordinator()))
    try:
        await results[0]()
        transport.close.assert_not_called()
        await results[0]()
        transport.close.assert_not_called()
        await results[1]()
        transport.close.assert_called_once_with()
        await results[1]()
        transport.close.assert_called_once_with()
    finally:
        first.close()
        second.close()


@pytest.mark.asyncio
async def test_overlay_failure_still_closes_backend_once(computer):
    computer._announcement = SimpleNamespace(
        show=Mock(), hide=Mock(side_effect=RuntimeError("hide failed"))
    )
    computer._band_state = cu._ChannelBandState()
    result = await cu.mount(MockCoordinator())
    with pytest.raises(RuntimeError, match="hide failed"):
        await result()
    with pytest.raises(RuntimeError, match="hide failed"):
        await result()
    computer._backend.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_failed_close_is_not_replayed_or_relabelled_success(computer):
    computer._backend.close.side_effect = RuntimeError("close unconfirmed")
    result = await cu.mount(MockCoordinator())
    for _ in range(2):
        with pytest.raises(RuntimeError, match="close unconfirmed"):
            await result()
    computer._backend.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_hook_cleanup_uses_registration_handles_and_restores_own_wrappers(
    computer,
):
    coordinator = MockCoordinator()
    await coordinator.mount("tools", computer, name="computer")
    provider = Provider()
    await coordinator.mount("providers", provider, name="test")
    original = dict(vars(provider))
    first = await hook.mount(coordinator)
    second = await hook.mount(coordinator)
    assert callable(first) and first["name"] == "hook-computer-use"
    calls = []

    async def sentinel(event, data):
        calls.append(event)
        return HookResult(action="continue")

    # Same public name: cleanup must use its exact registration, not name-wide removal.
    remove_sentinel = coordinator.hooks.register(
        hook.TOOL_PRE, sentinel, name="hook-computer-use-gate"
    )
    try:
        await coordinator.hooks.emit(hook.PROVIDER_REQUEST, {"provider": "test"})
        assert getattr(provider, hook._WRAPPED_FLAG)
        await second()
        assert getattr(provider, hook._WRAPPED_FLAG)
        await first()
        await first()
        assert vars(provider) == original
        await coordinator.hooks.emit(hook.TOOL_PRE, {})
        assert calls == [hook.TOOL_PRE]
    finally:
        remove_sentinel()


@pytest.mark.asyncio
async def test_hook_cleanup_does_not_replace_later_provider_adapter(computer):
    coordinator = MockCoordinator()
    await coordinator.mount("tools", computer, name="computer")
    provider = Provider()
    await coordinator.mount("providers", provider, name="test")
    result = await hook.mount(coordinator)
    await coordinator.hooks.emit(hook.PROVIDER_REQUEST, {"provider": "test"})
    own_complete = provider.complete

    async def later(request, **kwargs):
        return await own_complete(request, **kwargs)

    provider.complete = later
    await result()
    assert provider.complete is later
    assert getattr(provider, hook._WRAPPED_FLAG)
    # Budget was still exclusively ours and is restored independently.
    assert "request_budget" not in vars(provider)
    assert await provider.complete(SimpleNamespace(messages=[])) is not None


@pytest.mark.asyncio
async def test_cleanup_fences_retained_tool_before_backend_allocation(
    computer, monkeypatch
):
    computer._announced = True  # Must also reject the old announcement fast path.
    result = await cu.mount(MockCoordinator())
    await result()
    select = Mock(side_effect=AssertionError("post-cleanup backend allocation"))
    monkeypatch.setattr(cu, "select_backend", select)
    assert not computer.retarget("local").success
    with pytest.raises(cu.AnnouncementRefused, match="cleanup has started"):
        computer._ensure_announced()
    select.assert_not_called()
    computer._backend.close.assert_called_once_with()


def prepare_retarget(computer, final, monkeypatch):
    monkeypatch.setattr(cu, "select_backend", lambda cfg: final)
    monkeypatch.setattr(cu, "_build_coexistence_guard", lambda *args: object())
    monkeypatch.setattr(cu, "_build_announcement", lambda *args: None)
    monkeypatch.setattr(
        cu, "_get_channel_ledger", lambda *args: SimpleNamespace(held_tokens=[])
    )
    monkeypatch.setattr(
        cu, "_get_channel_band_state", lambda *args: cu._ChannelBandState()
    )
    monkeypatch.setattr(
        computer, "_resolve_display_for_target", lambda *args, **kwargs: (None, None)
    )


@pytest.mark.asyncio
async def test_retarget_in_progress_transfers_final_binding_before_cleanup(
    computer, monkeypatch
):
    original = computer._backend
    final = Backend()
    final.name = "inert-concurrent-retarget"
    prepare_retarget(computer, final, monkeypatch)
    result = await cu.mount(MockCoordinator())
    loop = asyncio.get_running_loop()
    selecting, cleanup_waiting = asyncio.Event(), asyncio.Event()
    proceed = threading.Event()
    lock = threading.Lock()

    class ObservedLock:
        def acquire(self, blocking=True):
            if blocking:
                loop.call_soon_threadsafe(cleanup_waiting.set)
            return lock.acquire(blocking=blocking)

        def release(self):
            lock.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *args):
            self.release()

    computer._announce_lock = ObservedLock()

    def select(cfg):
        loop.call_soon_threadsafe(selecting.set)
        assert proceed.wait(2), "test retarget gate was not released"
        return final

    monkeypatch.setattr(cu, "select_backend", select)
    switching = asyncio.create_task(asyncio.to_thread(computer.retarget, "local"))
    await asyncio.wait_for(selecting.wait(), 2)
    cleanup = asyncio.create_task(result())
    try:
        await asyncio.wait_for(cleanup_waiting.wait(), 2)
        assert not cleanup.done()
    finally:
        proceed.set()
    switched, _ = await asyncio.wait_for(asyncio.gather(switching, cleanup), 2)
    assert switched.success, switched.error
    assert computer._backend is final
    original.close.assert_called_once_with()
    final.close.assert_called_once_with()
    monkeypatch.setattr(
        cu, "select_backend", Mock(side_effect=AssertionError("resurrection"))
    )
    assert not computer.retarget("local").success
    cu.select_backend.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_in_progress_refuses_retarget_without_allocating(
    computer, monkeypatch
):
    result = await cu.mount(MockCoordinator())
    loop = asyncio.get_running_loop()
    closing, proceed = asyncio.Event(), threading.Event()

    def close():
        loop.call_soon_threadsafe(closing.set)
        assert proceed.wait(2), "test cleanup gate was not released"

    computer._backend.close.side_effect = close
    select = Mock(side_effect=AssertionError("cleanup-time allocation"))
    monkeypatch.setattr(cu, "select_backend", select)
    cleanup = asyncio.create_task(result())
    try:
        await asyncio.wait_for(closing.wait(), 2)
        assert not computer.retarget("local").success
        select.assert_not_called()
    finally:
        proceed.set()
    await asyncio.wait_for(cleanup, 2)
    assert not computer.retarget("local").success
    select.assert_not_called()


async def unavailable_mount(monkeypatch, coordinator):
    def unavailable(cfg):
        raise cu.NoBackendAvailable([("inert", "no display")])

    monkeypatch.setattr(cu, "_select_and_build", unavailable)
    return await cu.mount(coordinator)


@pytest.mark.asyncio
async def test_cleanup_before_activation_refuses_before_allocating(monkeypatch):
    coordinator = MockCoordinator()
    result = await unavailable_mount(monkeypatch, coordinator)
    stub = coordinator.get("tools", "computer_use_unavailable")
    await result()
    build = Mock(side_effect=AssertionError("post-cleanup activation allocation"))
    monkeypatch.setattr(cu, "_select_and_build", build)
    activated = await stub.execute({"action": "activate", "target": "local"})
    assert not activated.success
    build.assert_not_called()


@pytest.mark.asyncio
async def test_activation_and_cleanup_serialize_mount_and_custody_transfer(monkeypatch):
    desktop_mount, proceed = asyncio.Event(), asyncio.Event()

    class Coordinator(MockCoordinator):
        async def mount(self, point, module, name=None):
            if point == "tools" and name == "desktop":
                desktop_mount.set()
                await proceed.wait()
            await super().mount(point, module, name=name)

    coordinator = Coordinator()
    result = await unavailable_mount(monkeypatch, coordinator)
    stub = coordinator.get("tools", "computer_use_unavailable")
    computer = cu.ComputerTool(Backend())
    build = Mock(return_value=computer)
    monkeypatch.setattr(cu, "_select_and_build", build)
    activation = asyncio.create_task(stub.execute({"action": "activate"}))
    await asyncio.wait_for(desktop_mount.wait(), 2)
    cleanup_entered = asyncio.Event()

    async def cleanup():
        cleanup_entered.set()
        await result()

    cleaning = asyncio.create_task(cleanup())
    await cleanup_entered.wait()
    assert not cleaning.done()
    proceed.set()
    activated, _ = await asyncio.wait_for(asyncio.gather(activation, cleaning), 2)
    assert activated.success, activated.error
    computer._backend.close.assert_called_once_with()
    assert not (await stub.execute({"action": "activate"})).success
    assert not computer.retarget("local").success
    build.assert_called_once()


@pytest.mark.asyncio
async def test_cleanup_wins_queued_activation_race_before_any_allocation(monkeypatch):
    coordinator = MockCoordinator()
    result = await unavailable_mount(monkeypatch, coordinator)
    stub = coordinator.get("tools", "computer_use_unavailable")
    build = Mock(side_effect=AssertionError("queued activation after cleanup"))
    monkeypatch.setattr(cu, "_select_and_build", build)
    await stub._lifecycle_lock.acquire()
    cleanup_started, activation_started = asyncio.Event(), asyncio.Event()

    async def cleanup():
        cleanup_started.set()
        await result()

    async def activate():
        activation_started.set()
        return await stub.execute({"action": "activate"})

    cleaning = asyncio.create_task(cleanup())
    await cleanup_started.wait()  # cleanup has queued for the held asyncio lock
    activation = asyncio.create_task(activate())
    await activation_started.wait()
    stub._lifecycle_lock.release()
    _, activated = await asyncio.wait_for(asyncio.gather(cleaning, activation), 2)
    assert not activated.success
    build.assert_not_called()


class CoupledWindow:
    """X11 resource lifetime belongs to its creating connection, not a tool."""

    def __init__(self, connection):
        self.connection = connection
        self.alive = True
        self.mapped = False

    def map(self):
        assert not self.connection.closed
        self.mapped = True

    def unmap(self):
        self.mapped = False

    def destroy(self):
        self.alive = False

    def shape_rectangles(self, *args):
        pass

    def create_gc(self, **kwargs):
        return object()

    def fill_rectangle(self, *args):
        pass


class CoupledDisplay:
    """No socket: faithfully model close destroying every owned window."""

    def __init__(self, address):
        self.address = address
        self.closed = False
        self.close_calls = 0
        self.windows = []
        self.events = []

    def get_display_name(self):
        assert not self.closed
        return self.address

    def screen(self):
        assert not self.closed
        return SimpleNamespace(
            root=SimpleNamespace(create_window=self.create_window),
            root_depth=24,
            default_colormap=SimpleNamespace(
                alloc_color=lambda *args: SimpleNamespace(pixel=1)
            ),
        )

    def create_window(self, *args, **kwargs):
        window = CoupledWindow(self)
        self.windows.append(window)
        return window

    def sync(self):
        assert not self.closed

    def close(self):
        self.closed = True
        self.close_calls += 1
        for window in self.windows:
            window.destroy()

    def pending_events(self):
        assert not self.closed
        if not self.events:
            raise EOFError("end deterministic event pump")
        return len(self.events)

    def next_event(self):
        assert not self.closed
        return self.events.pop(0)


class ManualPollThread:
    """Exercise real event handling deterministically, without a desktop."""

    def __init__(self, target, args, **kwargs):
        self.target, self.args = target, args

    def start(self):
        pass

    def pump(self):
        self.target(*self.args)


@pytest.mark.asyncio
async def test_cached_linux_overlay_survives_creator_cleanup_and_can_raise_again(
    monkeypatch,
):
    owned_connections = []

    def connect(address):
        assert address == ":inert-lifecycle.0"
        connection = CoupledDisplay(address)
        owned_connections.append(connection)
        return connection

    monkeypatch.setattr(overlay_linux, "_IMPORT_ERROR", None)
    monkeypatch.setattr(overlay_linux, "_xdisplay", SimpleNamespace(Display=connect))
    monkeypatch.setattr(
        overlay_linux,
        "threading",
        SimpleNamespace(Event=threading.Event, Thread=ManualPollThread),
    )
    monkeypatch.setattr(
        overlay_linux,
        "X",
        SimpleNamespace(
            InputOutput=1,
            CopyFromParent=0,
            ExposureMask=1,
            ButtonPressMask=2,
            ButtonPress=4,
        ),
    )
    monkeypatch.setattr(
        overlay_linux,
        "shape",
        SimpleNamespace(
            SO=SimpleNamespace(Set=0),
            SK=SimpleNamespace(Input=0),
        ),
    )
    callbacks = []
    monkeypatch.setattr(
        cu, "_on_overlay_pause", lambda *args: callbacks.append("pause")
    )
    monkeypatch.setattr(
        cu, "_on_overlay_cancel", lambda *args: callbacks.append("cancel")
    )
    backend_a, backend_b = LinuxX11Backend(), LinuxX11Backend()
    creator_connection = CoupledDisplay(":inert-lifecycle.0")
    backend_a._display = creator_connection
    backend_b._display = CoupledDisplay(":inert-lifecycle.0")
    guard = SimpleNamespace(exclusion=ExclusionZone())
    display = Display(
        screen_width=400, screen_height=300, model_width=400, model_height=300
    )
    state = cu._ChannelBandState()
    ledger = SimpleNamespace(held_tokens=[])
    a, b = cu.ComputerTool(backend_a), cu.ComputerTool(backend_b)
    results = []
    handle = None
    try:
        for tool in (a, b):
            monkeypatch.setattr(cu, "_select_and_build", lambda cfg, t=tool: t)
            results.append(await cu.mount(MockCoordinator()))
            tool._coexistence_guard = guard
            tool._band_state = state
            tool._ledger = ledger
            tool._display = display
            tool._announcement = cu._build_announcement(
                tool._backend, guard, {}, display
            )
            tool._announced = True
        handle = b._announcement
        assert handle is a._announcement
        first_window = handle._window
        assert first_window.connection is not creator_connection
        token = b._band_enter()
        await results[0]()
        assert creator_connection.closed
        assert first_window.alive and first_window.mapped
        assert not owned_connections[0].closed

        def click_buttons():
            connection = handle._display
            for button in handle.buttons:
                connection.events.append(
                    SimpleNamespace(
                        type=4,
                        root_x=button.rect.x1 + 1,
                        root_y=button.rect.y1 + 1,
                    )
                )
            handle._poll_thread.pump()

        click_buttons()
        assert callbacks == ["pause", "cancel"]
        old_poll = handle._poll_thread
        b._band_exit(token)
        assert owned_connections[0].closed and not first_window.alive
        assert handle._display is None
        # Cached decision survives its creator, but an idle handle keeps no socket.
        assert cu._build_announcement(backend_b, guard, {}, display) is handle
        token = b._band_enter()
        assert handle._window.alive and handle._window.mapped
        assert len(owned_connections) == 2
        old_poll.pump()  # Must not consume the new generation's event queue.
        click_buttons()
        assert callbacks == ["pause", "cancel", "pause", "cancel"]
        b._band_exit(token)
        await results[1]()
        assert all(conn.closed and conn.close_calls == 1 for conn in owned_connections)
        assert backend_a._display is None and backend_b._display is None
    finally:
        if handle is not None:
            handle.hide()
        for cleanup in results:
            await cleanup()


def test_linux_owned_connection_is_released_if_show_fails(monkeypatch):
    creator = CoupledDisplay(":inert-lifecycle.0")
    owned = CoupledDisplay(creator.address)
    owned.screen = Mock(side_effect=RuntimeError("window creation failed"))
    monkeypatch.setattr(overlay_linux, "_IMPORT_ERROR", None)
    monkeypatch.setattr(
        overlay_linux, "_xdisplay", SimpleNamespace(Display=lambda name: owned)
    )
    handle = overlay_linux.LinuxOverlay(creator, own_display=True, screen_width=400)
    with pytest.raises(RuntimeError, match="window creation failed"):
        handle.show()
    assert owned.closed and owned.close_calls == 1
    assert not creator.closed
    assert handle._display is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"action": "key", "text": "Return", "repeat": 2},
        {
            "version": 1,
            "actions": [{"action": "key", "text": "Return"}],
            "capture_after": False,
        },
        {"action": "screenshot"},
        {"version": 1, "actions": [{"action": "screenshot"}], "capture_after": False},
    ],
)
async def test_cleanup_wins_after_band_before_computer_worker_dispatch(
    dispatch_computer,
    monkeypatch,
    payload,
):
    computer = dispatch_computer
    cleanup = await cu.mount(MockCoordinator())
    loop = asyncio.get_running_loop()
    queued, proceed = asyncio.Event(), threading.Event()
    run_owned = computer._run_owned_step

    def delayed(*args):
        loop.call_soon_threadsafe(queued.set)
        assert proceed.wait(2), "test dispatch gate was not released"
        return run_owned(*args)

    monkeypatch.setattr(computer, "_run_owned_step", delayed)
    running = asyncio.create_task(computer.execute(payload))
    try:
        await asyncio.wait_for(queued.wait(), 2)
        await asyncio.wait_for(cleanup(), 2)
        assert computer._backend._display is None
    finally:
        proceed.set()
    result = await asyncio.wait_for(running, 2)
    assert not result.success
    assert "cleanup has started" in result.error["message"]
    assert computer._backend.calls == []
    assert computer._backend.reconnections == 0
    assert computer._backend._display is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"action": "get_clipboard"},
        {"action": "set_clipboard", "text": "inert"},
        {"action": "list_monitors"},
        {"action": "select_monitor", "monitor": "virtual-desktop"},
        {"action": "list_windows"},
    ],
)
async def test_cleanup_wins_before_direct_desktop_worker_dispatch(
    dispatch_computer,
    monkeypatch,
    payload,
):
    computer = dispatch_computer
    cleanup = await cu.mount(MockCoordinator())
    desktop = cu.DesktopTool(computer)
    loop = asyncio.get_running_loop()
    queued, proceed = asyncio.Event(), threading.Event()
    submit = computer._submit_batch_thread

    def delayed_submit(function, *args, **kwargs):
        # Gate the actual queued backend worker, not the earlier announcement
        # or band workers. Cleanup must take the lock before this dispatch does.
        if function.__name__ != "_run_owned_step":
            return submit(function, *args, **kwargs)

        def delayed():
            loop.call_soon_threadsafe(queued.set)
            assert proceed.wait(2), "test desktop dispatch gate was not released"
            return function(*args, **kwargs)

        return submit(delayed)

    monkeypatch.setattr(computer, "_submit_batch_thread", delayed_submit)
    running = asyncio.create_task(desktop.execute(payload))
    try:
        await asyncio.wait_for(queued.wait(), 2)
        await asyncio.wait_for(cleanup(), 2)
        assert computer._backend._display is None
    finally:
        proceed.set()
    result = await asyncio.wait_for(running, 2)
    assert not result.success
    assert "cleanup has started" in result.error["message"]
    assert computer._backend.calls == []
    assert computer._backend.reconnections == 0
    assert computer._backend._display is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,payload",
    [
        ("computer", {"action": "key", "text": "Return"}),
        (
            "computer",
            {
                "version": 1,
                "actions": [{"action": "screenshot"}],
                "capture_after": False,
            },
        ),
        ("computer", {"action": "screenshot"}),
        ("desktop", {"action": "get_clipboard"}),
        ("desktop", {"action": "list_monitors"}),
        ("desktop", {"action": "select_monitor", "monitor": "virtual-desktop"}),
        ("desktop", {"action": "doctor"}),
    ],
)
async def test_public_execute_after_cleanup_never_reads_os(
    dispatch_computer,
    monkeypatch,
    tool,
    payload,
):
    computer = dispatch_computer
    cleanup = await cu.mount(MockCoordinator())
    doctor = Mock(side_effect=AssertionError("post-cleanup OS diagnostic"))
    monkeypatch.setattr(cu, "_build_doctor_report", doctor)
    await cleanup()
    executor = computer if tool == "computer" else cu.DesktopTool(computer)
    result = await executor.execute(payload)
    assert not result.success
    assert "cleanup has started" in result.error["message"]
    doctor.assert_not_called()
    assert computer._backend.calls == []
    assert computer._backend.reconnections == 0


@pytest.mark.asyncio
async def test_cleanup_before_band_acquisition_never_reopens_overlay(dispatch_computer):
    computer = dispatch_computer
    computer._announcement = SimpleNamespace(show=Mock(), hide=Mock(), shown=False)
    computer._band_state = cu._ChannelBandState()
    cleanup = await cu.mount(MockCoordinator())
    await cleanup()
    with pytest.raises(cu.AnnouncementRefused, match="cleanup has started"):
        computer._band_enter()
    computer._announcement.show.assert_not_called()
    assert computer._band_state.depth == 0
    assert computer._backend.calls == []
    assert computer._backend.reconnections == 0
