"""The presence detector - `docs/designs/coexistence.md` \u00a75.

Answers one question, cheaply and per elementary event: *did a human just touch
this machine?* The mechanism is idle-time reconciliation (O5, U1c): the agent
already knows exactly when it last injected a synthetic event
(`our_last_inject`, `time.monotonic()`); the OS idle counter tells it when *any*
input (synthetic or real) last landed (`idle`). If the OS's most-recent-input
time is *later* than our own last injection by more than `GUARD`, something
else touched the machine - a human.

Why this shape, specifically
-----------------------------
Revision 1 of the design recorded one timestamp per *operation* and used
`GUARD = 250ms`. Both were wrong, and O5 (`docs/designs/coexistence-probes.md`)
proved it: at 250ms, a human keystroke landing mid-`type_text` (60ms injection
cadence) is invisible for the *entire* duration of the operation, because the
guard band never clears between the agent's own injections. The fix proven
there and implemented here:

* `record_inject()` is called after **every elementary event** (every
  keystroke, not once per `type_text` call).
* `GUARD` is single-digit milliseconds on Linux (5ms - the number O5 measured
  zero false positives at, with the one real human event caught at
  margin=+25.1ms across 98 samples).
* The detector samples **once per inter-injection gap**, not once per
  operation - `sample()` is meant to be called right before the *next*
  injection, exactly matching O5's harness shape (inject, sample, inject,
  sample, ...).

Clocks: everything here is `time.monotonic()` on ONE machine (the target this
detector runs on). Comparing a controller-side timestamp against a target-side
idle counter would require the two clocks to agree to within `GUARD` - orders
of magnitude tighter than tailnet NTP skew provides (\u00a75.1). This module is
never handed a foreign timestamp; the `idle_source` callable it is constructed
with must itself be local to the machine it reads.

Confidence is a margin test, not a probability
------------------------------------------------
`confidence` in the returned `PresenceSnapshot` is exactly two values:
`"high"` (the margin comparison is unambiguous) or `"low"` (idle is readable
but stale, so "quiet" is asserted without a fresh confirming sample).
Inventing a continuous probability from one jitter measurement would be a
confidence costume the design explicitly rejects (\u00a75.3).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

#: Per-platform GUARD bands (milliseconds), \u00a75.5.
#:
#: Linux: proven directly by O5 - 98 samples at 60ms agent-injection cadence,
#: zero false positives, one true detection at margin=+25.1ms. 5ms is the
#: number the evidence supports, not a guess.
#:
#: Windows: O4 is answered. Measured directly on a real Windows 11 desktop
#: (`windows-host`, over its live WSL2 interop boundary - `SendInput` zero
#: visible-impact relative moves, `dx=0, dy=0`, per U1b) with THREE
#: independent 300-sample runs (900 samples total) of the reconciliation
#: margin `PresenceMonitor` actually computes: `T_inject` and `T_now` both
#: captured via `GetTickCount()` (the same coarse clock `GetLastInputInfo`'s
#: `dwTime` lives on - matching \u00a75.5's "sampling loop lives inside the
#: bridge script" shape, same process, same clock, no cross-machine skew),
#: 60ms apart (O5's `type_text` cadence), `margin_ms = dwTime - T_inject`:
#:   run 1 (n=300): min=0.000 p50=0.000 p90=0.000 p95=1.570 p99=2.612
#:                  mean=0.972 MAX=16.000ms, 19/300 (6.33%) exceed 8ms
#:   run 2 (n=300): min=0.000 p50=0.000 p90=0.000 p95=0.000  p99=16.000
#:                  mean=0.880 MAX=16.000ms, 17/300 (5.67%) exceed 8ms
#:   run 3 (n=300): min=0.000 p50=0.000 p90=0.000 p95=0.000  p99=16.000
#:                  mean=0.470 MAX=16.000ms,  9/300 (3.00%) exceed 8ms
#: All three runs independently top out at EXACTLY 16.000ms - the ceiling
#: of Windows' documented 10-16ms `GetTickCount` tick, not a fluke of one
#: run - and this happens often enough (3-6% of samples) to be the ordinary
#: case, not a rare tail event the way macOS's single 8.56ms outlier was.
#: Sweeping candidate GUARD values against the combined 900 samples:
#:   GUARD= 8ms -> 45/900 exceed (5.00% false positives)
#:   GUARD=16ms ->  0/900 exceed (0.00% false positives) - exactly the max
#:   GUARD=20ms ->  0/900 exceed (0.00% false positives)
#:   GUARD=32ms ->  0/900 exceed (0.00% false positives)
#: 20ms is adopted: the smallest round band with real headroom (25%) above
#: the observed AND documented ceiling (16ms), mirroring the same
#: conservative judgement applied to macOS's 10ms (also ~17% headroom
#: above its observed max) rather than shaving to the max itself. This
#: replaces the previous 32ms figure, which was a sound but unverified
#: inference ("~2x the worst-case tick") - real hardware confirms the
#: worst-case tick itself (16ms), so 32ms was carrying 2x headroom where
#: 1.25x already covers every sample observed. \u00a75.5's Windows conclusion
#: is UNCHANGED by this tightening: masked fraction at 60ms `type_text`
#: cadence is `20/60 = 33%` (worse than Linux's 8.3%/macOS's 16.7%) -
#: intra-op detection remains not viable on Windows at ANY of the bands
#: this sweep tested (8ms through 32ms all fail the masking-fraction bar),
#: so `"intra_op_detection": false` (\u00a75.5, D4) is unaffected; only the
#: op-granularity number changed, and got smaller.
#:
#: macOS: O4 is answered. `CGEventSourceSecondsSinceLastEventType`'s
#: resolution was measured directly on real hardware (a live MacBook,
#: macOS 26.6 arm64): 300 samples of inject-to-visible-in-idle latency -
#: n=300, p50=0.58ms p90=1.23ms p95=3.31ms p99=7.77ms mean=0.86ms
#: max=8.56ms. Sweeping candidate GUARD values against that distribution:
#:   GUARD= 5.0ms ->  6/300 exceed (2.00% false positives), masked@60ms =  8.3%
#:   GUARD= 8.0ms ->  2/300 exceed (0.67% false positives), masked@60ms = 13.3%
#:   GUARD=10.0ms ->  0/300 exceed (0.00% false positives), masked@60ms = 16.7%
#: 10ms is the smallest band with ZERO false positives against the full
#: 300-sample run, and is adopted with headroom above the observed 8.56ms
#: max rather than shaved to the max itself - the same conservative
#: judgement O5 already applies on Linux. Worth recording so nobody
#: re-tightens this from a smaller run later: an earlier, separate
#: 30-sample pilot produced one 22.56ms outlier, far outside the
#: distribution above. That pilot is NOT authoritative; the 300-sample
#: run supersedes it and is what this constant is based on - any future
#: re-measurement must be against an equal-or-larger sample, never against
#: the 30-sample pilot's single outlier.
GUARD_MS: dict[str, float] = {
    "linux-x11": 5.0,
    "windows-wsl2": 20.0,  # measured, O4 - 900-sample (3x300) run, see comment above
    "macos": 10.0,  # measured, O4 - 300-sample run, see comment above
}

#: Whether GUARD_MS for a platform is evidence-backed (O5, Linux) or a
#: conservative placeholder awaiting a probe (O4, macOS). Exposed so callers
#: (and `intra_op_detection` reporting, \u00a75.5) can tell the two apart honestly
#: rather than presenting an unmeasured number as if it were proven.
#: The distinction this flag draws is "measured on real hardware" vs "reasoned
#: from documentation". Both can be *correct*; only one is *evidence*. Keeping
#: them apart is what lets `intra_op_detection` reporting stay honest instead of
#: presenting an inference as a proof.
GUARD_MEASURED: dict[str, bool] = {
    # O5: 98 samples at 60ms cadence, zero false positives, one true detection.
    "linux-x11": True,
    # O4-win: NOW measured on real hardware (the WSL2 side of a live Win11
    # desktop) - three independent 300-sample runs, 900 samples total, of
    # inject-to-visible-in-idle latency using the production `SendInput`
    # (dx=0,dy=0) path. All three runs cap at EXACTLY 16.000ms, which is the
    # documented `GetTickCount` tick ceiling rather than a sampling fluke.
    # Sweep across all 900: 8ms -> 45 exceed; 16ms -> 0; 20ms -> 0; 32ms -> 0.
    # 20.0 is the smallest round band with real headroom (25%) over that
    # ceiling - the same conservative ratio applied to macOS's 10ms.
    # Note this REPLACED an inferred 32.0. The inference was sound but untested;
    # the measurement tightened it. Intra-`type_text` detection remains not
    # viable on Windows at any of these bands (masked fraction 20/60 = 33% at
    # production cadence) - unchanged conclusion, now evidence-backed.
    # OPEN QUESTION, recorded so it is not lost: a live end-to-end test DID
    # halt a real ComputerTool.execute() write at 297ms idle, with idle
    # collapsing 58s -> 94ms. But SendInput is refused (ret=0) from every
    # process reachable over SSH, including the detached .ps1 path that test
    # used. So SOMETHING produced input the guard attributed to a human, and it
    # was not the synthetic firer. The remaining candidate is the agent's OWN
    # writes through bridge.ps1 not being recorded via record_inject() in time
    # - which would make that halt a FALSE POSITIVE rather than a detection.
    # Unresolved. Do not treat the 297ms halt as proof of human detection until
    # this is settled; it is currently only proof that the halt PATH executes.
    #
    # MEASURED ON REAL HARDWARE 2026-08-02 - windows-host, the WSL2 side of a
    # live Win11 desktop (hostname EXAMPLE-DESKTOP, x86_64). n=300, ZERO false
    # positives at 20.0ms.
    # To be unambiguous about "local", since it is easy to misread: the SCRIPT
    # ran ON that box. "Local" means the timing loop ran inside ONE PowerShell
    # process on that Windows box with no SSH round trip INSIDE the loop - as
    # opposed to an earlier run that sampled across the wire and measured
    # ~780ms of transport latency instead of the guard band. The dev box this
    # was driven from is aarch64 Linux with no PowerShell and no /mnt/c, so
    # this measurement could not have run there at all. Artifact left in place
    # at r13:C:\Windows\Temp\cu_g11.txt, first line CONTROL=48812,49125,
    # matching the control figures below.
    # Reuses bridge.ps1's OWN Add-Type block verbatim:
    #     CONTROL (no injection): idle 48812 -> 49125ms  (monotonic - sane)
    #     SendInput rejections:   0/300
    #     min=0.0 p50=0.0 p90=0.0 p95=0.0 p99=16.0 max=16.0 mean=0.42
    #     sweep:  5ms -> 8/300 exceed (2.67%)
    #            16ms -> 0/300
    #            20ms -> 0/300
    # The distribution caps at exactly 16.0ms, the documented GetTickCount tick
    # ceiling. 20.0 has real headroom over it; note 5ms (Linux's band) would be
    # UNSAFE here - 2.67% false positives.
    #
    # This took nine attempts, and the reasons are worth keeping because they
    # are all the same reason: I re-derived primitives that already existed in
    # this file. bridge.ps1:147 `move` is SetCursorPos - it moves the cursor
    # WITHOUT registering input; bridge.ps1:142 `key` is SendInput - real input.
    # Seven attempts used a hand-written INPUT struct SendInput rejected
    # (ret=0), or `move`, and I twice published a wrong root cause from it
    # (once "UIPI blocks injection", once "move() works"). Two attempts mixed a
    # managed Stopwatch with GetTickCount, which bridge.ps1's own line-45
    # comment explicitly warns against. The measurement succeeded the moment it
    # reused bridge.ps1's marshalling and clock pair instead of reinventing
    # them. CURSOR MOVEMENT IS NOT INPUT REGISTRATION; assert SendInput != 0
    # and gate on idle actually dropping before trusting any sample.
    "windows-wsl2": True,
    # O4: measured directly on a live MacBook (macOS 26.6 arm64) - 300 samples,
    # max 8.56ms, and a 10ms band with 0/300 false positives. This is the
    # strongest evidence of the three platforms.
    "macos": True,
}

#: \u00a75.4 - once a human is seen, they are latched present for a full minute
#: of silence. Converts a noisy per-sample read into a stable state and models
#: the human who is reading rather than typing (\u00a72.2).
LATCH_DECAY_SECONDS = 60.0

#: \u00a75.4 - "nobody has typed for this long" needed before a `margin < -GUARD`
#: read (our own input is most recent) is asserted `quiet, high` rather than
#: `quiet, low`. Purely a confidence label; it never gates the halt invariant.
QUIET_FLOOR_SECONDS = 2.0


class PresenceState(str, Enum):
    UNKNOWN = "unknown"
    QUIET = "quiet"
    HUMAN_ACTIVE = "human_active"


class Confidence(str, Enum):
    HIGH = "high"
    LOW = "low"


class IdleUnreadableError(RuntimeError):
    """`idle_source()` could not report a value.

    \u00a79.6: this is a hard error, never silently treated as `quiet` - "assume
    nobody's there" is exactly the incorrect assumption behind the incident
    this feature exists to prevent.
    """


@dataclass(frozen=True)
class PresenceSnapshot:
    """The `presence` block attached to every result, \u00a75.3.

    `transport_latency_ms` / `effective_staleness_ms` (\u00a75.7): a remote
    `idle_source()` crosses SSH (and, on Windows, may start PowerShell), while
    a local read is in-process. `transport_latency_ms` is the ACTUAL
    wall-clock cost of this snapshot's idle read; it is never a fixed,
    invented, or looked-up network value. A former Windows-host benchmark
    (n=80, 296-875ms) is historical evidence that motivated measurement, not
    a statement about this connection. `effective_staleness_ms = guard_ms +
    transport_latency_ms` reports the sample's measured blind window. It
    never changes `_classify()` or the platform `guard_ms` threshold.
    """

    state: PresenceState
    confidence: Confidence
    basis: str
    last_human_input_ago_ms: float | None
    margin_ms: float | None
    guard_ms: float
    guard_measured: bool
    sample_interval_ms: float | None
    latched_until_ms: float | None
    #: \u00a75.7 - additive, defaults to 0.0 so every pre-existing construction
    #: site (tests, `halt_state.py`'s durable-record rebuild) stays valid
    #: unchanged. Always populated by `PresenceMonitor.sample()` itself.
    transport_latency_ms: float = 0.0

    @property
    def effective_staleness_ms(self) -> float:
        """\u00a75.7: the honest size of this sample's blind window - `guard_ms`
        plus whatever this specific `idle_source()` call actually cost in
        wall-clock time. Distinct from, and never a replacement for,
        `guard_ms` itself (that stays exactly the per-platform measured
        constant from `GUARD_MS` - see that table's own provenance
        comments)."""
        return self.guard_ms + self.transport_latency_ms

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "confidence": self.confidence.value,
            "basis": self.basis,
            "last_human_input_ago_ms": self.last_human_input_ago_ms,
            "margin_ms": self.margin_ms,
            "guard_ms": self.guard_ms,
            "guard_measured": self.guard_measured,
            "sample_interval_ms": self.sample_interval_ms,
            "latched_until_ms": self.latched_until_ms,
            "transport_latency_ms": self.transport_latency_ms,
            "effective_staleness_ms": self.effective_staleness_ms,
        }


@dataclass
class PresenceMonitor:
    """Per-elementary-event presence detector with a latch, \u00a75.2-\u00a75.4.

    `idle_source` returns the OS idle time in **milliseconds** (time since the
    last input event of any kind, synthetic or real) as measured on the
    machine this monitor runs against - e.g. `MIT-SCREEN-SAVER`'s
    `idle` field on Linux (see `linux_x11_idle_source`). It must be cheap
    (microseconds) and must raise on failure - never guess a fallback value.

    `platform` selects the GUARD band from `GUARD_MS`/`GUARD_MEASURED` above.
    An unknown platform name is a configuration error, not swallowed to a
    default - a silently-wrong guard band is precisely the defect O5 found in
    revision 1 (\u00a75.3).

    This class has NO configuration key, constructor parameter, or method
    that widens/disables the guard band or the human_active determination
    itself - that is deliberate (\u00a76.0's halt invariant sits one layer up in
    `coexistence_guard.CoexistenceGuard` and is likewise unconditional). See
    `test_halt_invariant.py`.
    """

    idle_source: Callable[[], float]
    platform: str
    _last_inject_monotonic: float | None = field(default=None, init=False)
    _last_sample_monotonic: float | None = field(default=None, init=False)
    _state: PresenceState = field(default=PresenceState.UNKNOWN, init=False)
    _latched_until: float | None = field(default=None, init=False)
    _last_snapshot: PresenceSnapshot | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.platform not in GUARD_MS:
            raise ValueError(
                f"unknown platform {self.platform!r} for PresenceMonitor; "
                f"known platforms: {sorted(GUARD_MS)}"
            )

    @property
    def guard_ms(self) -> float:
        return GUARD_MS[self.platform]

    @property
    def guard_measured(self) -> bool:
        return GUARD_MEASURED[self.platform]

    @property
    def state(self) -> PresenceState:
        return self._state

    @property
    def last_snapshot(self) -> PresenceSnapshot | None:
        return self._last_snapshot

    def record_inject(self, *, at: float | None = None) -> None:
        """Call immediately around every elementary injection syscall.

        \u00a75.2: "on every elementary event - not once per operation." A
        200-character `type_text` calls this 200 times, not once.
        """
        self._last_inject_monotonic = at if at is not None else time.monotonic()

    def sample(self, *, now: float | None = None) -> PresenceSnapshot:
        """Read idle, reconcile against our own last injection, update the
        latch, and return the snapshot (\u00a75.3/\u00a75.4).

        Intended to be called once per inter-injection gap - i.e. right
        before the *next* injection - matching O5's harness shape exactly.

        \u00a75.7 - measured safety gap fix: `now` is the anchor every downstream
        comparison treats as "the instant this observation became true." For
        an in-process `idle_source()` (Linux/macOS, microseconds) it does not
        matter whether `now` is captured immediately before or after that
        call. For a REMOTE `idle_source()` (`RemoteBackend.presence_idle_ms`,
        an SSH round trip - and on Windows a fresh `powershell.exe` spawn per
        read) it matters: the read returns after the target observation.
        Capturing `now` BEFORE issuing that call (the previous behavior)
        anchors every comparison to a moment up to a full round trip BEFORE
        `idle_ms` was actually valid, which silently manufactures staleness on
        top of whatever the transport already costs - `idle_ms` reflects the
        target's idle counter at roughly the END of the round trip, not the
        start. Capturing `now` immediately AFTER `idle_source()` returns (the
        fix here) anchors the comparison to the moment the observation is
        actually usable, cutting that self-inflicted bias to (at most) the
        transport's own response-transit tail - the one piece genuinely
        outside this process's ability to measure or correct without
        instrumenting the remote side. `transport_latency_ms` (measured
        below, \u00a75.7) reports the conservative upper bound on what remains:
        the FULL measured cost of the `idle_source()` call, never a smaller,
        invented "residual" this process cannot actually verify.

        Explicit `now=` (every existing unit test, using a fake clock) is
        left untouched by this fix - those tests already control both the
        call and its timestamp themselves, so nothing about their behavior
        changes.
        """
        explicit_now = now is not None
        t_call_start = time.monotonic()

        try:
            idle_ms = float(self.idle_source())
        except Exception as exc:  # any read failure -> unknown, never a guess
            transport_latency_ms = (time.monotonic() - t_call_start) * 1000.0
            bookkeeping_now = now if explicit_now else t_call_start
            interval_ms: float | None = None
            if self._last_sample_monotonic is not None:
                interval_ms = (bookkeeping_now - self._last_sample_monotonic) * 1000.0
            self._last_sample_monotonic = bookkeeping_now
            self._state = PresenceState.UNKNOWN
            snap = PresenceSnapshot(
                state=PresenceState.UNKNOWN,
                confidence=Confidence.LOW,
                basis="idle_unreadable",
                last_human_input_ago_ms=None,
                margin_ms=None,
                guard_ms=self.guard_ms,
                guard_measured=self.guard_measured,
                sample_interval_ms=interval_ms,
                latched_until_ms=self._latched_until,
                transport_latency_ms=transport_latency_ms,
            )
            self._last_snapshot = snap
            raise IdleUnreadableError(f"idle_source() raised: {exc}") from exc

        t_call_end = time.monotonic()
        transport_latency_ms = (t_call_end - t_call_start) * 1000.0
        resolved_now = now if explicit_now else t_call_end

        interval_ms = None
        if self._last_sample_monotonic is not None:
            interval_ms = (resolved_now - self._last_sample_monotonic) * 1000.0
        self._last_sample_monotonic = resolved_now

        inferred_last_input = resolved_now - (idle_ms / 1000.0)

        if self._last_inject_monotonic is None:
            # No injection has happened yet this session - nothing of ours to
            # reconcile against. Any measured idle simply reports quiet/active
            # by the QUIET_FLOOR rule below, basis stays idle_reconciliation.
            margin_ms: float | None = None
        else:
            margin_ms = (inferred_last_input - self._last_inject_monotonic) * 1000.0

        state, confidence = self._classify(margin_ms, idle_ms)

        if state is PresenceState.HUMAN_ACTIVE:
            self._latched_until = resolved_now + LATCH_DECAY_SECONDS
        elif self._latched_until is not None and resolved_now < self._latched_until:
            # Latch (\u00a75.4): once seen, present until LATCH_DECAY_SECONDS of
            # silence - a fresh `quiet` sample within the latch window does not
            # clear it early.
            state = PresenceState.HUMAN_ACTIVE
            confidence = Confidence.HIGH
        elif self._latched_until is not None and resolved_now >= self._latched_until:
            self._latched_until = None

        self._state = state
        snap = PresenceSnapshot(
            state=state,
            confidence=confidence,
            basis="idle_reconciliation",
            last_human_input_ago_ms=idle_ms,
            margin_ms=margin_ms,
            guard_ms=self.guard_ms,
            guard_measured=self.guard_measured,
            sample_interval_ms=interval_ms,
            latched_until_ms=self._latched_until,
            transport_latency_ms=transport_latency_ms,
        )
        self._last_snapshot = snap
        return snap

    def _classify(
        self, margin_ms: float | None, idle_ms: float
    ) -> tuple[PresenceState, Confidence]:
        """Classify one raw margin observation as `HUMAN_ACTIVE` or `QUIET`.

        \u00a75.3 states the margin comparison as three bands (`margin > GUARD`,
        `|margin| <= GUARD`, `margin < -GUARD`) with the middle band labelled
        "unknown". \u00a75.4's latch **state machine**, however, only ever drives
        `UNKNOWN` from a failed idle read or the pre-first-sample state - there
        is no listed transition INTO `UNKNOWN` from `QUIET`/`HUMAN_ACTIVE` on
        an ordinary sample - and algebraically, `|margin| <= GUARD` is not a
        rare edge case: in the steady state where nothing but our own
        injections has ever touched the machine, `inferred_last_input`
        converges on `our_last_inject` exactly, so margin sits at
        (approximately) zero for as long as nothing new happens - this is the
        ORDINARY "quiet" condition, not an occasional ambiguity. Given that
        inconsistency between \u00a75.3's per-sample table and \u00a75.4's transition
        list, this resolves it in the direction that keeps the halt invariant
        meaningful and the reported state useful: a margin at or below
        `GUARD` is `QUIET` (confidence keyed off `idle_ms` vs.
        `QUIET_FLOOR_SECONDS`, so a sample taken moments after our own
        injection is honestly reported as *low*-confidence quiet, one taken
        long after is *high*-confidence quiet) rather than a bare `UNKNOWN`
        that would never resolve on its own. `UNKNOWN` remains reserved for
        exactly what \u00a75.4 lists: idle unreadable (`IdleUnreadableError`,
        raised - never returned as a state), and "no sample has ever been
        taken".
        """
        if margin_ms is None:
            # Defect 1 (live safety defect, fixed 2026-08-02): never injected
            # yet means this guard has NO `our_last_inject` to reconcile
            # `idle_ms` against - it has zero evidence that any recent input
            # was its own. Reporting `QUIET` here on a small `idle_ms` (the
            # pre-fix behavior) infers "nobody's there" from "we have no
            # information either way" - exactly backwards for a safety gate
            # (absence of evidence is not evidence of absence). Only a
            # genuinely long idle read - nothing has touched the machine
            # recently, by anyone, for at least QUIET_FLOOR_SECONDS - is
            # honestly QUIET on a first sample; anything more recent than
            # that can only be attributed to something else, since we have
            # not yet injected anything ourselves, so it is HUMAN_ACTIVE at
            # high confidence, the same fail-safe direction section 9.6
            # already takes for an unreadable idle counter.
            if idle_ms > QUIET_FLOOR_SECONDS * 1000.0:
                return PresenceState.QUIET, Confidence.HIGH
            return PresenceState.HUMAN_ACTIVE, Confidence.HIGH

        if margin_ms > self.guard_ms:
            return PresenceState.HUMAN_ACTIVE, Confidence.HIGH
        # margin_ms <= guard_ms: nothing but our own injection(s) is the most
        # recent input this reconciliation can see.
        if idle_ms > QUIET_FLOOR_SECONDS * 1000.0:
            return PresenceState.QUIET, Confidence.HIGH
        return PresenceState.QUIET, Confidence.LOW


def linux_x11_idle_source(display: Any) -> Callable[[], float]:
    """Build an `idle_source` callable reading `MIT-SCREEN-SAVER` idle time
    (milliseconds) from an already-connected `Xlib.display.Display`.

    Kept as a tiny factory (rather than importing Xlib at module top) so
    `presence.py` itself has zero display-server dependency and can be
    unit-tested with a fake `idle_source` with no X server present at all -
    see `tests/test_presence.py`.
    """

    def _read() -> float:
        info = display.screen().root.screensaver_query_info()
        return float(info.idle)

    return _read
