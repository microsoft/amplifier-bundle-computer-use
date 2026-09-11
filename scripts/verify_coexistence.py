#!/usr/bin/env python3
"""Ship gate: C1 evidence item 2 of `docs/designs/coexistence.md` \u00a711 - the
sustained-injection interleave test, run for real, at statistical scale.

    "Sustained-injection interleave test, per platform - the incident's actual
    regime. The agent runs a continuous type_text at production cadence for
    >= 6 s. An independent process fires a single event at a time uniformly
    sampled within the run, unknown to the agent. Repeat >= 100 times.
    Record: detection rate, false-positive count across all non-human
    samples, detection latency, and the measured per-event masked fraction,
    which must match GUARD/P within noise."

This is NOT a mock and NOT a single-process simulation. Each trial:

  1. Spawns a genuinely SEPARATE OS process (`subprocess.Popen`, this same
     script re-invoked with `--human-inject`) that opens its OWN Xlib
     connection to the target display and fires exactly ONE synthetic input
     event (an XTEST pointer motion - the same "independent human stand-in"
     shape U1c/O5 used) at a delay drawn uniformly at random within the
     trial's window. The main process has no foreknowledge of that delay
     beyond "sometime in this window" - it is generated fresh per trial and
     only used to schedule the subprocess.
  2. Concurrently, the main process runs the SHIPPED
     `LinuxX11Backend.type_text()` + `CoexistenceGuard` (the exact production
     code path - `modules/tool-computer-use/.../linux_x11.py:536`,
     `coexistence_guard.py`), one character at a time at a fixed
     production-representative cadence (`CADENCE_S`, matching the exact
     figure `docs/designs/coexistence-probes.md` O5 measured against and
     `coexistence.md` \u00a75.2 cites - the shipped `type_text` has no inherent
     per-character delay of its own, so this harness supplies the cadence
     explicitly, exactly as O5's own probe did).
  3. The guard's `HaltedError` - or its absence - IS the measurement. No
     result is asserted or computed from anything other than what the real
     `PresenceMonitor`/`CoexistenceGuard` objects report.

No fallbacks, no synthetic detection, no degraded modes: a trial that fails
to detect is counted as a miss, not retried or excused.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "modules" / "tool-computer-use"))

#: Production-cadence assumption, matching O5 exactly (`coexistence-probes.md`
#: \u00a7O5: "agent injecting every 60ms for 6s (type_text cadence)") and
#: `coexistence.md` \u00a75.2/\u00a75.5's own citation of 60ms as the type_text
#: injection interval this design's masking arithmetic is built against.
CADENCE_S = 0.060
WINDOW_S = 6.0
#: Keep the human event well clear of both window edges: at least one
#: full sample gap before it (so there is a real "before" baseline) and one
#: full sample gap after it (so there is a real sample that CAN see it).
HUMAN_DELAY_MIN_S = CADENCE_S * 4
HUMAN_DELAY_MAX_S = WINDOW_S - CADENCE_S * 4

_TYPING_TEXT = (
    "The quick brown fox jumps over the lazy dog. Pack my box with five "
    "dozen liquor jugs. Amplifier drives the desktop while a human may "
    "still be sitting at it, and this sentence exists only to have enough "
    "characters to fill the window. "
)


def _characters_for_window(window_s: float, cadence_s: float) -> str:
    n = int(window_s / cadence_s) + 5  # small buffer past the window
    text = (_TYPING_TEXT * (n // len(_TYPING_TEXT) + 1))[:n]
    return text


# ---------------------------------------------------------------------------
# The independent human-input process (re-invocation of this same script)
# ---------------------------------------------------------------------------


def _run_human_inject(display_name: str, delay_s: float, out_path: str) -> None:
    """Run as a genuinely separate OS process. Sleeps `delay_s`, opens its
    OWN X11 connection (no state shared with the parent beyond argv), fires
    ONE elementary XTEST pointer-motion event, and records the wall-clock
    time it did so - the same "independent process records its own
    timestamp" discipline U1c used to prove the reconciliation arithmetic
    against a genuinely separate process, not a shared-state simulation.
    """
    time.sleep(delay_s)
    from Xlib import X
    from Xlib import display as xlib_display
    from Xlib.ext import xtest

    conn = xlib_display.Display(display_name)
    try:
        fired_at = time.time()
        # A single elementary event - one MotionNotify, matching \u00a76 of the
        # task brief ("fires ONE input event"). Absolute move to a fixed
        # point so this process needs no knowledge of the current pointer
        # position.
        xtest.fake_input(conn, X.MotionNotify, x=7, y=7)
        conn.sync()
    finally:
        conn.close()
    Path(out_path).write_text(json.dumps({"fired_at": fired_at}))


# ---------------------------------------------------------------------------
# One trial, driving the real shipped mechanism
# ---------------------------------------------------------------------------


@dataclass
class TrialResult:
    index: int
    baseline_idle_ms: float
    human_delay_s: float
    detected: bool
    false_positive_before_human: bool
    detection_latency_ms: float | None
    margin_ms: float | None
    guard_ms: float | None
    chars_typed: int
    error_repr: str | None
    released_on_halt: bool


class InvalidBaselineError(RuntimeError):
    """The gate could not establish a fresh, safely quiet trial baseline."""


def _run_one_trial(
    index: int, backend: Any, display_name: str, tmp_dir: Path
) -> TrialResult:
    # Imports deferred so `--human-inject` re-invocation (a plain, minimal
    # subprocess) never has to import the full coexistence stack.
    from amplifier_module_tool_computer_use.backend import BackendError
    from amplifier_module_tool_computer_use.coexistence_guard import (
        CoexistenceGuard,
        HaltedError,
    )
    from amplifier_module_tool_computer_use.presence import (
        QUIET_FLOOR_SECONDS,
        Confidence,
        IdleUnreadableError,
        PresenceMonitor,
        PresenceState,
    )

    released: list[str] = []

    def release_all(reason: str) -> list[str]:
        released.append(reason)
        return []

    presence = PresenceMonitor(
        idle_source=backend.presence_idle_ms, platform="linux-x11"
    )
    guard = CoexistenceGuard(presence=presence, release_all=release_all)

    # A settle sleep is not evidence that this display is quiet: another
    # process or person could have touched it during that interval. Take a
    # fresh production monitor sample before scheduling this trial's
    # independent human-input child, and reject rather than dilute the gate
    # if it cannot establish the strict quiet baseline.
    try:
        baseline = presence.sample()
    except IdleUnreadableError as exc:
        raise InvalidBaselineError(
            f"trial {index + 1}: idle counter unreadable while establishing "
            f"the pre-trial baseline: {exc}"
        ) from exc
    baseline_idle_ms = baseline.last_human_input_ago_ms
    quiet_floor_ms = QUIET_FLOOR_SECONDS * 1000.0
    if (
        baseline.state is not PresenceState.QUIET
        or baseline.confidence is not Confidence.HIGH
        or baseline_idle_ms is None
        or not math.isfinite(baseline_idle_ms)
        or baseline_idle_ms <= quiet_floor_ms
    ):
        raise InvalidBaselineError(
            f"trial {index + 1}: baseline is not safely quiet "
            f"(state={baseline.state.value}, confidence={baseline.confidence.value}, "
            f"idle_ms={baseline_idle_ms!r}; require QUIET/HIGH with finite "
            f"idle_ms > {quiet_floor_ms:.1f})"
        )

    human_delay = random.uniform(HUMAN_DELAY_MIN_S, HUMAN_DELAY_MAX_S)
    out_path = tmp_dir / f"human_{index}.json"
    proc = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--human-inject",
            "--display",
            display_name,
            "--delay",
            f"{human_delay:.6f}",
            "--out",
            str(out_path),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    text = _characters_for_window(WINDOW_S, CADENCE_S)
    start = time.monotonic()
    chars_typed = 0
    detected = False
    detection_latency_ms: float | None = None
    margin_ms: float | None = None
    guard_ms: float | None = None
    error_repr: str | None = None
    false_positive_before_human = False

    # Production cadence (\u00a75.2/O5): this harness supplies the ~60ms
    # inter-character delay explicitly, via `time.sleep`, between each
    # single-character `type_text` call - the shipped implementation itself
    # has no artificial delay (measured: ~0.03ms/char raw XTEST speed), so
    # without an explicit pacing here the entire 6s window's worth of
    # characters would be injected in a few milliseconds, leaving the rest
    # of the window with NO further `before_event()` sampling at all (and
    # therefore no chance to ever see a human event landing later in the
    # window). See the module docstring, point 2.
    for ch in text:
        elapsed = time.monotonic() - start
        if elapsed >= WINDOW_S:
            break
        char_start = time.monotonic()
        try:
            backend.type_text(ch, guard=guard)
            chars_typed += 1
        except HaltedError as exc:
            detection_time = time.time()
            detected = True
            margin_ms = exc.snapshot.margin_ms
            guard_ms = exc.snapshot.guard_ms
            error_repr = repr(exc)
            elapsed_before_detect = time.monotonic() - start
            if elapsed_before_detect < human_delay:
                false_positive_before_human = True
            # Wait for the human-inject subprocess to finish and report its
            # OWN recorded fire time, so latency is measured against a
            # timestamp this process never controlled.
            proc.wait(timeout=10)
            if out_path.exists():
                fired_at = float(json.loads(out_path.read_text())["fired_at"])
                detection_latency_ms = (detection_time - fired_at) * 1000.0
            break
        except BackendError as exc:
            error_repr = repr(exc)
            break
        remaining = CADENCE_S - (time.monotonic() - char_start)
        if remaining > 0:
            time.sleep(remaining)

    if proc.poll() is None:
        proc.wait(timeout=WINDOW_S + 10)

    return TrialResult(
        index=index,
        baseline_idle_ms=baseline_idle_ms,
        human_delay_s=human_delay,
        detected=detected,
        false_positive_before_human=false_positive_before_human,
        detection_latency_ms=detection_latency_ms,
        margin_ms=margin_ms,
        guard_ms=guard_ms,
        chars_typed=chars_typed,
        error_repr=error_repr,
        released_on_halt=bool(released),
    )


# ---------------------------------------------------------------------------
# Main: run N trials, aggregate, print PASS/FAIL
# ---------------------------------------------------------------------------


def _run_gate(n_trials: int, display_name: str) -> int:
    from amplifier_module_tool_computer_use.linux_x11 import LinuxX11Backend
    from amplifier_module_tool_computer_use.presence import (
        GUARD_MS,
        QUIET_FLOOR_SECONDS,
    )

    backend = LinuxX11Backend({"display": display_name})
    probe = backend.probe()
    if not probe.available:
        print(f"FAIL: backend unavailable on {display_name!r}: {probe.reason}")
        return 2

    # LinuxX11Backend lazily verifies its discrete-input path from
    # `type_text()`. Warm it up before any timed trial so that first-use setup
    # neither appears in trial 0 nor injects a key. Empty text reaches that
    # setup path but emits no input events.
    try:
        backend.type_text("")
    except Exception as exc:  # noqa: BLE001 - this invalidates the ship gate
        print(f"FAIL: setup warmup failed on {display_name!r}: {exc!r}")
        return 2

    guard_ms = GUARD_MS["linux-x11"]
    predicted_masked_fraction = guard_ms / (CADENCE_S * 1000.0)

    tmp_dir = Path(f"/tmp/verify_coexistence_{os.getpid()}")
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # Settle gap before every trial (defect 1's fix, presence.py::_classify):
    # each trial constructs a brand-new PresenceMonitor with no injection
    # history of its own, but all trials share ONE live display whose real
    # idle counter does not reset between them. Back-to-back trials with no
    # gap would let trial N's own synthetic typing (or its human-inject
    # subprocess's event) still be "recent" idle when trial N+1's guard
    # takes its first-ever sample - exactly the ambiguous case defect 1's
    # fix now correctly refuses to call QUIET, so it reports HUMAN_ACTIVE
    # for something that was actually this SAME harness's own prior trial,
    # not a real human. That is a real property of the fix (see this
    # method's own defect-1 comment above), not a bug in it - but it is
    # also not what this gate measures (the human-inject subprocess for
    # THIS trial hasn't even fired yet in that case). A settle gap longer
    # than QUIET_FLOOR_SECONDS before each trial gives each trial a genuinely
    # quiet baseline before its own timed window starts, matching a real
    # session boundary (see `halt_state.py`'s own evaluation evidence: a
    # real handoff between sessions was ~80s apart, not milliseconds).
    settle_s = QUIET_FLOOR_SECONDS + 0.5

    results: list[TrialResult] = []
    t_gate_start = time.monotonic()
    for i in range(n_trials):
        time.sleep(settle_s)
        try:
            result = _run_one_trial(i, backend, display_name, tmp_dir)
        except InvalidBaselineError as exc:
            print(f"FAIL: invalid test environment: {exc}")
            return 2
        results.append(result)
        status = "DETECTED" if result.detected else "MISSED"
        # Defect 1's fix (presence.py::_classify) makes a genuine, correct
        # detection possible with `margin_ms=None`: a human event landing
        # before this guard's very first `record_inject()` call (e.g. before
        # the agent has typed even its first character this trial) has no
        # `our_last_inject` to compute a margin against - the fix reports
        # HUMAN_ACTIVE anyway (see that method's docstring), but there is no
        # margin number to print. Report that plainly rather than crashing
        # the gate on a `None`-format - detected-with-no-margin is a real,
        # valid outcome now, not a formatting oversight to hide.
        margin_repr = (
            f"{result.margin_ms:+.2f}ms" if result.margin_ms is not None else "n/a"
        )
        print(
            f"trial {i + 1:>3}/{n_trials}: baseline_idle_ms="
            f"{result.baseline_idle_ms:8.1f} delay={result.human_delay_s:6.3f}s "
            f"chars_typed={result.chars_typed:>4} {status}"
            + (
                f" margin={margin_repr} latency={result.detection_latency_ms:.2f}ms"
                if result.detected and result.detection_latency_ms is not None
                else ""
            )
            + (
                " *** FALSE POSITIVE (before human event) ***"
                if result.false_positive_before_human
                else ""
            )
            + (
                f" error={result.error_repr}"
                if result.error_repr and not result.detected
                else ""
            ),
            flush=True,
        )
    gate_elapsed = time.monotonic() - t_gate_start

    n = len(results)
    detections = [r for r in results if r.detected]
    misses = [r for r in results if not r.detected]
    false_positives = [r for r in results if r.false_positive_before_human]
    detection_rate = len(detections) / n if n else 0.0
    measured_masked_fraction = len(misses) / n if n else 1.0
    latencies = [
        r.detection_latency_ms for r in detections if r.detection_latency_ms is not None
    ]
    margins = [r.margin_ms for r in detections if r.margin_ms is not None]
    released_ok = all(r.released_on_halt for r in detections)

    # 2-sigma tolerance around the predicted per-event masked fraction,
    # binomial variance at n trials (\u00a711 item 2's acceptance rule).
    p = predicted_masked_fraction
    sigma = (p * (1 - p) / n) ** 0.5 if n else 0.0
    masked_fraction_upper_bound = p + 2 * sigma

    print()
    print("=" * 78)
    print(
        "SHIP GATE: sustained-injection interleave test (docs/designs/coexistence.md \u00a711)"
    )
    print("=" * 78)
    print("platform:                  linux-x11")
    print(f"trials:                    {n}")
    print(f"cadence (production):      {CADENCE_S * 1000:.1f} ms/char")
    print(f"window per trial:          {WINDOW_S:.1f} s")
    print(f"GUARD (measured, O5):      {guard_ms:.1f} ms")
    print(f"wall time for gate:        {gate_elapsed:.1f} s")
    print()
    print(f"detections:                {len(detections)}/{n}")
    print(f"misses:                    {len(misses)}/{n}")
    print(f"detection rate:            {detection_rate * 100:.1f}%")
    print(f"false positives:           {len(false_positives)}")
    print(f"release_all fired on halt: {released_ok} ({len(detections)} detections)")
    print()
    print("detection latency distribution (ms, detection-time minus the human")
    print("subprocess's own recorded fire time):")
    if latencies:
        latencies_sorted = sorted(latencies)
        print(f"  n={len(latencies)}")
        print(f"  min    = {min(latencies_sorted):.2f}")
        print(f"  p50    = {statistics.median(latencies_sorted):.2f}")
        print(f"  mean   = {statistics.fmean(latencies_sorted):.2f}")
        if len(latencies_sorted) > 1:
            print(f"  stdev  = {statistics.stdev(latencies_sorted):.2f}")
        print(f"  max    = {max(latencies_sorted):.2f}")
    else:
        print("  (no detections - see FAIL below)")
    print()
    if margins:
        print(
            f"margin_ms at detection: min={min(margins):.2f} max={max(margins):.2f} "
            f"mean={statistics.fmean(margins):.2f}"
        )
    print()
    print(
        f"predicted per-event masked fraction (GUARD/cadence): {predicted_masked_fraction * 100:.2f}%"
    )
    print(
        f"measured per-event masked fraction (misses/trials):  {measured_masked_fraction * 100:.2f}%"
    )
    print(
        f"acceptance bound (predicted + 2\u03c3, \u03c3={sigma * 100:.2f}%):        {masked_fraction_upper_bound * 100:.2f}%"
    )
    print()

    # -- acceptance criteria, \u00a711 item 2 + \u00a712 -----------------------------
    checks: list[tuple[str, bool]] = [
        ("zero false positives", len(false_positives) == 0),
        ("detection rate >= 90% (single event, Linux)", detection_rate >= 0.90),
        (
            "measured masked fraction <= predicted + 2\u03c3",
            measured_masked_fraction <= masked_fraction_upper_bound,
        ),
        ("release_all fired on every detected halt", released_ok),
        ("at least one real detection occurred", len(detections) > 0),
    ]

    print("-" * 78)
    all_pass = True
    for name, ok in checks:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}")
        all_pass = all_pass and ok
    print("-" * 78)

    if all_pass:
        print("VERDICT: PASS")
        return 0
    print("VERDICT: FAIL")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--human-inject", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--display", default=os.environ.get("DISPLAY", ":99"))
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("--out", default="")
    parser.add_argument("--trials", type=int, default=100)
    args = parser.parse_args()

    if args.human_inject:
        _run_human_inject(args.display, args.delay, args.out)
        return 0

    return _run_gate(args.trials, args.display)


if __name__ == "__main__":
    raise SystemExit(main())
