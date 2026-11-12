"""Unit tests for the coordinate math shared by every backend (`geometry.py`).

This is pure logic with zero I/O - it had zero test coverage before this change,
despite being the one place MODEL<->SCREEN coordinate translation happens for every
click, drag, and scroll this bundle ever sends to a real desktop.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import pytest
from amplifier_module_tool_computer_use.geometry import (
    CoordinateOutOfRangeError,
    Display,
    ImageSpace,
    compute_display,
)

# -- compute_display -----------------------------------------------------------


@pytest.mark.parametrize(
    "width,height", [(0, 0), (0, 1080), (1920, 0), (-1, 1080), (1920, -1), (-1, -1)]
)
def test_compute_display_rejects_nonpositive_dimensions(width, height):
    with pytest.raises(ValueError, match="display dimensions must be positive"):
        compute_display(width, height, max_edge=1280, max_pixels=1_150_000)


def test_compute_display_preserves_aspect_ratio_when_downscaling():
    mw, mh = compute_display(3840, 2160, max_edge=1280, max_pixels=1_150_000)
    assert mw <= 1280
    assert mh <= 1280
    # 16:9 in, 16:9 out (within even-dimension rounding).
    assert abs((mw / mh) - (3840 / 2160)) < 0.01


def test_compute_display_never_upscales():
    # A screen already smaller than the budget must come back unchanged (mod evenness).
    mw, mh = compute_display(800, 600, max_edge=1280, max_pixels=1_150_000)
    assert mw <= 800
    assert mh <= 600


def test_compute_display_respects_max_pixels_even_under_max_edge():
    # 1280x1280 is within max_edge but is 1.6MP - over the 1.15MP budget.
    mw, mh = compute_display(1280, 1280, max_edge=1280, max_pixels=1_150_000)
    assert mw * mh <= 1_150_000


def test_compute_display_dimensions_always_even():
    for w, h in [(1024, 768), (3840, 2160), (1921, 1081), (801, 601)]:
        mw, mh = compute_display(w, h, max_edge=1280, max_pixels=1_150_000)
        assert mw % 2 == 0
        assert mh % 2 == 0


def test_compute_display_never_below_floor():
    # Degenerate/tiny inputs must not collapse to zero.
    mw, mh = compute_display(2, 2, max_edge=1280, max_pixels=1_150_000)
    assert mw >= 2
    assert mh >= 2


# -- Display.to_screen / to_model ------------------------------------------------


def test_to_screen_scales_and_clamps():
    disp = Display(
        screen_width=3840, screen_height=2160, model_width=1280, model_height=720
    )
    sx, sy = disp.to_screen(0, 0)
    assert (sx, sy) == (0, 0)
    sx, sy = disp.to_screen(1280, 720)  # bottom-right corner, model space
    assert sx == disp.screen_width - 1
    assert sy == disp.screen_height - 1


def test_to_screen_applies_origin_offset():
    # A secondary monitor at a non-zero origin (the real Windows multi-monitor case).
    disp = Display(
        screen_width=1920,
        screen_height=1080,
        model_width=1280,
        model_height=720,
        origin_x=1920,
        origin_y=0,
    )
    sx, sy = disp.to_screen(0, 0)
    assert sx == 1920
    assert sy == 0


def test_to_model_is_the_inverse_of_to_screen():
    """This inverse did not exist before this change - `cursor_position` had no
    correct way to report a real screen position back in the coordinate space the
    model actually uses."""
    disp = Display(
        screen_width=3840, screen_height=2160, model_width=1280, model_height=720
    )
    for mx, my in [(0, 0), (640, 360), (1279, 719)]:
        sx, sy = disp.to_screen(mx, my)
        rx, ry = disp.to_model(sx, sy)
        # Round-trip through integer rounding at 3x scale: within 1px.
        assert abs(rx - mx) <= 1
        assert abs(ry - my) <= 1


def test_to_model_clamps_to_model_bounds():
    disp = Display(
        screen_width=1024, screen_height=768, model_width=1024, model_height=768
    )
    x, y = disp.to_model(-100, -100)
    assert x == 0
    assert y == 0
    x, y = disp.to_model(999999, 999999)
    assert x == disp.model_width - 1
    assert y == disp.model_height - 1


def test_scale_x_and_scale_y_track_independent_axes():
    disp = Display(
        screen_width=1024, screen_height=768, model_width=1024, model_height=768
    )
    assert disp.scale_x == 1.0
    assert disp.scale_y == 1.0
    assert disp.scale == disp.scale_x  # documented alias


# -- negative origin (real multi-monitor case) ----------------------------------
#
# Measured on the real machine this feature was built for: four 3840x2160
# monitors including DISPLAY1 at (1946, -2160) and DISPLAY4 at (5786, -2163).
# `Display.origin_x`/`origin_y` used to only ever see a virtual-desktop origin
# (nonnegative in every environment this bundle was previously tested against);
# per-monitor targeting hands them a REAL monitor's origin instead, and that can
# be negative. Nothing here special-cases sign - `to_screen`/`to_model` are
# already generic arithmetic - but that genericity was never exercised by a test
# before, and an off-by-one or sign error would have been invisible without one.


def test_to_screen_handles_negative_origin_monitor():
    # DISPLAY1: 3840x2160 at (1946, -2160) - one of two negative-origin monitors
    # on the real four-monitor layout this feature was built for.
    disp = Display(
        screen_width=3840,
        screen_height=2160,
        model_width=1280,
        model_height=720,
        origin_x=1946,
        origin_y=-2160,
    )
    # Model-space top-left maps to this monitor's real top-left corner.
    sx, sy = disp.to_screen(0, 0)
    assert (sx, sy) == (1946, -2160)
    # Model-space bottom-right maps to this monitor's real bottom-right corner.
    sx, sy = disp.to_screen(1280, 720)
    assert sx == 1946 + 3840 - 1
    assert sy == -2160 + 2160 - 1  # == -1, not 2159 - a sign error would give this


def test_to_screen_clamps_within_negative_origin_monitor_bounds():
    # DISPLAY4: 3840x2160 at (5786, -2163).
    disp = Display(
        screen_width=3840,
        screen_height=2160,
        model_width=1280,
        model_height=720,
        origin_x=5786,
        origin_y=-2163,
    )
    # A legitimate at-the-edge model coordinate (the dimension itself, one past
    # the last 0-indexed pixel - see test_to_screen_scales_and_clamps) must clamp
    # to THIS monitor's real bounds, not to (0, 0) or to the positive quadrant -
    # a naive `max(0, ...)` clamp (correct for an origin of 0) would be wrong
    # here. This used to be exercised with wildly out-of-range inputs
    # (-999/999999); those now raise CoordinateOutOfRangeError (see
    # test_to_screen_raises_on_wildly_out_of_range_coordinate) - a real
    # negative-origin monitor's own bounds are exercised here instead, at the
    # legitimate edge.
    sx, sy = disp.to_screen(0, 0)
    assert (sx, sy) == (5786, -2163)
    sx, sy = disp.to_screen(1280, 720)
    assert (sx, sy) == (5786 + 3840 - 1, -2163 + 2160 - 1)


# -- Display.to_screen: out-of-range MODEL coordinates must fail loud -----------
#
# `to_screen` used to clamp ANY model coordinate - however far outside the
# model's own image bounds - to the nearest valid screen pixel, and the click
# would then "succeed" against whatever it happened to land on. A model
# coordinate hundreds of pixels outside the image it was shown is real evidence
# of a mis-scaled screenshot, a stale monitor selection, or a provider
# coordinate-space mismatch - not a rounding artifact to paper over.


def test_to_screen_raises_on_wildly_out_of_range_coordinate():
    disp = Display(
        screen_width=3840, screen_height=2160, model_width=1280, model_height=720
    )
    with pytest.raises(CoordinateOutOfRangeError):
        disp.to_screen(1680, 360)  # 400px past the 1280-wide model image
    with pytest.raises(CoordinateOutOfRangeError):
        disp.to_screen(640, -999)  # wildly negative, not a rounding artifact
    with pytest.raises(CoordinateOutOfRangeError):
        disp.to_screen(999999, 999999)


def test_to_screen_tolerates_the_dimension_itself_as_the_edge():
    """The single most common off-by-one a model makes: emitting the image's
    own width/height (one past the last valid 0-indexed pixel) instead of
    width-1/height-1. This must keep clamping, not raise - it is the exact
    case `test_to_screen_scales_and_clamps` already locks in."""
    disp = Display(
        screen_width=3840, screen_height=2160, model_width=1280, model_height=720
    )
    sx, sy = disp.to_screen(1280, 720)
    assert (sx, sy) == (disp.screen_width - 1, disp.screen_height - 1)


def test_to_screen_tolerates_small_rounding_overshoot_past_the_edge():
    """One pixel past the dimension itself is still within the documented
    edge tolerance (`_EDGE_TOLERANCE_PX=2`, one of which the dimension-as-
    edge case above already spends) - sub-pixel rounding differences across
    provider dialects, not a real out-of-range error."""
    disp = Display(
        screen_width=3840, screen_height=2160, model_width=1280, model_height=720
    )
    sx, sy = disp.to_screen(1281, 721)  # 1px past width/height - still tolerated
    assert (sx, sy) == (disp.screen_width - 1, disp.screen_height - 1)


def test_to_screen_raises_just_past_the_edge_tolerance():
    """One pixel beyond the documented tolerance must raise - proves the
    boundary is exactly where it is documented to be, not a looser 'close
    enough' heuristic."""
    disp = Display(
        screen_width=3840, screen_height=2160, model_width=1280, model_height=720
    )
    with pytest.raises(CoordinateOutOfRangeError):
        disp.to_screen(1282, 360)  # 1px past the tolerated 1281
    with pytest.raises(CoordinateOutOfRangeError):
        disp.to_screen(640, -3)  # 1px past the tolerated -2 on the low edge


