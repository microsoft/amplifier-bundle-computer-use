# Human/Agent Coexistence

**Status:** Design proposal — revision 2; acceptance correction `coexistence-boundary-v3`, 2026-11-04 (§11.1). Historical design/probes are not current platform qualification.
**Scope:** How a person and the agent share one desktop — how the agent knows a human is there, how the human knows the agent is there, and how either one stops the other.
**Companion:** `docs/designs/remote-transport.md` (transport, ledger, op classes, safety boundary)
**Evidence base:** `coexistence-probes.md` — U1b, U1c, U3, U4, U5, U6, U7, **O1, O2, O5**
**Date:** 2026-08-01

> **Numbering note.** Revision 1 used `O5` for "macOS locked bit unexercised." The probes file
> subsequently used `O5` for the sustained-injection masking test. The probes file wins: **`O5`
> means the masking test everywhere in this document**, and the macOS locked-bit question has been
> renumbered **`O8`**.

---

## 1. Recommendation, up front

**Build the detector everywhere, and make it fast enough to see a human mid-keystroke. Announce the agent by whatever mechanism each platform actually proves — a persistent overlay on Linux, a system dialog on macOS. Bind every multi-event operation to the window it was aimed at. Refuse to drive, loudly, when a human is present and no announcement channel exists.**

| Piece | Where | Status |
|---|---|---|
| Presence detector (per-event inject-timestamp reconciliation) | Target-side, in the agent process, all three platforms | Build first. **O5 proves it survives sustained typing; the ±250 ms guard band did not.** |
| Halt-on-observed-human-activity (`yield`) | All three platforms by default; finite remote-Windows operator exception §6.0a | Stop on observation unless that explicit policy is active; never clear a latched halt |
| Target binding (abort on focus change mid-op) | All three platforms | **New in revision 2.** Closes a gap no indicator speed can fix (§8.6) |
| Persistent overlay + click-to-pause | Linux X11 now; Windows with transport Phase 4 | Proven on Linux (U4/U5/U6) |
| macOS announcement (system dialog) | macOS now, **zero install** | **New in revision 2.** O1 proves `osascript display dialog` renders and returns the button from a Background-domain SSH process |
| macOS agent-drawn overlay | **Impossible.** O2 tested it directly | Closed. Not a deferral — a settled negative |
| Resident macOS LaunchAgent (old "Option C") | **Not built** | O1 removed its reason to exist (§4) |

Four claims carry this, and two of them are corrections to revision 1 of this document.

1. **The detector delivers most of the safety value, and it now has the evidence to back that claim in the regime that matters.** The incident had two failures: the human was surprised (no announcement) *and* the agent confidently misdiagnosed (no presence signal). The second is the dangerous one — a system that produces confident wrong causal attributions is worse than one that produces none. O5 tested exactly the case the council said would defeat it: 98 samples during 6 s of continuous 60 ms-cadence injection, one detection, the real one, zero false positives. See §2.1 and §5.

2. **The guard band was the defect, not the mechanism — and revision 1 got the number wrong by a factor of fifty.** `GUARD = 250 ms` silently disabled human detection for the entire duration of any typing action, which is the precise failure the feature exists to prevent. The band must be single-digit milliseconds and the injection timestamp must be recorded per elementary event. This has a hard consequence for Windows that §5.5 states plainly rather than papering over.

3. **macOS is not "indicator impossible." It is agent-drawn-overlay impossible and system-drawn-dialog possible.** O1 and O2 together settle this. Those are different interaction models — *persistent ambient* versus *announce and acknowledge* — and forcing them into one abstraction would produce a worse design than admitting they are two. See §7.

4. **Pause state is owned and enforced by the injector; every other actor is a client of it.** The corollary is worth more than it first appears: the human's stop button keeps working during a network partition, because it never crosses the network. See §8.

---

## 2. Testing the brief's framing

> "Treat them as two separable problems — detection is essentially solved while visibility/interruption is the harder, asymmetric one."

**Right about the difficulty. Wrong about the independence. And it undersells the detector.**

### 2.1 Detection fixes the worse half of the incident

Replay the incident under a detector alone, no announcement:

- Agent opens Spotlight. Human is surprised — *unchanged*, that is the announcement's job.
- Human reflexively dismisses it. **If a fresh sample attributes the input as `human_active`, the guard sees it even mid-`type_text` (O5 demonstrates one such observation, not every possible phase).**
- On that observation the agent halts before its next task-directed write, invokes held-input release, and reports the observation and its basis.

In the detected case this prevents continued input conflict and supplies a reported basis instead of an unsupported causal diagnosis. It does not guarantee that every interaction is detected.

The honest limit: detection is **post-hoc and incomplete**. `yield` is a stop-on-observation invariant, not a never-collide or universal stop-after-one-collision guarantee. The guard band and sample-to-write interval can mask an event entirely (§5.2).

### 2.2 Detection measures input, not attention

Idle counters see keystrokes and pointer events. A human reading the screen for ten minutes produces none.

- The **positive** case is strong evidence: "someone else just typed" is near-certain (U1c sub-ms typical; O5 zero false positives in 97 non-human samples under adversarial conditions).
- The **negative** case is weak evidence: `idle > 5 min` means "nobody has typed," not "nobody is watching."

Two load-bearing consequences:

- **Presence is a latch, not a sample** (§5.4). Otherwise every action re-decides on a noisy instantaneous read.
- **`quiet` is never sufficient authority to do something startling** (open Spotlight, full-screen a window, switch spaces) on a machine with a live console session. It is sufficient authority to *continue* an already-announced session.

### 2.3 The detector gates the announcement requirement

This is the composition that makes the design tractable and dissolves the platform asymmetry into a policy question:

> **An announcement is required when a human is detected. It is not required when one is not.**

A macOS box with no console user (U3: `IOConsoleUsers` / `kCGSSessionOnConsoleKey` readable over SSH), or with a console user idle for forty minutes, has nobody to inform — drive it. The same box with someone typing on it needs an announcement, and — since O1 — **has one**. The refusal now bites only where no channel exists at all, which after O1 is nowhere on the three supported platforms.

**Verdict on the framing:** two mechanisms, one policy loop. Separable to build, not separable to reason about.

---

## 3. What the evidence settles

### 3.1 Settled, and load-bearing

| Fact | Probe | Consequence |
|---|---|---|
| Our own injection resets the OS idle counter on all three platforms. Windows only via `SendInput` (10235→219 ms), *not* `SetCursorPos` (18421→18656 ms) — and `SendInput` is the production path | U1b | Naive idle reading is useless. Bookkeeping is mandatory. |
| `T_now − idle` correctly identifies a genuinely independent later input; sub-ms typical, ~10 ms worst across separate process spawns | U1c | The detector is viable. |
| **The detector sees a human keystroke interleaved into sustained 60 ms-cadence agent typing.** 98 samples, 1 detection, margin +25.1 ms, zero false positives | **O5** | The council's blocking objection does not hold — **at a 5 ms threshold.** It holds completely at 250 ms. §5.3. |
| Lock/session state readable from an SSH context: Linux yes, Windows yes, macOS partial | U3 | Console-user presence is a usable second signal everywhere. |
| X11 override-redirect window: appears, does not steal focus, receives clicks without moving focus, vanishes on `SIGKILL` | U4, U5, U6 | Linux overlay is nearly free and ghost-free by construction. |
| A GNOME-Shell-composited modal can obscure an override-redirect window despite topmost X stacking | U4 (secondary) | "Mapped" ≠ "visible." Must be verified, not assumed. §7.4. |
| **`osascript display dialog` from a Background-domain SSH process renders on the console user's screen and returns which button was pressed** (`button returned:, gave up:true`, rc=0) | **O1** | macOS has a human-visible, human-answerable channel at **zero install**. It is modal and transient, not ambient. §7.3. |
| **An SSH-launched process cannot put an `NSWindow` on the console user's desktop.** Created, self-reports `visible=True`, absent from `CGWindowListCopyWindowInfo(…OnScreenOnly)` (`matches=0`) | **O2** (direct test, confirming U4's inference) | No agent-drawn macOS overlay. Settled negative — stop probing this. |
| A WSL-interop process runs on `WinSta0\Default`, session 1 | U4/Windows | Windows overlay is feasible; not visually proven (O6). |

### 3.2 Still open, in descending order of how much a decision depends on it

