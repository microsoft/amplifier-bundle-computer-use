"""Coordinate geometry shared by every computer-use backend.

Coordinate spaces
------------------
MODEL space  - what Claude sees and emits. Matches the downscaled screenshot exactly.
SCREEN space - real physical pixels of the desktop (a Windows virtual desktop, an X11
               root window, ...).

`Display.to_screen()` / `Display.to_model()` are the only places the two are converted.
This module is pure math - no I/O, no subprocess, no backend-specific knowledge - so it
is shared unchanged by every backend (`WindowsBackend`, `LinuxX11Backend`, ...).
"""

from __future__ import annotations

from dataclasses import dataclass


class InvalidDisplayGeometryError(ValueError):
    """Nonpositive desktop dimensions cannot define a coordinate mapping."""


class CoordinateOutOfRangeError(ValueError):
    """Raised by `Display.to_screen` when a MODEL-space coordinate lies well
    outside the screenshot the model was actually shown.

    `to_screen` used to clamp ANY coordinate - no matter how far outside the
    model's own image bounds - to the nearest valid screen pixel, and the
    action would then report success against whatever it happened to land
    on. A model coordinate hundreds of pixels outside the image it was shown
    is not a rounding artifact at an edge; it is real evidence of a
    mis-scaled screenshot, a stale monitor selection, or a provider
    coordinate-space mismatch - and silently clicking the nearest edge
    instead of surfacing that is exactly the kind of silent degradation this
    bundle otherwise refuses to allow (see
    `ComputerUseHookIncompatibleProviderError`,
    `ComputerUseNativeToolPassthroughUnsupportedError` in hook-computer-use).

    Coordinates within `_EDGE_TOLERANCE_PX` of the image bounds are still
    clamped, not raised - see that constant's docstring for why that band is
    legitimate. This is a `ValueError` subclass so it is caught by every
    existing `except (BackendError, ValueError)` handler in `ComputerTool._run`
    /`execute` without any change to that dispatch code, and reaches the model
    as an ordinary tool error it can react to (e.g. by requesting a fresh
    screenshot) rather than as a click that silently landed on the wrong
    target.
    """


#: How far a MODEL-space coordinate may lie beyond the screenshot's own bounds
#: before `to_screen` treats it as a real error rather than a rounding/off-by-
#: one artifact. Judgement call, not evidence: `model_width`/`model_height`
#: THEMSELVES (one past the last valid 0-indexed pixel, e.g. x=1280 on a
#: 1280px-wide image) are always legitimate - that is the ordinary "used the
#: dimension instead of dimension-1" pattern models routinely produce at a
#: screen edge, and is exactly what `test_to_screen_scales_and_clamps` already
#: locks in. A couple more pixels of slack absorb sub-pixel rounding
#: differences across dialects (see `providers._normalize_gemini_action`'s
#: float-not-int comment: Gemini's 0..999 grid over an image lands fractions
#: of a pixel short of the true edge by construction, never past it - so this
#: tolerance is generous with room to spare for that case, not tuned tightly
#: to it). Anywhere past this band is not an edge - it is a different
#: coordinate space entirely, and clamping it there would click the wrong
#: target while reporting success.
_EDGE_TOLERANCE_PX = 2


@dataclass(frozen=True)
class ImageSpace:
    """The pixel size of the screenshot the model was actually shown.

    The *sub-fact* of `Display` that a provider dialect needs in order to read a
    tool call: a vendor that emits normalized or relative coordinates cannot be
    translated into MODEL space without knowing how big that image was. It is
    deliberately NOT a `Display` - a dialect has no business knowing the screen
    size, the monitor origin, or the model->screen mapping. It reads a payload;
    the only thing outside the payload it can legitimately need is what the
    model was looking at when it wrote it.

    Per-session and mutable mid-session (`ComputerTool.select_monitor`), so it
    is passed per call rather than closed over anywhere.
    """

    width: int
    height: int


