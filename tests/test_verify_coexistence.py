"""Headless policy/harness controls, NOT native X11 qualification.

The fake backend/child control time and emissions; classification and guard are
production objects. No display, process, network or environment is provisioned.
"""

from __future__ import annotations

import importlib.util
import io
import json
import math
import os
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_coexistence", ROOT / "scripts" / "verify_coexistence.py"
)
assert SPEC is not None and SPEC.loader is not None
verify = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = verify
SPEC.loader.exec_module(verify)

from amplifier_module_tool_computer_use.coexistence_guard import (
    CoexistenceGuard,
    HaltedError,
)
from amplifier_module_tool_computer_use.presence import (
    Confidence,
    PresenceMonitor,
    PresenceState,
)


class Clock:
    def __init__(self):
        self.ns = 3_000_000_000

    def advance(self, seconds):
        self.ns += round(seconds * 1e9)


class Backend:
    def __init__(self, clock):
        self.clock = clock
        self.last = 0
        self.writes = []
        self.closed = False
        # This synthetic unit spans one step of the independent 2ms clock.
        # Separate sub-tick controls below must retain barrier ambiguity.
        self.unit_s = 0.0021
        self.first_delay_s = 0
        self.register = True
        self.receiver = Receiver()

    def server_now(self):
        # A step clock, deliberately not Python monotonic rounded +/-1ms.
        return self.clock.ns // 2_000_000 * 2

    def presence_idle_ms(self):
        return float(self.server_now() - self.last)

    def type_text(self, text, guard=None):
        for _ in text:
            guard.before_event()
            if not self.writes:
                self.clock.advance(self.first_delay_s)
            self.writes.append(self.clock.ns)
            self.clock.advance(self.unit_s)
            if self.register:
                self.last = self.server_now()
            self.receiver.add(2, self.server_now(), detail=38)
            self.receiver.add(3, self.server_now(), detail=38)
            guard.after_event()

    def probe(self):
        return SimpleNamespace(available=True)

    def close(self):
        self.closed = True


class IntegerTickBackend(Backend):
    """One-ms server clock, independent from monotonic send/receive brackets."""

    query_s = 0.0

    def presence_idle_ms(self):
        idle = self.server_now() - self.last
        self.clock.advance(self.query_s)
        return float(idle)

    def server_now(self):
        return self.clock.ns // 1_000_000


class Receiver(verify.EventReceiver):
    def __init__(self):
        self.queue = []
        self.ordinal = 0
        self.armed = False
        self.keycode = 38
        self.window = SimpleNamespace(id=99)
        self.cleanup = {}

    def add(self, kind, timestamp, detail=0, x=0, y=0):
        self.ordinal += 1
        self.queue.append(
            {
                "ordinal": self.ordinal,
                "type": kind,
                "window": 99,
                "detail": detail,
                "x": x,
                "y": y,
                "server_ms": timestamp % 2**32,
                "send_event": False,
                "received_ns": verify.time.monotonic_ns(),
            }
        )

    def _drain(self):
        batch, self.queue = self.queue, []
        return batch

    def close(self):
        self.cleanup = {"closed": True, "errors": []}


class Oracle(verify.RegistrationOracle):
    def __init__(self, backend, receiver=None):
        self.backend = backend
        self.receiver = receiver or backend.receiver
        self.clock = SimpleNamespace(read=backend.server_now, identity={"fake": True})
        self.previous = None
        self.last_ordinal = 0


class Child:
    def __init__(self, backend):
        self.backend = backend
        self.calls = 0
        self.cleanup = {}

    def fire(self):
        self.calls += 1
        before = self.backend.clock.ns
        self.backend.clock.advance(0.00001)
        self.backend.last = self.backend.server_now()
        self.backend.receiver.add(6, self.backend.last, x=7 + self.calls, y=7)
        return {
            "id": self.calls,
            "send_before_ns": before,
            "sync_after_ns": self.backend.clock.ns,
        }

    def ready(self):
        pass

    def close(self):
        self.cleanup = {"reaped": True, "returncode": 0, "forced": False}


@pytest.fixture
def rig(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(verify.time, "monotonic", lambda: clock.ns / 1e9)
    monkeypatch.setattr(verify.time, "monotonic_ns", lambda: clock.ns)
    monkeypatch.setattr(verify.time, "sleep", clock.advance)
    backend = Backend(clock)
    native_case = verify._run_case
    monkeypatch.setattr(
        verify,
        "_run_case",
        lambda case, backend, child, oracle=None: native_case(
            case, backend, child, oracle or Oracle(backend)
        ),
    )
    monkeypatch.setattr(verify, "EventReceiver", lambda _display: backend.receiver)
    monkeypatch.setattr(verify, "RegistrationOracle", Oracle)
    return clock, backend, Child(backend)


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "missing",
        "foreign-window",
        "wrong-key",
        "SendEvent",
        "extra",
        "replay",
        "same-counter",
    ],
)
def test_main_exact_native_pattern_requires_delivery_and_server_counter(
    rig,
    monkeypatch,
    fault,
):
    clock, backend, _ = rig
    # Public numeric pattern only; no private receipt copied into the repository.
    before, after = 284093613888391, 284093614163353
    entry, returned, idle = 284093614228186, 284093614455820, 2.0
    assert before - (returned - (idle - 1) * 1e6) == 432571
    clock.ns = entry
    oracle = Oracle(backend)
    # Server ticks need not share Python's phase/age. This is a synthetic
    # same-clock witness, NOT evidence of Main's actual Xvfb clock choice.
    oracle.clock.read = lambda: 100002
    oracle.previous = 99940 if fault != "same-counter" else 100000
    oracle.receiver.arm()
    if fault != "missing":
        backend.receiver.add(2, 100000, detail=38)
        backend.receiver.add(3, 100000, detail=38)
        if fault == "foreign-window":
            backend.receiver.queue[0]["window"] = 100
        elif fault == "wrong-key":
            backend.receiver.queue[0]["detail"] = 39
        elif fault == "SendEvent":
            backend.receiver.queue[0]["send_event"] = True
        elif fault == "extra":
            backend.receiver.add(3, 100000, detail=38)
        elif fault == "replay":
            oracle.last_ordinal = 2

    def read():
        clock.ns = returned
        return idle

    monkeypatch.setattr(backend, "presence_idle_ms", read)
    row = {
        "emit_before_ns": before,
        "emit_after_ns": after,
        "sample_counter": {
            "server_before_ms": 99940,
            "server_after_ms": 99940,
            "idle_ms": 0,
        },
    }
    result = {"timeline": [row]}
    if fault:
        with pytest.raises(verify.InvalidCase):
            batch = oracle.consume("own")
            verify._confirm_own(backend, result, oracle, batch)
    else:
        batch = oracle.consume("own")
        verify._confirm_own(backend, result, oracle, batch)
        assert row["own_counter"]["corroborated"]
        assert row["own_counter"]["entry_ns"] == entry
        assert row["own_counter"]["return_ns"] == returned
        assert row["own_counter"]["event_server_ms"] == 100000
        assert "status" not in result  # registration alone never grants gate PASS