| # | Open question | Which decision it changes |
|---|---|---|
| **O4** | Per-platform reconciliation error and idle-counter quantisation. Windows `GetLastInputInfo` derives `dwTime` from `GetTickCount` (~10–16 ms tick); macOS `CGEventSourceSecondsSinceLastEventType` resolution is unmeasured | **Now critical, not cosmetic.** With `GUARD` at 5 ms, Windows quantisation alone is 2–3× the band. This decides whether Windows gets intra-typing detection at all before Phase 4. §5.5. |
| **O7** | Can the injector's own events be distinguished at the source rather than by timestamp? Linux XI2 virtual XTEST device; Windows `LLKHF_INJECTED` on a low-level hook; macOS `CGEventSourceStateID` | **Elevated.** A positive answer replaces the timestamp arithmetic entirely and makes `GUARD` irrelevant. This is the structurally correct detector. §5.6. |
| **O9** | Can the frontmost window/app be read from the agent's context on each platform? X11 `get_input_focus` (yes, trivially); Windows `GetForegroundWindow` (yes, but costs a spawn pre-Phase 4); macOS from Background domain (**unknown**) | Decides whether target binding (§8.6) is enforceable on macOS or only reportable. |
| **O10** | Does X `SHAPE` input-transparency (`ShapeInput`) let the overlay be visually large while only its button rects consume clicks? | Decides whether the overlay can be *noticeable* without stealing a large rectangle of screen from the agent. §7.5. |
| **O6** | Windows overlay was never rendered (U4 feasibility-only) | Whether the Windows overlay is a day of work or a week. |
| **O8** | macOS locked-bit could not be exercised (U3 PARTIAL) — *was `O5` in revision 1* | "Locked" is the cleanest "no human is looking" signal; without it macOS leans on idle + console-user. |
| **O11** | Does a distracted human actually notice the overlay? | Human-factors, not code. §7.5 no longer assumes the answer. |

**Closed and removed:** revision 1's `O1` (answered YES), `O2` (answered NO), and `O3` (`launchctl bootstrap` over SSH) — O3 existed only to cost the resident LaunchAgent, which O1 removed the reason to build.

---

## 4. What we are building, and what we are not

Revision 1 spent a full section and a nine-row matrix comparing four options. O1 collapsed that comparison, so here it is at its actual size.

**Build the detector and `yield` on all three platforms, plus the announcement mechanism each platform proves: a persistent overlay on Linux (U4/U5/U6), a system dialog on macOS (O1), and the Windows overlay folded into transport Phase 4 (U4/Windows).** Do not build a resident macOS LaunchAgent — O1 supplies the channel it existed to supply, at zero install, without reversing `remote-transport.md` §11's "no daemon, no resident component" property, without a new ghosting failure mode, and without a per-target install story. Do not treat an out-of-band alert (push/webhook) as a substitute for on-desktop announcement — a phone buzzing in another room does not make a person at the keyboard stand back — though it remains a fine *complement* for unattended machines.

**What this costs:** nothing is installed on any target; two new modules (~450 LOC); zero new resident processes; zero reversed architectural properties. **What it sacrifices:** macOS gets an interruption at session start rather than a persistent reminder, and no on-screen pause button. Input detection (§6.0) is conditional on observation, not a universal physical stop control.

---

## 5. The presence detector

### 5.1 Where the bookkeeping lives

**In the agent process on the target — the same process that owns the injection call site and the held-input ledger. Never on the controller.**

The decisive reason is clocks. The signal is `inferred_last_input = T_now − idle`, where `idle` comes from the *target's* OS counter. Comparing that against a `T_inject` recorded on the *controller's* clock requires the two clocks to agree to within the signal's resolution. Tailnet RTT is 1–8 ms and NTP skew between two consumer machines is routinely tens of milliseconds — orders of magnitude larger than the 5 ms band §5.3 now requires. Same host, same clock, no synchronisation problem. Use `time.monotonic()` on the target for both reads; only differences are ever taken.

### 5.2 Per-event timestamps, per-gap sampling

This is the substantive mechanism change in revision 2, and it is what O5 demands.

> **`our_last_inject` is updated on every elementary event, immediately around the injection syscall — not once per operation. The detector samples once in every inter-injection interval, not once per operation.**

Revision 1 implied one timestamp per op. Under that shape a 200-character `type_text` records one timestamp and reads idle once, so a human keystroke at character 47 is invisible by construction. O5's harness did the correct thing — inject, sample, inject, sample, at 60 ms cadence — and that is why it saw the human.

The masking arithmetic follows directly and should be stated rather than hidden:

```
injections at 0, P, 2P, …          detector samples just before each injection
human event at h ∈ (0, P):
    at sample time, last input is the human's;  margin = h
    detected  iff  h > GUARD
    masked    iff  h ∈ (0, GUARD]     equality is masked: production uses > GUARD

ideal uniform-phase per-event masking probability  =  GUARD / P
```

This is an **ideal model**, not a bound on all physical input. It assumes independent uniform phases, a latest-input counter that still refers to that event, and negligible sample-to-write time. An event after a completed sample but before our next emission can be overwritten by that emission before any later sample sees it. External input can also overwrite the counter. Neither effect is included in `GUARD/P`.

On Linux, `5/60 ≈ 8.3%` is conditional on that model and the **reference** 60 ms harness cadence. Production `type_text` supplies no fixed 60 ms delay. Multiple events from one interaction need not have independent phases; keydown/up may be tightly correlated. Do not square the fraction or claim 99% two-event detection without independent phase evidence.

### 5.3 The observation, and the corrected guard band

Every response envelope carries a `presence` block. Additive to the wire format, so `PROTOCOL_VERSION` bumps to `2` — cheap, because controller and agent deploy together content-addressed (`remote-transport.md` §7) and the controller already fails loud on version mismatch.

```json
"presence": {
  "state": "human_active",           // human_active | quiet | unknown
  "confidence": "high",              // high | low
  "basis": "idle_reconciliation",    // idle_reconciliation | injected_flag  (§5.6)
  "last_human_input_ago_ms": 61,
  "margin_ms": 25.1,                 // inferred_last_input − our_last_inject
  "guard_ms": 5,                     // the band actually in force on this platform
  "sample_interval_ms": 60,          // P — masking fraction is guard_ms / this
  "console_user": "user",
  "session_locked": false,           // null on macOS until O8
  "announcement": "shown",           // shown | occluded | acknowledged | none | failed
  "latched_until_ms": 59660
}
```

`confidence` is a **margin test**, not a score. Inventing a probability from one jitter measurement would be a confidence costume.

```
margin = inferred_last_input − our_last_inject

margin > GUARD   →  human_active, high     (the reconciliation attributes input)
margin ≤ GUARD   →  quiet; consult idle for confidence:
    idle > QUIET_FLOOR  →  quiet, high
    otherwise           →  quiet, low
no prior injection:
    idle > QUIET_FLOOR  →  quiet, high
    otherwise           →  human_active, high
idle unreadable   →  unknown  →  hard error for any mode that depends on it
```

**`GUARD = 5 ms` on Linux.** Not 250 ms. The basis is O5 directly: 97 non-human samples produced zero false positives at that threshold while the agent injected every 60 ms, and the one real human event was caught at `margin = +25.1 ms`. The 250 ms figure in revision 1 was derived from U1c's ~10 ms worst-case jitter and then inflated 25× "for safety" — which inverted the safety property, because the band is not a safety margin, it is **a window during which the human is invisible**. Widening it does not make the system safer; it makes the system blind for longer.

> **Two different 250 ms numbers.** The pause-latency bound in §8.4 and §12 is *also* 250 ms and is *unchanged and correct* — it bounds how long after a human clicks Pause the injector may still emit an event, and it is set by the longest atomic composite (~200 ms), not by the detector. Do not conflate the two. The guard band shrank; the pause bound did not move.

This table follows the maintained scalar-idle classifier. Readable near-zero margins are ordinary quiet observations, not unreadable evidence; low-confidence quiet is not authority or proof of absence. Structured Windows target-clock failures retain their separate unknown safety-halt behavior. Neither classification nor production thresholds change in the acceptance correction.

### 5.4 Presence is a latch

```
UNKNOWN ──(idle readable)──> QUIET
QUIET ──(margin > GUARD)──> HUMAN_ACTIVE
HUMAN_ACTIVE ──(no human-attributed input for LATCH_DECAY)──> QUIET
any ──(idle read fails)──> UNKNOWN            [hard error in modes that need it]
HUMAN_ACTIVE ──(halt invariant, §6.0)──> HALTED
any ──(human sets pause)──> PAUSED            [§8]
```

`LATCH_DECAY = 60 s` for presence observations. Once `human_active` is observed, the presence latch retains it for a minute; the guard's halt remains latched independently and does not decay into permission to write. A latch cannot recover a masked or overwritten event it never observed. Masking can therefore cost the entire interaction, not just one sample.

A human clicking the overlay's pause button can produce detectable input, but the explicit pause path must not depend on successful timestamp attribution.

### 5.5 What it costs to sample — and the Windows problem, stated plainly

| Platform | Mechanism | Quantisation | `GUARD` | Per-sample cost |
|---|---|---|---|---|
| Linux X11 | `MIT-SCREEN-SAVER` `screensaver_query_info().idle` | 1 ms | **5 ms** — proven (O5) | µs, in-process X round trip |
| macOS | `CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateHIDSystemState, …)` | See maintained `presence.py` O4 history | **10 ms** in production; not qualified by the Linux gate | µs, in-process |
| Windows | `GetLastInputInfo` target-clock observations | **10–16 ms** historical tick scale | **20 ms** in production; not qualified by the Linux gate | see below |

