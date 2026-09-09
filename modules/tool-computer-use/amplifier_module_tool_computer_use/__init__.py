"""Amplifier tool module: `computer` - Anthropic native computer-use, on whichever
desktop this machine can actually reach.

The tool mounts under the name `computer` so the orchestrator can execute the
`tool_use` blocks Claude emits for its built-in computer tool. `hook-computer-use`
promotes this tool's declaration to the *native* Anthropic tool type on the wire and
turns screenshot markers into real image content blocks.

Without the hook the tool still works as an ordinary function tool (Claude drives it
from the JSON schema below), it just cannot show Claude the screen.

Platform backend
-----------------
This module no longer assumes Windows. `mount()` probes every configured backend
(`registry.select_backend`) *before* registering any tool - D1: if nothing can serve
this machine, `computer`/`desktop` are not mounted at all, and the reason is logged
plainly. See `backend.py` for the protocol and why it is shaped the way it is.

Display geometry is resolved once, right after a backend is selected, and cached for
the life of the tool (D2): `native_tool_spec` used to call a bridge property that
shelled out to PowerShell with a 30s timeout on *every* provider request. It now
reads a plain in-memory value and can never block.
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import hashlib
import inspect
import json
import logging
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from amplifier_core.models import ToolResult

from .announce_macos import DEFAULT_TIMEOUT_SECONDS as MACOS_ANNOUNCE_TIMEOUT_SECONDS
from .announce_macos import AnnounceError, AnnounceResult
from .announce_macos import announce as macos_announce
from .backend import Backend, BackendError, MonitorInfo
from .coexistence_guard import CoexistenceGuard, HaltedError
from .exclusion import Rect
from .geometry import Display, ImageSpace, compute_display
from .halt_state import (
    load_halt,
    make_durable_halt_poll,
    record_halt,
    resolve_resume_command,
)
from .imaging import capture_scaled_b64
from .ledger import HeldInputLedger
from .linux_x11 import LinuxX11Backend
from .macos import MacOSBackend
from .monitors import PRIMARY, VIRTUAL_DESKTOP, attribute_monitor, select_monitor
from .overlay_linux import LinuxOverlay
from .overlay_windows import WindowsOverlay
from .presence import (
    GUARD_MS,
    Confidence,
    IdleUnreadableError,
    PresenceMonitor,
    PresenceSnapshot,
    PresenceState,
)
from .providers import ANTHROPIC, dialect_for_tool_type, read_call
from .registry import _TARGET_MODEL, NoBackendAvailable, select_backend
from .tool_versions import (
    beta_header_for,
    require_static_pairing,
    resolve_tool_version,
)
from .type_pacing import resolve_type_pacing_ms
from .windows import WindowsBackend

logger = logging.getLogger(__name__)

__version__ = "0.2.0"

#: Marker key the companion hook looks for in tool output.
MARKER = "__amplifier_computer_use__"

#: Reporting-only threshold for a successful remote presence read. It is
#: deliberately separate from `presence.GUARD_MS`: no timeout, action gate,
#: classification, or halt decision reads this value.
REMOTE_TRANSPORT_WARNING_MS = 2000.0

SHOT_DIR = Path.home() / ".amplifier" / "computer-use" / "shots"
SHOT_TTL_SECONDS = 2 * 60 * 60

#: Security hardening (adversarial review, no prior gating beyond the parent
#: directory's inherited umask): screenshots of a driven desktop are
#: sensitive content on a shared/multi-user controller box - a world- or
#: group-readable shot directory lets any other local account read them for
#: the full TTL window. `0700`/`0600` restrict both the per-session directory
#: and every file in it to this process's own user, regardless of umask
#: (umask only affects the mode `mkdir`/`open` request initially - it is not
#: itself a floor, so an explicit `os.chmod` after creation is what actually
#: guarantees this rather than merely hoping the umask happens to be strict).
_PRIVATE_DIR_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600


def _text_digest(text: str) -> str:
    """A short, stable, non-reversible fingerprint for an audit log line -
    never the plaintext itself. Matches the discipline
    `docs/designs/remote-transport.md` \u00a710 specifies for `type_text`
    (`args_digest`, not `args`) - this bundle previously did not actually
    implement that logging for ANY action; this is the shared primitive
    behind extending it to every write op that carries free-form text
    (`type`, `set_clipboard`)."""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _bytes_digest(data: bytes) -> str:
    """Same fingerprint primitive as `_text_digest`, for binary payloads
    (screenshot/zoom PNG bytes) - \u00a73 of the task: \"add at least a content
    hash entry for captures.\""""
    return hashlib.sha256(data).hexdigest()[:16]


ACTIONS = [
    "screenshot",
    "zoom",
    "cursor_position",
    "mouse_move",
    "left_click",
    "right_click",
    "middle_click",
    "double_click",
    "triple_click",
    "left_mouse_down",
    "left_mouse_up",
    "left_click_drag",
    "scroll",
    "key",
    "hold_key",
    "type",
    "wait",
    "screen_info",
    "list_windows",
    "focus_window",
]

#: Actions that change the user's machine. Used for the confirm/read-only gate.
MUTATING = {
    "mouse_move",
    "left_click",
    "right_click",
    "middle_click",
    "double_click",
    "triple_click",
    "left_mouse_down",
    "left_mouse_up",
    "left_click_drag",
    "scroll",
    "key",
    "hold_key",
    "type",
    "focus_window",
}

_CLICK_ACTIONS = {
    "left_click": ("left", 1),
    "right_click": ("right", 1),
    "middle_click": ("middle", 1),
    "double_click": ("left", 2),
    "triple_click": ("left", 3),
}


def _prune_shots() -> None:
    """Delete expired screenshot files across every per-session subdirectory.

    Screenshots now live under `SHOT_DIR/<session_id>/*.png` (one subdirectory
    per `ComputerTool` instance - see `ComputerTool.__init__`'s `_session_id`
    and `execute()`) rather than one flat shared directory, so this glob is
    `*/*.png`, not `*.png`. Also best-effort removes session directories that
    are now empty, so a long-lived controller does not accumulate one empty
    directory per past session forever.
    """
    cutoff = time.time() - SHOT_TTL_SECONDS
    try:
        for old in SHOT_DIR.glob("*/*.png"):
            if old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
        for session_dir in SHOT_DIR.glob("*"):
            if session_dir.is_dir():
                try:
                    session_dir.rmdir()  # no-op (raises, caught) if not empty
                except OSError:
                    pass
    except OSError:  # pragma: no cover - best-effort housekeeping
        pass


class _RetargetRefused(RuntimeError):
    """`desktop(action="retarget")` refused before touching anything -
    busy, or the new target fails one of §6.5's fail-loud checks. Distinct
    from `AnnouncementRefused` (a human declined disclosure) so a caller
    can tell "nobody answered" from "this retarget cannot even be
    attempted right now" apart."""


@dataclass(frozen=True)
class _Binding:
    """Everything about the machine a `ComputerTool` instance drives that
    must change together, atomically, when the session re-targets (M4,
    docs/designs/capability-awareness.md \\u00a76). This is the fix a six-lens
    council named for a verified race: `retarget()` used to be described as
    seven sequential field writes on `self` ("`self._backend = new`;
    `self._announced = False`; ..."), asserted to be atomic because they
    happen one after another in the same function. They are not - CPython's
    GIL preempts between individual bytecode instructions (not just at I/O),
    so a concurrent, UNLOCKED reader (`_ensure_announced`'s own fast path -
    see that method's docstring for why it is deliberately unlocked) could
    observe a MIX: the NEW backend already swapped in, paired with the OLD
    `_announced=True` not yet reset. That mix is exactly the bug: a model
    issuing `retarget(mac-B)` and `screenshot()` in the same turn could read
    "already announced" (true of mac-A) and capture mac-B's screen with no
    disclosure ever shown for it - a direct \\u00a77.1 violation.

    The fix: collapse every field that must change together into ONE
    object, and make retarget replace the WHOLE object with a single
    reference assignment (`ComputerTool._binding = new`), never a field at
    a time. `self._binding` is a single attribute; a `LOAD_ATTR`/`STORE_ATTR`
    on it is one bytecode op the GIL cannot preempt mid-instruction. Every
    reader - however many times, from however many threads, at whatever
    granularity it reads `self._binding` - therefore only ever sees the OLD
    binding whole or the NEW binding whole, never a mix of the two. That
    guarantee only holds because `retarget()` (see that method) builds the
    ENTIRE new binding - connect, guard, disclosure, policy, display - off
    to the side, fully formed and ALREADY DISCLOSED, before this object is
    ever installed. There is no "swapped but not yet disclosed" state a
    reader could ever observe through `self._binding`, because disclosure
    happens during construction, not after installation.

    Deliberately excludes `ComputerTool._mouse_pending`: \\u00a76.3 requires it
    to already be empty before a retarget is attempted (refused otherwise,
    see `retarget()` step 0), so it needs no atomic handling of its own -
    there is nothing in it to tear.

    Never mutated in place after construction - every field change, even a
    single-field one (e.g. `mount()` installing the coexistence guard right
    after construction, or `resolve_display()` updating `display`), goes
    through `dataclasses.replace()` and a fresh `self._binding = ...`
    assignment. `ComputerTool`'s `_backend`/`_is_remote`/... properties are
    sugar over exactly that pattern, kept so every existing reader
    (`self._backend`) and every existing single-field test/mount() writer
    (`tool._coexistence_guard = ...`) keeps working unchanged.
    """

    backend: Backend
    is_remote: bool
    read_only: bool
    gate_writes: bool
    clipboard_read_policy: str
    coexistence_guard: CoexistenceGuard | None
    channel_key: str | None
    ledger: HeldInputLedger | None
    band_state: _ChannelBandState | None
    display: Display | None
    current_monitor: MonitorInfo | None
    announced: bool
    announcement: Any | None
    announce_refused: AnnouncementRefused | None
    # Remote-only (\u00a78.1/\u00a79.1) - see `ComputerTool._sync_remote_announcement_state`.
    # Reset to `False` on every retarget: a stale `True` carried over from the
    # OLD target would mask a real Pause/Cancel click on the NEW target's own
    # overlay (each flag is edge-triggered, "at most once per session" - a
    # retarget starts a new physical channel, so it starts a new edge).
    remote_pause_seen: bool
    remote_cancel_seen: bool


