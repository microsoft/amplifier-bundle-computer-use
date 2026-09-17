# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project uses [Semantic Versioning](https://semver.org/) once it reaches a
tagged release. Everything below is currently unreleased, tracked against the `0.1.x`
line declared in `modules/tool-computer-use/pyproject.toml` and
`modules/hook-computer-use/pyproject.toml`.

## [Unreleased]

### Added

- Human/agent coexistence mechanism: presence detection, an unconditional halt that
  stops the agent's writes the moment a human is detected at the machine (not
  configurable off, on any platform), and target binding that aborts a multi-event
  operation if focus changes mid-op. Design record in the design notes.
- SSH-based remote transport: drive a desktop on a different machine across a private
  network (e.g. Tailscale), with a single persistent remote agent process per session
  and screenshots downscaled to model space on the target before crossing the wire.
  Design record in the design notes.
- macOS backend for the `computer` and `desktop` tools, alongside the existing Windows
  and Linux X11 backends.
- Per-monitor display targeting, fixing coordinate scaling on multi-monitor setups.
- Linux X11 backend and a `Backend` protocol/registry seam so platform support can be
  added without touching the tool's core logic.

### Fixed

- Remote-agent scratch cleanup now uses a locked v2 lease instead of treating directory age
  as liveness. Normal exits still clean their own directory; only valid, unlocked v2 leases
  past a 24-hour minimum retention period are later reclaimed, while legacy, unknown, and
  incomplete crash residue is retained conservatively.
- Documentation that still described macOS `type_text` as an open defect six weeks after
  it was fixed in `ccf0913`. The README's known-issues list, `docs/SETUP.md`'s capability
  table and its §9 all carried the pre-fix text, including a hypothesis about the event
  tap that the fix had already disproved. `BACKLOG.md`'s "RETRACTED" entry is annotated
  too: that retraction was also wrong, for a reason worth keeping (its re-test asserted
  the screen *changed*, not what it changed to). Re-verified on real hardware
  (macOS 26.6.2, remote over SSH): Spotlight received the typed string verbatim.
- Generalised that fallback from "the sole display" to "one display at a time", which is
  what the default configuration actually needs: `target_monitor` defaults to `"primary"`,
  so per-monitor capture routes through the per-display path, not the whole-virtual-desktop
  branch - and on **macOS 26.6.2** a second attached display therefore broke *every* ordinary
  screenshot with an error blaming a display that was awake and capturable. Same guards,
  `-D <1-based ordinal>` instead of `-m`, plus an active-display-list reorder check that the
  sole-display form did not need. `-D`'s mapping to `CGGetActiveDisplayList` order is
  verified by image CONTENT on macOS 26.6.2, not by size.
- Whole-virtual-desktop capture on multi-display Macs running **macOS 26.6.2**, which hit a
  flat 30.0s `CGWindowListCreateImage` cost - also the remote transport's per-op timeout, so
  it dropped the connection instead of returning an image. Composites per-display captures
  into the identical canvas (point-space bounding box at the largest backing scale). Verified
  on a mixed-DPI rig (2x built-in beside a 1x ultrawide): same 13696x2880 output,
  30.04s -> 0.47s.
- **Native capture stays primary on every path; this module's `screencapture` work is the
  fallback.** `CGDisplayCreateImage` and `CGWindowListCreateImage` answer a capture whenever
  they are healthy, so on a macOS where they are, none of the above ever runs. Measured on
  one machine across an OS update: on **26.6.2 (25G83)** `CGDisplayCreateImage` blocked ~5.0s
  and returned `NULL` while `CGWindowListCreateImage` took 30.04s to return a correct image;
  on **26.7 (25G229)** the same calls took 0.02-0.08s and 0.07s. Those are measurements of
  those two releases - no behaviour is inferred for any other, and there is no OS-version
  comparison anywhere in the backend.
- A native call that behaves pathologically once - returning `NULL`, **or** returning a
  correct image after tens of seconds - is latched as degraded and not attempted again in
  that process. Only the duration catches the second signature. The latch is per backend
  instance and never persisted, so an OS update takes effect on the next session with no
  cache to invalidate. This also removes the ~5s-per-screenshot cost on measured 26.6.2, where the
  dead call was previously re-made on every capture.
- A whole-desktop capture that cannot set up the compositor is now **reported rather than
  answered by retrying** `CGWindowListCreateImage`: reaching that point means the native call
  was skipped as degraded or returned `None`. Re-attempting a pathological call costs ~30s on
  the measured macOS 26.6.2, which is also the transport's per-op timeout.
- The session is re-read after the health probe and before the real native capture. The probe
  is itself a native call that consumes wall-clock, so it is a window in which a screen can
  lock between the entry check and the capture that check was meant to guard.
- Added a narrow macOS fallback after native per-display capture returns `None`: one bounded
  `screencapture -m` attempt for an unchanged single active main display, with fresh
  preflight, lock/topology checks, private temporary storage, and in-memory PNG decoding.
  Verified on real hardware - macOS 26.6.2, one active 5120x1440 display, over the SSH
  production path, full-screen and region capture
  ([report](https://github.com/microsoft/amplifier-bundle-computer-use/pull/13#issuecomment-5688667363)).
  Multiple active displays are out of scope here (see PR #11), as are three or more displays
  and non-top-aligned arrangements; the topology and permission checks are non-atomic and the
  20-second budget is elapsed-time accounting, not a hard wall-clock guarantee. Adapted from
  the capture-alternative lead reported by [@colombod in PR #11](https://github.com/microsoft/amplifier-bundle-computer-use/pull/11).
- A refused fallback now names WHICH non-positive preflight result it saw - a denied Screen
  Recording grant and an unavailable preflight symbol need different actions from the
  operator, and both previously collapsed into "preflight was not positive". The diagnosis
  formats the value the refusal was decided on rather than taking a second read.
- Two silent failures blocking end-to-end remote desktop control.
- Missing `python-xlib` now reported as a missing dependency rather than surfacing as
  an X server connection failure.
- Provider request hook now fails closed if the provider gains a `stream()` method
  (rather than silently no-op'ing and leaving computer-use blind); removed a dead
  `_default_headers` mutation; PowerShell is now resolved without depending on `PATH`
  (needed for non-login SSH shells, where `PATH` does not include `/mnt/c/...`).
- Non-ASCII window titles in `desktop.list_windows` (missing `CharSet.Unicode` on the
  `GetWindowText` P/Invoke declaration).

### Changed

- Repository takeover: sources repointed, scope corrected, original attribution
  preserved (see `LICENSE` and the Credits section of `README.md`).

## Attribution note

This project began as a Windows-from-WSL2 bundle by
[@ckrabach617](https://github.com/ckrabach617); that original contribution predates the
"Unreleased" entries above and is preserved in this repository's git history. See
`README.md`'s Credits section and `LICENSE`.