**The Windows limit:** the maintained 20 ms band gives an ideal `20/60 ≈ 33%` masking fraction at the 60 ms reference cadence. Structured target-clock checks narrow a timing race but are not atomic and do not eliminate masking. Linux controls do not establish Windows intra-typing qualification. Two design consequences remain:

1. **Windows presence detection at op granularity is fine and ships in C1.** Between ops, `P` is seconds, so `GUARD/P` is negligible. What does not ship is the claim that a human keystroke landing *inside* a Windows `type_text` will be seen.
2. **Windows results must not claim universal intra-op detection.** The design's proposed `"intra_op_detection": false` declaration is not a substitute for current Windows qualification. The house rule applies — the system does not claim a guarantee it does not have.

There is a second Windows cost worth stating: per-event sampling means a `GetLastInputInfo` between every keystroke, and pre-Phase-4 that would be a `powershell.exe` spawn per keystroke. It is not. **The sampling loop lives inside the bridge script**: the PowerShell side performs the typing loop, samples idle between keystrokes, halts in place on detection, and returns `{"delivered": 47, "halted_on": "human_active"}` in the same single invocation. Zero additional spawns. This is the same artifact Phase 4 wants, which is a reason to sequence them together.

### 5.6 The detector we actually want (O7)

Timestamp reconciliation is inference. Every platform exposes, in principle, a way to know *at the source* whether an event was synthetic:

| Platform | Mechanism | Status |
|---|---|---|
| Windows | `SetWindowsHookEx(WH_KEYBOARD_LL/WH_MOUSE_LL)` → `LLKHF_INJECTED` / `LLMHF_INJECTED` | Documented; needs a message loop on `WinSta0\Default` — **exactly the Phase 4 persistent process**. Unprobed. |
| Linux | XI2 device hierarchy exposes the virtual XTEST device distinctly from real hardware | Unprobed (O7). |
| macOS | `CGEventSourceStateID` on a `CGEventTap` distinguishes the agent's own source | Unprobed; likely needs Accessibility permission, which the Background domain may not have. |

If any of these works, `GUARD` becomes irrelevant on that platform, masking goes to zero, and the `basis` field in §5.3 flips from `idle_reconciliation` to `injected_flag`. **This is the structurally correct detector and the timestamp path is the fallback.** It is not on the C1 critical path — the timestamp path is proven and ships — but it is the single highest-leverage remaining probe, and on Windows it is the *only* path to intra-typing detection.

### 5.7 Remote transport staleness — measure every sample; report only sustained slowness

§5.5 states the Windows cost table on the assumption that `idle_source()` is an in-process call. Over `RemoteBackend` (`docs/designs/remote-transport.md` §5) it is an SSH round trip — on Windows, potentially with a fresh `powershell.exe` spawn per read. A historical Windows-host run (n=80, `key("shift")`, 80/80 registration) observed:

```
min=296.0  p50=781.0  p90=828.0  p95=843.0  p99=875.0  max=875.0  mean=779.6
```

That is historical evidence for measuring the live path, not a benchmark, ratio, or prediction for a new connection.

**What closes, mechanically, and what does not:**

1. **A real bug, fixed.** `PresenceMonitor.sample()` captures its `now` anchor immediately *after* `idle_source()` returns, rather than before a remote read whose value is only current near the end of that read. Explicit `now=` tests remain unchanged.
2. **Platform detection is unchanged.** `GUARD_MS`, `_classify()`, `effective_staleness_ms`, action eligibility, timeouts, and the read-only gate retain their existing behavior. Transport cost is never folded into the human-detection guard.
3. **Every successful sample remains measured.** `PresenceMonitor.sample()` records that sample's actual `transport_latency_ms`, and `effective_staleness_ms = guard_ms + transport_latency_ms` remains available in the result and a halt message. It describes that sample, rather than applying a historical connection's latency to another one.
4. **The fixed threshold reports; it does not control.** `REMOTE_TRANSPORT_WARNING_MS = 2000.0` lives in the tool composition/reporting layer. The first successful remote sample above it logs one concise measured warning per physical channel per controller process. Samples at or below it, including remote guard construction, are quiet. This observer is called immediately after the existing successful sample and cannot alter classification, a halt, pause, or release.
5. **What remains unproven.** The ordering fix removes a controller-side bias but cannot remove response transit after the target captures `idle_ms` without a target timestamp. `transport_latency_ms` remains the conservative full-call bound. No remote hardware was touched for this change; the historical 296-875ms result above was not re-measured.

### 5.8 Windows synthetic event time and syscall duration

The guarded Windows bridge sets both `KEYBDINPUT.time` and `MOUSEINPUT.time`
to the low DWORD of the same target `GetTickCount64` value captured immediately
before `SendInput`. Windows otherwise chooses the event time when `time=0`.
A slow syscall must not date our own event later than its receipt's injection
anchor and then classify that difference as outside input. The observed
recurrence had `before=37738890`, `after=37738921`,
`last_input=37738921`: a 31 ms interval against the unchanged 20 ms guard.
That trace establishes the timing conflict, not the physical author of every event.

This does not credit the syscall completion or a later `GetLastInputInfo`
value as our injection. Later input beyond the existing band still refuses;
count, epoch, tick regression, target, pause, durable halt and unknown-result
checks remain unchanged. Unguarded one-shot calls and safety-release events
retain their existing timestamp behavior. At low-DWORD zero, Windows still
applies its documented default-time rule; ambiguity is not silently credited.
Headless C# tests exercise stamping, delayed emission and later-input rejection.
They are not native emission/readback or general Windows safety qualification.

---

## 6. The handoff protocol

### 6.0 The console user's veto — default invariant

Revision 1 modelled consent entirely as the operator's decision. The person physically at the machine had no stated veto. That is corrected here, at mechanism level:

> **By default, once the guard observes `human_active`, it prevents the next task-directed emission, invokes the existing release path, and retains the halt, in every mode and on every platform. The sole live-classification exception is §6.0a. No configuration or controller action clears an already-latched halt. Required safety-release emissions are not task-directed writes.**

Concretely: the halt is enforced in the same guard as pause and target binding, at the injection call site (§8.6). `drive_anyway` in §7.6 still governs START only; `enabled:false` still declines disclosure only. Neither is a continuing-presence override. The intentional policy revision below replaces the earlier absolute “no configuration” claim, not the default behavior.

Outside §6.0a the invariant is unconditional **after observation**, not after every physical input event. If the product requires every physical event to prevent the next ordinary emission, that stronger requirement is **UNMET** by the timestamp mechanism. It needs future mechanism work (for example §5.6), not a lower acceptance threshold. Explicit Stop, pause, authority and durable-halt behavior are unchanged.

### 6.0a Intentional policy revision — finite operator presence override

An operator may explicitly authorize unattended input on one remote Windows channel even when aggregate idle reconciliation classifies other activity. `coexistence.operator_presence_override` is **absent/default-off**, not inferred from quiet, a screen, a file, a tool result, or an agent's claim of approval. Only the trusted mount/configuration owner supplies it after obtaining actual operator authorization. Its reason is an operator label, **not authenticated proof of authority or input authorship**. A warning logs the channel, deadline and reason at guard construction.

The mapping has exactly three keys: `channel` (the exact `_halt_key(backend)`, including SSH user/host and nondefault port), `expires_at` (finite absolute Unix seconds, strictly future and at most 3600 seconds from construction), and a printable nonempty `reason` (at most 160 characters). Booleans, strings as numbers, unknown keys, local/non-Windows backends, wrong channels and missing target binding are refused. Controller wall and monotonic deadlines both apply. Removing/changing that mount mapping or changing the backend channel invalidates the current policy. Observed expiry/revocation latches an unknown safety halt and invokes release before another write; adding it back cannot resume that guard.

Only the live `HUMAN_ACTIVE` halt decision in `before_event` and receipt acceptance is suppressed. Initial and post-result samples, classification, latches, margins and transport reporting remain unchanged. The policy never credits a foreign input as an own injection. UNKNOWN/unreadable observations, malformed/lost/rejected receipts, epoch or tick regression, failed SendInput counts, locked/missing GUI sessions, target binding, geometric exclusion, permission/read-only/write gates, disclosure, explicit pause, Cancel and host Stop are **not overridden**. The policy cannot ignore or clear a current, durable, legacy or newly polled halt; the existing explicit resume procedure and a fresh guard are still required.

The invocation-local Windows condition uses **condition version 2**, `operator_presence_override:true`, exact `operator_presence_channel`, `operator_presence_expires_at`, and `operator_presence_target`. The existing Windows clock observation includes its foreground-window identity; the operator policy binds on the first fresh sample of each operation and checks subsequent samples/receipts against it. This works with the real `RemoteBackend`, which has no `current_target` method, without adding a window-read RPC. Missing/zero foreground identity refuses input. The bridge validates types and the finite future deadline, retains epoch/tick checks, rechecks the bound foreground, and skips **only** the last-input-versus-own-baseline predicate while the policy is active. It checks wall and target-tick expiry immediately before each physical write. Condition/receipt forwarding remains mandatory; it is refreshed before controller forwarding and after each receipt so composites retain the flag, binding and deadline. An older v1-only bridge refuses v2 **before input** (or lacks required foreground evidence), without an unconditioned retry. Observation/receipt version stays 1. No new RPC, threshold widening, baseline reset, attribution guess or foreign-event credit is introduced.

