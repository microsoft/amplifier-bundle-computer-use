"""Falsification test for `providers.Dialect`: can the dispatch table absorb a
THIRD, genuinely divergent vendor without being restructured?

Gemini was chosen because it disagrees with both incumbents on every axis the
table was extracted from - no wire `type`, normalized coordinates instead of
pixels, verb-per-function instead of an `action`/`type` field. Everything
asserted here is transcribed from live traffic captured 2026-08-03 against
`gemini-2.5-computer-use-preview-10-2025` (`tests/fixtures/captures/gemini-*.json`).

This is the SECOND run of that measurement. The first (commit 31a033a) returned
PARTIAL: the record fit, but the table had no vocabulary for coordinate space,
so one expression had to be added to `ComputerTool.execute`, and two files
outside `providers.py` gave demonstrably wrong answers for a Gemini session -
pinned then as `test_seam_gap_*` tests asserting those WRONG answers.

Those gaps were closed IN THE BASE, vendor-neutrally, before this file was
rewritten: `read_call` now takes the coordinate space a payload's numbers were
measured against (`geometry.ImageSpace`); `tool_versions.beta_header_for` asks
which dialect OWNS a type, so "this vendor has no such concept" is a distinct
answer from "unknown type"; and `hook-computer-use` reads the tool's stated
`native_tool_type` instead of inferring it from a vendor-shaped wire dict. The
`test_seam_gap_*` tests are therefore GONE - replaced below by their opposites,
asserting the right answers. See `test_closed_gap_*`.

Three groups of tests:

  * WHAT FIT     - declaration, dispatch, verb translation, AND now coordinate
                   conversion: absorbed as one `Dialect` record, no caller
                   change.
  * WHAT CLOSED  - `test_closed_gap_*`: the answers that were wrong on the
                   first run and are right now.
  * WHAT STILL
    DOES NOT FIT - scroll magnitude. Not a vocabulary gap: the missing fact
                   (pixels-per-notch) belongs to the BACKEND, not the vendor,
                   so no `Dialect` field could hold a correct value. Fails
                   loud. Read the docstring before "fixing" it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))
sys.path.insert(0, str(ROOT / "modules" / "hook-computer-use"))

import pytest
from amplifier_module_tool_computer_use import providers
from amplifier_module_tool_computer_use.geometry import Display, ImageSpace
from amplifier_module_tool_computer_use.tool_versions import (
    KNOWN_MODEL_TOOL_VERSIONS,
    beta_header_for,
)

CAPTURES = ROOT / "tests" / "fixtures" / "captures"


def _capture(name: str) -> dict:
    return json.loads((CAPTURES / name).read_text(encoding="utf-8"))


def _display_from(cap: dict) -> Display:
    mw, mh = cap["image_space"]
    sw, sh = cap["monitor"]
    return Display(screen_width=sw, screen_height=sh, model_width=mw, model_height=mh)


# ===========================================================================
# WHAT FIT: absorbed as one `Dialect` record, nothing outside `providers.py`
# ===========================================================================


def test_declaration_has_no_type_key_at_all():
    """`{"type": "computer_use", ...}` -> 400 `Unknown name "type" at
    'tools[0]'`. The vendor key IS the discriminator. Both incumbent dialects
    put `type` at the top level; this one must not."""
    spec = providers.GEMINI.declare(
        "computer_use", width=1280, height=720, enable_zoom=True
    )
    assert spec == {"computer_use": {"environment": "ENVIRONMENT_DESKTOP"}}
    assert "type" not in spec


def test_gemini_owns_its_key_and_the_incumbents_are_untouched():
    assert providers.dialect_for_tool_type("computer_use") is providers.GEMINI
    assert providers.dialect_for_tool_type("computer") is providers.OPENAI
    assert providers.dialect_for_tool_type("computer_20251124") is providers.ANTHROPIC


def test_function_call_shape_is_read_as_gemini():
    """The verb is the FUNCTION NAME, not a field - detection is by shape, the
    same way `_read_openai_actions` sniffs its batch."""
    fn = _capture("gemini-response-1.json")["candidates"][0]["content"]["parts"][0][
        "functionCall"
    ]
    dialect, calls = providers.read_call(fn, ImageSpace(1280, 720))
    assert dialect is providers.GEMINI
    ((action, params),) = list(calls)
    assert action == "mouse_move"
    # 311/1000*1280, 84/1000*720 - already in MODEL space, straight out of the
    # reader. No second, display-aware pass anywhere.
    assert params["coordinate"] == [pytest.approx(398.08), pytest.approx(60.48)]


def test_hover_at_is_also_a_move():
    dialect, calls = providers.read_call(
        {"name": "hover_at", "args": {"x": 500, "y": 500}}, ImageSpace(1000, 1000)
    )
    assert dialect is providers.GEMINI
    assert list(calls) == [("mouse_move", {"coordinate": [500.0, 500.0]})]


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "left_click", "coordinate": [1, 2]},  # anthropic
        {"actions": [{"type": "screenshot"}]},  # openai
        {"name": "move_to"},  # no args mapping
        {"args": {"x": 1, "y": 2}},  # no verb
    ],
)
def test_gemini_declines_every_shape_that_is_not_its_own(payload):
    """Adding a third dialect must not steal payloads from the first two."""
    assert providers._read_gemini_actions(payload, ImageSpace(1280, 720)) is None


def test_incumbent_dispatch_is_unchanged_by_the_third_dialect():
    anth, anth_calls = providers.read_call(
        {"action": "left_click", "coordinate": [1, 2]}
    )
    assert anth is providers.ANTHROPIC
    assert list(anth_calls) == [
        ("left_click", {"action": "left_click", "coordinate": [1, 2]})
    ]
    oai, oai_calls = providers.read_call({"actions": [{"type": "screenshot"}]})
    assert oai is providers.OPENAI
    assert list(oai_calls) == [("screenshot", {})]


def test_verified_model_prefix_resolves_to_gemini_key():
    """`gemini-response-1.json` reports modelVersion
    `gemini-2.5-computer-use-preview-10-2025`; the undated prefix must cover
    the dated id, per the Anthropic precedent."""
    assert KNOWN_MODEL_TOOL_VERSIONS["gemini-2.5-computer-use"] == "computer_use"
    from amplifier_module_tool_computer_use.tool_versions import required_for_model

    assert (
        required_for_model("gemini-2.5-computer-use-preview-10-2025") == "computer_use"
    )


# ---------------------------------------------------------------------------
# Coordinate space: absorbed by the reader now, not patched into the caller
# ---------------------------------------------------------------------------


def test_coordinates_are_normalized_not_pixels():
    """`gemini-unknown5.json`: probing far-right/bottom-right of a 1280x720
    image returned x=999 and y=999. y=999 is impossible in pixel space on a
    720px-tall image, so the space is a fixed 0..999 grid."""
    cap = _capture("gemini-unknown5.json")
    _, image_h = cap["image_space"]
    assert cap["normalized_cap"] == providers._GEMINI_COORD_MAX == 999
    assert max(p[3] for p in cap["probes"]) == 999
    assert image_h - 1 < 999  # 719 < 999: cannot be a pixel coordinate


def test_normalized_conversion_reproduces_the_measured_cursor_exactly():
    """THE GROUND-TRUTH TEST. `gemini-coordproof.json` recorded where the real
    cursor actually landed for a known raw pair. The pipeline - `read_call`
    -> `Display.to_screen` - must reproduce it exactly.

    Note what is NOT in that pipeline any more: a second, display-aware
    normalization step applied by the caller. `read_call` is a complete
    translation for this dialect too now."""
    cap = _capture("gemini-coordproof.json")
    disp = _display_from(cap)

    _, calls = providers.read_call(cap["function_call"], disp.image_space)
    ((_, params),) = list(calls)
    x, y = params["coordinate"]

    assert list(disp.to_screen(x, y)) == cap["real_cursor_after_normalized_move"]
    assert list(disp.to_screen(x, y)) == [1194, 181]


def test_the_pixel_interpretation_is_wrong_by_hundreds_of_pixels():
    """What the tool did BEFORE the dialect converted, and what it would
    silently keep doing if a Gemini payload were fed through unconverted:
    (933, 252) instead of (1194, 181). 261px off in x, 71px in y - a click on
    the wrong thing, with no error anywhere."""
    cap = _capture("gemini-coordproof.json")
    disp = _display_from(cap)
    raw = cap["raw"]

    unconverted = disp.to_screen(float(raw["x"]), float(raw["y"]))
    assert list(unconverted) == cap["as_pixels"] == [933, 252]
    assert list(unconverted) != cap["real_cursor_after_normalized_move"]


def test_the_grid_divisor_is_1000_not_999():
    """A 0..999 grid over N pixels is N/1000 per cell. Dividing by 999 lands
    999 one past the edge and misses the captured cursor by a pixel."""
    cap = _capture("gemini-coordproof.json")
    disp = _display_from(cap)
    raw = cap["raw"]

    by_999 = disp.to_screen(
        raw["x"] / 999 * disp.model_width, raw["y"] / 999 * disp.model_height
    )
    assert list(by_999) != cap["real_cursor_after_normalized_move"]
    assert providers._GEMINI_COORD_SPAN == 1000


def test_model_space_stays_float_because_rounding_early_loses_a_pixel():
    """The reader must NOT round. y=84 is 60.48 model px, which scales to 181
    screen px (measured truth); a pre-rounded 60 scales to 180."""
    cap = _capture("gemini-coordproof.json")
    disp = _display_from(cap)

    _, calls = providers.read_call(cap["function_call"], disp.image_space)
    ((_, params),) = list(calls)
    x, y = params["coordinate"]

    assert isinstance(x, float) and isinstance(y, float)
    assert disp.to_screen(x, y)[1] == 181
    assert disp.to_screen(round(x), round(y))[1] == 180  # what rounding early costs


def test_a_missing_image_space_fails_loud_rather_than_assuming_a_size():
    """The `None` default on `read_call` is not a licence to guess. A
    normalized coordinate with no known image is unplaceable, and saying so is
    the only honest answer - `execute()` turns it into an ordinary tool error."""
    _, calls = providers.read_call({"name": "move_to", "args": {"x": 311, "y": 84}})
    with pytest.raises(ValueError, match="display geometry is not resolved"):
        list(calls)


def test_verb_errors_are_raised_lazily_so_execute_can_catch_them():
    """`ComputerTool.execute` calls `read_call` OUTSIDE its try block and
    iterates INSIDE it. An eager reader would move every Gemini verb error
    outside the caller's error handling - silently, since the type
    `Callable[..., Iterable | None]` cannot distinguish "raises on
    construction" from "raises on iteration"."""
    _, calls = providers.read_call(
        {"name": "open_web_browser", "args": {}}, ImageSpace(1280, 720)
    )  # must NOT raise here
    with pytest.raises(ValueError):
        list(calls)  # must raise HERE


