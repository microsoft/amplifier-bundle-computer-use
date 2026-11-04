"""Unit tests: `desktop(action="retarget")` (M4 - docs/designs/capability-
awareness.md \u00a76), and the atomic-binding fix a six-lens council named for
its verified race (\u00a76.3/\u00a710.4).

What this file proves:

1. The verified race is real (a naive, field-by-field swap - "self._backend
   = new" then, separately, "self._announced = False" - lets a concurrent
   unlocked reader observe a torn mix), and `ComputerTool.retarget()`'s
   single atomic `_Binding` reference swap closes it: hammering
   `retarget()` against a concurrent action never produces that mix.
2. Nothing built during a failed retarget leaks: the half-built new
   backend is always released, and the current binding is left exactly as
   it was.
3. The NEW target gets its own real disclosure before the swap - never
   reuses the OLD target's `announced` state.
4. A refusal (human present, disclosure declined) blocks the retarget and
   leaves the current binding untouched.

No real backend, no real display server, no real SSH - matching every
other test file in this suite. Every backend name/host used below is
unique per test (`_unique()`) because `_channel_ledgers`/
`_channel_band_state`/`_announcement_decisions` are module-level, process-
wide caches that persist across tests in the same run.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import amplifier_module_tool_computer_use as cu  # noqa: E402
from amplifier_module_tool_computer_use.backend import (  # noqa: E402
    BackendError,
    ScreenGeometry,
)


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class _FakeLocalBackend:
    """A local, presence-capable backend - real enough for
    `_build_coexistence_guard` to build a real guard, but not an instance
    of `LinuxX11Backend`/`WindowsBackend`/`MacOSBackend`, so
    `_dispatch_announcement` takes its "no channel implemented" scope-
    boundary branch (logs, returns `None`) - exactly like any backend type
    this module does not recognize. That is a real, exercised code path,
    not a test artifact.
    """

    is_remote = False

    def __init__(self, name: str) -> None:
        self.name = name
        self.presence_platform = "linux-x11"
        self.close_calls = 0
        # Policy-bypass race repro (\u00a76.3/\u00a710.4 fix): records every
        # `click()` this backend actually received, so a test can prove
        # WHICH backend a MUTATING action's write really landed on.
        self.click_calls: list[tuple] = []

    def type_text(self, text: str) -> None:
        """Legacy backend with no guarded-typing capability declaration."""

    def presence_idle_ms(self) -> float:
        return 999_999.0

    def current_target(self):
        return None

    def screen_geometry(self) -> ScreenGeometry:
        return ScreenGeometry(1920, 1080, 0, 0)

    def list_monitors(self):
        raise BackendError("no monitor enumeration on this fake")

    def click(self, x, y, button="left", count=1) -> None:
        self.click_calls.append((x, y, button, count))

    def close(self) -> None:
        self.close_calls += 1


class _FakeRemoteBackend:
    """A remote, presence-capable backend that answers the wire ops
    `_build_remote_announcement`/`_refuse_if_disclosure_declined_with_human_present`
    actually call - `announce_raise` (the remote overlay path, since
    `presence_platform="linux-x11"`) and `capture_scaled` (so `screenshot`
    can actually run against it). `disclose_calls`/`captures` are the
    instrumentation the race test reads: whether THIS backend's own
    disclosure had already happened by the time a capture ran against it.
    """

    is_remote = True

    def __init__(self, name: str, user_host: str, idle_ms: float = 999_999.0) -> None:
        self.name = name
        self.user_host = user_host
        self.presence_platform = "linux-x11"
        self.handshake: dict = {"ops": []}
        self._idle_ms = idle_ms
        self.close_calls = 0
        self.disclose_calls = 0
        self.captures: list[bool] = []
        # Policy-bypass race repro (\u00a76.3/\u00a710.4 fix) - see
        # `_FakeLocalBackend.click_calls`.
        self.click_calls: list[tuple] = []

    def type_text(self, text: str) -> None:
        pass

    def click(self, x, y, button="left", count=1) -> None:
        self.click_calls.append((x, y, button, count))

    def presence_idle_ms(self) -> float:
        return self._idle_ms

    def current_target(self):
        return None

    def screen_geometry(self) -> ScreenGeometry:
        return ScreenGeometry(1920, 1080, 0, 0)

    def list_monitors(self):
        raise BackendError("no monitor enumeration on this fake")

    def announce_raise(self, **kwargs):
        self.disclose_calls += 1
        return {"shown": True, "buttons": {}}

    def announcement_status(self) -> dict:
        return {}

    def capture_scaled(self, region, model_size, max_edge, max_pixels) -> str:
        self.captures.append(self.disclose_calls > 0)
        return "ZmFrZQ=="

    def close(self) -> None:
        self.close_calls += 1


class _FakeGuardCapableBackend:
    """Explicitly promises and honors per-character guard callbacks.

    Unlike the legacy fakes above, this backend owns the before/injection/
    after sequence. Accepting a `guard` parameter alone is not the contract.
    """

    is_remote = False
    type_text_guarded = True

    def __init__(self, name: str) -> None:
        self.name = name
        self.presence_platform = "linux-x11"
        self.close_calls = 0
        self.type_text_calls: list[tuple] = []
        self.type_events: list[tuple[str, str]] = []

    def type_text(self, text: str, guard=None) -> None:
        self.type_text_calls.append((text, guard))
        for ch in text:
            if guard is not None:
                guard.before_event()
                self.type_events.append(("before", ch))
            self.type_events.append(("type", ch))
            if guard is not None:
                guard.after_event()
                self.type_events.append(("after", ch))

    def presence_idle_ms(self) -> float:
        return 999_999.0

    def current_target(self):
        return None

    def screen_geometry(self) -> ScreenGeometry:
        return ScreenGeometry(1920, 1080, 0, 0)

    def list_monitors(self):
        raise BackendError("no monitor enumeration on this fake")

    def click(self, x, y, button="left", count=1) -> None:
        pass

    def close(self) -> None:
        self.close_calls += 1


def _mounted_tool(backend, cfg: dict | None = None) -> cu.ComputerTool:
    """Mirrors `mount()`'s own sequencing (guard -> channel_key -> ledger ->
    band_state), without going through the real `mount()`/coordinator."""
    cfg = cfg or {}
    tool = cu.ComputerTool(backend, cfg)
    tool.resolve_display()
    guard = cu._build_coexistence_guard(backend, cfg)
    tool._coexistence_guard = guard
    if guard is not None:
        key = cu._channel_identity(backend)
        tool._channel_key = key
        tool._ledger = cu._get_channel_ledger(key)
        tool._band_state = cu._get_channel_band_state(key)
    return tool


# -- 1a. The race is real: a naive, field-by-field swap reproduces it -------


def test_naive_field_by_field_swap_reproduces_the_verified_race():
    """FAILS (demonstrates the exact bug the council verified) when the
    swap is done as SEPARATE, sequential field writes - "self._backend =
    new" and, later, "self._announced = False" - instead of one atomic
    `_Binding` replacement. Deliberately bypasses `ComputerTool.retarget()`
    (which performs the swap as ONE reference assignment) to prove the
    vulnerability class is real, not hypothetical - see the next test for
    proof that `retarget()` itself closes it.
    """
    old_backend = _FakeLocalBackend(_unique("race1-old"))
    new_backend = _FakeLocalBackend(_unique("race1-new"))
    tool = _mounted_tool(old_backend)
    tool._announced = True  # old target already disclosed, sometime in the past

    observed_undisclosed_mix = threading.Event()
    swap_started = threading.Event()
    stop_hammering = threading.Event()

    def _naive_swap() -> None:
        # The exact shape the review named as the pre-fix bug: the
        # backend field flips to the NEW target first; only afterwards
        # (standing in for "the other six field writes") does `announced`
        # reset. Any unlocked reader in between sees NEW backend + STALE
        # announced=True.
        tool._backend = new_backend
        swap_started.set()
        time.sleep(0.05)
        tool._announced = False
        stop_hammering.set()

    def _hammer() -> None:
        swap_started.wait(timeout=2.0)
        while not stop_hammering.is_set():
            if tool._backend is new_backend and tool._announced:
                observed_undisclosed_mix.set()
                return

    t1 = threading.Thread(target=_naive_swap)
    t2 = threading.Thread(target=_hammer)
    t1.start()
    t2.start()
    t1.join(timeout=2.0)
    t2.join(timeout=2.0)

    assert observed_undisclosed_mix.is_set(), (
        "expected to observe backend=NEW paired with a STALE announced=True "
        "at least once during a naive field-by-field swap - this is the "
        "exact torn read the six-lens council verified. If this becomes "
        "flaky, the sleep() above is too short for this machine, not that "
        "the race stopped being real."
    )


# -- 1b. The fix: retarget()'s atomic swap never produces that mix ----------


def test_retarget_races_a_concurrent_action_and_never_desyncs_backend_from_announced(
    monkeypatch,
):
    """The real scenario named in the design doc: a model issues
    `retarget(mac-B)` and `screenshot()` in the same turn. Hammered across
    many iterations under real thread contention. The invariant: whenever
    a concurrent action observes `announced=True` paired with the NEW
    backend, that backend's OWN disclosure (`announce_raise`) must have
    ALREADY run - `retarget()` never installs a binding that is not yet
    disclosure-consistent with itself, because disclosure happens during
    BUILD, before the single reference swap (see `_Binding`'s docstring).
    """
    iterations = 30
    saw_new_backend_at_least_once = False

    for i in range(iterations):
        target = f"ssh://user@{_unique(f'race2-host-{i}')}"
        old_backend = _FakeLocalBackend(_unique(f"race2-old-{i}"))
        new_backend = _FakeRemoteBackend(
            "remote-ssh:linux-x11", user_host=target.replace("ssh://", "")
        )
        monkeypatch.setattr(cu, "select_backend", lambda _cfg, _b=new_backend: _b)

        tool = _mounted_tool(old_backend)
        tool._announced = True  # old target already disclosed

        results: dict = {}

        def _do_retarget(_tool=tool, _target=target, _results=results) -> None:
            _results["retarget"] = _tool.retarget(_target)

        def _do_action(_tool=tool, _results=results) -> None:
            try:
                _tool._ensure_announced()
                _results["action"] = (_tool._backend, _tool._announced)
            except cu.AnnouncementRefused as exc:  # pragma: no cover - defensive
                _results["action_refused"] = exc

        t1 = threading.Thread(target=_do_retarget)
        t2 = threading.Thread(target=_do_action)
        t1.start()
        t2.start()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)

        assert "retarget" in results, f"iteration {i}: retarget thread never finished"
        assert results["retarget"].success is True, results["retarget"].error

        backend_seen, announced_seen = results.get("action", (None, None))
        if backend_seen is new_backend:
            saw_new_backend_at_least_once = True
            assert announced_seen is True, (
                f"iteration {i}: action observed the NEW backend with "
                "announced=False - a reader should never see a not-yet-"
                "installed binding"
            )
            assert new_backend.disclose_calls > 0, (
                f"iteration {i}: action observed announced=True paired with "
                "the NEW backend BEFORE that backend's own disclosure ran - "
                "this is exactly the undisclosed-capture bug the council "
                "verified"
            )

    assert saw_new_backend_at_least_once, (
        "the concurrent action never once observed the NEW backend across "
        f"{iterations} iterations - widen the iteration count; this test "
        "proves nothing if the race window was never entered"
    )


# -- 2. No leak on failure between build and swap ----------------------------


def test_failure_between_build_and_swap_releases_new_backend_and_leaves_old_untouched(
    monkeypatch,
):
    """Injects a failure AFTER the new backend is connected and its guard
    is built, but before the commit point (the actual failure site is
    `_build_announcement`, standing in for "anything else that fails
    between BUILD and SWAP" per `retarget()`'s own comment). Proves: (a)
    the half-built new backend is released exactly once, (b) the OLD
    binding's backend is never closed, (c) `self._binding` still IS the
    old one afterward.
    """
    old_backend = _FakeLocalBackend(_unique("leak-old"))
    new_backend = _FakeRemoteBackend(
        "remote-ssh:linux-x11", user_host=_unique("leak-new-host")
    )
    target = f"ssh://user@{new_backend.user_host}"
    monkeypatch.setattr(cu, "select_backend", lambda _cfg: new_backend)

    def _boom(*_a, **_k):
        raise RuntimeError("simulated failure between guard and swap")

    monkeypatch.setattr(cu, "_build_announcement", _boom)

    tool = _mounted_tool(old_backend)
    tool._announced = True
    binding_before = tool._binding

    result = tool.retarget(target)

    assert result.success is False
    assert result.error["type"] == "RuntimeError"
    assert new_backend.close_calls == 1, (
        "the half-built new backend must be released exactly once on failure"
    )
    assert old_backend.close_calls == 0, (
        "a failed retarget must NEVER touch the old binding's backend"
    )
    assert tool._binding is binding_before, (
        "a failed retarget must leave self._binding EXACTLY as it was - no "
        "partial retarget"
    )
    assert tool._backend is old_backend
    assert tool._announced is True


# -- 3. The new target gets its own disclosure -------------------------------


def test_retarget_discloses_the_new_target_before_swapping_in():
    """The new target's OWN disclosure must run - never inherit the OLD
    target's `announced` state. Proven two ways: the new backend's real
    `announce_raise` was actually called, and the resulting binding's
    `announcement` handle names the NEW backend, not the old one.
    """
    old_backend = _FakeLocalBackend(_unique("disclose-old"))
    new_backend = _FakeRemoteBackend(
        "remote-ssh:linux-x11", user_host=_unique("disclose-new-host")
    )
    target = f"ssh://user@{new_backend.user_host}"

    def _fake_select_backend(_cfg):
        return new_backend

    import amplifier_module_tool_computer_use.registry as registry_mod

    orig_select = registry_mod.select_backend
    cu_module = cu

    def _select_backend(cfg):
        # Exercise the REAL select_backend/registry.select_backend contract
        # shape (still returns our fake instance) rather than bypassing it
        # entirely - only the SSH connect step itself is stubbed.
        del cfg
        return new_backend

    cu_module.select_backend = _select_backend
    try:
        tool = _mounted_tool(old_backend)
        tool._announced = True
        assert new_backend.disclose_calls == 0

        result = tool.retarget(target)

        assert result.success is True, result.error
        assert new_backend.disclose_calls == 1, (
            "retarget must disclose the NEW target for real, not reuse the "
            "OLD target's already-True announced flag"
        )
        assert tool._backend is new_backend
        assert tool._announced is True
        assert isinstance(tool._announcement, cu._RemoteAnnouncementHandle)
        assert tool._announcement.backend_name == new_backend.name
    finally:
        cu_module.select_backend = orig_select


# -- 4. A refusal blocks the retarget ----------------------------------------


def test_disclosure_declined_with_human_present_blocks_retarget(monkeypatch):
    """`coexistence.announce=False` + a human genuinely detected present on
    the NEW target must refuse the retarget outright (\u00a76.2 step 3,
    mount-time-shaped refusal) - the old binding must survive untouched,
    and the new backend must be released, not left connected.
    """
    old_backend = _FakeLocalBackend(_unique("refuse-old"))
    new_backend = _FakeRemoteBackend(
        "remote-ssh:linux-x11",
        user_host=_unique("refuse-new-host"),
        idle_ms=0.0,  # a human is actively using the new target right now
    )
    target = f"ssh://user@{new_backend.user_host}"
    monkeypatch.setattr(cu, "select_backend", lambda _cfg: new_backend)

    tool = _mounted_tool(old_backend, cfg={"coexistence": {"announce": False}})
    tool._announced = True
    binding_before = tool._binding

    result = tool.retarget(target)

    assert result.success is False
    assert result.error["type"] == "RetargetRefused"
    assert new_backend.disclose_calls == 0, "must refuse BEFORE ever disclosing"
    assert new_backend.close_calls == 1, "the refused new backend must be released"
    assert tool._binding is binding_before
    assert tool._backend is old_backend


# -- 5. Busy / same-target / no-guard refusals -------------------------------


def test_retarget_refuses_when_an_action_is_in_flight():
    old_backend = _FakeLocalBackend(_unique("busy-old"))
    tool = _mounted_tool(old_backend)
    tool._announced = True
    assert tool._band_state is not None
    tool._band_state.depth = 1  # simulate an in-flight action

    result = tool.retarget("local")

    assert result.success is False
    assert result.error["type"] == "RetargetRefused"
    assert tool._backend is old_backend


def test_retarget_refuses_when_input_is_held():
    old_backend = _FakeLocalBackend(_unique("held-old"))
    tool = _mounted_tool(old_backend)
    tool._announced = True
    assert tool._ledger is not None
    tool._ledger.hold("mouse", "mouse:left", lambda: None)

    result = tool.retarget("local")

    assert result.success is False
    assert result.error["type"] == "RetargetRefused"
    assert tool._backend is old_backend


def test_retarget_to_the_same_target_is_a_no_op_and_never_redisc_loses():
    """\u00a76.5: same target as current -> no-op, reported as such."""
    backend = _FakeRemoteBackend("remote-ssh:linux-x11", user_host=_unique("same-host"))
    target = f"ssh://user@{backend.user_host}"

    # Two DIFFERENT instances that resolve to the SAME channel identity
    # (same user_host) - `retarget()` must recognize this via
    # `_channel_identity`, not object identity.
    reconnect_backend = _FakeRemoteBackend(
        "remote-ssh:linux-x11", user_host=backend.user_host
    )

    import amplifier_module_tool_computer_use as cu_mod

    monkeypatch_target = reconnect_backend
    orig = cu_mod.select_backend
    cu_mod.select_backend = lambda _cfg, _b=monkeypatch_target: _b
    try:
        tool = _mounted_tool(backend)
        tool._announced = True
        binding_before = tool._binding

        result = tool.retarget(target)

        assert result.success is True
        assert "no-op" in result.output
        assert reconnect_backend.close_calls == 1, (
            "the extra probe connection opened to check identity must be released"
        )
        assert tool._binding is binding_before, (
            "a no-op retarget must not swap anything"
        )
        assert backend.disclose_calls == 0, "a no-op must never re-disclose"
    finally:
        cu_mod.select_backend = orig


def test_retarget_refuses_a_new_target_with_no_guard_by_default(monkeypatch):
    """\u00a76.5: a new target with no coexistence guard at all is refused by
    default - a mid-session downgrade of an already-protected session."""

    class _NoPresenceBackend:
        is_remote = True

        def __init__(self, name, user_host):
            self.name = name
            self.user_host = user_host
            self.handshake = {"ops": []}
            self.close_calls = 0

        def type_text(self, text):
            pass

        def screen_geometry(self) -> ScreenGeometry:
            return ScreenGeometry(1920, 1080, 0, 0)

        def list_monitors(self):
            raise BackendError("no monitor enumeration on this fake")

        def close(self):
            self.close_calls += 1

    old_backend = _FakeLocalBackend(_unique("noguard-old"))
    new_backend = _NoPresenceBackend(
        "remote-ssh:mystery-platform", user_host=_unique("noguard-host")
    )
    target = f"ssh://user@{new_backend.user_host}"
    monkeypatch.setattr(cu, "select_backend", lambda _cfg: new_backend)

    tool = _mounted_tool(old_backend)
    tool._announced = True

    result = tool.retarget(target)

    assert result.success is False
    assert result.error["type"] == "RetargetRefused"
    assert new_backend.close_calls == 1
    assert tool._backend is old_backend

    # Overridable, explicitly and logged - same shape as `drive_anyway`.
    tool2 = _mounted_tool(
        old_backend, cfg={"coexistence": {"retarget_allow_no_guard": True}}
    )
    tool2._announced = True
    new_backend2 = _NoPresenceBackend(
        "remote-ssh:mystery-platform", user_host=_unique("noguard-host2")
    )
    target2 = f"ssh://user@{new_backend2.user_host}"
    monkeypatch.setattr(cu, "select_backend", lambda _cfg: new_backend2)
    result2 = tool2.retarget(target2)
    assert result2.success is True, result2.error
    assert tool2._backend is new_backend2
    assert tool2._coexistence_guard is None


# -- 6. The policy-bypass race (a six-lens council's worse finding) ---------
#
# The atomic `_Binding` swap above (tests 1a/1b) closes the DISCLOSURE race
# the review originally verified. A later review reproduced a DIFFERENT,
# worse race against unmodified production code: a MUTATING action's
# `read_only` check and its actual backend dispatch used to read
# `self._binding` SEPARATELY (`self._read_only` in `_execute_calls`, then
# `self._backend` inside `_run`, moments later) - a concurrent `retarget()`
# swap could land in between, so a write checked under an OLD, PERMISSIVE
# binding could be dispatched against a NEW, RESTRICTIVE one instead. The
# fix: `_execute_calls`/`DesktopTool._execute_action` now read
# `self._binding` ONCE, before the check, and pass that SAME snapshot into
# `_run` for the dispatch (see `_run`'s own docstring).
#
# This section deterministically controls the interleaving of real
# `retarget()` and `execute()` calls. Only inert backend input and worker
# scheduling are wrapped; production policy checks and locking run unchanged.


def _execute_across_retarget(
    monkeypatch, computer, executor, request, target, backend_method, *, dispatch_first
):
    """Hold either inert input or the queued worker at an explicit barrier.

    Keep real execute/retarget, policy checks and locking intact. A dispatch
    already injecting holds the serialization lock, so retarget must refuse
    until it finishes. A queued dispatch must reject its stale binding after
    retarget wins. No scheduler timing decides which path this test exercises.
    """
    old_binding = computer._binding
    reached = threading.Event()
    release = threading.Event()
    results: dict = {}

    if dispatch_first:
        original = getattr(old_binding.backend, backend_method)

        def held_dispatch(*args, **kwargs):
            result = original(*args, **kwargs)
            reached.set()
            assert release.wait(timeout=5.0), "input barrier was not released"
            return result

        monkeypatch.setattr(old_binding.backend, backend_method, held_dispatch)
    else:
        original = computer._run_flat_step

        def queued_dispatch(action, params, binding, approved):
            assert binding is old_binding, "worker must have pinned the OLD binding"
            reached.set()
            assert release.wait(timeout=5.0), "queued worker barrier was not released"
            return original(action, params, binding, approved)

        monkeypatch.setattr(computer, "_run_flat_step", queued_dispatch)

    def execute():
        try:
            results["execute"] = asyncio.run(executor.execute(request))
        except BaseException as exc:
            results["exception"] = exc

    worker = threading.Thread(target=execute)
    worker.start()
    try:
        assert reached.wait(timeout=5.0), "execute never reached its barrier"
        retarget = computer.retarget(target)
        if dispatch_first:
            assert retarget.success is False
            assert retarget.error["type"] == "RetargetRefused"
            assert computer._binding is old_binding
        else:
            assert retarget.success is True, retarget.error
    finally:
        release.set()
        worker.join(timeout=5.0)

    assert not worker.is_alive(), "execute thread did not settle"
    assert "exception" not in results, results.get("exception")
    result = results["execute"]
    if dispatch_first:
        assert result.success is True, result.error
        retarget = computer.retarget(target)
        assert retarget.success is True, retarget.error
    else:
        assert result.success is False
        assert result.error["type"] == "BackendError"
        assert "target or policy changed" in result.error["message"]
    return result


def test_read_only_check_and_dispatch_never_straddle_a_retarget_swap(monkeypatch):
    """Both orders: OLD allowed input succeeds; NEW restrictive input never runs."""
    for dispatch_first in (True, False):
        old_backend = _FakeLocalBackend(_unique("bypass-old"))
        new_backend = _FakeRemoteBackend(
            "remote-ssh:linux-x11", user_host=_unique("bypass-new-host")
        )
        target = f"ssh://user@{new_backend.user_host}"
        monkeypatch.setattr(cu, "select_backend", lambda _cfg, _b=new_backend: _b)

        # No explicit policy: local defaults permissive, remote restrictive.
        tool = _mounted_tool(old_backend, cfg={})
        tool._announced = True
        assert tool._binding.read_only is False
        request = {"action": "left_click", "coordinate": [15, 30]}
        expected_xy = tool._binding.display.to_screen(15, 30)
        _execute_across_retarget(
            monkeypatch,
            tool,
            tool,
            request,
            target,
            "click",
            dispatch_first=dispatch_first,
        )

        assert old_backend.click_calls == (
            [(*expected_xy, "left", 1)] if dispatch_first else []
        )
        assert tool._binding.backend is new_backend
        assert tool._binding.read_only is True
        # A fresh request must also respect NEW policy, not reuse OLD permission.
        blocked = asyncio.run(tool.execute(request))
        assert blocked.success is False
        assert "read_only" in blocked.error["message"]
        assert new_backend.click_calls == []


def test_desktop_tool_gate_writes_check_and_dispatch_never_straddle_a_retarget_swap(
    monkeypatch,
):
    """Exercise both deterministic orders through DesktopTool's focus path."""
    import amplifier_module_tool_computer_use.backend as backend_mod

    class _FocusableLocalBackend(_FakeLocalBackend):
        def __init__(self, name: str) -> None:
            super().__init__(name)
            self.focus_calls: list[str] = []

        def list_windows(self):
            return backend_mod.WindowList(windows=[], foreground=None)

        def focus_window(self, handle: str) -> None:
            self.focus_calls.append(handle)

    class _FocusableRemoteBackend(_FakeRemoteBackend):
        def __init__(self, name: str, user_host: str) -> None:
            super().__init__(name, user_host)
            self.focus_calls: list[str] = []

        def list_windows(self):
            return backend_mod.WindowList(windows=[], foreground=None)

        def focus_window(self, handle: str) -> None:
            self.focus_calls.append(handle)

    for dispatch_first in (True, False):
        old_backend = _FocusableLocalBackend(_unique("desktop-bypass-old"))
        new_backend = _FocusableRemoteBackend(
            "remote-ssh:linux-x11", user_host=_unique("desktop-bypass-new")
        )
        target = f"ssh://user@{new_backend.user_host}"
        monkeypatch.setattr(cu, "select_backend", lambda _cfg, _b=new_backend: _b)

        computer = _mounted_tool(old_backend, cfg={})
        computer._announced = True
        desktop = cu.DesktopTool(computer)
        assert computer._binding.read_only is False
        request = {"action": "focus_window", "handle": "42"}
        _execute_across_retarget(
            monkeypatch,
            computer,
            desktop,
            request,
            target,
            "focus_window",
            dispatch_first=dispatch_first,
        )

        assert old_backend.focus_calls == (["42"] if dispatch_first else [])
        assert computer._binding.backend is new_backend
        assert computer._binding.read_only is True
        blocked = asyncio.run(desktop.execute(request))
        assert blocked.success is False
        assert "read_only" in blocked.error["message"]
        assert new_backend.focus_calls == [], (
            "focus_window must never reach the NEW restrictive backend"
        )


# -- 5. Council re-review Finding 1: the guard-support flag goes stale ------


def test_type_text_guard_support_flag_refreshes_across_retarget(monkeypatch):
    """Council re-review Finding 1: `_backend_type_text_supports_guard` used
    to be computed ONCE in `ComputerTool.__init__`, from the ORIGINAL
    backend's `type_text` signature, and was never refreshed on retarget.

    Repro: mount on `_FakeLocalBackend` (no explicit guarded-typing contract),
    then retarget onto `_FakeGuardCapableBackend` (`type_text_guarded=True`)
    which gets its own real, actively-disclosed coexistence guard. Before the fix,
    the stale `False` flag makes `type`'s `guard_active` resolve `False` for
    the NEW backend too - `check_start_permission()`/`bind_target()`/pacing
    are silently skipped, `type_text` is called with no guard at all, and
    nothing raises or logs. This test fails (RED) against the pre-fix code
    and passes (GREEN) once the flag is derived from the CURRENT binding's
    backend at every `type` call, not cached from the backend `__init__` saw.
    """
    old_backend = _FakeLocalBackend(_unique("guardflag-old"))
    assert getattr(old_backend, "type_text_guarded", False) is False
    tool = _mounted_tool(old_backend)
    assert tool._binding.coexistence_guard is not None, (
        "setup invalid: the OLD backend must get a real guard, or this "
        "repro proves nothing about a guard being silently skipped"
    )

    new_backend = _FakeGuardCapableBackend(_unique("guardflag-new"))
    assert new_backend.type_text_guarded is True
    target = f"ssh://user@{_unique('guardflag-host')}"
    monkeypatch.setattr(cu, "select_backend", lambda _cfg, _b=new_backend: _b)

    result = tool.retarget(target)

    assert result.success is True, result.error
    assert tool._binding.backend is new_backend
    assert tool._binding.coexistence_guard is not None, (
        "setup invalid: the NEW backend must also get a real guard - this "
        "is exactly the scenario where a stale flag causes a SILENT skip, "
        "not a loud, easily-noticed one"
    )

    pinned_binding = tool._binding
    new_guard = pinned_binding.coexistence_guard
    assert new_guard.presence._last_inject_monotonic is None
    tool._run("type", {"text": "hi"}, binding=pinned_binding)

    assert new_backend.type_text_calls, (
        "type_text was never called on the new backend - repro setup broken"
    )
    for _text, guard in new_backend.type_text_calls:
        assert guard is new_guard, (
            "SILENT SKIP CONFIRMED: type_text was called with guard=None "
            "against a backend that declares type_text_guarded=True and HAS an active "
            "coexistence guard - check_start_permission()/bind_target()/"
            "pacing were skipped with zero exception and zero log line. "
            "The guard-support flag is stale from the ORIGINAL backend "
            "(_FakeLocalBackend, no capability declaration), not refreshed for the "
            "NEW backend this session now actually drives."
        )
    assert new_backend.type_events == [
        ("before", "h"),
        ("type", "h"),
        ("after", "h"),
        ("before", "i"),
        ("type", "i"),
        ("after", "i"),
    ]
    assert new_guard.presence._last_inject_monotonic is not None
    assert old_backend.close_calls == 1