Revocation and Stop cannot undo input already in transit or in a native call. Controller revocation is enforced before its next forwarded request; target expiry bounds already-forwarded composites. Existing target emergency-release/finally and retained-on-failure ledger paths remain in force, even when the policy expires. This is not a general target-side instantaneous Stop protocol.

`as_dict` reports the configured scope/deadline/reason and currently active fact, explicitly `authority_verified:false`. Cached diagnostics report only the active-at-enforcement boolean beside the unchanged classification; reading them samples no clock or target and does not assert current activity. Focused offline tests qualify policy, real forwarding and inert C# 5 predicate behavior only. Actual Windows PowerShell 5.1/native input and the user's task still need the separately owned device trial. Historical qualification receipts and the stopped Linux campaign are unchanged.

### 6.1 `yield` — halt and stay halted (default)

On the first `human_active` observation at high confidence:

1. **Halt before the next elementary event** (§8.4 for composites).
2. **`ledger.release_all(reason="yielded")`** — every held key and button, immediately, locally.
3. **State becomes `HALTED`.** Every subsequent write op fails fast with a structured, unmistakable error — never a silent stall, never a generic failure.
4. **The model is told exactly what happened and how it was determined**, including `margin_ms` and `guard_ms`. This is the sentence the incident lacked.

**Resume after yield is `manual` by default.** The alternative — resume after N seconds of inactivity — resumes while the human may simply be reading, which §2.2 establishes is invisible to the detector. `after_inactivity(<seconds>)` is offered as an explicit policy for unattended machines where a passer-by jogging a mouse should not end a long run. **User decision — §13, D3.**

### 6.2 `wait_for_quiet` — wait for inactivity

Blocks until `quiet` at high confidence for a continuous window `Q`. Needs no channel to the human, so it works on every platform.

- **Bounded slice.** The op returns after `min(Q_remaining, slice)` — e.g. 15 s — with `{"waited_ms": …, "state": "human_active", "proceeded": false}`, and the model retries. It never blocks inside one tool call for five minutes: that would stall cancellation (`remote-transport.md` C4 exists precisely to avoid this) and would produce a transcript in which the agent appears to have done nothing.
- **Hard ceiling.** After `max_wait`, fail with "human still active after N s," not "proceeding anyway."

### 6.3 `request` — recommended **out of v1 scope**

Revision 1 specified a third mode: the agent asks permission for a specific action, with a timeout and optional auto-takeover. One council lens argued it should not exist at all, on the grounds that it never traced back to the incident. **That objection is correct and this revision acts on it.**

The incident is fully addressed by: detector + `yield` + announcement. `request` adds a per-action consent protocol, a countdown, timeout semantics that O1 has now shown to be genuinely ambiguous (§7.3), and an auto-takeover safety argument — all for a use case that has never been demonstrated. O1 made `request` *cheap* on macOS; it did not make it *needed*. Cheap and unneeded is still complexity.

**Recommendation: ship `yield` and `wait_for_quiet`. Do not ship `request` or per-action auto-takeover in v1.** Re-open it when a real blocked-session case appears, at which point O1 means the macOS channel is already there.

What survives from §6.3 of revision 1 is its one genuinely good idea — *a timeout may only grant permission if presence is `quiet` at the moment it expires* — which is retained and reused in §7.3 for the session-start announcement, where it does real work.

**This is a live disagreement and it is surfaced as a user decision — §13, D1.**

### 6.4 Mode availability

| Mode | Linux | Windows | macOS |
|---|---|---|---|
| Halt on detected human (§6.0) | ✅ invariant | ✅ invariant (op granularity; intra-`type_text` pending §5.6) | ✅ invariant |
| `yield` | ✅ | ✅ | ✅ |
| `wait_for_quiet` | ✅ | ✅ | ✅ |
| `request` | *out of scope, v1* | *out of scope, v1* | *out of scope, v1* |

---

## 7. Announcing the agent

### 7.1 It is a control where it can be, and a disclosure where it cannot

Where the platform supports a persistent overlay, the announcement carries the pause button — and then **an announcement that failed to render is a stop button the human cannot reach.** Liveness becomes a safety precondition, verified rather than assumed (§7.4, §9.1).

Where the platform supports only a modal dialog, the announcement carries no pause button, and the human's stop control is §6.0 — their own hands. That is not a degraded overlay; it is a different mechanism with a different guarantee, and the design says so rather than pretending at parity.

### 7.2 Per session, not per action

The announcement is raised when a driving session begins and lowered at session end. It is **not** shown and hidden per action. Per-action flicker is worse UX, and per-action state changes are only free because of a structural fact worth stating:

> **The announcement lives on the target, beside the injector. It never crosses the network.**

If the controller drove it, every action would cost a round trip and a serialisation point. The only thing that crosses the wire is state, and it crosses lazily — the controller learns of it on the next response.

### 7.3 Per platform — two genuinely different models

**Linux X11 — persistent ambient. A thread in the agent process.** Override-redirect window, one per monitor (keyboard focus is global and cannot be partitioned). Proven: appears without stealing focus (U4), receives clicks without moving focus (U5), sub-millisecond map/unmap (U6), and **ghost-free by construction** — the X server destroys the window when the connection dies, even on `SIGKILL` (U6). No IPC: the overlay and the injector are the same process, so pause is a variable, not a message.

**Windows — persistent ambient, deferred to Phase 4.** A WSL Python process cannot create a Win32 window; `powershell.exe` is the process on `WinSta0\Default` (U4/Windows). The Windows overlay wants exactly the artifact transport Phase 4 already wants — a long-lived PowerShell reading NDJSON on stdin — and so does the §5.5 in-bridge sampling loop and the §5.6 injected-flag hook. **Three pieces of work want the same process. Sequence them together.**

**macOS — announce and acknowledge. `osascript`, zero install.** O1 settles the mechanism; O2 settles what is *not* available.

- The channel is a **system-drawn modal dialog**, not an agent-drawn window. The agent cannot place pixels on that desktop (O2, direct test) and should stop trying.
- It is **modal and transient**: it interrupts, takes focus, is answered, and disappears. It cannot be a persistent "AGENT DRIVING" reminder.
- It **returns the button pressed** (`button returned:` on the answered path), so it is a complete round trip, not one-way output.

Therefore macOS gets **one announcement at session start**, before the first write, with a real decision point:

```
This Mac is about to be driven by an automated agent
  ("<session label>", from <controller host>).

This prompt closes in 30 seconds.
If nobody answers and this Mac is idle, driving will start.
If nobody answers and someone is using this Mac, driving will NOT start.

              [ Don't allow ]   [ Allow ]
```

**Resolving the `gave up` ambiguity.** O1 returned `button returned:, gave up:true` — a countdown the human was never told about, mapping to a result that means neither yes nor no. The council called that worse than either pure failure mode, and it is. Three rules fix it:

1. **The timeout is disclosed in the dialog text.** The house rule against undisclosed timers applies to the dialog itself, not only to the protocol. The text above states the duration and both outcomes. A countdown the human can read is not an undisclosed countdown.
2. **`gave up:true` is never consent.** At the wire level it is unambiguous — the answered path returns a button name, the timeout path returns an empty `button returned:` — so the agent can always tell them apart. The *semantic* ambiguity is resolved by declaring the mapping rather than inferring it: **timeout is not an answer.**
3. **What a non-answer permits is decided by the detector, not by the clock.** At timeout, sample presence. `quiet` → nobody is there to have been asked, proceed. `human_active` or `unknown` → treat as **Don't allow**. This is §6.3's one good idea, doing real work: the timeout never grants permission on a machine somebody is using.

Two honest notes. First, this dialog **steals focus** — it is an interruption. That is deliberate and it is the opposite of the incident: the incident's harm was an *undisclosed* surprise with no decision point, and this is a *disclosed* interruption that is entirely a decision point. Second, after it is dismissed macOS has **no persistent reminder** that the agent is driving. The mitigation is §6.0 — the human's hands stop the agent at any moment, no button required. Whether that is sufficient is **user decision D2 (§13)**.

### 7.4 "Mapped" is not "visible"

U4's secondary finding — a GNOME-Shell-composited polkit modal obscured a window `query_tree` confirmed was topmost among client windows — means X stacking order is not a visibility guarantee on a compositing desktop.

**The overlay verifies itself with the capture path the agent already owns.** After raising, capture its own rectangle and check for expected pixels. Report `shown` / `occluded` / `failed`. This turns an assumption into a measurement using machinery that already exists.

Cadence: on raise, and on a slow timer (~30 s) — not per action. A small region capture is cheap but not free, and per-action verification would put a screenshot on the input hot path.

`occluded` is reported to the model and treated by policy as *not shown*: it halts the same as `failed` wherever an announcement is required.