# ===========================================================================
# WHAT CLOSED: the two answers that were wrong on the first measurement
# ===========================================================================


def test_closed_gap_beta_header_for_gemini_is_none_not_anthropics_header():
    """WAS `test_seam_gap_beta_header_for_gemini_returns_anthropics_header`,
    which pinned the wrong answer `"computer-use-2025-11-24"`.

    `beta_header_for` now asks which dialect OWNS the type instead of doing a
    flat dict lookup with an Anthropic-shaped default, so `beta_headers={}` on
    an owning dialect means "this vendor has no such concept" - a real answer -
    rather than being indistinguishable from "type I have never heard of".
    Closed in `tool_versions.py` as a vendor-neutral base change, using the
    table already in hand; no new `Dialect` field, and nothing to edit when
    adding this dialect."""
    assert beta_header_for("computer_use") is None
    assert beta_header_for("computer_20251124") == "computer-use-2025-11-24"
    assert beta_header_for("computer_99999999") == "computer-use-2025-11-24"


# ===========================================================================
# WHAT STILL DOES NOT FIT
# ===========================================================================


def test_scroll_magnitude_cannot_be_absorbed_and_says_why():
    """`gemini-unknown6.json`: `magnitude: 999` against a 720px-tall image - a
    NORMALIZED DISTANCE. This tool's `scroll_amount` (and `Backend.scroll`'s
    `amount`) is a WHEEL NOTCH COUNT.

    NOT the coordinate problem again, and this is the substantive finding: the
    reader is now handed the `ImageSpace`, so it could compute the distance in
    pixels - and it still cannot produce a notch count, because
    pixels-per-notch is a property of the TARGET, not the vendor.
    `LinuxX11Backend.scroll` issues `amount` discrete button-4/5 events;
    `MacOSBackend.scroll` posts `amount` `kCGScrollEventUnitLine` lines. How
    far either travels depends on the focused application's line height and the
    user's scroll settings. There is no value a `Dialect` field could hold that
    would be correct, because the missing fact does not belong to the vendor.

    Closing this needs a measurement, not a type: a capture pinning
    pixels-per-notch against a real target, which then belongs on `Backend`."""
    cap = _capture("gemini-unknown6.json")
    call = cap["calls"][0]
    assert call["args"]["magnitude"] == 999
    assert call["args"]["magnitude"] > cap["image_h"]  # 999 > 720: not pixels

    dialect, calls = providers.read_call(call, ImageSpace(1280, 720))
    assert dialect is providers.GEMINI
    with pytest.raises(ValueError, match="wheel-notch count"):
        list(calls)