class ComputerTool:
    """Executes Anthropic computer-tool actions against whatever desktop the
    selected `Backend` can reach."""

    def __init__(self, backend: Backend, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        # `backend` itself is folded into `self._binding` at the end of this
        # constructor (M4, docs/designs/capability-awareness.md \u00a76) - see
        # `_Binding`'s own docstring for why the backend and every field
        # below that is scoped to IT (not held in separate `self._X`
        # attributes here). Local variables (`is_remote`, `read_only`, ...)
        # carry these values through the rest of `__init__` unchanged from
        # before this fix; only the FINAL storage is different.
        # Kept for `_ensure_announced` (see that method): the announcement is
        # no longer built at mount() time, so the config it needs must be
        # available later, at first real use.
        self._cfg: dict[str, Any] = cfg
        self._max_edge = int(cfg.get("max_edge", 1280))
        self._max_pixels = int(cfg.get("max_pixels", 1_150_000))
        self._enable_zoom = bool(cfg.get("enable_zoom", True))

        # -- model <-> tool_version coupling fix -----------------------------
        # `_configured_tool_version`/`_model_hint` are the raw config values;
        # `_tool_version` is the live, resolved value `native_tool_spec` reads.
        # Resolved once here (may raise ToolVersionError - see tool_versions.py
        # module docstring for why mount time is allowed to fail loud) and then
        # kept current by `note_model()`, called by hook-computer-use on every
        # `provider:request` with the model actually about to be used.
        self._configured_tool_version: str | None = cfg.get("tool_version")
        self._model_hint: str | None = cfg.get("model")
        self._tool_version: str = require_static_pairing(
            self._model_hint, self._configured_tool_version
        )
        self._cross_dialect_override_notices: set[tuple[str, str]] = set()

        # -- remote-target safety posture ------------------------------------
        # `is_remote` is a plain attribute (not an isinstance/import check) so
        # any Backend can opt in without this module depending on
        # RemoteBackend's concrete type - only RemoteBackend sets it True
        # today (see remote_backend.py).
        is_remote = bool(getattr(backend, "is_remote", False))
        read_only_cfg = cfg.get("read_only")
        # Unconfigured default: ON for remote (a machine you are, by
        # definition, not looking at - see docs/designs/remote-transport.md
        # \u00a714), unchanged (OFF) for local - preserves every existing
        # local-mode caller's behavior exactly.
        read_only = is_remote if read_only_cfg is None else bool(read_only_cfg)
        gate_cfg = cfg.get("gate_writes")
        if gate_cfg is None:
            # "Destructive" is undecidable from a click (Delete looks like any
            # other click) - the only two honest options are gate-every-write
            # or gate-none (\u00a710.4). Default is gate-every-write, but only
            # matters when read_only is off (read_only already blocks every
            # write outright) and only for remote targets - local behavior is
            # unaffected. Flipping read_only off on a remote target therefore
            # can never silently produce "full write access + no gate": the
            # gate turns on in the same step, unless explicitly disabled below.
            gate_writes = is_remote and not read_only
        else:
            gate_writes = bool(gate_cfg)
            if is_remote and not gate_writes and not read_only:
                logger.warning(
                    "computer-use: gate_writes explicitly disabled for a remote, "
                    "non-read_only target - every write action will execute with "
                    "no confirmation gate. This is a deliberate, logged opt-out, "
                    "not a default."
                )
        # `unattended_writes_ok` and `gate_writes` are two answers to the SAME
        # policy question (\u00a710.4: "gate every write, or gate none") - not two
        # independent mechanisms. The config knob itself lives in
        # hook-computer-use (see that module's `mount()` docstring for why:
        # it is the module that answers `ask_user`/EOF-avoidance too, so it
        # owns the one place an operator sets it). `hook-computer-use`'s gate
        # handler syncs the live value onto THIS attribute on every
        # `tool:pre` call for `computer`/`desktop`, strictly before this
        # tool's own `execute()` runs for that same call - so by the time
        # `DesktopTool.execute()`'s fail-safe check reads it below, it
        # reflects the exact same decision the hook already made, instead of
        # silently re-deciding and contradicting it. Defaults False: with no
        # gate hook mounted (or one that has not run yet), this stays False
        # and the fail-safe denies - never a silent, un-authored escape
        # hatch.
        self._unattended_writes_ok: bool = False
        # A SEPARATE per-call signal from `_unattended_writes_ok` above -
        # deliberately, not the same flag wearing a second meaning. That one
        # answers "is nobody at the keyboard, and is that explicitly OK?" -
        # always False when a human interactively approves via `ask_user`,
        # since the whole point of that path is a human WAS asked. Reusing
        # it for the interactive case would make its name lie about which
        # of the two questions it is answering (see this module's own
        # incident history: names and return values that did not match
        # reality). This is the interactive counterpart: "did a human just
        # grant THIS specific call via `ask_user`?" `hook-computer-use`'s
        # gate handler resets it to False at the top of every `tool:pre`
        # call (same unconditional-sync point as `_unattended_writes_ok`,
        # so a stale True from an earlier approved call can never survive
        # into a later one), then sets it True only when it is about to
        # hand this exact call's decision to `ask_user`. That is safe to do
        # before the human actually answers: `ask_user` is a blocking gate
        # (`HOOKS_API.md` - priority 2, same tier as `deny`) - if the human
        # declines or times out, this tool's `execute()` is never called
        # for that call at all, so no observer ever reads a `True` paired
        # with a decline. Defaults False: with no gate hook mounted (or one
        # that has not run yet), this stays False and the fail-safe denies
        # - same "never a silent, un-authored escape hatch" rule as above.
        self._interactive_write_approved: bool = False
        # Per-monitor targeting (see monitors.py): default is "primary", i.e. one
        # real monitor, not the virtual-desktop bounding box around all of them.
        # Set to monitors.VIRTUAL_DESKTOP to opt into the old whole-desktop
        # behavior, or to a specific monitor id from list_monitors().
        self._target_monitor: str = str(cfg.get("target_monitor") or PRIMARY)
        # Whether `_target_monitor` was actually asked for (config, or a runtime
        # `select_monitor()` call) vs. just being the unconfigured default. See
        # `_resolve_display_for_target` - this is what decides whether monitor
        # enumeration failing is allowed to fall back to virtual-desktop mode
        # (default, silent-to-the-user machines) or must fail loud (an explicit
        # request the caller is entitled to know failed).
        self._target_monitor_explicit: bool = bool(cfg.get("target_monitor"))
        self._monitors: list[MonitorInfo] = []
        # `current_monitor`/`display` (below, on `_Binding`): `current_monitor`
        # is `None` in virtual-desktop mode, otherwise the MonitorInfo
        # `display` is currently scoped to (see `_resolve_display_for_target`).
        # `display` is resolved once (by mount(), right after backend
        # selection) and cached - never touched on the request hot path (see
        # `resolve_display`). Both start `None` in the `_Binding` constructed
        # at the end of this method.

        # -- security hardening: per-session screenshot scoping ---------------
        # A fresh id per `ComputerTool` instance (i.e. per mount, in practice
        # per session) - `execute()` writes screenshots under
        # `SHOT_DIR/self._session_id/`, not the flat shared directory every
        # session previously wrote into together. See `execute()` and
        # `_prune_shots()`.
        self._session_id: str = uuid.uuid4().hex

        # -- security hardening: clipboard read policy ------------------------
        # `get_clipboard` (docs/DesktopTool) previously had no gate
        # beyond `read_only` - a full clipboard read flows verbatim into
        # `ToolResult.output`, then the model provider's API, then a durable
        # transcript, and a clipboard can carry things a screenshot never
        # shows (a just-copied password, an unseen paste buffer). This is a
        # DISTINCT, explicit policy from `read_only`/`gate_writes` (a read,
        # not a write) - see `DesktopTool.execute()`'s `get_clipboard` branch
        # for where it is enforced and audit-logged.
        #   - "allow": clipboard content returned verbatim (the only
        #     behavior that existed before this hardening pass).
        #   - "redact": length + a short content digest only, never the text
        #     itself - the same digest-not-plaintext discipline this pass
        #     also applies to `type_text`/`set_clipboard` audit logging.
        #   - "block": the action fails, same shape as `read_only` blocking
        #     a write.
        # Default mirrors this module's existing `read_only`/`gate_writes`
        # precedent exactly (safer for remote - a machine you are, by
        # definition, not looking at; unchanged for local, preserving every
        # existing local caller's behavior): "allow" locally, "redact" for a
        # remote target - UNLESS the operator already blocked clipboard
        # reads entirely via `read_only` (`_READ_ONLY_BLOCKED` in
        # `DesktopTool`), in which case this policy is moot.
        clipboard_policy_cfg = cfg.get("clipboard_read_policy")
        if clipboard_policy_cfg is None:
            clipboard_read_policy = "redact" if is_remote else "allow"
        else:
            clipboard_read_policy = str(clipboard_policy_cfg)
            if clipboard_read_policy not in {"allow", "redact", "block"}:
                raise ValueError(
                    "config 'clipboard_read_policy' must be one of "
                    f"'allow'/'redact'/'block', got {clipboard_policy_cfg!r}"
                )

        # -- human/agent coexistence (docs/designs/coexistence.md) -----------
        # `coexistence_guard`/`channel_key`/`ledger`/`band_state` below are
        # ALL `None` here and set together by `mount()` right after
        # construction (`_build_coexistence_guard` needs the concrete
        # backend instance's identity, which is settled but not yet worth
        # duplicating logic for here) - `None`/`None`/`None`/`None` whenever
        # no guard was built for this backend (no coexistence layer at all:
        # no held-input tracking, no band-lifetime tracking, no disclosure
        # channel - see `presence.GUARD_MEASURED`, \u00a75.5's "never claim a
        # guarantee you don't have"). `channel_key` identifies the PHYSICAL
        # channel (`_channel_identity`); `ledger`/`band_state` are the
        # shared, channel-scoped objects `_get_channel_ledger`/
        # `_get_channel_band_state` hand out - the same objects every other
        # `ComputerTool` mount() driving this same channel also holds, which
        # is what closes F8 (band-lifetime.md \\u00a710).
        #
        # Per-instance pending-coordinate box for a held mouse button,
        # mirroring `RemoteAgent._mouse_pending` exactly (`remote_agent.py`) -
        # `left_mouse_up` updates the box with its real coordinates and
        # releases THROUGH the ledger so `backend.mouse_up` fires exactly
        # once, whether triggered by the explicit up or by the ledger's own
        # deadman/release_all. NOT part of `_Binding` (see that class's
        # docstring): \u00a76.3 requires it to already be empty before a
        # retarget is attempted.
        self._mouse_pending: dict[str, dict[str, int | None]] = {}
        # -- session-start disclosure: gated at FIRST REAL USE, not mount() --
        # See `_ensure_announced` for the full rationale (docs/designs/
        # coexistence.md \u00a77, and the double-mount defect this closes: a
        # protocol-compliance probe calls mount() on a throwaway
        # MockCoordinator before every real session - see amplifier_core's
        # validation.tool.ToolValidator._check_protocol_compliance). This
        # lock is per-INSTANCE (not the module-level `_announcement_lock`,
        # which guards the cross-instance/cross-process channel cache) - it
        # serializes this instance's own check-then-act sequence against two
        # actions racing to be "first", AND (M4) against a concurrent
        # `retarget()` - see that method's docstring for why sharing this
        # one lock with `_ensure_announced` is what makes the verified race
        # structurally impossible rather than merely unlikely.
        self._announce_lock: threading.Lock = threading.Lock()
        # `announced`/`announcement`/`announce_refused` below: `announced`
        # sticky-False until this session's first real action successfully
        # builds (or is refused) a disclosure; `announce_refused` sticky for
        # the life of a BINDING once set (see `_ensure_announced`) - once
        # set, every later call re-raises this SAME error immediately,
        # without touching the backend again, which is what makes a
        # refusal mean "stop driving" structurally: `_ensure_announced` is
        # the one gate both tools' `execute()` call before doing anything
        # else, so there is no second door a refused session can get
        # through. `announcement` is the disclosure channel's own handle -
        # an overlay object to keep alive for the tool's lifetime, or
        # `None` for a one-shot channel (macOS's dialog) or a backend with
        # no channel at all.
        #
        # `remote_pause_seen`/`remote_cancel_seen`: remote-only
        # (docs/designs/coexistence.md \u00a78.1/\u00a79.1) - the target's own
        # persistent overlay is invisible to this controller until asked
        # (see `_sync_remote_announcement_state`) - nothing observes the
        # target's real input events directly. Edge-triggered, not level:
        # each flips true at most once per session, so a human clicking
        # Pause/Cancel is applied to this session's guard exactly once, not
        # once per guarded write for the remainder of the session.
        #
        # All of the above (backend, is_remote/read_only/gate_writes/
        # clipboard_read_policy, coexistence_guard/channel_key/ledger/
        # band_state, display/current_monitor, announced/announcement/
        # announce_refused, remote_pause_seen/remote_cancel_seen) live
        # together on ONE `_Binding` - see that class's docstring for why:
        # this is the fix for the verified re-target race (M4,
        # docs/designs/capability-awareness.md \u00a76). `mount()`/
        # `resolve_display()`/`select_monitor()`/`_ensure_announced()`
        # update individual fields via the properties below (sugar over
        # `dataclasses.replace`); `retarget()` replaces the WHOLE object in
        # one assignment.
        self._binding = _Binding(
            backend=backend,
            is_remote=is_remote,
            read_only=read_only,
            gate_writes=gate_writes,
            clipboard_read_policy=clipboard_read_policy,
            coexistence_guard=None,
            channel_key=None,
            ledger=None,
            band_state=None,
            display=None,
            current_monitor=None,
            announced=False,
            announcement=None,
            announce_refused=None,
            remote_pause_seen=False,
            remote_cancel_seen=False,
        )
        # Defect 1 (halt surfacing): every `HaltedError` this session hits is
        # recorded here (`execute()`), and `hook-computer-use` reads this
        # list on every `tool:post` to inject a standing reminder into the
        # model's own context - a halted session must not be able to close
        # out a turn without the fact in front of it. Never cleared during a
        # session; a session in which a halt fired stays flagged for the
        # rest of that session, on purpose (see hook module docstring).
        self.halt_notices: list[dict[str, Any]] = []

        # -- type_text pacing (measured safety gap, see type_pacing.py) ------
        # `None` (default) = auto: `type_pacing.AUTO_PACING_MS` when a
        # coexistence guard is active for this `type` call, `0` (full speed,
        # unchanged) when it is not - see `resolve_type_pacing_ms`. An
        # explicit integer overrides auto in both directions, including `0`
        # to force full speed even with a guard active (logged once at
        # WARNING when it fires - see `_run`'s `type` action).
        pacing_cfg = cfg.get("type_pacing_ms")
        if pacing_cfg is None:
            self._type_pacing_ms: int | None = None
        else:
            try:
                parsed_pacing = int(pacing_cfg)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "config 'type_pacing_ms' must be an integer number of "
                    f"milliseconds, or omitted for auto - got {pacing_cfg!r}"
                ) from exc
            if parsed_pacing < 0:
                raise ValueError(
                    f"config 'type_pacing_ms' must be >= 0 - got {parsed_pacing!r}"
                )
            self._type_pacing_ms = parsed_pacing

    # -- M4 binding properties (docs/designs/capability-awareness.md \u00a76) ------
    # Every field below lives on `self._binding` (see `_Binding`'s docstring).
    # Each property is sugar for a SINGLE-FIELD `dataclasses.replace()` -
    # kept so every existing reader (`self._backend`) and every existing
    # single-field writer (`mount()`'s `computer._coexistence_guard = ...`,
    # `_ensure_announced`'s `self._announced = True`, and test fixtures
    # across this bundle's test suite) keeps working completely unchanged.
    # `retarget()` itself never goes through these setters for its own
    # atomic swap - it builds one whole `_Binding` off to the side and
    # assigns `self._binding = new` exactly once; that single assignment,
    # not these properties, is what makes the swap atomic.
    @property
    def _backend(self) -> Backend:
        return self._binding.backend

    @_backend.setter
    def _backend(self, value: Backend) -> None:
        self._binding = replace(self._binding, backend=value)

    @property
    def _is_remote(self) -> bool:
        return self._binding.is_remote

    @_is_remote.setter
    def _is_remote(self, value: bool) -> None:
        self._binding = replace(self._binding, is_remote=value)

    @property
    def _read_only(self) -> bool:
        return self._binding.read_only

    @_read_only.setter
    def _read_only(self, value: bool) -> None:
        self._binding = replace(self._binding, read_only=value)

    @property
    def _gate_writes(self) -> bool:
        return self._binding.gate_writes

    @_gate_writes.setter
    def _gate_writes(self, value: bool) -> None:
        self._binding = replace(self._binding, gate_writes=value)

    @property
    def _clipboard_read_policy(self) -> str:
        return self._binding.clipboard_read_policy

    @_clipboard_read_policy.setter
    def _clipboard_read_policy(self, value: str) -> None:
        self._binding = replace(self._binding, clipboard_read_policy=value)

    @property
    def _coexistence_guard(self) -> CoexistenceGuard | None:
        return self._binding.coexistence_guard

    @_coexistence_guard.setter
    def _coexistence_guard(self, value: CoexistenceGuard | None) -> None:
        self._binding = replace(self._binding, coexistence_guard=value)

    @property
    def _channel_key(self) -> str | None:
        return self._binding.channel_key

    @_channel_key.setter
    def _channel_key(self, value: str | None) -> None:
        self._binding = replace(self._binding, channel_key=value)

    @property
    def _ledger(self) -> HeldInputLedger | None:
        return self._binding.ledger

    @_ledger.setter
    def _ledger(self, value: HeldInputLedger | None) -> None:
        self._binding = replace(self._binding, ledger=value)

    @property
    def _band_state(self) -> _ChannelBandState | None:
        return self._binding.band_state

    @_band_state.setter
    def _band_state(self, value: _ChannelBandState | None) -> None:
        self._binding = replace(self._binding, band_state=value)

    @property
    def _display(self) -> Display | None:
        return self._binding.display

    @_display.setter
    def _display(self, value: Display | None) -> None:
        self._binding = replace(self._binding, display=value)

    @property
    def _current_monitor(self) -> MonitorInfo | None:
        return self._binding.current_monitor

    @_current_monitor.setter
    def _current_monitor(self, value: MonitorInfo | None) -> None:
        self._binding = replace(self._binding, current_monitor=value)

    @property
    def _announced(self) -> bool:
        return self._binding.announced

    @_announced.setter
    def _announced(self, value: bool) -> None:
        self._binding = replace(self._binding, announced=value)

    @property
    def _announcement(self) -> Any | None:
        return self._binding.announcement

    @_announcement.setter
    def _announcement(self, value: Any | None) -> None:
        self._binding = replace(self._binding, announcement=value)

    @property
    def _announce_refused(self) -> AnnouncementRefused | None:
        return self._binding.announce_refused

    @_announce_refused.setter
    def _announce_refused(self, value: AnnouncementRefused | None) -> None:
        self._binding = replace(self._binding, announce_refused=value)

    @property
    def _remote_pause_seen(self) -> bool:
        return self._binding.remote_pause_seen

    @_remote_pause_seen.setter
    def _remote_pause_seen(self, value: bool) -> None:
        self._binding = replace(self._binding, remote_pause_seen=value)

    @property
    def _remote_cancel_seen(self) -> bool:
        return self._binding.remote_cancel_seen

    @_remote_cancel_seen.setter
    def _remote_cancel_seen(self, value: bool) -> None:
        self._binding = replace(self._binding, remote_cancel_seen=value)

    # -- display resolution (D2) -------------------------------------------------
    def resolve_display(self, refresh: bool = False) -> Display:
        """Resolve and cache display geometry for the current target monitor.

        Called once by `mount()`, right after the backend is selected. The other
        callers are the `screen_info` action, which passes `refresh=True` as its
        explicit, deliberate refresh path (e.g. after a resolution change), and
        `select_monitor()`, which re-resolves for a *different* target. Those are
        the only places this ever talks to the backend again after mount.
        """
        if self._display is not None and not refresh:
            return self._display
        disp, monitor = self._resolve_display_for_target(
            self._target_monitor, allow_fallback=not self._target_monitor_explicit
        )
        # One `replace()`, not two property assignments - `display` and
        # `current_monitor` describe the SAME resolution and always change
        # together (matches `_Binding`'s own field grouping).
        self._binding = replace(self._binding, display=disp, current_monitor=monitor)
        return disp

    def _resolve_display_for_target(
        self,
        target: str,
        allow_fallback: bool = False,
        *,
        backend: Backend | None = None,
    ) -> tuple[Display, MonitorInfo | None]:
        """Build a `Display` scoped to `target` against `backend` (defaults to
        `self._backend`) and return `(Display, MonitorInfo | None)` - the
        caller decides how/whether to install the result.

        Single shared implementation for both `resolve_display()` (mount time and
        the `screen_info` refresh path) and `select_monitor()` (runtime target
        switch) - one place computes monitor-scoped geometry, so there is no
        separate "switch monitor" code path that could drift out of sync with how
        mount-time resolution works.

        Takes an explicit `backend` (M4, docs/designs/capability-awareness.md
        \u00a76.2 step 6) rather than always reading `self._backend`, and returns
        the resolved monitor instead of writing `self._current_monitor`
        directly, for the same reason: `retarget()`'s build phase must be able
        to resolve display geometry for a NEW backend this instance has not
        adopted yet, without touching `self` at all until the swap. The
        `self._monitors` refresh-cache side effect below is therefore scoped
        to calls against THIS instance's own current backend only - resolving
        for a not-yet-adopted backend must never clobber the cache the
        CURRENT backend's `list_windows`/`focus_window` attribution still
        relies on mid-build.

        `allow_fallback` governs exactly one failure mode: monitor enumeration
        being genuinely unavailable (`Backend.list_monitors()` raising
        `BackendError` - no RandR, no RandR Monitor objects, etc.). Verified for
        real on a headless/virtual single-display X11 session during
        development (RandR present, `GetMonitors` returns zero - a real,
        legitimate configuration, not a hypothetical): that machine has exactly
        one display, so falling back to `screen_geometry()`'s virtual-desktop
        bounding box - the code path that has ALWAYS been correct for a single
        display - reports the SAME rectangle enumeration would have, had it
        worked. That is not "pretending there is one monitor" (no `MonitorInfo`
        is invented); it is correctly falling back to the one mode that was
        already right for this machine.

        Third-instance-of-a-defect-class fix: whether that fallback is logged
        at WARNING or DEBUG now depends on `exc.expected`
        (`backend.MonitorEnumerationUnavailable` - `getattr(exc, "expected",
        False)` for a plain, un-decorated `BackendError`, which stays exactly
        as loud as before this fix). `expected=True` means the backend itself
        proved no monitor is attached (see `linux_x11.LinuxX11Backend.
        _connected_output_count`) - a platform STATE, not a defeated ask,
        the same "no intent was defeated" distinction `_mount_unavailable`'s
        silent/loud split already draws for a different mount-time condition.
        `expected=False` (an actively detected anomaly, OR a backend that
        cannot make the determination at all) stays exactly as loud as this
        warning has always been - only the PROVEN-benign case moved off the
        human's console; the fact is never lost, `desktop(action="doctor")`'s
        `target_mode.monitor_count` still reports it on demand. Also deduped
        to at most once per physical channel per process, reusing
        `_channel_registry_lock`/`_channel_identity` - the exact mechanism
        `_mark_remote_latency_warned` already uses to solve "more than one
        mount() in this process, one physical channel" for a different fact,
        applied here rather than inventing a second one (this is also the fix
        for why the message printed twice: `mount()` runs this path once for
        `amplifier_core`'s protocol-compliance probe and once for the real
        mount - see `test_double_mount_defect.py`).

        This is why `allow_fallback` is only ever `True` when `target` is the
        unconfigured default (`resolve_display()` with no explicit
        `target_monitor` config) - an explicit ask (`target_monitor` config, or
        a runtime `select_monitor()` call) always fails loud instead (raises,
        below - never reaches this logging at all), and a target id that IS
        enumerated but does not match a request always fails loud regardless
        of `allow_fallback` (see `select_monitor()` call below): a config typo
        must never silently degrade to a different region of the real,
        multi-monitor desktop it was supposed to protect against.
        """
        target_backend = self._backend if backend is None else backend
        if target == VIRTUAL_DESKTOP:
            geo = target_backend.screen_geometry()
            current_monitor: MonitorInfo | None = None
            origin_x, origin_y = geo.origin_x, geo.origin_y
            width, height = geo.width, geo.height
        else:
            try:
                monitors = target_backend.list_monitors()
            except BackendError as exc:
                if not allow_fallback:
                    raise
                if _mark_monitor_enum_warned(_channel_identity(target_backend)):
                    if bool(getattr(exc, "expected", False)):
                        logger.debug(
                            "computer-use: monitor enumeration unavailable "
                            "for target %r (%s); falling back to "
                            "whole-desktop bounding-box mode for this "
                            "session - the backend confirmed no monitor is "
                            "physically attached, so this is expected "
                            "platform state, not a failure.",
                            target,
                            exc,
                        )
                    else:
                        logger.warning(
                            "computer-use: monitor enumeration unavailable "
                            "for target %r (%s); falling back to "
                            "whole-desktop bounding-box mode for this "
                            "session. This was NOT confirmed as the "
                            "expected headless case - on a real desktop "
                            "this is worth investigating rather than "
                            "trusting the fallback.",
                            target,
                            exc,
                        )
                return self._resolve_display_for_target(
                    VIRTUAL_DESKTOP, backend=backend
                )
            if target_backend is self._backend:
                self._monitors = monitors
            # `select_monitor` (the module-level function) always fails loud on
            # an unmatched explicit id - deliberately NOT gated by
            # allow_fallback. Enumeration succeeding but not containing the
            # requested id is a config typo, not an environmental limitation;
            # silently substituting a different monitor would be exactly the
            # "silently targets the wrong region" failure mode this feature
            # exists to eliminate.
            chosen = select_monitor(monitors, None if target == PRIMARY else target)
            if target == PRIMARY and not chosen.primary:
                # Not a synthesized fallback - `chosen` is a real, enumerated
                # monitor. Logged because picking one among several equally
                # real candidates, absent a primary flag, is an environmental
                # fact worth surfacing, not something to bury silently.
                logger.warning(
                    "computer-use: no monitor reported as primary; "
                    "deterministically targeting %r (%dx%d at %d,%d)",
                    chosen.id,
                    chosen.width,
                    chosen.height,
                    chosen.x,
                    chosen.y,
                )
            current_monitor = chosen
            origin_x, origin_y = chosen.x, chosen.y
            width, height = chosen.width, chosen.height

        mw, mh = compute_display(width, height, self._max_edge, self._max_pixels)
        disp = Display(width, height, mw, mh, origin_x, origin_y)
        logger.info(
            "computer-use display: target=%r screen %dx%d at (%d,%d) -> model %dx%d",
            target,
            width,
            height,
            origin_x,
            origin_y,
            mw,
            mh,
        )
        return disp, current_monitor

    def list_monitors(self) -> list[MonitorInfo]:
        """Enumerate monitors via the backend, refreshing the cached list.

        Independent of the current target: works the same whether `display` is
        currently scoped to a monitor or to the whole virtual desktop.
        """
        self._monitors = self._backend.list_monitors()
        return self._monitors

    def _monitors_for_attribution(
        self, binding: _Binding | None = None
    ) -> list[MonitorInfo]:
        """Best-effort monitor list for `monitors.attribute_monitor`, used by
        the `list_windows`/`focus_window` actions below.

        Prefers a fresh `list_monitors()` call (a window can move between
        monitors between calls) but falls back to whatever was last resolved
        (`self._monitors`, set by `mount()`/`select_monitor()`/an earlier
        `list_monitors()` call) if enumeration fails right now - a transient
        enumeration failure degrades attribution to "unknown" for each window
        (`attribute_monitor` already returns `None` on an empty list), it
        does not make `list_windows`/`focus_window` themselves fail.

        `binding`: council re-review Finding 3 - `_run` dispatches every
        action against ONE pinned `_Binding` snapshot (see that method's
        docstring for why); this call used to read `self._backend` fresh
        instead, which can observe a DIFFERENT backend than the one the
        action itself just ran against if a `retarget()` lands mid-call.
        Defaults to a fresh `self._binding` read when not given (direct
        callers with no snapshot of their own to stay pinned to, e.g.
        `list_monitors()`/existing tests).
        """
        binding = binding if binding is not None else self._binding
        try:
            return binding.backend.list_monitors()
        except BackendError:
            return self._monitors

    def _focus_monitor_warning(
        self, handle: str, binding: _Binding | None = None
    ) -> str:
        """After `focus_window`, tell the caller - explicitly, in the result
        text - if the window it just raised is on a DIFFERENT monitor than
        the one `computer` currently captures.

        This closes the exact gap that made a working `focus_window` look
        broken for three sessions in a row: `focus_window` can succeed
        completely (the foreground window genuinely changes) while the next
        screenshot shows no difference, because capture is scoped to one
        monitor at a time (see `monitors.py`) and the window landed on a
        different one. Without this, "focus succeeded but nothing visibly
        changed" and "focus silently did nothing" are indistinguishable from
        the caller's side - which is precisely what happened.

        Deliberately a WARNING, never an automatic `select_monitor` switch:
        changing the capture target is a separate, already-explicit action
        (`desktop.select_monitor`) - silently doing it here would mutate
        session state the caller never asked to change, on the strength of
        one `focus_window` call. That mirrors this same method's cursor-clamp
        warning a few lines up (`_run`'s `cursor_position` branch): inform,
        never silently substitute.

        `binding`: council re-review Finding 3, same rationale as
        `_monitors_for_attribution` above - pin to the SAME snapshot
        `_run` already dispatched `focus_window` against, rather than
        re-reading `self._backend`/`self._current_monitor` fresh right
        after a guarded write. Defaults to a fresh `self._binding` read
        when not given.

        Returns `""` (no note) when: this session is in virtual-desktop mode
        (`binding.current_monitor is None` - capture already shows the whole
        desktop, so there is nothing to warn about); the window landed on the
        SAME monitor `computer` is already scoped to; or fresh window
        enumeration itself fails (cannot verify either way - say nothing
        false rather than fabricate a warning).
        """
        binding = binding if binding is not None else self._binding
        if binding.current_monitor is None:
            return ""
        try:
            result = binding.backend.list_windows()
        except BackendError:
            return ""
        entry = next((w for w in result.windows if w.handle == handle), None)
        if entry is None or entry.rect is None:
            return (
                " [warning: could not verify which monitor this window is "
                "on now - this backend does not report window geometry for "
                "it; take a screenshot to confirm the focus actually landed "
                "where expected]"
            )
        target = binding.current_monitor.id
        landed = attribute_monitor(entry.rect, self._monitors_for_attribution(binding))
        if landed == target:
            return ""
        if landed is None:
            return (
                " [warning: window is not within any enumerated monitor "
                f"(possibly off-screen or minimized) - `computer` is scoped "
                f"to {target!r} and will not show it; take a screenshot to "
                "confirm]"
            )
        return (
            f" [warning: window is now on monitor {landed!r}, but `computer` "
            f"screenshots are scoped to {target!r} - it will not appear "
            f"there; use desktop.select_monitor({landed!r}) to see it]"
        )

    @property
    def current_monitor(self) -> MonitorInfo | None:
        """The monitor `display` is scoped to, or `None` in virtual-desktop mode."""
        return self._current_monitor

    def select_monitor(self, target: str) -> Display:
        """Switch the active target (a monitor id, `"primary"`, or
        `monitors.VIRTUAL_DESKTOP`) and re-resolve `display` for it.

        Safe to call mid-session. `hook-computer-use` reads `native_tool_spec`
        fresh on *every* provider request (see that property's docstring) - it is
        never cached at the hook layer - so the very next request after this call
        returns automatically declares the new `display_width_px`/
        `display_height_px` with no extra plumbing. The only state this needs to
        update is `self._display`/`self._current_monitor`, exactly what
        `_resolve_display_for_target` already does.

        Always fails loud on enumeration failure (`allow_fallback=False`):
        unlike the unconfigured default, this is an explicit ask - the caller
        (config or a live `desktop.select_monitor` call) is entitled to know it
        failed, not have it silently ignored in favor of the previous target.
        """
        disp, monitor = self._resolve_display_for_target(target, allow_fallback=False)
        self._binding = replace(self._binding, display=disp, current_monitor=monitor)
        self._target_monitor = target
        self._target_monitor_explicit = True
        return disp

    @property
    def display(self) -> Display:
        if self._display is None:
            # Should never happen in normal operation - mount() resolves eagerly -
            # but if it does, fail loudly rather than silently blocking the hot path
            # on a subprocess the way the old `native_tool_spec` property did.
            raise BackendError(
                "display geometry not resolved; resolve_display() must be called at mount time"
            )
        return self._display

    @property
    def image_space(self) -> ImageSpace | None:
        """The coordinate space a tool-call payload's numbers are relative to:
        the size of the screenshot the model was actually shown.

        `None`, rather than raising like `display`, when geometry was never
        resolved - and that difference is deliberate. This is read at the very
        top of `execute()`, BEFORE its error handling; raising here would turn
        every unmounted-display tool call into an exception escaping `execute()`
        instead of the clean `ToolResult` error it returns today. A dialect that
        needs the size and is handed `None` raises `ValueError` naming what is
        missing, which `execute()` already converts into an ordinary tool error.
        Loud, in the right place, without moving the failure outside the handler.
        """
        return None if self._display is None else self._display.image_space

    # -- Tool protocol ----------------------------------------------------------
    @property
    def name(self) -> str:
        return "computer"

    @property
    def description(self) -> str:
        return (
            "Control the user's real desktop: capture the screen, move and click the "
            "mouse, drag, scroll, type text, press key combinations, and list or focus windows. "
            "Coordinates are in the pixel space of the screenshots returned by this tool. "
            "Always take a screenshot before acting so you can see where things are. "
            "This machine may be in use by a human at the same time you are driving it: "
            "your keystrokes and theirs can interleave, so a command you believe you typed "
            "verbatim may land with extra or missing characters. If a result looks off "
            "(an unexpected error, a typo, output that doesn't match), verify what actually "
            "landed before assuming your own input was wrong."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ACTIONS,
                    "description": "Operation to perform.",
                },
                "coordinate": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "[x, y] target. For zoom: a 4-element region [x1, y1, x2, y2].",
                },
                "region": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Alias for a zoom region: [x1, y1, x2, y2].",
                },
                "start_coordinate": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "[x, y] drag origin for left_click_drag.",
                },
                "text": {
                    "type": "string",
                    "description": "Text to type, or key combo such as 'ctrl+s'.",
                },
                "scroll_direction": {
                    "type": "string",
                    "enum": ["up", "down", "left", "right"],
                },
                "scroll_amount": {
                    "type": "integer",
                    "description": "Wheel notches to scroll.",
                },
                "duration": {
                    "type": "number",
                    "description": "Seconds, for wait and hold_key.",
                },
                "handle": {
                    "type": "string",
                    "description": "Window handle from list_windows.",
                },
            },
            "required": ["action"],
        }

    # -- native promotion (read by hook-computer-use) ---------------------------
    @property
    def native_tool_type(self) -> str:
        """The native tool type this tool has resolved for THIS turn - the key
        `providers.dialect_for_tool_type` dispatches on.

        Exists because `hook-computer-use` needs exactly this fact and had no
        honest way to get it. It was reading `native_tool_spec["type"]`, i.e.
        recovering a vendor-neutral fact by parsing a VENDOR-SHAPED artifact.
        That works only for vendors that happen to put their type under a key
        named `type`; a vendor that discriminates by some other key has no
        `type` at all, and the hook silently fell back to a default belonging to
        a different vendor and probed the mounted provider for the wrong wire
        convention entirely.

        The fix is not to teach the hook more wire formats - it declares
        `dependencies = []` and cannot import `providers.py`, by design. It is
        to stop making it infer: the tool already knows the answer, so it says
        it. Plain `str`, no import, no coupling - the same duck-typed read the
        hook already does for `native_tool_spec`.

        For both dialects that put their type on the wire this is exactly
        `native_tool_spec["type"]`, so the hook's answer for them is unchanged -
        pinned in `tests/test_provider_dialects.py`.
        """
        return self._tool_version

    @property
    def native_tool_spec(self) -> dict[str, Any]:
        """The native tool declaration for whichever dialect `_tool_version`
        belongs to (`providers.py`) - sized to the cached display when that
        dialect requires a size, bare when it rejects one.

        This used to build ONE shape (Anthropic's: `name` +
        `display_width_px`/`display_height_px`) no matter which provider was in
        play, and gate `enable_zoom` on `self._tool_version >=
        "computer_20251124"` - a string comparison that silently did double
        duty as a provider check, because OpenAI's bare `"computer"` happens to
        sort below it. OpenAI tolerated the surplus fields only because
        `provider-openai` discards everything but `type`; the declaration
        itself was wrong for that wire and nothing here said so. Each dialect
        now owns its own shape (see `providers._declare_anthropic` /
        `_declare_openai`).

        D2 fix: this used to call `self._bridge.display()`, a property that shelled
        out to PowerShell with a 30s timeout on *every single* provider request. That
        subprocess call is why the hook's `hasattr` guard (D3) mattered so much: any
        transient bridge failure here raised on the hot path. Display is now resolved
        once at mount and cached in-memory; this property does no I/O and cannot
        raise for that reason again.

        Per-monitor targeting tension: `desktop.select_monitor` can change what
        `self.display` reports *mid-session*, and this property's declared
        `display_width_px`/`display_height_px` must never drift out of sync with
        what `computer.screenshot` is actually capturing. That is why this stays a
        plain property reading `self.display` rather than something cached at
        construction: the orchestrator's `ToolSpec` construction (see
        `amplifier-module-loop-streaming`'s `_build_tool_spec`, which reads this
        property fresh to build every request's tool list - it is never cached
        there either; only D2's *I/O* was cached here, not the *value*) re-reads
        `native_tool_spec` on every `provider:request`, so the very next request
        after a monitor switch automatically declares the new dimensions. The
        in-memory state this property reads (`self._display`) is exactly what
        `select_monitor` updates - one piece of state, two readers (this property
        and `_run`), always in sync.
        """
        disp = self.display
        return dialect_for_tool_type(self._tool_version).declare(
            self._tool_version,
            width=disp.model_width,
            height=disp.model_height,
            enable_zoom=self._enable_zoom,
        )

    @property
    def native_beta_header(self) -> str | None:
        """`None` when the vendor owning this tool type has no beta-header
        mechanism at all - a distinct answer from any header string, and one
        this used to be unable to give (see `tool_versions.beta_header_for`).
        A caller must send no header for `None`, never an empty one."""
        return beta_header_for(self._tool_version)

    def select_provider_native_tool_type(
        self, native_tool_type: str, model: str | None = None
    ) -> None:
        """Select the provider-proven native dialect before building its spec."""
        selected_dialect = dialect_for_tool_type(native_tool_type)
        configured = self._configured_tool_version
        if configured and dialect_for_tool_type(configured) is not selected_dialect:
            notice = (native_tool_type, configured)
            if notice not in self._cross_dialect_override_notices:
                logger.info(
                    "computer-use: ignoring cross-dialect configured tool_version %r "
                    "while provider selected native type %r",
                    configured,
                    native_tool_type,
                )
                self._cross_dialect_override_notices.add(notice)

        if selected_dialect is not ANTHROPIC:
            if self._tool_version != native_tool_type:
                self._tool_version = native_tool_type
            return

        anthro_configured = (
            configured
            if configured and dialect_for_tool_type(configured) is ANTHROPIC
            else None
        )
        anthro_previous = (
            self._tool_version
            if dialect_for_tool_type(self._tool_version) is ANTHROPIC
            else None
        )
        resolved, corrected = resolve_tool_version(
            model, anthro_configured, previous=anthro_previous
        )
        if dialect_for_tool_type(resolved) is not ANTHROPIC:
            resolved, corrected = resolve_tool_version(
                None, anthro_configured, previous=anthro_previous
            )
        if corrected:
            logger.info(
                "computer-use: model %r requires tool_version %r; correcting "
                "from %r to avoid the API rejecting every request with this pairing "
                "(see tool_versions.py)",
                model,
                resolved,
                self._tool_version,
            )
        if self._tool_version != resolved:
            self._tool_version = resolved

    def note_model(self, model: str | None) -> None:
        """Legacy model-only correction path for tools without provider dialect selection.

        `hook-computer-use` prefers `select_provider_native_tool_type()` so the
        provider-selected dialect is authoritative. This remains for legacy
        hooks and fake tools that only supply the model.

        Never raises: a mid-session exception here would take down the whole
        request.
        """
        resolved, corrected = resolve_tool_version(
            model, self._configured_tool_version, previous=self._tool_version
        )
        if corrected:
            logger.info(
                "computer-use: model %r requires tool_version %r; correcting "
                "from %r to avoid the API rejecting every request with this "
                "pairing (see tool_versions.py)",
                model,
                resolved,
                self._tool_version,
            )
        self._tool_version = resolved

    # -- coexistence guard wiring for every mutating action ----------------------
    @contextmanager
    def _guard_write(
        self,
        *,
        coord: tuple[int, int] | None = None,
        binding: _Binding | None = None,
    ):
        """Wrap one mutating action in the coexistence guard's before/after
        discipline (`docs/designs/coexistence.md` \u00a75.2/\u00a78.6), extended in
        this pass from `type_text` (the only action guarded before) to every
        action in `MUTATING`.

        Checked ONCE, around the whole action - not once per constituent
        click/motion inside a composite - matching \u00a78.4's "complete the
        composite (\u2264~200ms), then honour the pause" rule: `double_click`/
        `triple_click` (multiple clicks), `left_click_drag` (down+move+up),
        and `scroll` (N wheel notches) are each faster than the OS's own
        double-click timing window, so interrupting between their
        constituent events would silently convert a double_click into a
        single click - exactly the failure \u00a78.4 warns against. A human
        detected mid-composite is instead caught at the very next action's
        guard check, bounded by the same ~200ms this design already accepts
        as the pause-latency cost of an atomic composite (\u00a712). `type`
        keeps its own separate, finer-grained per-keystroke wiring
        (`backend.type_text(..., guard=guard)`) precisely because a
        multi-hundred-character string is NOT a tightly-timed composite -
        the two cases are handled differently on purpose, not by oversight.

        A no-op context (nothing enforced, identical to every action's
        behavior before this pass) when no guard exists for this backend/
        platform (`self._coexistence_guard is None` - e.g. Windows, or any
        platform with coexistence explicitly disabled).

        `binding`: the pinned `_Binding` snapshot `_run` is already
        dispatching this action against (see `_run`'s own docstring) - the
        policy-bypass race fix: the guard resolved here must be the SAME
        one that belongs to the backend `_run` is about to call, never a
        fresh `self._coexistence_guard` re-read that a concurrent
        `retarget()` could have already swapped out from under it. Defaults
        to a fresh `self._binding` read for callers with no pinned snapshot
        of their own (existing tests that call `_guard_write`/`_run`
        directly, outside `_execute_calls`).
        """
        binding = binding if binding is not None else self._binding
        guard = binding.coexistence_guard
        if guard is None:
            yield
            return
        if binding.is_remote and binding.announcement is not None:
            self._sync_remote_announcement_state(guard)
        guard.check_start_permission()
        guard.bind_target()
        guard.before_event(coord=coord)
        try:
            yield
        finally:
            guard.after_event()
            guard.release_target()

    # -- held-input ledger wiring for the LOCAL input path (band-lifetime.md
    # -- finding #1: `HeldInputLedger.hold()` used to be called ONLY from
    # -- `remote_agent.py` - a local `left_mouse_down` had zero enforcement:
    # -- no deadman, no release on halt/pause/cancel/target-change) ---------
    def _hold_mouse_button(
        self,
        button: str,
        backend: Backend,
        *,
        ledger: HeldInputLedger | None = None,
    ) -> None:
        """Register a just-pressed mouse button in the channel-scoped held-
        input ledger. Mirrors `RemoteAgent._op_mouse_down`'s pattern exactly
        (`remote_agent.py`): the release_fn reads its coordinates out of a
        mutable pending box at the moment it actually fires, rather than
        closing over this call's `(x, y)`, so a ledger-triggered release
        (deadman, halt, pause, target-change - none of which have a real
        `left_mouse_up` to supply fresh coordinates) still safely defaults
        to releasing at the button's last-known position.

        A no-op when this backend has no channel-scoped ledger (no
        coexistence guard was built for it - \\u00a75.5's \"never claim a
        guarantee you don't have\").

        `ledger`: the pinned binding's ledger (see `_run`'s docstring) -
        defaults to a fresh `self._ledger` read for callers with no pinned
        snapshot of their own.
        """
        ledger = ledger if ledger is not None else self._ledger
        if ledger is None:
            return
        token = f"mouse:{button}"
        pending: dict[str, int | None] = {"x": None, "y": None}
        self._mouse_pending[token] = pending

        def _release() -> None:
            backend.mouse_up(pending["x"], pending["y"], button)
            self._mouse_pending.pop(token, None)

        ledger.hold("mouse", token, _release)

    def _release_mouse_button(
        self,
        button: str,
        backend: Backend,
        x: int | None,
        y: int | None,
        *,
        ledger: HeldInputLedger | None = None,
    ) -> None:
        """Counterpart to `_hold_mouse_button`. When a matching hold is
        tracked, release THROUGH the ledger (after updating the pending box
        with this call's REAL coordinates) so `backend.mouse_up` fires
        EXACTLY ONCE - never once here and once more via the ledger's own
        release_fn. Falls back to calling `backend.mouse_up` directly when
        nothing is tracked (no ledger, or no matching down - e.g. `read_only`
        was toggled mid-session) - identical fallback to
        `RemoteAgent._op_mouse_up`.

        `ledger`: the pinned binding's ledger (see `_run`'s docstring) -
        defaults to a fresh `self._ledger` read for callers with no pinned
        snapshot of their own.
        """
        ledger = ledger if ledger is not None else self._ledger
        token = f"mouse:{button}"
        pending = self._mouse_pending.get(token)
        if ledger is not None and pending is not None:
            pending["x"], pending["y"] = x, y
            ledger.release(token)
        else:
            backend.mouse_up(x, y, button)

    def _sync_remote_announcement_state(self, guard: CoexistenceGuard) -> None:
        """Pull the target-side overlay's Pause/Cancel state
        (docs/designs/coexistence.md \u00a78.1/\u00a79.1) into THIS session's guard
        before every guarded write. The overlay lives entirely on the
        target (only that process can draw on that desktop, or observe a
        real click there) - a click is otherwise invisible to this
        controller until asked. Piggybacked on the exact cadence
        `before_event()`'s own presence sample already uses for a remote
        backend (\u00a75.2/\u00a75.7) rather than a second background thread or a
        parallel polling channel - the same once-per-guarded-write wire
        round trip discipline `presence_idle` already established, applied
        to a second fact instead of a new mechanism.

        Reuses `_on_overlay_pause`/`_on_overlay_cancel` verbatim - the same
        functions a LOCAL overlay's own click callback already calls
        in-process - so a remote pause/cancel is handled identically to a
        local one from this point forward (guard.pause.set(...) /
        durable-halt-record + release_all). Edge-triggered via
        `_remote_pause_seen`/`_remote_cancel_seen`: each fires at most once
        per session, since the target's own flags are latched
        (level-triggered, never cleared - see `RemoteAgent
        ._op_announcement_status`) and calling `_on_overlay_cancel` twice
        would write a redundant durable halt record for no benefit.

        Best-effort: a read failure here must never block the write path
        this guard already protects - \u00a76.0's halt invariant and this
        session's own live presence sample apply regardless of whether this
        secondary signal could be read this time.
        """
        status_fn = getattr(self._backend, "announcement_status", None)
        if status_fn is None:
            return
        try:
            status = status_fn()
        except BackendError as exc:
            logger.warning(
                "coexistence: remote announcement_status read failed "
                "(backend=%r): %s - a human's Pause/Cancel click on the "
                "remote overlay would be invisible to this controller until "
                "the next successful read",
                self._backend.name,
                exc,
            )
            return
        if status.get("cancelled") and not self._remote_cancel_seen:
            self._remote_cancel_seen = True
            # Bug-hunt defect A fix: `_halt_key`, not the bare
            # `backend.name` - see that function's docstring.
            _on_overlay_cancel(guard, _halt_key(self._backend))
            return
        if status.get("paused") and not self._remote_pause_seen:
            self._remote_pause_seen = True
            _on_overlay_pause(guard, self._backend.name)

    # -- session-start disclosure: fired at first real use, not mount() ---------
    def _ensure_announced(self) -> None:
        """Fire the session-start disclosure (docs/designs/coexistence.md \u00a77) on
        THIS session's first real action, and never before.

        Why not `mount()` (the defect this closes): `amplifier_core`'s loader
        calls every tool module's `mount()` TWICE per real session - once as a
        throwaway protocol-compliance probe
        (`amplifier_core.validation.tool.ToolValidator._check_protocol_compliance`,
        against a fresh `MockCoordinator` whose `mount()` result is discarded
        and torn down in a `finally` block a few lines later - see that
        module's source), and once for real
        (`loader.mount_with_config_ep`/`mount_with_config_direct_ep`). Both
        calls run the module's real `mount()` function with the real config.
        Building the disclosure inside `mount()` meant the FIRST dialog a
        human ever answered could be for the discarded probe - and any
        consent given applied to a `ComputerTool`/`CoexistenceGuard` pair
        about to be thrown away, not the one actually about to drive
        anything. Gating here instead means a validation probe - which never
        calls `execute()` on the tool it mounted, only `mount()` itself -
        cannot trigger this at all: there is no code path from
        `_check_protocol_compliance` to this method.

        What counts as "first use": ANY action on `computer` or `desktop`,
        not just a mutating one. Both classes' `execute()` call this before
        doing anything else (`ComputerTool.execute` directly;
        `DesktopTool.execute` via `self._computer._ensure_announced()`), so a
        pure read - `screenshot`, `zoom`, `list_windows`, `get_clipboard` - is
        gated identically to a click or a keystroke. A screenshot IS a
        capture of a human's screen; gating only writes would let an agent
        silently see everything on the target's display before ever
        disclosing that it was watching, which defeats the entire purpose of
        a session-start disclosure. There is exactly one gate, not one gate
        for writes and a silent hole for reads.

        Ordering: because this runs synchronously (via `asyncio.to_thread`,
        same as `_run` itself) as the very first statement of `execute()`,
        the announcement fully COMPLETES - dialog answered, overlay actually
        shown, or the channel's own failure policy resolved - before any
        backend call for that action begins. Not concurrent with the first
        action, not merely started before it: this call returns (or raises)
        before `execute()`'s own dispatch logic ever runs.

        Concurrency: idempotent and thread-safe. Two actions issued by the
        model in the same turn can race to be "first" on two different
        worker threads. `self._announce_lock` is a per-instance lock
        (distinct from the module-level `_announcement_lock`, which guards
        the cross-instance/cross-process channel cache in
        `_build_announcement`) - double-checked so the actual
        dialog/overlay/RPC call, which can genuinely block (a countdown
        timer, an SSH round trip), only ever runs once per instance; every
        other caller either returns immediately (already announced) or
        blocks briefly on the lock and then reuses whatever the winner
        decided.

        Refusal is STICKY for the life of this instance (this mount, this
        session): once `self._announce_refused` is set, every later call -
        from this thread or any other, for `computer` OR `desktop` -
        re-raises the SAME `AnnouncementRefused` immediately, without
        touching the backend again. Combined with there being exactly one
        gate both tools' `execute()` methods call, this makes "refused" mean
        "stop driving" structurally, not by convention: no action from
        either tool can reach the backend without passing through here
        first, and once refused this method never again returns normally.

        M4 (docs/designs/capability-awareness.md \u00a76): reads `self._binding`
        ONCE per check (`binding = self._binding`), never `self._announce_refused`
        and `self._announced` as two separate attribute reads - see
        `_Binding`'s own docstring for why that distinction is the whole fix.
        `retarget()` shares THIS SAME `self._announce_lock` for its own
        build+swap, so a first-use build here and a concurrent `retarget()`
        can never both be "in progress" at once: whichever gets the lock
        first runs to completion (installing a fully-formed, ALREADY-
        disclosed `_Binding`) before the other can even start, which is what
        makes this method's unlocked fast-path reads safe against a
        concurrent retarget, not merely unlikely to lose the race.
        """
        binding = self._binding
        if binding.announce_refused is not None:
            raise binding.announce_refused
        if binding.announced:
            return
        with self._announce_lock:
            # Double-checked: another thread (or a retarget) may have
            # already announced (or been refused, or installed an entirely
            # new binding) while this thread waited for the lock above -
            # re-read rather than trust the snapshot taken before it.
            binding = self._binding
            if binding.announce_refused is not None:
                raise binding.announce_refused
            if binding.announced:
                return
            try:
                handle = _build_announcement(
                    binding.backend,
                    binding.coexistence_guard,
                    self._cfg,
                    self.resolve_display(),
                )
            except AnnouncementRefused as exc:
                self._announce_refused = exc
                # Same best-effort, idempotent, refcounted cleanup mount()
                # used to perform on a mount-time refusal. Safe for a REMOTE
                # backend too - `close()` only decrements THIS handle's own
                # refcount (see shared_transport.py); it can never tear down
                # a connection a different, already-consented session is
                # using against the same target.
                try:
                    binding.backend.close()
                except Exception:  # noqa: BLE001 - best-effort cleanup on refusal
                    logger.debug(
                        "tool-computer-use: backend.close() failed after a "
                        "first-use announcement refusal",
                        exc_info=True,
                    )
                raise
            self._announcement = handle
            self._announced = True

    # -- M4: live re-target (docs/designs/capability-awareness.md \u00a76) --------
    def retarget(self, target: str | None) -> ToolResult:
        """`desktop(action="retarget")` - switch which machine this session
        drives, mid-conversation, without restarting. \u00a76.2's sequence:
        build the WHOLE new binding (connect, guard, disclosure, policy,
        display) before touching anything this session currently uses,
        then replace `self._binding` with ONE atomic reference assignment
        (see `_Binding`'s own docstring for why that - not a convenience -
        is the fix for the verified race). Any failure before the swap
        leaves the CURRENT binding exactly as it was; never a partial
        retarget.

        `target`: `"ssh://user@host[:port]"` to switch to a remote machine,
        or `"local"` (or falsy/omitted) to switch back to whatever local
        backend this machine can serve. Takes NO other arguments - \u00a76.6:
        this is a machine-selection mechanism, not a policy-escalation one.
        `read_only`/`gate_writes`/`clipboard_read_policy` are always
        recomputed from the SAME mount-time config, against the new
        target's remoteness, exactly like `__init__` does at mount.
        """
        normalized = str(target).strip() if target else ""
        new_cfg = dict(self._cfg)
        if normalized and normalized != "local":
            new_cfg["target"] = normalized
        else:
            new_cfg.pop("target", None)

        # Step 0 (\u00a76.2): REFUSE-IF-BUSY. Reusing `_announce_lock` (rather
        # than a second lock) for the "is a disclosure decision already
        # being built" half of this is what makes the verified race
        # structurally impossible - see `_ensure_announced`'s docstring.
        # Non-blocking: a busy channel means genuinely busy right now: fail
        # loud and let the caller retry, rather than block this tool call
        # for up to 30s behind someone else's dialog/connect.
        if not self._announce_lock.acquire(blocking=False):
            return ToolResult(
                success=False,
                error={
                    "message": (
                        "retarget refused: a disclosure/announcement decision "
                        "is already being built for this session (its first "
                        "real action, or another retarget) - retry once it "
                        "completes"
                    ),
                    "type": "RetargetRefused",
                },
            )
        try:
            current = self._binding
            # Second TOCTOU fix: this read used to be unlocked
            # (`current.band_state.depth != 0`) while `_band_enter`/
            # `_band_exit` only ever mutate `depth` under `state.lock` - an
            # unlocked reader here could race a concurrent increment/
            # decrement and observe a torn/stale value. Read it under the
            # SAME lock those two methods use.
            #
            # Labeling correction (council re-review): a prior round of this
            # fix was described as having "eliminated" band-depth tracking's
            # role here. That framing was wrong - nothing about band-depth
            # tracking was eliminated. It still exists, unchanged, right
            # below, and is still consulted here: a lens correctly objected,
            # "nothing was eliminated, it survived, and grew a lock and a
            # paragraph" (the TOCTOU lock fix above and this very comment).
            #
            # What WAS eliminated is narrower and precise: the policy-bypass
            # race's fix no longer DEPENDS on band-depth tracking for
            # correctness. This check is a BUSY-REFUSAL COURTESY, not the
            # safety mechanism that prevents the race: band-depth tracking
            # only ever engages for a LOCAL Linux-X11/Windows overlay (see
            # `_live_band_handle`) - it is `None`/never-incremented for
            # macOS and for every REMOTE target, i.e. for most of what M4
            # retarget exists to reach. Making this check airtight would
            # still leave every other platform unprotected. The actual fix
            # for the policy-bypass race is the single-snapshot discipline
            # in `_execute_calls`/`_run` (see those methods' docstrings) -
            # deliberately made independent of this counter's availability,
            # rather than extending band-depth tracking to every platform/
            # remote target (a materially larger change - a new wire
            # control op for remote raise/lower, per `_live_band_handle`'s
            # own docstring - for no correctness benefit once the snapshot
            # discipline holds on its own).
            band_state = current.band_state
            if band_state is not None:
                with band_state.lock:
                    depth = band_state.depth
                if depth != 0:
                    return ToolResult(
                        success=False,
                        error={
                            "message": (
                                f"retarget refused: {depth} "
                                "action(s) currently in flight against the "
                                "current binding"
                            ),
                            "type": "RetargetRefused",
                        },
                    )
            if current.ledger is not None and current.ledger.held_tokens:
                return ToolResult(
                    success=False,
                    error={
                        "message": (
                            "retarget refused: input still held on the "
                            f"current binding ({current.ledger.held_tokens!r}) "
                            "- release it (or let it release) before "
                            "switching targets"
                        ),
                        "type": "RetargetRefused",
                    },
                )

            # Step 1 (\u00a76.2): PARSE + BUILD - connect + handshake. Any
            # failure here leaves the CURRENT binding completely untouched -
            # nothing has been built yet to release.
            try:
                new_backend = select_backend(new_cfg)
            except (NoBackendAvailable, ValueError, TypeError) as exc:
                return ToolResult(
                    success=False,
                    error={
                        "message": f"retarget refused: {exc}",
                        "type": type(exc).__name__,
                    },
                )
            except Exception as exc:
                # Same pattern `mount()` uses: `select_backend`'s remote
                # branch raises `RemoteTargetUnavailable` for an explicit,
                # unreachable target, deliberately NOT as `NoBackendAvailable`
                # - it must never be silently swallowed into a fallback.
                from .remote_backend import RemoteTargetUnavailable

                if not isinstance(exc, RemoteTargetUnavailable):
                    raise
                return ToolResult(
                    success=False,
                    error={
                        "message": f"retarget refused: {exc}",
                        "type": type(exc).__name__,
                    },
                )

            # Same-channel short circuit (\u00a76.5: "Same target as current ->
            # No-op, reported as such. Never silently re-disclose") -
            # computed from the actual CONNECTED backend's identity, not
            # the requested string, so two differently-spelled targets that
            # resolve to the same physical machine are still recognized
            # (matches `_channel_identity`'s own reasoning). Release the
            # extra connection this probe just opened (refcounted - see
            # `_build_ssh_transport`; a no-op for local backends) and
            # return without touching anything else.
            if _channel_identity(new_backend) == _channel_identity(current.backend):
                try:
                    new_backend.close()
                except Exception:  # noqa: BLE001 - best-effort
                    logger.debug(
                        "computer-use: new_backend.close() failed after a "
                        "same-target retarget no-op",
                        exc_info=True,
                    )
                return ToolResult(
                    success=True,
                    output=(
                        f"retarget no-op: already targeting {current.backend.name!r}"
                    ),
                )

            try:
                # Step 2 (\u00a76.2): GUARD.
                new_guard = _build_coexistence_guard(new_backend, new_cfg)
                new_coexistence_cfg = dict(new_cfg.get("coexistence") or {})
                if new_guard is None:
                    if bool(new_coexistence_cfg.get("retarget_allow_no_guard", False)):
                        logger.warning(
                            "computer-use: retarget to %r proceeding with NO "
                            "coexistence guard (coexistence."
                            "retarget_allow_no_guard override) - no halt "
                            "protection, no disclosure channel",
                            new_backend.name,
                        )
                    else:
                        # \u00a76.5: a mid-session downgrade of an already-
                        # protected session to a target with no presence
                        # detector at all is silent degradation by default -
                        # refuse, the same explicit/logged opt-out shape
                        # \u00a77.6 already uses for `drive_anyway`.
                        raise _RetargetRefused(
                            f"retarget refused: no coexistence guard could be "
                            f"built for {new_backend.name!r} - this target has "
                            "no proven presence-detector wiring, and switching "
                            "to it mid-session would silently remove halt "
                            "protection and the disclosure channel for an "
                            "already-protected session (docs/designs/"
                            "capability-awareness.md \u00a76.5). Set "
                            "coexistence.retarget_allow_no_guard=true to "
                            "override - logged every time it fires."
                        )
                new_channel_key = _channel_identity(new_backend)
                new_ledger = _get_channel_ledger(new_channel_key)
                new_band_state = _get_channel_band_state(new_channel_key)

                # Step 3 (\u00a76.2): MOUNT-TIME REFUSAL - the same check
                # `mount()` itself runs, against the NEW backend.
                refusal = _refuse_if_disclosure_declined_with_human_present(
                    new_coexistence_cfg, new_guard, new_backend
                )
                if refusal is not None:
                    raise _RetargetRefused(f"retarget refused: {refusal}")

                # Step 6 (\u00a76.2), computed here (before DISCLOSE) so a
                # display-resolution failure never leaves an already-shown
                # dialog/overlay with nothing to release but the backend
                # (\u00a76.4: never hide/tear down an announcement handle here -
                # it may be a SHARED channel a different consumer already
                # relies on; only `new_backend.close()` is ever safe to call
                # unconditionally on failure - see that call's own comment
                # in the exception handler below).
                new_display, new_monitor = self._resolve_display_for_target(
                    self._target_monitor,
                    allow_fallback=not self._target_monitor_explicit,
                    backend=new_backend,
                )

                # Step 4 (\u00a76.2): DISCLOSE - may show a dialog / raise an
                # overlay on the NEW target. `AnnouncementRefused` propagates
                # (caught below); the old binding is untouched either way.
                new_handle = _build_announcement(
                    new_backend, new_guard, new_cfg, new_display
                )

                # Step 5 (\u00a76.2): POLICY - recomputed from the SAME config,
                # against the NEW target's remoteness. Identical rules to
                # `__init__` - kept in sync deliberately (\u00a76.6: retarget is
                # not a way to widen policy; explicit config still wins,
                # exactly like mount).
                new_is_remote = bool(getattr(new_backend, "is_remote", False))
                read_only_cfg = new_cfg.get("read_only")
                new_read_only = (
                    new_is_remote if read_only_cfg is None else bool(read_only_cfg)
                )
                gate_cfg = new_cfg.get("gate_writes")
                if gate_cfg is None:
                    new_gate_writes = new_is_remote and not new_read_only
                else:
                    new_gate_writes = bool(gate_cfg)
                clipboard_cfg = new_cfg.get("clipboard_read_policy")
                if clipboard_cfg is None:
                    new_clipboard_policy = "redact" if new_is_remote else "allow"
                else:
                    new_clipboard_policy = str(clipboard_cfg)
            except AnnouncementRefused as exc:
                try:
                    new_backend.close()
                except Exception:  # noqa: BLE001 - best-effort cleanup on refusal
                    logger.debug(
                        "computer-use: new_backend.close() failed after a "
                        "retarget disclosure refusal",
                        exc_info=True,
                    )
                return ToolResult(
                    success=False,
                    error={"message": str(exc), "type": "AnnouncementRefused"},
                )
            except _RetargetRefused as exc:
                try:
                    new_backend.close()
                except Exception:  # noqa: BLE001 - best-effort cleanup on refusal
                    logger.debug(
                        "computer-use: new_backend.close() failed after a "
                        "retarget refusal",
                        exc_info=True,
                    )
                return ToolResult(
                    success=False,
                    error={"message": str(exc), "type": "RetargetRefused"},
                )
            except Exception as exc:  # noqa: BLE001
                # Anything else that fails between BUILD and the commit
                # point below (a guard helper raising, display resolution
                # raising, ...): release the half-built new backend -
                # refcounted, so this NEVER tears down a DIFFERENT
                # consumer's connection to the same target, see
                # `RemoteBackend.close`/`shared_transport.py` - and leave
                # the CURRENT binding exactly as it was. Never hide/release
                # `new_handle` here even if one was already built above:
                # \u00a76.4's own rule ("release, never destroy") applies with
                # equal force to a FAILED retarget - the handle may be a
                # cached, SHARED channel a different session is already
                # relying on.
                try:
                    new_backend.close()
                except Exception:  # noqa: BLE001 - best-effort cleanup on failure
                    logger.debug(
                        "computer-use: new_backend.close() failed while "
                        "cleaning up a failed retarget",
                        exc_info=True,
                    )
                logger.exception(
                    "computer-use: retarget to %r failed", normalized or "local"
                )
                return ToolResult(
                    success=False,
                    error={
                        "message": f"retarget failed: {exc}",
                        "type": type(exc).__name__,
                    },
                )

            # --- commit point (\u00a76.2): nothing above this line has touched
            # the current binding -------------------------------------------
            new_binding = _Binding(
                backend=new_backend,
                is_remote=new_is_remote,
                read_only=new_read_only,
                gate_writes=new_gate_writes,
                clipboard_read_policy=new_clipboard_policy,
                coexistence_guard=new_guard,
                channel_key=new_channel_key,
                ledger=new_ledger,
                band_state=new_band_state,
                display=new_display,
                current_monitor=new_monitor,
                announced=True,
                announcement=new_handle,
                announce_refused=None,
                remote_pause_seen=False,
                remote_cancel_seen=False,
            )
            old = self._binding
            # Step 7 (\u00a76.2): SWAP - the fix the review named: ONE atomic
            # reference assignment, never a field at a time. A reader can
            # only ever observe the OLD binding whole or the NEW binding
            # whole (see `_Binding`'s docstring).
            self._binding = new_binding
            self._cfg = new_cfg
            self._monitors = []
            # Step 8 (\u00a76.2): RELEASE OLD - release this session's OWN
            # reference, never the channel itself (\u00a76.4: a delegated child,
            # or a second tool config, may still be driving the SAME old
            # channel through the SAME shared handle - `close()` only
            # decrements THIS handle's refcount).
            try:
                old.backend.close()
            except Exception:  # noqa: BLE001 - best-effort
                logger.debug(
                    "computer-use: old_backend.close() failed after a "
                    "successful retarget",
                    exc_info=True,
                )
            logger.info(
                "computer-use: retargeted from %r to %r",
                old.backend.name,
                new_backend.name,
            )
            return ToolResult(
                success=True,
                output=json.dumps(
                    {
                        "retargeted_to": new_backend.name,
                        "is_remote": new_is_remote,
                        "user_host": getattr(new_backend, "user_host", None),
                    },
                    default=str,
                ),
            )
        finally:
            self._announce_lock.release()

    # -- band lifetime (docs/designs/band-lifetime.md, Alt A - \u00a711.1) --------
    # Scopes the Linux/Windows disclosure band's lifetime to actual activity
    # instead of the tool's process lifetime: raised before every execute(),
    # lowered the instant the last in-flight execute() against this CHANNEL
    # returns. No reaper thread, no trailing window `T` - the adversarial
    # review's blocking finding was against that apparatus specifically, not
    # against scoping the band to activity per se (see the design doc's
    # revision history for the full review).
    #
    # `_ensure_announced` (above) is the CONSENT gate and is untouched by
    # this: it fires once, asks a human if needed, and builds the handle
    # this section re-raises/lowers. Nothing here can ask a human anything
    # or clear a sticky refusal.
    def _live_band_handle(self) -> Any | None:
        """This session's announcement handle, narrowed to one that supports
        live re-raise/lower (`show()`/`hide()`) - the Linux/Windows overlay.
        `None` for macOS's one-shot dialog, a remote overlay handle (no local
        object to call - \\u00a76.4's raise/lower cross the wire as a DIFFERENT,
        not-yet-built control op, deliberately out of scope here, see the
        design doc \\u00a76.4/\\u00a711), or no channel at all. Alt A's raise/lower
        dance applies to none of those - they are unaffected, exactly as
        before this fix.
        """
        handle = self._announcement
        if handle is not None and hasattr(handle, "show") and hasattr(handle, "hide"):
            return handle
        return None

    def _band_enter(self) -> tuple[Any, _ChannelBandState] | None:
        """Call before doing any work for one `execute()` call (the WHOLE
        call - a batch of N actions is one raise, matching \\u00a74.2's \"one
        execute(), one raise, one lower\"). Returns the (handle, state) pair
        `_band_exit` needs, or `None` when band-lifetime tracking does not
        apply to this backend (see `_live_band_handle`) - in which case
        `_band_exit(None)` is a no-op.

        Raises `AnnouncementRefused` if a re-raise is needed and fails with a
        human detected present (\\u00a77.6, via `_handle_channel_failure` -
        reused verbatim, the exact policy the FIRST raise already applies).
        """
        handle = self._live_band_handle()
        state = self._band_state
        if handle is None or state is None:
            return None
        with state.lock:
            state.depth += 1
            try:
                self._ensure_band_raised(handle)
            except BaseException:
                # Never leave `depth` incremented with no matching
                # decrement - a failed raise must not leak a phantom
                # in-flight action that later blocks every legitimate
                # lower forever.
                state.depth -= 1
                raise
        return handle, state

    def _ensure_band_raised(self, handle: Any) -> None:
        """Re-raise a channel that was already consented to. Never asks a
        human anything (that already happened, in `_ensure_announced`) and
        never clears a sticky refusal - only the FIRST raise can do either.
        A no-op when the band is already up (`handle.shown`), which is the
        overwhelmingly common case: one continuous episode raises once and
        stays up for its whole duration, exactly like today's behavior."""
        if getattr(handle, "shown", False):
            return
        guard = self._coexistence_guard
        try:
            handle.show()
        except Exception as exc:  # noqa: BLE001 - \\u00a77.6 policy decides below
            if guard is not None:
                _handle_channel_failure(guard, self._backend.name, "band re-raise", exc)
            else:
                logger.error(
                    "coexistence: band re-raise failed for backend %r and no "
                    "coexistence guard exists to apply \\u00a77.6 policy - "
                    "proceeding without reconfirming disclosure: %s",
                    self._backend.name,
                    exc,
                )

    def _band_exit(self, token: tuple[Any, _ChannelBandState] | None) -> None:
        """Call in a `finally` around whatever `_band_enter` guarded, always
        - even when `_band_enter` itself raised (see `execute()`), so a
        raise failure still decrements the depth it incremented.

        The band actually lowers only when BOTH: this was the last in-flight
        execute() against the channel (`depth == 0`, re-checked under the
        SAME lock right before calling `hide()` - without that check, a new
        action could start between scheduling this call and acquiring the
        lock, and lowering would drop the band out from under it, which the
        hard \"no path may drop the band while an action is in flight\"
        constraint forbids), AND the channel's held-input ledger has nothing
        held (\\u00a74.2's second invariant clause: a button held past its own
        execute() call means force is still being applied even with no
        execute() in flight).
        """
        if token is None:
            return
        handle, state = token
        with state.lock:
            state.depth -= 1
            depth_now = state.depth
        if depth_now != 0:
            return
        ledger = self._ledger
        if ledger is not None and ledger.held_tokens:
            return
        with state.lock:
            if state.depth != 0:
                return
            handle.hide()

    # -- execution --------------------------------------------------------------
    def _run(
        self,
        action: str,
        params: dict[str, Any],
        *,
        binding: _Binding | None = None,
    ) -> tuple[str, str | None]:
        """Run one Anthropic computer-tool action against a backend.

        Returns (text_summary, base64_png_or_None). Mirrors the dispatch logic that
        used to live inside `WindowsBridge.execute` - now backend-agnostic: it only
        ever calls the `Backend` protocol, never a concrete backend's internals.

        `binding`: the fix for a six-lens council's verified policy-bypass
        race - a lens's standalone repro against unmodified production code
        reproduced it on the first try: a MUTATING action's `read_only`
        check (in `_execute_calls`) passed under the OLD, permissive
        binding, but by the time this method went on to read `self._backend`
        a moment later, a concurrent `retarget()` had already swapped in a
        NEW, restrictive binding - and the write landed on the NEW target
        anyway, having been checked against a binding that no longer
        applied. The fix: the caller (`_execute_calls`/
        `DesktopTool._execute_action`) reads `self._binding` ONCE, BEFORE
        its own check, and passes that SAME object here - so the check and
        every backend/guard/display access this call makes are pinned to
        one atomic-view snapshot, and a `retarget()` swap that lands
        anywhere during this call can never change what it dispatches
        against. This now includes `list_windows`/`focus_window`'s monitor
        attribution (`_monitors_for_attribution`/`_focus_monitor_warning`,
        both take this SAME `binding`) and `type`'s guard-support check
        (derived fresh from `binding.backend` on every call, never cached
        from a backend a `retarget()` may have since replaced) - council
        re-review Findings 1 and 3 named both as reads that used to bypass
        the pin. Defaults to a fresh, single `self._binding` read when not
        given (existing tests that call `_run(...)` directly, with no check
        of their own to keep in sync) - there is no check to straddle in
        that case, but a single fresh read here still fixes this method's
        own former internal inconsistency (`disp = self.display` and
        `backend = self._backend` used to be two SEPARATE `self._binding`
        reads, themselves racy against a retarget landing between them).

        Two reads are DELIBERATELY excluded from the pin, same spirit as
        `_Binding`'s own `_mouse_pending` exclusion - named here rather
        than left for the claim above to be read as unqualified:
        `screen_info`'s `self.resolve_display(refresh=True)` call (this
        action's whole purpose is to report CURRENT geometry, not this
        call's pinned snapshot - see that branch's own comment), and
        `_monitors_for_attribution`'s `self._monitors` fallback (a best-
        effort cache used only when live enumeration fails right now,
        degrading attribution to "unknown" rather than affecting anything
        this method actually dispatches against).
        """
        binding = binding if binding is not None else self._binding
        if binding.display is None:
            # Same fail-loud contract as the `display` property (mount()
            # resolves eagerly; this should never happen in practice) - not
            # bypassed just because this reads `binding` instead of the
            # property now.
            raise BackendError(
                "display geometry not resolved; resolve_display() must be called at mount time"
            )
        disp = binding.display
        backend = binding.backend

        def coord(key: str = "coordinate") -> tuple[int, int]:
            raw = params.get(key)
            if not isinstance(raw, (list, tuple)) or len(raw) < 2:
                raise ValueError(f"action {action!r} requires {key} as [x, y]")
            return disp.to_screen(float(raw[0]), float(raw[1]))

        text = params.get("text") or params.get("key")

        if action == "screenshot":
            # Scoped to the current monitor, not cropped from a full-desktop grab:
            # when a monitor is targeted, pass its bounds as an explicit region so
            # both backends capture that region directly (`Graphics.CopyFromScreen`
            # on Windows, X `GetImage` on Linux) - never the whole virtual desktop
            # downscaled and then implicitly "close enough". `None` here (only in
            # virtual-desktop mode) preserves the original whole-desktop capture.
            region = None
            if binding.current_monitor is not None:
                m = binding.current_monitor
                region = (m.x, m.y, m.x + m.width, m.y + m.height)
            b64 = capture_scaled_b64(
                backend, disp, region, self._max_edge, self._max_pixels
            )
            # \u00a73 audit hardening: a content hash for every capture, never the
            # pixels themselves in the log - the same digest-not-plaintext
            # discipline applied below to type/set_clipboard.
            logger.info(
                "computer-use audit: op=screenshot sha256=%s",
                _bytes_digest(base64.standard_b64decode(b64)),
            )
            return "screenshot captured", b64

        if action == "zoom":
            # Models reach for `region` about as often as `coordinate`, and sometimes
            # split it across start_coordinate/coordinate. Accept all three rather
            # than burning a turn on a schema correction.
            raw = params.get("coordinate") or params.get("region")
            if (not isinstance(raw, (list, tuple)) or len(raw) < 4) and params.get(
                "start_coordinate"
            ):
                s_, e_ = params["start_coordinate"], params.get("coordinate") or []
                if len(s_) >= 2 and len(e_) >= 2:
                    raw = [s_[0], s_[1], e_[0], e_[1]]
            if not isinstance(raw, (list, tuple)) or len(raw) < 4:
                raise ValueError(
                    "zoom requires a 4-element region: coordinate=[x1, y1, x2, y2]"
                )
            x1, y1 = disp.to_screen(raw[0], raw[1])
            x2, y2 = disp.to_screen(raw[2], raw[3])
            region = (x1, y1, max(x1 + 8, x2), max(y1 + 8, y2))
            b64 = capture_scaled_b64(
                backend, disp, region, self._max_edge, self._max_pixels
            )
            return f"zoomed to region {list(raw)}", b64

        if action == "cursor_position":
            sx, sy = backend.cursor_position()
            mx, my = disp.to_model(sx, sy)
            note = ""
            if binding.current_monitor is not None:
                m = binding.current_monitor
                if not (m.x <= sx < m.x + m.width and m.y <= sy < m.y + m.height):
                    # Honest, not synthetic: to_model() above already clamped
                    # (sx, sy) to the targeted monitor's edge because the real
                    # cursor is elsewhere. Say so rather than silently reporting
                    # a clamped position as if it were exact.
                    note = (
                        f" [warning: real cursor is outside targeted monitor "
                        f"{m.id!r} ({m.width}x{m.height} at {m.x},{m.y}); "
                        "position above is clamped to the nearest edge, not exact]"
                    )
            return f"cursor at [{mx}, {my}] (model space){note}", None

        if action == "screen_info":
            # The one deliberate refresh path (alongside select_monitor):
            # re-resolves and re-caches geometry for the CURRENT target, so a
            # resolution change is picked up without touching the hot path.
            fresh = self.resolve_display(refresh=True)
            payload: dict[str, Any] = {
                "screen_width": fresh.screen_width,
                "screen_height": fresh.screen_height,
                "model_width": fresh.model_width,
                "model_height": fresh.model_height,
                "origin_x": fresh.origin_x,
                "origin_y": fresh.origin_y,
                "target_monitor": self._target_monitor,
            }
            if binding.current_monitor is not None:
                payload["monitor_id"] = binding.current_monitor.id
                payload["monitor_primary"] = binding.current_monitor.primary
            elif self._target_monitor != VIRTUAL_DESKTOP:
                # Degraded: a per-monitor target was requested but enumeration
                # was unavailable, so geometry is the whole virtual-desktop
                # bounding box. A logger.warning alone is not enough - the model
                # is the one reasoning about this coordinate space, so it has to
                # be told. On a multi-monitor desktop the bounding box can span
                # large regions where no display exists at all, and clicks there
                # land nowhere.
                payload["degraded"] = "monitor-enumeration-unavailable"
                payload["coordinate_space"] = "virtual-desktop-bounding-box"
                payload["warning"] = (
                    "Per-monitor targeting is unavailable on this host, so these "
                    "coordinates span the whole virtual desktop. On a multi-monitor "
                    "setup this space may contain gaps with no display behind them; "
                    "clicks there do nothing."
                )
            return json.dumps(payload), None

        if action == "list_windows":
            result = backend.list_windows()
            monitors = self._monitors_for_attribution(binding)
            visible = [w for w in result.windows if not w.minimized][:25]
            lines = []
            for w in visible:
                mon = attribute_monitor(w.rect, monitors)
                lines.append(f"  [{w.handle}] {w.title} (monitor={mon!r})")
            listing = "\n".join(lines)
            return f"visible windows (foreground={result.foreground}):\n{listing}", None

        if action == "focus_window":
            handle = params.get("handle")
            if not handle:
                raise ValueError("action 'focus_window' requires 'handle'")
            with self._guard_write(binding=binding):
                backend.focus_window(str(handle))
            note = self._focus_monitor_warning(str(handle), binding)
            return f"focused window {handle}{note}", None

        if action in _CLICK_ACTIONS:
            button, count = _CLICK_ACTIONS[action]
            x, y = coord() if params.get("coordinate") is not None else (None, None)
            with self._guard_write(
                coord=(x, y) if x is not None and y is not None else None,
                binding=binding,
            ):
                backend.click(x, y, button=button, count=count)
            where = (
                f" at {params.get('coordinate')}" if params.get("coordinate") else ""
            )
            logger.info("computer-use audit: op=%s%s", action, where)
            return f"{action}{where}", None

        if action == "mouse_move":
            x, y = coord()
            with self._guard_write(coord=(x, y), binding=binding):
                backend.move(x, y)
            return f"mouse_move at {params.get('coordinate')}", None

        if action == "left_mouse_down":
            x, y = coord() if params.get("coordinate") is not None else (None, None)
            with self._guard_write(
                coord=(x, y) if x is not None and y is not None else None,
                binding=binding,
            ):
                backend.mouse_down(x, y, "left")
                self._hold_mouse_button("left", backend, ledger=binding.ledger)
            return "left_mouse_down", None

        if action == "left_mouse_up":
            x, y = coord() if params.get("coordinate") is not None else (None, None)
            with self._guard_write(
                coord=(x, y) if x is not None and y is not None else None,
                binding=binding,
            ):
                self._release_mouse_button("left", backend, x, y, ledger=binding.ledger)
            return "left_mouse_up", None

        if action == "left_click_drag":
            start = (
                coord("start_coordinate") if params.get("start_coordinate") else None
            )
            end = coord()
            # Guarded once around the whole drag (down+move+up), per
            # `_guard_write`'s docstring - a drag is exactly the kind of
            # tightly-timed composite \u00a78.4 says must complete, not tear.
            # `coord=end` (not `start`): the exclusion-zone check (\u00a77.5)
            # cares about where the drag's synthetic input actually lands.
            with self._guard_write(coord=end):
                backend.drag(start, end)
            logger.info(
                "computer-use audit: op=left_click_drag start=%s end=%s", start, end
            )
            return f"dragged to {params.get('coordinate')}", None

        if action == "scroll":
            x, y = coord() if params.get("coordinate") is not None else (None, None)
            direction = params.get("scroll_direction") or params.get("direction")
            if not direction:
                raise ValueError("action 'scroll' requires 'scroll_direction'")
            amount = int(params.get("scroll_amount") or params.get("amount") or 3)
            with self._guard_write(
                coord=(x, y) if x is not None and y is not None else None
            ):
                backend.scroll(x, y, str(direction), amount)
            return f"scrolled {direction} x{amount}", None

        if action in {"key", "hold_key"}:
            if not text:
                raise ValueError(f"action {action!r} requires 'text'")
            with self._guard_write():
                if action == "key":
                    backend.key(str(text))
                else:
                    backend.hold_key(str(text), float(params.get("duration") or 1.0))
            # Combos (e.g. "ctrl+s") are short, symbolic, and not free-form
            # secret text the way typed prose or a clipboard payload can be -
            # logged directly, unlike \u00a73's digest-not-plaintext rule for
            # `type`/`set_clipboard` below.
            logger.info("computer-use audit: op=%s combo=%s", action, text)
            return f"pressed {text}", None

        if action == "type":
            if not text:
                raise ValueError("action 'type' requires 'text'")
            body = str(text)
            guard = binding.coexistence_guard
            # Council re-review Finding 1: this used to be a `self.X` flag
            # cached ONCE in `__init__` from the ORIGINAL backend's
            # `type_text` signature, and never refreshed on retarget -
            # mount on a backend without a `guard` kwarg (flag caches
            # `False`), retarget onto one that HAS it and gets a real,
            # active guard, and the whole check_start_permission()/
            # bind_target()/pacing sequence below was silently skipped for
            # `type`, with zero exception and zero log line. Deriving it
            # HERE, from `binding.backend` (the same single-snapshot pin
            # every other backend/guard/display access in this method
            # already uses - see this method's own docstring), closes that
            # gap: it can never be stale, because it is never cached across
            # a retarget in the first place. `inspect.signature()` is cheap
            # (microseconds) and this runs once per `type` action call, not
            # once per keystroke - the per-keystroke loop below only calls
            # `backend.type_text` itself.
            backend_supports_guard = (
                "guard" in inspect.signature(backend.type_text).parameters
            )
            guard_active = guard is not None and backend_supports_guard
            # Measured safety gap (type_pacing.py): a 202-character string
            # typed at full speed via a per-character guarded loop can
            # complete in ~70ms - an inter-character gap far narrower than
            # any platform's GUARD_MS, making the presence detector
            # structurally blind for the whole operation. Pacing is applied
            # HERE, in the one shared call site every backend routes
            # through, so Linux and macOS both benefit from a single fix
            # rather than a per-backend patch - and only when a guard is
            # actually active, per `resolve_type_pacing_ms`'s contract.
            pacing_ms = resolve_type_pacing_ms(
                self._type_pacing_ms, guard_active=guard_active
            )
            if guard_active and self._type_pacing_ms == 0:
                assert guard is not None
                logger.warning(
                    "computer-use: type_pacing_ms=0 explicitly configured "
                    "while a coexistence guard is active (backend=%r, "
                    "guard_ms=%.1f) - this disables the pacing that keeps "
                    "the inter-character gap wider than the guard band, "
                    "making the presence detector structurally blind for "
                    "the duration of this type_text call "
                    "(docs/designs/coexistence.md \u00a75.2). A deliberate, "
                    "logged choice, not a default.",
                    backend.name,
                    guard.presence.guard_ms,
                )
            if guard_active:
                assert guard is not None
                if binding.is_remote and binding.announcement is not None:
                    self._sync_remote_announcement_state(guard)
                # \u00a75.2/\u00a78.6: bind the delivery target once at operation
                # start; `backend.type_text` re-checks it (via the guard)
                # before EVERY keystroke, and records a fresh injection
                # timestamp after each one - this is what lets a human
                # keystroke landing mid-`type_text` be detected (O5), not
                # just between whole operations.
                guard.check_start_permission()
                guard.bind_target()
                try:
                    if pacing_ms > 0:
                        pacing_seconds = pacing_ms / 1000.0
                        for ch in body:
                            backend.type_text(ch, guard=guard)
                            time.sleep(pacing_seconds)
                    else:
                        backend.type_text(body, guard=guard)
                finally:
                    guard.release_target()
            else:
                backend.type_text(body)
            # §3 audit hardening: a digest, never the plaintext - typed
            # content frequently includes credentials (the same rationale
            # `docs/designs/remote-transport.md` §10 already gives for
            # `type_text`'s `args_digest`; this is where that discipline is
            # actually implemented, and where `set_clipboard` below matches it).
            logger.info(
                "computer-use audit: op=type chars=%d sha256=%s",
                len(body),
                _text_digest(body),
            )
            return f"typed {len(body)} characters", None

        if action == "wait":
            duration = float(params.get("duration", 1.0))
            time.sleep(duration)
            return f"waited {duration}s", None

        raise ValueError(f"unsupported action {action!r}")

    async def execute(self, input: dict[str, Any]) -> ToolResult:
        """Run one tool call, in whichever provider dialect it arrived in.

        The two live wire forms disagree about shape and cardinality -
        Anthropic sends one `{"action": ..., "coordinate": [...]}` per call;
        OpenAI batches N `{"type": ..., "x": ..., ...}` entries under `actions`
        and expects ONE result for the whole batch (`providers.py` has the full
        comparison). `providers.read_call` is the only place that knows the
        difference: it identifies the dialect from the payload's actual shape -
        never by asking which provider is mounted - and yields this tool's own
        `(action, params)` vocabulary. Everything after that line is shared, and
        was already shared before this seam existed: one `ACTIONS` check, one
        `read_only`/`MUTATING` gate, one `_run()`, one set of error handlers.
        There is no second, parallel dispatcher per vendor and there must never
        be one.

        Cardinality is not special-cased either: a single Anthropic action is a
        one-element batch, and the loop below is identical for both. The one
        genuine per-dialect difference is what a result must carry -
        `result_must_carry_screenshot`.

        The very first thing this does - before parsing the call, before
        anything else - is `_ensure_announced()` (see that method): the
        session-start disclosure gate, moved here from `mount()` so a
        throwaway protocol-compliance probe can never trigger it.
        """
        try:
            await asyncio.to_thread(self._ensure_announced)
        except AnnouncementRefused as exc:
            return ToolResult(
                success=False,
                error={"message": str(exc), "type": "AnnouncementRefused"},
            )
        # Band lifetime (docs/designs/band-lifetime.md, Alt A): one raise for
        # the WHOLE call (a batch of N actions is one execute()), lowered in
        # the `finally` below the instant this is the last in-flight
        # execute() against the channel. A re-raise failure with a human
        # detected present is the SAME refusal shape as the first raise.
        try:
            band_token = await asyncio.to_thread(self._band_enter)
        except AnnouncementRefused as exc:
            return ToolResult(
                success=False,
                error={"message": str(exc), "type": "AnnouncementRefused"},
            )
        try:
            return await self._execute_calls(input)
        finally:
            await asyncio.to_thread(self._band_exit, band_token)

    async def _execute_calls(self, input: dict[str, Any]) -> ToolResult:
        """The dialect-read + per-action dispatch loop, unchanged from
        before band-lifetime tracking existed - split out of `execute()`
        only so that method's own `try/finally` around `_band_enter`/
        `_band_exit` does not have to re-indent this whole body.

        Single-snapshot discipline (the fix for a six-lens council's
        verified policy-bypass race, reproduced against unmodified
        production code): `binding = self._binding` is read ONCE per
        action, BEFORE the `read_only`/`MUTATING` check, and that SAME
        object is passed into `_run` for the actual dispatch - never a
        second, later `self._binding` read (via `self._read_only`/
        `self._backend`/...) that a concurrent `retarget()` could have
        already swapped. See `_run`'s own docstring for the full rationale
        and the exact repro this closes.
        """
        dialect, calls = read_call(input, self.image_space)

        last_summary = ""
        last_image_b64: str | None = None
        try:
            # The iterable may be lazy and may raise while being pulled (see
            # `providers._normalize_openai_batch`), so iteration happens INSIDE
            # this handler: a malformed entry halfway through a batch fails
            # after the good actions before it have already run, exactly as the
            # per-item loop did before. Every ValueError raised by `_run` is
            # caught below and returned, so the only thing reaching this
            # handler is a dialect read failure.
            for action, params in calls:
                if action not in ACTIONS:
                    return ToolResult(
                        success=False,
                        error={
                            "message": f"unknown action {action!r}; expected one of {', '.join(ACTIONS)}"
                        },
                    )
                # ONE binding read covering BOTH the policy check below AND
                # the dispatch (`_run(..., binding=binding)`) - see this
                # method's own docstring and `_run`'s.
                binding = self._binding
                if binding.read_only and action in MUTATING:
                    return ToolResult(
                        success=False,
                        error={
                            "message": f"action {action!r} blocked: computer-use is mounted read_only"
                        },
                    )
                try:
                    # C4: `Backend` stays synchronous (local paths and all existing
                    # tests untouched), but a remote action can block on a network
                    # round trip for hundreds of milliseconds. Running it in a
                    # thread keeps the event loop live so cancellation can actually
                    # be serviced during that wait, instead of stalling behind a
                    # screenshot transfer. Cheap locally too - `asyncio.to_thread`
                    # on a microsecond-scale X11/Quartz call costs a thread-pool
                    # round trip, not a network one.
                    summary, image_b64 = await asyncio.to_thread(
                        self._run, action, params, binding=binding
                    )
                except HaltedError as exc:
                    return self._record_halt_result(action, exc)
                except (BackendError, ValueError) as exc:
                    return ToolResult(
                        success=False,
                        error={"message": str(exc), "type": type(exc).__name__},
                    )
                except Exception as exc:
                    logger.exception("computer action %s failed", action)
                    return ToolResult(
                        success=False,
                        error={"message": str(exc), "type": type(exc).__name__},
                    )
                last_summary = summary
                if image_b64 is not None:
                    last_image_b64 = image_b64
        except ValueError as exc:
            return ToolResult(
                success=False, error={"message": str(exc), "type": "ValueError"}
            )

        if last_image_b64 is None:
            if not dialect.result_must_carry_screenshot:
                return ToolResult(success=True, output=last_summary)
            # OpenAI's `computer_call_output` is invalid without an image, so
            # take one more if the batch's own actions produced none - the
            # model always sees the result of what it just did. A read
            # (never gated by `read_only`), so a fresh binding read here
            # (no check to keep in sync with) is fine.
            try:
                last_summary, last_image_b64 = await asyncio.to_thread(
                    self._run, "screenshot", {}
                )
            except (BackendError, ValueError) as exc:
                return ToolResult(
                    success=False,
                    error={"message": str(exc), "type": type(exc).__name__},
                )
        # `_run("screenshot", ...)` always returns image bytes (never None) -
        # see its own branch - so this is a real invariant, not a defensive guess.
        assert last_image_b64 is not None
        return self._screenshot_tool_result(last_summary, last_image_b64)

    def _record_halt_result(self, action: str, exc: HaltedError) -> ToolResult:
        """Shared halt bookkeeping for both `execute()`'s single-action path
        and `_execute_openai_action_batch()`'s per-item loop - see
        `execute()`'s original inline comment (Defect 1 + defect 2,
        docs/designs/coexistence.md \u00a76.0/\u00a713 D3) for the full rationale;
        moved here unchanged so both callers hit the exact same recording
        logic rather than two copies drifting apart."""
        self.halt_notices.append(
            {
                "at": time.time(),
                "action": action,
                "message": str(exc),
                "margin_ms": exc.snapshot.margin_ms,
                "guard_ms": exc.snapshot.guard_ms,
                "last_human_input_ago_ms": exc.snapshot.last_human_input_ago_ms,
                # \u00a75.7 (measured safety gap): declared alongside guard_ms,
                # not silently folded into it or omitted - see
                # presence.PresenceSnapshot's own docstring. ~0 for a
                # local backend; real and large for a remote one.
                "transport_latency_ms": exc.snapshot.transport_latency_ms,
                "effective_staleness_ms": exc.snapshot.effective_staleness_ms,
            }
        )
        # Bug-hunt defect A fix: `_halt_key(self._backend)`, not the bare
        # `backend.name` - see that function's docstring for why the bare
        # composite name is not unique per remote host.
        backend_name = _halt_key(self._backend)
        record_halt(backend_name, exc.snapshot, reason=str(exc))
        return ToolResult(
            success=False, error={"message": str(exc), "type": type(exc).__name__}
        )

    def _screenshot_tool_result(self, summary: str, image_b64: str) -> ToolResult:
        """Package a successful `_run()` outcome that produced an image into
        the marker `ToolResult` `hook-computer-use` looks for - unchanged
        logic, extracted out of `execute()` so `_execute_openai_action_batch()`
        can reuse it instead of re-implementing screenshot persistence.

        Screenshots live on disk; only a path travels in the transcript. The hook
        inlines the bytes at request time, so the transcript never carries base64.
        (Marker protocol unchanged - see hook-computer-use.)

        Security hardening: previously one flat directory shared by every
        session, relying entirely on the inherited umask for permissions -
        on a shared/multi-user controller box that can leave screenshots of
        a driven desktop world- or group-readable for the full TTL window.
        Now: a per-session subdirectory (`self._session_id`, set once in
        `__init__`), and BOTH the directory and the file get an explicit
        `os.chmod` after creation - umask only affects the mode requested
        at creation time, it is not itself a guarantee, so this is what
        actually enforces owner-only access regardless of umask.
        """
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(SHOT_DIR, _PRIVATE_DIR_MODE)
        # Prune BEFORE creating this call's own session directory - not
        # after. `_prune_shots()` also removes now-empty session
        # directories (housekeeping for past sessions); running it after
        # creating (but before writing into) the CURRENT session directory
        # would race with that cleanup and delete the directory this very
        # call is about to write into, since it is briefly empty.
        _prune_shots()
        session_dir = SHOT_DIR / self._session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(session_dir, _PRIVATE_DIR_MODE)

        path = session_dir / f"{uuid.uuid4().hex}.png"
        path.write_bytes(base64.standard_b64decode(image_b64))
        os.chmod(path, _PRIVATE_FILE_MODE)
        disp = self.display
        return ToolResult(
            success=True,
            output=json.dumps(
                {
                    MARKER: 1,
                    "text": f"{summary} ({disp.model_width}x{disp.model_height})",
                    "images": [str(path)],
                }
            ),
        )