### 7.5 The agent must not be able to click its own Resume — and the overlay must still be noticeable

U5 is double-edged. It proves the overlay receives synthetic clicks — which proves the control is reachable, and simultaneously proves **the agent can reach it too**. An agent that can click its own Resume has no pause at all.

**Defence: geometric exclusion, enforced at the injection call site, in the same guard as pause and target binding (§8.6).** The injector refuses to emit any event whose coordinates fall inside a registered control rect. Certain, platform-independent, no dependency on O7, and — because it is in the injector rather than the overlay — it holds even if the overlay's own event handling is buggy. Device-origin filtering (§5.6) is strictly stronger in principle and would be a welcome addition, but it is unprobed and X11-only today, so it is not the load-bearing defence.

**Revision 1's "keep the indicator small, in a corner" was reasoning from render cost to a UX conclusion, and the council was right to call it out.** The excluded area must be small (it is screen the agent cannot click). The *visible* area must be large enough that a distracted human notices it. Revision 1 resolved that tension by shrinking both, which optimised the wrong one — a pause control nobody notices is a pause control nobody presses.

The two constraints separate cleanly if input and pixels are decoupled:

> **Only the interactive button rects are click-excluded. The status band is input-transparent.**

X `SHAPE` with `ShapeInput` sets a window's input region independently of its visible region, so the status band can be as large and as loud as it needs to be while passing clicks through to the application underneath, and only the small Pause / Cancel rects consume input and enter the exclusion list. **This is unprobed here — O10** — and it is a small probe. If it fails, the fallback is a large visible band that *does* consume clicks, and the exclusion list grows to match; that costs the agent screen area and is the honest price of noticeability.

**Whether the result is actually noticeable to a distracted human is O11, and it is a human-factors test, not a code test** — put the overlay up and interrupt somebody mid-task. No amount of pixel arithmetic answers it.

Consequences to state plainly: the excluded rects are screen the agent cannot click, and the overlay **will appear in the model's screenshots**. Leave it there — the model and the human seeing the same reality is a feature. Describe the exclusion zones in the tool description so the model does not waste turns trying to click them.

### 7.6 Driving with no channel

After O1 there is no supported platform without an announcement channel, so this path should now be rare. It still needs to exist for the cases that remain — a Linux target where the overlay reports `failed` or `occluded`, a Windows target before Phase 4, a macOS target where `osascript` returns non-zero.

> **Human detected + no working announcement channel → refuse to write.** Overridable only by an explicit per-target opt-out that is logged every session.

The opt-out governs *starting*. It does not, and cannot, override §6.0.

**Defect fix, post-revision-2:** `coexistence.enabled: false` used to skip building the whole coexistence layer (halt, pause, target binding, exclusion, *and* disclosure) in one boolean, at `logger.info` - which made C1 acceptance item 6 false (a config key *could* disable the halt, by never building the guard that enforces it). Fixed: whenever a backend structurally supports presence detection, the guard is now built unconditionally - no config key of any kind can prevent that. `enabled` was not deleted; it was narrowed to an alias of `announce` (declining session-start *disclosure only*), and both keys are now gated the same way this section already gates a technically-failed channel: a real-time presence check refuses to *mount* (not merely to write, closing a related gap where a declined disclosure surfaced only as an ordinary-looking tool error on first use) when a human is already detected present, and proceeds loudly (never silently) when nobody is there. See `_disclosure_decline_reason`/`_refuse_if_disclosure_declined_with_human_present` in `__init__.py`.

---

## 8. Pause, cancel, and target binding

### 8.1 Where the state lives — U7, corrected

U7 concluded that pause must be enforced by the process owning the injection call site, and observed that on Linux this is also the overlay owner. On macOS they are forced apart. The precise rule:

> **The injector owns and enforces pause state. The announcement is a privileged local client that may set it. The controller may only observe it.**

| Actor | Set pause | Clear pause | Read pause |
|---|---|---|---|
| Human, via the overlay | ✅ | ✅ | ✅ |
| Human, via their own input (§6.0) | ✅ (halt) | ❌ | — |
| Agent / injector | (self, on `yield`) | ❌ | ✅ |
| Controller / model | ❌ | ❌ | ✅ |

**Only the human clears a human-set pause.** If the controller could clear it, pause would be a suggestion — a buggy controller unpauses on its next action and the human's stop did nothing. There is no `pause` or `resume` op the controller can send.

The operator at the controller has an independent stop path: cancel the tool call or the session. Two humans, two independent stop mechanisms, neither dependent on the other.

### 8.2 The property this buys

**The human's stop button keeps working during a network partition**, because it is enforced locally and needs no controller. If pause lived on the controller, the stop button would be dead exactly when the human most wants it — mid-drag, link down, agent still holding a mouse button.

### 8.3 How the agent learns it is paused

It is **told**, never left to infer. Three layers, all required:

1. **The in-flight op returns a distinct structured error** — type `Paused`, with partial-progress detail where one exists (`"typed 47 of 200 characters"`). Not a generic failure, not `success=True`.
2. **Every subsequent op fails fast with the same error.** No silent no-ops. The model must never be in a position to ask "why did my clicks stop landing" — the exact epistemic hole that produced the TCC misdiagnosis.
3. **The `presence` block on every result** carries the pause state, its setter, and its timestamp.

Optionally (policy, not mechanism): a `tool:post` hook turning a pause into an `inject_context` system message so the fact survives across turns. Use the kernel's existing mechanism (`HOOKS_API.md`); do not build a second one.

### 8.4 Held inputs, in-flight actions, and drags

**Held inputs: released immediately, via the existing ledger.** `release_all(reason="paused")`. A pause that leaves Shift held is worse than no pause. Non-negotiable.

**Pause is checked between elementary events, not between ops** — the same cadence the detector now samples at (§5.2). A 200-character `type_text` that only checked at op boundaries would ignore the human for the whole string.

**Except for tightly-timed composites.** A `double_click` is two clicks inside an OS-defined timing window; pausing between them silently converts it into a single click, which many applications treat as a different action. So: **complete the composite (≤ ~200 ms), then honour the pause.** Pause latency is bounded by the longest atomic composite, not the longest op — hence the **< 250 ms** bound in §12, which is unrelated to the guard band (§5.3).

**In-flight drag: the drag ends where the pointer is, and this is reported loudly.** The button must be released (safety). Moving the pointer back to the drag origin first would be *driving the machine during a pause*, which contradicts what pause means. So the release happens in place, and the result says so explicitly, because dropping a file somewhere unintended is a real outcome the human needs to know about. There is no clean answer here; this is the least-bad one, and pretending otherwise would be worse.

### 8.5 Resume and cancel are different

| | **Pause** | **Cancel** |
|---|---|---|
| Agent process | Stays alive | Torn down |
| Overlay | Stays up (so the human can resume) | Vanishes — on X11 by construction (U6) |
| Held inputs | Released | Released |
| Reversible | Yes, by the human only | No |
| Model is told | "Paused by human at `<host>` at `<time>`" | "Session cancelled by the human at `<host>`" |

Cancel is terminal and unambiguous. It is not a long pause.

### 8.6 Target binding — the gap no announcement speed can fix

**New in revision 2.** The council identified a failure mode this design previously had no answer for:

> If the human switches window focus mid-`type_text`, the remaining keystrokes land in the wrong application — as a *side effect of the human's own action*, with no perceivable decision point and nothing for the human to have noticed or clicked.

A faster indicator does not help. A faster detector does not help either: the human's focus change may produce no input the detector attributes to them within the same interval, and even when it does, the damage is the keystrokes already queued behind it. This needs a mechanism guarantee, not a latency improvement.

> **Every multi-event operation is bound to a delivery target at its start. Before each elementary event, the injector re-reads the current target. If it changed, the operation aborts and reports.**

Concretely, in the same guard that already checks pause, halt, and geometric exclusion:

```
before each elementary event:
    if halted or paused:                 → stop, release_all, report          (§6.0, §8.3)
    if coords ∈ any excluded rect:       → refuse, report                     (§7.5)
    if current_target ≠ bound_target:    → stop, release_all, report          (this section)
    else emit
```

Target identity per platform, and its cost:

| Platform | Target identity | Cost per event | Status |
|---|---|---|---|
| Linux X11 | `get_input_focus()` window id (+ `_NET_ACTIVE_WINDOW` where present) | one X round trip, µs | Available now |
| Windows | `GetForegroundWindow()` HWND | free **inside the bridge loop** (§5.5); a spawn per event outside it | Rides the same in-bridge loop as sampling |
| macOS | frontmost app — mechanism from the Background domain **unverified (O9)** | unknown | **See below** |

**The abort is unconditional and dumb, on purpose.** It trips on *any* target change, including one the agent's own keystroke caused — an autocomplete popup, a dialog opened by Enter. That will occasionally abort a benign operation and cost a turn. That is the accepted price: distinguishing "focus change the agent caused" from "focus change the human caused" requires exactly the causal attribution this whole design exists because the agent gets wrong. The error names both the old and the new target so the model can re-issue deliberately against the new one if that was the intent. **Fail loud beats clever.**

