"""Offline operator-policy checks, not Windows/device/physical-author qualification."""

from __future__ import annotations

import asyncio
import sys
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import amplifier_module_tool_computer_use as cu
from amplifier_module_tool_computer_use.coexistence_guard import (
    CoexistenceGuard,
    ExcludedCoordinateError,
    HaltedError,
    OperatorPresenceOverride,
)
from amplifier_module_tool_computer_use.exclusion import Rect
from amplifier_module_tool_computer_use.guard_diagnostics import read_cache
from amplifier_module_tool_computer_use.pause import PausedError
from amplifier_module_tool_computer_use.presence import PresenceMonitor, PresenceState
from amplifier_module_tool_computer_use.target_binding import TargetChangedError
from amplifier_module_tool_computer_use.target_clock import (
    CURRENT_TARGET_CLOCK,
    TargetClockExchange,
    TargetClockFailure,
    target_clock_exchange,
    without_target_clock,
)

CHANNEL = "remote-ssh:windows-wsl2:operator@synthetic-wsl"
EPOCH = "synthetic-boot/session"


def observation(now=10000, last=9999, foreground="window-a"):
    return {
        "version": 1,
        "epoch": EPOCH,
        "now_ms": now,
        "last_input_ms": last,
        "foreground": foreground,
    }


def receipt(before=10010, *, last=10060):
    return {
        "ok": True,
        "target_clock": {
            "observation": observation(before + 100, last),
            "injection": {
                "epoch": EPOCH,
                "before_ms": before,
                "after_ms": before + 1,
                "count": 2,
            },
        },
    }


@pytest.fixture
def rig():
    state = SimpleNamespace(
        wall=time.time(),
        mono=100.0,
        channel=CHANNEL,
        target="window-a",
        observation=observation(),
        sample_hook=lambda: None,
    )
    config = {
        "channel": CHANNEL,
        "expires_at": state.wall + 600,
        "reason": "approved unattended trial",
    }
    policy = OperatorPresenceOverride(
        **config,
        channel_source=lambda: state.channel,
        config_source=lambda: config,
        wall_clock=lambda: state.wall,
        monotonic_clock=lambda: state.mono,
    )
    releases, samples = [], []

    def read():
        state.sample_hook()
        return deepcopy(state.observation)

    monitor = PresenceMonitor(
        lambda: pytest.fail("no scalar fallback"),
        platform="windows-wsl2",
        target_clock_source=read,
    )
    guard = CoexistenceGuard(
        monitor,
        release_all=lambda reason: (
            releases.append((reason, CURRENT_TARGET_CLOCK.get())) or []
        ),
        operator_presence_override=policy,
        target_source=lambda: state.target,
        on_presence_sample=samples.append,
    )
    guard.bind_target()
    return SimpleNamespace(
        state=state,
        config=config,
        policy=policy,
        guard=guard,
        monitor=monitor,
        releases=releases,
        samples=samples,
    )


def test_live_initial_sample_and_post_receipt_classification_preserved(rig, caplog):
    guard = rig.guard
    guard.before_event()
    assert rig.samples[-1].state is PresenceState.HUMAN_ACTIVE
    guard.check_start_permission()
    assert not guard.halted
    assert any(
        "OPERATOR PRESENCE OVERRIDE" in r.message for r in caplog.get_records("setup")
    )
    with guard.target_clock_event():
        exchange = CURRENT_TARGET_CLOCK.get()
        initial = dict(exchange.condition)
        assert initial["version"] == 2
        assert initial["operator_presence_override"] is True
        assert initial["operator_presence_channel"] == CHANNEL
        assert initial["operator_presence_target"] == "window-a"
        assert initial["inject_before_ms"] is None
        exchange.finish(receipt(), require_injection=True)
        assert rig.samples[-1].state is PresenceState.HUMAN_ACTIVE
        assert exchange.condition["version"] == 2
        assert (
            exchange.condition["operator_presence_expires_at"] == rig.policy.expires_at
        )
        assert exchange.condition["inject_before_ms"] == 10010  # not foreign last=10060
        exchange.finish(receipt(10120, last=10180), require_injection=True)
        assert exchange.prepare()["operator_presence_override"] is True
        assert exchange.condition["inject_before_ms"] == 10120
    assert not guard.halted and not rig.releases
    assert guard.as_dict()["operator_presence_override"] == {
        **rig.config,
        "active": True,
        "authority_verified": False,
    }
    cache = read_cache(guard._diagnostics)
    assert cache["accepted_receipts"] == 2 and not cache["fault"]
    assert cache["records"][-1]["presence_state"] == "human_active"
    assert cache["records"][-1]["operator_presence_override_active"] is True
    assert CHANNEL not in str(cache)  # cached diagnostics remain payload/identity-free