DESKTOP_ACTIONS = [
    "list_windows",
    "focus_window",
    "screen_info",
    "get_clipboard",
    "set_clipboard",
    "list_monitors",
    "select_monitor",
    # M3 (docs/designs/capability-awareness.md \u00a75): the capability report -
    # see `DesktopTool.execute`'s disclosure-exemption comment for why this
    # one action is dispatched before `_ensure_announced`, and
    # `_build_doctor_report` for the hard content boundary that earns it.
    "doctor",
    # M4 (docs/designs/capability-awareness.md \u00a76): live re-target - like
    # `doctor`, dispatched before `_ensure_announced` (see `DesktopTool.
    # execute`'s comment) because this action's whole job is to establish
    # disclosure for a NEW target itself (`ComputerTool.retarget`'s own \u00a76.2
    # steps 3-4); gating it behind the CURRENT/OLD target's disclosure would
    # show a possibly-irrelevant dialog for a machine the caller is about to
    # leave.
    "retarget",
]

#: Clipboard *reads* travel to the model provider as tool output (see README Safety
#: section), the same exfiltration-risk surface `read_only` exists to close - but a
#: clipboard can carry things a screenshot never shows (a just-copied password, an
#: unseen paste buffer). `read_only` is documented as "screenshots only, all input
#: blocked"; a clipboard read is neither a screenshot nor input, but it is exactly
#: the kind of invisible exfiltration `read_only` mode is meant to prevent. Gated
#: accordingly - this is a deliberate behavior change from the original ungated
#: `get_clipboard`, not an oversight.
_READ_ONLY_BLOCKED = {"focus_window", "set_clipboard", "get_clipboard"}