**Where binding cannot be enforced, it must be declared.** If O9 comes back negative for macOS, `type_text` on macOS reports `"target_binding": "unverified"` in its result, and long strings are chunked so the blast radius of a mid-op focus change is bounded by the chunk rather than the string. The house rule again: the system does not claim a guarantee it does not have.

---

## 9. Failure modes

### 9.1 Announcement dies while the agent is driving

- **Linux:** impossible in the recommended shape. The overlay is a thread in the injector; they die together, and U6 proves the window dies with the connection.
- **macOS:** not applicable — the dialog is transient by design and is not expected to persist. Its failure mode is `osascript` returning non-zero at session start, which is handled at §7.6 before any write occurs.
- **Windows (post-Phase 4):** the persistent PowerShell process is the same process that performs injection, so its death stops driving by construction — the same property Linux has.

Note what the O1-based design bought here: revision 1's Option C would have added a resident helper whose death had to *stop driving*, converting a two-part system into a three-part one with a new mandatory dependency and a new ghosting mode (revision 1 §9.2). None of that exists now.

### 9.2 Agent dies with an announcement up

- **Linux:** cannot happen. Same process; U6 proves ghost-free teardown even on `SIGKILL`.
- **macOS:** the dialog is modal and answered before driving starts; there is nothing to ghost.
- **Windows:** same-process after Phase 4.

### 9.3 Network partition mid-drag

Already covered by the existing ledger — stdin EOF plus the 5 s deadman, both verified (`remote-transport.md` §3.3, §10.2). Coexistence adds nothing that can break here and one thing that helps: **the human's pause still works** (§8.2). The deadman fires at 5 s and releases everything, so a partition and a pause converge on the same safe state from two independent directions.

### 9.4 Human pauses during a multi-step action

§8.4. Partial progress reported honestly with a count; composites complete; drags end in place and say so.

### 9.5 Human changes focus during a multi-step action

§8.6. The operation aborts at the next elementary event, releases held inputs, and reports both targets and the delivered count.

### 9.6 The detector itself fails

`idle` unreadable → `state: unknown`. Under the house rule this is **not** treated as `quiet`. Every mode that depends on presence fails loud. There is no "assume nobody's there" path — that assumption is exactly how the incident happened.

### 9.7 Human input masked by the guard band

Residual, not eliminated (§5.2). `GUARD/P` describes only the ideal uniform-phase model; it is not the whole blind window or a real two-event guarantee. The latch (§5.4) retains a caught event but cannot reconstruct one overwritten before sampling. Source attribution (§5.6) is future mechanism work, not established elimination of every race.

---

## 10. What this costs

### 10.1 Latency

| Path | Cost | Notes |
|---|---|---|
| Presence sample, Linux | µs per elementary event | In-process X call. At 60 ms cadence this is noise. |
| Presence sample, macOS | µs per elementary event | In-process Quartz call |
| Presence sample, Windows | **zero extra spawns** | Sampling loop runs inside the bridge script (§5.5); returns halt-in-place in the same result |
| Target-binding read, Linux | one X round trip per event | Same order as the presence sample |
| Target-binding read, Windows | zero extra spawns | Same in-bridge loop |
| Overlay raise/lower, Linux | 0.28 ms show / 0.31 ms hide (U6) | Local X IPC only, **zero network hops** |
| macOS announcement | one `osascript` invocation per session | Blocking, modal, ≤ the disclosed timeout. **Session start only — never on the injection path** |
| Overlay self-verification | one small region capture, ~30 s | Deliberately off the per-action path |
| Pause / halt / exclusion / binding enforcement | zero | Four variable reads in one guard at the injection call site |

**Per-action cost of the announcement is zero.** Per-action cost of the detector is one in-process syscall on Linux and macOS, and zero additional process spawns on Windows.

### 10.2 Moving parts

| | New modules | New processes | New installs | Reverses a stated property? |
|---|---|---|---|---|
| **This design** | 2 (~450 LOC) | 0 (Linux thread; Windows rides Phase 4; macOS `osascript` is transient) | none | **no** |
| Rejected resident macOS helper | 3 (~700 LOC) | 1 resident per macOS target | LaunchAgent plist + PyObjC or signed binary | **yes** — "no daemon, no resident component" |

### 10.3 Protocol surface

`PROTOCOL_VERSION` 1 → 2. Additive:

- Every response envelope gains `presence` (§5.3).
- New **READ** ops: `presence`, `announcement_status`.
- New **CONTROL** ops: `announce_raise`, `announce_lower`.
- **No new WRITE ops, and deliberately no controller-settable `pause`/`resume`** (§8.1).
- Write-op results gain `target_binding` and, on Windows `type_text`, `intra_op_detection` (§5.5, §8.6).

The existing `classify_op` policy applies unchanged. Because controller and agent deploy together content-addressed with fail-loud version checking, the bump costs nothing operationally.

---

## 11. Phasing

**Phase C1 — the detector and the halt invariant, everywhere.** `PresenceMonitor` at the injection call site; per-elementary-event inject timestamps and per-gap sampling (§5.2); per-platform `GUARD` from O4; latch state machine; `presence` block on every response; protocol bump to 2; the §6.0 halt invariant with ledger release and a structured error. Windows sampling loop inside the bridge script. **Ships the fix for the misdiagnosis.** No overlay, no new processes, no installs.

Evidence required to call C1 done — item 2 follows the explicitly scoped acceptance revision in §11.1:

1. On each platform, an independent process producing input is detected as `human_active` with `margin > GUARD`, and the agent's own injection three seconds earlier is not.
2. **Five required deterministic mechanism controls with independently placed input.** The Linux harness runs each once: six seconds of quiet own typing at the 60 ms reference cadence; nominal 2 ms inside-band input; and nominal 8/15/55 ms outside-band input before a fresh sample. A fresh `QUIET`/high-confidence baseline with finite idle strictly above the quiet floor is mandatory for each case. Child readiness, matching request IDs, send-before/sync-after intervals, counter registration, sample entry/return and acknowledged own-write/injection anchors establish placement; nominal sleeps do not. Missing, straddling or unplaceable required evidence is `INVALID` and the run `UNQUALIFIED`, never retried or excluded. Quiet controls require no false positives. Proven outside-band input must halt before the next task-directed emission, invoke release, and refuse another attempted write. Inside-band detection or masking is allowed; a miss is not required. The artificial post-sample/pre-write IPC barrier runs separately as a diagnostic under §11.1, not as a sixth required mechanism control. It discloses exposure and attribution limits, not a measured natural blind-window width or a must-detect/must-miss promise. Headless exact `G−ε`, `G`, `G+ε` controls test the production classifier/guard; scheduling cannot establish native equality. Other platforms need their own qualification; Linux success does not qualify Windows or macOS.
3. After an observed `human_active`, `yield` prevents the next task-directed write, releases everything held (`RELEASED:` lines in agent stderr), and the model receives an error naming the observation, margin and guard. The boundary harness checks callback invocation only; real ledger release and mounted authority/Stop paths still need their own evidence.
4. Windows presence read adds **zero** additional `powershell.exe` spawns per action — measured, not asserted.
5. Idle unreadable → `state: unknown` → the dependent mode fails loud. No path treats it as `quiet`.
6. The default §6.0 halt invariant remains unchanged, including `drive_anyway`/`enabled:false`; verify the explicitly authorized, default-off remote-Windows §6.0a exception separately, including expiry/revocation, target forwarding and all independent refusal/release paths.

### 11.1 Acceptance correction — 2026-11-04, `coexistence-boundary-v3`

The original maintained 100-trial result, **89 detections / 11 misses / zero false positives, remains FAILED under its original conjunctive policy** (fixed ≥90% detection AND masked fraction ≤ `5/60 + 2σ`). A later check must not relabel it. With independent uniform phases and `q=5/60`, `P[Binomial(100,q) ≥ 11] = 0.210558…`: the fixed-rate rule rejects about 21.06% of model-consistent runs. Repeating until green, lowering 90% to another fixed rate, or treating the noise bound as overriding the failed rate is not a correction.

Main's bounded paired comparison is attributed evidence, not execution by this patch: 12 trials / six paired conditions matched baseline `673cd63f` and PR36 `fe61c545` (accepted source `bf3adece`, identical tree `97ab56bb`). Quiet continued; ~2 ms was masked in both; 15/55 ms halted both; the forced post-sample barrier was masked in both. Across 180 quiet checks per version, sample-return to guard-return means were 2.45/15.90 µs and maxima 18.30/60.61 µs. No many-millisecond diagnostic regression was reproduced. This neither explains every historical miss nor justifies suppressing Linux diagnostics. The forced barrier is artificial.

