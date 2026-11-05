#!/usr/bin/env python3
"""Conditional coexistence qualification, policy coexistence-boundary-v3 (2026-11-04).

Default `--mode boundary` needs an explicitly owned Linux X11 display. It runs
five required controls and one separate diagnostic once, using the shipped backend,
monitor and guard and one independent, readiness-acknowledged XTEST child.
Requested delays are NOT
placement evidence. Invalid evidence blocks qualification; there are no retries.
Only a fully evidenced shared-tick post-sample diagnostic may report an attribution
limit instead of distinct counter advancement. Safety violations always block.

`--mode random-model --misses K` is OFFLINE analysis of supplied counts only.
Its exact binomial tail is conditional on assumptions it cannot establish; it
never grants safety PASS. The historical 89/100 remains FAILED under its original
fixed-rate policy. Neither mode proves detection of every physical input event.

Always supply --output NEW.json. No display is provisioned, no persistent halt is
read or cleared, and no production threshold, pacing or diagnostics are modified.
An external supervisor must bound native Xlib calls (which can block in the OS).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import select
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "modules" / "tool-computer-use"))
POLICY = "coexistence-boundary-v3"
SCHEMA = "coexistence-qualification-v1"
CADENCE_S = 0.060  # O5 reference cadence; production type_text has no such pacing.
WINDOW_S = 6.0
QUIET_MAX_GAP_S = 0.080  # Fixed harness scheduling limit, not a production bound.
MECHANISM_CONTROLS_PASS = "MECHANISM_CONTROLS_PASS"
REQUIRED_CASES = (
    "quiet",
    "inside-2ms",
    "outside-8ms",
    "outside-15ms",
    "outside-55ms",
)
DIAGNOSTIC_CASES = ("post-sample",)
CASES = REQUIRED_CASES + DIAGNOSTIC_CASES
IPC_TIMEOUT_S = 3.0
MAX_MESSAGE = 4096
RECEIVER_CAPTURE_LIMIT = 16  # Evidence cap, never an input allowance.
MAPPING_BATCH_LIMIT = 8
MAPPING_RUN_LIMIT = 64
INPUT_TYPES = (2, 3, 4, 5, 6)


class InvalidCase(RuntimeError):
    """Missing or unplaceable evidence, not a detection miss."""

    def __init__(self, message, evidence=None):
        super().__init__(message)
        self.evidence = evidence


class UnexpectedInput(InvalidCase):
    """Positive bounded receiver evidence, distinct from a broken read."""

    def __init__(self, message, batch=(), count=None):
        super().__init__(message)
        self.evidence = {
            "count": len(batch) if count is None else count,
            "batch": batch,
        }


class SharedServerTick(InvalidCase):
    """Valid timestamp consistency, but no evidence of a distinct reset."""


def _sync_requests():
    """Private read-only SYNC binding; python-xlib 0.33 has no SYNC module.

    Wire definitions: xorgproto/include/X11/extensions/syncproto.h, opcodes 0/1/5.
    Xlib uses the client's native byte order, including for the variable payload.
    No extension registration or production backend mutation.
    """
    from Xlib.protocol import rq

    class Initialize(rq.ReplyRequest):
        _request = rq.Struct(
            rq.Card8("opcode"),
            rq.Opcode(0),
            rq.RequestLength(),
            rq.Card8("major"),
            rq.Card8("minor"),
            rq.Pad(2),
        )
        _reply = rq.Struct(
            rq.Pad(2),
            rq.Card16("sequence_number"),
            rq.Card32("length"),
            rq.Card8("major"),
            rq.Card8("minor"),
            rq.Pad(22),
        )

    class ListCounters(rq.ReplyRequest):
        _request = rq.Struct(rq.Card8("opcode"), rq.Opcode(1), rq.RequestLength())
        _reply = rq.Struct(
            rq.Pad(2),
            rq.Card16("sequence_number"),
            rq.Card32("length"),
            rq.Int32("count"),
            rq.Pad(20),
            rq.Binary("payload"),
        )

    class Query(rq.ReplyRequest):
        _request = rq.Struct(
            rq.Card8("opcode"),
            rq.Opcode(5),
            rq.RequestLength(),
            rq.Card32("counter"),
        )
        _reply = rq.Struct(
            rq.Pad(2),
            rq.Card16("sequence_number"),
            rq.Card32("length"),
            rq.Int32("hi"),
            rq.Card32("lo"),
            rq.Pad(16),
        )

    return Initialize, ListCounters, Query


def _server_counter(count: int, payload: bytes) -> tuple[int, int]:
    """Parse 14-byte records, each name AND record padded jointly to four bytes."""
    if not 0 <= count <= 256 or len(payload) > 65536:
        raise InvalidCase("invalid SYNC counter list size")
    found = []
    offset = 0
    for _ in range(count):
        if len(payload) - offset < 14:
            raise InvalidCase("truncated SYNC counter record")
        counter, hi, lo, size = struct.unpack_from("=IiIH", payload, offset)
        end = offset + 14 + size
        padded = (end + 3) & ~3
        if padded > len(payload):
            raise InvalidCase("truncated SYNC counter name")
        if payload[offset + 14 : end] == b"SERVERTIME":
            found.append((counter, (hi << 32) + lo))
        offset = padded
    if offset != len(payload) or len(found) != 1 or found[0][0] == 0:
        raise InvalidCase("missing/ambiguous SYNC SERVERTIME")
    return found[0]


class ServerClock:
    def __init__(self, connection):
        initialize, counters, self.query = _sync_requests()
        extension = connection.query_extension("SYNC")
        if extension is None or not extension.present:
            raise InvalidCase("SYNC unavailable; no server-clock registration oracle")
        self.display = connection.display
        self.opcode = extension.major_opcode
        version = initialize(display=self.display, opcode=self.opcode, major=3, minor=1)
        if (version.major, version.minor) != (3, 1) or version.length != 0:
            raise InvalidCase("requires SYNC 3.1")
        listing = counters(display=self.display, opcode=self.opcode)
        if listing.length * 4 != len(listing.payload):
            raise InvalidCase("invalid SYNC counter payload length")
        self.counter, resolution = _server_counter(listing.count, listing.payload)
        if resolution <= 0:
            raise InvalidCase("invalid SERVERTIME resolution")
        self.identity = {
            "extension": "SYNC",
            "version": [3, 1],
            "counter": self.counter,
            "name": "SERVERTIME",
            "resolution_ms": resolution,
            "registration": "server-clock-v2",
        }
        self.first = self.last = None

    def read(self):
        reply = self.query(
            display=self.display,
            opcode=self.opcode,
            counter=self.counter,
        )
        value = (reply.hi << 32) + reply.lo
        if (
            reply.length != 0
            or value < 0
            or (
                self.last is not None
                and (value < self.last or value - self.first > 90_000)
            )
        ):
            # Includes rollback masquerading as a 32-bit epoch increment and
            # signed-64 wrap. A genuine 32-bit rollover in a <=90s run is fine.
            raise InvalidCase("SERVERTIME regressed/wrapped or exceeded 90s bound")
        if self.first is None:
            self.first = value
        self.last = value
        return value


def _event_time(timestamp: int, server_now: int) -> int:
    if type(timestamp) is not int or not 0 <= timestamp < 2**32:
        raise InvalidCase("invalid native event timestamp")
    age = (server_now - timestamp) % 2**32
    if age >= 2**31:
        raise InvalidCase("native event future/ambiguous at server-clock wrap")
    return server_now - age


class EventReceiver:
    """Independent connection and owned window, never a grab or an emitter."""

    def __init__(self, display_name):
        from Xlib import XK, X
        from Xlib import display as xlib_display

        self.conn = xlib_display.Display(display_name)
        self.window = None
        self.focus = None
        self.ordinal = 0
        self.event_sequence = 0
        self.mapping_notifications = 0
        self.last_drain = None
        self.armed = False
        self.cleanup = {}
        try:
            setup_errors = []

            def setup_error(error, _request):
                setup_errors.append(repr(error))
                return True

            self.focus = self.conn.get_input_focus()
            pairs = list(self.conn.keysym_to_keycodes(XK.string_to_keysym("a")))
            if not pairs or min(pairs, key=lambda p: p[1])[1] != 0:
                raise InvalidCase("receiver requires existing unshifted 'a' keymap")
            self.keycode = min(pairs, key=lambda p: p[1])[0]
            self.window = self.conn.screen().root.create_window(
                0,
                0,
                64,
                64,
                0,
                X.CopyFromParent,
                X.InputOutput,
                X.CopyFromParent,
                override_redirect=True,
                event_mask=X.KeyPressMask
                | X.KeyReleaseMask
                | X.ButtonPressMask
                | X.ButtonReleaseMask
                | X.PointerMotionMask,
            )
            self.window.map(onerror=setup_error)
            self.window.configure(stack_mode=X.Above, onerror=setup_error)
            self.window.set_input_focus(
                X.RevertToParent,
                X.CurrentTime,
                onerror=setup_error,
            )
            self.conn.sync()
            if setup_errors or self.conn.get_input_focus().focus != self.window:
                raise InvalidCase("receiver focus not established")
            # Structural setup only. No warmup input or queue-discard fallback.
            self.empty()
        except BaseException:
            self.close()
            raise

    def _mapping(self, event, summary):
        """X11 §11 MappingNotify is global and cannot be deselected.

        https://www.x.org/releases/current/doc/xproto/x11protocol.html
        Only this documented non-input event is accepted. Keyboard ranges are
        validated against setup; Modifier/Pointer's unused CARD8 fields are
        retained, not interpreted as input. No SendEvent notification is trusted.
        """
        from Xlib import XK, X
        from Xlib.protocol.event import MappingNotify

        request, first, count = (
            summary["request"],
            summary["first_keycode"],
            summary["count"],
        )
        if (
            not isinstance(event, MappingNotify)
            or summary["send_event"] is not False
            or type(request) is not int
            or request not in (X.MappingModifier, X.MappingKeyboard, X.MappingPointer)
            or any(type(v) is not int or not 0 <= v <= 255 for v in (first, count))
        ):
            raise InvalidCase("malformed/SendEvent MappingNotify")
        if request == X.MappingKeyboard:
            info = self.conn.display.info
            if (
                count == 0
                or first < info.min_keycode
                or first + count - 1 > info.max_keycode
            ):
                raise InvalidCase("invalid MappingNotify keyboard range")
        # python-xlib 0.33 refreshes the cached keyboard range for Keyboard,
        # and explicitly does nothing for Modifier/Pointer. Refresh first, then
        # require the original oracle keycode AND its current level-zero symbol.
        self.conn.refresh_keyboard_mapping(event)
        symbol = XK.string_to_keysym("a")
        pairs = list(self.conn.keysym_to_keycodes(symbol))
        current = self.conn.get_keyboard_mapping(self.keycode, 1)
        if (
            not pairs
            or min(pairs, key=lambda p: (p[1], p[0])) != (self.keycode, 0)
            or len(current) != 1
            or not current[0]
            or current[0][0] != symbol
        ):
            raise InvalidCase("MappingNotify changed expected unshifted 'a' keycode")
        summary["mapping_validated"] = True

    def _drain(self):
        # One fixed snapshot and bounded prefix, not a drain-until-empty loop.
        # Retain raw evidence BEFORE classifying/refreshing anything. A later
        # mapping error cannot conceal positive native input during refusal.
        captured, batch, native = [], [], []
        evidence = self.last_drain = {
            "pending_count": None,
            "capture_limit": RECEIVER_CAPTURE_LIMIT,
            "batch": captured,
            "omitted_count": None,
            "truncated": False,
            "remaining_count": None,
            "mapping_count": 0,
            "type_counts": {},
            "input_count": 0,
        }
        try:
            self.conn.sync()
            count = self.conn.pending_events()
            evidence["pending_count"] = _event_scalar(count)
            if type(count) is not int or count < 0:
                raise InvalidCase("invalid receiver queue count")
            evidence.update(
                omitted_count=count, truncated=count > RECEIVER_CAPTURE_LIMIT
            )
            for _ in range(min(count, RECEIVER_CAPTURE_LIMIT)):
                event = self.conn.next_event()
                self.event_sequence += 1
                summary = _event_summary(event)
                summary["sequence"] = self.event_sequence
                summary["ordinal"] = None
                if type(summary["type"]) is int and summary["type"] in INPUT_TYPES:
                    self.ordinal += 1
                    summary["ordinal"] = self.ordinal
                    batch.append(summary)
                    evidence["input_count"] += 1
                captured.append(summary)
                native.append(event)
                evidence["omitted_count"] -= 1
                kind = str(summary["type"])
                evidence["type_counts"][kind] = evidence["type_counts"].get(kind, 0) + 1
            evidence["remaining_count"] = self.conn.pending_events()
            if evidence["truncated"] or evidence["remaining_count"]:
                raise InvalidCase("receiver evidence capture exceeds bound/new traffic")
            mappings = [e for e in captured if e["type"] == 34]
            evidence["mapping_count"] = len(mappings)
            self.mapping_notifications += len(mappings)
            evidence["mapping_run_count"] = self.mapping_notifications
            if (
                len(mappings) > MAPPING_BATCH_LIMIT
                or self.mapping_notifications > MAPPING_RUN_LIMIT
            ):
                raise InvalidCase("receiver MappingNotify budget exceeds bound")
            if len(batch) > 3:
                raise InvalidCase("receiver input batch exceeds bound")
            for event, summary in zip(native, captured, strict=True):
                if summary["type"] == 34:
                    self._mapping(event, summary)
                elif (
                    type(summary["type"]) is not int
                    or summary["type"] not in INPUT_TYPES
                ):
                    raise InvalidCase("unexpected non-input receiver event")
            # Mapping refresh performs read-only queries. Traffic arriving during
            # them is not silently consumed or deferred into a positive match.
            evidence["remaining_count"] = self.conn.pending_events()
            if evidence["remaining_count"]:
                raise InvalidCase("new receiver traffic during mapping validation")
        except Exception as exc:
            evidence["error"] = repr(exc)[:MAX_MESSAGE]
            if evidence["omitted_count"]:
                evidence["truncated"] = True
            if batch:
                error = UnexpectedInput(
                    "receiver rejected batch containing native input", batch
                )
                error.evidence["receiver"] = evidence
                raise error from exc
            raise InvalidCase(f"receiver evidence invalid: {exc}", evidence) from exc
        return batch

    def empty(self):
        batch = self._drain()
        if batch:
            if any(e["type"] in (2, 3, 4, 5, 6) for e in batch):
                raise UnexpectedInput(
                    "stale/unexpected receiver input before arming", batch
                )
            raise InvalidCase("unexpected non-input receiver event")

    def arm(self):
        if self.armed:
            raise InvalidCase("receiver already armed")
        self.empty()
        self.armed = True

    def consume(self, kind, detail=None):
        if not self.armed:
            raise InvalidCase("receiver batch not armed/replayed")
        self.armed = False  # even a rejected batch cannot be replayed
        batch = self._drain()
        return _match_batch(batch, kind, self.window.id, self.keycode, detail)

    def close(self):
        errors = []

        def native_error(error, _request):
            errors.append(f"receiver cleanup request: {error!r}")
            return True

        try:
            if self.focus is not None:
                try:
                    from Xlib import X

                    # Restore only the saved valid focus; no guessed window.
                    focus = self.focus.focus
                    if hasattr(focus, "get_attributes"):
                        focus.get_attributes()
                    self.conn.set_input_focus(
                        focus,
                        self.focus.revert_to,
                        X.CurrentTime,
                        onerror=native_error,
                    )
                    self.conn.sync()
                except Exception as exc:
                    errors.append(f"focus restore: {exc!r}")
        finally:
            try:
                if self.window is not None:
                    self.window.destroy(onerror=native_error)
                    self.conn.sync()
            except Exception as exc:
                errors.append(f"window destroy: {exc!r}")
            finally:
                try:
                    self.conn.close()
                except Exception as exc:
                    errors.append(f"receiver close: {exc!r}")
        self.cleanup = {
            "closed": not errors,
            "window_id": getattr(self.window, "id", None),
            "last_ordinal": self.ordinal,
            "last_event_sequence": self.event_sequence,
            "mapping_notifications": self.mapping_notifications,
            "errors": errors,
        }


def _event_scalar(value):
    """Only bounded protocol scalars; never arbitrary event data or repr payload."""
    return value if value is None or type(value) in (int, bool) else "<invalid>"


def _event_summary(event):
    fields = {
        "type": "type",
        "detail": "detail",
        "x": "event_x",
        "y": "event_y",
        "server_ms": "time",
        "send_event": "send_event",
        "request": "request",
        "first_keycode": "first_keycode",
        "count": "count",
    }
    return {
        **{
            key: _event_scalar(getattr(event, attr, None))
            for key, attr in fields.items()
        },
        "name": type(event).__name__[:64],
        "window": _event_scalar(getattr(getattr(event, "window", None), "id", None)),
        "received_ns": time.monotonic_ns(),
    }


def _match_batch(batch, kind, window, keycode, detail=None):
    expected = {"own": [2, 3], "child": [6], "none": []}[kind]
    if kind == "none" and any(e["type"] in (2, 3, 4, 5, 6) for e in batch):
        raise UnexpectedInput("ordinary native input during refusal/activity", batch)
    if [e["type"] for e in batch] != expected:
        raise InvalidCase(f"{kind} native delivery missing/extra/wrong input")
    for event in batch:
        if event["window"] != window or event["send_event"] is not False:
            raise InvalidCase("foreign-window or SendEvent delivery")
        if kind == "own" and event["detail"] != keycode:
            raise InvalidCase("wrong native key")
        if kind == "child" and (
            event["detail"] != 0 or (event["x"], event["y"]) != (7 + detail, 7)
        ):
            raise InvalidCase("wrong native child motion")
        if type(event["server_ms"]) is not int:
            raise InvalidCase("missing native server timestamp")
    if any(
        b["ordinal"] != a["ordinal"] + 1 for a, b in zip(batch, batch[1:], strict=False)
    ):
        raise InvalidCase("repeated/replayed receiver ordinal")
    return batch


class RegistrationOracle:
    def __init__(self, backend, receiver):
        self.backend, self.receiver = backend, receiver
        self.clock = ServerClock(backend._display)
        self.previous = None
        self.last_ordinal = 0

    def consume(self, kind, detail=None):
        batch = self.receiver.consume(kind, detail)
        if batch and batch[0]["ordinal"] != self.last_ordinal + 1:
            raise InvalidCase("replayed native delivery")
        if batch:
            self.last_ordinal = batch[-1]["ordinal"]
        return batch

    def confirm(self, counter, batch, prior=None):
        low, high = _server_interval(counter)
        if not batch:
            raise InvalidCase("registration requires native delivery")
        stamps = [
            _event_time(e["server_ms"], counter["server_after_ms"]) for e in batch
        ]
        if stamps != sorted(stamps) or not low <= stamps[-1] <= high:
            raise InvalidCase("native delivery inconsistent with idle counter")
        if (
            low == high == stamps[-1] == self.previous
            and prior is not None
            and _server_interval(prior)[1] < low
        ):
            raise SharedServerTick(
                "own counter registration: shared server tick cannot prove a distinct reset",
                {"shared_server_ms": low},
            )
        if (self.previous is not None and low <= self.previous) or (
            prior is not None and low <= _server_interval(prior)[1]
        ):
            raise InvalidCase(
                "counter registration did not advance; same-tick/ambiguous"
            )
        self.previous = stamps[-1]
        counter.update(corroborated=True, event_server_ms=stamps[-1])


def binomial_tail(n: int, k: int, q: float) -> float:
    """Exact one-sided P[X >= k]; no normal approximation or adjustable alpha."""
    if not 0 <= k <= n <= 1000 or not 0 <= q <= 1:
        raise ValueError("require 0 <= k <= n <= 1000 and 0 <= q <= 1")
    return math.fsum(
        math.comb(n, j) * q**j * (1 - q) ** (n - j) for j in range(k, n + 1)
    )


def legacy_policy_passes(
    n: int, misses: int, false_positives: int, released: bool
) -> bool:
    """Historical AND rule only; never used for current qualification."""
    q = 5 / 60
    return bool(
        n > 0
        and 0 <= misses <= n
        and false_positives == 0
        and released
        and n - misses > 0
        and (n - misses) / n >= 0.90
        and misses / n <= q + 2 * math.sqrt(q * (1 - q) / n)
    )


def random_model_report(n: int, misses: int) -> dict[str, Any]:
    p_value = binomial_tail(n, misses, 5 / 60)
    return {
        "status": "UNSUPPORTED",
        "qualification": "UNQUALIFIED",
        "trials": n,
        "misses": misses,
        "null": "q <= 5/60, independent uniform phases, negligible sample-to-write gap",
        "alpha": 0.05,
        "conditional_p_value": p_value,
        "conditional_model_result": "MODEL_REJECTED"
        if p_value <= 0.05
        else "NOT_REJECTED",
        "reason": "Counts alone do not establish realized phases, independence, event validity, "
        "false positives, release/latch behavior, or explain misses. This is not a safety gate.",
    }


def _read_message(
    fd: int,
    deadline: float,
    stderr_fd: int | None = None,
    errors: bytearray | None = None,
) -> dict[str, Any]:
    # Unbuffered, bounded lines: no reader threads, temporary files or outstanding commands.
    data = bytearray()
    while time.monotonic() < deadline:
        ready, _, _ = select.select(
            [fd] + ([stderr_fd] if stderr_fd is not None else []),
            [],
            [],
            max(0, deadline - time.monotonic()),
        )
        if stderr_fd in ready:
            chunk = os.read(stderr_fd, MAX_MESSAGE)
            if errors is not None:
                errors.extend(chunk[: max(0, MAX_MESSAGE - len(errors))])
            if not chunk:
                stderr_fd = None
        if fd not in ready:
            continue
        chunk = os.read(fd, 1)
        if not chunk:
            raise InvalidCase("child pipe EOF")
        data.extend(chunk)
        if len(data) > MAX_MESSAGE:
            raise InvalidCase("child message exceeds limit")
        if chunk == b"\n":
            result = json.loads(data)
            if not isinstance(result, dict):
                raise InvalidCase("child message is not an object")
            return result
    raise InvalidCase("child pipe deadline")


def _send_message(fd: int, message: dict[str, Any]) -> None:
    data = json.dumps(message, allow_nan=False).encode() + b"\n"
    if len(data) > MAX_MESSAGE or os.write(fd, data) != len(data):
        raise InvalidCase("incomplete/oversized child command")


def _event_child(display_name: str) -> None:
    from Xlib import X
    from Xlib import display as xlib_display
    from Xlib.ext import xtest

    conn = xlib_display.Display(display_name)
    try:
        conn.sync()
        _send_message(1, {"id": 0, "kind": "ready"})
        for expected_id in range(1, 6):
            command = _read_message(0, time.monotonic() + 30)
            if command != {"id": expected_id, "kind": "fire"}:
                raise InvalidCase("child command sequence mismatch")
            before = time.monotonic_ns()
            # Exactly one elementary event, at a different point each time.
            xtest.fake_input(conn, X.MotionNotify, x=7 + expected_id, y=7)
            conn.sync()
            after = time.monotonic_ns()
            _send_message(
                1,
                {
                    "id": expected_id,
                    "kind": "fired",
                    "send_before_ns": before,
                    "sync_after_ns": after,
                },
            )
    finally:
        conn.close()


class EventChild:
    def __init__(self, display_name: str) -> None:
        self.proc = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--event-child",
                "--display",
                display_name,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.sequence = 0
        self.errors = bytearray()
        self.cleanup: dict[str, Any] = {}

    def ready(self) -> None:
        if self._receive() != {"id": 0, "kind": "ready"}:
            raise InvalidCase("child readiness mismatch")

    def _receive(self) -> dict[str, Any]:
        assert self.proc.stdout is not None and self.proc.stderr is not None
        return _read_message(
            self.proc.stdout.fileno(),
            time.monotonic() + IPC_TIMEOUT_S,
            self.proc.stderr.fileno(),
            self.errors,
        )

    def fire(self) -> dict[str, Any]:
        self.sequence += 1
        assert self.proc.stdin is not None
        _send_message(self.proc.stdin.fileno(), {"id": self.sequence, "kind": "fire"})
        response = self._receive()
        if response.get("id") != self.sequence or response.get("kind") != "fired":
            raise InvalidCase("child response sequence mismatch")
        before, after = response.get("send_before_ns"), response.get("sync_after_ns")
        if (
            type(before) is not int
            or type(after) is not int
            or not 0 < before <= after <= time.monotonic_ns()
        ):
            raise InvalidCase("child timestamp interval invalid")
        return response

    def close(self) -> None:
        assert self.proc.stdin is not None and self.proc.stderr is not None
        self.proc.stdin.close()
        forced = False
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            forced = True
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)
        self.errors.extend(self.proc.stderr.read(MAX_MESSAGE - len(self.errors)))
        self.cleanup = {
            "reaped": self.proc.returncode is not None,
            "returncode": self.proc.returncode,
            "forced": forced,
            "stderr_prefix": self.errors.decode(errors="replace"),
        }
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is not None:
                stream.close()


def _quiet_baseline(snapshot: Any) -> None:
    from amplifier_module_tool_computer_use.presence import (
        QUIET_FLOOR_SECONDS,
        Confidence,
        PresenceState,
    )

    idle = snapshot.last_human_input_ago_ms
    if (
        snapshot.state is not PresenceState.QUIET
        or snapshot.confidence is not Confidence.HIGH
        or idle is None
        or not math.isfinite(idle)
        or idle <= QUIET_FLOOR_SECONDS * 1000
    ):
        raise InvalidCase(
            "baseline requires fresh QUIET/HIGH, finite idle > quiet floor"
        )


def _server_interval(counter):
    before, after, idle = (
        counter["server_before_ms"],
        counter["server_after_ms"],
        counter["idle_ms"],
    )
    if (
        type(before) is not int
        or type(after) is not int
        or not 0 <= before <= after
        or after - before > 90_000
        or not isinstance(idle, (int, float))
        or isinstance(idle, bool)
        or not math.isfinite(idle)
        or not 0 <= idle < 2**31
        or int(idle) != idle
    ):
        raise InvalidCase("invalid/noninteger/regressing server counter read")
    return before - int(idle), after - int(idle)


def _counter_read(backend: Any, destination: dict[str, Any], clock) -> tuple[int, int]:
    """Bracket the *actual* idle query in SERVERTIME, not Python monotonic."""
    destination["entry_ns"] = time.monotonic_ns()
    try:
        destination["server_before_ms"] = clock.read()
        idle = backend.presence_idle_ms()
        destination["idle_ms"] = (
            idle if isinstance(idle, (float, int)) and math.isfinite(idle) else None
        )
        destination["server_after_ms"] = clock.read()
    finally:
        destination["return_ns"] = time.monotonic_ns()
    return _server_interval(destination)


def _confirm_own(backend: Any, result: dict[str, Any], oracle, batch) -> None:
    row = result["timeline"][-1]
    counter = row["own_counter"] = {}
    _counter_read(backend, counter, oracle.clock)
    try:
        oracle.confirm(counter, batch, row["sample_counter"])
    except SharedServerTick:
        _validate_shared_barrier(result, oracle)
        raise
    except InvalidCase as exc:
        raise InvalidCase(f"own counter registration: {exc}") from exc


def _confirm_event(backend: Any, result: dict[str, Any], oracle) -> None:
    try:
        batch = oracle.consume("child", result["human"]["id"])
    finally:
        result["human"]["receiver_evidence"] = getattr(
            oracle.receiver, "last_drain", None
        )
    result["human"]["delivery"] = batch
    counter = result["event_counter"] = {}
    _counter_read(backend, counter, oracle.clock)
    oracle.confirm(counter, batch)


def _instrument(backend: Any, child: Any, result: dict[str, Any], oracle):
    """Harness-local wrappers only; production sample/guard/emit order is retained."""
    from amplifier_module_tool_computer_use.coexistence_guard import (
        CoexistenceGuard,
        HaltedError,
    )
    from amplifier_module_tool_computer_use.presence import PresenceMonitor

    rows = result["timeline"]
    releases = result["releases"]
    observer_error: list[str] = []
    armed = False
    active_row: dict[str, Any] | None = None
    sample_row: dict[str, Any] | None = None
    call_id = 0

    def idle_source():
        assert sample_row is not None
        counter = sample_row["sample_counter"] = {}
        _counter_read(backend, counter, oracle.clock)
        return counter["idle_ms"]

    class TimedPresence(PresenceMonitor):
        def sample(self, **kwargs):
            nonlocal sample_row
            row = active_row if active_row is not None else {}
            sample_row = row
            if active_row is None:
                rows.append(row)
            row["sample_entry_ns"] = time.monotonic_ns()
            try:
                snapshot = super().sample(**kwargs)
                row["snapshot"] = snapshot.to_dict()
                return snapshot
            finally:
                row["sample_return_ns"] = time.monotonic_ns()
                sample_row = None

    presence = TimedPresence(idle_source=idle_source, platform="linux-x11")

    def release(reason):
        releases.append(
            {
                "reason": reason,
                "at_ns": time.monotonic_ns(),
                "guard_call_id": active_row["guard_call_id"]
                if active_row is not None
                else None,
            }
        )
        # This checks callback invocation, NOT a real held-key ledger.
        return []

    def observer(_snapshot):
        nonlocal armed
        if armed:
            armed = False
            try:
                result["human"] = child.fire()
                _confirm_event(backend, result, oracle)
                oracle.receiver.arm()  # child consumed before the possible own pair
            except Exception as exc:
                # Production contains observer exceptions. Retain it and refuse
                # the harness's next emission after before_event returns.
                observer_error.append(repr(exc))
                result["barrier_receiver_evidence"] = getattr(
                    oracle.receiver, "last_drain", None
                )

    guard = CoexistenceGuard(
        presence=presence, release_all=release, on_presence_sample=observer
    )

    class TrackedGuard:
        def before_event(self):
            nonlocal active_row, call_id
            call_id += 1
            row = active_row = {
                "guard_call_id": call_id,
                "guard_entry_ns": time.monotonic_ns(),
            }
            rows.append(row)
            try:
                guard.before_event()
            except HaltedError:
                row["halted"] = True
                raise
            finally:
                row["guard_return_ns"] = time.monotonic_ns()
                active_row = None
            if "snapshot" not in row:
                raise InvalidCase("guard returned without a recorded presence sample")
            if observer_error:
                raise InvalidCase(f"barrier observer failed: {observer_error}")
            row["emit_before_ns"] = time.monotonic_ns()

        def after_event(self):
            row = rows[-1]
            row["emit_after_ns"] = time.monotonic_ns()  # backend sync already returned
            row["inject_before_ns"] = time.monotonic_ns()
            guard.after_event()
            row["inject_after_ns"] = time.monotonic_ns()
            row["injection_anchor_ns"] = round(presence._last_inject_monotonic * 1e9)

    def arm():
        nonlocal armed
        armed = True

    return presence, guard, TrackedGuard(), arm


def placement(
    case: str,
    prior: dict[str, Any],
    sample: dict[str, Any],
    human: dict[str, Any],
    guard_ms: float,
) -> str:
    """Conservative timing-interval proof. Nominal delay never enters this test."""
    try:
        anchor = prior["injection_anchor_ns"]
        before, after = human["send_before_ns"], human["sync_after_ns"]
        entry, returned = sample["sample_entry_ns"], sample["sample_return_ns"]
        values = (anchor, before, after, entry, returned, prior["emit_after_ns"])
        if any(type(value) is not int or value <= 0 for value in values):
            return "INVALID"
        if not prior["emit_after_ns"] <= anchor <= before <= after or entry > returned:
            return "INVALID"
        if case == "post-sample":
            return (
                "BARRIER"
                if returned <= before <= after <= sample["guard_return_ns"]
                else "INVALID"
            )
        if after > entry:  # event completion must precede the fresh observation
            return "INVALID"
        low_ms, high_ms = (before - anchor) / 1e6, (after - anchor) / 1e6
        if case.startswith("inside") and 0 <= low_ms <= high_ms <= guard_ms:
            return "INSIDE"
        if case.startswith("outside") and low_ms > guard_ms:
            return "OUTSIDE"
    except (KeyError, TypeError):
        pass
    return "INVALID"


def _halt_violation(result: dict[str, Any]) -> str | None:
    """Observations, not just exceptions, impose the invariant on every call."""
    if result.get("forbidden_native_input"):
        return "observation/halt invariant: native input after activity/refusal"
    rows = result["timeline"][1:]
    for i, row in enumerate(rows):
        if row.get("snapshot", {}).get("state") != "human_active" and not row.get(
            "halted"
        ):
            continue
        released_in_call = any(
            release.get("guard_call_id") == row["guard_call_id"]
            and release["reason"] == "halted"
            and row["guard_entry_ns"] <= release["at_ns"] <= row["guard_return_ns"]
            for release in result["releases"]
        )
        if (
            not row.get("halted")
            or not released_in_call
            or any(
                "emit_before_ns" in later or "emit_after_ns" in later
                for later in rows[i:]
            )
            or not result.get("latch_refused")
            or result.get("latch_call_id", -1) < row["guard_call_id"]
        ):
            return f"observation/halt invariant failed in guard call {row['guard_call_id']}"
    return None


def _quiet_timing(rows: list[dict[str, Any]], *, complete: bool = False) -> None:
    for previous, current in zip(rows, rows[1:], strict=False):
        if not (
            current["emit_before_ns"] - previous["emit_after_ns"]
            >= round(CADENCE_S * 1e9)
            and current["emit_after_ns"] - previous["emit_after_ns"]
            <= round(QUIET_MAX_GAP_S * 1e9)
        ):
            raise InvalidCase(
                "quiet acknowledged cadence outside declared 60..80ms bounds"
            )
    if complete and rows[-1]["emit_after_ns"] - rows[0]["emit_after_ns"] < round(
        WINDOW_S * 1e9
    ):
        raise InvalidCase("quiet acknowledged span below six seconds")


def _assess(result: dict[str, Any], guard_ms: float) -> None:
    rows = result["timeline"][1:]  # exclude the fresh pre-injection baseline
    violation = _halt_violation(result)
    if violation:
        result.update(status="FAIL", reason=violation)
        return
    if any(
        "emit_after_ns" in row and not row.get("own_counter", {}).get("corroborated")
        for row in rows
    ):
        raise InvalidCase("acknowledged own emission lacks counter corroboration")
    if result["case"] == "quiet":
        result["status"] = (
            "PASS"
            if len(rows) == 101
            and not any(r.get("halted") for r in rows)
            and all("emit_after_ns" in r for r in rows)
            else "FAIL"
        )
        if result["status"] == "PASS":
            _quiet_timing(rows, complete=True)
        return
    if len(rows) < 2 or "human" not in result or "emit_after_ns" not in rows[0]:
        raise InvalidCase("missing own-write/sample/event evidence")
    location = placement(result["case"], rows[0], rows[1], result["human"], guard_ms)
    result["placement"] = location
    if location == "INVALID":
        raise InvalidCase(
            "event interval unplaceable for predeclared control; no retry"
        )
    if location != "BARRIER":
        # Registration confirmation alone is insufficient if another input
        # overwrote the counter before the production sample. Its returned
        # observation must still overlap the independent event interval.
        low, high = _server_interval(rows[1]["sample_counter"])
        timestamp = result["event_counter"]["event_server_ms"]
        own_timestamp = rows[0]["own_counter"]["event_server_ms"]
        if not low <= timestamp <= high or low <= own_timestamp <= high:
            raise InvalidCase("fresh guard counter no longer places the child event")
    if location == "OUTSIDE":
        margin = rows[1]["snapshot"]["margin_ms"]
        # No circular conditioning: a time-proven outside event with a quiet
        # observation is counterevidence, not an excused inside-band trial.
        result["status"] = (
            "PASS"
            if rows[1].get("halted")
            and "emit_after_ns" not in rows[1]
            and margin is not None
            and margin > guard_ms
            else "FAIL"
        )
    elif location == "BARRIER":
        if rows[1]["snapshot"]["state"] == "human_active":
            result["status"] = "FAIL"
            result["reason"] = "human_active observation preceded the barrier event"
            return
        if (
            "emit_after_ns" in rows[1]
            and result["human"]["sync_after_ns"] > rows[1]["emit_before_ns"]
        ):
            raise InvalidCase("barrier did not finish before emission")
        result.update(status="OBSERVED", attribution="ATTRIBUTION_RESOLVED")
        result["limitation"] = (
            "Artificial IPC barrier; either outcome retained, not a natural blind-window measurement or a must-detect/must-miss assertion."
        )
    else:
        result["status"] = "PASS"  # masking OR earlier detection, never require a miss


def _validate_shared_barrier(result, oracle):
    """The sole diagnostic exception: two valid deliveries sharing one server tick.

    Called only after the ordinary oracle consumed/matched the own pair and
    rejected distinct advancement with SharedServerTick, never on arbitrary errors.
    No new input or counter read; all checks use this attempt's retained evidence.
    """
    from amplifier_module_tool_computer_use.presence import GUARD_MS

    if result.get("case") != "post-sample" or len(result["timeline"]) != 3:
        raise InvalidCase("shared tick outside the single post-sample diagnostic")
    baseline, first, current = result["timeline"]
    human, child_counter = result["human"], result["event_counter"]
    own_counter = current["own_counter"]
    tick, high = _server_interval(own_counter)
    child_tick = child_counter["event_server_ms"]
    if (
        _halt_violation(result)
        or not first["own_counter"].get("corroborated")
        or not child_counter.get("corroborated")
        or own_counter.get("corroborated")
        or not tick == high == child_tick == oracle.previous
        or not first["own_counter"]["event_server_ms"] < tick
        or type(human["id"]) is not int
        or not 1 <= human["id"] <= 5
        or placement("post-sample", first, current, human, GUARD_MS["linux-x11"])
        != "BARRIER"
    ):
        raise InvalidCase("incomplete/unsafe shared-tick diagnostic")
    batches = (
        _match_batch(
            first["delivery"], "own", oracle.receiver.window.id, oracle.receiver.keycode
        ),
        _match_batch(
            human["delivery"],
            "child",
            oracle.receiver.window.id,
            oracle.receiver.keycode,
            human["id"],
        ),
        _match_batch(
            current["delivery"],
            "own",
            oracle.receiver.window.id,
            oracle.receiver.keycode,
        ),
    )
    events = [event for batch in batches for event in batch]
    if any(
        b["ordinal"] != a["ordinal"] + 1
        for a, b in zip(events, events[1:], strict=False)
    ):
        raise InvalidCase("shared-tick diagnostic has missing/replayed delivery")
    if any(
        _event_time(e["server_ms"], own_counter["server_after_ms"]) != tick
        for e in batches[1] + batches[2]
    ):
        raise InvalidCase("shared-tick diagnostic has inconsistent native timestamps")
    for row in (baseline, first, current):
        sample = row["sample_counter"]
        _server_interval(sample)
        if (
            row["snapshot"]["state"] != "quiet"
            or row["snapshot"]["last_human_input_ago_ms"] != sample["idle_ms"]
            or not row["sample_entry_ns"]
            <= sample["entry_ns"]
            <= sample["return_ns"]
            <= row["sample_return_ns"]
        ):
            raise InvalidCase("shared-tick diagnostic lacks valid prior observation")
    sample_low, sample_high = _server_interval(current["sample_counter"])
    child_low, child_high = _server_interval(child_counter)
    if (
        not sample_low <= first["own_counter"]["event_server_ms"] <= sample_high < tick
        or not child_low <= tick <= child_high
        or not first["emit_after_ns"]
        <= first["injection_anchor_ns"]
        <= current["sample_entry_ns"]
        or not human["sync_after_ns"]
        <= child_counter["entry_ns"]
        <= child_counter["return_ns"]
        <= current["guard_return_ns"]
        or not current["guard_return_ns"]
        <= current["emit_before_ns"]
        <= current["emit_after_ns"]
        <= own_counter["entry_ns"]
        <= own_counter["return_ns"]
    ):
        raise InvalidCase(
            "shared-tick diagnostic has inconsistent counter/time ordering"
        )


def _outside_miss(result, guard_ms):
    """A proven pre-sample outside miss survives a later oracle error."""
    rows = result["timeline"][1:]
    if len(rows) < 2 or not result.get("event_counter", {}).get("corroborated"):
        return False
    try:
        low, high = _server_interval(rows[1]["sample_counter"])
        return (
            placement(result["case"], rows[0], rows[1], result["human"], guard_ms)
            == "OUTSIDE"
            and low <= result["event_counter"]["event_server_ms"] <= high
            and not low <= rows[0]["own_counter"]["event_server_ms"] <= high
            and not rows[1].get("halted")
            and rows[1].get("snapshot", {}).get("state") == "quiet"
        )
    except (KeyError, InvalidCase):
        return False


def _run_case(case: str, backend: Any, child: Any, oracle) -> dict[str, Any]:
    from amplifier_module_tool_computer_use.coexistence_guard import HaltedError
    from amplifier_module_tool_computer_use.presence import GUARD_MS

    result: dict[str, Any] = {
        "case": case,
        "category": "diagnostic" if case in DIAGNOSTIC_CASES else "required",
        "status": "INVALID",
        "timeline": [],
        "releases": [],
    }
    try:
        presence, guard, tracked, arm = _instrument(backend, child, result, oracle)
        _quiet_baseline(presence.sample())

        def attempt():
            oracle.receiver.arm()
            halted = False
            try:
                backend.type_text("a", guard=tracked)
            except HaltedError:
                halted = True
            finally:
                row = result["timeline"][-1]
                forbidden = halted or any(
                    r.get("halted")
                    or r.get("snapshot", {}).get("state") == "human_active"
                    for r in result["timeline"][1:]
                )
                try:
                    if not oracle.receiver.armed:
                        # A failed barrier already consumed its child batch.
                        # Check for any additional input without replaying it.
                        oracle.receiver.empty()
                        batch = []
                    else:
                        batch = oracle.consume(
                            "own"
                            if "emit_after_ns" in row and not forbidden
                            else "none"
                        )
                    row["delivery"] = batch
                    row["receiver_evidence"] = getattr(
                        oracle.receiver, "last_drain", None
                    )
                except Exception as exc:
                    row["receiver_evidence"] = getattr(
                        oracle.receiver, "last_drain", None
                    )
                    if forbidden and isinstance(exc, UnexpectedInput):
                        result["forbidden_native_input"] = True
                        row["unexpected_delivery"] = exc.evidence
                    raise
            if not halted:
                _confirm_own(backend, result, oracle, batch)
            return halted

        if attempt():
            result["status"] = "FAIL"
            result["reason"] = "halt before independent event (false positive)"
            return result
        start = result["timeline"][-1]["emit_after_ns"] / 1e9
        if case == "quiet":
            for _ in range(100):
                previous_ack = result["timeline"][-1]["emit_after_ns"] / 1e9
                time.sleep(max(0, previous_ack + CADENCE_S - time.monotonic()))
                if attempt():
                    break
                _quiet_timing(result["timeline"][-2:])
        else:
            if case == "post-sample":
                arm()
            else:
                delay = float(case.split("-")[1].removesuffix("ms")) / 1000
                anchor = result["timeline"][-1]["injection_anchor_ns"] / 1e9
                time.sleep(max(0, anchor + delay - time.monotonic()))
                oracle.receiver.arm()
                result["human"] = child.fire()
                _confirm_event(backend, result, oracle)
            # Keep a reference gap before the next fresh sample. Placement
            # uses actual intervals; delayed child acknowledgements invalidate
            # a nominal inside control instead of silently becoming a pass.
            time.sleep(max(0, start + CADENCE_S - time.monotonic()))
            attempt()
            if case == "post-sample" and not guard.halted:
                time.sleep(CADENCE_S)
                attempt()  # observe whether the next own write overwrote the event
        if any(
            row.get("halted") or row.get("snapshot", {}).get("state") == "human_active"
            for row in result["timeline"][1:]
        ):
            emissions = sum("emit_after_ns" in r for r in result["timeline"])
            result["latch_refused"] = attempt() and emissions == sum(
                "emit_after_ns" in r for r in result["timeline"]
            )
            result["latch_call_id"] = result["timeline"][-1]["guard_call_id"]
        _assess(result, GUARD_MS["linux-x11"])
    except SharedServerTick as exc:
        # _confirm_own already validated the complete, narrowly typed pattern.
        # Stop here: no later write to try to manufacture distinct advancement.
        result.update(
            status="LIMIT",
            attribution="ATTRIBUTION_UNRESOLVED",
            attribution_evidence=exc.evidence,
            reason=str(exc),
            limitation="Artificial IPC barrier; distinct counter reset unresolved, not a natural blind-window measurement.",
        )
    except Exception as exc:  # noqa: BLE001 - retain failed evidence, never a miss
        result["status"] = "INVALID"
        result["reason"] = repr(exc)[:MAX_MESSAGE]
        result["receiver_evidence"] = getattr(oracle.receiver, "last_drain", None)
        if isinstance(exc, UnexpectedInput):
            result["unexpected_receiver_input"] = exc.evidence
            if any(
                row.get("halted")
                or row.get("snapshot", {}).get("state") == "human_active"
                for row in result["timeline"][1:]
            ):
                result["forbidden_native_input"] = True
    finally:
        # Attribute only against the child's actual send interval, never the
        # requested delay. A sample overlapping an event is not labelled FP.
        event_before = result.get("human", {}).get("send_before_ns", math.inf)
        result["false_positive_samples_before_event"] = sum(
            row.get("snapshot", {}).get("state") == "human_active"
            and row["sample_return_ns"] <= event_before
            for row in result["timeline"][1:]
        )
        result["ordinary_emissions"] = sum(
            "emit_after_ns" in row for row in result["timeline"][1:]
        )
        result["detected"] = any(
            row.get("snapshot", {}).get("state") == "human_active"
            for row in result["timeline"][1:]
        )
        violation = _halt_violation(result)
        if violation:
            result.update(status="FAIL", reason=violation)
        elif _outside_miss(result, GUARD_MS["linux-x11"]):
            result.update(
                status="FAIL", reason="proven outside event permitted ordinary input"
            )
        if (
            result["status"] in ("PASS", "OBSERVED", "LIMIT")
            and result["false_positive_samples_before_event"]
        ):
            result["status"] = "FAIL"
    return result


def _run_boundary(report: dict[str, Any], display_name: str) -> None:
    from amplifier_module_tool_computer_use.linux_x11 import LinuxX11Backend
    from amplifier_module_tool_computer_use.presence import (
        GUARD_MS,
        QUIET_FLOOR_SECONDS,
    )

    backend = LinuxX11Backend({"display": display_name})
    child = None
    receiver = None
    report.update(
        platform="linux-x11",
        guard_ms=GUARD_MS["linux-x11"],
        reference_cadence_ms=CADENCE_S * 1000,
        predeclared_cases=list(CASES),
        required_case_ids=list(REQUIRED_CASES),
        diagnostic_case_ids=list(DIAGNOSTIC_CASES),
        cases=[],
        diagnostics=[],
    )
    try:
        probe = backend.probe()
        if not probe.available:
            raise InvalidCase(f"backend unavailable: {probe.reason}")
        backend.type_text("")  # first-use setup, emits nothing
        receiver = EventReceiver(display_name)
        oracle = RegistrationOracle(backend, receiver)
        report["registration_oracle"] = oracle.clock.identity
        child = EventChild(display_name)
        child.ready()
        for case in CASES:
            time.sleep(QUIET_FLOOR_SECONDS + 0.5)
            result = _run_case(case, backend, child, oracle)
            report["diagnostics" if case in DIAGNOSTIC_CASES else "cases"].append(
                result
            )
            if result["status"] != "PASS":
                break  # preserve the first failure, no replacement or retry
    finally:
        try:
            if child is not None:
                child.close()
                report["child_cleanup"] = child.cleanup
        finally:
            try:
                if receiver is not None:
                    receiver.close()
                    report["receiver_cleanup"] = receiver.cleanup
            finally:
                report["backend_close_called"] = True
                backend.close()
                # Production contains close errors; return is not closure proof.
                report["backend_close_returned"] = True
    cleanup = report.get("child_cleanup", {})
    complete = [r["case"] for r in report["cases"]] == list(REQUIRED_CASES) and all(
        r["status"] == "PASS" for r in report["cases"]
    )
    diagnostic_ok = [r["case"] for r in report["diagnostics"]] == list(
        DIAGNOSTIC_CASES
    ) and (
        report["diagnostics"][0]["status"],
        report["diagnostics"][0].get("attribution"),
    ) in (("OBSERVED", "ATTRIBUTION_RESOLVED"), ("LIMIT", "ATTRIBUTION_UNRESOLVED"))
    report["required_controls_status"] = "PASS" if complete else "UNQUALIFIED"
    report["diagnostic_status"] = (
        report["diagnostics"][0]["status"] if diagnostic_ok else "UNQUALIFIED"
    )
    report["status"] = (
        MECHANISM_CONTROLS_PASS
        if complete
        and diagnostic_ok
        and cleanup.get("returncode") == 0
        and not cleanup.get("forced", True)
        and report.get("receiver_cleanup", {}).get("closed") is True
        else "UNQUALIFIED"
    )
    report["scope"] = (
        "Five required conditional Linux mechanism controls; artificial barrier diagnostic reported separately. Not physical-input universality, real ledger release, Windows/macOS qualification, permissions/Stop or release adoption."
    )


def _source_hashes() -> dict[str, str]:
    relative = ["scripts/verify_coexistence.py"] + [
        f"modules/tool-computer-use/amplifier_module_tool_computer_use/{name}.py"
        for name in ("linux_x11", "presence", "coexistence_guard", "guard_diagnostics")
    ]
    return {
        path: hashlib.sha256((REPO_ROOT / path).read_bytes()).hexdigest()
        for path in relative
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=("boundary", "random-model"), default="boundary"
    )
    parser.add_argument(
        "--display", help="explicitly owned Linux X11 test display; no ambient fallback"
    )
    parser.add_argument(
        "--output", type=Path, help="new JSON receipt; refuses replacement"
    )
    parser.add_argument(
        "--trials",
        type=int,
        default=100,
        help="offline count analysis only (fixed n=100)",
    )
    parser.add_argument(
        "--misses", type=int, help="offline supplied count; does not collect trials"
    )
    parser.add_argument("--event-child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.event_child:
        if not args.display:
            parser.error("child needs --display")
        _event_child(args.display)
        return 0
    if args.output is None:
        parser.error("--output NEW.json is required")
    if args.mode == "boundary" and (
        not args.display or args.misses is not None or args.trials != 100
    ):
        parser.error(
            "boundary requires --display and does not accept statistical counts"
        )
    if args.mode == "random-model" and (
        args.display is not None
        or args.trials != 100
        or args.misses is None
        or not 0 <= args.misses <= 100
    ):
        parser.error("random-model requires --misses 0..100, n=100, and no display")
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "policy": POLICY,
        "run_id": str(uuid.uuid4()),
        "mode": args.mode,
        "status": "INCOMPLETE",
        "source_sha256": _source_hashes(),
        "started_monotonic_ns": time.monotonic_ns(),
        "python": sys.version,
    }
    # Reserve the exact receipt before any native effect. An external kill can
    # leave INCOMPLETE (or a partial final write); neither authorizes replay.
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(report, output)
        output.flush()
        try:
            if args.mode == "random-model":
                report.update(random_model_report(args.trials, args.misses))
            else:
                _run_boundary(report, args.display)
        except Exception as exc:  # noqa: BLE001 - retain setup/cleanup failures
            report.update(status="UNQUALIFIED", error=repr(exc))
            report["receiver_evidence"] = getattr(exc, "evidence", None)
        finally:
            report["finished_monotonic_ns"] = time.monotonic_ns()
            output.seek(0)
            json.dump(report, output, indent=2, allow_nan=False)
            output.write("\n")
            output.truncate()
    print(
        json.dumps(
            {
                key: report[key]
                for key in ("schema", "policy", "run_id", "mode", "status")
            }
        )
    )
    return 0 if report["status"] == MECHANISM_CONTROLS_PASS else 2


if __name__ == "__main__":
    raise SystemExit(main())