@pytest.mark.parametrize(
    "failure", ["missing", "count", "epoch", "regression", "rejected", "lost"]
)
def test_override_never_suppresses_safety_evidence_halts(rig, failure):
    rig.guard.before_event()
    result = receipt()
    if failure == "missing":
        result.pop("target_clock")
    elif failure == "count":
        result["target_clock"]["injection"]["count"] = 0
    elif failure == "epoch":
        result["target_clock"]["observation"]["epoch"] = "wrong-boot"
    elif failure == "regression":
        result["target_clock"]["observation"]["now_ms"] = 1
    elif failure == "rejected":
        result["ok"] = False
    with pytest.raises(HaltedError) as error, rig.guard.target_clock_event():
        if failure != "lost":
            CURRENT_TARGET_CLOCK.get().finish(result, require_injection=True)
    assert error.value.snapshot.state is PresenceState.UNKNOWN
    assert rig.guard.halted
    assert rig.releases and all(ctx is None for _, ctx in rig.releases)
    with pytest.raises(HaltedError):
        rig.guard.before_event()


@pytest.mark.parametrize(
    "seam", ["before", "after-sample", "condition", "prepare", "receipt"]
)
@pytest.mark.parametrize(
    "cause",
    ["wall-expiry", "monotonic-expiry", "revoke", "change-channel", "malformed"],
)
def test_expiry_or_revocation_fails_closed_at_each_boundary(rig, seam, cause):
    guard = rig.guard
    guard.before_event()

    def invalidate():
        if cause == "wall-expiry":
            rig.state.wall = rig.policy.expires_at
        elif cause == "monotonic-expiry":
            rig.state.wall -= 100  # rollback must not extend the original lifetime
            rig.state.mono += 601
        elif cause == "revoke":
            rig.config.clear()
        elif cause == "change-channel":
            rig.state.channel += "-different"
        else:
            rig.config["expires_at"] = True

    with pytest.raises(HaltedError) as error:
        if seam in {"prepare", "receipt"}:
            with guard.target_clock_event():
                exchange = CURRENT_TARGET_CLOCK.get()
                invalidate()
                if seam == "prepare":
                    exchange.prepare()  # used by BOTH real forwarding backends
                else:
                    exchange.finish(receipt(), require_injection=True)
        else:
            if seam == "after-sample":
                rig.state.sample_hook = invalidate
            else:
                invalidate()
            if seam == "condition":
                guard.target_condition()
            else:
                guard.before_event()
    assert error.value.snapshot.state is PresenceState.UNKNOWN
    assert guard.halted and rig.releases
    assert all(ctx is None for _, ctx in rig.releases)


@pytest.mark.parametrize("control", ["pause", "cancel", "target", "excluded"])
def test_independent_controls_still_abort_and_release(rig, control):
    guard = rig.guard
    guard.before_event()
    if control == "pause":
        guard.pause.set("overlay_click", reason="pause")
        expected = PausedError
    elif control == "cancel":
        guard.durable_halt_poll = lambda: rig.monitor.last_snapshot
        expected = HaltedError
    elif control == "target":
        rig.state.target = "window-b"
        expected = TargetChangedError
    else:
        guard.exclusion.register("Pause", Rect(1, 1, 20, 20))
        expected = ExcludedCoordinateError
    with pytest.raises(expected):
        guard.before_event(coord=(5, 5))
    assert rig.releases


@pytest.mark.parametrize("seed", ["already-latched", "durable"])
def test_override_cannot_unlatch_or_ignore_persisted_halt(rig, seed):
    rig.monitor.sample()
    snapshot = rig.monitor.last_snapshot
    if seed == "already-latched":
        rig.guard.seed_halted(snapshot)
    else:
        rig.guard.durable_halt_poll = lambda: snapshot
    for check in (
        rig.guard.before_event,
        rig.guard.check_start_permission,
        rig.guard.target_condition,
    ):
        with pytest.raises(HaltedError) as error:
            check()
        assert error.value.snapshot is snapshot
    assert rig.guard.halted