def test_open_web_browser_has_no_desktop_equivalent_and_says_so():
    """`gemini-task-turn0.json`: shown a Windows desktop, the model called
    `open_web_browser`. There is no such action in this tool's vocabulary."""
    fn = _capture("gemini-task-turn0.json")["candidates"][0]["content"]["parts"][0][
        "functionCall"
    ]
    assert fn["name"] == "open_web_browser"
    _, calls = providers.read_call(fn, ImageSpace(1280, 720))
    with pytest.raises(ValueError, match="no desktop equivalent"):
        list(calls)


def test_uncaptured_verbs_fail_loud_rather_than_being_inferred_from_docs():
    """Google's published list names actions this traffic never produced
    (`click_at`, `type_text_at`, ...). Encoding them would be inference, which
    is exactly what the `models` tables forbid."""
    _, calls = providers.read_call(
        {"name": "click_at", "args": {"x": 1, "y": 2}}, ImageSpace(1280, 720)
    )
    with pytest.raises(ValueError, match="unsupported Gemini computer-use action"):
        list(calls)


# ===========================================================================
# THE REAL PATH: through ComputerTool.execute, not just the reader
# ===========================================================================


class _RecordingBackend:
    """Minimal `Backend` recording where `move` actually landed."""

    name = "linux-x11"

    def __init__(self, screen_w: int, screen_h: int) -> None:
        self._w, self._h = screen_w, screen_h
        self.moves: list[tuple[int, int]] = []

    def screen_geometry(self):
        from amplifier_module_tool_computer_use.backend import ScreenGeometry

        return ScreenGeometry(self._w, self._h, 0, 0)

    def list_monitors(self):
        from amplifier_module_tool_computer_use.backend import BackendError

        raise BackendError("no monitor enumeration on this fake")

    def cursor_position(self) -> tuple[int, int]:
        return (0, 0)

    def move(self, x: int, y: int) -> None:
        self.moves.append((x, y))

    def type_text(self, text) -> None:  # pragma: no cover - never reached here
        raise AssertionError("type_text is not part of this test's path")

    def close(self) -> None:  # pragma: no cover
        pass