def test_to_model_round_trips_through_negative_origin():
    """The inverse must hold exactly (mod rounding) for a negative-origin
    monitor, not just for the origin=0 case `test_to_model_is_the_inverse_of_to_screen`
    already covers."""
    disp = Display(
        screen_width=3840,
        screen_height=2160,
        model_width=1280,
        model_height=720,
        origin_x=1946,
        origin_y=-2160,
    )
    for mx, my in [(0, 0), (1, 1), (640, 360), (1279, 719)]:
        sx, sy = disp.to_screen(mx, my)
        rx, ry = disp.to_model(sx, sy)
        assert abs(rx - mx) <= 1
        assert abs(ry - my) <= 1


def test_to_model_clamps_real_screen_coords_from_a_different_negative_monitor():
    """A real absolute screen coordinate that lies on a DIFFERENT monitor (e.g.
    the cursor is on DISPLAY3 at the origin while `disp` targets DISPLAY1, whose
    negative-y bounds do not contain (0, 0) at all) must clamp into `disp`'s own
    model bounds, not silently produce a negative or out-of-range model
    coordinate."""
    disp = Display(
        screen_width=3840,
        screen_height=2160,
        model_width=1280,
        model_height=720,
        origin_x=1946,
        origin_y=-2160,
    )
    mx, my = disp.to_model(0, 0)  # (0, 0) is real, but on DISPLAY3, not this one
    assert 0 <= mx <= disp.model_width - 1
    assert 0 <= my <= disp.model_height - 1


# -- ImageSpace ----------------------------------------------------------------


def test_image_space_is_just_the_model_image_size():
    """What a provider dialect needs to read a payload, and nothing more: a
    dialect has no business knowing the screen size or the monitor origin."""
    disp = Display(
        screen_width=3840,
        screen_height=2160,
        model_width=1280,
        model_height=720,
        origin_x=1920,
        origin_y=0,
    )
    space = disp.image_space
    assert (space.width, space.height) == (1280, 720)
    assert space == ImageSpace(1280, 720)
