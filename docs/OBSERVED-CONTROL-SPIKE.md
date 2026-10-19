# Optional observed control

Defaults are unchanged. Explicit `tool-computer-use.config.observed_control: true`
exposes `computer.observe` and `computer.act`. This experiment requires local Linux
X11, Tesseract and the existing capture/input dependencies. OCR regions are not
semantic buttons or accessibility controls. Candidates are click, wait and
controller-only DONE/BLOCKED; no Box coordinates or new typing path are enabled.

Act resolves an owned observation/candidate into the canonical computer action.
Existing exact-action approval, read-only policy, announcement, presence/halt,
target binding and coordinate conversion remain authoritative. After approval and
presence waits, the executor verifies the same target and screen immediately
before input. Changed pixels, age over 15 seconds, retargeting or revoked proof
stop dispatch. Public observation mutation cannot rewrite stored candidates.
Uncertain canonical execution returns unknown, without replay.

Whole-screen equality is conservative. Animations, cursors and overlays may cause
frequent stale results. Human coexistence, accessibility controls, desktop typing
and other platforms need separate qualification. Existing individual tools remain
the first-class recovery path.