#: Desktop actions that change target state - gated by `gate_writes` the same
#: way `MUTATING` gates `computer` actions (see `ComputerTool.__init__`).
#: `get_clipboard` is a read (already covered by `_READ_ONLY_BLOCKED` above for
#: its exfiltration risk, not because it changes anything).
MUTATING_DESKTOP = {"focus_window", "set_clipboard"}

#: M3 action-surface computation (docs/designs/capability-awareness.md \u00a75.3):
#: every `computer`/`desktop` action mapped to the ONE underlying `Backend`
#: call (and therefore the one remote wire op - `remote_agent.py`'s
#: `_HANDLERS` keys are the SAME names, see `remote_backend.py`'s
#: `RemoteBackend` methods) it actually depends on. `None` means the action
#: never reaches `Backend` at all (`wait` is a bare `time.sleep`) - such an
#: action is never "not carried by this binding", remote or local.
#: `select_monitor` depends on the same enumeration `list_monitors` does
#: (`ComputerTool.select_monitor` reads `self._monitors`, refreshed via the
#: same backend call - see that method).
_ACTION_WIRE_OP: dict[str, str | None] = {
    "screenshot": "capture_scaled",
    "zoom": "capture_scaled",
    "cursor_position": "cursor_position",
    "mouse_move": "move",
    "left_click": "click",
    "right_click": "click",
    "middle_click": "click",
    "double_click": "click",
    "triple_click": "click",
    "left_mouse_down": "mouse_down",
    "left_mouse_up": "mouse_up",
    "left_click_drag": "drag",
    "scroll": "scroll",
    "key": "key",
    "hold_key": "hold_key",
    "type": "type_text",
    "wait": None,
    "screen_info": "screen_geometry",
    "list_windows": "list_windows",
    "focus_window": "focus_window",
    "get_clipboard": "get_clipboard",
    "set_clipboard": "set_clipboard",
    "list_monitors": "list_monitors",
    "select_monitor": "list_monitors",
}

