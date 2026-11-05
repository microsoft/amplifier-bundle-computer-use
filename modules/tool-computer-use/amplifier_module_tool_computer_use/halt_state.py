"""Durable, cross-session halt memory - the defect-2 fix.

`CoexistenceGuard` (`coexistence_guard.py`) is - by design and by
`test_halt_invariant.py::test_no_way_to_clear_a_latched_halt_from_the_guard_itself`
- a one-way latch: once `_halted` is set, nothing on the class can clear it.
The design's stated resume path is "a human clearing the halt through the one
channel that requires nothing of the controller at all - restarting a fresh
driving session after they choose to" (`coexistence_guard.py` module
docstring, citing `docs/designs/coexistence.md` \u00a713 D3: "resume is manual
when a console user is present").

That sentence has an unstated assumption: that a "fresh driving session"
only ever begins because a human chose to start one. Real evaluation data
(`.amplifier/evaluation/computer-use/20260802T113341Z/s2-interrupt-halt/`)
shows that assumption does not hold. A `computer-operator` sub-agent halted
five times over ~57s (session `...23365dc078194312...`, all against the
SAME in-memory `CoexistenceGuard`, which correctly stayed halted for the
rest of that sub-agent's life - `_halted` is sticky, verified directly
against the shipped classes, not assumed). Control then returned to the
PARENT session (`...22d78f0d-c698-43aa-961a-506af4dc1d59...`), which mounts
its OWN `ComputerTool`/`CoexistenceGuard` - a brand-new instance with no
memory of the halt at all - and its first `left_click` succeeded 80s after
the first halt, entirely automatically. No human ever chose to resume
anything; the orchestrator simply started a new session on its own.

`CoexistenceGuard`'s in-memory latch (`presence.LATCH_DECAY_SECONDS`) has no
way to survive that boundary - it is a plain dataclass field on an object
that stops existing when its mount does. This module adds the missing
layer: a small durable, per-platform record written to disk the moment a
real human-detected halt fires, and consulted the moment a NEW guard is
about to be built (`_build_coexistence_guard` in `__init__.py`). If the
record is present, the new guard is seeded already-halted
(`CoexistenceGuard.seed_halted`) - so the unconditional halt invariant
(\u00a76.0) now holds ACROSS session/mount boundaries, not just within one.

The record is cleared by exactly one path: `clear_halt()`, called only by a
human-operated entry point (`scripts/resume_after_halt.py`) - never by any
code on the automated tool-call path. There is no time-based expiry here on
purpose: the task this module exists to satisfy is "resume requires an
explicit signal, not the mere passage of time." `presence.LATCH_DECAY_SECONDS`
keeps doing its own, unrelated job (converting noisy per-sample reads into a
stable IN-SESSION state, \u00a75.4) - this module does not touch it, read it, or
duplicate its role.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .presence import Confidence, PresenceSnapshot, PresenceState

logger = logging.getLogger(__name__)

#: The console-script name `pyproject.toml` registers for `resume_cli:main`.
_RESUME_CONSOLE_SCRIPT = "amplifier-computer-use-resume"

#: The dotted module path `-m` can invoke directly - `resume_cli` is a
#: sibling module inside this exact package (`amplifier_module_tool_computer_use`).
_RESUME_MODULE = "amplifier_module_tool_computer_use.resume_cli"


def resolve_resume_command() -> str:
    """A resume command guaranteed runnable in THIS process's environment -
    resolved, never guessed.

    The defect this exists to close: `pip`/`uv` install the
    `amplifier-computer-use-resume` console script into the SAME directory
    as the interpreter that installed it (`sys.executable`'s own
    `bin`/`Scripts`) - never onto a user's shell PATH. A message that names
    the bare script sends an operator chasing `command not found` across
    directories and shells for a command that was never on PATH to begin
    with (that is exactly what shipped and what this fixes).

    Two branches, both unconditionally correct for this process, neither a
    guess:

    1. If the console script exists on disk next to this interpreter, hand
       back its full, absolute path - copy-pasteable, no PATH required.
    2. If it doesn't (partial install, a source checkout with no `pip
       install` step, a venv that never installed this module) - fall back
       to `<this interpreter> -m amplifier_module_tool_computer_use.resume_cli`.
       This branch cannot be wrong: `resume_cli` is a sibling module in the
       SAME package as this very file, which this exact interpreter has
       already imported successfully (it is running this code right now) -
       so that interpreter can always import it again.

    Never emits a path that doesn't exist and never hands back a bare
    command name for the operator to discover isn't on PATH the hard way.
    """
    script_name = _RESUME_CONSOLE_SCRIPT
    if sys.platform == "win32":
        script_name += ".exe"
    script_path = Path(sys.executable).resolve().parent / script_name
    if script_path.exists():
        return str(script_path)
    return f"{sys.executable} -m {_RESUME_MODULE}"


#: Mirrors `SHOT_DIR`'s convention (`__init__.py`) - a per-feature directory
#: under the same `~/.amplifier/computer-use/` root.
DEFAULT_STATE_DIR = Path.home() / ".amplifier" / "computer-use" / "halt"

_PRIVATE_DIR_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600

#: Distinguishes a seeded-from-disk snapshot from a live sample
#: (`presence.py`'s own `basis` values are `"idle_reconciliation"` and
#: `"idle_unreadable"`) - never invent a fake "this was just measured" basis
#: for a fact that is actually a memory of a PRIOR session.
PERSISTED_BASIS = "persisted_halt_from_prior_session"
PERSISTED_UNKNOWN_BASIS = "persisted_target_clock_failure"
TARGET_CLOCK_FAILURE_BASES = frozenset(
    {
        "target_clock_unreadable",
        "target_clock_unacknowledged",
        "target_precondition_rejected",
        "operator_presence_target_changed",
        "focus_transition_unverified",
    }
)


@dataclass(frozen=True)
class PersistedHalt:
    """One durable halt record for one platform/backend name."""

    platform: str
    detected_at: float  # time.time() wall clock - for humans reading the file
    reason: str
    last_human_input_ago_ms: float | None
    margin_ms: float | None
    guard_ms: float
    guard_measured: bool
    # Absent in old records: retain their original human-detection semantics.
    # Only new, explicitly UNKNOWN target-clock halts populate this field.
    basis: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "platform": self.platform,
            "detected_at": self.detected_at,
            "reason": self.reason,
            "last_human_input_ago_ms": self.last_human_input_ago_ms,
            "margin_ms": self.margin_ms,
            "guard_ms": self.guard_ms,
            "guard_measured": self.guard_measured,
        }
        if self.basis is not None:
            result["basis"] = self.basis
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PersistedHalt:
        return cls(
            platform=str(data["platform"]),
            detected_at=float(data["detected_at"]),
            reason=str(data.get("reason", "")),
            last_human_input_ago_ms=(
                float(data["last_human_input_ago_ms"])
                if data.get("last_human_input_ago_ms") is not None
                else None
            ),
            margin_ms=(
                float(data["margin_ms"]) if data.get("margin_ms") is not None else None
            ),
            guard_ms=float(data.get("guard_ms", 0.0)),
            guard_measured=bool(data.get("guard_measured", False)),
            basis=(
                data["basis"]
                if isinstance(data.get("basis"), str)
                and data["basis"] in TARGET_CLOCK_FAILURE_BASES
                else None
            ),
        )

    def to_snapshot(self) -> PresenceSnapshot:
        """Rebuild a `PresenceSnapshot` suitable for
        `CoexistenceGuard.seed_halted()` - honestly labelled as a durable
        memory (`PERSISTED_BASIS`), never as a fresh live sample."""
        unknown = self.basis in TARGET_CLOCK_FAILURE_BASES
        return PresenceSnapshot(
            state=PresenceState.UNKNOWN if unknown else PresenceState.HUMAN_ACTIVE,
            confidence=Confidence.LOW if unknown else Confidence.HIGH,
            basis=(
                f"{PERSISTED_UNKNOWN_BASIS}:{self.basis}"
                if unknown
                else PERSISTED_BASIS
            ),
            last_human_input_ago_ms=self.last_human_input_ago_ms,
            margin_ms=self.margin_ms,
            guard_ms=self.guard_ms,
            guard_measured=self.guard_measured,
            sample_interval_ms=None,
            latched_until_ms=None,
        )


def _safe_filename(platform: str) -> str:
    """Platform names are internal constants today (`"linux-x11"`, etc.) but
    treat them as untrusted anyway - this becomes a filename."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", platform)
    return f"{safe or 'unknown'}.json"


def _path_for(platform: str, state_dir: Path) -> Path:
    return state_dir / _safe_filename(platform)


def record_halt(
    platform: str,
    snapshot: PresenceSnapshot,
    *,
    reason: str,
    state_dir: Path = DEFAULT_STATE_DIR,
) -> None:
    """Write the durable halt record for `platform`. Called once per
    detected `HaltedError` (`ComputerTool.execute()`) - idempotent to call
    repeatedly (a still-halted session re-persists the same fact), and
    NEVER called from any path that would clear it.

    Fails loud on write errors other than the directory not existing yet
    (created here) - a halt that silently failed to persist is exactly the
    defect this module exists to close.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(state_dir, _PRIVATE_DIR_MODE)
    basis = snapshot.basis.removeprefix(PERSISTED_UNKNOWN_BASIS + ":")
    record = PersistedHalt(
        platform=platform,
        detected_at=time.time(),
        reason=reason,
        last_human_input_ago_ms=snapshot.last_human_input_ago_ms,
        margin_ms=snapshot.margin_ms,
        guard_ms=snapshot.guard_ms,
        guard_measured=snapshot.guard_measured,
        basis=(
            basis
            if snapshot.state is PresenceState.UNKNOWN
            and basis in TARGET_CLOCK_FAILURE_BASES
            else None
        ),
    )
    path = _path_for(platform, state_dir)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp-{os.getpid()}")
    tmp_path.write_text(json.dumps(record.to_dict(), indent=2), encoding="utf-8")
    os.chmod(tmp_path, _PRIVATE_FILE_MODE)
    os.replace(tmp_path, path)  # atomic on the same filesystem
    logger.warning(
        "coexistence: durable halt record written for backend %r (%s) - a "
        "safety halt is retained; every future session for this backend "
        "starts already HALTED until a human explicitly clears it (see "
        "scripts/resume_after_halt.py)",
        platform,
        reason,
    )


def load_halt(
    platform: str, *, state_dir: Path = DEFAULT_STATE_DIR
) -> PersistedHalt | None:
    """Return the durable halt record for `platform`, or `None` if there is
    none. A machine nobody has ever been detected on pays nothing here - the
    common case is "no file", one `Path.exists()` check.

    A corrupted record fails SAFE, not silent: rather than guessing "no
    halt" (exactly the wrong-direction assumption behind the incident this
    whole feature exists to prevent), a record that cannot be parsed is
    treated as an unresolved halt with an honest, distinguishable reason -
    the human still has to look and explicitly clear it either way.
    """
    path = _path_for(platform, state_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return PersistedHalt.from_dict(data)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.error(
            "coexistence: durable halt record for backend %r at %s is "
            "unreadable/corrupt (%s) - treating as an unresolved halt rather "
            "than silently assuming none occurred; delete the file or run "
            "scripts/resume_after_halt.py to clear it explicitly",
            platform,
            path,
            exc,
        )
        return PersistedHalt(
            platform=platform,
            detected_at=0.0,
            reason=f"corrupt halt record at {path} - treated as latched for safety: {exc}",
            last_human_input_ago_ms=None,
            margin_ms=None,
            guard_ms=0.0,
            guard_measured=False,
        )


def clear_halt(platform: str, *, state_dir: Path = DEFAULT_STATE_DIR) -> bool:
    """Explicit human resume signal - the ONLY way a durable halt record
    goes away. This function must never be called from `mount()`,
    `_build_coexistence_guard()`, `ComputerTool.execute()`, or any other
    point on the automated tool-call path - only from a human-operated
    entry point (`scripts/resume_after_halt.py`).

    Returns whether a record existed and was removed.
    """
    path = _path_for(platform, state_dir)
    if not path.exists():
        return False
    path.unlink()
    logger.warning(
        "coexistence: durable halt record for backend %r explicitly cleared "
        "- future sessions may drive again",
        platform,
    )
    return True


def list_halted_platforms(*, state_dir: Path = DEFAULT_STATE_DIR) -> list[str]:
    """List platform names with a current durable halt record - used by
    `scripts/resume_after_halt.py --all` and by anything reporting current
    state to a human."""
    if not state_dir.exists():
        return []
    return sorted(p.stem for p in state_dir.glob("*.json"))


def make_durable_halt_poll(
    platform: str, *, state_dir: Path = DEFAULT_STATE_DIR
) -> Callable[[], PresenceSnapshot | None]:
    """Build a cheap, per-event-safe poll for `CoexistenceGuard.before_event()`
    (defect 2 - live safety defect, fixed 2026-08-02).

    Before this existed, the durable halt record was only ever consulted
    once, at mount time (`_build_coexistence_guard` in `__init__.py`). A
    guard mounted BEFORE some other session (e.g. a sub-agent, same backend)
    wrote a halt record could never learn about it - it kept its own
    in-memory `_halted=False` for its entire life, and every write it made
    after that point succeeded, exactly the defect this closes (see the
    module docstring above for the real evaluation evidence).

    The fix is to poll on every `before_event()` call, not just at
    construction - but `load_halt()` does a full file read + JSON parse,
    which is not something to pay on every elementary injected event
    (`docs/designs/coexistence.md` \u00a75's per-sample cost budget: this
    mechanism already does an in-process idle read on that same hot path,
    and is sized in microseconds, not milliseconds). This closure keeps the
    common case - no halt has ever been recorded for this platform - down
    to exactly one `Path.stat()` syscall: `FileNotFoundError` short-circuits
    before any read or parse happens. The heavier `load_halt()` path runs at
    most ONCE per guard's lifetime: the moment it does run, the caller is
    expected to call `CoexistenceGuard.seed_halted()` (additive-only, never
    a clear), which flips `_halted` to `True` permanently - `before_event()`
    stops calling this poll at all once halted (see its own docstring), so
    there is nothing left to poll for the rest of that guard's life either
    way.

    Returns `None` when there is nothing to report (no record, or the
    caller should keep going) - never raises for the common "no file" case,
    since that is not a failure, it is the ordinary state of a machine
    nobody has been detected on.
    """

    def _poll() -> PresenceSnapshot | None:
        path = _path_for(platform, state_dir)
        try:
            path.stat()
        except FileNotFoundError:
            return None
        persisted = load_halt(platform, state_dir=state_dir)
        return persisted.to_snapshot() if persisted is not None else None

    return _poll
