"""Pure target-clock/guard contracts; no backend, OS input, transport or display.

All clock observations and SendInput receipts below are synthetic. These tests
exercise the real parser, PresenceMonitor and CoexistenceGuard. They establish
controller behavior, not Windows hardware timing or live-event attribution.
"""

from __future__ import annotations

import asyncio
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

from amplifier_module_tool_computer_use import presence
from amplifier_module_tool_computer_use.coexistence_guard import (
    CoexistenceGuard,
    HaltedError,
)
from amplifier_module_tool_computer_use.target_clock import (
    CURRENT_TARGET_CLOCK,
    TargetObservation,
    target_clock_exchange,
    without_target_clock,
)

EPOCH = "synthetic-boot-a/session-a"
DELETE = object()


def observation(now=10000, last=1000, *, epoch=EPOCH):
    return {"version": 1, "epoch": epoch, "now_ms": now, "last_input_ms": last}


def receipt(before=10010, *, now=10012, last=None, epoch=EPOCH):
    return {
        "ok": True,
        "target_clock": {
            "observation": observation(
                now, before if last is None else last, epoch=epoch
            ),
            "injection": {
                "epoch": epoch,
                "before_ms": before,
                "after_ms": before + 1,
                "count": 2,
            },
        },
    }


def changed(raw, path, value):
    """Corrupt one independent wire field, keeping other fields valid."""
    result = deepcopy(raw)
    parent = result
    for key in path[:-1]:
        parent = parent[key]
    if value is DELETE:
        del parent[path[-1]]
    else:
        parent[path[-1]] = value
    return result


