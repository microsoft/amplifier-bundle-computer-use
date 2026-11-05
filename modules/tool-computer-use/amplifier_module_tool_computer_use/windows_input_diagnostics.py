"""Opt-in Windows marker correlation. Never imported by guard decisions.

No process, transport, desktop, clipboard or halt access occurs here. A matching
marker is spoofable backend-scope correlation, not physical-author proof.
"""

from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROBE_PS1 = Path(__file__).with_name("input_probe.ps1")
_KINDS = {
    "key-down",
    "key-up",
    "keyboard-other",
    "mouse-move",
    "mouse-button-down",
    "mouse-button-up",
    "mouse-wheel",
    "mouse-other",
}
_CLEANUP = {
    "deadline_timer_disposed",
    "sample_timer_killed",
    "mouse_unhooked",
    "keyboard_unhooked",
    "input_desktop_handle_closed",
}
_REPORT = {
    "version",
    "ready",
    "status",
    "epoch",
    "requested_duration_ms",
    "event_cap",
    "change_cap",
    "keyboard_hook_installed",
    "mouse_hook_installed",
    "initial",
    "final",
    "counter_changes",
    "counter_change_count",
    "counter_changes_dropped",
    "sample_count",
    "max_sample_gap_ms",
    "events",
    "keyboard_callback_count",
    "mouse_callback_count",
    "events_dropped",
    "callback_errors",
    "deadline_stop_posted",
    "deadline_post_error",
    "cleanup",
}


@dataclass(frozen=True)
class ProbeRequest:
    """Private launch material; repr excludes the raw marker and stdin body."""

    script: Path
    stdin: str = field(repr=False)


def prepare_probe(marker: int, duration_ms: int = 35000) -> ProbeRequest:
    """Prepare, never launch. Send stdin only to the owned helper, not a log."""
    _integer(marker, 1, (1 << (struct.calcsize("P") * 8 - 1)) - 1)
    _integer(duration_ms, 1, 35000)
    return ProbeRequest(
        PROBE_PS1,
        json.dumps({"marker": marker, "duration_ms": duration_ms}) + "\n",
    )


def _invalid() -> None:
    # Never include an offending value/key: it may contain payload or identity.
    raise ValueError("Malformed bounded input diagnostic")


