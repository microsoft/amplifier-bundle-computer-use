"""Synthetic mounted-method contracts, not device attribution or timing proof.

Real ComputerTool/DesktopTool, Windows typing, observed wrapper, guard and receipt
validation run here. Only target I/O, disclosure and persistence are inert seams.
No display, SSH, installed runtime change, halt override or live probe is used.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import amplifier_module_tool_computer_use as cu
from amplifier_module_tool_computer_use import guard_diagnostics as diagnostics
from amplifier_module_tool_computer_use.backend import BackendError
from amplifier_module_tool_computer_use.coexistence_guard import CoexistenceGuard
from amplifier_module_tool_computer_use.geometry import Display
from amplifier_module_tool_computer_use.halt_state import (
    PERSISTED_BASIS,
    PERSISTED_UNKNOWN_BASIS,
)
from amplifier_module_tool_computer_use.observed_control import ObservedControl
from amplifier_module_tool_computer_use.presence import (
    Confidence,
    PresenceMonitor,
    PresenceSnapshot,
    PresenceState,
)
from amplifier_module_tool_computer_use.target_clock import (
    CURRENT_TARGET_CLOCK,
    guarded_target_call,
)
from amplifier_module_tool_computer_use.windows import WindowsBackend

EPOCH = "a" * 64
SECRET = "private-host/path/key/text/clipboard/pixels/error"


@pytest.fixture(autouse=True)
def own_loop():
    try:
        previous = asyncio.get_event_loop()
    except RuntimeError:
        previous = None
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield
    loop.run_until_complete(loop.shutdown_asyncgens())
    loop.run_until_complete(loop.shutdown_default_executor())
    loop.close()
    asyncio.set_event_loop(previous)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def bomb(*args, **kwargs):
    pytest.fail("diagnostic read invoked execution/I/O")


def observation(now=10000, last=1000, epoch=EPOCH):
    return {"version": 1, "epoch": epoch, "now_ms": now, "last_input_ms": last}


def receipt(before=10010, epoch=EPOCH):
    return {
        "ok": True,
        "target_clock": {
            "observation": observation(before + 2, before, epoch),
            "injection": {
                "epoch": epoch,
                "before_ms": before,
                "after_ms": before + 1,
                "count": 2,
            },
        },
    }


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setattr(cu, "record_halt", lambda *a, **k: None)
    monkeypatch.setattr(
        cu, "time", SimpleNamespace(time=lambda: 0, sleep=lambda _: None)
    )

    def make():
        source = SimpleNamespace(value=observation(), reads=0)
        emitted, releases, wrappers = [], [], []
        backend = WindowsBackend()  # Constructor only stores config; no probe.

        def read():
            source.reads += 1
            return copy.deepcopy(source.value)

        monitor = PresenceMonitor(bomb, "windows-wsl2", target_clock_source=read)
        guard = CoexistenceGuard(
            monitor,
            release_all=lambda reason: releases.append(reason) or [],
            target_source=lambda: SECRET,
        )

        def raw(action, **kwargs):
            emitted.append((action, kwargs))
            exchange = CURRENT_TARGET_CLOCK.get()
            assert exchange is not None and exchange.guard is guard
            result = receipt(source.value["now_ms"] + 10)
            source.value = copy.deepcopy(result["target_clock"]["observation"])
            exchange.finish(result, require_injection=True)
            return result

        original_type = backend.type_text

        def type_text(text, guard=None):
            wrappers.append(guard)
            return original_type(text, guard)

        backend.raw = raw
        backend.type_text = type_text
        computer = cu.ComputerTool(backend)
        computer._display = Display(800, 600, 800, 600)
        computer._coexistence_guard = guard
        computer._announced = True
        desktop = cu.DesktopTool(computer)
        return SimpleNamespace(
            computer=computer,
            desktop=desktop,
            backend=backend,
            source=source,
            guard=guard,
            monitor=monitor,
            emitted=emitted,
            releases=releases,
            wrappers=wrappers,
            raw=raw,
        )

    return make


def read(rig):
    result = run(rig.desktop.execute({"action": "diagnostic_snapshot"}))
    assert result.success, result.error
    return result.output


def type_body(rig, body):
    result = run(rig.computer.execute({"action": "type", "text": body}))
    assert result.success, result.error
    assert result.output == f"typed {len(body)} characters"
    return result


def last_record(snapshot):
    return snapshot["guard"]["records"][-1]


def test_same_guard_33_receipts_vs_other_guard_and_unattributed_advance(rig):
    same, older = rig(), rig()
    type_body(older, "z")
    type_body(same, "x" * 33)
    saved = read(same)
    completed = saved["guard"]["type_complete"]
    assert completed["accepted_receipts"] == 33
    assert completed["last_receipt"] == {
        "before_ms": 10394,
        "after_ms": 10395,
        "count": 2,
    }
    assert completed["baseline_ms"] == 10394
    assert completed["observation"] == {
        "status": "valid",
        "epoch": EPOCH,
        "now_ms": 10396,
        "last_input_ms": 10394,
    }
    assert completed["unit_index"] == 32
    assert completed["guard_id"] == saved["guard_id"]
    assert completed["binding_id"] == saved["binding_id"]
    assert completed["structured_clock"]
    assert completed["observed_wrapper"] is False
    assert completed["proof_present"] is False
    assert same.monitor.guard_ms == 20.0

    # Same target epoch, but another guard only credited its older receipt.
    older.source.value = copy.deepcopy(same.source.value)
    result = run(older.computer.execute({"action": "key", "text": "Return"}))
    assert not result.success and result.error["type"] == "HaltedError"
    other = read(older)
    before = other["guard"]["before_next_write"]
    assert before["baseline_ms"] == 10010
    assert before["observation"] == completed["observation"]
    assert before["guard_id"] != completed["guard_id"]
    assert before["presence_id"] != completed["presence_id"]
    assert len(older.emitted) == 1

    # A later sample advances independently of any receipt: no attribution is
    # invented. These values model the reported gap; they are NOT live evidence.
    same.source.value = observation(10394 + 44641 + 7187, 10394 + 44641)
    result = run(same.computer.execute({"action": "key", "text": "Return"}))
    assert not result.success and result.error["type"] == "HaltedError"
    halted = read(same)
    assert halted["guard"]["type_complete"] == completed
    before = halted["guard"]["before_next_write"]
    assert before["baseline_ms"] == 10394
    assert before["observation"]["last_input_ms"] - before["baseline_ms"] == 44641
    assert (
        before["observation"]["now_ms"] - before["observation"]["last_input_ms"] == 7187
    )
    assert before["request_id"] != completed["request_id"]
    assert last_record(halted)["halt_origin"] == "live_presence"
    assert halted["guard"]["accepted_receipts"] == 33
    assert len(same.emitted) == 33 and same.releases == ["halted"]


@pytest.mark.parametrize(
    ("basis", "state", "origin"),
    [
        (PERSISTED_BASIS, PresenceState.HUMAN_ACTIVE, "durable_presence"),
        (
            PERSISTED_UNKNOWN_BASIS + ":target_clock_unreadable",
            PresenceState.UNKNOWN,
            "durable_safety",
        ),
    ],
)
def test_seeded_durable_halt_remains_distinct_and_cached(rig, basis, state, origin):
    r = rig()
    snap = PresenceSnapshot(
        state=state,
        confidence=Confidence.LOW,
        basis=basis,
        last_human_input_ago_ms=None,
        margin_ms=None,
        guard_ms=20.0,
        guard_measured=True,
        sample_interval_ms=None,
        latched_until_ms=None,
    )
    r.guard.seed_halted(snap)
    saved = read(r)
    assert last_record(saved)["halt_origin"] == origin
    assert saved["guard"]["type_complete"] is None
    r.guard.durable_halt_poll = bomb
    r.monitor.target_clock_source = bomb
    assert read(r) == saved
    assert r.source.reads == 0 and not r.emitted and not r.releases


@pytest.mark.parametrize("mode", ["flat", "enabled_without_proof", "observed"])
def test_actual_observed_wrapper_and_normal_flat_receipt_paths(rig, monkeypatch, mode):
    r = rig()
    if mode != "flat":
        control = ObservedControl(r.computer)
        r.computer._observed_control = control
        if mode == "observed":
            # Synthetic observation at the pixel-I/O boundary. The actual proof
            # validation, act->canonical execution and wrapper forwarding run.
            monkeypatch.setattr(
                control, "capture", lambda binding: (None, "digest", 800, 600)
            )
            control.observations["revision"] = {
                "binding": r.computer._binding,
                "digest": "digest",
                "observation": {
                    "created_at": cu.time.time(),
                    "candidates": [{"id": "focused:type", "operation": "TYPE_TEXT"}],
                },
            }
            # Use the real clock for the freshness check's synthetic timestamp.
            import time

            control.observations["revision"]["observation"]["created_at"] = (
                time.monotonic()
            )
            result = run(
                r.computer.execute(
                    {
                        "action": "act",
                        "revision": "revision",
                        "candidate": "focused:type",
                        "text": "abc",
                    }
                )
            )
            assert result.success and result.output == {"status": "completed"}
        else:
            type_body(r, "abc")
    else:
        type_body(r, "abc")
    completed = read(r)["guard"]["type_complete"]
    assert completed["accepted_receipts"] == 3
    assert completed["structured_clock"]
    assert completed["observed_wrapper"] is (mode == "observed")
    assert completed["proof_present"] is (mode == "observed")
    records = read(r)["guard"]["records"]
    condition = next(
        record
        for record in records
        if record["phase"] == "condition_constructed" and record["unit_index"] == 2
    )
    accepted = next(
        record
        for record in records
        if record["phase"] == "receipt" and record["unit_index"] == 2
    )
    assert condition["constructed_condition_present"] == "present"
    assert accepted["constructed_condition_present"] == "not_recorded"
    assert condition["request_id"] == accepted["request_id"] == completed["request_id"]
    assert all(
        record["wire_condition_presence"] == "not_measured" for record in records
    )
    assert all((g is not r.guard) is (mode == "observed") for g in r.wrappers)
    if mode == "observed":
        assert all(g.guard is r.guard for g in r.wrappers)


@pytest.mark.parametrize("corrupt", ["missing", "count", "observation", "lost"])
def test_missing_or_corrupt_receipts_still_halt_without_additional_input(rig, corrupt):
    r = rig()
    type_body(r, "a")
    baseline = r.monitor._target_inject_before_ms

    def bad_raw(action, **kwargs):
        r.emitted.append((action, kwargs))
        if corrupt == "lost":
            raise BackendError(SECRET)
        result = receipt(r.source.value["now_ms"] + 10)
        if corrupt == "missing":
            result["target_clock"]["injection"] = None
        elif corrupt == "count":
            result["target_clock"]["injection"]["count"] = float("nan")
        else:
            result["target_clock"]["observation"]["last_input_ms"] = SECRET
        CURRENT_TARGET_CLOCK.get().finish(result, require_injection=True)
        return result

    r.backend.raw = bad_raw
    failed = run(r.computer.execute({"action": "type", "text": SECRET}))
    assert not failed.success and failed.error["type"] == "HaltedError"
    saved = read(r)
    assert saved["guard"]["accepted_receipts"] == 1
    assert last_record(saved)["halt_origin"] == "live_safety"
    assert r.monitor._target_inject_before_ms == baseline
    assert len(r.emitted) == 2
    again = run(r.computer.execute({"action": "key", "text": "Return"}))
    assert not again.success and again.error["type"] == "HaltedError"
    assert len(r.emitted) == 2
    assert SECRET not in json.dumps(read(r))


@pytest.mark.parametrize("halted", [False, True])
def test_snapshot_backend_method_bombs_zero_io_and_unchanged_state(
    rig, monkeypatch, halted
):
    r = rig()
    type_body(r, "abc")
    if halted:
        r.source.value = observation(20000, 19000)
        result = run(r.computer.execute({"action": "key", "text": SECRET}))
        assert not result.success and result.error["type"] == "HaltedError"
    state = (
        vars(r.monitor).copy(),
        vars(r.guard).copy(),
        r.source.reads,
        len(r.emitted),
    )
    saved = read(r)
    for name in dir(WindowsBackend):
        if not name.startswith("_") and callable(getattr(WindowsBackend, name, None)):
            monkeypatch.setattr(r.backend, name, bomb)
    for name in ("sample", "record_inject", "target_condition", "record_target_result"):
        monkeypatch.setattr(r.monitor, name, bomb)
    monkeypatch.setattr(r.guard, "durable_halt_poll", bomb)
    for name in (
        "_ensure_announced",
        "_band_enter",
        "_band_exit",
        "_approve_write",
        "resolve_display",
    ):
        monkeypatch.setattr(r.computer, name, bomb)
    # Exclude our deliberately installed bombs from the state comparison.
    monitor_before = {k: v for k, v in vars(r.monitor).items() if k in state[0]}
    guard_before = {k: v for k, v in vars(r.guard).items() if k in state[1]}
    releases_before = r.releases[:]
    r.computer._cleanup_started = True
    with r.computer._announce_lock:
        assert read(r) == saved
    assert monitor_before == {k: vars(r.monitor)[k] for k in monitor_before}
    assert guard_before == {k: vars(r.guard)[k] for k in guard_before}
    assert (r.source.reads, len(r.emitted)) == state[2:]
    assert r.releases == releases_before


def test_retarget_and_queued_flat_dispatch_keep_exact_identities(rig, monkeypatch):
    r, new = rig(), rig()
    type_body(r, "a")
    old = r.computer._binding
    saved = read(r)
    monkeypatch.setattr(cu, "select_backend", lambda cfg: new.backend)
    monkeypatch.setattr(
        cu,
        "_channel_identity",
        lambda backend: "new" if backend is new.backend else "old",
    )
    monkeypatch.setattr(cu, "_build_coexistence_guard", lambda *a: new.guard)
    monkeypatch.setattr(cu, "_get_channel_ledger", lambda key: None)
    monkeypatch.setattr(cu, "_get_channel_band_state", lambda key: None)
    monkeypatch.setattr(cu, "_build_announcement", lambda *a: None)
    monkeypatch.setattr(
        cu, "_refuse_if_disclosure_declined_with_human_present", lambda *a: None
    )
    monkeypatch.setattr(
        r.computer, "_resolve_display_for_target", lambda *a, **k: (old.display, None)
    )
    queued, release = threading.Event(), threading.Event()
    original = r.computer._run_flat_step

    def wait_for_dispatch(*args):
        queued.set()
        assert release.wait(2)
        return original(*args)

    flight = {
        "cancelled": threading.Event(),
        "lock": threading.Lock(),
        "running": True,
        "cleanup": None,
    }
    outcome = []

    def worker():
        try:
            r.computer._run_owned_step(
                "key",
                old,
                flight,
                wait_for_dispatch,
                ("key", {"text": SECRET}, old, None),
            )
        except BackendError as error:
            outcome.append(error)

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert queued.wait(2)
        assert r.computer.retarget("local").success
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive() and len(outcome) == 1
    current = read(r)
    assert current["tool_id"] == saved["tool_id"]
    assert current["process_id"] == saved["process_id"]
    assert current["binding_id"] != saved["binding_id"]
    assert current["guard_id"] != saved["guard_id"]
    failed = current["tool"]["records"][-1]
    assert failed["phase"] == "dispatch_failed"
    assert failed["binding_id"] == saved["binding_id"]
    assert failed["guard_id"] == saved["guard_id"]
    assert not new.emitted and len(r.emitted) == 1
    assert read(r) == current


def test_bounded_closed_schema_with_malicious_fields_and_no_payload(rig):
    r = rig()
    original = r.backend.raw

    def malicious_raw(action, **kwargs):
        # Additional fields on an accepted receipt are ignored by the cache.
        exchange = CURRENT_TARGET_CLOCK.get()
        result = receipt(r.source.value["now_ms"] + 10)
        result.update(hostname=SECRET, text=SECRET, error=SECRET, pid=123456)
        result["target_clock"]["injection"]["payload"] = SECRET
        r.emitted.append((action, kwargs))
        r.source.value = copy.deepcopy(result["target_clock"]["observation"])
        exchange.finish(result, require_injection=True)
        return result

    r.backend.raw = malicious_raw
    type_body(r, SECRET * 4)
    saved = read(r)
    assert saved["guard"]["truncated"] and saved["tool"]["truncated"]
    assert len(saved["guard"]["records"]) == diagnostics.MAX_RECORDS
    assert len(json.dumps(saved)) < 22000
    assert SECRET not in json.dumps(saved)
    for log in (saved["tool"], saved["guard"]):
        assert all(diagnostics._valid_record(record) for record in log["records"])
        assert log["type_complete"]["accepted_receipts"] == len(SECRET) * 4
    assert re.fullmatch("[0-9a-f]{32}", saved["tool_id"])
    # Returned copies cannot poison later reads.
    saved["guard"]["records"][-1]["text"] = SECRET
    assert SECRET not in json.dumps(read(r))
    # Unexpected cached records are omitted, never returned or stringified.
    r.guard._diagnostics._records.append({"text": SECRET})
    corrupt = read(r)["guard"]
    assert corrupt["fault"] and SECRET not in json.dumps(corrupt)
    r.backend.raw = original


@pytest.mark.parametrize(
    "exception", [RuntimeError(SECRET), asyncio.CancelledError(SECRET)]
)
def test_collection_fault_preserves_result_halt_error_and_cleanup(
    rig, monkeypatch, exception
):
    r = rig()

    def fail(*a, **k):
        raise exception

    monkeypatch.setattr(r.guard._diagnostics, "capture", fail)
    monkeypatch.setattr(r.computer._diagnostics, "capture", fail)
    type_body(r, "abc")
    assert r.guard.binding.status == "not_bound"
    assert read(r)["guard"]["fault"] and read(r)["tool"]["fault"]
    r.source.value = observation(20000, 19000)
    result = run(r.computer.execute({"action": "key", "text": SECRET}))
    assert not result.success and result.error["type"] == "HaltedError"
    assert len(r.emitted) == 3 and r.releases == ["halted"]

    # An actual execution error is not swallowed as a diagnostic failure.
    flight = {
        "cancelled": threading.Event(),
        "lock": threading.Lock(),
        "running": True,
        "cleanup": lambda: r.releases.append("cleanup"),
    }
    for original in (
        RuntimeError("original execution failure"),
        asyncio.CancelledError(),
    ):

        def execution(original=original):
            raise original

        with pytest.raises(type(original)) as raised:
            r.computer._run_owned_step(
                "wait", r.computer._binding, flight, execution, ()
            )
        assert raised.value is original
        assert r.releases[-1] == "cleanup" and flight["running"] is False


def test_diagnostic_setup_fault_and_read_fault_are_fixed_flags(rig, monkeypatch):
    r = rig()
    monkeypatch.setattr(diagnostics, "new_id", bomb)
    type_body(r, "abc")
    assert read(r)["tool"]["fault"]
    monkeypatch.setattr(r.guard._diagnostics, "read", bomb)
    cache = read(r)["guard"]
    assert cache == diagnostics.empty_cache(fault=True)


def test_cancel_inflight_input_no_replay_and_snapshot_never_waits(rig):
    r = rig()
    entered, finish, settled = threading.Event(), threading.Event(), threading.Event()
    raw = r.backend.raw
    cleanup = []

    def blocked_raw(*a, **k):
        entered.set()
        try:
            assert finish.wait(2)
            return raw(*a, **k)
        finally:
            settled.set()

    r.backend.raw = blocked_raw
    r.computer._band_exit = lambda token: cleanup.append("band_exit")

    async def scenario():
        task = asyncio.create_task(
            r.computer.execute({"action": "type", "text": SECRET})
        )
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The worker still owns _announce_lock. Reading must not wait for it.
        result = await r.desktop.execute({"action": "diagnostic_snapshot"})
        assert result.success and not cleanup
        with r.guard._diagnostics._lock:
            result = await r.desktop.execute({"action": "diagnostic_snapshot"})
            assert result.output["guard"]["available"] is False
        finish.set()
        assert await asyncio.to_thread(settled.wait, 2)

    try:
        run(scenario())
    finally:
        finish.set()
        # Drain all owned synthetic worker work before inspecting final cleanup.
        run(asyncio.get_event_loop().shutdown_default_executor())
    assert len(r.emitted) == 1
    assert cleanup == ["band_exit"] and r.guard.binding.status == "not_bound"
    assert read(r)["guard"]["type_complete"] is None
    assert diagnostics._CURRENT.get() is None


def test_missing_guard_and_new_guard_identity_are_explicit(rig):
    r = rig()
    assert (
        "diagnostic_snapshot" in r.desktop.input_schema["properties"]["action"]["enum"]
    )
    first = read(r)
    assert first["guard"]["accepted_receipts"] == 0
    r.computer._coexistence_guard = None
    missing = read(r)
    assert missing["guard"] is None and missing["guard_id"] is None
    r.computer._coexistence_guard = CoexistenceGuard(r.monitor, release_all=bomb)
    fresh = read(r)
    assert fresh["guard_id"] != first["guard_id"]
    assert fresh["binding_id"] == first["binding_id"]
    assert last_record(fresh)["presence_id"] == last_record(first)["presence_id"]
    rejected = run(r.desktop.execute({"action": "diagnostic_snapshot", "text": SECRET}))
    assert not rejected.success and SECRET not in str(rejected.error)


def test_non_hash_epoch_is_redacted_without_changing_guard_results(rig):
    r = rig()
    r.source.value["epoch"] = SECRET

    def raw(action, **kwargs):
        r.emitted.append((action, kwargs))
        result = receipt(r.source.value["now_ms"] + 10, epoch=SECRET)
        r.source.value = copy.deepcopy(result["target_clock"]["observation"])
        CURRENT_TARGET_CLOCK.get().finish(result, require_injection=True)
        return result

    r.backend.raw = raw
    type_body(r, "abc")
    cached = read(r)
    assert cached["guard"]["type_complete"]["observation"]["epoch"] is None
    assert cached["guard"]["type_complete"]["observation"]["status"] == "redacted"
    assert cached["guard"]["accepted_receipts"] == 3
    assert SECRET not in json.dumps(cached)
    assert r.monitor._target_observation.epoch == SECRET


def test_identity_entropy_failure_is_missing_not_an_execution_failure(rig, monkeypatch):
    def fail():
        raise OSError(SECRET)

    # Replace only the helper's module reference, not the process uuid module.
    monkeypatch.setattr(diagnostics, "uuid", SimpleNamespace(uuid4=fail))
    r = rig()
    type_body(r, "abc")
    cached = read(r)
    assert cached["tool_id"] is None and cached["binding_id"] is None
    assert cached["guard_id"] is None
    assert cached["tool"]["fault"] and cached["guard"]["fault"]
    assert cached["guard"]["accepted_receipts"] == 3
    assert SECRET not in json.dumps(cached)


def test_counter_saturation_is_explicit_without_unbounded_growth(rig):
    r = rig()
    r.guard._diagnostics._accepted = diagnostics.MAX_NUMBER
    type_body(r, "a")
    cached = read(r)["guard"]
    assert cached["accepted_receipts"] == diagnostics.MAX_NUMBER
    assert cached["count_saturated"] is True
    assert len(cached["records"]) <= diagnostics.MAX_RECORDS


def test_contended_cache_cannot_stall_guard_or_cleanup(rig):
    r = rig()

    async def scenario():
        with r.guard._diagnostics._lock:
            result = await asyncio.wait_for(
                r.computer.execute({"action": "type", "text": "a"}), timeout=2
            )
            assert result.success
            r.source.value = observation(20000, 19000)
            result = await asyncio.wait_for(
                r.computer.execute({"action": "key", "text": SECRET}), timeout=2
            )
            assert not result.success and result.error["type"] == "HaltedError"

    run(scenario())
    assert len(r.emitted) == 1 and r.releases == ["halted"]
    assert read(r)["guard"]["fault"]
    assert read(r)["tool"]["accepted_receipts"] == 1


def test_guarded_target_call_construction_and_receipt_share_only_opaque_unit_context(
    rig,
):
    r = rig()
    with diagnostics.operation(
        r.computer._diagnostics, r.computer._binding, r.computer._diagnostics.identity
    ):
        for index in range(2):
            diagnostics.typing_unit(index)
            r.guard.before_event()
            with guarded_target_call(r.guard):
                r.raw("type", text=SECRET)
            r.guard.after_event()

    records = read(r)["guard"]["records"]
    for index in range(2):
        unit = [record for record in records if record["unit_index"] == index]
        condition = next(
            record for record in unit if record["phase"] == "condition_constructed"
        )
        accepted = next(record for record in unit if record["phase"] == "receipt")
        assert re.fullmatch("[0-9a-f]{32}", condition["request_id"])
        for key in (
            "request_id",
            "unit_index",
            "tool_id",
            "binding_id",
            "guard_id",
            "presence_id",
        ):
            assert condition[key] == accepted[key]
        assert condition["constructed_condition_present"] == "present"
        assert accepted["constructed_condition_present"] == "not_recorded"
    assert all(
        record["wire_condition_presence"] == "not_measured" for record in records
    )
    assert SECRET not in json.dumps(read(r))
    assert CURRENT_TARGET_CLOCK.get() is None


@pytest.mark.parametrize("configured", [False, True])
def test_clock_configuration_alone_never_claims_constructed_or_wire_condition(
    rig, monkeypatch, configured
):
    r = rig()
    if configured:

        def fail():
            raise ValueError(SECRET)

        monkeypatch.setattr(r.monitor, "target_condition", fail)
        r.backend.raw = bomb
        result = run(r.computer.execute({"action": "type", "text": "a"}))
        assert not result.success and result.error["type"] == "HaltedError"
        assert not r.emitted
    else:
        r.monitor.target_clock_source = None
        r.monitor.idle_source = lambda: 999999.0
        r.backend.raw = lambda *args, **kwargs: {"ok": True}
        type_body(r, "a")
    # Configuration alone (including a construction failure) is not a witness.
    records = read(r)["guard"]["records"]
    assert records
    assert all(record["structured_clock"] is configured for record in records[1:])
    assert all(
        record["constructed_condition_present"] == "not_recorded" for record in records
    )
    assert all(
        record["wire_condition_presence"] == "not_measured" for record in records
    )
    assert SECRET not in json.dumps(read(r))


def test_diagnostic_presence_labels_never_identify_input_author(rig):
    live, durable = rig(), rig()
    type_body(live, "a")
    live.source.value = observation(20000, 19000)
    result = run(live.computer.execute({"action": "key", "text": SECRET}))
    assert not result.success and result.error["type"] == "HaltedError"
    durable.guard.seed_halted(
        PresenceSnapshot(
            state=PresenceState.HUMAN_ACTIVE,
            confidence=Confidence.HIGH,
            basis=PERSISTED_BASIS,
            last_human_input_ago_ms=None,
            margin_ms=None,
            guard_ms=20.0,
            guard_measured=True,
            sample_interval_ms=None,
            latched_until_ms=None,
        )
    )
    assert last_record(read(live))["halt_origin"] == "live_presence"
    assert last_record(read(durable))["halt_origin"] == "durable_presence"
    for r in (live, durable):
        cached = read(r)
        encoded = json.dumps(cached)
        assert "live_human" not in encoded and "durable_human" not in encoded
        assert SECRET not in encoded
        for record in cached["guard"]["records"]:
            assert diagnostics._valid_record(record)
            for key, value in (
                ("halt_origin", "live_human"),
                ("constructed_condition_present", SECRET),
                ("wire_condition_presence", "present"),
            ):
                corrupt = dict(record, **{key: value})
                assert not diagnostics._valid_record(corrupt)