def _run(coro):
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)


def test_end_to_end_through_execute_lands_the_cursor_on_the_measured_pixel():
    """The measurement that matters: drive `ComputerTool.execute` - the actual
    entry point a provider calls - with the captured `functionCall`, and check
    where the backend was actually told to move.

    A reader-level test would pass even if `execute` never handed the dialect
    the coordinate space. This one would not."""
    from amplifier_module_tool_computer_use import ComputerTool

    cap = _capture("gemini-coordproof.json")
    sw, sh = cap["monitor"]
    backend = _RecordingBackend(sw, sh)
    computer = ComputerTool(backend, {"tool_version": "computer_use"})
    computer.resolve_display()
    # The tool's own resolved geometry must match the capture's, or the
    # comparison below would be measuring the fake instead of the conversion.
    assert (computer.display.model_width, computer.display.model_height) == tuple(
        cap["image_space"]
    )

    result = _run(computer.execute(cap["function_call"]))

    assert result.success is True, result.error
    assert backend.moves == [tuple(cap["real_cursor_after_normalized_move"])]
    assert backend.moves == [(1194, 181)]


def test_end_to_end_an_unmappable_verb_is_a_clean_tool_error_not_an_exception():
    """`execute` calls `read_call` OUTSIDE its try block. An eager reader would
    let this escape as an exception instead of becoming a `ToolResult`."""
    from amplifier_module_tool_computer_use import ComputerTool

    backend = _RecordingBackend(3840, 2160)
    computer = ComputerTool(backend, {"tool_version": "computer_use"})
    computer.resolve_display()

    result = _run(computer.execute({"name": "open_web_browser", "args": {}}))

    assert result.success is False
    assert "no desktop equivalent" in result.error["message"]
    assert backend.moves == []


def test_end_to_end_incumbent_anthropic_path_is_untouched():
    """Same entry point, an Anthropic payload: absolute model pixels, scaled by
    `Display.to_screen` only. Nothing the coordinate-space vocabulary added may
    perturb this - it is a live path."""
    from amplifier_module_tool_computer_use import ComputerTool

    backend = _RecordingBackend(3840, 2160)
    computer = ComputerTool(backend, {"tool_version": "computer_20251124"})
    computer.resolve_display()
    disp = computer.display

    result = _run(
        computer.execute({"action": "mouse_move", "coordinate": [398.08, 60.48]})
    )

    assert result.success is True, result.error
    assert backend.moves == [disp.to_screen(398.08, 60.48)]
    assert backend.moves == [(1194, 181)]