@dataclass(frozen=True)
class Display:
    """Mapping between the real desktop and the image the model actually sees."""

    screen_width: int
    screen_height: int
    model_width: int
    model_height: int
    origin_x: int = 0
    origin_y: int = 0

    @property
    def image_space(self) -> ImageSpace:
        """Just the model-image size, for callers with no business knowing the
        rest of this mapping - see `ImageSpace` and `providers.read_call`."""
        return ImageSpace(self.model_width, self.model_height)

    @property
    def scale_x(self) -> float:
        return self.screen_width / self.model_width

    @property
    def scale_y(self) -> float:
        return self.screen_height / self.model_height

    @property
    def scale(self) -> float:
        """Alias for `scale_x`, kept for callers that only need one factor.

        `compute_display` preserves aspect ratio, so `scale_x` and `scale_y` are equal
        to within a pixel of rounding; `to_screen`/`to_model` always use the correct
        per-axis factor internally and never rely on this alias.
        """
        return self.scale_x

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        """MODEL coords -> SCREEN coords, clamped to the desktop bounds.

        Raises `CoordinateOutOfRangeError` if `(x, y)` lies more than
        `_EDGE_TOLERANCE_PX` outside the model image this `Display` was built
        for - see that error's docstring for why a coordinate that far out is
        a real error, not a rounding artifact to silently paper over.
        """
        lo_x, hi_x = -_EDGE_TOLERANCE_PX, self.model_width - 1 + _EDGE_TOLERANCE_PX
        lo_y, hi_y = -_EDGE_TOLERANCE_PX, self.model_height - 1 + _EDGE_TOLERANCE_PX
        if not (lo_x <= x <= hi_x):
            raise CoordinateOutOfRangeError(
                f"model x={x!r} is outside the {self.model_width}x{self.model_height} "
                f"image the model was shown (valid x is {lo_x}..{hi_x} including the "
                f"{_EDGE_TOLERANCE_PX}px edge tolerance) - refusing to clamp a click "
                "onto the wrong target"
            )
        if not (lo_y <= y <= hi_y):
            raise CoordinateOutOfRangeError(
                f"model y={y!r} is outside the {self.model_width}x{self.model_height} "
                f"image the model was shown (valid y is {lo_y}..{hi_y} including the "
                f"{_EDGE_TOLERANCE_PX}px edge tolerance) - refusing to clamp a click "
                "onto the wrong target"
            )
        sx = round(x * self.scale_x) + self.origin_x
        sy = round(y * self.scale_y) + self.origin_y
        sx = max(self.origin_x, min(sx, self.origin_x + self.screen_width - 1))
        sy = max(self.origin_y, min(sy, self.origin_y + self.screen_height - 1))
        return sx, sy

    def to_model(self, sx: float, sy: float) -> tuple[int, int]:
        """SCREEN coords -> MODEL coords (inverse of `to_screen`), clamped to the image.

        This is the missing inverse: without it, anything that reports a real screen
        position back to the model (e.g. `cursor_position`) has no correct way to
        express it in the coordinate space the model actually uses.
        """
        x = round((sx - self.origin_x) / self.scale_x)
        y = round((sy - self.origin_y) / self.scale_y)
        x = max(0, min(x, self.model_width - 1))
        y = max(0, min(y, self.model_height - 1))
        return x, y


def compute_display(
    screen_w: int, screen_h: int, max_edge: int, max_pixels: int
) -> tuple[int, int]:
    """Largest model-space size that preserves aspect ratio and respects both budgets."""
    if screen_w <= 0 or screen_h <= 0:
        raise InvalidDisplayGeometryError(
            f"display dimensions must be positive; got {screen_w}x{screen_h}"
        )
    scale = min(
        1.0,
        max_edge / max(screen_w, screen_h),
        (max_pixels / (screen_w * screen_h)) ** 0.5,
    )
    # Even dimensions avoid half-pixel rounding drift when mapping coordinates back.
    return max(2, int(screen_w * scale) // 2 * 2), max(
        2, int(screen_h * scale) // 2 * 2
    )