@pytest.mark.parametrize(
    "case", ["quiet", "inside-2ms", "outside-8ms", "outside-15ms", "outside-55ms"]
)
def test_integer_tick_full_controls_include_registration_and_fresh_sample(rig, case):
    clock, _, _ = rig
    clock.ns += 900_000  # exercise a nonintegral phase, never rescue-delay a trial
    backend = IntegerTickBackend(clock)
    child = Child(backend)
    result = verify._run_case(case, backend, child)
    assert result["status"] == "PASS", result
    if case != "quiet":
        assert result["event_counter"]["idle_ms"].is_integer()
        sample = result["timeline"][2]
        assert sample["snapshot"]["last_human_input_ago_ms"].is_integer()
        low, high = verify._server_interval(sample["sample_counter"])
        assert low <= result["event_counter"]["event_server_ms"] <= high


@pytest.mark.parametrize("unit_s", [0.00001, 0.0004])
def test_integer_tick_barrier_ambiguous_advancement_stays_invalid(rig, unit_s):
    clock, _, _ = rig
    backend = IntegerTickBackend(clock)
    backend.unit_s = unit_s
    child = Child(backend)
    result = verify._run_case("post-sample", backend, child)
    assert result["status"] == "INVALID"
    assert "own counter registration" in result["reason"]
    assert child.calls == 1


@pytest.mark.parametrize("case", ["quiet", "inside-2ms", "outside-15ms", "post-sample"])
def test_integer_tick_hooks_without_registration_cannot_qualify(rig, case):
    clock, _, _ = rig
    backend = IntegerTickBackend(clock)
    backend.register = False
    child = Child(backend)
    result = verify._run_case(case, backend, child)
    assert result["status"] == "INVALID"
    assert "own counter registration" in result["reason"]
    assert len(backend.writes) == 1 and child.calls == 0


def test_integer_tick_child_sync_without_registration_stays_invalid(rig, monkeypatch):
    clock, _, _ = rig
    backend = IntegerTickBackend(clock)
    child = Child(backend)

    def no_registration():
        child.calls += 1
        before = clock.ns
        clock.advance(0.00001)
        backend.receiver.add(6, backend.server_now(), x=7 + child.calls, y=7)
        return {"id": child.calls, "send_before_ns": before, "sync_after_ns": clock.ns}

    monkeypatch.setattr(child, "fire", no_registration)
    result = verify._run_case("outside-15ms", backend, child)
    assert result["status"] == "INVALID"
    assert "counter" in result["reason"]
    assert child.calls == 1 and len(backend.writes) == 1


def test_integer_tick_barrier_ineffective_own_write_cannot_borrow_child_registration(
    rig, monkeypatch
):
    clock, _, _ = rig
    backend = IntegerTickBackend(clock)
    child = Child(backend)
    fire = child.fire

    def child_then_disable_own():
        response = fire()
        backend.register = False
        return response

    monkeypatch.setattr(child, "fire", child_then_disable_own)
    result = verify._run_case("post-sample", backend, child)
    assert result["status"] == "INVALID"
    assert "own counter registration" in result["reason"]
    assert child.calls == 1 and len(backend.writes) == 2


def test_server_bracket_excludes_prior_child_timestamp(rig):
    _, backend, _ = rig
    oracle = Oracle(backend)
    oracle.previous = 100
    counter = {"server_before_ms": 101, "server_after_ms": 102, "idle_ms": 1}
    with pytest.raises(verify.InvalidCase, match="did not advance"):
        oracle.confirm(counter, [{"server_ms": 101}])


