#!/usr/bin/env python3
"""Conditional coexistence qualification, policy coexistence-boundary-v1 (2026-11-04).

Default `--mode boundary` needs an explicitly owned Linux X11 display. It runs
six predeclared controls once, using the shipped backend, monitor and guard and
one independent, readiness-acknowledged XTEST child. Requested delays are NOT
placement evidence. Unplaceable controls invalidate the run; there are no retries.
The artificial post-sample barrier exposes a limitation, not the natural race's size.

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
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "modules" / "tool-computer-use"))
POLICY = "coexistence-boundary-v1"
SCHEMA = "coexistence-qualification-v1"
CADENCE_S = 0.060  # O5 reference cadence; production type_text has no such pacing.
WINDOW_S = 6.0
QUIET_MAX_GAP_S = 0.080  # Fixed harness scheduling limit, not a production bound.
MECHANISM_CONTROLS_PASS = "MECHANISM_CONTROLS_PASS"
CASES = (
    "quiet",
    "inside-2ms",
    "outside-8ms",
    "outside-15ms",
    "outside-55ms",
    "post-sample",
)
IPC_TIMEOUT_S = 3.0
MAX_MESSAGE = 4096


class InvalidCase(RuntimeError):
    """Missing or unplaceable evidence, not a detection miss."""


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


def _counter_interval(
    entry_ns: int, return_ns: int, idle_ms: float
) -> tuple[float, float]:
    """Bracket last input for a difference of two integer millisecond ticks.

    Quantising both timestamps makes the elapsed-time error bidirectional:
    actual idle is within idle_ms +/- 1ms, not [idle_ms, idle_ms + 1].
    Nonnegative idle also bounds the last input by the query's return.
    """
    return entry_ns - (idle_ms + 1) * 1e6, return_ns - max(0, idle_ms - 1) * 1e6


def _counter_read(backend: Any, destination: dict[str, Any]) -> tuple[float, float]:
    """Raw corroboration only; never updates the production presence monitor."""
    entry = time.monotonic_ns()
    idle = float(backend.presence_idle_ms())
    returned = time.monotonic_ns()
    destination.update(
        entry_ns=entry,
        return_ns=returned,
        idle_ms=idle if math.isfinite(idle) else None,
    )
    if not math.isfinite(idle) or idle < 0:
        raise InvalidCase("counter unreadable/nonfinite")
    return _counter_interval(entry, returned, idle)


def _sample_counter_interval(row: dict[str, Any]) -> tuple[float, float]:
    idle = row["snapshot"]["last_human_input_ago_ms"]
    return _counter_interval(row["sample_entry_ns"], row["sample_return_ns"], idle)


def _confirm_own(backend: Any, result: dict[str, Any]) -> None:
    """Corroborate every acknowledged own unit, not its text/display semantics."""
    row = result["timeline"][-1]
    counter = row["own_counter"] = {}
    low, high = _counter_read(backend, counter)
    previous_high = _sample_counter_interval(row)[1]
    if "event_counter" in result:
        event = result["event_counter"]
        previous_high = max(
            previous_high,
            _counter_interval(event["entry_ns"], event["return_ns"], event["idle_ms"])[
                1
            ],
        )
    before, after = row["emit_before_ns"], row["emit_after_ns"]
    # Intersect independent event and counter brackets for consistency. Prove
    # advancement with the UNCONDITIONED counter intervals: clipping first to
    # an assumed successful write could falsely credit an ineffective emitter.
    registered_low, registered_high = max(low, before), min(high, after)
    if registered_low > registered_high or low <= previous_high:
        raise InvalidCase("own counter registration unplaceable or did not advance")
    counter["registration_interval_ns"] = [registered_low, registered_high]
    counter["corroborated"] = True


def _confirm_event(backend: Any, result: dict[str, Any]) -> None:
    """Observe counter registration without updating the production monitor.

    XTEST sync alone does not prove the idle counter registered input. Retain a
    raw counter read and its interval. Symmetric +/-1ms accounts for both integer
    timestamps, not a change to the guard. A different/unplaceable input or a
    missing read invalidates the control rather than becoming a masking miss.
    """
    counter = result["event_counter"] = {}
    inferred_low, inferred_high = _counter_read(backend, counter)
    human = result["human"]
    prior = result["timeline"][1]
    if (
        inferred_high < human["send_before_ns"]
        or inferred_low > human["sync_after_ns"]
        or inferred_low <= prior["emit_after_ns"]
    ):
        raise InvalidCase("idle counter does not independently place the child event")


def _instrument(backend: Any, child: Any, result: dict[str, Any]):
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
    call_id = 0

    def idle_source():
        idle = float(backend.presence_idle_ms())
        if not math.isfinite(idle) or idle < 0:
            raise InvalidCase("nonfinite/negative idle")
        return idle

    class TimedPresence(PresenceMonitor):
        def sample(self, **kwargs):
            row = active_row if active_row is not None else {}
            if active_row is None:
                rows.append(row)
            row["sample_entry_ns"] = time.monotonic_ns()
            try:
                snapshot = super().sample(**kwargs)
                row["snapshot"] = snapshot.to_dict()
                return snapshot
            finally:
                row["sample_return_ns"] = time.monotonic_ns()

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
                _confirm_event(backend, result)
            except Exception as exc:
                # Production contains observer exceptions. Retain it and refuse
                # the harness's next emission after before_event returns.
                observer_error.append(repr(exc))

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
        low, high = _sample_counter_interval(rows[1])
        if (
            low > result["human"]["sync_after_ns"]
            or high < result["human"]["send_before_ns"]
        ):
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
        result["status"] = "PASS"
        result["limitation"] = (
            "Artificial IPC barrier; either outcome retained, not a natural blind-window measurement or a must-detect/must-miss assertion."
        )
    else:
        result["status"] = "PASS"  # masking OR earlier detection, never require a miss


def _run_case(case: str, backend: Any, child: Any) -> dict[str, Any]:
    from amplifier_module_tool_computer_use.coexistence_guard import HaltedError
    from amplifier_module_tool_computer_use.presence import GUARD_MS

    result: dict[str, Any] = {
        "case": case,
        "status": "INVALID",
        "timeline": [],
        "releases": [],
    }
    try:
        presence, guard, tracked, arm = _instrument(backend, child, result)
        _quiet_baseline(presence.sample())

        def attempt():
            try:
                backend.type_text("a", guard=tracked)
                _confirm_own(backend, result)
                return False
            except HaltedError:
                return True

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
                result["human"] = child.fire()
                _confirm_event(backend, result)
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
    except Exception as exc:  # noqa: BLE001 - retain failed evidence, never a miss
        result["status"] = "INVALID"
        result["reason"] = repr(exc)[:MAX_MESSAGE]
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
        if result["status"] == "PASS" and result["false_positive_samples_before_event"]:
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
    report.update(
        platform="linux-x11",
        guard_ms=GUARD_MS["linux-x11"],
        reference_cadence_ms=CADENCE_S * 1000,
        predeclared_cases=list(CASES),
        cases=[],
    )
    try:
        probe = backend.probe()
        if not probe.available:
            raise InvalidCase(f"backend unavailable: {probe.reason}")
        backend.type_text("")  # first-use setup, emits nothing
        child = EventChild(display_name)
        child.ready()
        for case in CASES:
            time.sleep(QUIET_FLOOR_SECONDS + 0.5)
            result = _run_case(case, backend, child)
            report["cases"].append(result)
            if result["status"] != "PASS":
                break  # preserve the first failure, no replacement or retry
    finally:
        try:
            if child is not None:
                child.close()
                report["child_cleanup"] = child.cleanup
        finally:
            report["backend_close_called"] = True
            backend.close()
            # The production close method contains connection-close errors.
            # Its return is not independent proof that an X connection closed.
            report["backend_close_returned"] = True
    cleanup = report.get("child_cleanup", {})
    complete = len(report["cases"]) == len(CASES) and all(
        r["status"] == "PASS" for r in report["cases"]
    )
    report["status"] = (
        MECHANISM_CONTROLS_PASS
        if complete
        and cleanup.get("returncode") == 0
        and not cleanup.get("forced", True)
        else "UNQUALIFIED"
    )
    report["scope"] = (
        "Conditional Linux mechanism controls only; not physical-input universality, real ledger release, Windows/macOS qualification, permissions/Stop or release adoption."
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