#: Actions that synthesize input (click/type/key/scroll/focus) and therefore
#: depend on macOS Accessibility being granted - the exact set `MUTATING`
#: already names for the read_only gate (\u00a73.1: "Accessibility granted?" ->
#: `AXIsProcessTrusted()`), reused rather than re-declared so the two lists
#: cannot drift apart.
_ACCESSIBILITY_GATED_ACTIONS = MUTATING

#: Actions that read pixels and therefore depend on macOS Screen Recording
#: being granted (\u00a73.1: `CGPreflightScreenCaptureAccess()`).
_SCREEN_RECORDING_GATED_ACTIONS = {"screenshot", "zoom"}


class DesktopTool:
    """Window and clipboard helpers that the native `computer` tool cannot express.

    Once `computer` is promoted to Anthropic's server-side tool type, the model only
    knows that tool's fixed action list - so window management and clipboard access
    have to live somewhere else. This is that somewhere.
    """

    def __init__(self, computer: ComputerTool) -> None:
        self._computer = computer

    @property
    def name(self) -> str:
        return "desktop"

    @property
    def description(self) -> str:
        # `desktop` is an ordinary tool (has its own input_schema) - unlike
        # `computer`, it is never replaced by a provider's native server-side
        # tool block, so this text is one of the few computer-use surfaces
        # that reliably reaches the model on every dialect. That makes it the
        # right place for facts that must not be lost to schema-stripping:
        # the config.target shape fact (`_TARGET_MODEL`, shared verbatim with
        # `registry._REMEDIATION`'s failure-path text - see that module) and
        # the keystroke-interleaving safety note (moved here from the
        # always-loaded awareness context to free its token budget; see
        # `context/computer-use-awareness.md`).
        return (
            "Desktop helpers that complement the `computer` tool: list open windows, "
            "bring a window to the front before typing into it, read the display geometry, "
            "read or write the clipboard, and list/switch which physical monitor "
            "`computer` screenshots and clicks are scoped to. Use `list_windows` then "
            "`focus_window` to make sure keystrokes land in the right application. "
            "Clipboard access is the reliable way to pull exact text out of an app "
            "(select, copy, then get_clipboard). On a multi-monitor desktop, use "
            "`list_monitors` to see what's available and `select_monitor` to target one - "
            "`computer` is scoped to a single monitor by default so screenshots stay legible. "
            "This machine may be in use by a human at the same time as you: a window's "
            "focus or clipboard contents can change from something other than your own "
            "actions between calls, so re-check with `list_windows`/`get_clipboard` rather "
            "than assuming the state you last set still holds. Your keystrokes and theirs "
            "can interleave rather than queue: a command you believe you typed verbatim may "
            "land with extra or missing characters, so if a result looks off, verify what "
            "actually landed before assuming your own input was wrong. Which machine this "
            f"session controls is fixed for the whole session, not chosen per call: {_TARGET_MODEL}"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": DESKTOP_ACTIONS},
                "handle": {
                    "type": "string",
                    "description": "Window handle from list_windows (focus_window).",
                },
                "text": {
                    "type": "string",
                    "description": "Text to place on the clipboard (set_clipboard).",
                },
                "monitor": {
                    "type": "string",
                    "description": (
                        "Monitor id from list_monitors, or 'primary' / "
                        "'virtual-desktop' (select_monitor)."
                    ),
                },
                "target": {
                    "type": "string",
                    "description": (
                        "New machine for this session to drive (retarget) - "
                        "'ssh://user@host[:port]' for a remote machine, or "
                        "'local' to switch back to this machine's own local "
                        "backend. Takes no other arguments: policy "
                        "(read_only/gate_writes/clipboard_read_policy) is "
                        "always recomputed from this session's own config, "
                        "never widened by a retarget call."
                    ),
                },
            },
            "required": ["action"],
        }

    async def execute(self, input: dict[str, Any]) -> ToolResult:
        # M3 disclosure-gate exemption (docs/designs/capability-awareness.md
        # \u00a75.4) - dispatched BEFORE `_ensure_announced`, deliberately, and
        # this is the one place in the whole module that is. `doctor` is the
        # orienting call an agent makes to find out what it can do here,
        # including whether a dialog is about to appear - gating it behind
        # the same disclosure check every other action goes through would
        # mean the FIRST call ever made against a fresh macOS binding pops a
        # 30s modal on someone's Mac before the agent could say a word about
        # it, and an agent cannot warn a human about a dialog using the tool
        # that fires it. That chicken-and-egg is real, not hypothetical - see
        # \u00a78 of the design doc's end-to-end walkthrough (step 1: `doctor` on
        # a fresh binding, before anything else has happened).
        #
        # This is a SECOND door past the one gate `_ensure_announced`'s own
        # docstring insists on ("exactly one gate, not one gate for writes
        # and a silent hole for reads") - that argument's stated reason is
        # CONTENT ("a screenshot IS a capture of a human's screen"), and the
        # exemption earns its keep only by removing exactly that: `doctor`
        # reports about the machine, never anything on it. See
        # `_build_doctor_report`'s docstring for the hard boundary this
        # promise depends on, and `test_doctor_cannot_return_screen_contents`
        # for the structural proof, not just a comment asserting it.
        #
        # Also unlike the OTHER path that skips this gate - the mount-failure
        # stub (`ComputerUseUnavailableTool`, only registered when mount()
        # could not get a working backend at all) - `doctor` lives on the tool
        # that DID mount. It answers "what can I do on the machine I am bound
        # to", which only has an answer once there is a machine.
        if str(input.get("action") or "").strip() == "doctor":
            return _build_doctor_report(self._computer)
        # M4 disclosure-gate exemption (docs/designs/capability-awareness.md
        # \u00a76.1): `retarget` is dispatched BEFORE `_ensure_announced` for the
        # SAME reason `doctor` is, above - this call's whole job is to
        # establish disclosure for a NEW target (`ComputerTool.retarget`'s
        # own \u00a76.2 steps 3-4), and gating it behind the CURRENT/OLD target's
        # disclosure would show a possibly-irrelevant dialog for a machine
        # the caller is about to leave, and would violate the "warn before
        # the dialog" ordering (\u00a78 step 5 vs step 6 of the design doc's
        # walkthrough) for the identical reason `doctor` cannot go through
        # this gate either. `retarget` runs on a background thread (C4:
        # connecting can block for seconds) and enforces its OWN full
        # sequence, including its own disclosure, internally.
        if str(input.get("action") or "").strip() == "retarget":
            return await asyncio.to_thread(self._computer.retarget, input.get("target"))
        # Same gate `computer` runs first (see `ComputerTool._ensure_announced`
        # and `ComputerTool.execute`'s own docstring) - `desktop` shares the
        # SAME `ComputerTool` instance, so this reuses (and, if this is the
        # first action of either tool this session, actually fires) the exact
        # same disclosure decision. A pure read like `get_clipboard` never
        # reaches `ComputerTool._run()` (it calls `backend.get_clipboard()`
        # directly, below) - so this explicit call, not `_run()` alone, is
        # what makes the gate cover every `desktop` action too, not just the
        # ones that happen to share `_run()` with `computer`.
        try:
            await asyncio.to_thread(self._computer._ensure_announced)
        except AnnouncementRefused as exc:
            return ToolResult(
                success=False,
                error={"message": str(exc), "type": "AnnouncementRefused"},
            )
        # Band lifetime (docs/designs/band-lifetime.md, Alt A): `desktop`
        # shares `computer`'s ComputerTool instance (and therefore its
        # channel), so this participates in the SAME depth counter - a
        # `desktop` action keeps the band up exactly like a `computer`
        # action would, and is what closes F9 (desktop actions bypassing
        # the counter).
        try:
            band_token = await asyncio.to_thread(self._computer._band_enter)
        except AnnouncementRefused as exc:
            return ToolResult(
                success=False,
                error={"message": str(exc), "type": "AnnouncementRefused"},
            )
        try:
            return await self._execute_action(input)
        finally:
            await asyncio.to_thread(self._computer._band_exit, band_token)

    async def _execute_action(self, input: dict[str, Any]) -> ToolResult:
        """The actual per-action dispatch, unchanged from before band-
        lifetime tracking existed - split out of `execute()` only so that
        method's own `try/finally` around `_band_enter`/`_band_exit` does
        not have to re-indent this whole body."""
        action = str(input.get("action") or "").strip()
        if action not in DESKTOP_ACTIONS:
            return ToolResult(
                success=False,
                error={
                    "message": f"unknown action {action!r}; expected one of {', '.join(DESKTOP_ACTIONS)}"
                },
            )
        binding = self._computer._binding
        if binding.read_only and action in _READ_ONLY_BLOCKED:
            return ToolResult(
                success=False,
                error={"message": f"action {action!r} blocked: mounted read_only"},
            )
        backend = binding.backend
        try:
            # C4, same reasoning as ComputerTool.execute: keep the sync Backend
            # protocol, move the (possibly-remote) blocking call off the event
            # loop at this boundary instead.
            if action == "get_clipboard":
                # §3 hardening: `clipboard_read_policy` (ComputerTool.__init__)
                # is an explicit, named, always-audited gate distinct from
                # `read_only` - see that attribute's docstring for the full
                # rationale and default rules.
                policy = binding.clipboard_read_policy
                if policy == "block":
                    return ToolResult(
                        success=False,
                        error={
                            "message": "action 'get_clipboard' blocked by "
                            "clipboard_read_policy=block"
                        },
                    )
                output = await asyncio.to_thread(backend.get_clipboard)
                digest = _text_digest(output)
                logger.info(
                    "computer-use audit: op=get_clipboard policy=%s chars=%d sha256=%s",
                    policy,
                    len(output),
                    digest,
                )
                if policy == "redact":
                    return ToolResult(
                        success=True,
                        output=(
                            f"<clipboard content redacted by policy "
                            f"(clipboard_read_policy=redact): {len(output)} chars, "
                            f"sha256={digest}>"
                        ),
                    )
                return ToolResult(success=True, output=output)
            if action == "set_clipboard":
                body = str(input.get("text") or "")
                # Same guard discipline every other mutating action in
                # `ComputerTool._run` now gets (§2) - `set_clipboard` mutates
                # target state but is dispatched here, not through `_run`,
                # so it needs its own explicit wiring rather than inheriting
                # `_guard_write` for free.
                with self._computer._guard_write(binding=binding):
                    await asyncio.to_thread(backend.set_clipboard, body)
                # §3 audit hardening: digest, never plaintext - the same
                # discipline `type` uses, since clipboard content is exactly
                # the kind of thing that "frequently includes credentials."
                logger.info(
                    "computer-use audit: op=set_clipboard chars=%d sha256=%s",
                    len(body),
                    _text_digest(body),
                )
                return ToolResult(success=True, output="clipboard set")
            if action == "list_monitors":
                monitors = await asyncio.to_thread(self._computer.list_monitors)
                current = self._computer.current_monitor
                lines = [
                    f"  [{m.id}] {m.width}x{m.height} at ({m.x},{m.y})"
                    f"{' (primary)' if m.primary else ''}"
                    f"{' [ACTIVE]' if current is not None and current.id == m.id else ''}"
                    for m in monitors
                ]
                mode = "virtual-desktop" if current is None else f"monitor {current.id}"
                return ToolResult(
                    success=True,
                    output=f"target mode: {mode}\nmonitors:\n" + "\n".join(lines),
                )
            if action == "select_monitor":
                target = str(input.get("monitor") or "").strip()
                if not target:
                    raise ValueError("action 'select_monitor' requires 'monitor'")
                disp = await asyncio.to_thread(self._computer.select_monitor, target)
                return ToolResult(
                    success=True,
                    output=(
                        f"target monitor set to {target!r}: screen "
                        f"{disp.screen_width}x{disp.screen_height} at "
                        f"({disp.origin_x},{disp.origin_y}) -> model "
                        f"{disp.model_width}x{disp.model_height}"
                    ),
                )
            if (
                binding.gate_writes
                and action in MUTATING_DESKTOP
                and not self._computer._unattended_writes_ok
                and not self._computer._interactive_write_approved
            ):
                # Fail-safe of last resort: reached only when NOTHING already
                # granted this write - no gate hook synced
                # `unattended_writes_ok=True` (the explicit unattended
                # config opt-in), no gate hook synced
                # `interactive_write_approved=True` (a human granting THIS
                # call via `ask_user`), and `gate_writes` was not explicitly
                # turned off. Every remedy is named here, not just pointed
                # at - a doc pointer alone has already shipped as an
                # unreachable-in-the-moment reference three times in this
                # repo.
                return ToolResult(
                    success=False,
                    error={
                        "message": (
                            f"action {action!r} requires confirmation "
                            "(gate_writes) and nothing granted it: no gate "
                            "hook is registered (or none has run yet), "
                            "'unattended_writes_ok' is not set, and "
                            "'gate_writes' was not explicitly disabled. The "
                            "write was NOT sent. To proceed, do ONE of: (1) "
                            "mount hook-computer-use so its 'tool:pre' gate "
                            "hook can grant approval (interactively via "
                            "'ask_user', or unattended - see (2)); (2) set "
                            "hook-computer-use config "
                            "'unattended_writes_ok: true' to allow writes on "
                            "this target with no human confirmation - a "
                            "deliberate, logged opt-in, never a default; or "
                            "(3) set tool-computer-use config "
                            "'gate_writes: false' to disable the gate "
                            "entirely for this target - a deliberate, "
                            "logged opt-out. See "
                            "docs/designs/remote-transport.md \u00a710.4 for "
                            "the policy rationale."
                        )
                    },
                )
            summary, _ = await asyncio.to_thread(
                self._computer._run, action, input, binding=binding
            )
            return ToolResult(success=True, output=summary)
        except (BackendError, ValueError) as exc:
            return ToolResult(
                success=False, error={"message": str(exc), "type": type(exc).__name__}
            )
        except Exception as exc:
            logger.exception("desktop action %s failed", action)
            return ToolResult(
                success=False, error={"message": str(exc), "type": type(exc).__name__}
            )


def _display_server_report(backend: Backend) -> dict[str, Any] | None:
    """Best-effort, side-effect-free report of which display server this
    binding is actually running under (docs/designs/capability-awareness.md
    \u00a72.3c/\u00a73.3): `LinuxX11Backend.probe()` checks `DISPLAY`/`XAUTHORITY`/
    session-match only, and ACTIVELY SUCCEEDS on a Wayland session - XWayland's
    own auth file is one of the candidates `linux_x11._resolve_xauthority`
    tries. Ubuntu 24.04 defaults to Wayland, so this is not a hypothetical
    edge case.

    Never claims a confirmation the probe never made: `verified=True` only
    for a session that positively reports `XDG_SESSION_TYPE=x11`; a detected
    Wayland/XWayland session, or no signal at all, is reported
    `verified=False` with an honest reason - never silently promoted to
    \"x11 (works)\".

    `None` when this binding is not local Linux X11 (macOS/Windows have no
    such ambiguity) or is a REMOTE target (this reads the CONTROLLER's own
    environment variables, which say nothing about a target's session type;
    reporting them for a remote binding would be exactly the kind of guess
    this whole feature exists to refuse).
    """
    if backend.name != "linux-x11" or bool(getattr(backend, "is_remote", False)):
        return None
    session_type = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
    wayland_display = os.environ.get("WAYLAND_DISPLAY")
    if session_type == "wayland" or wayland_display:
        return {
            "value": "wayland-or-xwayland",
            "verified": False,
            "note": (
                f"this session reports Wayland (XDG_SESSION_TYPE={session_type!r}, "
                f"WAYLAND_DISPLAY={wayland_display!r}) - the X11 probe that "
                "mounted this backend does not check for Wayland and can "
                "succeed via XWayland's own auth file "
                "(docs/designs/capability-awareness.md \u00a72.3c). Input/capture "
                "behavior under XWayland is UNVERIFIED by this codebase; do "
                "not assume it matches native X11."
            ),
        }
    if session_type == "x11":
        return {
            "value": "x11",
            "verified": True,
            "note": "XDG_SESSION_TYPE=x11 - a native X11 session, not XWayland.",
        }
    return {
        "value": "unknown",
        "verified": False,
        "note": (
            "XDG_SESSION_TYPE is not set (or unrecognised) - cannot determine "
            "whether this is native X11 or XWayland; the probe that mounted "
            "this backend does not check either way, so this is reported as "
            "unverified rather than assumed to be plain X11."
        ),
    }


def _macos_permission_state(backend: Backend) -> dict[str, Any]:
    """Tri-state (\u00a73.4) permission + session-lock facts for a macOS binding -
    the ONLY platform with a permission model at all (\u00a73.2/\u00a73.3: Windows and
    Linux have none). Never collapses \"could not determine\" into \"denied\" -
    a missing/unknown fact is reported as `\"unknown\"`, exactly like the
    connect-time handshake probe already does
    (`remote_agent._probe_permissions`) and for the same reason.

    LOCAL macOS: probed LIVE, prompt-free - the same ctypes calls
    `remote_agent._probe_permissions` uses on the target side.
    REMOTE macOS: read from the connect-time handshake (M1) - never
    re-probed live (no wire op for it exists in this build); the
    handshake's own age is surfaced alongside so this is never mistaken for
    a fresh read.
    Anything else (Windows, Linux, remote windows/linux): `applicable: False`
    - there is no TCC-style gate on those platforms at all.
    """
    is_remote = bool(getattr(backend, "is_remote", False))
    platform_name = (
        getattr(backend, "presence_platform", None) if is_remote else backend.name
    )
    if platform_name != "macos":
        return {
            "applicable": False,
            "note": f"{platform_name or backend.name!r} has no TCC-style permission model",
        }

    def _tri(value: bool | None) -> str:
        if value is None:
            return "unknown"
        return "granted" if value else "denied"

    if is_remote:
        handshake = getattr(backend, "handshake", None) or {}
        permissions = handshake.get("permissions") or {}
        return {
            "applicable": True,
            "accessibility": _tri(permissions.get("accessibility")),
            "screen_recording": _tri(permissions.get("screen_recording")),
            "session_state": "unknown",
            "source": "connect-time handshake snapshot (M1)",
            "snapshot_age_seconds": getattr(backend, "handshake_age_seconds", None),
            "note": (
                "session lock state is not queried for a remote macOS target "
                "in this build (no wire op exists for it yet) - a locked "
                "screen and a missing grant are indistinguishable without it."
            ),
        }
    accessibility = "unknown"
    screen_recording = "unknown"
    session_state = "unknown"
    try:
        from . import macos as _macos  # local import: only importable on Darwin

        accessibility = _tri(_macos._ax_is_process_trusted())
        screen_recording = _tri(_macos._cg_preflight_screen_capture_access())
        session_state, _detail = _macos._macos_session_state()
    except Exception:  # noqa: BLE001 - best-effort diagnostic, never fatal
        logger.debug("doctor: macOS permission probe failed", exc_info=True)
    return {
        "applicable": True,
        "accessibility": accessibility,
        "screen_recording": screen_recording,
        "session_state": session_state,
        "source": "live probe (prompt-free)",
        "note": (
            "which PROCESS must hold these grants cannot be determined from "
            "here - TCC binds to the responsible process, which differs by "
            "launch chain (docs/designs/capability-awareness.md \u00a73.1)."
        ),
    }


def _action_surface(computer: ComputerTool) -> dict[str, Any]:
    """Compute the works / blocked-by-policy / blocked-by-os-permission /
    not-carried-by-binding split for every `computer` + `desktop` action
    (docs/designs/capability-awareness.md \u00a75.3, plus the council's 4th
    bucket). Exactly one bucket per action, in this precedence order:

      1. not carried by this binding at all
      2. blocked by a CONFIRMED macOS permission denial - \"dispatched, not
         policy-blocked, but the OS will silently refuse it\". This is the
         council's finding: a naive report could say `drag: works` while
         Accessibility is denied, which is the same confident-wrong-answer
         failure this whole design exists to close, one layer down.
      3. blocked by this session's own policy (read_only / gate_writes)
      4. works

    \"Unknown\" permission state (\u00a73.4: never collapsed into \"denied\") does
    NOT move an action into bucket 2 - it stays in `permissions` (tri-state),
    so a user is told to go look, never told a false negative.
    """
    backend = computer._backend
    is_remote = computer._is_remote
    ops: set[str] | None = None
    if is_remote:
        handshake = getattr(backend, "handshake", None) or {}
        raw_ops = handshake.get("ops")
        if isinstance(raw_ops, list):
            ops = set(raw_ops)

    permissions = _macos_permission_state(backend)

    def _permission_denied(action: str) -> str | None:
        if not permissions.get("applicable"):
            return None
        if (
            action in _SCREEN_RECORDING_GATED_ACTIONS
            and permissions.get("screen_recording") == "denied"
        ):
            return "screen_recording"
        if (
            action in _ACCESSIBILITY_GATED_ACTIONS
            and permissions.get("accessibility") == "denied"
        ):
            return "accessibility"
        return None

    def _policy_reason(action: str) -> str | None:
        if computer._read_only and (action in MUTATING or action in _READ_ONLY_BLOCKED):
            return "blocked: mounted read_only"
        if (
            action in MUTATING_DESKTOP
            and computer._gate_writes
            and not computer._unattended_writes_ok
            and not computer._interactive_write_approved
        ):
            return (
                "blocked: gate_writes is on and nothing has approved this "
                "write yet (no gate hook, no unattended_writes_ok, no "
                "interactive approval this call)"
            )
        return None

    works: list[str] = []
    blocked_by_policy: dict[str, str] = {}
    blocked_by_os_permission: dict[str, str] = {}
    not_carried: dict[str, str] = {}

    for action in sorted(_ACTION_WIRE_OP):
        op = _ACTION_WIRE_OP[action]
        if op is not None and is_remote:
            if ops is None:
                not_carried[action] = (
                    "unknown: this remote agent's handshake did not report "
                    "its dispatch table (ops) - cannot assert this action is "
                    "carried, so it is not assumed to be"
                )
                continue
            if op not in ops:
                not_carried[action] = (
                    f"not carried: this binding's agent has no {op!r} handler"
                )
                continue
        denied_gate = _permission_denied(action)
        if denied_gate is not None:
            blocked_by_os_permission[action] = (
                f"dispatched, but macOS has DENIED {denied_gate!r} to this "
                "process - the action would reach the OS and be silently "
                'refused, not "work" (see this report\'s own permissions '
                "section for the exact pane to grant it in)"
            )
            continue
        policy_reason = _policy_reason(action)
        if policy_reason is not None:
            blocked_by_policy[action] = policy_reason
            continue
        works.append(action)

    return {
        "works": works,
        "blocked_by_policy": blocked_by_policy,
        "blocked_by_os_permission": blocked_by_os_permission,
        "not_carried_by_binding": not_carried,
    }


