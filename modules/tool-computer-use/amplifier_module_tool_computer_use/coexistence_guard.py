"""The single guard - `docs/designs/coexistence.md` \u00a78.6's combined check,
composing presence detection, the halt invariant, pause, target binding, and
geometric exclusion into the one call site every elementary injected event
passes through:

    before each elementary event:
        if halted or paused:                 -> stop, release_all, report
        if coords in any excluded rect:       -> refuse, report
        if current_target != bound_target:    -> stop, release_all, report
        else emit
    [event emitted by the caller]
    after each elementary event:
        record our own injection timestamp
        sample presence -> latch human_active if detected

THE HALT INVARIANT (\u00a76.0) - the load-bearing property of this whole module
---------------------------------------------------------------------------
> A detected human halts writes before the next one - every platform, every
> mode, and no configuration key disables it.

`CoexistenceGuard.__init__` takes exactly one policy knob relevant to
"whether to keep going": `drive_anyway` (\u00a77.6/D5), and it governs ONLY
`check_start_permission()` - whether *this session may begin driving at all*
when a human is already detected at construction time. It has no effect on
`before_event()`'s halt check. There is no parameter, config key, or method on
this class that disables `before_event()` raising `HaltedError` once
`_halted` is set - `test_halt_invariant.py` verifies this by construction
(inspecting `__init__`'s signature) and functionally (setting `drive_anyway`,
detecting a human mid-session, and confirming the very next `before_event()`
still raises).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .exclusion import ExclusionZone
from .halt_state import PERSISTED_BASIS, resolve_resume_command
from .pause import DragState, PauseController, PausedError
from .presence import PresenceMonitor, PresenceSnapshot, PresenceState
from .target_binding import TargetBinding, TargetChangedError

logger = logging.getLogger(__name__)

#: Sentinel distinguishing "caller passed no current_target" (read fresh from
#: `target_source`) from "caller explicitly passed `current_target=None`"
#: (platform genuinely cannot determine a target - use that, don't re-read).
_UNSET = object()


class HaltedError(RuntimeError):
    """Raised by `before_event()` once a human has been detected (\u00a76.0).

    Unconditional: no config key, constructor argument, or opt-out reaches
    this raise. The only way past it is a human clearing the halt through the
    one channel that requires nothing of the controller at all - restarting a
    fresh driving session after they choose to (\u00a713, D3: resume is manual
    when a console user is present).
    """

    def __init__(self, snapshot: PresenceSnapshot) -> None:
        self.snapshot = snapshot
        # \u00a75.7: declare, don't fake - when this sample's own idle_source()
        # call cost real transport time (a remote backend), say so in the
        # same sentence a human/operator actually reads, not only in a field
        # they have to know to inspect. `transport_latency_ms` is ~0 for an
        # in-process (local) read, so this clause is silent for the common
        # case and only speaks up when there is something to declare.
        transport_note = ""
        if snapshot.transport_latency_ms > 1.0:
            transport_note = (
                f" (this observation crossed a transport costing "
                f"{snapshot.transport_latency_ms:.1f}ms - effective_staleness="
                f"{snapshot.effective_staleness_ms:.1f}ms, not guard_ms alone)"
            )
        if snapshot.basis == PERSISTED_BASIS:
            # This snapshot was loaded from `halt_state.py`'s durable, on-disk
            # record (`seed_halted()`, called from `_build_coexistence_guard`)
            # - a MEMORY of a human detected in a PRIOR session, not a live
            # sample. The live-sample wording below ("produced input Xms ago")
            # would be actively wrong here: real wall-clock time has passed
            # since detection - minutes, or the six weeks this exact
            # ambiguity was reported over - and `last_human_input_ago_ms` on
            # this snapshot is frozen at whatever it read at the ORIGINAL
            # session's detection, not "just now". Presenting it as present
            # tense invites exactly the wrong read - "someone's there right
            # now, I'll come back later" - when waiting is guaranteed never
            # to clear it; only an explicit human signal does.
            # The command below is RESOLVED against this process's own
            # interpreter (`resolve_resume_command()`, `halt_state.py`), not
            # a bare name assumed to be on PATH - `pip`/`uv` install console
            # scripts next to the interpreter that installed them, which is
            # not necessarily anywhere on a user's shell PATH. See that
            # function's docstring for why this can never emit a path that
            # doesn't exist.
            super().__init__(
                "halted: a PRIOR session recorded a human detected at this "
                "machine and persisted that fact to disk - this is a MEMORY, "
                "not a live reading. It does NOT mean someone is at the "
                "keyboard right now, and it will not clear itself no matter "
                "how long you wait - only an explicit human signal does. "
                "Halting before the next write. To resume: after confirming "
                f"it is safe, run `{resolve_resume_command()}` for this "
                "backend."
            )
        else:
            super().__init__(
                "halted: a human at this machine produced input "
                f"{snapshot.last_human_input_ago_ms:.1f}ms ago that this agent did "
                f"not generate (margin={snapshot.margin_ms}, "
                f"guard={snapshot.guard_ms}ms){transport_note}. "
                "Halting before the next write."
            )


class ExcludedCoordinateError(RuntimeError):
    """The requested coordinate falls inside a registered exclusion rect
    (e.g. the overlay's own Pause/Cancel buttons, \u00a77.5) - refused, not
    forwarded to the backend."""

    def __init__(self, name: str, x: int, y: int) -> None:
        self.name = name
        self.x = x
        self.y = y
        super().__init__(
            f"refused: coordinate ({x}, {y}) falls inside excluded rect "
            f"{name!r} - the agent cannot target its own overlay controls"
        )


@dataclass
class CoexistenceGuard:
    """One guard object per driving session, threaded through every
    elementary injected event.

    `presence` is the `PresenceMonitor` for this platform (constructed by the
    caller with the correct `idle_source`/`platform`, \u00a75). `release_all` is
    called (with `reason=`) whenever the guard aborts an in-flight operation
    for any reason that requires releasing held input - halt, pause, or a
    target change - matching \u00a76.0/\u00a78.4's "held inputs released immediately,
    via the existing ledger."
    """

    presence: PresenceMonitor
    release_all: Callable[[str], list[str]]
    drive_anyway: bool = False
    #: Optional callable returning the current delivery target id (e.g. the
    #: foreground window handle), or `None` if the platform cannot determine
    #: one right now. Reading this is the caller's only target-binding
    #: responsibility - `bind_target()`/`before_event()` do the rest, so a
    #: `type_text` implementation does not need to know target binding exists
    #: at all (\u00a78.6).
    target_source: Callable[[], str | None] | None = None
    #: Defect 2 fix (live safety defect, fixed 2026-08-02): optional callable
    #: consulted on every `before_event()` call (while this guard is not yet
    #: halted) for a durable, cross-session halt record that may have
    #: appeared SINCE this guard was constructed - e.g. a different session's
    #: guard, same backend, detected a human and persisted it after this one
    #: already mounted. `None` (the default, and every existing caller/test
    #: that predates this fix) makes this a complete no-op, identical to
    #: before defect 2 was closed - only `_build_coexistence_guard` in
    #: `__init__.py` supplies a real one
    #: (`halt_state.make_durable_halt_poll`). Additive only: a non-`None`
    #: result here can only ever call `seed_halted()`, which can only ever
    #: escalate `_halted` from `False` to `True`, never the reverse - the
    #: same one-way-latch property `seed_halted()` already documents.
    durable_halt_poll: Callable[[], PresenceSnapshot | None] | None = None
    #: Optional reporting-only observer for a successful live presence sample.
    #: It never controls classification, halt eligibility, or the rest of the
    #: guard: observer failures are contained in `_notify_presence_sample()`.
    on_presence_sample: Callable[[PresenceSnapshot], None] | None = None
    pause: PauseController = field(default_factory=PauseController)
    exclusion: ExclusionZone = field(default_factory=ExclusionZone)
    binding: TargetBinding = field(default_factory=TargetBinding)
    drag: DragState = field(default_factory=DragState)
    _halted: bool = field(default=False, init=False)
    _halt_snapshot: PresenceSnapshot | None = field(default=None, init=False)

    # -- target binding convenience (\u00a78.6) -----------------------------------
    def bind_target(self) -> None:
        """Bind to the current target at the start of a multi-event
        operation, via `target_source` (or unbound/unverified if no
        `target_source` was supplied - `TargetBinding.check()` is then a
        no-op, matching \u00a78.6's "where binding cannot be enforced, it must
        be declared" rather than silently pretending to enforce it)."""
        if self.target_source is not None:
            self.binding.bind(self.target_source())

    def release_target(self) -> None:
        self.binding.release()

    # -- start permission (D5, \u00a77.6) - governs BEGINNING to drive only ------
    def check_start_permission(self) -> None:
        """Call once, before the first write of a session. Raises
        `HaltedError` if a human is already detected AND `drive_anyway` is
        False. This is the ONLY method `drive_anyway` affects - it never
        reaches `before_event()`'s halt check (\u00a76.0 is unconditional)."""
        if self.presence.state is PresenceState.HUMAN_ACTIVE and not self.drive_anyway:
            assert self.presence.last_snapshot is not None
            raise HaltedError(self.presence.last_snapshot)
        if self.presence.state is PresenceState.HUMAN_ACTIVE and self.drive_anyway:
            logger.warning(
                "coexistence: drive_anyway=True - beginning to drive with a "
                "human detected present (logged per \u00a77.6, not silent)"
            )

    # -- the combined per-event guard (\u00a78.6's pseudocode, verbatim) ---------
    def before_event(
        self,
        *,
        coord: tuple[int, int] | None = None,
        current_target: str | None | object = _UNSET,
        progress: str = "",
    ) -> None:
        """Call before every elementary injected event (keystroke, click,
        motion sample). Raises (never silently skips) on any of: halted,
        paused, an excluded coordinate, or a target change. On halt/pause/
        target-change, `release_all()` has already been called by the time
        this raises.

        `current_target` is normally left unset - it is then read fresh from
        `target_source` (\u00a78.6: "re-reads the current target" before every
        elementary event), so a caller looping over keystrokes never needs to
        know target binding exists. Pass it explicitly only when the caller
        already has a fresher/cheaper read than another `target_source()`
        call would provide.

        Presence is sampled HERE, not in `after_event()` (\u00a75.2: "the detector
        samples once in every inter-injection interval"). Concretely: this
        sample compares idle against the injection timestamp recorded by the
        PREVIOUS `after_event()` call - the gap between them is exactly the
        window a human keystroke could land in undetected otherwise.
        Sampling and recording the injection back-to-back in the same call
        (no elapsed time between them) would make every margin read
        approximately zero and detect nothing - this ordering is load-bearing,
        not stylistic.

        \u00a79.6: idle-unreadable is a hard error for any mode that depends on
        it - `presence.sample()` raises `IdleUnreadableError` rather than
        guessing, and this method does not catch it: an existing halt is
        never cleared just because a later sample "couldn't tell".

        Defect 2 fix (live safety defect, fixed 2026-08-02): before sampling
        live presence, poll `durable_halt_poll` (if one was supplied) for a
        cross-session halt record that may have appeared since this guard
        was constructed - see `_poll_durable_halt` and the field's own
        docstring above.
        """
        self._poll_durable_halt()
        snap = self.presence.sample()
        self._notify_presence_sample(snap)
        if snap.state is PresenceState.HUMAN_ACTIVE:
            self._halted = True
            self._halt_snapshot = snap
        if self._halted:
            assert self._halt_snapshot is not None
            self.release_all("halted")
            raise HaltedError(self._halt_snapshot)
        try:
            self.pause.check(progress=progress)
        except PausedError:
            self.release_all("paused")
            raise
        if coord is not None:
            excluded = self.exclusion.contains(coord[0], coord[1])
            if excluded is not None:
                raise ExcludedCoordinateError(excluded, coord[0], coord[1])
        resolved_target = (
            self.target_source()
            if current_target is _UNSET and self.target_source is not None
            else (None if current_target is _UNSET else current_target)
        )
        try:
            self.binding.check(resolved_target)  # type: ignore[arg-type]
        except TargetChangedError:
            self.release_all("target_changed")
            raise

    def _notify_presence_sample(self, snapshot: PresenceSnapshot) -> None:
        """Run optional reporting after a successful sample without letting it
        weaken the halt/pause/target-binding path.

        Observers are deliberately called immediately after `sample()` and
        before classification can halt the write. A broken observer is only a
        reporting failure; it must never turn a detected human into a write.
        """
        if self.on_presence_sample is None:
            return
        try:
            self.on_presence_sample(snapshot)
        except Exception:  # noqa: BLE001 - reporting must not weaken safety
            logger.exception("coexistence: presence sample observer failed")

    def seed_halted(self, snapshot: PresenceSnapshot) -> None:
        """Mark this guard as already-halted, from construction, because a
        durable record says a human was detected in a PRIOR session on this
        same target and has not yet been explicitly cleared (\\u00a713, D3 -
        see `halt_state.py`).

        This is an ADDITIVE seed, never a clear: it can only ever cause
        `before_event()` to raise `HaltedError` sooner (immediately, on this
        guard's very first call) than it otherwise would - it has no way to
        make a guard less halted, and does not affect the sticky, one-way
        nature of `_halted` at all. `test_halt_invariant.py`'s
        `test_no_way_to_clear_a_latched_halt_from_the_guard_itself` still
        holds: this method's name is not in that forbidden set, and calling
        it can only ever add a halt, never remove one.
        """
        self._halted = True
        self._halt_snapshot = snapshot

    def _poll_durable_halt(self) -> None:
        """Defect 2 fix (live safety defect, fixed 2026-08-02): escalate to
        halted if a durable, cross-session halt record has appeared since
        this guard was constructed.

        A no-op in three cases, all deliberate: no `durable_halt_poll` was
        supplied (every caller/test that predates this fix - unchanged
        behavior); this guard is already halted (nothing further to detect,
        and it never gets this far again - `before_event()` raises before
        reaching this call); or the poll itself reports nothing new. Calling
        `seed_halted()` here is safe by construction - see that method's own
        docstring for why it can only ever escalate, never clear.
        """
        if self._halted or self.durable_halt_poll is None:
            return
        persisted = self.durable_halt_poll()
        if persisted is not None:
            self.seed_halted(persisted)

    def after_event(self) -> None:
        """Call immediately after every elementary injected event (right
        around the injection syscall, \u00a75.2): record our own injection
        timestamp. This is what the NEXT `before_event()` call's presence
        sample compares against - the two are deliberately split across two
        methods, with the caller's actual injection happening in between,
        so a real elapsed gap exists for a human event to land in.
        """
        self.presence.record_inject()

    @property
    def halted(self) -> bool:
        return self._halted

    def as_dict(self) -> dict[str, Any]:
        """The `presence` block for a tool result envelope (\u00a75.3), plus the
        guard-level facts (\u00a710.3) that ride alongside it."""
        snap = self.presence.last_snapshot
        out: dict[str, Any] = {
            "halted": self._halted,
            "paused": self.pause.is_paused,
            "target_binding": self.binding.status,
        }
        if snap is not None:
            out["presence"] = snap.to_dict()
        return out