def test_sync_wire_layouts_without_server():
    initialize, counters, query = verify._sync_requests()
    assert initialize._request.to_binary(opcode=134, major=3, minor=1) == struct.pack(
        "=BBHBBH", 134, 0, 2, 3, 1, 0
    )
    assert counters._request.to_binary(opcode=134) == struct.pack("=BBH", 134, 1, 1)
    assert query._request.to_binary(opcode=134, counter=0x12345678) == struct.pack(
        "=BBHI", 134, 5, 2, 0x12345678
    )
    version, rest = initialize._reply.parse_binary(
        struct.pack("=BBHIBB22x", 1, 0, 42, 0, 3, 1),
        None,
    )
    assert (version.major, version.minor, version.length, rest) == (3, 1, 0, b"")
    payload = _counter_record(77, b"SERVERTIME")
    listing, rest = counters._reply.parse_binary(
        struct.pack("=BBHIi20x", 1, 0, 43, len(payload) // 4, 1) + payload,
        None,
    )
    assert listing.payload == payload and listing.count == 1 and rest == b""
    assert verify._server_counter(listing.count, listing.payload) == (77, 4)
    for hi, lo, expected in [(1, 7, 2**32 + 7), (-1, 0xFFFFFFFF, -1)]:
        value, rest = query._reply.parse_binary(
            struct.pack("=BBHIiI16x", 1, 0, 44, 0, hi, lo),
            None,
        )
        assert (value.hi << 32) + value.lo == expected and rest == b""


def _counter_record(counter, name):
    data = struct.pack("=IiIH", counter, 0, 4, len(name)) + name
    return data + b"\0" * (-len(data) % 4)


@pytest.mark.parametrize(
    "fault", ["missing", "duplicate", "truncated", "tail", "count"]
)
def test_sync_counter_discovery_rejects_missing_or_malformed(fault):
    payload = _counter_record(2, b"odd") + _counter_record(77, b"SERVERTIME")
    count = 2
    assert verify._server_counter(count, payload) == (77, 4)
    if fault == "missing":
        payload = _counter_record(2, b"odd")
        count = 1
    elif fault == "duplicate":
        payload = _counter_record(77, b"SERVERTIME") * 2
    elif fault == "truncated":
        payload = payload[:-1]
    elif fault == "tail":
        payload += b"\0"
    else:
        count = 257
    with pytest.raises(verify.InvalidCase):
        verify._server_counter(count, payload)


def test_server_clock_binding_discovers_only_servertime(monkeypatch):
    calls = []

    def request(kind, **kw):
        calls.append((kind, kw))
        return {
            "initialize": SimpleNamespace(major=3, minor=1, length=0),
            "counters": SimpleNamespace(
                length=6,
                count=1,
                payload=_counter_record(77, b"SERVERTIME"),
            ),
            "query": SimpleNamespace(hi=1, lo=17, length=0),
        }[kind]

    monkeypatch.setattr(
        verify,
        "_sync_requests",
        lambda: (
            lambda **kw: request("initialize", **kw),
            lambda **kw: request("counters", **kw),
            lambda **kw: request("query", **kw),
        ),
    )
    connection = SimpleNamespace(
        display=object(),
        query_extension=lambda name: SimpleNamespace(present=True, major_opcode=134),
    )
    clock = verify.ServerClock(connection)
    assert clock.read() == 2**32 + 17
    assert clock.identity["counter"] == 77
    assert calls[0][1]["major"] == 3 and calls[0][1]["minor"] == 1
    assert calls[-1][1]["counter"] == 77
    connection.query_extension = lambda name: None
    with pytest.raises(verify.InvalidCase, match="SYNC unavailable"):
        verify.ServerClock(connection)


@pytest.mark.parametrize(
    "values,valid",
    [
        ([2**32 - 1, 2**32 + 1], True),
        ([200, 199], False),
        ([200, 2**32 + 199], False),  # rollback hidden by Xorg epoch update
        ([0, 90001], False),
        ([2**63 - 1, -(2**63)], False),
    ],
)
def test_server_clock_wrap_and_regress_are_conservative(values, valid):
    replies = iter(values)
    clock = object.__new__(verify.ServerClock)
    clock.display, clock.opcode, clock.counter = None, 134, 77
    clock.first = clock.last = None

    def query(**kw):
        value = next(replies)
        return SimpleNamespace(hi=value >> 32, lo=value & 0xFFFFFFFF, length=0)

    clock.query = query
    assert clock.read() == values[0]
    if valid:
        assert clock.read() == values[1]
    else:
        with pytest.raises(verify.InvalidCase, match="regressed/wrapped"):
            clock.read()


def test_native_event_low32_unwrap_never_maps_to_python():
    assert verify._event_time(0xFFFFFFFF, 2**32 + 2) == 2**32 - 1
    assert verify._event_time(1, 2**32 + 2) == 2**32 + 1
    for timestamp in [3, 2**31 + 2, -1, 2**32, 1.0, True]:
        with pytest.raises(verify.InvalidCase):
            verify._event_time(timestamp, 2**32 + 2)


@pytest.mark.parametrize("idle", [math.nan, math.inf, -1, 0.25, 2**31, True, None])
def test_invalid_raw_counter_is_never_a_registration_witness(rig, monkeypatch, idle):
    _, backend, _ = rig
    monkeypatch.setattr(backend, "presence_idle_ms", lambda: idle)
    with pytest.raises(verify.InvalidCase):
        verify._counter_read(backend, {}, Oracle(backend).clock)


def test_regressing_query_bracket_and_broken_read(rig, monkeypatch):
    _, backend, _ = rig
    ticks = iter([3000, 2999])
    with pytest.raises(verify.InvalidCase, match="regressing"):
        verify._counter_read(backend, {}, SimpleNamespace(read=lambda: next(ticks)))
    monkeypatch.setattr(
        backend,
        "presence_idle_ms",
        lambda: (_ for _ in ()).throw(OSError("broken query")),
    )
    with pytest.raises(OSError, match="broken query"):
        verify._counter_read(backend, {}, Oracle(backend).clock)


@pytest.mark.parametrize("stale_type", [2, 4, 5])
def test_receiver_arms_empty_and_consumes_each_batch_only_once(rig, stale_type):
    _, backend, _ = rig
    receiver = backend.receiver
    receiver.add(stale_type, 3000, detail=38)
    with pytest.raises(verify.UnexpectedInput, match="stale"):
        receiver.arm()
    receiver.arm()
    receiver.add(2, 3000, detail=38)
    receiver.add(3, 3000, detail=38)
    assert len(receiver.consume("own")) == 2
    with pytest.raises(verify.InvalidCase, match="replayed"):
        receiver.consume("own")
    receiver.arm()
    receiver.add(6, 3001, x=8, y=7)
    receiver.queue[-1]["x"] = 9
    with pytest.raises(verify.InvalidCase, match="child motion"):
        receiver.consume("child", 1)


@pytest.mark.parametrize("fault", ["repeated", "reversed", "older-than-prior"])
def test_delivery_identity_and_timestamp_cannot_replay(rig, fault):
    _, backend, _ = rig
    oracle = Oracle(backend)
    oracle.receiver.arm()
    backend.receiver.add(2, 3001, detail=38)
    backend.receiver.add(3, 3000, detail=38)
    if fault == "repeated":
        backend.receiver.queue[1]["ordinal"] = backend.receiver.queue[0]["ordinal"]
    elif fault == "older-than-prior":
        backend.receiver.queue[0]["server_ms"] = 3000
        oracle.previous = 3001
    with pytest.raises(verify.InvalidCase):
        batch = oracle.consume("own")
        oracle.confirm(
            {"server_before_ms": 3001, "server_after_ms": 3001, "idle_ms": 1},
            batch,
        )


@pytest.mark.parametrize("types,detail", [((2, 3), 38), ((4, 5), 1)])
def test_native_input_after_active_fails_even_without_emit_callback(
    rig, monkeypatch, types, detail
):
    _, backend, child = rig
    type_text = backend.type_text

    def broken(text, guard):
        try:
            type_text(text, guard)
        except HaltedError:
            for event_type in types:
                backend.receiver.add(
                    event_type, backend.server_now(), detail=detail, x=8, y=7
                )
            raise

    monkeypatch.setattr(backend, "type_text", broken)
    result = verify._run_case("outside-15ms", backend, child)
    assert result["status"] == "FAIL" and result["forbidden_native_input"]
    assert "emit_after_ns" not in result["timeline"][2]
    assert result["timeline"][2]["unexpected_delivery"]["count"] == 2
    assert [
        event["type"] for event in result["timeline"][2]["unexpected_delivery"]["batch"]
    ] == list(types)


def test_callbacks_without_native_delivery_cannot_qualify(rig, monkeypatch):
    _, backend, child = rig
    monkeypatch.setattr(backend.receiver, "add", lambda *a, **kw: None)
    result = verify._run_case("quiet", backend, child)
    assert result["status"] == "INVALID" and "native delivery" in result["reason"]
    assert len(backend.writes) == 1


def test_proven_outside_miss_survives_subsequent_own_counter_error(rig, monkeypatch):
    _, backend, child = rig
    classify = PresenceMonitor._classify

    def quiet(self, margin, idle):
        if margin is not None and margin > self.guard_ms:
            backend.register = False
            return PresenceState.QUIET, Confidence.LOW
        return classify(self, margin, idle)

    monkeypatch.setattr(PresenceMonitor, "_classify", quiet)
    result = verify._run_case("outside-15ms", backend, child)
    assert result["status"] == "FAIL"
    assert "proven outside" in result["reason"]


@pytest.mark.parametrize("fault", [None, "keymap", "focus", "restore", "destroy"])
def test_receiver_structural_setup_and_independent_cleanup(monkeypatch, fault):
    from Xlib import X
    from Xlib import display as xlib_display

    calls = []
    saved_focus = SimpleNamespace(get_attributes=lambda: calls.append("validate-focus"))
    focus = saved_focus

    def select_focus(*args, **kw):
        nonlocal focus
        focus = window
        calls.append("focus")
        if fault == "focus":
            kw["onerror"](OSError("focus error"), None)

    def restore(*args, **kw):
        calls.append("restore")
        assert args[0] is saved_focus
        if fault == "restore":
            kw["onerror"](OSError("restore error"), None)

    def destroy(**kw):
        calls.append("destroy")
        if fault == "destroy":
            kw["onerror"](OSError("destroy error"), None)

    window = SimpleNamespace(
        id=99,
        map=lambda **kw: calls.append("map"),
        configure=lambda **kw: calls.append("raise"),
        set_input_focus=select_focus,
        destroy=destroy,
    )

    def create(*args, **kw):
        calls.append("create")
        assert args[:4] == (0, 0, 64, 64)
        assert kw["override_redirect"] is True
        assert kw["event_mask"] & (X.ButtonPressMask | X.ButtonReleaseMask) == (
            X.ButtonPressMask | X.ButtonReleaseMask
        )
        return window

    connection = SimpleNamespace(
        screen=lambda: SimpleNamespace(root=SimpleNamespace(create_window=create)),
        get_input_focus=lambda: SimpleNamespace(focus=focus, revert_to=1),
        keysym_to_keycodes=lambda sym: [(38, 1 if fault == "keymap" else 0)],
        set_input_focus=restore,
        pending_events=lambda: 0,
        sync=lambda: None,
        close=lambda: calls.append("close"),
    )
    monkeypatch.setattr(xlib_display, "Display", lambda name: connection)
    if fault in ("keymap", "focus"):
        with pytest.raises(verify.InvalidCase):
            verify.EventReceiver(":fake-only")
    else:
        receiver = verify.EventReceiver(":fake-only")
        assert calls[:4] == ["create", "map", "raise", "focus"]
        receiver.close()
        assert receiver.cleanup["closed"] is (fault is None)
    assert calls[-1] == "close"
    if fault != "keymap":
        assert calls[-3:] == ["restore", "destroy", "close"]


def test_real_receiver_drain_has_fixed_batch_bound_without_native_display():
    receiver = object.__new__(verify.EventReceiver)
    receiver.ordinal = 0
    receiver.conn = SimpleNamespace(sync=lambda: None, pending_events=lambda: 4)
    with pytest.raises(verify.InvalidCase, match="exceeds bound"):
        receiver._drain()


def test_actual_monitor_idle_query_is_between_same_connection_server_reads(
    rig, monkeypatch
):
    _, backend, child = rig
    oracle = Oracle(backend)
    calls = []
    read = backend.presence_idle_ms
    monkeypatch.setattr(
        backend, "presence_idle_ms", lambda: calls.append("idle") or read()
    )
    oracle.clock.read = lambda: calls.append("server") or backend.server_now()
    result = {"timeline": [], "releases": []}
    presence, _, _, _ = verify._instrument(backend, child, result, oracle)
    snapshot = presence.sample()
    assert calls == ["server", "idle", "server"]
    assert result["timeline"][0]["snapshot"] == snapshot.to_dict()
    assert (
        result["timeline"][0]["sample_counter"]["idle_ms"]
        == snapshot.last_human_input_ago_ms
    )


@pytest.mark.parametrize("failure", ["oracle", "child-close", "receiver-close"])
def test_cleanup_paths_independent_and_cannot_qualify(rig, monkeypatch, failure):
    from amplifier_module_tool_computer_use import linux_x11

    _, backend, child = rig
    monkeypatch.setattr(linux_x11, "LinuxX11Backend", lambda config: backend)
    monkeypatch.setattr(verify, "EventChild", lambda display: child)
    monkeypatch.setattr(
        verify,
        "_run_case",
        lambda case, *_args: {"case": case, "status": "PASS"},
    )
    if failure == "oracle":
        monkeypatch.setattr(
            verify,
            "RegistrationOracle",
            lambda *a: (_ for _ in ()).throw(verify.InvalidCase("SYNC unavailable")),
        )
    elif failure == "child-close":
        monkeypatch.setattr(
            child,
            "close",
            lambda: (_ for _ in ()).throw(OSError("close failed")),
        )
    else:
        monkeypatch.setattr(
            backend.receiver,
            "close",
            lambda: setattr(backend.receiver, "cleanup", {"closed": False}),
        )
    report = {}
    if failure == "receiver-close":
        verify._run_boundary(report, ":fake-only")
        assert report["status"] == "UNQUALIFIED"
    else:
        with pytest.raises((OSError, verify.InvalidCase)):
            verify._run_boundary(report, ":fake-only")
        assert backend.receiver.cleanup["closed"]
    assert backend.closed
    assert report.get("status") != verify.MECHANISM_CONTROLS_PASS


@pytest.mark.parametrize("offset,halts", [(-0.001, False), (0, False), (0.001, True)])
def test_exact_guard_boundary_uses_production_classifier_and_guard(
    monkeypatch, offset, halts
):
    # Exact arithmetic control, not a scheduler claim about equality at 5ms.
    margin = 5.0 + offset
    monitor = PresenceMonitor(idle_source=lambda: 0.0, platform="linux-x11")
    state, confidence = monitor._classify(margin, 0)
    assert (state is PresenceState.HUMAN_ACTIVE) == halts
    assert confidence is (Confidence.HIGH if halts else Confidence.LOW)
    monitor.record_inject(at=0)
    monkeypatch.setattr(verify.time, "monotonic", lambda: margin / 1000)
    releases = []
    guard = CoexistenceGuard(
        presence=monitor, release_all=lambda reason: releases.append(reason) or []
    )
    if halts:
        with pytest.raises(HaltedError):
            guard.before_event()
        # Quiet now, but guard and presence latches refuse a subsequent write.
        monitor.record_inject(at=margin / 1000)
        with pytest.raises(HaltedError):
            guard.before_event()
        assert releases == ["halted", "halted"]
    else:
        guard.before_event()
        assert releases == []


@pytest.mark.parametrize("case", verify.CASES)
def test_each_predeclared_control_once_with_production_objects(rig, case):
    _, backend, child = rig
    result = verify._run_case(case, backend, child)
    assert result["status"] == "PASS", result
    assert child.calls == (0 if case == "quiet" else 1)
    if case.startswith("outside"):
        assert result["placement"] == "OUTSIDE"
        assert result["detected"] is True
        assert result["latch_refused"] is True
        assert len(backend.writes) == 1
        assert len(result["releases"]) == 2
    elif case == "inside-2ms":
        assert result["placement"] == "INSIDE"
        assert result["detected"] is False
        assert len(backend.writes) == 2
    elif case == "post-sample":
        assert result["placement"] == "BARRIER"
        assert result["detected"] is False
        assert len(backend.writes) == 3
        assert "not a natural" in result["limitation"]
    else:
        assert len(backend.writes) == 101
        assert (
            result["timeline"][-1]["emit_after_ns"]
            - result["timeline"][1]["emit_after_ns"]
            >= 6_000_000_000
        )
    assert all(
        row["own_counter"]["corroborated"]
        for row in result["timeline"][1:]
        if "emit_after_ns" in row
    )


@pytest.mark.parametrize("idle", [1, 2000, math.nan, math.inf, -1])
def test_bad_baseline_invalid_without_child_or_input(rig, monkeypatch, idle):
    _, backend, child = rig
    monkeypatch.setattr(backend, "presence_idle_ms", lambda: idle)
    result = verify._run_case("outside-15ms", backend, child)
    assert result["status"] == "INVALID"
    assert child.calls == 0 and backend.writes == []


def test_unreadable_baseline_is_not_a_masking_miss(rig, monkeypatch):
    _, backend, child = rig
    monkeypatch.setattr(
        backend,
        "presence_idle_ms",
        lambda: (_ for _ in ()).throw(OSError("read failed")),
    )
    result = verify._run_case("outside-15ms", backend, child)
    assert result["status"] == "INVALID" and "read failed" in result["reason"]
    assert child.calls == 0


def test_inside_nominal_delay_is_not_placement_proof_and_no_retry(rig, monkeypatch):
    clock, backend, child = rig
    original = child.fire

    def late():
        clock.advance(0.007)
        return original()

    monkeypatch.setattr(child, "fire", late)
    result = verify._run_case("inside-2ms", backend, child)
    assert result["status"] == "INVALID"
    assert child.calls == 1
    assert "unplaceable" in result["reason"]


def test_sync_without_counter_registration_invalidates_control(rig, monkeypatch):
    _, backend, child = rig

    def no_event():
        return {
            "id": 1,
            "send_before_ns": backend.clock.ns,
            "sync_after_ns": backend.clock.ns,
        }

    monkeypatch.setattr(child, "fire", no_event)
    result = verify._run_case("outside-15ms", backend, child)
    assert result["status"] == "INVALID"
    assert "native delivery" in result["reason"]
    assert len(backend.writes) == 1


def test_barrier_child_failure_contained_by_production_but_prevents_harness_write(
    rig, monkeypatch
):
    _, backend, child = rig
    monkeypatch.setattr(
        child, "fire", lambda: (_ for _ in ()).throw(verify.InvalidCase("lost ack"))
    )
    result = verify._run_case("post-sample", backend, child)
    assert result["status"] == "INVALID"
    assert "lost ack" in result["reason"]
    assert len(backend.writes) == 1


def test_quiet_false_positive_fails_not_invalid_or_miss(rig, monkeypatch):
    clock, backend, child = rig
    real_read = backend.presence_idle_ms
    reads = 0

    def noisy_read():
        nonlocal reads
        reads += 1
        if reads == 4:
            return 0
        return real_read()

    monkeypatch.setattr(backend, "presence_idle_ms", noisy_read)
    result = verify._run_case("quiet", backend, child)
    assert result["status"] == "FAIL"
    assert child.calls == 0
    assert len(backend.writes) == 1
    assert clock.ns >= 3_060_000_000


def test_outside_unexplained_miss_is_failure_not_reconditioned_away(rig, monkeypatch):
    _, backend, child = rig
    original = PresenceMonitor._classify

    def broken_classifier(self, margin, idle):
        # Fault injection: timing and returned counter still place the event,
        # but classification regresses. Must not condition this miss away.
        if margin is not None and margin > self.guard_ms:
            return PresenceState.QUIET, Confidence.LOW
        return original(self, margin, idle)

    monkeypatch.setattr(PresenceMonitor, "_classify", broken_classifier)
    result = verify._run_case("outside-15ms", backend, child)
    assert result["status"] == "FAIL"
    assert result["placement"] == "OUTSIDE"
    assert result["detected"] is False


def test_inside_detection_is_allowed_not_required_miss(rig, monkeypatch):
    _, backend, child = rig
    original = PresenceMonitor._classify

    def conservative_classifier(self, margin, idle):
        # Policy control only: emulate earlier attribution without changing the
        # counter/timing evidence. This is not native early-detection evidence.
        if margin is not None and margin > 0:
            return PresenceState.HUMAN_ACTIVE, Confidence.HIGH
        return original(self, margin, idle)

    monkeypatch.setattr(PresenceMonitor, "_classify", conservative_classifier)
    result = verify._run_case("inside-2ms", backend, child)
    assert result["status"] == "PASS"
    assert result["detected"] and result["latch_refused"]


@pytest.mark.parametrize("counter", ["older", "newer"])
def test_changed_counter_before_fresh_sample_is_invalid_not_a_masking_miss(
    rig, monkeypatch, counter
):
    _, backend, child = rig
    original = backend.presence_idle_ms
    reads = 0

    def contaminated():
        nonlocal reads
        reads += 1
        if reads == 5:
            return (
                backend.server_now() - backend.writes[0] // 2_000_000 * 2 - 2
                if counter == "older"
                else 0
            )
        return original()

    monkeypatch.setattr(backend, "presence_idle_ms", contaminated)
    result = verify._run_case("outside-15ms", backend, child)
    assert result["status"] == "INVALID"
    assert "fresh guard counter" in result["reason"]
    assert child.calls == 1


def test_nonfinite_registration_counter_retains_json_safe_invalid_evidence(
    rig, monkeypatch
):
    _, backend, child = rig
    original = backend.presence_idle_ms
    reads = 0

    def broken():
        nonlocal reads
        reads += 1
        return math.nan if reads == 4 else original()

    monkeypatch.setattr(backend, "presence_idle_ms", broken)
    result = verify._run_case("outside-15ms", backend, child)
    assert result["status"] == "INVALID"
    assert result["event_counter"]["idle_ms"] is None
    json.dumps(result, allow_nan=False)


def test_outside_release_failure_never_qualifies(rig, monkeypatch):
    _, backend, child = rig
    original = verify._instrument

    def instrument(*args):
        presence, guard, tracked, arm = original(*args)
        guard.release_all = lambda reason: []
        return presence, guard, tracked, arm

    monkeypatch.setattr(verify, "_instrument", instrument)
    result = verify._run_case("outside-15ms", backend, child)
    assert result["status"] == "FAIL"


@pytest.mark.parametrize("case,active_sample", [("inside-2ms", 3), ("post-sample", 4)])
def test_activity_with_defective_guard_return_and_emission_fails_every_case(
    rig, monkeypatch, case, active_sample
):
    _, backend, child = rig
    classify = PresenceMonitor._classify
    before_event = CoexistenceGuard.before_event
    samples = 0

    def activity(self, margin, idle):
        nonlocal samples
        samples += 1
        if samples == active_sample:
            return PresenceState.HUMAN_ACTIVE, Confidence.HIGH
        return classify(self, margin, idle)

    def broken_return(self):
        try:
            before_event(self)
        except HaltedError:
            pass  # deliberately broken: activity/release, then permit emission

    monkeypatch.setattr(PresenceMonitor, "_classify", activity)
    monkeypatch.setattr(CoexistenceGuard, "before_event", broken_return)
    result = verify._run_case(case, backend, child)
    active = [
        row
        for row in result["timeline"][1:]
        if row["snapshot"]["state"] == "human_active"
    ]
    assert active and "emit_after_ns" in active[0] and not active[0].get("halted")
    assert result["status"] == "FAIL"
    assert "observation/halt invariant" in result["reason"]
    assert result["detected"] is True
    assert child.calls == 1


def test_retry_release_cannot_cover_omitted_initial_halt_release(rig, monkeypatch):
    _, backend, child = rig
    instrument = verify._instrument

    def missing_first(*args):
        presence, guard, tracked, arm = instrument(*args)
        release = guard.release_all
        calls = 0

        def retry_only(reason):
            nonlocal calls
            calls += 1
            return release(reason) if calls > 1 else []

        guard.release_all = retry_only
        return presence, guard, tracked, arm

    monkeypatch.setattr(verify, "_instrument", missing_first)
    result = verify._run_case("outside-15ms", backend, child)
    first_halt = result["timeline"][2]
    assert first_halt["halted"] and result["latch_refused"]
    assert len(result["releases"]) == 1
    assert result["releases"][0]["guard_call_id"] == result["latch_call_id"]
    assert result["releases"][0]["guard_call_id"] != first_halt["guard_call_id"]
    assert result["status"] == "FAIL"
    assert "observation/halt invariant" in result["reason"]


@pytest.mark.parametrize("alter", ["wrong-call", "before-entry", "after-return"])
def test_release_must_belong_to_and_finish_inside_each_guard_call(rig, alter):
    _, backend, child = rig
    result = verify._run_case("outside-15ms", backend, child)
    assert result["status"] == "PASS"
    row = result["timeline"][2]
    release = result["releases"][0]
    if alter == "wrong-call":
        release["guard_call_id"] = result["latch_call_id"]
    elif alter == "before-entry":
        release["at_ns"] = row["guard_entry_ns"] - 1
    else:
        release["at_ns"] = row["guard_return_ns"] + 1
    verify._assess(result, 5)
    assert result["status"] == "FAIL"


def test_quiet_first_emission_delay_does_not_shorten_acknowledged_span(rig):
    _, backend, child = rig
    backend.first_delay_s = 1
    result = verify._run_case("quiet", backend, child)
    assert result["status"] == "PASS"
    rows = result["timeline"][1:]
    assert rows[0]["emit_after_ns"] - rows[0]["emit_before_ns"] >= 1_000_000_000
    assert rows[-1]["emit_after_ns"] - rows[0]["emit_after_ns"] >= 6_000_000_000
    verify._quiet_timing(rows, complete=True)


@pytest.mark.parametrize("wake_adjustment", [-0.030, 0.100])
def test_quiet_early_or_late_scheduler_invalidates_without_catchup(
    rig, monkeypatch, wake_adjustment
):
    clock, backend, child = rig
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock.advance(max(0, seconds + wake_adjustment))

    monkeypatch.setattr(verify.time, "sleep", sleep)
    result = verify._run_case("quiet", backend, child)
    assert result["status"] == "INVALID"
    assert "cadence" in result["reason"]
    assert len(sleeps) == 1 and len(backend.writes) == 2
    assert child.calls == 0


def test_quiet_bounded_lateness_never_causes_a_catchup_burst(rig, monkeypatch):
    clock, backend, child = rig
    monkeypatch.setattr(
        verify.time, "sleep", lambda seconds: clock.advance(seconds + 0.010)
    )
    result = verify._run_case("quiet", backend, child)
    assert result["status"] == "PASS"
    rows = result["timeline"][1:]
    assert all(
        current["emit_before_ns"] - previous["emit_after_ns"] >= 70_000_000
        for previous, current in zip(rows, rows[1:], strict=False)
    )
    assert rows[-1]["emit_after_ns"] - rows[0]["emit_after_ns"] >= 7_000_000_000


def test_quiet_full_row_count_cannot_qualify_compressed_actual_span(rig):
    _, backend, child = rig
    result = verify._run_case("quiet", backend, child)
    assert result["status"] == "PASS"
    first = result["timeline"][1]["emit_after_ns"]
    for i, row in enumerate(result["timeline"][1:]):
        row["emit_after_ns"] = first + i * 50_000_000
        row["emit_before_ns"] = row["emit_after_ns"] - 1_100_000
    assert len(result["timeline"][1:]) == 101
    with pytest.raises(verify.InvalidCase, match="cadence|span"):
        verify._assess(result, 5)


@pytest.mark.parametrize("case", ["quiet", "inside-2ms", "outside-15ms", "post-sample"])
def test_hooks_without_any_own_counter_registration_never_qualify(rig, case):
    _, backend, child = rig
    backend.register = False
    result = verify._run_case(case, backend, child)
    assert result["status"] == "INVALID"
    assert "own counter registration" in result["reason"]
    assert len(backend.writes) == 1 and child.calls == 0


@pytest.mark.parametrize("stop_after", [1, 50, 100])
def test_quiet_counter_registration_is_required_throughout_span(
    rig, monkeypatch, stop_after
):
    _, backend, child = rig
    type_text = backend.type_text

    def stop_registering(text, guard=None):
        if len(backend.writes) >= stop_after:
            backend.register = False
        return type_text(text, guard)

    monkeypatch.setattr(backend, "type_text", stop_registering)
    result = verify._run_case("quiet", backend, child)
    assert result["status"] == "INVALID"
    assert "own counter registration" in result["reason"]
    assert len(backend.writes) == stop_after + 1


def test_own_counter_read_does_not_sample_monitor_and_remains_bounded(rig):
    _, backend, child = rig
    result = verify._run_case("quiet", backend, child)
    assert result["status"] == "PASS"
    assert len(result["timeline"]) == 102  # one baseline + 101 guarded units
    assert len([row for row in result["timeline"] if "own_counter" in row]) == 101
    assert child.calls == 0


def test_barrier_own_advance_unresolvable_at_counter_quantisation_is_invalid(rig):
    _, backend, child = rig
    backend.unit_s = 0.00001
    result = verify._run_case("post-sample", backend, child)
    assert result["status"] == "INVALID"
    assert "own counter registration" in result["reason"]
    assert child.calls == 1


@pytest.mark.parametrize(
    "change", ["missing", "straddle", "sample-overlap", "backwards", "wrong-side"]
)
def test_placement_requires_complete_intervals(change):
    prior = {"emit_after_ns": 1_000_000, "injection_anchor_ns": 1_000_010}
    sample = {"sample_entry_ns": 60_000_000, "sample_return_ns": 60_100_000}
    human = {"send_before_ns": 8_000_000, "sync_after_ns": 8_100_000}
    assert verify.placement("outside-8ms", prior, sample, human, 5) == "OUTSIDE"
    if change == "missing":
        human.pop("sync_after_ns")
    elif change == "straddle":
        human.update(send_before_ns=5_900_000, sync_after_ns=6_100_000)
    elif change == "sample-overlap":
        human["sync_after_ns"] = 60_050_000
    elif change == "backwards":
        human["send_before_ns"] = 9_000_000
    else:
        human.update(send_before_ns=2_000_000, sync_after_ns=2_100_000)
    assert verify.placement("outside-8ms", prior, sample, human, 5) == "INVALID"


def test_boundary_runner_owns_cleanup_and_has_no_retries(rig, monkeypatch):
    from amplifier_module_tool_computer_use import linux_x11

    _, backend, child = rig
    monkeypatch.setattr(linux_x11, "LinuxX11Backend", lambda config: backend)
    monkeypatch.setattr(verify, "EventChild", lambda display: child)
    report = {}
    verify._run_boundary(report, ":fake-owned")
    assert report["status"] == verify.MECHANISM_CONTROLS_PASS
    assert [r["case"] for r in report["cases"]] == list(verify.CASES)
    assert child.calls == 5 and backend.closed
    assert report["backend_close_returned"] and report["child_cleanup"]["reaped"]


def test_invalid_first_case_stops_run_and_closes_resources(rig, monkeypatch):
    from amplifier_module_tool_computer_use import linux_x11

    _, backend, child = rig
    monkeypatch.setattr(linux_x11, "LinuxX11Backend", lambda config: backend)
    monkeypatch.setattr(verify, "EventChild", lambda display: child)
    monkeypatch.setattr(
        verify, "_run_case", lambda case, *_args: {"case": case, "status": "INVALID"}
    )
    report = {}
    verify._run_boundary(report, ":fake-owned")
    assert report["status"] == "UNQUALIFIED"
    assert len(report["cases"]) == 1 and backend.closed
    assert child.cleanup["reaped"]


def test_ready_failure_closes_child_and_backend(rig, monkeypatch):
    from amplifier_module_tool_computer_use import linux_x11

    _, backend, child = rig
    monkeypatch.setattr(linux_x11, "LinuxX11Backend", lambda config: backend)
    monkeypatch.setattr(verify, "EventChild", lambda display: child)
    monkeypatch.setattr(
        child, "ready", lambda: (_ for _ in ()).throw(verify.InvalidCase("not ready"))
    )
    report = {}
    with pytest.raises(verify.InvalidCase, match="not ready"):
        verify._run_boundary(report, ":fake-owned")
    assert backend.closed and child.cleanup["reaped"]


def test_ipc_bounded_messages_eof_and_matching_ids(monkeypatch):
    read_fd, write_fd = os.pipe()
    try:
        verify._send_message(write_fd, {"id": 0, "kind": "ready"})
        assert verify._read_message(read_fd, verify.time.monotonic() + 1) == {
            "id": 0,
            "kind": "ready",
        }
        os.write(write_fd, b"x" * (verify.MAX_MESSAGE + 1))
        with pytest.raises(verify.InvalidCase, match="exceeds"):
            verify._read_message(read_fd, verify.time.monotonic() + 1)
    finally:
        os.close(write_fd)
        os.close(read_fd)
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    try:
        with pytest.raises(verify.InvalidCase, match="EOF"):
            verify._read_message(read_fd, verify.time.monotonic() + 1)
    finally:
        os.close(read_fd)
    child = object.__new__(verify.EventChild)
    child.sequence = 0
    child.proc = SimpleNamespace(stdin=SimpleNamespace(fileno=lambda: 0))
    monkeypatch.setattr(verify, "_send_message", lambda *_args: None)
    monkeypatch.setattr(child, "_receive", lambda: {"id": 2, "kind": "fired"})
    with pytest.raises(verify.InvalidCase, match="sequence"):
        child.fire()


def test_ipc_deadline_does_not_retry():
    read_fd, write_fd = os.pipe()
    try:
        with pytest.raises(verify.InvalidCase, match="deadline"):
            verify._read_message(read_fd, verify.time.monotonic() - 1)
    finally:
        os.close(write_fd)
        os.close(read_fd)


@pytest.mark.parametrize("fault", [None, "command-id", "sync"])
def test_event_child_protocol_owns_connection_and_one_event_per_command(
    monkeypatch, fault
):
    # No real Xlib/display/process: exercise the child protocol with fake OS
    # boundaries, including finally cleanup when command or connection fails.
    events, messages = [], []
    connection = SimpleNamespace(syncs=0, closed=False)

    def sync():
        connection.syncs += 1
        if fault == "sync" and connection.syncs == 2:
            raise OSError("sync failed")

    connection.sync = sync
    connection.close = lambda: setattr(connection, "closed", True)
    fake_xtest = SimpleNamespace(fake_input=lambda *a, **kw: events.append((a, kw)))
    fake_xlib = SimpleNamespace(
        X=SimpleNamespace(MotionNotify=6),
        display=SimpleNamespace(Display=lambda name: connection),
    )
    monkeypatch.setitem(sys.modules, "Xlib", fake_xlib)
    monkeypatch.setitem(sys.modules, "Xlib.ext", SimpleNamespace(xtest=fake_xtest))
    commands = iter({"id": i, "kind": "fire"} for i in range(1, 6))
    monkeypatch.setattr(
        verify,
        "_read_message",
        lambda *_args: (
            {"id": 9, "kind": "fire"} if fault == "command-id" else next(commands)
        ),
    )
    monkeypatch.setattr(
        verify, "_send_message", lambda _fd, message: messages.append(message)
    )
    if fault:
        with pytest.raises((verify.InvalidCase, OSError)):
            verify._event_child(":fake-only")
    else:
        verify._event_child(":fake-only")
        assert len(events) == 5 and connection.syncs == 6
        assert [m["id"] for m in messages] == list(range(6))
        assert all(m["send_before_ns"] <= m["sync_after_ns"] for m in messages[1:])
    assert messages[0] == {"id": 0, "kind": "ready"}
    assert connection.closed


def test_child_cleanup_reaps_owned_process_and_caps_stderr():
    class Proc:
        stdin = io.BytesIO()
        stdout = io.BytesIO()
        stderr = io.BytesIO(b"x" * 9000)
        returncode = None
        waits = 0
        terminated = False

        def wait(self, timeout):
            self.waits += 1
            if self.waits == 1:
                raise verify.subprocess.TimeoutExpired("owned", timeout)
            self.returncode = -15

        def terminate(self):
            self.terminated = True

    child = object.__new__(verify.EventChild)
    child.proc, child.errors = Proc(), bytearray()
    child.close()
    assert child.proc.terminated and child.proc.waits == 2
    assert child.cleanup["reaped"] and child.cleanup["forced"]
    assert len(child.cleanup["stderr_prefix"]) == 4096


def test_original_89_remains_failed_and_model_nonrejection_cannot_clear_it():
    assert not verify.legacy_policy_passes(100, 11, 0, True)
    assert verify.legacy_policy_passes(100, 3, 0, True)
    assert not verify.legacy_policy_passes(100, 3, 1, True)
    assert not verify.legacy_policy_passes(100, 3, 0, False)
    result = verify.random_model_report(100, 11)
    assert result["conditional_p_value"] == pytest.approx(0.210558032669744)
    assert result["conditional_model_result"] == "NOT_REJECTED"
    assert (
        result["status"] == "UNSUPPORTED" and result["qualification"] == "UNQUALIFIED"
    )


def test_exact_tail_fixed_alpha_size_and_power():
    n, q = 100, 5 / 60
    critical = next(k for k in range(101) if verify.binomial_tail(n, k, q) <= 0.05)
    assert verify.binomial_tail(n, critical - 1, q) > 0.05
    assert verify.binomial_tail(n, critical, q) <= 0.05
    assert 0.94 < verify.binomial_tail(n, critical, 0.20) < 0.97
    assert verify.binomial_tail(100, 0, q) == pytest.approx(1)
    assert verify.binomial_tail(100, 100, 0) == 0
    assert verify.binomial_tail(100, 100, 1) == 1
    assert (
        verify.random_model_report(100, 20)["conditional_model_result"]
        == "MODEL_REJECTED"
    )
    with pytest.raises(ValueError):
        verify.binomial_tail(100, 101, q)


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--output", "unused.json"],
        ["--mode", "random-model", "--output", "unused.json"],
        ["--mode", "random-model", "--misses", "101", "--output", "unused.json"],
    ],
)
def test_cli_invalid_arguments_never_begin_native_work(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["verify_coexistence.py", *argv])
    monkeypatch.setattr(
        verify, "_run_boundary", lambda *_args: pytest.fail("must not begin")
    )
    with pytest.raises(SystemExit) as exc:
        verify.main()
    assert exc.value.code == 2