@pytest.fixture
def mount_backend(monkeypatch):
    monkeypatch.setattr(cu, "load_halt", lambda key: None)
    monkeypatch.setattr(cu, "_durable_halt_poll_for", lambda backend: None)
    monkeypatch.setattr(
        cu,
        "_get_channel_ledger",
        lambda key: SimpleNamespace(release_all=lambda **kw: []),
    )
    return SimpleNamespace(
        name="remote-ssh:windows-wsl2",
        is_remote=True,
        user_host="operator@synthetic-wsl",
        presence_platform="windows-wsl2",
        presence_idle_ms=lambda: 1,
        presence_observation=lambda: observation(),
        current_target=lambda: "window-a",
    )


def valid_config():
    return {
        "channel": CHANNEL,
        "expires_at": time.time() + 600,
        "reason": "approved bounded trial",
    }


@pytest.mark.parametrize(
    "raw",
    [
        None,
        True,
        False,
        [],
        "yes",
        {},
        {**valid_config(), "extra": True},
        {**valid_config(), "channel": CHANNEL + "other"},
        {**valid_config(), "channel": True},
        {**valid_config(), "expires_at": True},
        {**valid_config(), "expires_at": "2000000000"},
        {**valid_config(), "expires_at": float("nan")},
        {**valid_config(), "expires_at": float("inf")},
        {**valid_config(), "expires_at": 10**1000},
        {**valid_config(), "expires_at": time.time() - 1},
        {**valid_config(), "expires_at": time.time() + 3700},
        {**valid_config(), "reason": ""},
        {**valid_config(), "reason": " " * 1000 + "x"},
        {**valid_config(), "reason": "untrusted\nline"},
    ],
)
def test_mount_rejects_malformed_or_wrong_channel_config(mount_backend, raw):
    with pytest.raises(ValueError):
        cu._build_coexistence_guard(
            mount_backend, {"coexistence": {"operator_presence_override": raw}}
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"is_remote": False},
        {"is_remote": 1},
        {"presence_platform": "linux-x11"},
        {"name": "windows-wsl2"},
        {"user_host": "another-host"},
        {"presence_idle_ms": None},
    ],
)
def test_override_rejects_unsupported_backend_or_unbound_target(mount_backend, changes):
    for key, value in changes.items():
        setattr(mount_backend, key, value)
    with pytest.raises(ValueError):
        cu._build_coexistence_guard(
            mount_backend,
            {"coexistence": {"operator_presence_override": valid_config()}},
        )


def test_real_builder_config_is_revocable_and_default_still_halts(mount_backend):
    # The REAL RemoteBackend has no current_target method. Policy binding must
    # use the existing fresh clock observation, not an invented RPC/callback.
    del mount_backend.current_target
    default = cu._build_coexistence_guard(mount_backend, {})
    with pytest.raises(HaltedError):
        default.before_event()
    config = {"coexistence": {"operator_presence_override": valid_config()}}
    guard = cu._build_coexistence_guard(mount_backend, config)
    guard.before_event()
    assert guard.presence.state is PresenceState.HUMAN_ACTIVE and not guard.halted
    config["coexistence"].pop("operator_presence_override")
    with pytest.raises(HaltedError):
        guard.before_event()


def test_real_builder_honors_existing_durable_halt_with_valid_override(
    mount_backend, monkeypatch
):
    monitor = PresenceMonitor(
        lambda: 1,
        platform="windows-wsl2",
        target_clock_source=lambda: observation(),
    )
    snapshot = monitor.sample()
    persisted = SimpleNamespace(
        to_snapshot=lambda: snapshot, reason="synthetic prior halt"
    )
    monkeypatch.setattr(cu, "load_halt", lambda key: persisted)
    guard = cu._build_coexistence_guard(
        mount_backend,
        {"coexistence": {"operator_presence_override": valid_config()}},
    )
    assert guard.halted
    with pytest.raises(HaltedError) as error:
        guard.before_event()
    assert error.value.snapshot is snapshot


@pytest.mark.parametrize("condition", [None, True, False, [], "override"])
def test_non_object_condition_cannot_select_an_override(condition):
    with pytest.raises(ValueError):
        TargetClockExchange(condition)


@pytest.mark.parametrize("seam", ["sample", "receipt"])
@pytest.mark.parametrize("foreground", [None, "0", "window-b"])
def test_missing_or_changed_clock_foreground_cannot_continue(
    mount_backend, seam, foreground
):
    state = SimpleNamespace(value=observation())
    mount_backend.presence_observation = lambda: state.value
    guard = cu._build_coexistence_guard(
        mount_backend, {"coexistence": {"operator_presence_override": valid_config()}}
    )
    guard.bind_target()
    guard.before_event()
    if seam == "sample":
        state.value = observation(10110, 10100, foreground)
        check = guard.before_event
    else:
        result = receipt()
        result["target_clock"]["observation"]["foreground"] = foreground
        check = lambda: guard.accept_target_result(result, require_injection=True)
    with pytest.raises((HaltedError, TargetChangedError)):
        check()


