"""The Linux announcement overlay - `docs/designs/coexistence.md` \u00a77.4-\u00a77.5.

An X11 override-redirect window (proven ghost-free and non-focus-stealing by
U4/U5/U6 in `coexistence-probes.md`): it appears without the window manager's
involvement, never steals input focus, receives clicks on its own buttons
without moving focus away from whatever the agent is driving, and is
destroyed automatically by the X server the instant this process's
connection dies (`SIGKILL` included - no separate cleanup code can leave a
ghost indicator behind).

Two properties this module is responsible for, both load-bearing:

1. **The status band is input-transparent.** `SHAPE`/`ShapeInput` (O10) lets
   the window be visually large (noticeable) while only the Pause/Cancel
   button rectangles actually consume clicks - everywhere else, clicks pass
   through to whatever is underneath.
2. **Those same button rectangles are excluded at the injection call site**
   (`exclusion.ExclusionZone`, registered here) - so the agent's own
   synthetic clicks cannot land on its own controls. This is a second,
   independent check from (1): the SHAPE input mask governs *real* input
   from the human; the exclusion zone governs *synthetic* input from the
   agent. Both are required; neither substitutes for the other.

This module talks to Xlib directly (like `linux_x11.py`) rather than going
through `Backend` - the overlay is infrastructure the coexistence layer owns
directly on the same connection style, not a `computer`-tool action.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .exclusion import ExclusionZone, Rect

logger = logging.getLogger(__name__)

_IMPORT_ERROR: str | None = None
try:
    from Xlib import X as _X
    from Xlib import display as _xdisplay
    from Xlib.ext import shape as _shape
except Exception as exc:  # noqa: BLE001 - see linux_x11.py's identical pattern
    _X = _shape = _xdisplay = None  # type: ignore[assignment]
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
X: Any = _X
shape: Any = _shape

#: Status band geometry, in pixels - a thin strip along the top edge, wide
#: enough to be noticeable (O11 is a human-factors question, not settled
#: here) without covering much of the screen the agent needs to see.
BAND_HEIGHT = 36
BUTTON_WIDTH = 90
BUTTON_MARGIN = 8

#: Colors as (R, G, B) 0-255 - a plain, high-contrast amber band with a red
#: Cancel and a grey Pause button. Rendered via Xlib `GC` fills only - no
#: font/text rendering dependency, keeping this module's only dependency the
#: same Xlib the rest of the Linux backend already requires.
_BAND_COLOR = (0xB8, 0x86, 0x0B)
_PAUSE_COLOR = (0x55, 0x55, 0x55)
_CANCEL_COLOR = (0xA4, 0x1E, 0x1E)


@dataclass(frozen=True)
class OverlayButton:
    name: str
    rect: Rect


class LinuxOverlay:
    """An override-redirect status band with Pause/Cancel buttons.

    `display` is an already-connected `Xlib.display.Display`. With
    `own_display=True`, retain only its exact address and open an independent
    connection while shown. A channel-cached overlay must outlive the tool
    backend that created it; backend cleanup must not destroy sibling controls.
    Standalone/remote-agent callers may still borrow their display (the default).
    """

    def __init__(
        self,
        display: Any,
        *,
        own_display: bool = False,
        screen_width: int,
        screen_x: int = 0,
        screen_y: int = 0,
        exclusion: ExclusionZone | None = None,
        on_pause: Callable[[], None] | None = None,
        on_cancel: Callable[[], None] | None = None,
    ) -> None:
        if _IMPORT_ERROR is not None:
            raise RuntimeError(
                f"python-xlib SHAPE extension not available ({_IMPORT_ERROR}); "
                "cannot build the Linux coexistence overlay"
            )
        self._own_display = own_display
        self._display_name = display.get_display_name() if own_display else None
        self._display = None if own_display else display
        self._screen_x = screen_x
        self._screen_y = screen_y
        self._screen_width = screen_width
        self._exclusion = exclusion
        self._on_pause = on_pause
        self._on_cancel = on_cancel
        self._window: Any = None
        self._buttons: list[OverlayButton] = []
        self._poll_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._shown = False

    # -- geometry (pure, unit-testable without a live window) ----------------
    def _button_rects(self) -> list[OverlayButton]:
        """Right-aligned Pause/Cancel button rects within the band, in SCREEN
        space (absolute desktop coordinates - what `exclusion.ExclusionZone`
        and the injection call site both operate in)."""
        y1 = self._screen_y
        y2 = self._screen_y + BAND_HEIGHT
        cancel_x2 = self._screen_x + self._screen_width - BUTTON_MARGIN
        cancel_x1 = cancel_x2 - BUTTON_WIDTH
        pause_x2 = cancel_x1 - BUTTON_MARGIN
        pause_x1 = pause_x2 - BUTTON_WIDTH
        return [
            OverlayButton("pause", Rect(pause_x1, y1, pause_x2, y2)),
            OverlayButton("cancel", Rect(cancel_x1, y1, cancel_x2, y2)),
        ]

    # -- lifecycle -------------------------------------------------------------
    def show(self) -> None:
        """Create, shape, and map the overlay. Idempotent."""
        if self._shown:
            return
        # Never reopen the creator backend. The cached address is the original
        # target, not a fresh ambient DISPLAY lookup. hide() releases this
        # connection so an idle cached handle holds no X11 resources.
        if self._own_display and self._display is None:
            self._display = _xdisplay.Display(self._display_name)
        self._stop = threading.Event()
        try:
            self._show_window()
        except BaseException:
            self.hide()
            raise

    def _show_window(self) -> None:
        screen = self._display.screen()
        root = screen.root
        self._window = root.create_window(
            self._screen_x,
            self._screen_y,
            self._screen_width,
            BAND_HEIGHT,
            0,
            screen.root_depth,
            X.InputOutput,
            X.CopyFromParent,
            override_redirect=1,
            event_mask=X.ExposureMask | X.ButtonPressMask,
            background_pixel=self._alloc_color(_BAND_COLOR),
        )
        self._buttons = self._button_rects()
        if self._exclusion is not None:
            for btn in self._buttons:
                self._exclusion.register(f"overlay_{btn.name}_button", btn.rect)
        self._window.map()
        self._apply_input_shape()
        self._paint()
        self._display.sync()
        self._shown = True
        self._poll_thread = threading.Thread(
            target=self._poll_events,
            args=(self._stop, self._display),
            name="cu-overlay-poll",
            daemon=True,
        )
        self._poll_thread.start()
        logger.info(
            "coexistence overlay shown: band=%dx%d at (%d,%d), buttons=%s",
            self._screen_width,
            BAND_HEIGHT,
            self._screen_x,
            self._screen_y,
            [b.name for b in self._buttons],
        )

    def hide(self) -> None:
        """Unmap and destroy. Also called implicitly by process death (U6:
        the X server tears down every resource owned by a dead connection,
        so a crash never leaves a ghost - this method just makes the clean
        shutdown path explicit and fast)."""
        self._stop.set()
        try:
            if self._exclusion is not None:
                for btn in self._buttons:
                    self._exclusion.unregister(f"overlay_{btn.name}_button")
            if self._window is not None:
                try:
                    self._window.unmap()
                    self._window.destroy()
                except Exception:
                    logger.debug(
                        "coexistence overlay: error during hide", exc_info=True
                    )
                self._window = None
            self._shown = False
        finally:
            if self._own_display and self._display is not None:
                display, self._display = self._display, None
                display.close()

    @property
    def shown(self) -> bool:
        return self._shown

    @property
    def buttons(self) -> list[OverlayButton]:
        return list(self._buttons)

    # -- SHAPE: input-transparent everywhere except the two button rects -----
    def _apply_input_shape(self) -> None:
        """O10: `SHAPE`/`ShapeInput` lets the band be visually large while
        only the button rects actually take input - everywhere else, a real
        click passes through to whatever window is underneath."""
        rects = [
            (
                b.rect.x1 - self._screen_x,
                b.rect.y1 - self._screen_y,
                b.rect.x2 - b.rect.x1,
                b.rect.y2 - b.rect.y1,
            )
            for b in self._buttons
        ]
        self._window.shape_rectangles(shape.SO.Set, shape.SK.Input, 0, 0, 0, rects)

    def _paint(self) -> None:
        gc_pause = self._window.create_gc(foreground=self._alloc_color(_PAUSE_COLOR))
        gc_cancel = self._window.create_gc(foreground=self._alloc_color(_CANCEL_COLOR))
        for btn, gc in zip(self._buttons, (gc_pause, gc_cancel), strict=True):
            self._window.fill_rectangle(
                gc,
                btn.rect.x1 - self._screen_x,
                btn.rect.y1 - self._screen_y,
                btn.rect.x2 - btn.rect.x1,
                btn.rect.y2 - btn.rect.y1,
            )

    def _alloc_color(self, rgb: tuple[int, int, int]) -> int:
        cmap = self._display.screen().default_colormap
        r, g, b = rgb
        color = cmap.alloc_color(r * 257, g * 257, b * 257)
        return color.pixel

    # -- click handling --------------------------------------------------------
    def _poll_events(self, stop: threading.Event, display: Any) -> None:
        """Background thread: block on this window's own event queue for
        `ButtonPress` on a button rect. A dedicated thread (rather than
        folding into the caller's own loop) keeps this class usable
        standalone and testable without requiring the caller to pump events.
        """
        # Each show generation owns its queue/stop token. A thread stopping
        # after hide must never consume events from a later re-raise connection.
        while not stop.is_set():
            try:
                if display.pending_events() == 0:
                    stop.wait(0.05)
                    continue
                event = display.next_event()
            except Exception:  # noqa: BLE001 - connection torn down under us
                return
            if stop.is_set() or event.type != X.ButtonPress:
                continue
            name = (
                self._exclusion.contains(event.root_x, event.root_y)
                if self._exclusion
                else None
            )
            if name == "overlay_pause_button" and self._on_pause is not None:
                self._on_pause()
            elif name == "overlay_cancel_button" and self._on_cancel is not None:
                self._on_cancel()