The corrected gate qualifies the five conditional mechanisms in item 2, **not the stronger universal physical-event requirement in §6.0**. The additive receipt retains schema `coexistence-qualification-v1`, the policy ID, unique run ID, source hashes, predeclared cases and bounded timelines in a new, non-overwriting file. `required_case_ids` and `diagnostic_case_ids` declare the split; `cases` holds the five required results, `diagnostics` the barrier. `required_controls_status` reports required results independently; `diagnostic_status` is `OBSERVED`, `LIMIT` or `UNQUALIFIED` (also when not reached). Top-level `MECHANISM_CONTROLS_PASS` (exit 0) requires the five required results to pass, a validly observed or precisely unresolved diagnostic, and successful cleanup. It is not all-six PASS, adoption or general safety; generic `PASS` is not a successful top-level verdict. Required failure/invalidity, any diagnostic safety failure, other diagnostic invalid/unknown evidence or cleanup failure keeps the run `UNQUALIFIED` (exit 2). Main retains review, publication, current evidence and adoption ownership; Windows retains the original trial. No guard, permission, Stop, pacing, threshold or diagnostic-recording behavior changes.

**Artificial diagnostic separated, not historical evidence rewritten.** Main's independently reviewed, retained run at `7ea71f22a1253a78ad264631ec3e65f73e97a5e1` remains **overall UNQUALIFIED / exit 2**. Its 101 quiet emissions spanned 6.14 s, and quiet/inside/8/15/55 ms controls passed. In the barrier, delivered child motion and own key release shared server timestamp **291045271**, and the last-input bracket had that same value. Distinct resets were not attributable. This is not proof of an observed-input halt violation. V3 makes that artificial attribution case diagnostic, without revising any retained result, raw receipt, earlier failure, invalid, unknown or incomplete outcome. The native campaign is **stopped**; this source correction uses offline checks only, not another native run.

A narrowly typed shared-tick diagnostic may report **`status: LIMIT` / `attribution: ATTRIBUTION_UNRESOLVED`** only after valid matching child/own native batches, ordered receiver identities, valid prior quiet observations, independently corroborated child registration, completed-sample → child send/sync → own emission ordering, and consistent server-clock evidence. The own bracket must collapse to the shared child/release timestamp, strictly beyond the pre-child sample's prior-input interval. This supports native delivery and clock consistency, **not distinct own counter advancement**; the own read is never marked corroborated. The diagnostic ends at that evidence without a further write to resolve ambiguity. Missing, wrong, malformed, replayed or extra input, receiver/clock faults, inconsistent observations, time ordering or other/broader ambiguities remain invalid and the run unqualified; no catch-all `InvalidCase` conversion is allowed. A valid diagnostic with distinct advancement reports **`OBSERVED` / `ATTRIBUTION_RESOLVED`**, not required-control `PASS`. Earlier safe detection in its follow-up is allowed; no must-mask or must-detect outcome is imposed. All observed-activity halt/no-ordinary-input, same-call release and subsequent refusal rules remain unconditional and override diagnostic labels or later errors. Proven outside misses remain FAIL.

The harness enforces the observation invariant on **every** guarded row, including inside-band and barrier follow-up observations: `human_active` requires a corresponding halt, a release callback identified with and timestamped inside that guard invocation, no ordinary emission in that row or later rows, and a subsequent refused attempt. A retry's release cannot conceal an omitted initial release. A separate Xlib receiver connection owns a mapped, focused window at (0,0), covering the child's coordinates. Existing unshifted level-0 `a` mapping is mandatory; no scratch mapping, grab or backend patch is introduced. Setup is structural, not warmup input. Each empty-queue-armed batch must contain exactly the matching own press/release pair or child motion, with correct window/detail/coordinates, `SendEvent=False`, and new receiver ordinals. Ordinals identify reception order, not X sequence numbers or timestamps. Stale, extra, missing, wrong or replayed batches are invalid. Every attempted backend call, including a refusal or exception, checks the receiver. Ordinary native input after activity/halt is **FAIL**, even if no emission callback was recorded and later corroboration fails. Proven pre-sample outside-band quiet responses also remain **FAIL**, not excused by a later registration error.

Quiet control scheduling anchors to the first acknowledged own emission and then to each previous acknowledgement, without catch-up bursts. Actual acknowledgements must span at least 6 s across 101 units; each acknowledgement-to-next-send gap is at least 60 ms and acknowledgement-to-next-acknowledgement gap at most 80 ms. These fixed harness bounds are not production latency promises. An early wake, excessive lateness, uncorroborated unit or shorter span is invalid, not a weaker qualifying run. All six case counts, one child's lifetime and per-unit evidence remain bounded.

**Receiver traffic correction.** Main's separate native attempt at PR38 `1120634` remains **UNQUALIFIED** after 2.83 s: SYNC/SERVERTIME discovery succeeded, but the first quiet unit exceeded a pre-classification queue limit, without retaining its count/types; registration and later controls were not reached. A subsequent owned-fixture observation retained four actual events: native `MappingKeyboard`, native `MappingModifier`, `KeyPress`, `KeyRelease`. The first two are documented, globally delivered [X11 MappingNotify notifications](https://www.x.org/releases/current/doc/xproto/x11protocol.html), not extra device input. The receiver now validates only this notification type separately, refreshes keyboard mappings through python-xlib, and requires the original expected keycode and current level-zero `a` mapping to remain unchanged. Unknown, malformed, out-of-range or SendEvent notifications remain invalid. Per-batch/run notification budgets are 8/64; input matching and its three-input rejection limit are unchanged. Input ordinals and full-event sequence are distinct.

Each drain retains an exact pending count and at most 16 bounded event summaries with type counts, omitted/truncated and remaining status **before** classification/refresh. A bounded evidence prefix is not an enlarged input allowance. Overflow, partial reads, new traffic or exceeded notification budgets cannot grant PASS, and captured native input after activity remains FAIL despite a simultaneous notification or evidence error. The original random failure, quiet-unit-49 counter inconsistency, PR38 first-unit overflow, and owned receiver observation remain separate outcomes; this correction does not relabel them or establish a production guard regression.

**Native counter evidence corrected, separately from acceptance scope.** Main reviewed exact PR37 `c76b98e` and passed 111 focused headless checks, then ran **one** native owned virtual-X-server attempt. It remains **UNQUALIFIED**, at quiet unit 49/101 after 5.75 s; none of the other five controls ran. Own send/sync was `[284093613888391, 284093614163353] ns`; the raw query was `[284093614228186, 284093614455820] ns` with `idle=2 ms`. The v1 ±1 ms Python-time inverse ended **0.432571 ms before emission**. That is inconsistent qualification evidence, not an established production safety regression. The original 89/100 failure and this later unqualified attempt remain separate. The focused regression retains these numeric values, not a copy of the private receipt; synthetic delivery/server-clock witnesses demonstrate registration only, never native or whole-gate qualification.