def _config_source(cfg: dict[str, Any], key: str, *, is_remote: bool) -> str:
    """Whether an effective policy value came from explicit config or from
    this module's own local/remote default (\u00a75.3: \"a user asking why can't
    you click needs to know whether to change a setting or a machine\")."""
    if cfg.get(key) is not None:
        return "config"
    return "remote-default" if is_remote else "local-default"


def _safety_state(computer: ComputerTool) -> dict[str, Any]:
    guard = computer._coexistence_guard
    if guard is None:
        return {
            "guard": None,
            # `guard: null` is structurally UNREACHABLE for the three
            # backends this bundle ships today (Linux X11, macOS, Windows,
            # and RemoteBackend forwarding to whichever of those the target
            # runs) - `_build_coexistence_guard` builds one unconditionally
            # whenever `presence_idle_ms()` exists AND resolves to a known
            # `GUARD_MS` platform, which is true for all four. This is a
            # forward-looking fail-safe for a hypothetical future backend
            # with no proven presence-detector wiring, not a live alarm
            # about the machine this report was just generated for - see
            # `_build_coexistence_guard`'s own docstring for exactly which
            # two conditions leave a backend with no guard.
            "note": (
                "no coexistence guard for this backend - no halt protection "
                "and no disclosure channel. See _build_coexistence_guard's "
                "docstring for when this can happen (not reachable for any "
                "backend this bundle ships today)."
            ),
        }
    out: dict[str, Any] = {
        "guard": guard.as_dict(),
        "guard_ms": guard.presence.guard_ms,
        "guard_measured": guard.presence.guard_measured,
    }
    if guard.halted:
        out["resume_command"] = resolve_resume_command()
    return out


def _bound_target(computer: ComputerTool) -> dict[str, Any]:
    backend = computer._backend
    is_remote = computer._is_remote
    out: dict[str, Any] = {"name": backend.name, "is_remote": is_remote}
    if is_remote:
        out["user_host"] = getattr(backend, "user_host", None)
        out["presence_platform"] = getattr(backend, "presence_platform", None)
        out["handshake_age_seconds"] = getattr(backend, "handshake_age_seconds", None)
        out["note"] = (
            "handshake-derived facts in this report (action carriage, "
            "permissions) are a CONNECT-TIME SNAPSHOT, not live - see "
            "handshake_age_seconds. Permissions can be revoked and the "
            "screen can be locked mid-session, long after this snapshot."
        )
    return out


def _target_mode(computer: ComputerTool) -> dict[str, Any]:
    """Current monitor targeting + the config.target shape fact, reused
    VERBATIM from `registry._TARGET_MODEL` (\u00a75.3: \"never paraphrased, so it
    cannot drift from the two places that already share it\").

    Calls `list_monitors()` for a fresh count - geometry only, explicitly
    permitted by the \u00a75.4 content boundary (hardware, not content).
    """
    current = computer.current_monitor
    try:
        monitor_count: int | str = len(computer.list_monitors())
    except BackendError as exc:
        monitor_count = f"unavailable: {exc}"
    return {
        "mode": "virtual-desktop" if current is None else f"monitor:{current.id}",
        "monitor_count": monitor_count,
        "target_shape": _TARGET_MODEL,
    }


def _build_doctor_report(computer: ComputerTool) -> ToolResult:
    """`desktop(action=\"doctor\")` (docs/designs/capability-awareness.md \u00a75) -
    the capability report. Read-only and side-effect-free beyond one
    monitor-geometry enumeration (\u00a75.4 explicitly permits that: hardware,
    not content) and, on local macOS, two prompt-free TCC preflight calls
    plus one `ioreg` shell-out for lock state - all already proven side-
    effect-free (\u00a73.1 / `remote_agent._probe_permissions`).

    HARD BOUNDARY (\u00a75.4, and this is what earns the disclosure-gate
    exemption in `DesktopTool.execute`): this function reads cached
    handshake/config/guard state and calls `list_monitors` (geometry only,
    justified above) - it must NEVER call `capture`, `capture_scaled`,
    `cursor_position`, `list_windows`, or `get_clipboard`.
    `test_doctor_cannot_return_screen_contents` enforces this at runtime by
    making every one of those methods raise if called - not just a comment
    asserting it.
    """
    report: dict[str, Any] = {
        "bound_target": _bound_target(computer),
        "action_surface": _action_surface(computer),
        "permissions": _macos_permission_state(computer._backend),
        "display_server": _display_server_report(computer._backend),
        "effective_policy": {
            "read_only": {
                "value": computer._read_only,
                "source": _config_source(
                    computer._cfg, "read_only", is_remote=computer._is_remote
                ),
            },
            "gate_writes": {
                "value": computer._gate_writes,
                "source": _config_source(
                    computer._cfg, "gate_writes", is_remote=computer._is_remote
                ),
            },
            "clipboard_read_policy": {
                "value": computer._clipboard_read_policy,
                "source": _config_source(
                    computer._cfg,
                    "clipboard_read_policy",
                    is_remote=computer._is_remote,
                ),
            },
        },
        "safety_state": _safety_state(computer),
        "target_mode": _target_mode(computer),
    }
    return ToolResult(success=True, output=json.dumps(report, indent=2, default=str))


def _build_coexistence_guard(
    backend: Backend, cfg: dict[str, Any]
) -> CoexistenceGuard | None:
    """Build the `CoexistenceGuard` for `backend`, or `None` if this backend
    has no proven presence-detector wiring yet (`docs/designs/coexistence.md`).

    Deliberately conservative: a guard is only ever constructed for a backend
    that exposes `presence_idle_ms()` (today: `LinuxX11Backend`, `MacOSBackend`
    since `presence.GUARD_MS["macos"]` was measured by O4, `WindowsBackend`
    (via `bridge.ps1`'s `presence_idle` action), and `RemoteBackend` (forwards
    the read to the SAME method on the target's own backend, \u00a75 of
    `docs/designs/remote-transport.md`) - see each method's docstring). A
    backend with no such method gets no coexistence layer at all, rather than
    one built on a guessed/unmeasured `GUARD` band - the same "do not claim a
    guarantee you do not have" principle \u00a75.5 applies to Windows `type_text`.

    `cfg["coexistence"]` (all keys optional):
      - `enabled`: **no longer controls whether this guard is built.**
        Defect fix (see `docs/designs/coexistence.md` \u00a76.0/C1 item 6): this
        key used to make `enabled: False` return `None` here, which silently
        removed the halt invariant, pause, target binding, and geometric
        exclusion in one boolean, at `logger.info`, with no consumer
        anywhere ever noticing. That directly contradicted \u00a76.0 ("no
        configuration key disables [the halt]") and made C1's acceptance
        item 6 false. Whenever a backend structurally supports presence
        detection (the two checks below), this guard is now built
        unconditionally - no config key of any kind can prevent that.
        `enabled` is not deleted: it keeps a real, narrower effect - see
        `_disclosure_decline_reason` - as an alias of `announce` for
        declining session-start *disclosure only*, gated by a mount-time
        presence check (`_refuse_if_disclosure_declined_with_human_present`).
      - `drive_anyway` (default `False`, \u00a77.6/D5): permits *beginning* to
        drive when a human is already detected present at guard-construction
        time. Logged when it fires. Never affects the halt invariant.
    """
    coexistence_cfg = dict(cfg.get("coexistence") or {})
    idle_source = getattr(backend, "presence_idle_ms", None)
    if idle_source is None:
        return None
    # Coverage-gap fix: `backend.name` is the right key for LOCAL backends
    # ("linux-x11", "macos", "windows-wsl2" - each IS its own GUARD_MS
    # platform), but for `RemoteBackend` it is a COMPOSITE identifier
    # ("remote-ssh:windows-wsl2", unique per remote target - see that
    # class's own docstring for why it stays that way for logs/halt-state
    # keys) that is deliberately never a `GUARD_MS` key. `presence_platform`
    # (set only on `RemoteBackend`, from its own handshake) resolves to the
    # REMOTE machine's actual measured platform band instead - network
    # latency never gets folded into the guard band; the underlying
    # platform's own measured GUARD_MS is used unchanged, exactly as if
    # driving that platform locally.
    platform_for_guard = getattr(backend, "presence_platform", None) or backend.name
    if platform_for_guard not in GUARD_MS:
        logger.warning(
            "coexistence: backend %r exposes presence_idle_ms() but resolves "
            "to platform %r, which has no GUARD_MS entry; not building a guard",
            backend.name,
            platform_for_guard,
        )
        return None
    presence = PresenceMonitor(idle_source=idle_source, platform=platform_for_guard)
    # Channel-scoped, not one-per-mount() (see `_get_channel_ledger` above) -
    # this is finding #2 of the adversarial review of
    # docs/designs/band-lifetime.md: `HeldInputLedger()` used to be built
    # fresh here on every call, so a parent session's mount() and a
    # delegated child's mount() sharing the same overlay each got their OWN,
    # disconnected ledger.
    channel_key = _channel_identity(backend)
    ledger = _get_channel_ledger(channel_key)
    target_source = getattr(backend, "current_target", None)
    guard = CoexistenceGuard(
        presence=presence,
        release_all=lambda reason: ledger.release_all(reason=reason),
        drive_anyway=bool(coexistence_cfg.get("drive_anyway", False)),
        target_source=target_source,
        # Defect 2 fix: consult the durable halt record on every
        # before_event(), not just once here at mount time - closes the
        # window where a DIFFERENT session (same backend) detects a human
        # and persists it AFTER this guard already mounted (see
        # `halt_state.make_durable_halt_poll`'s docstring for the real
        # evaluation evidence).
        #
        # Bug-hunt defect A fix: keyed by `_halt_key(backend)`, not
        # `backend.name` - see that function's docstring for why the bare
        # composite name is not unique per remote host. A legacy
        # pre-migration record (written under the old, collision-prone key
        # by a version of this code before the fix) is also polled, fail-
        # safe, so it does not silently stop applying the moment this
        # ships - see `_legacy_halt_key`'s docstring.
        durable_halt_poll=_durable_halt_poll_for(backend),
        on_presence_sample=(
            _remote_transport_warning_observer(channel_key, backend.name)
            if bool(getattr(backend, "is_remote", False))
            else None
        ),
    )
    logger.info(
        "coexistence: guard built for backend %r (guard_ms=%.1f, measured=%s)",
        backend.name,
        presence.guard_ms,
        presence.guard_measured,
    )
    # Defect 2 fix: a brand-new guard has no memory of a human detected in a
    # PRIOR session against this same backend - `_halted` is a plain
    # in-memory field on an object that stops existing when its mount does
    # (see `halt_state.py` module docstring for the real evaluation evidence
    # this closes: a halted sub-agent session ended, its parent session
    # mounted its OWN fresh guard, and writes resumed automatically ~80s
    # later with no human ever choosing to resume anything). If a durable
    # halt record exists for this backend, seed this guard already-halted -
    # the ONLY way past it is an explicit human action (`resolve_resume_command()`
    # below - resolved against THIS process, never a bare name assumed to be
    # on PATH), never the mere passage of time.
    #
    # Bug-hunt defect A fix: `_halt_key(backend)`, not `backend.name` - see
    # that function's docstring. Falls back to the pre-fix, platform-wide
    # `_legacy_halt_key` ONLY when the per-host key has no record, so an
    # existing halt written before this fix keeps applying (fail-safe) even
    # though it may have originated on a DIFFERENT host of this platform -
    # logged distinctly so that ambiguity is never silent.
    halt_key = _halt_key(backend)
    persisted = load_halt(halt_key)
    legacy_key = _legacy_halt_key(backend)
    used_legacy_key = False
    if persisted is None and legacy_key is not None:
        persisted = load_halt(legacy_key)
        used_legacy_key = persisted is not None
    if persisted is not None:
        guard.seed_halted(persisted.to_snapshot())
        if used_legacy_key:
            logger.warning(
                "coexistence: backend %r has no per-host durable halt "
                "record (key=%r) but a pre-migration, platform-wide one "
                "exists (key=%r, reason=%r) - honoring it fail-safe since "
                "it may have been recorded against a DIFFERENT host of "
                "this same platform; this session starts already HALTED; "
                "run %s to clear it explicitly, and consider clearing the "
                "legacy key too (docs/designs/coexistence.md \u00a713 D3)",
                backend.name,
                halt_key,
                legacy_key,
                persisted.reason,
                resolve_resume_command(),
            )
        else:
            logger.warning(
                "coexistence: backend %r has a durable halt record from a prior "
                "session (reason=%r) - this session starts already HALTED; run "
                "%s to clear it explicitly (docs/designs/coexistence.md \u00a713 D3)",
                backend.name,
                persisted.reason,
                resolve_resume_command(),
            )
    return guard


class AnnouncementRefused(RuntimeError):
    """The session-start announcement gate refused to let this session begin
    driving (`docs/designs/coexistence.md` \u00a77.3/\u00a77.6).

    Distinct from `NoBackendAvailable`: the backend itself is fine (the
    display, the injector, the guard all work) - what refused is the
    disclosure gate. Caught by `mount()` exactly like `NoBackendAvailable`:
    logged plainly, nothing mounted, no traceback.
    """


def _channel_failure_snapshot(guard: CoexistenceGuard) -> PresenceSnapshot:
    """A live presence read taken at the moment a disclosure channel failed
    to display - safe to call here because every caller of `_build_announcement`
    (`ComputerTool._ensure_announced`) holds that instance's own
    `_announce_lock` for the whole call, so there is at most one thread per
    `ComputerTool` inside `_build_announcement`/`_dispatch_announcement` at a
    time, and that thread is the only one touching this `guard` this early -
    every other action on the same instance blocks on `_ensure_announced`
    before it can reach anything that mutates `guard.presence` (unlike the
    overlay's own click callbacks - see `_on_overlay_cancel` below, which
    deliberately does NOT call this)."""
    return guard.presence.sample()


def _handle_channel_failure(
    guard: CoexistenceGuard, backend_name: str, channel: str, exc: Exception
) -> None:
    """\u00a77.6's rule, applied uniformly to every disclosure channel (Linux
    overlay, Windows overlay, macOS dialog): a human detected present, with
    no working way to tell them an agent is about to drive this machine, is
    refused outright. Nobody detected present -> proceed, but LOUDLY (an
    `logger.error`, not a swallowed exception) - this is the \"loud, not
    silent\" requirement: a channel that failed to display must never look,
    from the caller's side, identical to a channel that was never attempted.
    """
    try:
        snap = _channel_failure_snapshot(guard)
        human_present = snap.state is PresenceState.HUMAN_ACTIVE
    except IdleUnreadableError:
        # \u00a79.6: an unreadable idle counter is a hard error, never guessed as
        # "quiet" - the same fail-safe direction applies here: treat it as if
        # a human were detected.
        human_present = True
    if human_present:
        raise AnnouncementRefused(
            f"{channel} failed to display on backend {backend_name!r} ({exc}) "
            "and a human is currently detected at this machine - refusing to "
            "begin driving with no working disclosure channel "
            "(docs/designs/coexistence.md \u00a77.6). Not overridable by default; "
            "see that section for the explicit, logged per-target opt-out."
        ) from exc
    logger.error(
        "coexistence: %s failed to display on backend %r (%s) - no human is "
        "currently detected, so this session proceeds WITHOUT a disclosure "
        "channel. This is loud, not silent: if a human sits down mid-session "
        "there is nothing to warn them beyond the halt invariant itself.",
        channel,
        backend_name,
        exc,
    )


def _on_overlay_pause(guard: CoexistenceGuard, backend_name: str) -> None:
    """Wired as the overlay's Pause button callback - runs on the overlay's
    own background poll thread (`overlay_linux.LinuxOverlay._poll_events` /
    `overlay_windows.WindowsOverlay._poll_events`), never on the guard's
    usual single-threaded call path. This is safe BY DESIGN, not by luck:
    `pause.PauseController` exists specifically so a human, via the overlay,
    may set pause (`HUMAN_SOURCES` names `\"overlay_click\"` explicitly in its
    own docstring) - this is that wiring, not a new one invented here.
    """
    guard.pause.set("overlay_click", reason="human clicked Pause on the overlay")
    logger.warning("coexistence: human paused via overlay (backend=%r)", backend_name)


def _on_overlay_cancel(guard: CoexistenceGuard, backend_name: str) -> None:
    """Wired as the overlay's Cancel button callback (\u00a78.5: cancel is
    terminal, unlike pause). Runs on the SAME background poll thread as
    `_on_overlay_pause` above.

    Deliberately does NOT call `guard.seed_halted()` or `guard.presence.sample()`
    directly from this thread - both mutate multi-field state
    (`CoexistenceGuard._halted`/`_halt_snapshot`, `PresenceMonitor`'s internal
    fields) that `before_event()` reads/writes from the guard's own
    single-threaded call path, and doing so from a second thread would be a
    genuinely NEW race this codebase does not otherwise have (unlike
    `PauseController.set()`, which was built for exactly this cross-thread
    call). Instead this only writes the DURABLE halt record
    (`halt_state.record_halt`) - the exact mechanism `_poll_durable_halt()`
    already polls for, from the guard's own thread, on the very next
    `before_event()` call. That is what actually latches `_halted=True`;
    this function only ever gets the fact onto disk.
    """
    guard.release_all("cancelled_via_overlay")
    snap = PresenceSnapshot(
        state=PresenceState.HUMAN_ACTIVE,
        confidence=Confidence.HIGH,
        basis="overlay_cancel",
        last_human_input_ago_ms=0.0,
        margin_ms=None,
        guard_ms=guard.presence.guard_ms,
        guard_measured=guard.presence.guard_measured,
        sample_interval_ms=None,
        latched_until_ms=None,
    )
    record_halt(backend_name, snap, reason="cancelled via overlay Cancel button")
    logger.warning(
        "coexistence: human clicked Cancel via overlay (backend=%r) - a "
        "durable halt record was written; every write on this backend is "
        "refused starting with the next guard check, this session and any "
        "future one, until a human explicitly clears it (see "
        "halt_state.resolve_resume_command())",
        backend_name,
    )


def _macos_announce_message(timeout: int, *, controller_host: str) -> str:
    """The \u00a77.3 disclosed-timeout dialog text, shared by the local and
    remote paths - `controller_host` is the machine driving the Mac (its own
    hostname when local, the actual controller's hostname when remote), so
    the same sentence is honest in both deployment shapes.
    """
    return (
        "An automated agent (Amplifier computer-use) wants to drive this Mac "
        f"from {controller_host}.\n\n"
        f"This prompt closes in {timeout} seconds.\n"
        "If nobody answers and this Mac is idle, driving will start.\n"
        "If nobody answers and someone is using this Mac, driving will NOT "
        "start.\n\n"
        "Click Continue to allow driving to begin, or Pause to refuse."
    )


def _apply_macos_announce_result(
    guard: CoexistenceGuard, backend_name: str, result: AnnounceResult
) -> None:
    """The \u00a77.3 policy (rules 1-3), applied to an `AnnounceResult` -
    shared by `_handle_macos_announce` (the dialog ran in THIS process,
    because this process IS the Mac) and `_handle_remote_macos_announce`
    (the dialog ran on a DIFFERENT Mac, relayed back over the wire). The
    decision rules do not care which; only where the dialog physically
    displayed differs, and that distinction is already resolved by the time
    an `AnnounceResult` reaches this function.
    """
    if result.acknowledged:
        if result.button == "Continue":
            logger.info(
                "coexistence: macOS announce dialog acknowledged (Continue) "
                "- driving begins (backend=%r)",
                backend_name,
            )
            return
        raise AnnouncementRefused(
            f"macOS announce dialog was answered {result.button!r} - the "
            "human explicitly declined to allow driving to begin this "
            "session (docs/designs/coexistence.md \u00a77.3)."
        )
    # gave_up: a countdown nobody answered is never consent (rule 2). What it
    # permits is decided by a fresh presence sample, not the clock (rule 3).
    try:
        snap = guard.presence.sample()
    except IdleUnreadableError as exc:
        raise AnnouncementRefused(
            "macOS announce dialog timed out with no answer, and presence "
            f"could not be read to decide what that permits ({exc}) - "
            "refusing to begin driving (docs/designs/coexistence.md \u00a77.3)."
        ) from exc
    if snap.state is PresenceState.QUIET:
        logger.info(
            "coexistence: macOS announce dialog timed out with nobody there "
            "to answer (presence=quiet) - proceeding, per "
            "docs/designs/coexistence.md \u00a77.3 rule 3 (backend=%r)",
            backend_name,
        )
        return
    raise AnnouncementRefused(
        "macOS announce dialog timed out (gave_up) and presence sampled "
        f"{snap.state.value!r} at that moment - a non-answer while someone "
        "may be at the machine is treated as a refusal, never as consent "
        "(docs/designs/coexistence.md \u00a77.3 rules 2-3)."
    )


def _handle_macos_announce(
    guard: CoexistenceGuard, backend_name: str, cfg: dict[str, Any]
) -> None:
    """One announce-and-acknowledge dialog at session start, before the first
    write (`docs/designs/coexistence.md` \u00a77.3) - implements that section's
    three rules exactly (see `_apply_macos_announce_result`).

    The dialog's actual buttons are "Pause"/"Continue" (the tested,
    implemented contract in `announce_macos.py` - see `tests/test_announce_macos.py`),
    not the illustrative "Don't allow"/"Allow" mockup text in the design
    doc's prose; this follows the code that was actually built and tested.
    """
    coexistence_cfg = dict(cfg.get("coexistence") or {})
    timeout = int(
        coexistence_cfg.get("announce_timeout_seconds", MACOS_ANNOUNCE_TIMEOUT_SECONDS)
    )
    message = _macos_announce_message(timeout, controller_host=socket.gethostname())
    try:
        result = macos_announce(message, timeout_seconds=timeout)
    except AnnounceError as exc:
        _handle_channel_failure(guard, backend_name, "macOS announce dialog", exc)
        return
    _apply_macos_announce_result(guard, backend_name, result)


#: Guards `_announcement_decisions` below. A plain `threading.Lock`, not
#: per-key: contention is momentary (dict read/write only - never held
#: across the actual dialog/overlay/RPC call, see `_build_announcement`) and
#: mount() itself is rare enough in any one process that a single lock is
#: not a bottleneck worth complicating.
_announcement_lock = threading.Lock()

#: One decision per PHYSICAL disclosure channel, cached for the life of this
#: controller process - see `_channel_identity` for what "physical channel"
#: means and `_build_announcement` for why this exists. The defect this
#: fixes: a parent session's own `mount()` and a delegated child session's
#: `mount()` (`tool-delegate` inherits the parent's tool config, including
#: any `target:` - see `amplifier_module_tool_delegate._merge_tools`) each
#: used to independently decide whether to proceed, against the SAME
#: machine, without knowing the other had already asked.
#: `shared_transport.py` already solved this exact "more than one consumer
#: in this process talks to the same target" problem for the SSH connection
#: itself (see that module's own docstring); this dict is the same fix one
#: layer up, for the disclosure gate.
_announcement_decisions: dict[str, _AnnouncementDecision] = {}


@dataclass(frozen=True)
class _AnnouncementDecision:
    """The outcome of the FIRST mount() to ask this channel's question -
    reused verbatim by every later mount() in this process rather than
    asking (or refusing) again. Exactly one of `handle`/`refused` is
    meaningful; `refused is not None` means the channel was declined, and
    every subsequent mount() for this channel refuses too, without
    re-showing anything - re-asking after a human already said no is worse
    than not asking at all (it trains people to click through)."""

    handle: Any
    refused: AnnouncementRefused | None


def _channel_identity(backend: Backend) -> str:
    """A stable identity for the PHYSICAL machine a disclosure channel would
    target - the same identity for every `ComputerTool` mount in this
    process that would show a dialog/overlay on the SAME desktop, however
    many separate `mount()` calls (parent session, delegated child session,
    a second delegated child, ...) each independently construct their own
    `Backend`/`ComputerTool` instances for it.

    Remote: `user_host` (added to `RemoteBackend` alongside this fix) - the
    actual `user@host` string, not `backend.name` (which is
    `"remote-ssh:<platform>"` - identical for any two DIFFERENT hosts that
    happen to run the same platform, e.g. two macOS targets). Falls back to
    `backend.name` only if some future `Backend` sets `is_remote = True`
    without a `user_host` - never true for `RemoteBackend` itself past
    `connect()`.

    Local: `backend.name` alone (e.g. "linux-x11", "windows-wsl2", "macos")
    - a controller process only ever drives one local desktop, so the
    backend type is already a unique-enough key.
    """
    if bool(getattr(backend, "is_remote", False)):
        host = getattr(backend, "user_host", None)
        return f"remote:{host}" if host else f"remote:{backend.name}"
    return f"local:{backend.name}"


#: Bug-hunt defect A (verified against `remote_backend.py:73/123`): the
#: durable-halt-state key handed to `record_halt`/`load_halt`/
#: `make_durable_halt_poll` was `backend.name` unchanged - identical to the
#: composite string `_channel_identity` was ALREADY built to avoid using for
#: exactly this reason (see that function's own docstring, "identical for
#: any two DIFFERENT hosts that happen to run the same platform"). Two
#: different remote macOS targets both resolve `backend.name` to
#: `"remote-ssh:macos"`, so a halt detected on one silently also applied to
#: the other - never a live safety hole (the direction is fail-SAFE, over-
#: halting, not under-halting), but wrong, and a future live-retarget
#: feature must not inherit it.
def _halt_key(backend: Backend) -> str:
    """The durable-halt-state key for `backend` - MUST be unique per
    PHYSICAL target, not per backend TYPE. Local backends are unaffected
    (`backend.name` alone, e.g. `"linux-x11"`, is already unique enough - a
    controller process only ever drives one local desktop - so existing
    local halt records keep applying exactly as before, byte-identical
    key). Remote backends get `backend.name` (kept, so an existing
    operator reading a filename/log line still recognizes the platform)
    plus the backend's own `user_host` (unique per SSH target - see
    `RemoteBackend.user_host`'s docstring, and note it now folds in the
    port for a non-standard-port target too - defect B), so two different
    remote hosts of the same platform now get two different keys/files.
    """
    if bool(getattr(backend, "is_remote", False)):
        host = getattr(backend, "user_host", None)
        if host:
            return f"{backend.name}:{host}"
    return backend.name


