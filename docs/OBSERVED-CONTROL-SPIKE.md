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
before input. Observation age is a freshness hint, not an authorization deadline:
the executor re-captures and compares pixels on the same unchanged binding even
when selection or approval took longer than 15 seconds. A changed binding,
changed screen or failed capture refuses dispatch; elapsed time never renews a
grant, clears a halt or permits replay. Public observation mutation cannot
rewrite stored candidates. This replaces the initial 15-second rejection with
the same fresh-read requirement already used after approval waits.

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

This local-X11 change does not renew the remote-Windows operator presence
override. Its explicit grant deadline remains terminal under
[coexistence §6.0a](designs/coexistence.md#60a-intentional-policy-revision--finite-operator-presence-override).

For a focused installed-module check, run `scripts/qualify_observed_freshness.py`
with the installed interpreter in isolated mode, `--installed-root` naming its
environment and `--output` naming a new evidence directory. It refuses an ambient
`DISPLAY` and owns its Xvfb, synthetic window and state. It checks an actually
aged selection, native click delivery and changed-pixel/approval/binding/Stop
refusals, plus a deliberately lost reply after real input without replay. The
fixture supplies the candidate; this is not OCR, Unified-host, physical-device
or Windows qualification.