The removed inversion assumed more than integer quantisation. In pinned Xorg **21.1.13**, [`ProcScreenSaverQueryInfo`, Xext/saver.c](https://gitlab.freedesktop.org/xorg/xserver/-/raw/xorg-server-21.1.13/Xext/saver.c) returns `GetTimeInMillis() - LastEventTime(XIAllDevices).milliseconds`. [`GetTimeInMillis`, os/utils.c](https://gitlab.freedesktop.org/xorg/xserver/-/raw/xorg-server-21.1.13/os/utils.c) can select `CLOCK_MONOTONIC_COARSE`, then `CLOCK_MONOTONIC`, or a wall-clock fallback. A constant origin offset alone cancels; variable clock age and event-clock semantics invalidate the claimed Python-monotonic ±1 ms bound. **Main's installed Xvfb version/build/selected clock was not established; coarse clock is not asserted as its measured cause.**

Policy v2 uses **two independent obligations**: native delivery and same-server-clock counter corroboration. A harness-private read-only python-xlib SYNC 3.1 binding implements only Initialize, ListSystemCounters and QueryCounter, using the [protocol wire definitions, xorgproto 2024.1](https://gitlab.freedesktop.org/xorg/proto/xorgproto/-/raw/xorgproto-2024.1/include/X11/extensions/syncproto.h). [`Xext/sync.c`, Xorg 21.1.13](https://gitlab.freedesktop.org/xorg/xserver/-/raw/xorg-server-21.1.13/Xext/sync.c), `ServertimeQueryValue`/`GetTime`, extends the same `GetTimeInMillis()` clock into 64-bit `SERVERTIME`. The counter is discovered by name, not assumed by XID; its version, ID, resolution and registration revision enter the receipt. No extension/binding fallback or invented API is permitted.

On the **backend's same connection**, `SERVERTIME S0 → actual idle query → SERVERTIME S1` yields a server-millisecond last-input interval **`[S0 − idle, S1 − idle]`**. This surrounds the idle query **inside each actual `PresenceMonitor.sample()`**, retaining the copied production snapshot and monotonic sample timings. A distinct raw bracket after every own acknowledgement corroborates registration without updating the monitor. The independently delivered last release/motion timestamp, unwrapped from low 32 bits relative to `S1`, must lie in the interval, which must be strictly later than the preceding competing timestamp and, for own input, the pre-write sample's prior-input interval. Ambiguity stays `INVALID` for every required control; only the precise artificial diagnostic exception above reports a limit **without** a distinct advancement claim. No clipping to an assumed successful write, arbitrary allowance, delayed rescue or retry establishes advancement. Broken/noninteger/missing reads, backward server clocks, signed-64 wrap, a server span above the 90 s run bound or event age ambiguous at half the 32-bit range cannot qualify. Missing SYNC/SERVERTIME is a precise `UNQUALIFIED` setup limitation.

Server timestamps are **never converted to Python monotonic time**. Independent XTEST send/sync intervals establish emission placement against the recorded production injection anchor and completed sample; receiver receipt times are recorded separately. XTEST acknowledgements alone do not prove delivery, and delivery alone does not prove idle-counter provenance. This bounded isolated-server oracle is not a physical-input source classifier or proof of typed text. Additional receiver synchronization and server-clock queries are explicit harness overhead, not production timing or a natural blind-window benchmark. All resources have independent finally cleanup paths (child normal close/reap; saved valid focus restore; receiver window destroy/connection close; backend close), and missing/failed cleanup cannot grant PASS. The production guard and its universal physical-event limit remain unchanged.

Optional `--mode random-model --trials 100 --misses K` is **offline supplied-count analysis, not a new collector or safety gate**. At fixed α=0.05 it reports the exact one-sided tail for `H0: q ≤ 5/60`, with conditional `MODEL_REJECTED` or `NOT_REJECTED`. Realized uniform phase coverage, independence, negligible sample-to-write exposure, counter registration, zero false positives, release/latch behavior and explained misses are not established by counts. Therefore the mode reports `UNSUPPORTED` / `UNQUALIFIED` even on non-rejection. No CLI alpha/threshold tuning is offered. At n=100 it has about 95% power against q=0.20; non-rejection is not evidence of no misses. A future measured statistical check must predeclare its seed, sampling plan and assumptions and retain unexplained misses as unqualified.

**Phase C2 — target binding.** §8.6, all platforms, in the same guard as pause. Requires O9 for macOS.

Evidence: a scripted focus change mid-`type_text` aborts within one elementary event, releases held inputs, reports both targets and the delivered count; measured benign-abort rate over a realistic typing corpus is recorded (it is a cost, and it should be a known number rather than a surprise).

**Phase C3 — the macOS announcement.** `osascript` session-start dialog with the §7.3 disclosed-timeout text; `gave up` mapped to *not an answer*; quiet-gated proceed at timeout.

Evidence: dialog renders on the console user's screen; answered path returns the button; timeout path returns empty `button returned:` and is distinguishable in code; with a human actively typing at timeout, driving does **not** start; with the machine idle at timeout, it does.

**Phase C4 — the Linux overlay.** Override-redirect window per monitor, in-agent thread; click-to-pause and click-to-cancel; geometric exclusion of the button rects at the injection call site; `SHAPE`/`ShapeInput` for the status band if O10 succeeds; self-verification via the capture path. Run O11 (noticeability) before fixing placement.

**Phase C5 — Windows, folded into transport Phase 4.** One persistent PowerShell process serving three consumers: the injection bridge, the in-bridge sampling loop (§5.5), and the overlay (§7.3). Attempt the `LLKHF_INJECTED` hook (§5.6) here — if it works, Windows gets intra-typing detection and `GUARD` stops mattering on that platform.

---

## 12. Success metrics

| Metric | Target |
|---|---|
| **Sessions outside §6.0a in which the guard observed `human_active` and permitted a further task-directed write** | **Zero.** Default §6.0 is unconditional after observation; required safety releases remain allowed. Finite operator exceptions remain explicitly reported, not relabelled quiet. |
| Every physical input detected before the next ordinary write | **UNMET product requirement** with timestamp masking and a sample-to-write race. The conditional gate does not solve or silently waive it. |
| Independently placed outside-band input, before a fresh sample | Every valid predeclared control halts before the next ordinary emission, invokes release and retains the halt. Missing/unplaceable evidence is unqualified. |
| Human input detected during sustained typing, all phases / multiple correlated events | Record observations and platform limits; no universal 100% or unsupported 99% two-event claim. |
| Ideal uniform-phase `GUARD/P` model | Separately scoped exact-binomial diagnostic only after its assumptions are established; supplied counts alone are unsupported (§11.1). |
| Sessions in which the model attributes a failure to a wrong cause when a human intervened | **Zero.** The incident's signature failure. |
| Keystrokes delivered to a window other than the one the op was bound to | **Zero.** §8.6. |
| Benign target-binding aborts per 100 `type_text` ops | Recorded, not targeted. It is a known cost; an unknown one is the problem. |
| Stuck inputs after a pause, cancel, or halt | **Zero.** P0. The ledger already guarantees this. |
| Added round trips per action from coexistence | **Zero.** |
| Added `powershell.exe` spawns per action on Windows | **Zero.** |
| Pause latency, click to enforcement | **< 250 ms** worst case, bounded by the longest atomic composite (§8.4). Survives a network partition (§8.2). *Not the guard band.* |
| Overlay claimed `shown` while actually occluded | **Zero** — self-verification (§7.4) makes this measurable rather than assumed. |
| macOS sessions where the timeout path granted permission while a human was active | **Zero.** §7.3. |

---

## 13. Decisions for you

Three shipping shapes, then five decisions. Everything not listed here is settled by the evidence and I will stand behind it.

### The options

| | Scope | Cost | What it does not do |
|---|---|---|---|
| **1. Detector only** | C1 + C2. Halt invariant, target binding, no announcement anywhere | ~250 LOC, no installs | The human is still surprised once per session, on every platform |
| **2. Detector + announcement** ⭐ **recommended** | C1–C5. Adds the macOS dialog and the Linux/Windows overlay | ~450 LOC, no installs, nothing resident | No per-action consent protocol; no persistent reminder on macOS |
| **3. Option 2 + `request`** | Adds per-action consent with countdown and optional auto-takeover | ~600 LOC, plus the timeout-semantics surface | — |

**Pick 2.** Option 1 leaves the human-facing half of the incident unaddressed on platforms where the fix is now proven and free. Option 3 adds a consent protocol for a use case that has never occurred.

### The decisions

**D1 — Does `request` / per-action auto-takeover belong in scope at all? (live disagreement)**
One council lens argued it should not exist, because it never traced back to the incident. **I agree and this revision removes it (§6.3).** The counter-position is that O1 has now made the macOS channel free, so the marginal cost of `request` is small and it covers a plausible future case (an agent needing permission for one specific destructive action rather than for the session). The disagreement is real and unresolved after three rounds. **My recommendation: cut it from v1, re-open on a demonstrated need.** Your call, and it is the only one where reasonable people on this review actively disagreed.

**D2 — Is a single acknowledged interruption sufficient announcement on macOS?**
O2 makes a persistent macOS reminder impossible without a resident LaunchAgent. O1 makes a one-time disclosed dialog free. So macOS is: announce once, acknowledge, then the human's hands are the only control (§6.0). **I recommend accepting this and not building the LaunchAgent** — it would reverse `remote-transport.md`'s "no daemon, no resident component," add a ghosting failure mode, add a per-target install story, and buy only a visual reminder. If you judge the persistent reminder to be worth that, this is the decision that authorises it. **I recommend: accept, no LaunchAgent.**

**D3 — Resume policy after a halt.**
`manual` (safe, but a passer-by jogging a mouse ends a long unattended run) versus `after_inactivity(N)` (convenient, but resumes while the human may simply be reading — invisible to the detector per §2.2). **I recommend `manual` for any target with a console user, `after_inactivity(120)` for targets with none.** This is a values call about whose time is more expensive.

**D4 — Windows `type_text` before Phase 4.**
Historical revision-2 options were to declare the intra-op limit, slow typing, or wait for source attribution. The maintained band is now 20 ms (§5.5), with ideal `20/60 ≈ 33%` masking at the reference cadence, not the historical 53%. None of those options is adopted by this acceptance correction. Windows current qualification and adoption remain with their owners; a declaration discloses a limit but does not make an unmet guarantee true.

**D5 — Does the opt-out in §7.6 exist at all?**
It permits driving to *begin* on a target with a human present and no working announcement channel. After O1 this should be a near-empty set. It cannot override the §6.0 halt. **I recommend keeping it, logged every session** — the alternative is a hard block with no operator escape hatch, and hard blocks with no escape hatch get worked around in worse ways. If you would rather it not exist, that is defensible and it costs nothing to remove.

### Confirmed closed, not for decision

- **Resident macOS LaunchAgent** — O1 removed its reason to exist. Closed unless D2 goes the other way.
- **Agent-drawn macOS overlay** — O2 tested it directly. Settled negative. Stop probing. — **DISPUTED: `docs/designs/disclosure-v2.md` §5 reports probes that overturn O2 (its negative was a race, not a wall). Read that before relying on this line.**
- **`GUARD = 250 ms`** — O5 proved it disables the feature during exactly the operation the incident occurred in. Replaced by a per-platform band, 5 ms on Linux, measured elsewhere.