def _legacy_halt_key(backend: Backend) -> str | None:
    """The PRE-fix durable-halt key `backend` would have collided under
    (`backend.name` alone, composite and platform-wide for a remote
    target) - `None` for local backends, whose key never changed.

    Consulted ONLY as a one-time, read-side migration fallback
    (`_build_coexistence_guard` below) so a halt record written by a
    pre-fix version of this code does not silently stop applying the
    moment this fix ships - a stale key that no longer matches would be a
    halt that silently stops applying, which is a WORSE defect than the
    one this closes (over-halting is the fail-safe direction; an
    unrecognized halt record is not). Deliberately one-directional: NEW
    halts are only ever written under `_halt_key`'s per-host key (see
    `_record_halt_result`/`_on_overlay_cancel` callers) - this legacy key
    is never written to again, only read, so the platform-wide collision
    this whole fix exists to close cannot reappear going forward.
    """
    if bool(getattr(backend, "is_remote", False)) and getattr(
        backend, "user_host", None
    ):
        return backend.name
    return None


def _durable_halt_poll_for(backend: Backend) -> Callable[[], PresenceSnapshot | None]:
    """Wrap `halt_state.make_durable_halt_poll` for `backend`'s per-host
    key (`_halt_key`) - and, for remote backends only, ALSO poll the
    pre-fix platform-wide key (`_legacy_halt_key`) as a fail-safe migration
    net, so a halt recorded by an older version of this code does not
    silently stop applying purely because this fix shipped. The per-host
    poll wins when both would report a halt (it is the accurate one); the
    legacy poll is only ever consulted when the per-host key has nothing.
    Local backends: unchanged, a single poll on `backend.name`, identical
    to pre-fix behavior.
    """
    primary = make_durable_halt_poll(_halt_key(backend))
    legacy_key = _legacy_halt_key(backend)
    if legacy_key is None:
        return primary
    legacy = make_durable_halt_poll(legacy_key)

    def _poll() -> PresenceSnapshot | None:
        return primary() or legacy()

    return _poll


#: Guards `_channel_ledgers`/`_channel_band_state` below - the same
#: momentary-contention rationale as `_announcement_lock` above (dict
#: read/write only, never held across a backend call).
_channel_registry_lock = threading.Lock()

#: One `HeldInputLedger` per PHYSICAL channel (docs/designs/band-lifetime.md,
#: adversarial review finding #2), not one per `ComputerTool` mount(). Before
#: this fix, `_build_coexistence_guard` built a fresh `HeldInputLedger()` on
#: every call - so a parent session's mount() and a delegated child's mount()
#: sharing the SAME overlay via `_announcement_decisions` each got their OWN,
#: disconnected ledger. A hold registered by one was invisible to the other's
#: `release_all`/halt path and invisible to the band-lowering decision below.
#: Keyed exactly like `_announcement_decisions` (`_channel_identity`), for
#: the same reason: these are properties of the physical machine, not of any
#: one mount().
_channel_ledgers: dict[str, HeldInputLedger] = {}


def _get_channel_ledger(channel_key: str) -> HeldInputLedger:
    """Get-or-create the one `HeldInputLedger` for `channel_key`, shared by
    every `ComputerTool` mount() that drives the same physical channel."""
    with _channel_registry_lock:
        ledger = _channel_ledgers.get(channel_key)
        if ledger is None:
            ledger = HeldInputLedger()
            _channel_ledgers[channel_key] = ledger
        return ledger


#: Bug-hunt defect B: which PHYSICAL channels have already had the remote-
#: latency notice (`_build_coexistence_guard` above) printed once in this
#: process - reused rather than a new lock, for the same momentary-
#: contention reason `_channel_registry_lock` already exists (dict
#: read/write only, never held across the actual `logger.warning` call).
#: Keyed exactly like `_channel_ledgers`/`_announcement_decisions`
#: (`_channel_identity`): this is a property of the physical machine, not of
#: any one mount() - a root session's mount() and a delegated child's
#: mount() against the SAME remote target must warn once between them, not
#: once each.
_remote_latency_warned: set[str] = set()


def _mark_remote_latency_warned(channel_key: str) -> bool:
    """Return True the FIRST time `channel_key` is seen in this process
    (caller should log the warning); False every later call for the same
    channel (already warned - caller must not log again)."""
    with _channel_registry_lock:
        if channel_key in _remote_latency_warned:
            return False
        _remote_latency_warned.add(channel_key)
        return True


def _remote_transport_warning_observer(
    channel_key: str, backend_name: str
) -> Callable[[PresenceSnapshot], None]:
    """Make the remote guard's reporting-only sample observer.

    Construction is intentionally quiet: only a successful, measured presence
    sample above `REMOTE_TRANSPORT_WARNING_MS` can log. This observer does not
    participate in presence classification or any write eligibility decision.
    """

    def _observe(snapshot: PresenceSnapshot) -> None:
        latency_ms = snapshot.transport_latency_ms
        if latency_ms <= REMOTE_TRANSPORT_WARNING_MS:
            return
        if _mark_remote_latency_warned(channel_key):
            logger.warning(
                "coexistence: remote presence sample on backend %r took %.3fms "
                "(threshold=%.1fms); reporting only - human detection and write "
                "eligibility are unchanged",
                backend_name,
                latency_ms,
                REMOTE_TRANSPORT_WARNING_MS,
            )

    return _observe


#: Third-instance-of-a-defect-class fix (`_resolve_display_for_target`'s
#: monitor-enumeration fallback, `__init__.py`): which PHYSICAL channels have
#: already had that fallback's log line (WARNING or DEBUG, depending on
#: `exc.expected`) printed once in this process. The SAME pattern as
#: `_remote_latency_warned` immediately above (reusing `_channel_registry_lock`,
#: not a new lock) applied to a different fact, for the same reason: `mount()`
#: runs this path once for `amplifier_core`'s protocol-compliance probe and
#: once for the real mount (see `test_double_mount_defect.py`) - both against
#: the SAME physical channel - so without this, one real condition printed
#: twice.
_monitor_enum_warned: set[str] = set()


def _mark_monitor_enum_warned(channel_key: str) -> bool:
    """Return True the FIRST time `channel_key` is seen in this process for
    the monitor-enumeration-unavailable fallback (caller should log); False
    every later call for the same channel (already logged - caller must not
    log again), regardless of whether that later call's `expected`
    classification differs from the first."""
    with _channel_registry_lock:
        if channel_key in _monitor_enum_warned:
            return False
        _monitor_enum_warned.add(channel_key)
        return True


@dataclass
class _ChannelBandState:
    """Band-lifetime bookkeeping for one physical channel
    (docs/designs/band-lifetime.md \\u00a75.1/\\u00a711.1 - Alt A shape: no
    reaper thread, no trailing window `T`). `depth` is the number of
    `execute()` calls currently in flight against this channel, summed
    across EVERY `ComputerTool`/`DesktopTool` sharing it - this is what
    closes finding F8: two tools sharing one overlay handle via
    `_announcement_decisions` must not let one tool's idle depth lower a
    band the other tool is actively driving under. `lock` serialises every
    transition (increment+raise, decrement+maybe-lower) - see `_band_enter`/
    `_band_exit` on `ComputerTool` for the actual state machine.
    """

    lock: threading.Lock = field(default_factory=threading.Lock)
    depth: int = 0


_channel_band_state: dict[str, _ChannelBandState] = {}


def _get_channel_band_state(channel_key: str) -> _ChannelBandState:
    """Get-or-create the one `_ChannelBandState` for `channel_key`."""
    with _channel_registry_lock:
        state = _channel_band_state.get(channel_key)
        if state is None:
            state = _ChannelBandState()
            _channel_band_state[channel_key] = state
        return state


def _disclosure_decline_reason(coexistence_cfg: dict[str, Any]) -> str | None:
    """Which config key, if any, declined session-start disclosure for this
    session (docs/designs/coexistence.md \u00a77.6) - `announce` (the
    disclosure-specific key) or the legacy `enabled`.

    `enabled` used to ALSO skip building the halt invariant, pause, target
    binding, and exclusion (`_build_coexistence_guard`'s old behavior - see
    that function's docstring for the defect this closed). It no longer can:
    that guard is now unconditional wherever a backend supports presence
    detection. What `enabled: False` can still legitimately do - decline
    disclosure only, on the SAME gated terms `announce` already uses - is
    preserved here rather than deleted, so an operator's existing config
    keeps a real (if narrower) effect instead of silently doing nothing or
    erroring outright. Both keys are read; either being `False` declines.
    Returns the key name (for log lines / refusal messages), or `None` if
    disclosure was not declined.
    """
    if not bool(coexistence_cfg.get("announce", True)):
        return "announce"
    if not bool(coexistence_cfg.get("enabled", True)):
        return "enabled"
    return None


def _refuse_if_disclosure_declined_with_human_present(
    coexistence_cfg: dict[str, Any], guard: CoexistenceGuard | None, backend: Backend
) -> str | None:
    """mount()-time counterpart to `_build_announcement`'s `announce`/
    `enabled` handling (\u00a77.6) - closes the defect where a declined
    disclosure surfaced only as an ordinary-looking
    `ToolResult(success=False)` on whatever action happened to run first
    (`_ensure_announced` only fires on this session's FIRST REAL ACTION, not
    `mount()` - see that method's docstring for why), indistinguishable from
    any other recoverable tool error.

    Performs the exact same \u00a77.6 judgement (human detected + no working
    disclosure -> refuse) here, at mount, with a single side-effect-free
    presence sample: no dialog shown, no overlay built, no consent asked,
    nothing `_ensure_announced`/`_build_announcement` themselves do. Returns
    a refusal reason for `mount()` to hand to `_mount_unavailable`, or `None`
    to mount normally - `_build_announcement`'s own \u00a77.6 policy still runs
    again, unchanged, at first use; this only closes the "silent until first
    action" gap, it does not replace that gate.
    """
    declined_by = _disclosure_decline_reason(coexistence_cfg)
    if declined_by is None:
        return None
    if guard is None:
        # No presence detector for this backend at all (\u00a75.5) - nothing to
        # sample. `_build_announcement` hits its own `guard is None` branch
        # later and logs the same combination; nothing new to refuse here.
        return None
    try:
        snap = guard.presence.sample()
        human_present = snap.state is PresenceState.HUMAN_ACTIVE
    except IdleUnreadableError:
        # \u00a79.6 fail-safe: unreadable idle is treated as present, exactly
        # like `_handle_channel_failure`'s identical fallback.
        human_present = True
    if not human_present:
        return None
    return (
        f"coexistence.{declined_by} declined session-start disclosure and a "
        f"human is currently detected at backend {backend.name!r} - "
        "refusing to mount rather than drive with no disclosure channel "
        "and someone present (docs/designs/coexistence.md \u00a77.6). The halt "
        "invariant (\u00a76.0) is never affected by this key. If this is "
        "intentional, use coexistence.drive_anyway instead - logged every "
        "session, and it does not silently remove disclosure either."
    )


def _build_announcement(
    backend: Backend,
    guard: CoexistenceGuard | None,
    cfg: dict[str, Any],
    disp: Display,
) -> Any | None:
    """Build (and show) the session-start disclosure this backend supports,
    or refuse via `AnnouncementRefused` (\u00a77.3/\u00a77.6). Called exactly once per
    `ComputerTool` instance, from `ComputerTool._ensure_announced` on that
    session's first real action - NOT from `mount()` (see that function's
    own comment for why: a throwaway protocol-compliance probe also calls
    `mount()`, and must never be able to trigger this).

    Returns whatever handle (if any) needs to stay alive for this tool's
    lifetime (an overlay object) so it is not garbage-collected - `None` for
    a one-shot channel (macOS's dialog) or when no channel exists at all.

    `cfg[\"coexistence\"][\"announce\"]` (default `True`): the on/off switch for
    this feature, symmetric with `_build_coexistence_guard`'s own `enabled`
    key - \"on by default\" per the design brief, with the same kind of
    explicit, logged opt-out the rest of this module already uses for other
    policy knobs.
    """
    coexistence_cfg = dict(cfg.get("coexistence") or {})
    declined_by = _disclosure_decline_reason(coexistence_cfg)
    if declined_by is not None:
        logger.info(
            "coexistence: announcement disabled by config (coexistence.%s) "
            "for backend %r",
            declined_by,
            backend.name,
        )
        return None
    if guard is None:
        # No presence detector at all for this backend (\u00a75.5) - there is no
        # way to apply \u00a77.6's human-detected-vs-not policy, so nothing can be
        # safely built here. Loud, not silent: this backend has neither halt
        # protection nor a disclosure channel.
        logger.warning(
            "coexistence: backend %r has no presence detector, so no "
            "announcement channel can be safely gated either "
            "(docs/designs/coexistence.md \u00a77.6) - this session has neither "
            "halt protection nor a disclosure channel",
            backend.name,
        )
        return None

    channel_key = _channel_identity(backend)
    with _announcement_lock:
        cached = _announcement_decisions.get(channel_key)
    if cached is not None:
        if cached.refused is not None:
            logger.warning(
                "coexistence: NOT re-asking for %r - an earlier mount() in "
                "this process already asked and the human declined (%s). "
                "This mount is refused without showing anything again: "
                "re-asking after a refusal is worse than not asking at all.",
                channel_key,
                cached.refused,
            )
            raise AnnouncementRefused(str(cached.refused))
        logger.info(
            "coexistence: reusing the disclosure decision an earlier "
            "mount() in this process already made for %r - a delegated "
            "child session (or a second tool config) targeting the same "
            "machine does not get its own dialog/overlay",
            channel_key,
        )
        return cached.handle

    try:
        handle = _dispatch_announcement(backend, guard, cfg, disp)
    except AnnouncementRefused as exc:
        with _announcement_lock:
            _announcement_decisions.setdefault(
                channel_key, _AnnouncementDecision(handle=None, refused=exc)
            )
        raise
    with _announcement_lock:
        _announcement_decisions.setdefault(
            channel_key, _AnnouncementDecision(handle=handle, refused=None)
        )
    return handle


def _dispatch_announcement(
    backend: Backend,
    guard: CoexistenceGuard,
    cfg: dict[str, Any],
    disp: Display,
) -> Any | None:
    """The actual per-backend-type disclosure logic `_build_announcement`
    memoizes above - unchanged from before that cache existed. Split out so
    the memoization wrapper never has to duplicate (or risk drifting from)
    any of these branches; `guard` is narrowed to non-None here because
    `_build_announcement` already returned early for that case."""

    if isinstance(backend, LinuxX11Backend):
        try:
            overlay = LinuxOverlay(
                backend.display,
                screen_width=disp.screen_width,
                screen_x=disp.origin_x,
                screen_y=disp.origin_y,
                exclusion=guard.exclusion,
                on_pause=lambda: _on_overlay_pause(guard, backend.name),
                # Bug-hunt defect A fix: `_halt_key`, not the bare
                # `backend.name` - see that function's docstring.
                on_cancel=lambda: _on_overlay_cancel(guard, _halt_key(backend)),
            )
            overlay.show()
        except Exception as exc:  # noqa: BLE001 - any failure -> the shared \u00a77.6 policy
            _handle_channel_failure(guard, backend.name, "Linux overlay", exc)
            return None
        logger.info("coexistence: Linux overlay shown for backend %r", backend.name)
        return overlay

    if isinstance(backend, WindowsBackend):
        overlay = WindowsOverlay(
            screen_width=disp.screen_width,
            screen_x=disp.origin_x,
            screen_y=disp.origin_y,
            exclusion=guard.exclusion,
            on_pause=lambda: _on_overlay_pause(guard, backend.name),
            # Bug-hunt defect A fix: `_halt_key`, not the bare
            # `backend.name` - see that function's docstring.
            on_cancel=lambda: _on_overlay_cancel(guard, _halt_key(backend)),
            powershell_path=cfg.get("powershell_path"),
        )
        try:
            overlay.show()
        except Exception as exc:  # noqa: BLE001 - any failure -> the shared \u00a77.6 policy
            _handle_channel_failure(guard, backend.name, "Windows overlay", exc)
            return None
        # No cross-process handle ties this detached PID's life to this
        # agent process's (see overlay_windows.py's module docstring, Phase
        # C5/transport Phase 4) - `atexit` is the honest, minimal stand-in:
        # best-effort cleanup on every normal exit path this process has.
        atexit.register(overlay.hide)
        logger.info("coexistence: Windows overlay shown for backend %r", backend.name)
        return overlay

    if isinstance(backend, MacOSBackend):
        _handle_macos_announce(guard, backend.name, cfg)
        return None

    if bool(getattr(backend, "is_remote", False)):
        return _build_remote_announcement(backend, guard, cfg, disp)

    # Deliberate scope boundary, not a silent gap: a genuinely new backend
    # type this module does not yet know how to announce for.
    logger.warning(
        "coexistence: no announcement channel implemented for backend %r - "
        "a deliberate scope boundary, not a silent gap: the halt invariant "
        "(\u00a76.0) still enforces stop-on-detected-human for this backend, but "
        "there is no proactive disclosure to a human who sits down "
        "mid-session",
        backend.name,
    )
    return None


@dataclass(frozen=True)
class _RemoteAnnouncementHandle:
    """Truthy sentinel stored in `ComputerTool._announcement` when a REMOTE
    target's persistent overlay was raised successfully - there is nothing
    LOCAL to keep alive for it (no thread, no subprocess: the overlay lives
    entirely in the target-side `RemoteAgent`, torn down by ITS OWN shutdown
    path - see `remote_agent.RemoteAgent._teardown_overlay`, wired into the
    same `finally`/signal-handler paths that already guarantee held-input
    release for a remote session). Its only job is to tell
    `ComputerTool._sync_remote_announcement_state` that a channel exists and
    is worth polling before every guarded write - `bool(handle)` is always
    `True` for a real instance, so `self._announcement is not None` alone
    already means \"poll it\".
    """

    backend_name: str


def _build_remote_announcement(
    backend: Backend, guard: CoexistenceGuard, cfg: dict[str, Any], disp: Display
) -> Any | None:
    """The remote counterpart of the three local branches above
    (docs/designs/coexistence.md \u00a77, \u00a710.3) - closes the gap the prior
    pass left as a deliberate scope boundary (see BACKLOG.md): every
    existing announcement module was architected around running in the
    SAME process that owns the injection call site, which for a remote
    session is the TARGET-side `RemoteAgent`, never this controller. This
    function only ever asks the target to raise its own channel
    (`RemoteBackend.announce_raise`, forwarding to
    `remote_agent.RemoteAgent._op_announce_raise`) and applies the exact
    same \u00a77.3/\u00a77.6 policy the local branches already enforce to whatever
    comes back.

    `presence_platform` (set on `RemoteBackend` from its own handshake, the
    same field `_build_coexistence_guard` already uses to resolve
    `GUARD_MS`) - not `backend.name`, which for a remote backend is the
    composite `"remote-ssh:<platform>"` identifier - selects which flavor
    of channel to ask for.
    """
    platform = getattr(backend, "presence_platform", None)
    if platform == "macos":
        _handle_remote_macos_announce(backend, guard, cfg)
        return None
    if platform in ("linux-x11", "windows-wsl2"):
        return _handle_remote_overlay_announce(backend, guard, disp)
    logger.warning(
        "coexistence: no announcement channel implemented for remote "
        "platform %r (backend=%r) - a deliberate scope boundary, not a "
        "silent gap: the halt invariant (\u00a76.0) still enforces "
        "stop-on-detected-human for this backend, but there is no proactive "
        "disclosure to a human who sits down mid-session",
        platform,
        backend.name,
    )
    return None


def _handle_remote_macos_announce(
    backend: Backend, guard: CoexistenceGuard, cfg: dict[str, Any]
) -> None:
    """Remote counterpart of `_handle_macos_announce`: the dialog runs ON
    the target (`remote_agent.RemoteAgent._op_announce_raise`), blocking
    THAT process, not this one, for up to `timeout` seconds - session start
    only, exactly like the local case, never on the injection path. The
    message discloses THIS controller's own hostname (`socket.gethostname()`
    here IS the controller, unlike the local case where it is the Mac
    naming itself) so the human at the far end knows where the session is
    coming from.
    """
    coexistence_cfg = dict(cfg.get("coexistence") or {})
    timeout = int(
        coexistence_cfg.get("announce_timeout_seconds", MACOS_ANNOUNCE_TIMEOUT_SECONDS)
    )
    message = _macos_announce_message(timeout, controller_host=socket.gethostname())
    announce_raise = getattr(backend, "announce_raise", None)
    if announce_raise is None:
        _handle_channel_failure(
            guard,
            backend.name,
            "macOS announce dialog (remote)",
            RuntimeError("remote backend has no announce_raise()"),
        )
        return
    try:
        raw = announce_raise(message=message, timeout_seconds=timeout)
    except BackendError as exc:
        _handle_channel_failure(
            guard, backend.name, "macOS announce dialog (remote)", exc
        )
        return
    result = AnnounceResult(
        button=raw.get("button"), gave_up=bool(raw.get("gave_up")), raw_stdout=""
    )
    _apply_macos_announce_result(guard, backend.name, result)


def _handle_remote_overlay_announce(
    backend: Backend, guard: CoexistenceGuard, disp: Display
) -> Any | None:
    """Remote counterpart of the local Linux/Windows overlay branches: ask
    the target to raise its OWN persistent overlay
    (`remote_agent.RemoteAgent._op_announce_raise`), at the exact screen
    geometry this session already resolved for that target (`disp`, from
    `RemoteBackend.screen_geometry()`/`list_monitors()` - already proven to
    work over the wire). On success, registers the overlay's own
    Pause/Cancel button rects into `guard.exclusion` (\u00a77.5) so THIS
    session's own synthetic clicks refuse to land on them, exactly as the
    local branches already do via `exclusion=guard.exclusion` - the only
    difference is the rects are reported back over the wire rather than
    read off a local object, since the buttons themselves were drawn on
    the target, not here.
    """
    announce_raise = getattr(backend, "announce_raise", None)
    if announce_raise is None:
        _handle_channel_failure(
            guard,
            backend.name,
            "remote overlay",
            RuntimeError("remote backend has no announce_raise()"),
        )
        return None
    try:
        raw = announce_raise(
            screen_width=disp.screen_width,
            screen_x=disp.origin_x,
            screen_y=disp.origin_y,
        )
    except BackendError as exc:
        _handle_channel_failure(guard, backend.name, "remote overlay", exc)
        return None
    if not raw.get("shown"):
        _handle_channel_failure(
            guard,
            backend.name,
            "remote overlay",
            RuntimeError(f"target reported the overlay was not shown: {raw!r}"),
        )
        return None
    for name, rect in (raw.get("buttons") or {}).items():
        if isinstance(rect, (list, tuple)) and len(rect) == 4:
            guard.exclusion.register(
                f"overlay_{name}_button", Rect(*(int(v) for v in rect))
            )
    logger.info("coexistence: remote overlay shown for backend %r", backend.name)
    return _RemoteAnnouncementHandle(backend.name)


class _MountRefused(RuntimeError):
    """Raised by `_select_and_build` for the ONE build-time failure that is
    never a platform/reachability fact: `_refuse_if_disclosure_declined_with_human_present`
    (mount-time counterpart of §7.6) refusing because a human is
    already detected present and coexistence.announce/enabled was
    EXPLICITLY declined in config. Kept as its own type (not folded into
    `NoBackendAvailable`/`RemoteTargetUnavailable`) so callers can route it
    to the LOUD branch of the silent/loud split below without re-deriving
    that classification from a string message - see `mount()`'s own
    docstring for the split itself.
    """


def _select_and_build(cfg: dict[str, Any]) -> ComputerTool:
    """The blocking half of "get a working `ComputerTool` from a config":
    select a backend (may block for seconds on a remote `connect()` -
    C4), build the coexistence guard, and run the SAME mount-time
    disclosure-declined-with-human-present check `mount()` has always run.

    This is deliberately the ONE place this sequence is written - both
    `mount()` (the normal, session-start path) and
    `ComputerUseUnavailableTool._activate` (the "nothing was ever mounted,
    an agent is now pointing this capability at a machine for the first
    time" bootstrap path, see that method) call it, rather than each
    re-implementing "connect, guard, disclose" a second/third time. It
    mirrors (does not replace) the equivalent sequence `ComputerTool.retarget`
    runs for an ALREADY-mounted tool (§6.2 steps 1-4) - retarget's own
    machinery is reused as-is for every retarget AFTER this bootstrap; this
    function only exists to get the FIRST `ComputerTool` into existence, a
    case retarget cannot cover because it is an instance method with no
    instance yet to call it on.

    Raises `NoBackendAvailable`, `ValueError`/`TypeError` (malformed
    `target`), `RemoteTargetUnavailable` (from `.remote_backend`, imported
    lazily by `select_backend` itself), or `_MountRefused` - exactly the
    set both callers already know how to translate into a diagnostic.
    """
    backend = select_backend(cfg)
    computer = ComputerTool(backend, cfg)
    # D2: resolve display once, here, before the tool ever answers a provider
    # request - not lazily on the first `native_tool_spec` read.
    computer.resolve_display()
    # Human/agent coexistence (docs/designs/coexistence.md) - only built for
    # backends with a proven presence-detector wiring (see
    # `_build_coexistence_guard`). `None` on every other backend, unchanged
    # from before this feature existed.
    computer._coexistence_guard = _build_coexistence_guard(backend, cfg)
    # Band lifetime (docs/designs/band-lifetime.md): the channel-scoped
    # ledger and depth-counter this session's held-input tracking and
    # band-lowering decisions use - `None` whenever no guard was built
    # (same population as before this feature existed: no coexistence
    # layer at all). Computed from the SAME backend `_build_coexistence_guard`
    # was just given, so `_channel_identity` returns the identical key.
    if computer._coexistence_guard is not None:
        computer._channel_key = _channel_identity(backend)
        computer._ledger = _get_channel_ledger(computer._channel_key)
        computer._band_state = _get_channel_band_state(computer._channel_key)
    # Defect fix (docs/designs/coexistence.md §7.6): `coexistence.announce`/
    # `coexistence.enabled` declining disclosure used to surface only as an
    # ordinary-looking `ToolResult(success=False)` on this session's first
    # real action (`_ensure_announced` fires there, not here). Check here, at
    # mount, with a single side-effect-free presence sample: refuse to mount
    # outright when a human is already detected present with no disclosure
    # channel, exactly as loud and exactly as early as `NoBackendAvailable`.
    mount_coexistence_cfg = dict(cfg.get("coexistence") or {})
    disclosure_refusal = _refuse_if_disclosure_declined_with_human_present(
        mount_coexistence_cfg, computer._coexistence_guard, backend
    )
    if disclosure_refusal is not None:
        try:
            backend.close()
        except Exception:  # noqa: BLE001 - best-effort cleanup on refusal
            logger.debug(
                "tool-computer-use: backend.close() failed after a "
                "mount-time disclosure refusal",
                exc_info=True,
            )
        raise _MountRefused(disclosure_refusal)
    return computer


