"""Bounded controller-local evidence, never a guard input or a target operation.

Only projections enter the cache: no operation arguments, object addresses,
exception strings, target names, or content digests. Reading uses no execution
lock, clock, source callback, filesystem, or transport. Missing evidence is not
reconstructed. This module is not imported by the remote payload.
"""

from __future__ import annotations

import copy
import re
import threading
import uuid
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar

MAX_RECORDS = 8
MAX_NUMBER = 2**53 - 1
_HEX = re.compile(r"[0-9a-f]{64}")
_TOKEN = re.compile(r"[0-9a-f]{32}")
_PHASES = frozenset(
    {
        "dispatch_queued",
        "dispatch_complete",
        "dispatch_failed",
        "type_complete",
        "before_sample",
        "before_next_write",
        "receipt",
        "receipt_invalid",
        "halt",
        "guard_created",
        "halt_recheck",
        "condition_constructed",
    }
)
_CURRENT = ContextVar("computer_guard_diagnostic_request", default=None)


def new_id():
    """An opaque instance token, unrelated to any host/session identifier."""
    try:
        return uuid.uuid4().hex
    except BaseException:  # noqa: BLE001 - missing identity cannot affect execution
        return None


PROCESS_ID = new_id()


def _number(value):
    return value if type(value) is int and 0 <= value <= MAX_NUMBER else None


def _opaque(value):
    return value if type(value) is str and _TOKEN.fullmatch(value) else None


def _valid_receipt(value):
    return value is None or (
        type(value) is dict
        and value.keys() == {"before_ms", "after_ms", "count"}
        and all(_number(x) is not None for x in value.values())
        and value["before_ms"] <= value["after_ms"]
        and 0 < value["count"] < 2**32
    )


def _valid_record(value):
    """Validate the cache too; corruption must not disclose a malformed record."""
    if type(value) is not dict:
        return False
    tokens = {"request_id", "tool_id", "binding_id", "guard_id", "presence_id"}
    numbers = {"unit_index", "baseline_ms", "accepted_receipts"}
    flags = {
        "structured_clock",
        "observed_wrapper",
        "proof_present",
        "halted",
        "operator_presence_override_active",
    }
    labels = {
        "phase": _PHASES,
        "presence_state": {"quiet", "human_active", "unknown"},
        "halt_origin": {
            "none",
            "unknown",
            "live_presence",
            "live_safety",
            "durable_presence",
            "durable_safety",
        },
        "receipt_status": {"invalid", "accepted", "not_recorded"},
        "constructed_condition_present": {"not_recorded", "present"},
        "wire_condition_presence": {"not_measured"},
    }
    if value.keys() != tokens | numbers | flags | labels.keys() | {
        "observation",
        "last_receipt",
    }:
        return False
    if not all(value[k] is None or _opaque(value[k]) is not None for k in tokens):
        return False
    if not all(value[k] is None or _number(value[k]) is not None for k in numbers):
        return False
    if not all(type(value[k]) is bool for k in flags):
        return False
    if not all(
        type(value[k]) is str and value[k] in choices for k, choices in labels.items()
    ):
        return False
    observation = value["observation"]
    return (
        _valid_receipt(value["last_receipt"])
        and type(observation) is dict
        and observation.keys() == {"status", "epoch", "now_ms", "last_input_ms"}
        and type(observation["status"]) is str
        and observation["status"] in {"missing", "invalid", "redacted", "valid"}
        and (
            observation["epoch"] is None
            or type(observation["epoch"]) is str
            and _HEX.fullmatch(observation["epoch"]) is not None
        )
        and all(
            observation[k] is None or _number(observation[k]) is not None
            for k in ("now_ms", "last_input_ms")
        )
    )


def _observation(value):
    out = {"status": "missing", "epoch": None, "now_ms": None, "last_input_ms": None}
    if value is None:
        return out
    # TargetObservation is a plain controller dataclass. Never call a source or
    # stringify unknown values; even its epoch is exposed only as existing SHA256.
    fields = vars(value)
    now, last = _number(fields.get("now_ms")), _number(fields.get("last_input_ms"))
    if now is None or last is None or last > now or now - last >= 2**32:
        out["status"] = "invalid"
        return out
    epoch = fields.get("epoch")
    valid_epoch = type(epoch) is str and _HEX.fullmatch(epoch) is not None
    out.update(
        status="valid" if valid_epoch else "redacted",
        epoch=epoch if valid_epoch else None,
        now_ms=now,
        last_input_ms=last,
    )
    return out