@pytest.fixture
def make_rig(monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(presence, "time", SimpleNamespace(monotonic=lambda: clock.now))

    def make(
        *, epoch=EPOCH, now=10000, last=1000, sample_tail=0.0, controller_offset=0.0
    ):
        clock.now = 1000.0 + controller_offset
        source = SimpleNamespace(
            value=observation(now, last, epoch=epoch), reads=0, tail=sample_tail
        )
        releases = []

        def read_target():
            source.reads += 1
            clock.now += source.tail
            return deepcopy(source.value)

        def forbidden_scalar_read():
            pytest.fail("a configured target clock must not fall back to scalar idle")

        def release(reason):
            releases.append((reason, CURRENT_TARGET_CLOCK.get()))
            return []

        monitor = presence.PresenceMonitor(
            forbidden_scalar_read,
            platform="windows-wsl2",
            target_clock_source=read_target,
        )
        guard = CoexistenceGuard(monitor, release_all=release)
        guard.before_event()
        assert monitor.last_snapshot.state is presence.PresenceState.QUIET
        return SimpleNamespace(
            clock=clock,
            source=source,
            monitor=monitor,
            guard=guard,
            releases=releases,
        )

    return make


def accept(rig, result, *, require_injection=True):
    """Use the same invocation-local exchange the real guarded callers use."""
    with rig.guard.target_clock_event():
        exchange = CURRENT_TARGET_CLOCK.get()
        assert exchange is not None and exchange.guard is rig.guard
        exchange.finish(result, require_injection=require_injection)


def test_recorded_31ms_send_interval_does_not_credit_its_completion(make_rig):
    """Keep the real recurrence's conservative refusal, not an after-time fix."""
    rig = make_rig(now=37738250, last=37631281)
    raw = receipt(before=37738890, now=37738968, last=37738921)
    raw["target_clock"]["injection"]["after_ms"] = 37738921
    raw["target_clock"]["injection"]["count"] = 1
    with pytest.raises(HaltedError) as error:
        accept(rig, raw)
    assert error.value.snapshot.margin_ms == 31
    assert rig.guard.halted
    assert rig.monitor.target_condition()["inject_before_ms"] == 37738890


def assert_sticky_unknown(rig, error, *, baseline=10010):
    snapshot = error.value.snapshot
    assert snapshot.state is presence.PresenceState.UNKNOWN
    assert snapshot.confidence is presence.Confidence.LOW
    assert snapshot.margin_ms is None
    assert rig.guard.halted
    assert rig.monitor.target_condition()["inject_before_ms"] == baseline
    assert rig.releases and all(reason == "halted" for reason, _ in rig.releases)
    assert all(context is None for _, context in rig.releases)

    # A genuinely quiet observation, well beyond latch decay, is not a reset.
    rig.clock.now += presence.LATCH_DECAY_SECONDS + 120
    rig.source.value = observation(200000, baseline or 1000)
    assert rig.monitor.sample().state is presence.PresenceState.QUIET
    with pytest.raises(HaltedError) as later:
        rig.guard.before_event()
    assert later.value.snapshot is snapshot
    assert rig.guard.halted


@pytest.mark.parametrize("controller_offset", [-1000000.0, 0.0, 1000000000.0])
@pytest.mark.parametrize(
    ("write_tail", "sample_tail"),
    [(0.0, 0.0), (0.010, 0.046518420995), (0.200, 0.005), (0.001, 10.0)],
    ids=["no-tail", "longer-presence-tail", "longer-write-tail", "ten-second-read"],
)
@pytest.mark.parametrize("margin", [0, 16, 20, 21, 60])
@pytest.mark.parametrize("detection_seam", ["receipt", "next-sample"])
def test_target_margin_ignores_controller_offset_and_response_tails(
    make_rig, controller_offset, write_tail, sample_tail, margin, detection_seam
):
    rig = make_rig(sample_tail=sample_tail, controller_offset=controller_offset)
    rig.clock.now += write_tail
    result = receipt(now=10100, last=10010 + margin)

    if detection_seam == "next-sample":
        accept(rig, receipt())
        rig.guard.after_event()
        rig.source.value = result["target_clock"]["observation"]
        check = rig.guard.before_event
    else:
        check = lambda: accept(rig, result)

    if margin > 20:
        with pytest.raises(HaltedError) as error:
            check()
        assert error.value.snapshot.state is presence.PresenceState.HUMAN_ACTIVE
        assert error.value.snapshot.margin_ms == margin
        assert rig.guard.halted and rig.releases
    else:
        check()
        assert not rig.guard.halted and not rig.releases
        assert rig.monitor.last_snapshot.state is presence.PresenceState.QUIET
    assert rig.monitor.last_snapshot.margin_ms == margin
    assert rig.monitor.guard_ms == 20.0
    assert rig.monitor.target_condition()["inject_before_ms"] == 10010
    assert rig.monitor._last_inject_monotonic is None
    if detection_seam == "next-sample":
        snapshot = rig.monitor.last_snapshot
        assert snapshot.transport_latency_ms == pytest.approx(
            sample_tail * 1000, abs=0.001
        )
        assert snapshot.effective_staleness_ms == pytest.approx(
            20 + sample_tail * 1000, abs=0.001
        )


@pytest.mark.parametrize("at", [-1000000.0, 0.0, 1000000000.0, None])
def test_generic_record_inject_cannot_create_or_overwrite_target_baseline(make_rig, at):
    rig = make_rig()
    rig.monitor.record_inject(at=at)
    assert rig.monitor.target_condition()["inject_before_ms"] is None
    accept(rig, receipt())
    expected = rig.monitor.target_condition()
    rig.monitor.record_inject(at=at)
    rig.guard.after_event()
    assert rig.monitor.target_condition() == expected
    assert rig.monitor._last_inject_monotonic is None
    rig.source.value = observation(10100, 10050)
    with pytest.raises(HaltedError) as error:
        rig.guard.before_event()
    assert error.value.snapshot.margin_ms == 40


# Invalid JSON shapes and exact-integer violations must fail closed, rather
# than credit an emission using a coercion or a controller-clock approximation.
INVALID_RESULTS = [
    pytest.param((), None, id="missing-result"),
    pytest.param((), [], id="non-object-result"),
    pytest.param(("target_clock",), DELETE, id="missing-clock"),
    pytest.param(("target_clock",), None, id="null-clock"),
    pytest.param(("target_clock", "observation"), DELETE, id="missing-observation"),
    pytest.param(("target_clock", "observation"), {}, id="empty-observation"),
    pytest.param(("target_clock", "injection"), DELETE, id="missing-injection"),
    pytest.param(("target_clock", "injection"), None, id="unacknowledged-input"),
    pytest.param(("target_clock", "injection"), [], id="non-object-injection"),
    pytest.param(("target_clock", "injection", "count"), 0, id="zero-sendinput-count"),
    pytest.param(("target_clock", "injection", "count"), 2**32, id="overflow-count"),
    pytest.param(
        ("target_clock", "injection", "before_ms"), 10000, id="predates-check"
    ),
    pytest.param(
        ("target_clock", "injection", "before_ms"), 10112, id="reversed-syscall"
    ),
    pytest.param(
        ("target_clock", "injection", "after_ms"), 10113, id="future-completion"
    ),
    pytest.param(("target_clock", "observation", "now_ms"), 10011, id="regressed-now"),
    pytest.param(
        ("target_clock", "observation", "last_input_ms"), 10009, id="regressed-input"
    ),
    pytest.param(
        ("target_clock", "observation", "last_input_ms"), 10113, id="future-input"
    ),
]


@pytest.mark.parametrize(("path", "value"), INVALID_RESULTS)
def test_invalid_receipt_latches_unknown_without_credit_or_quiet_reset(
    make_rig, path, value
):
    rig = make_rig()
    accept(rig, receipt())
    valid = receipt(10110, now=10112)
    result = changed(valid, path, value) if path else value
    with pytest.raises(HaltedError) as error:
        accept(rig, result)
    assert CURRENT_TARGET_CLOCK.get() is None
    assert_sticky_unknown(rig, error)


@pytest.mark.parametrize("field", ["before_ms", "after_ms", "count"])
@pytest.mark.parametrize(
    "value",
    [DELETE, None, True, False, -1, 1.0, "1", float("nan"), float("inf"), 2**53],
    ids=[
        "absent",
        "null",
        "true",
        "false",
        "negative",
        "float",
        "string",
        "nan",
        "inf",
        "inexact-json",
    ],
)
def test_invalid_emission_integer_is_never_credited(make_rig, field, value):
    rig = make_rig()
    accept(rig, receipt())
    result = changed(
        receipt(10110, now=10112), ("target_clock", "injection", field), value
    )
    with pytest.raises(HaltedError) as error:
        accept(rig, result)
    assert_sticky_unknown(rig, error)


INVALID_OBSERVATIONS = [
    pytest.param((), None, id="missing-observation"),
    pytest.param((), [], id="non-object-observation"),
    pytest.param(("version",), DELETE, id="missing-version"),
    pytest.param(("version",), True, id="boolean-version"),
    pytest.param(("version",), 2, id="unsupported-version"),
    pytest.param(("epoch",), DELETE, id="missing-epoch"),
    pytest.param(("epoch",), "", id="empty-epoch"),
    pytest.param(("epoch",), True, id="boolean-epoch"),
    pytest.param(("epoch",), "x" * 257, id="oversized-epoch"),
    pytest.param(("now_ms",), 10011, id="regressed-now"),
    pytest.param(("last_input_ms",), 10009, id="regressed-last-input"),
    pytest.param(("last_input_ms",), 10101, id="future-last-input"),
    pytest.param(("now_ms",), 10010 + 2**32, id="unwrappable-idle"),
]


@pytest.mark.parametrize("delivery", ["sample", "receipt"])
@pytest.mark.parametrize(("path", "value"), INVALID_OBSERVATIONS)
def test_invalid_observation_is_sticky_unknown_on_both_evidence_paths(
    make_rig, delivery, path, value
):
    rig = make_rig()
    accept(rig, receipt())
    raw = observation(10100, 10010)
    invalid = changed(raw, path, value) if path else value
    if delivery == "sample":
        rig.source.value = invalid
        check = rig.guard.before_event
    else:
        result = {
            "ok": True,
            "target_clock": {"observation": invalid, "injection": None},
        }
        check = lambda: accept(rig, result, require_injection=False)
    with pytest.raises(HaltedError) as error:
        check()
    assert_sticky_unknown(rig, error)


@pytest.mark.parametrize("delivery", ["sample", "receipt"])
@pytest.mark.parametrize("field", ["now_ms", "last_input_ms"])
@pytest.mark.parametrize(
    "value",
    [
        DELETE,
        None,
        True,
        False,
        -1,
        10010.0,
        "10010",
        float("nan"),
        float("inf"),
        2**53,
    ],
    ids=[
        "absent",
        "null",
        "true",
        "false",
        "negative",
        "float",
        "string",
        "nan",
        "inf",
        "inexact-json",
    ],
)
def test_observation_ticks_require_exact_nonboolean_integers(
    make_rig, delivery, field, value
):
    rig = make_rig()
    accept(rig, receipt())
    invalid = changed(observation(10100, 10010), (field,), value)
    if delivery == "sample":
        rig.source.value = invalid
        check = rig.guard.before_event
    else:
        result = {
            "ok": True,
            "target_clock": {"observation": invalid, "injection": None},
        }
        check = lambda: accept(rig, result, require_injection=False)
    with pytest.raises(HaltedError) as error:
        check()
    assert_sticky_unknown(rig, error)


@pytest.mark.parametrize(
    "epoch",
    ["synthetic-boot-b/session-a", "synthetic-boot-a/session-b"],
    ids=["boot-changed", "session-changed"],
)
@pytest.mark.parametrize("seam", ["sample", "observation", "injection"])
def test_boot_or_session_epoch_mismatch_cannot_resume_on_old_baseline(
    make_rig, epoch, seam
):
    rig = make_rig()
    accept(rig, receipt())
    if seam == "sample":
        rig.source.value = observation(10100, 10010, epoch=epoch)
        check = rig.guard.before_event
    else:
        result = receipt(10110, now=10112)
        result["target_clock"][seam]["epoch"] = epoch
        check = lambda: accept(rig, result)
    with pytest.raises(HaltedError) as error:
        check()
    assert_sticky_unknown(rig, error)


@pytest.mark.parametrize(
    "injection",
    [None, {"epoch": EPOCH, "before_ms": 10110, "after_ms": 10111, "count": 0}],
)
def test_failed_input_without_successful_sendinput_does_not_credit_a_baseline(
    make_rig, injection
):
    rig = make_rig()
    accept(rig, receipt())
    result = {
        "ok": False,
        "error": "synthetic SendInput refusal",
        "target_clock": {
            "observation": observation(10112, 10010),
            "injection": injection,
        },
    }
    with pytest.raises(HaltedError) as error:
        accept(rig, result)
    assert_sticky_unknown(rig, error)


@pytest.mark.parametrize("last", [10010, 10050])
def test_failed_composite_credits_only_acknowledged_earlier_emission(make_rig, last):
    # A failed composite can acknowledge an earlier successful syscall. That is
    # different evidence from count=0 or a lost acknowledgement; do not demand
    # that all failures erase genuine earlier receipts or credit LastInputInfo.
    rig = make_rig()
    result = receipt(now=10100, last=last)
    result["ok"] = False
    with pytest.raises(HaltedError) as error:
        accept(rig, result)
    assert rig.monitor.target_condition()["inject_before_ms"] == 10010
    expected = (
        presence.PresenceState.HUMAN_ACTIVE
        if last > 10030
        else presence.PresenceState.UNKNOWN
    )
    assert error.value.snapshot.state is expected
    assert rig.guard.halted


@pytest.mark.parametrize("crossing", [0, 1, 2])
def test_unwrapped_dword_crossing_is_an_ordinary_same_epoch_pair(make_rig, crossing):
    wrap = (crossing + 1) * 2**32
    rig = make_rig(now=wrap - 20, last=wrap - 10000)
    first = receipt(wrap - 10, now=wrap + 6, last=wrap + 6)
    parsed = TargetObservation.parse(first["target_clock"]["observation"])
    assert parsed.now_ms == wrap + 6 and parsed.last_input_ms == wrap + 6
    accept(rig, first)
    assert rig.monitor.last_snapshot.margin_ms == 16
    assert rig.monitor.last_snapshot.state is presence.PresenceState.QUIET
    rig.source.value = observation(wrap + 100, wrap + 6)
    rig.guard.before_event()
    accept(rig, receipt(wrap + 110, now=wrap + 112))
    rig.source.value = observation(wrap + 200, wrap + 110)
    rig.guard.before_event()
    assert rig.monitor.last_snapshot.margin_ms == 0
    assert rig.monitor.target_condition()["inject_before_ms"] == wrap + 110
    assert not rig.guard.halted and not rig.releases


@pytest.mark.parametrize(
    "failure", [None, OSError("lost acknowledgement"), ValueError("corrupt wire")]
)
def test_missing_result_or_exchange_failure_restores_context_and_stays_halted(
    make_rig, failure
):
    rig = make_rig()
    with target_clock_exchange(rig.monitor.target_condition()) as outer:
        with pytest.raises(HaltedError) as error, rig.guard.target_clock_event():
            assert CURRENT_TARGET_CLOCK.get() is not outer
            if failure is not None:
                raise failure
        assert CURRENT_TARGET_CLOCK.get() is outer
    assert CURRENT_TARGET_CLOCK.get() is None
    assert_sticky_unknown(rig, error, baseline=None)


class SyntheticFailure(RuntimeError):
    pass


def test_nested_exchange_and_cleanup_exception_restore_the_exact_previous_context():
    condition = {**observation(), "inject_before_ms": None, "guard_ms": 20.0}
    assert CURRENT_TARGET_CLOCK.get() is None
    with target_clock_exchange(condition) as outer:
        with pytest.raises(SyntheticFailure), target_clock_exchange(condition) as inner:
            assert CURRENT_TARGET_CLOCK.get() is inner
            with pytest.raises(SyntheticFailure), without_target_clock():
                assert CURRENT_TARGET_CLOCK.get() is None
                raise SyntheticFailure("cleanup failed")
            assert CURRENT_TARGET_CLOCK.get() is inner
            raise SyntheticFailure("invocation failed")
        assert CURRENT_TARGET_CLOCK.get() is outer
    assert CURRENT_TARGET_CLOCK.get() is None


def test_concurrent_invocations_keep_conditions_results_and_exception_restore_isolated(
    make_rig,
):
    first = make_rig(epoch="boot-first/session-first")
    second = make_rig(epoch="boot-second/session-second")
    outer_condition = {**observation(), "inject_before_ms": None, "guard_ms": 20.0}

    async def scenario(outer):
        first_ready, second_ready, first_done = (asyncio.Event() for _ in range(3))

        async def invoke(rig, ready, peer_ready, *, fail):
            inherited = CURRENT_TARGET_CLOCK.get()
            assert inherited is outer
            expected = receipt(epoch=rig.source.value["epoch"])

            async def operation():
                with rig.guard.target_clock_event():
                    exchange = CURRENT_TARGET_CLOCK.get()
                    assert exchange.guard is rig.guard
                    assert exchange.condition["epoch"] == rig.source.value["epoch"]
                    ready.set()
                    await peer_ready.wait()
                    assert CURRENT_TARGET_CLOCK.get() is exchange
                    exchange.finish(expected, require_injection=True)
                    assert exchange.result is expected
                    assert exchange.condition == rig.monitor.target_condition()
                    assert exchange.condition["inject_before_ms"] == 10010
                    if fail:
                        await first_done.wait()
                        assert CURRENT_TARGET_CLOCK.get() is exchange
                        raise SyntheticFailure("one invocation only")
                    first_done.set()
                    await asyncio.sleep(0)  # interleave tasks, never a wall-clock wait
                    assert CURRENT_TARGET_CLOCK.get() is exchange

            if fail:
                with pytest.raises(SyntheticFailure):
                    await operation()
            else:
                await operation()
            assert CURRENT_TARGET_CLOCK.get() is inherited
            return rig.monitor.target_condition()

        results = await asyncio.gather(
            invoke(first, first_ready, second_ready, fail=False),
            invoke(second, second_ready, first_ready, fail=True),
        )
        assert results[0]["epoch"] == "boot-first/session-first"
        assert results[1]["epoch"] == "boot-second/session-second"
        assert CURRENT_TARGET_CLOCK.get() is outer

    with target_clock_exchange(outer_condition) as outer:
        asyncio.run(scenario(outer))
        assert CURRENT_TARGET_CLOCK.get() is outer
    assert CURRENT_TARGET_CLOCK.get() is None
    assert not first.guard.halted and not second.guard.halted