async def _mount_backend(coordinator: Any, cfg: dict[str, Any]) -> dict[str, Any]:
    """Build a working `computer`/`desktop` pair from `cfg` and mount both -
    the successful-path body `mount()` used to inline directly. Extracted so
    `ComputerUseUnavailableTool._activate` (the stub's new "point this at a
    machine" operation - see that class) can reach the identical, already-
    reviewed sequence instead of a parallel one. Raises exactly what
    `_select_and_build` raises; callers translate that into a diagnostic
    (`mount()`) or a `ToolResult` (`_activate`).
    """
    computer = await asyncio.to_thread(_select_and_build, cfg)
    # Session-start disclosure (docs/designs/coexistence.md §7) is
    # deliberately NOT built here - see `ComputerTool._ensure_announced` for
    # why it fires on first real action instead (a validation-probe mount()
    # must never be able to trigger a real dialog/overlay).
    await coordinator.mount("tools", computer, name=computer.name)
    desktop = DesktopTool(computer)
    await coordinator.mount("tools", desktop, name=desktop.name)
    backend_name = computer._backend.name
    logger.info(
        "tool-computer-use mounted: 'computer' (%s, backend=%s) + 'desktop'",
        computer._tool_version,
        backend_name,
    )
    return {
        "name": "tool-computer-use",
        "version": __version__,
        "provides": ["computer", "desktop"],
        "description": f"Anthropic native computer-use via backend={backend_name}",
    }


# ============================================================================
# Discovery (`ComputerUseUnavailableTool` action="discover") - read-only,
# side-effect-free survey of machines this agent could plausibly point
# computer-use at. Every field below is exactly what its source reported -
# never a guess papered over a gap. In particular: a Tailscale peer's
# reported "owner" is the Tailscale ACCOUNT the node is registered to, NOT
# necessarily a Unix login name on that machine - real-world example that
# motivated this: a tailnet reporting owner "alice@github" for a machine
# whose actual working ssh user is "a-user". Treating that owner string as
# an ssh user would silently produce a wrong, confidently-stated target.
# Only `~/.ssh/config`'s explicit `User` directive for a Host is trusted as
# an asserted ssh login user; everything else comes back with `user: null,
# ambiguous_user: true` so the caller (agent or human) resolves it rather
# than this function guessing.
# ============================================================================


def _parse_ssh_config(path: Path) -> dict[str, dict[str, str]]:
    """Minimal `~/.ssh/config` reader: one dict per concrete (non-wildcard)
    `Host` alias, with whatever of `HostName`/`User`/`Port` that block sets.
    Deliberately not a full ssh_config parser (no `Match`/`Include`/multi-
    pattern precedence) - this only ever feeds `discover`'s candidate list,
    where "found a plausible lead" is the bar, not "authoritative ssh
    resolution" (`ssh` itself remains the actual authority when a connection
    is attempted).
    """
    if not path.is_file():
        return {}
    hosts: dict[str, dict[str, str]] = {}
    current: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        key, value = parts[0].lower(), parts[1].strip()
        if key == "host":
            current = [
                alias
                for alias in value.split()
                if "*" not in alias and "?" not in alias
            ]
            for alias in current:
                hosts.setdefault(alias, {})
            continue
        if key in ("hostname", "user", "port"):
            for alias in current:
                hosts[alias][key] = value
    return hosts


def _parse_known_hosts(path: Path) -> list[str]:
    """Hostnames/IPs this machine has previously connected to. Hashed
    entries (`|1|...`, the OpenSSH default since 6.6) carry no recoverable
    hostname and are skipped rather than guessed at. No user information
    exists in this file format at all - every candidate from this source is
    `user: null, ambiguous_user: true`.
    """
    if not path.is_file():
        return []
    names: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("|1|"):
            continue
        first_field = line.split(None, 1)[0]
        for host in first_field.split(","):
            host = host.strip()
            if host.startswith("[") and "]" in host:
                host = host[1 : host.index("]")]
            if host:
                names.add(host)
    return sorted(names)


def _tailscale_peers() -> tuple[list[dict[str, Any]], str | None]:
    """`tailscale status --json` peers, or `([], reason)` if the binary is
    missing, the daemon is unreachable, or output could not be parsed - a
    tailnet is an optional source, never a hard requirement for `discover`.
    """
    import shutil
    import subprocess

    ts_path = shutil.which("tailscale")
    if ts_path is None:
        return [], "tailscale: not installed / not on PATH"
    try:
        proc = subprocess.run(
            [ts_path, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
            # Explicit, not the parent's fd 0 (a real terminal on a real
            # desktop) - this is a one-shot, no-input helper call, exactly
            # the discipline test_subprocess_stdin_safety.py enforces
            # across this whole module.
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"tailscale status --json failed to run: {exc}"
    if proc.returncode != 0:
        return [], (
            f"tailscale status --json exited {proc.returncode}: "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return [], f"tailscale status --json returned invalid JSON: {exc}"
    users = data.get("User") or {}
    peers: list[dict[str, Any]] = []
    for peer in (data.get("Peer") or {}).values():
        user_id = str(peer.get("UserID") or "")
        owner = (users.get(user_id) or {}).get("LoginName")
        ips = peer.get("TailscaleIPs") or []
        peers.append(
            {
                "hostname": peer.get("HostName") or None,
                "dns_name": (peer.get("DNSName") or "").rstrip(".") or None,
                "tailscale_ip": next((ip for ip in ips if "." in ip), None)
                or (ips[0] if ips else None),
                "tailscale_owner": owner,
                "online": bool(peer.get("Online")),
                "os": peer.get("OS") or None,
            }
        )
    return peers, None


def _discover_candidates() -> ToolResult:
    """`action="discover"` - read-only, three sources, no network probing
    beyond `tailscale status` (which talks to the LOCAL tailscaled, not the
    candidate machines themselves - nothing here attempts to reach any
    candidate). See the module-level comment above this section for the
    ambiguous-user policy this implements.
    """
    candidates: list[dict[str, Any]] = []
    warnings: list[str] = []

    for alias, fields in sorted(
        _parse_ssh_config(Path.home() / ".ssh" / "config").items()
    ):
        candidates.append(
            {
                "source": "ssh_config",
                "host_alias": alias,
                "hostname": fields.get("hostname", alias),
                "user": fields.get("user"),
                "port": fields.get("port"),
                "ambiguous_user": "user" not in fields,
                "suggested_target": (
                    f"ssh://{fields['user']}@{fields.get('hostname', alias)}"
                    + (f":{fields['port']}" if fields.get("port") else "")
                    if "user" in fields
                    else None
                ),
            }
        )

    ts_peers, ts_warning = _tailscale_peers()
    if ts_warning:
        warnings.append(ts_warning)
    for peer in ts_peers:
        candidates.append(
            {
                "source": "tailscale",
                "hostname": peer["hostname"] or peer["dns_name"],
                "dns_name": peer["dns_name"],
                "tailscale_ip": peer["tailscale_ip"],
                "tailscale_owner": peer["tailscale_owner"],
                "online": peer["online"],
                "os": peer["os"],
                "user": None,
                "ambiguous_user": True,
                "suggested_target": None,
            }
        )

    for host in _parse_known_hosts(Path.home() / ".ssh" / "known_hosts"):
        candidates.append(
            {
                "source": "known_hosts",
                "hostname": host,
                "user": None,
                "ambiguous_user": True,
                "suggested_target": None,
            }
        )

    return ToolResult(
        success=True,
        output=json.dumps(
            {
                "candidates": candidates,
                "warnings": warnings,
                "guidance": (
                    "Every candidate with ambiguous_user=true (user: null) has "
                    "NO asserted ssh login user - do not guess one (a "
                    "tailscale 'tailscale_owner' is the Tailscale ACCOUNT the "
                    "node is registered to, not necessarily a Unix username on "
                    "it, and the two commonly differ). Ask the human which "
                    "user to connect as. Only ssh_config candidates with "
                    "ambiguous_user=false carry a ready-to-use "
                    "'suggested_target'. Once a target string is confirmed: "
                    "if you want it to survive a restart, call action="
                    '"persist" FIRST (order matters - a successful '
                    '"activate" unmounts this very tool, so a later '
                    '"persist" call against it would have nothing to call), '
                    'then call action="activate" to make it live now.'
                ),
            },
            default=str,
        ),
    )


# ============================================================================
# Persistence (`ComputerUseUnavailableTool` action="persist") - the ONLY
# operation on this stub that writes the user's own settings.yaml, and it
# writes ONLY when explicitly called with this action. A prior six-lens
# council ruled 6/6 that an agent must never silently rewrite `config.target`
# - "the answer to 'which machine am I about to control' ... the worst
# mutation available in this bundle." That ruling is honored here in full:
# discovery and activation never touch this file; this is the one, named,
# explicit path that does, and it reports exactly what it wrote and how to
# undo it. It exists at all because the user who owns that ruling has since
# explicitly asked for exactly this capability, twice.
# ============================================================================


def _amplifier_home() -> Path:
    """`~/.amplifier`, or `$AMPLIFIER_HOME` if set - the same two-line
    resolution `amplifier_foundation.paths.resolution.get_amplifier_home()`
    uses. Reimplemented locally (not imported) rather than adding a
    dependency on the app-layer foundation package for two stdlib calls -
    see IMPLEMENTATION_PHILOSOPHY.md's "Conventions via instructions, not
    code": the pattern is documented, not shared, because this module
    should not need to depend on `amplifier_foundation` at all.
    """
    env_home = os.environ.get("AMPLIFIER_HOME")
    if env_home:
        return Path(env_home).expanduser()
    return Path.home() / ".amplifier"


def _persist_target(
    target: str | None, *, settings_path: Path | None = None
) -> ToolResult:
    """`action="persist"` - write (or update in place) ONE
    `config.tools[]` entry: `{module: "tool-computer-use", config: {target:
    <target>}}` - the exact shape the app CLI's settings merge already
    expects (list of `{module, config}` dicts under `config.providers`/
    `config.tools`/`config.hooks`, iterated by
    `amplifier_app_cli/runtime/config.py`'s `resolve_bundle_config`). Every
    other key in the file, and every other field already on this module's
    own entry, is preserved untouched - only `config.target` inside THIS
    entry is written. `settings_path` is a test seam (default: `_amplifier_home()
    / "settings.yaml"`, the real file a user edits by hand today).
    """
    import yaml

    path = settings_path or (_amplifier_home() / "settings.yaml")
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            return ToolResult(
                success=False,
                error={
                    "message": (
                        f"refusing to write: {path} is not valid YAML "
                        f"({exc}) - fix or remove it by hand first"
                    ),
                    "type": "InvalidSettingsFile",
                },
            )
        if loaded is not None and not isinstance(loaded, dict):
            return ToolResult(
                success=False,
                error={
                    "message": (
                        f"refusing to write: {path} does not contain a "
                        "YAML mapping at the top level"
                    ),
                    "type": "InvalidSettingsFile",
                },
            )
        existing = loaded or {}

    config_section = existing.setdefault("config", {})
    if not isinstance(config_section, dict):
        return ToolResult(
            success=False,
            error={
                "message": f"refusing to write: {path}'s 'config' key is not a mapping",
                "type": "InvalidSettingsFile",
            },
        )
    tools_section = config_section.setdefault("tools", [])
    if not isinstance(tools_section, list):
        return ToolResult(
            success=False,
            error={
                "message": f"refusing to write: {path}'s 'config.tools' key is not a list",
                "type": "InvalidSettingsFile",
            },
        )

    entry = next(
        (
            item
            for item in tools_section
            if isinstance(item, dict) and item.get("module") == "tool-computer-use"
        ),
        None,
    )
    if entry is None:
        entry = {"module": "tool-computer-use", "config": {}}
        tools_section.append(entry)
    entry_cfg = entry.setdefault("config", {})
    if not isinstance(entry_cfg, dict):
        return ToolResult(
            success=False,
            error={
                "message": (
                    "refusing to write: the existing tool-computer-use "
                    "entry's 'config' is not a mapping"
                ),
                "type": "InvalidSettingsFile",
            },
        )

    normalized = str(target).strip() if target else ""
    if normalized and normalized != "local":
        entry_cfg["target"] = normalized
        action_desc = f"set config.tools[].config.target={normalized!r}"
    else:
        entry_cfg.pop("target", None)
        action_desc = "removed config.tools[].config.target (persisted as local)"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(existing, sort_keys=False), encoding="utf-8")

    return ToolResult(
        success=True,
        output=json.dumps(
            {
                "wrote": str(path),
                "change": action_desc,
                "undo": (
                    f"edit {path}: remove the 'target' key from the "
                    "config.tools[] entry whose module is 'tool-computer-use' "
                    "(or delete that whole list entry), then restart the "
                    "session - nothing else in the file was touched"
                ),
            },
            default=str,
        ),
    )


class ComputerUseUnavailableTool:
    """Registered in place of `computer`/`desktop` whenever `mount()` could not
    obtain a working backend (see `_mount_unavailable`).

    Defect 2 fix: D1's refusal to mount `computer`/`desktop` for a backend that
    cannot serve them is correct and is NOT changed by this class - what was
    wrong is what happened *after* that refusal. Previously `mount()` returned
    a manifest with `provides: []` and logged a line nobody was watching; the
    session then continued with computer-use simply absent - no tool, no
    error, no trace in the model's own context. A live session hit exactly
    this: the model, given no `computer`/`desktop` tool and no indication one
    was ever supposed to exist, silently improvised its own `ssh`+`screencapture`
    workaround instead of telling the user computer-use was unavailable.

    Self-configuring fix: the tombstone above closed HALF the defect - the
    model could finally SEE it was unavailable, but had no way to DO anything
    about it, so a technical user still had to hand-edit settings.yaml (and,
    for real, get the shape wrong - the old message named the config KEY but
    never the FILE, the nesting, or that `config.tools[]` mirrors
    `config.providers[]`). Three new `action`s close the other half, entirely
    through calls this tool itself exposes - no config file ever has to be
    hand-edited for an agent to go from "nothing mounted" to "driving a
    machine": `discover` (read-only survey of candidate machines - see
    `_discover_candidates`), `activate` (build a real, disclosed
    `ComputerTool`/`DesktopTool` pair and mount them for real - see
    `_activate`, which reuses `_mount_backend`/`_select_and_build`, the exact
    machinery `mount()` itself uses, rather than a parallel path), and
    `persist` (the ONE explicit, named write to the user's settings.yaml -
    see `_persist_target` for why it is never a side effect of the other two).

    This tool is NOT a degraded form of `computer`/`desktop` - it never
    attempts to serve a single real screen/mouse/keyboard action, so it does
    not weaken D1's "do not pretend to work" invariant. Its `execute()` for
    any action OTHER than the three above is a fallback for the (unlikely,
    since its own description says not to) case the model calls it expecting
    `computer`/`desktop` semantics anyway - still an honest, immediate
    failure, never a hang or a fabricated result.

    Deliberately a distinct name (`computer_use_unavailable`), not `computer`/
    `desktop`: those names are reserved for a tool that can actually act
    (`ComputerTool`/`DesktopTool`) - reusing them here for a stub would blur
    "the tool exists but is broken" with "the tool never existed" in the
    model's own tool list, and defeats the whole point of a distinct signal.
    """

    def __init__(
        self,
        reason: str,
        coordinator: Any = None,
        cfg: dict[str, Any] | None = None,
    ) -> None:
        self._reason = reason
        # Both default to None/{} - existing callers (and tests) that
        # construct this with just a reason string keep working unchanged;
        # `discover`/`persist` need neither, only `activate` needs
        # `_coordinator` (to actually mount the real tools) and uses `_cfg`
        # as the base config to layer a `target` onto (see `_activate`).
        self._coordinator = coordinator
        self._cfg: dict[str, Any] = dict(cfg or {})

    @property
    def name(self) -> str:
        return "computer_use_unavailable"

    @property
    def description(self) -> str:
        return (
            "computer-use ('computer'/'desktop': screen capture, mouse, keyboard, "
            "window control) is NOT available this session and those tools were "
            f"NOT mounted. Reason: {self._reason} Do not attempt to see or "
            "control a screen this session via any other tool (e.g. improvising "
            "a workaround over a shell tool) - tell the user computer-use is "
            "unavailable and why, UNLESS you can resolve it yourself with the "
            "actions below. This tool can point computer-use at a machine "
            'without anyone hand-editing config: action="discover" surveys '
            "candidate machines (Tailscale, ~/.ssh/config, ~/.ssh/known_hosts) "
            "read-only; action=\"activate\" (target='ssh://user@host[:port]' or "
            "omitted/'local') builds and mounts real `computer`/`desktop` tools "
            "for this session right now, replacing this stub on success; "
            'action="persist" (same target argument) writes that choice to '
            "the user's settings.yaml so it survives a restart - this is the "
            "ONLY action that writes anything, and it always reports exactly "
            "what it wrote and how to undo it. Any OTHER action on this tool "
            "always fails - it never simulates a real screen/mouse/keyboard "
            "action."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["discover", "activate", "persist"],
                    "description": (
                        "discover: read-only candidate-machine survey. "
                        "activate: build+mount real computer/desktop tools "
                        "for this session now. persist: write the target to "
                        "settings.yaml so it survives a restart."
                    ),
                },
                "target": {
                    "type": "string",
                    "description": (
                        "'ssh://user@host[:port]' for activate/persist, or "
                        "omitted/'local' for this machine's own local backend."
                    ),
                },
            },
            "required": ["action"],
        }

    async def _activate(self, target: str | None) -> ToolResult:
        """`action="activate"` - the bootstrap entry point requirement 1
        needs: from a session where NOTHING mounted, build a real, disclosed
        `ComputerTool`/`DesktopTool` pair and mount them, so the real tools
        become callable in THIS running session with no restart. Reuses
        `_mount_backend`/`_select_and_build` - the exact sequence `mount()`
        itself runs - rather than a parallel implementation (see this
        class's own docstring). On success, unmounts this stub (`self`) so
        the model never sees a contradictory "unavailable" tool sitting next
        to a working one. On failure, this stub simply stays mounted and
        reports why, exactly like every other honest failure in this module -
        never a silent no-op, never a fabricated success.
        """
        if self._coordinator is None:
            return ToolResult(
                success=False,
                error={
                    "message": (
                        "no coordinator bound to this stub instance - it was "
                        "constructed directly (e.g. in a test) rather than "
                        "via mount(), so activate has nothing to mount into"
                    ),
                    "type": "ComputerUseUnavailable",
                },
            )
        new_cfg = dict(self._cfg)
        normalized = str(target).strip() if target else ""
        if normalized and normalized != "local":
            new_cfg["target"] = normalized
        else:
            new_cfg.pop("target", None)

        try:
            manifest = await _mount_backend(self._coordinator, new_cfg)
        except NoBackendAvailable as exc:
            return ToolResult(
                success=False,
                error={"message": str(exc), "type": "NoBackendAvailable"},
            )
        except (ValueError, TypeError) as exc:
            return ToolResult(
                success=False,
                error={
                    "message": f"invalid configuration: {exc}",
                    "type": type(exc).__name__,
                },
            )
        except _MountRefused as exc:
            return ToolResult(
                success=False,
                error={"message": str(exc), "type": "MountRefused"},
            )
        except Exception as exc:  # noqa: BLE001 - see mount()'s identical guard
            from .remote_backend import RemoteTargetUnavailable

            if not isinstance(exc, RemoteTargetUnavailable):
                raise
            return ToolResult(
                success=False,
                error={"message": str(exc), "type": "RemoteTargetUnavailable"},
            )

        try:
            await self._coordinator.unmount("tools", name=self.name)
        except Exception:  # noqa: BLE001 - best-effort; the real tools are
            # already live either way, this only tidies up the now-redundant
            # stub entry.
            logger.debug(
                "tool-computer-use: failed to unmount the unavailable-stub "
                "after a successful activate",
                exc_info=True,
            )
        self._cfg = new_cfg
        logger.info(
            "tool-computer-use: activated from the unavailable-stub - %s",
            manifest.get("description"),
        )
        return ToolResult(
            success=True,
            output=json.dumps(
                {**manifest, "activated_target": new_cfg.get("target", "local")},
                default=str,
            ),
        )

    async def execute(self, input: dict[str, Any]) -> ToolResult:
        action = str(input.get("action") or "").strip()
        if action == "discover":
            return await asyncio.to_thread(_discover_candidates)
        if action == "activate":
            return await self._activate(input.get("target"))
        if action == "persist":
            return await asyncio.to_thread(_persist_target, input.get("target"))
        del input
        return ToolResult(
            success=False,
            error={"message": self._reason, "type": "ComputerUseUnavailable"},
        )


# ============================================================================
# Silent-vs-loud split (self-configuring / silent-when-inapplicable):
#
# `_mount_unavailable`'s ONE caller-visible knob is `loud`. The two classes
# it distinguishes:
#
#   STATE, not a defeated ask (`loud=False`, logged at DEBUG only): no
#   `target` was configured AND no local backend is possible on this
#   platform (`NoBackendAvailable` - `select_backend` only ever raises this
#   in the no-`target` path, see registry.py). Nobody asked this session to
#   drive a screen; a broad bundle simply happened to include this module.
#   There is nothing to warn about - the stub still mounts (the MODEL always
#   learns the capability is off, via that tool's own description, on every
#   request - fail-loud to the one audience that can act on it) but the
#   HUMAN'S console stays clean, because their request was never refused.
#
#   INTENT DEFEATED (`loud=True`, logged at ERROR, unchanged from before this
#   fix): a `target` WAS configured and is unreachable (`RemoteTargetUnavailable`),
#   config is malformed (`ValueError`/`TypeError`), or coexistence disclosure
#   was explicitly declined while a human is present (`_MountRefused`).
#   Someone configured something and did not get it - that is always worth a
#   human's attention, exactly as it was before this fix.
#
# This is NOT "no fallbacks" erosion: a real failure (any INTENT DEFEATED
# case above) is exactly as loud as it always was. Only the case where
# nothing was ever asked for and nothing was denied - a platform fact, not a
# failure - stops paging a human who never made a request in the first
# place. Next reader: if you add a new exception branch here, ask "did this
# session ask for something and not get it?" - if yes, `loud=True`; if the
# honest answer is "nothing was asked for", `loud=False`.
# ============================================================================


async def _mount_unavailable(
    coordinator: Any, reason: str, cfg: dict[str, Any], *, loud: bool = True
) -> dict[str, Any]:
    """Shared "not mounted" path for every branch of `mount()` that cannot
    obtain a working backend - see `ComputerUseUnavailableTool` for why a
    stub tool, not just a log line, is what actually closes Defect 2, and
    the comment block directly above this function for what `loud` means
    and why it exists.
    """
    if loud:
        logger.error("tool-computer-use: NOT MOUNTING computer/desktop - %s", reason)
    else:
        logger.debug(
            "tool-computer-use: not mounting computer/desktop this session "
            "(no target configured and no local backend on this platform - "
            "a state, not a failed request; the model still sees this via "
            "the mounted stub's own description) - %s",
            reason,
        )
    stub = ComputerUseUnavailableTool(reason, coordinator, cfg)
    await coordinator.mount("tools", stub, name=stub.name)
    return {
        "name": "tool-computer-use",
        "version": __version__,
        "provides": [stub.name],
        "description": f"computer-use not mounted: {reason}",
    }


async def mount(
    coordinator: Any, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Probe for a usable backend, and only then mount `computer` and `desktop`.

    D1 fix: this used to construct `WindowsBridge` and mount both tools
    unconditionally - on any platform. Now every configured backend is probed
    first (`registry.select_backend`, via `_select_and_build`); if none can
    serve this machine, `computer`/`desktop` are not mounted at all - this
    function still returns normally (it does not raise - a missing backend is
    not a bundle-load failure) - but see `_mount_unavailable`/
    `ComputerUseUnavailableTool`: D1's refusal to mount a non-functional
    backend is unchanged, only what happens instead of silence, and (as of
    the self-configuring fix) whether that "instead" is loud - see the
    comment block above `_mount_unavailable` for the silent/loud split.
    """
    cfg = config or {}
    try:
        return await _mount_backend(coordinator, cfg)
    except NoBackendAvailable as exc:
        # No `target` configured and no local backend on this platform - a
        # state, not a defeated ask. See the silent/loud comment block above.
        return await _mount_unavailable(coordinator, str(exc), cfg, loud=False)
    except (ValueError, TypeError) as exc:
        # A malformed config (e.g. `target: user@host` instead of
        # `target: ssh://user@host`) raises out of `select_backend`, NOT as
        # `NoBackendAvailable`. Before this branch existed it escaped the handler
        # above entirely and the tool simply never appeared - no traceback, no
        # log line, nothing in the session to explain the absence. Observed for
        # real: a session was asked to drive a remote desktop, found no tool, and
        # silently improvised its own ssh+screencapture workaround instead.
        return await _mount_unavailable(
            coordinator, f"invalid configuration: {exc}", cfg, loud=True
        )
    except _MountRefused as exc:
        return await _mount_unavailable(coordinator, str(exc), cfg, loud=True)
    except Exception as exc:
        # Defect 2 fix: `select_backend`'s remote branch raises
        # `RemoteTargetUnavailable` (`.remote_backend`) for an explicitly
        # configured target that could not be reached, and - unlike
        # `NoBackendAvailable` - that exception is deliberately NOT caught
        # above (registry.py's own docstring: "an unreachable target fails
        # loud... never falls back to a local backend"). That design assumed
        # an uncaught exception here would be loud on its own. It is not:
        # `amplifier_core._session_init` wraps every tool module's `mount()`
        # in a blanket `except Exception` and merely logs a WARNING before
        # continuing the session - so this exception was reaching that
        # handler, being logged at a level nobody was watching, and the
        # session was silently proceeding with no computer-use tool and no
        # signal to the model. That kernel behavior is out of this bundle's
        # control; this module closes its own end of the gap instead of
        # relying on an escape that gets swallowed one layer up.
        #
        # Imported here, not at module top: `remote_backend.py` pulls in
        # `ssh_transport.py` (subprocess/tarfile), which a local-only mount
        # (no `target:` configured) has no reason to load - same discipline
        # `registry.select_backend` already applies to this same import. By
        # the time this line runs, `remote_backend` is already in
        # `sys.modules` regardless (the exception could only have originated
        # from code that already imported it), so this costs nothing extra.
        from .remote_backend import RemoteTargetUnavailable

        if not isinstance(exc, RemoteTargetUnavailable):
            raise
        return await _mount_unavailable(coordinator, str(exc), cfg, loud=True)