def _origin(halted, snapshot):
    # Lazy to keep PresenceMonitor's identity factory free of import cycles.
    from .halt_state import PERSISTED_BASIS, PERSISTED_UNKNOWN_BASIS

    if not halted:
        return "none"
    if snapshot is None:
        return "unknown"
    fields = vars(snapshot)
    basis = fields.get("basis")
    # These fixed labels are the guard's existing durable provenance, not file
    # hashes, timing deductions, or a claim about the physical input's author.
    if basis == PERSISTED_BASIS:
        return "durable_presence"
    if type(basis) is str and basis.startswith(PERSISTED_UNKNOWN_BASIS + ":"):
        return "durable_safety"
    state = getattr(fields.get("state"), "value", None)
    return (
        "live_presence"
        if state == "human_active"
        else "live_safety"
        if state == "unknown"
        else "unknown"
    )


class DiagnosticLog:
    """One fixed-size cache per tool/guard. No references to backend or payload."""

    def __init__(self):
        self.identity = new_id()
        self._lock = threading.Lock()
        self._records = deque(maxlen=MAX_RECORDS)
        self._truncated = False
        self._fault = self.identity is None
        self._accepted = 0
        self._saturated = False
        self._last_receipt = None
        self._type_complete = None
        self._before_next_write = None

    def capture(self, guard, phase, *, result=None):
        if phase not in _PHASES:
            raise ValueError("unknown diagnostic phase")
        context = _CURRENT.get()
        presence = vars(guard).get("presence") if guard is not None else None
        fields = vars(presence) if presence is not None else {}
        snap = fields.get("_last_snapshot")
        state = getattr(vars(snap).get("state"), "value", None) if snap else None
        state = state if state in {"quiet", "human_active", "unknown"} else "unknown"
        guard_fields = vars(guard) if guard is not None else {}
        halted = guard_fields.get("_halted") is True
        receipt = None
        if phase == "receipt":
            # Called ONLY after the real presence receipt validator returns.
            # Project independently: a future validator change cannot make this
            # cache a route for new fields or arbitrary values.
            clock = result.get("target_clock") if type(result) is dict else None
            injection = clock.get("injection") if type(clock) is dict else None
            if injection is not None:
                if type(injection) is not dict:
                    raise ValueError("invalid diagnostic receipt")
                before = _number(injection.get("before_ms"))
                after = _number(injection.get("after_ms"))
                count = _number(injection.get("count"))
                if (
                    before is None
                    or after is None
                    or count is None
                    or not before <= after
                    or not 0 < count < 2**32
                ):
                    raise ValueError("invalid diagnostic receipt")
                receipt = {"before_ms": before, "after_ms": after, "count": count}
        diagnostic = guard_fields.get("_diagnostics")
        record = {
            "phase": phase,
            "request_id": context["request_id"] if context else None,
            "unit_index": context["unit_index"] if context else None,
            "tool_id": context["tool_id"] if context else None,
            "binding_id": context["binding_id"] if context else None,
            "guard_id": diagnostic.identity if diagnostic is not None else None,
            "presence_id": _opaque(fields.get("_diagnostic_id")),
            "structured_clock": fields.get("target_clock_source") is not None,
            # Only this checkpoint witnesses construction. Other phases can be
            # correlated by request/unit, never inferred from configured clocks
            # or a prior unit. No serialization/transport seam is measured here.
            "constructed_condition_present": (
                "present" if phase == "condition_constructed" else "not_recorded"
            ),
            "wire_condition_presence": "not_measured",
            "observed_wrapper": context["observed_wrapper"] if context else False,
            "proof_present": context["proof_present"] if context else False,
            "baseline_ms": _number(fields.get("_target_inject_before_ms")),
            "observation": _observation(fields.get("_target_observation")),
            "presence_state": state,
            "halted": halted,
            # Cached at enforcement, NOT resampled by diagnostic_snapshot.
            # No channel/reason or inferred authority enters this cache.
            "operator_presence_override_active": (
                not halted
                and guard_fields.get("_operator_presence_override_active") is True
            ),
            "halt_origin": _origin(halted, guard_fields.get("_halt_snapshot")),
            "receipt_status": (
                "invalid"
                if phase == "receipt_invalid"
                else "accepted"
                if receipt is not None
                else "not_recorded"
            ),
        }
        # Projection happens before the private lock. Nothing executed while
        # holding it can wait for a backend, guard callback or another lock.
        # Diagnostics may lose evidence (declared by fault), never stall input
        # cancellation, halt detection, or emergency release behind a reader.
        if not self._lock.acquire(blocking=False):
            self._fault = True
            return
        try:
            if receipt is not None:
                self._saturated |= self._accepted == MAX_NUMBER
                self._accepted = min(self._accepted + 1, MAX_NUMBER)
                self._last_receipt = receipt
            record["accepted_receipts"] = self._accepted
            record["last_receipt"] = self._last_receipt
            self._truncated |= len(self._records) == MAX_RECORDS
            self._records.append(record)
            if phase == "type_complete":
                self._type_complete = record
            if phase == "before_next_write":
                self._before_next_write = record
        finally:
            self._lock.release()

    def read(self):
        # A snapshot may be missing during a concurrent cache update; it never
        # waits behind an in-flight write or delays halt/release execution.
        out = empty_cache(fault=self._fault)
        if not self._lock.acquire(blocking=False):
            return out
        try:
            records = list(self._records)
            retained = [r for r in records[-MAX_RECORDS:] if _valid_record(r)]
            last_receipt = (
                self._last_receipt if _valid_receipt(self._last_receipt) else None
            )
            type_complete = (
                self._type_complete if _valid_record(self._type_complete) else None
            )
            before_next = (
                self._before_next_write
                if _valid_record(self._before_next_write)
                else None
            )
            invalid = (
                len(retained) != len(records)
                or last_receipt is not self._last_receipt
                or type_complete is not self._type_complete
                or before_next is not self._before_next_write
            )
            out.update(
                available=True,
                fault=self._fault is True or invalid,
                truncated=self._truncated is True or len(records) > MAX_RECORDS,
                count_saturated=self._saturated is True,
                accepted_receipts=_number(self._accepted),
                last_receipt=last_receipt,
                type_complete=type_complete,
                before_next_write=before_next,
                records=retained,
            )
            return copy.deepcopy(out)
        finally:
            self._lock.release()