@pytest.mark.parametrize(
    "mutation", ["v1-flag", "bool-version", "false-flag", "bad-channel", "bad-expiry"]
)
def test_forwarded_condition_refuses_invalid_override_without_os_effects(rig, mutation):
    rig.guard.before_event()
    condition = rig.guard.target_condition()
    if mutation == "v1-flag":
        condition["version"] = 1
    elif mutation == "bool-version":
        condition["version"] = True
    elif mutation == "false-flag":
        condition["operator_presence_override"] = False
    elif mutation == "bad-channel":
        condition["operator_presence_channel"] = "local"
    else:
        condition["operator_presence_expires_at"] = True
    with pytest.raises(ValueError):
        TargetClockExchange(condition)


def test_focus_scope_is_one_shot_nested_and_restores_cleanup_context(rig):
    rig.guard.before_event()
    original = rig.guard.target_condition()
    with rig.guard.target_clock_event(focus_target="window-b"):
        focus = CURRENT_TARGET_CLOCK.get()
        assert focus.prepare()["focus_target"] == "window-b"
        assert focus.condition["operator_presence_target"] == "window-a"
        with target_clock_exchange(original) as nested:
            assert nested.focus_target is None
            assert "focus_target" not in nested.prepare()
        assert CURRENT_TARGET_CLOCK.get() is focus
        with without_target_clock():
            assert CURRENT_TARGET_CLOCK.get() is None
        result = receipt()
        result["focused"] = "window-b"
        result["target_clock"]["observation"]["foreground"] = "window-b"
        focus.finish(result, require_injection=True)
        assert focus.focus_target is None
        assert focus.focus_check is None
        assert focus.prepare()["operator_presence_target"] == "window-b"
        assert focus.condition["version"] == 2 and "focus_target" not in focus.condition
    assert CURRENT_TARGET_CLOCK.get() is None


def test_forwarding_focus_scope_is_concurrent_and_cannot_authorize_a_second_call(rig):
    rig.guard.before_event()
    condition = rig.guard.target_condition()

    async def scenario():
        ready = [asyncio.Event(), asyncio.Event()]

        async def invoke(index):
            destination = f"window-{index}"
            with target_clock_exchange(
                {**condition, "version": 3, "focus_target": destination}
            ) as exchange:
                ready[index].set()
                await ready[1 - index].wait()
                assert CURRENT_TARGET_CLOCK.get() is exchange
                assert exchange.prepare()["focus_target"] == destination
                exchange.finish({"ok": True}, require_injection=True)
                assert exchange.focus_target is None
                with pytest.raises(TargetClockFailure):
                    exchange.prepare()
            assert CURRENT_TARGET_CLOCK.get() is None

        await asyncio.gather(invoke(0), invoke(1))

    asyncio.run(scenario())
    assert CURRENT_TARGET_CLOCK.get() is None


@pytest.mark.parametrize("destination", [None, "", "0", True, 10, "x" * 257])
def test_v3_focus_condition_requires_exact_nonzero_destination(rig, destination):
    rig.guard.before_event()
    condition = {
        **rig.guard.target_condition(),
        "version": 3,
        "focus_target": destination,
    }
    with pytest.raises(ValueError):
        TargetClockExchange(condition)


def test_focus_field_cannot_be_silently_ignored_in_older_conditions(rig):
    rig.guard.before_event()
    condition = rig.guard.target_condition()
    with pytest.raises(ValueError):
        TargetClockExchange({**condition, "focus_target": "window-b"})
    with pytest.raises(ValueError):
        TargetClockExchange(
            {**condition, "version": 3, "focus_target": "window-b"},
            focus_target="window-c",
        )


def test_default_focus_does_not_acquire_operator_transition_authority(rig):
    # This is the normal, default Windows guard, not an override. Keep its v1
    # condition and existing classifier; passing an explicit handle adds no grant.
    rig.guard.operator_presence_override = None
    rig.state.observation = observation(10000, 1000)
    rig.guard.before_event()
    with rig.guard.target_clock_event(focus_target="window-b"):
        exchange = CURRENT_TARGET_CLOCK.get()
        assert exchange.focus_target is None
        assert exchange.prepare()["version"] == 1
        result = receipt(last=10010)
        exchange.finish(result, require_injection=True)
        assert exchange.prepare()["version"] == 1
    assert CURRENT_TARGET_CLOCK.get() is None