def _integer(value: Any, minimum: int = 0, maximum: int = (1 << 64) - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _invalid()
    return value


def _object(value: Any, fields: set[str], optional: set[str] = frozenset()) -> dict:
    if type(value) is not dict or not fields <= value.keys() <= fields | optional:
        _invalid()
    return value


def _bool(value: Any) -> bool:
    if type(value) is not bool:
        _invalid()
    return value


def event_category(
    injected: bool, lower_il_injected: bool, marker_matches: bool
) -> str:
    """Flags alone never identify our sender or establish hardware/human input."""
    for value in (injected, lower_il_injected, marker_matches):
        _bool(value)
    if lower_il_injected and not injected:
        return "inconsistent_flags"
    if injected:
        return "own_marker" if marker_matches else "other_injected"
    return "inconsistent_marker" if marker_matches else "unattributed_noninjected"


def _sample(value: Any) -> dict:
    value = _object(value, {"observed_tick64", "last_input_tick_dword"})
    _integer(value["observed_tick64"])
    _integer(value["last_input_tick_dword"], maximum=(1 << 32) - 1)
    return value


def _unwrap(tick: int, observed: int) -> int:
    value = (observed & ~((1 << 32) - 1)) | tick
    if value > observed:
        value -= 1 << 32
    # No inversion, timing grace, negative/future or wrap-ambiguous inference.
    if value < 0 or observed - value >= 1 << 31:
        _invalid()
    return value


def correlate_probe(frames: list[dict], receipt: dict) -> dict:
    """Correlate three completed helper frames with ONE existing emission receipt.

    Closed input/output schemas exclude payloads. Raises a payload-free ValueError
    on malformed frames. Missing/failed/lossy evidence stays unresolved. Even
    `correlated` is diagnostic only and must never feed guard credit or a reset.
    Caller must also observe process exit/reaping; frames do not prove that.
    """
    unresolved = {
        "version": 1,
        "status": "unresolved",
        "reason": "incomplete_evidence",
        "events": [],
        "counter_changes": [],
    }
    if type(frames) is not list or len(frames) != 3:
        return unresolved
    ready = _object(frames[0], {"event", "version", "epoch", "observed_tick64"})
    envelope = _object(frames[1], {"event", "result"})
    final = _object(
        frames[2],
        {
            "event",
            "status",
            "compiler_temp_files",
            "input_calls",
            "clipboard_access",
            "gui_actions",
            "halt_access",
        },
    )
    if (
        ready["event"] != "probe_ready"
        or ready["version"] != 1
        or envelope["event"] != "probe_result"
        or final["event"] != "probe_final"
    ):
        _invalid()
    report = _object(envelope["result"], _REPORT, {"error_type"})
    if "error_type" in report and (
        type(report["error_type"]) is not str
        or not re.fullmatch(r"[A-Za-z]{1,80}", report["error_type"])
    ):
        _invalid()
    receipt = _object(receipt, {"epoch", "before_ms", "after_ms", "count"})
    for epoch in (ready["epoch"], report["epoch"], receipt["epoch"]):
        if type(epoch) is not str or not re.fullmatch(r"[0-9a-f]{64}", epoch):
            _invalid()
    _integer(ready["version"], 1, 1)
    _integer(report["version"], 1, 1)
    _integer(ready["observed_tick64"])
    _integer(report["requested_duration_ms"], 1, 35000)
    _integer(report["event_cap"], 256, 256)
    _integer(report["change_cap"], 32, 32)
    for name in (
        "ready",
        "keyboard_hook_installed",
        "mouse_hook_installed",
        "deadline_stop_posted",
    ):
        _bool(report[name])
    for name in (
        "counter_change_count",
        "counter_changes_dropped",
        "sample_count",
        "max_sample_gap_ms",
        "keyboard_callback_count",
        "mouse_callback_count",
        "events_dropped",
        "callback_errors",
        "deadline_post_error",
    ):
        _integer(report[name])
    cleanup = _object(report["cleanup"], _CLEANUP)
    for value in cleanup.values():
        _bool(value)
    for name in ("input_calls", "clipboard_access", "gui_actions", "halt_access"):
        _integer(final[name], 0, 0)
    if (
        type(final["compiler_temp_files"]) is not list
        or len(final["compiler_temp_files"]) > 32
    ):
        _invalid()
    temp_clean = True
    for item in final["compiler_temp_files"]:
        item = _object(
            item, {"existed_before_cleanup", "exists_after_cleanup", "error_type"}
        )
        _bool(item["existed_before_cleanup"])
        _bool(item["exists_after_cleanup"])
        if item["error_type"] is not None:
            if type(item["error_type"]) is not str or not re.fullmatch(
                r"[A-Za-z]{1,80}", item["error_type"]
            ):
                _invalid()
            temp_clean = False
        temp_clean &= not item["exists_after_cleanup"]
    before, after = (_integer(receipt[n]) for n in ("before_ms", "after_ms"))
    count = _integer(receipt["count"], 1, 256)
    if after < before or after - before >= 1 << 31:
        _invalid()
    if report["status"] not in {"completed", "failed"} or final["status"] not in {
        "completed",
        "failed",
    }:
        _invalid()
    if report["initial"] is None or report["final"] is None:
        return unresolved
    initial, end = _sample(report["initial"]), _sample(report["final"])
    start, finish = initial["observed_tick64"], end["observed_tick64"]
    if not start <= finish or ready["observed_tick64"] != start:
        _invalid()
    events, changes = report["events"], report["counter_changes"]
    if (
        type(events) is not list
        or len(events) > 256
        or type(changes) is not list
        or len(changes) > 32
    ):
        _invalid()
    if (
        report["keyboard_callback_count"] + report["mouse_callback_count"]
        != len(events) + report["events_dropped"]
        or report["counter_change_count"]
        != len(changes) + report["counter_changes_dropped"]
    ):
        _invalid()
    projected, ticks = [], []
    for event in events:
        event = _object(
            event,
            {
                "kind",
                "callback_tick_dword",
                "observed_tick64",
                "injected",
                "lower_il_injected",
                "marker_matches",
                "category",
            },
        )
        if type(event["kind"]) is not str or event["kind"] not in _KINDS:
            _invalid()
        tick = _integer(event["callback_tick_dword"], maximum=(1 << 32) - 1)
        observed = _integer(event["observed_tick64"])
        if not start <= observed <= finish or (
            projected and observed < projected[-1]["observed_tick64"]
        ):
            _invalid()
        category = event_category(
            event["injected"], event["lower_il_injected"], event["marker_matches"]
        )
        if event["category"] != category:
            _invalid()
        tick64 = _unwrap(tick, observed)
        relation = (
            "within_receipt"
            if before <= tick64 <= observed <= after
            else "late"
            if tick64 > after or observed > after
            else "outside_receipt"
        )
        projected.append({**event, "receipt_relation": relation})
        ticks.append(tick64)
    complete = (
        report["status"] == final["status"] == "completed"
        and report["ready"]
        and report["keyboard_hook_installed"]
        and report["mouse_hook_installed"]
        and all(cleanup.values())
        and temp_clean
        and report["sample_count"] >= 2
        and report["events_dropped"]
        == report["counter_changes_dropped"]
        == report["callback_errors"]
        == report["deadline_post_error"]
        == 0
        and ready["epoch"] == report["epoch"] == receipt["epoch"]
        and start <= before <= after <= finish
    )
    within = [e for e in projected if e["receipt_relation"] == "within_receipt"]
    receipt_matches = len(within) == count and all(
        e["category"] == "own_marker" for e in within
    )
    transitions = []
    previous_time, previous_tick = start, initial["last_input_tick_dword"]
    for change in changes:
        change = _sample(change)
        observed = change["observed_tick64"]
        tick = change["last_input_tick_dword"]
        if not previous_time <= observed <= finish or tick == previous_tick:
            _invalid()
        # Every event at this counter tick counts, even a callback recorded AFTER
        # the sample: temporal filtering must not hide a same-tick competitor.
        matches = [i for i, t in enumerate(ticks) if t == _unwrap(tick, observed)]
        reason, category = "no_matching_event", "unresolved"
        if len(matches) > 1:
            reason = "same_tick_ambiguous"
        elif matches:
            e = projected[matches[0]]
            category = e["category"]
            between_samples = [
                item
                for item in projected
                if previous_time <= item["observed_tick64"] <= observed
            ]
            reason = (
                "correlated_marker_only"
                if complete
                and receipt_matches
                and len(between_samples) == 1
                and category == "own_marker"
                and e["receipt_relation"] == "within_receipt"
                and e["observed_tick64"] <= observed
                else "unresolved_event_or_coverage"
            )
        transitions.append(
            {
                **change,
                "category": category,
                "reason": reason,
                "attribution": "correlated"
                if reason == "correlated_marker_only"
                else "unresolved",
            }
        )
        previous_time, previous_tick = observed, tick
    if (
        previous_tick != end["last_input_tick_dword"]
        and not report["counter_changes_dropped"]
    ):
        _invalid()
    return {
        "version": 1,
        "status": "correlated"
        if complete
        and receipt_matches
        and len(within) == len(projected)
        and transitions
        and all(t["attribution"] == "correlated" for t in transitions)
        else "unresolved",
        "reason": "diagnostic_only_not_authority",
        "events": projected,
        "counter_changes": transitions,
    }
