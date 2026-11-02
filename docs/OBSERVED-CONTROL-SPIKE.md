# Optional observed control

Defaults are unchanged. Explicit `tool-computer-use.config.observed_control: true`
exposes `computer.observe` and `computer.act`. This experiment requires local Linux
X11, Tesseract and the existing capture/input dependencies. OCR regions are not
semantic buttons or accessibility controls. Candidates include OCR-region clicks,
typing into the currently focused field, bounded key presses (`Tab`, `Return`,
`Escape`, `ctrl+a`, `ctrl+s`, `BackSpace`, `Down`, `Up`), scrolling up/down, wait,
and controller-only DONE/BLOCKED. Typing does not establish or identify focus:
first focus the intended field. All input uses the existing canonical actions;
selectors cannot supply arbitrary click coordinates.

Act resolves an owned observation/candidate into the canonical computer action.
Existing exact-action approval, read-only policy, announcement, presence/halt,
target binding and coordinate conversion remain authoritative. After approval and
presence waits, the executor verifies the same target and screen immediately
before input. Observations older than 15 seconds are rejected at initial
validation. An initially valid proof may survive a longer exact-action approval
wait only if target identity, authority and pixels still match before input.
Public observation mutation cannot rewrite stored candidates.

Guarded typing checks the screen after the first per-keystroke presence wait,
before the first character. Later characters retain per-keystroke presence, halt,
target and active-proof checks; they do not compare against the initial pixels,
because typing itself changes the screen. Retargeting or revocation before input
stops dispatch. Interruption after a prefix may have landed returns unknown,
without replay. Unguarded typing also rechecks the proof immediately before its
canonical backend call. Uncertain canonical execution always returns unknown.

Whole-screen equality is conservative. Animations, cursors and overlays may cause
frequent stale results. Offline tests use fake capture/input and synthetic approval
and presence waits; they qualify authority and dispatch ordering, not physical
desktop operation. Live human coexistence, accessibility controls, desktop typing
and other platforms need separate qualification. Existing individual tools remain
the first-class recovery path.