def test_offline_receipt_identity_and_refuses_replacement(monkeypatch, tmp_path):
    output = tmp_path / "model.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_coexistence.py",
            "--mode",
            "random-model",
            "--misses",
            "11",
            "--output",
            str(output),
        ],
    )
    monkeypatch.setattr(
        verify,
        "_run_boundary",
        lambda *_args: pytest.fail("offline must not run native"),
    )
    assert verify.main() == 2
    first = output.read_bytes()
    report = json.loads(first)
    assert report["schema"] == verify.SCHEMA and report["policy"] == verify.POLICY
    assert report["run_id"] and len(report["source_sha256"]) == 5
    assert report["status"] == "UNSUPPORTED"
    with pytest.raises(FileExistsError):
        verify.main()
    assert output.read_bytes() == first


def test_boundary_default_and_setup_failure_receipt(monkeypatch, tmp_path):
    output = tmp_path / "boundary.json"
    seen = []

    def failed_setup(report, display):
        seen.append((report["mode"], display))
        assert json.loads(output.read_text())["status"] == "INCOMPLETE"
        raise verify.InvalidCase("backend unavailable")

    monkeypatch.setattr(verify, "_run_boundary", failed_setup)
    monkeypatch.setattr(
        sys,
        "argv",
        ["verify_coexistence.py", "--display", ":owned", "--output", str(output)],
    )
    assert verify.main() == 2
    assert seen == [("boundary", ":owned")]
    report = json.loads(output.read_text())
    assert (
        report["status"] == "UNQUALIFIED" and "backend unavailable" in report["error"]
    )


@pytest.mark.parametrize(
    "status,exit_code",
    [(verify.MECHANISM_CONTROLS_PASS, 0), ("PASS", 2), ("UNQUALIFIED", 2)],
)
def test_cli_zero_exit_only_for_scoped_mechanism_verdict(
    monkeypatch, tmp_path, status, exit_code
):
    output = tmp_path / "conditional.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["verify_coexistence.py", "--display", ":fake-only", "--output", str(output)],
    )
    monkeypatch.setattr(
        verify, "_run_boundary", lambda report, _display: report.update(status=status)
    )
    assert verify.main() == exit_code
    assert json.loads(output.read_text())["status"] == status