def empty_cache(*, fault=False):
    return {
        "available": False,
        "fault": fault is True,
        "truncated": False,
        "count_saturated": False,
        "accepted_receipts": None,
        "last_receipt": None,
        "type_complete": None,
        "before_next_write": None,
        "records": [],
    }


def read_cache(log):
    try:
        return log.read()
    except BaseException:  # noqa: BLE001 - this block contains only cache reads
        return empty_cache(fault=True)


def record(log, guard, phase, *, result=None):
    """Contain ONLY diagnostic faults, including cancellation from a collector.

    Actual execution is never inside this try block. A failed report must not
    replace a successful result, halt, execution error, or original cancellation.
    """
    if log is None:
        return
    try:
        log.capture(guard, phase, result=result)
    except BaseException:  # noqa: BLE001 - reporting is never execution authority
        log._fault = True


def checkpoint(guard, phase, *, result=None):
    record(getattr(guard, "_diagnostics", None), guard, phase, result=result)
    context = _CURRENT.get()
    if context:
        record(context["log"], guard, phase, result=result)


def typing_path(*, wrapper, proof):
    context = _CURRENT.get()
    if context:
        context["observed_wrapper"] = wrapper is True
        context["proof_present"] = proof is True


def typing_unit(index):
    context = _CURRENT.get()
    if context:
        context["unit_index"] = _number(index)


@contextmanager
def operation(log, binding, tool_id):
    """One token per actual controller step, including queued/failed dispatch."""
    guard = binding.coexistence_guard
    token = None
    try:
        token = _CURRENT.set(
            {
                "log": log,
                "request_id": new_id(),
                "tool_id": tool_id,
                "binding_id": binding.diagnostic_id,
                "unit_index": None,
                "observed_wrapper": False,
                "proof_present": False,
            }
        )
        if _CURRENT.get()["request_id"] is None:
            log._fault = True
    except BaseException:  # noqa: BLE001 - no execution in diagnostic setup
        log._fault = True
    try:
        checkpoint(guard, "dispatch_queued")
        try:
            yield
        except BaseException:
            checkpoint(guard, "dispatch_failed")
            raise
        else:
            checkpoint(guard, "dispatch_complete")
    finally:
        if token is not None:
            _CURRENT.reset(token)


def snapshot(tool):
    """Read only cached references; safe before disclosure, while halted/closed."""
    binding = tool._binding
    guard = binding.coexistence_guard
    log = getattr(guard, "_diagnostics", None)
    return {
        "version": 1,
        "cached": True,
        "process_id": _opaque(PROCESS_ID),
        "tool_id": _opaque(tool._diagnostics.identity),
        "binding_id": _opaque(binding.diagnostic_id),
        "guard_id": _opaque(log.identity) if log is not None else None,
        "max_records": MAX_RECORDS,
        "tool": read_cache(tool._diagnostics),
        "guard": read_cache(log) if log is not None else None,
    }
