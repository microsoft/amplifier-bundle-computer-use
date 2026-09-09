# Capability awareness and live re-targeting

**Status:** proposal, unreviewed. Written against `9653a65`.
**Companion docs:** `coexistence.md` (§7.1, §7.6 are load-bearing here), `remote-transport.md` (§6.1, §10), `band-lifetime.md`.
**Constraint honored throughout:** no design changes to `coexistence_guard.py`, `presence.py`, `halt_state.py`. Every mechanism below depends only on their observable behavior, and §10 shows the trace that makes that possible.

---

## 0. Recommendation, up front

Four mechanisms, in dependency order. Only the first three are Increment 1.

| # | Mechanism | Why this mechanism | Ship |
|---|---|---|---|
| **M1** | **Keep the handshake.** `registry.select_backend` currently throws away the remote agent's handshake dict. Store it on the backend. | Every honest claim about a remote target is already in that dict, computed on the target, and then discarded three lines later. This is the cheapest, highest-value change in the document. | **Inc. 1** |
| **M2** | **The agent reports its own dispatch table** — add `ops: sorted(_HANDLERS)` to the handshake. | This is the fix for "safe to assert." Today the answer to "can I drag on this target?" lives in a prose table that has been **wrong for 27 commits** (§2.1). Machine-generated facts cannot go stale; prose does. | **Inc. 1** |
| **M3** | **`desktop(action="doctor")`** — one read-only action rendering M1 + M2 + live local probes + effective policy, in the `ios-tester`/`android-tester` shape: every problem at once, each with its fix. | It must be reachable when the capability *works*, so it lives on the working tool. `desktop` is mounted in the same breath as `computer` (`__init__.py:3120-3122`) and is never replaced by a provider's native tool block, so its schema always reaches the model. | **Inc. 1** |
| **M4** | **`desktop(action="retarget", target=...)`** — two-phase rebind: build the new binding completely, *including its own disclosure*, before releasing the old one. | Re-target is achievable without touching the guard files, and the existing channel-keyed disclosure cache already does the right thing for it with zero changes (§10.3). But it mutates the safety-critical binding and deserves its own review round after M3 has proven the reporting surface. | **Inc. 2** |

**Deferred, with reasons, in §11:** persisting a target across sessions, network/LAN scanning, a permission *re-trigger* action, and any new tool.

**The one thing I want the council to attack first:** M3 must be exempt from the `_ensure_announced` disclosure gate, and §5.4 argues the boundary that earns that exemption. If that argument is wrong, M3's placement is wrong.

---

## 1. Problem framing

The user's ask, decomposed into four questions that have genuinely different answers:

1. **"What is safe to assert?"** — an agent must not tell a user "I can drag windows on your Mac" unless that is true *of this binding, right now*.
2. **"Where will a human have to click something?"** — OS permission prompts are fine; *unannounced* OS permission prompts are not.
3. **"What machines could I drive?"** — discovery.
4. **"Can I switch machines without restarting?"** — live re-target.

They are ordered by dependency, not by difficulty. (4) is the headline ask and the hardest, but (1) is the one currently producing wrong statements to real users, and (4) is unusable without (1).

### 1.1 The failure this design exists to prevent

> An agent, asked "can you get on my MacBook and build the iOS app," answers confidently — and the answer is fiction. Either it claims a capability the wire does not carry, or it denies one that has worked for a month, or it walks a non-technical user into a modal dialog nobody warned them about, on a Mac they are sitting in front of.

Every mechanism below is judged against that sentence.

---

## 2. What I verified, and what I did not

Everything in this section was read at `9653a65` before it was stated. The recurring failure in this repository's design history is a confidently-stated claim that was never checked, so the claims are separated from the reasoning and each carries its trace.

### 2.1 The brief's own premise about the nine ops is **false at `9653a65`** — this is a finding, not a quibble

The brief lists as a verified fact: *"Nine operations are not implemented over the wire and raise `BackendError`."* That was true once. It has not been true for 27 commits.

| Evidence | Trace |
|---|---|
| All nine ops are registered in the agent's dispatch table | `remote_agent.py:768-778` — `mouse_down`, `mouse_up`, `drag`, `scroll`, `hold_key`, `list_windows`, `focus_window`, `get_clipboard`, `set_clipboard` |
| All nine marshal across the wire from the controller | `remote_backend.py:306-369` — real `self._call(...)` dispatches, no stubs |
| All nine are classified in the retry policy | `wire.py:54-68` (`WRITE_OPS`), `wire.py:33-53` (`READ_OPS`) |
| The only remaining `BackendError` stub in `RemoteBackend` is `capture()` — deliberate, superseded by `capture_scaled` | `remote_backend.py:262-271` |
| The commit that did it | `94b667e` — *"feat(remote): implement Phase 2 — the nine ops that raised instead of working"* — `git merge-base --is-ancestor 94b667e HEAD` → true; `git rev-list --count 94b667e..HEAD` → 27 |
| The docs that still say otherwise (**at `9653a65`, when this was written**) | `README.md:140-144`; `docs/SETUP.md:80`, `docs/SETUP.md:386-393`, `docs/SETUP.md:658`, `docs/SETUP.md:681` |

**Correction, added at Increment 1 implementation time (`2ee1457`): the row above is now stale.** `2ee1457` — *"docs: the capability table had been lying for 27 commits, and an agent repeated it to a user"* — already fixed `README.md`/`docs/SETUP.md` before this design was implemented; both now correctly state all nine ops work over the wire. A document whose entire thesis is "prose goes stale, generated facts don't" should not itself ship a stale claim about which prose is stale — this note exists so it doesn't. The finding above (a prose table drifted wrong for 27 commits, in the understating direction) remains true and remains the strongest argument for M2; only the "docs still say otherwise" clause needed correcting.

**This is the strongest possible argument for M2 and it was found by reading the code the brief said not to re-derive.** A prose capability table drifted from reality for 27 commits, in the exact direction that makes an agent *understate* its capability to a user — and it drifted inside a repository whose stated discipline is "real hardware or it didn't happen." No process fixed it. Only a generated report can.

Note the correction runs *both* ways: an agent quoting `README.md:140` today tells a Mac user "I cannot focus a window on your machine," which is false, and it will keep being false-in-a-new-direction every time the wire surface changes.

### 2.2 Verified, and load-bearing for what follows

| Claim | Trace |
|---|---|
| `config["target"]` is read exactly once, at mount, before any tool is registered | `registry.py:185-210`, called from `__init__.py:3011` |
| A configured-but-unreachable target raises `RemoteTargetUnavailable` and never falls back to local | `registry.py:175-183` (docstring), `remote_backend.py:44-55`, caught at `__init__.py:3024-3053` → `_mount_unavailable` |
| **`backend.connect()`'s return value — the whole handshake — is discarded** | `registry.py:203-206`: the call is a bare statement; the dict is never bound |
| The handshake already carries `probe`, `capabilities`, `permissions`, `monitors`, `platform`, `backend`, `python` | `remote_agent.py:432-442` |
| macOS TCC state is probed **for real, every connection**, and shipped in the handshake | `remote_agent.py:88-145`, called at `remote_agent.py:441` |
| `_ensure_announced` fires on first real action, not mount, and short-circuits on `self._announced` | `__init__.py:1063-1162`; short-circuit at `:1129`; sticky refusal at `:1127-1128`, `:1136-1137` |
| `_announcement_decisions` is process-wide and **channel-keyed** | `__init__.py:2421` (lock), `:2435` (dict), `:2452-2475` (`_channel_identity`) |
| A cached *refusal* re-raises without re-showing anything | `__init__.py:2658-2669` |
| `_refuse_if_disclosure_declined_with_human_present` runs at mount | defined `__init__.py:2562-2593`, wired `__init__.py:3081-3084` |
| Remote targets get stricter defaults **only when the key is absent** | `read_only` `__init__.py:234-241`; `gate_writes` `:243-256`; `clipboard_read_policy` `:353-356` |
| `select_monitor` re-targets a *monitor* on the already-bound backend and never touches the host | `monitors.py:30-58`, called from `__init__.py:2004-2007` |
| **No code path re-binds the host mid-session** | `self._backend` is assigned once in `ComputerTool.__init__` and never reassigned; `grep -rn "retarget\|rebind\|reconfigure"` across `modules/` and `tools/` returns nothing |
| The coexistence guard is built per-backend by `__init__.py`, not by the guard files | `__init__.py:2073-2205` |
| `desktop.execute()` calls the disclosure gate before anything else | `__init__.py:1893` |
| The failure stub only exists when mount failed | `__init__.py:2915-2972`, reached only via `_mount_unavailable` (`:2976-2996`) |

### 2.3 Verified defects found along the way, reported not designed around

**(a) The durable halt key is not per-host for remote targets.** `record_halt`/`load_halt`/`make_durable_halt_poll` are all called with `backend.name` (`__init__.py:1729`, `:2152`, `:2193`). For a `RemoteBackend`, `name` starts as `remote-ssh:<host>` (`remote_backend.py:72`) and is **overwritten at connect with `remote-ssh:<platform>`** (`remote_backend.py:123`). Two different macOS targets therefore share the halt-state file `remote-ssh_macos.json` (`halt_state.py:180-188`).

The codebase already knows this hazard and fixed it *only for the announcement key* — `_channel_identity` (`__init__.py:2452-2475`) explicitly switches to `user_host` because `backend.name` is "identical for any two DIFFERENT hosts that happen to run the same platform, e.g. two macOS targets," and `tests/test_announcement_dedup.py:144` asserts exactly that. The halt key never got the same treatment, and `remote_backend.py:79-84`'s docstring still claims `name` is "unique per remote target," which is untrue after `connect()`.

**Failure direction: over-halting.** A halt recorded on mac-A causes mac-B to start halted. That is fail-safe, not fail-open, which is why this is reported rather than treated as blocking. It is a **caller-side key choice in `__init__.py`**, not a change to `halt_state.py`, so fixing it stays inside the constraint. §10.4 states what M4 must not assume because of it.

**(b) `ssh://host:port` parses but cannot work.** `_SSH_TARGET_RE` (`registry.py:95`) captures host as `[^/]+`, so `ssh://user@host:2222` yields host `host:2222` and `_parse_target` returns `user@host:2222` (`registry.py:98-110`), which is handed to `ssh` as a hostname (`ssh_transport.py:356`). There is no `-p` anywhere in `_SSH_OPTS` (`ssh_transport.py:99-114`). Verified by executing the regex; the ssh-side consequence is reasoned from `ssh`'s argument grammar, **not executed against a real non-22 host** — see §12.

**(c) The Linux probe does not check for Wayland, and will actively succeed on it.** `LinuxX11Backend.probe()` (`linux_x11.py:249+`) checks `DISPLAY`, session match, and `XAUTHORITY` only. `linux_x11.py:202` lists `/run/user/<uid>/.mutter-Xwaylandauth` as an XAUTHORITY *candidate*, so on a Wayland session the probe finds XWayland's auth file and reports available. `docs/SETUP.md:64` already states there is no Wayland check; this adds that the probe leans toward mounting there. Ubuntu 24.04 defaults to Wayland.

---

## 3. What is genuinely queryable about permission state

This is the section the user asked to be concrete and honest about, so it is a table of *mechanisms*, not a summary.

### 3.1 macOS

| Fact | Queryable without prompting? | Mechanism | Trace |
|---|---|---|---|
| Screen Recording granted? | **Yes** | `CGPreflightScreenCaptureAccess()` via ctypes — prompt-free, non-capturing | `macos.py:236-259` |
| ...on an OS/pyobjc without the symbol? | **No** — returns `None`, an honest "could not determine," never a guess | same | `macos.py:257-259` |
| Accessibility granted? | **Yes** | `AXIsProcessTrusted()` via ctypes — prompt-free | `macos.py:274-289` |
| **Which process** the grant must be given to | **No.** TCC binds to the *responsible* process, which differs by launch chain (`python3` directly vs `uv run` vs an sshd-launched login shell) | — | `remote_agent.py:92-95` states this directly; `macos.py:594-606` tells the user to read the pane "after the first attempted action" |
| Is the screen locked (vs. permission missing)? | **Yes, and they are otherwise indistinguishable** | `ioreg -n Root -d1 -a` → `CGSSessionScreenIsLocked` / `IOConsoleLocked`; three states incl. `unknown`, which refuses rather than guessing | `macos.py:301`, comment `macos.py:290-300`, `docs/SETUP.md` §8 |
| **Granting** either permission | **No. GUI only, by Apple's design.** | — | — |
| **Triggering** the prompt | **Not implemented today.** `CGRequestScreenCaptureAccess` and `AXIsProcessTrustedWithOptions(kAXTrustedCheckOptionPrompt)` both exist in the OS; neither appears anywhere in this codebase (`grep` → no matches) | — | — |

**The consequence the user specifically asked about, stated plainly:** *"calling the tools again re-triggers"* is **false today.** `CGDisplayCreateImage` returning `None` does not raise a dialog — it returns nothing, and `_capture_none_error` (`macos.py:825-871`) turns that into a good error message. Repeating the call produces the same good error message and no prompt. A real re-trigger needs `CGRequestScreenCaptureAccess()`, which is a small addition and is **deferred to Increment 2** (§11) for two reasons: it steals focus and shows a dialog, which is exactly the class of thing §7 says must never be a surprise; and its behavior from an SSH-launched, non-GUI-owning process is **unverified** (§12).

There is a real silver lining here that the design leans on: **both grants are readable prompt-free, and both are already read at every connect** (`remote_agent.py:88-145`) — then discarded with the rest of the handshake. So the entire preflight half of the "warn before the dialog" sequence already exists on the target and just needs to survive the trip home. That is M1.

### 3.2 Windows

**There is no TCC analogue and no user-clicked permission dialog at all.** The gates are structural and all queryable:

| Gate | Mechanism | Trace |
|---|---|---|
| Running under WSL2 | `shutil.which("wslpath")` | `windows.py:186-187` |
| WSL↔Windows interop | `powershell.exe` resolvable without `PATH` | `windows.py:188-196` |
| Session not locked | `LogonUI.exe` presence, inside the existing per-action bridge call; `bridge.ps1` throws `SESSION_LOCKED` | `docs/SETUP.md` §8 |
| Native Windows (no WSL2) | **Not supported at all** — a missing implementation, not a flag | `docs/SETUP.md` §4 |

An agent must not tell a Windows user "you'll need to approve a permission prompt." It must tell them "this needs WSL2, and if it isn't installed we cannot drive this machine." That asymmetry is invisible unless the report states it, which is why the doctor output is per-platform rather than a generic checklist.

### 3.3 Linux/X11

No permission model. Gates are `DISPLAY`, `XAUTHORITY`, and session match (`linux_x11.py:249+`). **Wayland is unchecked and probably mounts (§2.3c)** — the doctor should report `display_server: unverified` on Linux rather than implying an X11 confirmation the probe did not make.

### 3.4 The rule this produces

> **Report grant state as a tri-state — `granted` / `denied` / `unknown` — and never collapse `unknown` into `denied`.**

`_probe_permissions` already gets this right by *omitting* a key it could not determine (`remote_agent.py:135-138`), and `validate_handshake` treats a missing key as not-granted for the purpose of *refusing to connect* (`wire.py:206-212`). Those are two different jobs and the distinction must survive into the report: fail-closed is correct for a gate, and dishonest for a diagnosis. An agent telling a user "your Mac has denied Screen Recording" when the truth is "this OS is too old to ask" sends them to the wrong pane.

---

## 4. M1 — keep the handshake

**Change:** bind `backend.connect(...)`'s return value at `registry.py:203-206` and store it on the backend as a read-only attribute (e.g. `handshake`). Nothing else.

**Why this is the first change:** every honest claim about a remote target is computed *on the target*, arrives in that dict, and is dropped on the floor. Currently the only consumer is `validate_handshake`, which uses it to decide whether to raise, and `RemoteBackend.connect` itself, which keeps two fields (`name`, `presence_platform` — `remote_backend.py:122-125`). Nothing surfaces `permissions`, `capabilities`, `monitors`, or `python` to anyone.

**Risk:** none identifiable. It is data already in memory in that stack frame.

**What it must not become:** a live cache that gets stale. The handshake is a **connect-time snapshot** and the doctor must label it as such, with its age. Permissions can be revoked mid-session; a user can lock the screen. §5.3 says which doctor fields are live and which are snapshots, and that distinction is not cosmetic.

---

## 5. M3 — the capability report

M2 is one line inside M3's story, so they are described together.

### 5.1 Where it lives, and why not anywhere else

| Candidate | Verdict |
|---|---|
| **A new action on `desktop`** | **Chosen.** `desktop` is mounted in the same call as `computer` (`__init__.py:3120-3122`), so it exists exactly when the capability works. It is an ordinary tool with its own `input_schema`, never replaced by a provider's native server-side tool block — `__init__.py:1826-1835` already identifies it as the surface that "reliably reaches the model on every dialect." Adding an enum entry to `DESKTOP_ACTIONS` (`__init__.py:1783-1790`) costs one line and zero new tool slots. |
| A new top-level tool (`computer_doctor`) | Rejected. Costs a tool slot and a description on *every* provider request forever, to expose one action that has a natural home. This is the unearned apparatus two prior designs were failed for. |
| The failure stub | Rejected, and the brief is right about why: `ComputerUseUnavailableTool` only exists when mount failed (`__init__.py:2976-2996`). It cannot answer "what can I do here" because there is no "here." |

### 5.2 But the stub still has a job, and it already does it

The gap in the choice above is real: when mount fails, `desktop` does not exist, so the report is unreachable at exactly the moment someone wants setup guidance.

That gap is **already closed** and needs no new code. `ComputerUseUnavailableTool.description` (`__init__.py:2951-2962`) carries the reason plus `registry._REMEDIATION` (`registry.py:55-61`), which names the per-platform causes and the `config.target` shape. It reaches the model in the tool declarations of every request. The two surfaces answer two different questions and should stay separate:

- **mounted →** "what can I do on the machine I am bound to?" → `desktop(action="doctor")`
- **not mounted →** "why not, and what would make it work?" → the stub's description, unchanged

The one addition worth making: the stub's description should point at the discovery procedure (§7), because "why not" and "what else could I drive" are the same user question at that moment.

### 5.3 What it reports — snapshot vs. live, labelled

Every field carries its provenance, because a stale fact presented as a live one is how this repository's worst incidents started.

**Bound target** *(live, in-process)*
`backend.name`, `is_remote`, `user_host` (`remote_backend.py:88-101`), `presence_platform`, connected-since.

**Action surface** *(computed — this is M2)*
The intersection of the local action list (`ACTIONS` `__init__.py:125-145`, `DESKTOP_ACTIONS` `:1783-1790`) with what this binding can actually carry:
- local backend → all actions the platform's backend implements
- remote backend → derived from a new handshake field `ops: sorted(_HANDLERS)` (`remote_agent.py:760-799`)

Rendered as three lists: **works / blocked-by-policy / not-carried-by-this-binding.** The second and third must never be merged — `focus_window` on a default remote target is *blocked by `read_only`* (`__init__.py:1801`, `remote_agent.py:493-500`), not unimplemented, and the fix is a config change, not a code change. Telling a user "that isn't supported" when the truth is "that is switched off for your safety, here is the switch" is the same class of wrong answer as §2.1.

**Wire compatibility of the `ops` field — no version bump needed.** The controller *deploys* the agent on every connect and verifies its sha256 against what it sent (`remote_agent.py:77-85`, `wire.py:194-200`), so controller and agent are always the same build. A new handshake field can never meet an old agent. `validate_handshake` (`wire.py:180-215`) only inspects named keys, so adding one is inert to it.

**Permissions** *(macOS: connect-time snapshot from M1, re-readable live)*
Tri-state per §3.4, plus **the exact process string** the user must find in the Privacy pane, plus current lock state from `_macos_session_state()` (`macos.py:301`) so "locked" is never misreported as "denied."

**Effective policy** *(live)*
`read_only`, `gate_writes`, `clipboard_read_policy` and, for each, **whether it came from config or from the remote default** (`__init__.py:234-256`, `:353-356`). A user asking "why can't you click?" needs to know whether to change a setting or a machine.

**Safety state** *(live)*
`guard.as_dict()` (`coexistence_guard.py`), `presence.guard_measured` / `guard_ms` (`presence.py`), whether a durable halt is seeded and `halt_state.resolve_resume_command()` if so, and — for remote — each sample's `effective_staleness_ms`. Guard construction is quiet; a measured transport warning is emitted only after a successful remote sample exceeds the reporting threshold.

**Target-mode** *(live)*
Current monitor vs. virtual desktop (`__init__.py:672`), and the `config.target` shape fact (`registry._TARGET_MODEL`, `registry.py:36-43`) — reused verbatim, never paraphrased, so it cannot drift from the two places that already share it.

### 5.4 The disclosure-gate exemption — the sharpest decision in this document

`DesktopTool.execute()` calls `self._computer._ensure_announced()` before anything else (`__init__.py:1893`). If `doctor` goes through that gate, **an agent orienting itself fires a modal dialog on someone's Mac.**

That is not a minor annoyance. `coexistence.md` §7.3 warns that a dialog people learn to click through is worse than no dialog.

**Correction (found in review, and every lens that checked it agreed): the sentence that used to stand here was false, and is deleted rather than kept as "directionally right."** It claimed an agent gated behind `_ensure_announced` "will show that dialog constantly." It would not. `_announcement_decisions` (`__init__.py:2435`) caches the disclosure decision per PHYSICAL CHANNEL (`_channel_identity`) for the life of the controller process (§2.2) — so a gated `doctor` would show the dialog **at most once per channel, ever**, identical to every other action's own experience of that gate today. The real argument for the exemption is not frequency, it is *sequencing*: the FIRST call on a fresh binding is the one that has not shown the dialog yet, and that one call happening to be an orienting "what can I do here?" question means the agent cannot warn the human about the dialog using the tool that is about to fire it (§8 step 5 vs step 6 — the warning must precede the click, and a gated `doctor` collapses that ordering into a single, unannounced pop). That chicken-and-egg is real and sufficient on its own; the "constantly" claim was not needed to make the case and should never have been in it.

**Recommendation: `doctor` is exempt from `_ensure_announced`, and it earns that exemption with a hard boundary:**

> **`doctor` reports about the machine. It never reports anything about what is on it.**
> No capture. No window titles. No clipboard. No cursor position.
> Monitor *geometry* is permitted — it is already in the connect-time handshake (`remote_agent.py:419-431`), taken before this session's first action, and describes hardware, not content.

This is checkable in review and testable: doctor's implementation may call `probe`, `presence_idle`, and read cached handshake/config/guard state, and may not call `capture`, `capture_scaled`, `list_windows`, `get_clipboard`, or `cursor_position`.

**The counter-argument, stated fairly, because the council should weigh it and not me alone:** `_ensure_announced`'s own docstring (`__init__.py:1085-1096`) argues there must be "exactly one gate, not one gate for writes and a silent hole for reads." An exemption *is* a second door. My answer is that the docstring's stated reason is content — *"A screenshot IS a capture of a human's screen"* — and the boundary above removes exactly that. But this is the load-bearing judgement in M3 and if it does not survive review, M3 must move to a place that is not behind the gate, which most likely means the standalone tool I rejected in §5.1.

---

## 6. M4 — live re-target

### 6.1 Mechanism

`desktop(action="retarget", target="ssh://user@host" | "local")`.

**Why an action on `desktop` rather than a capability, a hook, or config-watching:**
- It must be *invocable by the model mid-conversation* — that is the entire ask — which rules out anything driven by config reload.
- It must be able to **fail loudly back to the caller**, with the reason in the model's context. A tool result does this natively; a hook or background reload does not.
- It must be the same surface that carries `doctor`, because the sequence is always `doctor` → `retarget` → `doctor`.
- `select_monitor` (`__init__.py:2004-2007`) is the existing precedent for a `desktop` action that mutates session-scoped targeting state. This is the same shape, one level up.

### 6.2 The sequence — build fully, then release

Ordering is the whole design. The old binding is not touched until the new one is proven, disclosed, and consented to.

```
0. REFUSE-IF-BUSY      band depth != 0                  -> refuse, name the in-flight action
                       ledger.held_tokens non-empty     -> refuse, name the held button
1. PARSE + BUILD       registry.select_backend({...cfg, target})   [connect + handshake]
2. GUARD               _build_coexistence_guard(new_backend, cfg)
                       + new channel key / ledger / band state
3. MOUNT-TIME REFUSAL  _refuse_if_disclosure_declined_with_human_present(...)
4. DISCLOSE            _build_announcement(new_backend, ...)   <- may show a dialog / raise an overlay
5. POLICY              recompute read_only / gate_writes / clipboard_read_policy from cfg + new is_remote
6. DISPLAY             resolve_display() on the new backend
--- commit point: nothing above this line has touched the old binding ---
7. SWAP                self._backend = new; guard/ledger/band/channel_key/display = new
8. RELEASE OLD         old_backend.close()  (refcounted; see 6.4)
```

Any failure at 0-6 leaves the session **exactly** where it was, and returns a `ToolResult(success=False)` naming the step and the reason. There is no partial state and no fallback — `select_backend`'s existing refusal to fall back to local on an unreachable explicit target (`registry.py:175-183`) is inherited unchanged and is precisely the property this needs.

### 6.3 What must reset, and the one that must not

Per-instance state on `ComputerTool` that is bound to a *machine* and must be replaced at step 7:

| Field | Why |
|---|---|
| `_announced` → `False` | **The §7.1 breach in one line.** `_ensure_announced` short-circuits on this (`__init__.py:1129`); leaving it `True` drives a new machine on the old machine's disclosure. |
| `_announcement` → new handle | Old handle belongs to the old channel |
| `_coexistence_guard`, `_channel_key`, `_ledger`, `_band_state` | All channel-scoped (`__init__.py:3070-3073`) |
| `_is_remote`, `_read_only`, `_gate_writes`, `_clipboard_read_policy` | Local→remote **must** pick up stricter defaults; remote→local must not carry stale strictness. Explicit config still wins, identically to mount (`__init__.py:234-256`) |
| `_display`, `_current_monitor` | Different hardware |
| `_mouse_pending` | Must be empty — step 0 already guaranteed it |

**`_announce_refused` also resets to `None` — and that is safe only because of a property that already exists.** A refusal on machine A must not permanently block machine B, so it cannot be preserved verbatim. But clearing it looks like a way to re-ask a human who already said no, which `_build_announcement` explicitly forbids (`__init__.py:2660-2669`: *"re-asking after a refusal is worse than not asking at all"*).

The saving grace is that the refusal does not actually live on the instance. It lives in the **process-wide, channel-keyed** `_announcement_decisions` cache (`__init__.py:2435`). So re-targeting *back* to a previously-refused channel hits `:2658-2669`, raises `AnnouncementRefused` without showing anything, and step 4 fails — leaving the old binding intact. **The instance field is a cache of the real decision, not the decision.** This is the single most important trace in the document and it is why re-target does not need to weaken anything.

### 6.4 The old target's channel — release, never destroy

The instinct is "tear down the old overlay." **That would be a §7.1 violation against a third party.**

`_announcement_decisions` is process-wide and shared: a delegated child session, or a second tool config, may be driving the same channel through the same handle (`__init__.py:2426-2434`, `2670-2677`). Purging that entry or calling `hide()` on the handle would remove *their* disclosure while they are still driving.

**Rule: a re-targeting session releases its reference and nothing more.**

- `old_backend.close()` — for a remote target this only decrements *this handle's* refcount (`shared_transport.py`, and `__init__.py:1147-1152` already relies on exactly this property); the agent process and its overlay only die when the last consumer leaves, and its own `_teardown_overlay` handles that (`remote_agent.py:382`).
- `_announcement_decisions[old_key]` is **left alone**.
- The band needs no action: `_band_exit` already lowers it when channel depth hits zero (`__init__.py:1245-1270`), and step 0 guaranteed depth is zero.

**Accepted consequence, stated rather than hidden:** during a re-target there is a window where two channels are disclosed at once — the old target's overlay may still be up while the new target's is raised. That is *over*-disclosure. It is the correct direction to err and it resolves on its own.

### 6.5 What must fail loud

| Condition | Behavior |
|---|---|
| Target unparseable | Refuse. `_parse_target` already raises `ValueError` with the expected shape (`registry.py:104-108`) |
| Target unreachable | Refuse. `RemoteTargetUnavailable`, **never** fall through to local (`registry.py:175-183`) |
| New target has no presence detector / no `GUARD_MS` entry | Refuse **by default**. Mount tolerates this (`__init__.py:2110-2132` → `None` guard) because the alternative at mount is no capability at all; mid-session it means silently *downgrading* an already-protected session, which is exactly the silent degradation this repo forbids. Overridable only by the same explicit, logged opt-out shape §7.6 already uses |
| Disclosure refused or channel failed on the new target | Refuse. Old binding intact |
| Anything in flight or held | Refuse before doing any work (step 0) |
| Same target as current | No-op, reported as such. Never silently re-disclose |

### 6.6 What re-target is *not*

It is **not** a way to widen policy. `read_only`/`gate_writes` are recomputed from the *same config* against the new target's remoteness. `retarget` takes no policy arguments. If it did, it would be a privilege-escalation surface reachable by the model — a machine-selection mechanism that can also turn off `read_only` is not a machine-selection mechanism.

---

## 7. Discovery — a document, not a tool

### 7.1 The recommendation, and it will be unpopular

**Build no discovery code.** Write the procedure into `agents/computer-operator.md` and the awareness context, and let the agent use the shell it already has.

`tailscale status --json`, reading `~/.ssh/config`, and `ssh -o BatchMode=yes host true` are three shell commands. An agent with bash can already run all of them. What it lacks is not capability — it is **knowing which ones are safe, in what order, and where the traps are.** That is a context problem, and wrapping three shell commands in a tool module adds a surface to maintain, version, and test without adding a single new fact.

The counter-pressure is real and I want it named: §2.1 proves prose drifts. So the split is not "prose vs. code" but **which kind of claim each carries**:

> **Facts that can drift are generated (M2/M3). Procedure that requires judgement is prose.**

Discovery is judgement — chiefly the judgement in §7.3 about when to stop and ask. There is nothing in it to go stale.

### 7.2 The ladder, and what each rung actually proves

| Rung | Question | Cost | Mechanism | Proves |
|---|---|---|---|---|
| 1 | What machines exist? | ~0 | `tailscale status --json`; `~/.ssh/config` `Host` blocks | Names. **Nothing else.** |
| 2 | Which username? | 0 | `~/.ssh/config` `User` for that host — else **ask** (§7.3) | The only safe source |
| 3 | Can I log in? | ~1s | `ssh -o BatchMode=yes -o ConnectTimeout=5 <target> true` | SSH works. **Not that computer-use will.** |
| 4 | Can computer-use run there? | 5-30s | **`retarget`** — needs `uv`, a working backend, a GUI session | Everything rung 3 does not |
| 5 | Will input work? | — | permissions; on macOS **needs a human** | §3 |

**Rung 4 has no separate mechanism, and that is deliberate.** The honest deep probe for "can I drive this machine" *is* attempting the binding: `select_backend`'s remote branch already deploys the agent, verifies its hash, runs `probe()`, and reads permissions (`registry.py:186-210` → `ssh_transport.connect` → `wire.validate_handshake`). Building a second, shallower "dry-run" probe would mean maintaining a second definition of "works" that can disagree with the real one — which is how §2.1 happened. `retarget` fails loud and leaves you where you were (§6.2), so trying *is* the safe probe.

### 7.3 The username rule — the user's own trap this week

Their tailnet showed `alice@` as device owner. The working SSH user was `a-user`.

> **A tailnet or LAN listing gives you HOSTS. It never gives you USERS.**
> The Tailscale owner field is an account identity — an email or SSO login — not a POSIX username on that machine. Deriving one from the other is the guess that fails.

Rules that follow, and they are absolute:

1. **Never synthesize a username** from an owner field, an email local-part, the local `$USER`, or a hostname.
2. `~/.ssh/config` `User` for that host is the **only** inferred source, because a human wrote it.
3. If there is no `User` entry: **ask.** One question — *"what username do you log in as on `example-macbook`?"* — beats a failed connect that produces an authentication error a non-technical user cannot read.
4. When asking, **offer the hosts you found**, not a guess at the full target. "I can see `example-macbook` and `example-desktop` on your tailnet. Which one, and what's your username there?" is one question that closes both unknowns.

**Discovery that asks is better than discovery that guesses** — because the failure mode of guessing is not "it doesn't work," it is *"it doesn't work and the user cannot tell why."*

### 7.4 What is safe to probe, and where the creepy line is

| Source | Verdict | Reason |
|---|---|---|
| `tailscale status --json` | **Safe** | Local daemon query. Fast. Names, OS, online state. The user already runs Tailscale |
| `~/.ssh/config` | **Safe** | Local file the user authored for exactly this purpose |
| `ssh ... true` on a **user-named** host | **Safe** | The user named it; a login attempt is what they asked for |
| `ssh ... true` on a host they did **not** name | **Never** | Connecting to machines the user didn't mention is the creepy line. Discovery enumerates; connection requires a named target |
| `~/.ssh/known_hosts` | **Don't** | It is a list of every host this user has ever connected to — work, client, personal — and reading it to a model exports that. It is also commonly hashed (`HashKnownHosts` default), so it is *both* invasive and useless |
| ARP / ping sweep / `nmap` | **Don't** | Slow, trips IDS, and yields IPs that are not usable targets without a username anyway. Fails §7.3 at rung 2 |
| mDNS (`dns-sd -B _ssh._tcp`, `avahi-browse`) | **Only if asked** | The one tolerable LAN form; still yields hosts without users |

**The governing rule:** *enumerate from what the user already told their own machine; connect only to what they tell you now.*

---

## 8. The non-technical user's path, end to end

> *"Can you get on my MacBook and build the iOS app in Xcode?"*

Each step names who acts. **Bold = a human must do something.**

**1. Agent orients — `doctor` on the current binding.** Reports: bound to `local:linux-x11`, no target configured. So the MacBook is not reachable from this session yet.

**2. Agent enumerates (rung 1).** `tailscale status --json` → `example-macbook` (macOS, online).

**3. Agent checks for a username (rung 2).** `~/.ssh/config` has no `User` for that host.

**4. → HUMAN: answer one question.** Agent asks:
> *"I can see `example-macbook` on your tailnet. What username do you log in with on that Mac? (I don't want to guess — the Tailscale owner name and the login name are often different.)"*

This is the §7.3 step. It exists because guessing here cost this user real time this week.

**5. Agent sets expectations *before* anything happens.** This is the "warn before the dialog" beat, and it must come *before* step 6, not after it fails:
> *"I'm going to connect to `a-user@example-macbook` and take over that Mac's screen and keyboard. Three things will happen there, in this order:*
> *1. A dialog will appear on that Mac saying an automated agent is about to drive it, with **Allow** / **Don't allow**. It closes itself after 30 seconds. **Someone has to click Allow.***
> *2. macOS may then say the process isn't allowed to record the screen or control the computer. Those are two separate permissions in two separate panes. I'll tell you exactly which process to enable when I see which one it asks for.*
> *3. Once those are granted, I can see and drive it.*
> *Is someone at that Mac right now?"*

**6. → HUMAN: click Allow on the Mac.** Agent calls `retarget(target="ssh://a-user@example-macbook")`. The macOS announce-and-acknowledge dialog is step 4 of §6.2, raised on the *new* target. If nobody answers and the Mac is idle → proceed; if nobody answers and someone is using it → **not allowed** (`coexistence.md` §7.3). If it is declined, the session stays on Linux and says so.

**7. Agent verifies — `doctor` again.** Now bound to `remote-ssh:macos`. Suppose it reports `screen_recording: denied`, `accessibility: unknown`, `read_only: true (remote default)`, `session: unlocked`.

**8. Agent narrates the *specific* fix.** Not "grant permissions" — the exact pane and the exact process name from §5.3:
> *"That Mac hasn't given screen access to the process I'm running through. On that Mac: **System Settings → Privacy & Security → Screen Recording**, and turn on `python3.12`. It may not be listed until after my first attempt, in which case try my screenshot first and then look. Accessibility is a second, separate switch in the same Privacy list — Xcode work will need both, because I'll be clicking and typing."*

**9. → HUMAN: flip the toggles.** Agent re-runs `doctor` to confirm — **not** a screenshot, which conflates "granted" with "unlocked" with "display awake." Doctor separates all three (§3.1).

**10. → HUMAN: allow writes.** The Mac is remote, so `read_only` defaults on (`__init__.py:234-241`). Driving Xcode is all writes. Agent says:
> *"Right now I'm in look-but-don't-touch mode, which is the default for a machine you're not sitting at. To actually click and type in Xcode I need write access. Say the word and I'll turn it on for this session; every write will still ask you first unless you turn that off too."*

This is a **decision, not a step** — the user is choosing to let an agent type on a machine they cannot see. It gets its own beat and its own consent.

**11. Agent works.** Coexistence is live throughout: if someone touches that Mac's keyboard, the guard halts (`coexistence_guard.py:245-256`), the halt is durable (`__init__.py:1729`), and only an explicit human action resumes it (`halt_state.resolve_resume_command()`).

**Human touchpoints: four** — the username (4), Allow on the Mac (6), the TCC toggles (9), the write-access decision (10). Every one is announced before it is needed. **None of them is a surprise, and that is the entire product.**

**The gap this walkthrough exposes, named not hidden:** at step 8 the user must be *at* the Mac. If they are not, the flow stalls with no recovery beat, and no mechanism in this design fixes that — it is Apple's design and no amount of tooling routes around it. The agent's honest move is to say so at step 5 ("is someone at that Mac right now?"), which is why that question is in the script and not an afterthought.

---

## 9. Tradeoffs

| Dimension | This design | Cost |
|---|---|---|
| **Latency** | `doctor` is cached-read + at most one `presence_idle` round trip. `retarget` costs a full connect (5-30s, `connect_timeout` default 30 — `registry.py:205`) | `retarget` is slow enough that it needs a "this takes a moment" line in the agent's script |
| **Complexity** | One new action for the whole reporting surface; one for rebinding. No new tools, modules, threads, background loops, or persisted state | The `doctor` renderer is the one place with real branching (per-platform, per-remoteness) |
| **Reliability** | `retarget` cannot leave an ambiguous binding: build-then-swap, refuse-if-busy, no fallback | The overlap window in §6.4 (two disclosures briefly up) is real and accepted |
| **Cost** | ~0 tokens when unused. `doctor`'s output is the only real token cost and it is agent-invoked | A verbose doctor is a footgun; it should be scannable, not exhaustive |
| **Security** | `retarget` takes **no policy arguments** (§6.6). Discovery never connects to unnamed hosts (§7.4). `doctor` reports about the machine, never its contents (§5.4) | The §5.4 exemption is a second door past the disclosure gate and must be reviewed as one |
| **Scalability** | Bounded by hosts a user actually has. Nothing scans | — |
| **Reversibility** | `doctor` is read-only and trivially removable. `retarget` is session-scoped and persists nothing | The handshake field in M2 is the only wire change, and it is inert to old readers (§5.3) |
| **Org fit** | Increment 1 is three small changes in files already being edited. Matches the `doctor` precedent in `ios-tester`/`android-tester` | — |

**What this optimizes for:** an agent that never says something false about what it can do.
**What it sacrifices:** it does not make setup *easier* — it makes setup *legible*. The human still clicks every dialog they clicked before.

---

## 10. Is live re-target achievable without touching the guard files?

**Yes.** The trace, mechanism by mechanism.

### 10.1 The three files are constructed *by* `__init__.py`, per backend, and hold no reference back

- `CoexistenceGuard` is a dataclass built at `__init__.py:2140-2154` from a `PresenceMonitor`, a `release_all` lambda, a `target_source`, and a `durable_halt_poll`. Every one of those is supplied by the caller. Re-target builds a *new* guard for the new backend by calling the same function. `coexistence_guard.py` is unchanged.
- `PresenceMonitor` is constructed at `__init__.py:2133` with `idle_source=backend.presence_idle_ms` and a `platform`. New backend → new monitor. `presence.py` is unchanged.
- `halt_state` is called as free functions with a key the caller chooses (`__init__.py:1729`, `:2152`, `:2193`). New backend → new key. `halt_state.py` is unchanged.

Grep confirms no back-reference: nothing in the three files imports or reaches into `ComputerTool`.

### 10.2 The disclosure invariant survives because the gate is per-instance and the *decision* is per-channel

`_ensure_announced` gates on `self._announced` (`__init__.py:1129`). Resetting it forces a fresh `_build_announcement` for the new backend, which computes a **new channel key** (`_channel_identity`, `__init__.py:2452-2475`) and therefore misses the cache and raises a real disclosure on the new machine. **This is the §7.1 guarantee holding by construction, not by a new rule.**

### 10.3 The dedup cache already does exactly the right thing for re-target, unchanged

Three behaviors fall out of `_announcement_decisions` being channel-keyed and process-wide (`__init__.py:2435`, `:2652-2691`), with no modification:

- **New machine → new key → real disclosure.** Correct.
- **Back to a machine that already consented → cache hit → handle reused, no second dialog** (`:2670-2677`). Correct: re-disclosing to someone who already said yes trains click-through.
- **Back to a machine that *refused* → cache hit on `refused` → `AnnouncementRefused` re-raised without showing anything** (`:2658-2669`), so step 4 fails and the old binding survives. Correct, and it is what makes clearing the per-instance `_announce_refused` safe (§6.3).

### 10.4 The two things re-target must not assume

1. **Halt keys are not per-host for remote targets** (§2.3a). Re-targeting mac-A → mac-B can inherit mac-A's durable halt. Fail-safe direction (over-halting), so it does not block M4 — but `doctor` must **report the seeded halt and the resume command** so the state is legible rather than mysterious, and the design must not claim halt isolation between same-platform remote targets. Fixing the key is a one-line caller-side change at `__init__.py:2152`/`:2193`/`:1729` if wanted; it is out of this design's scope.
2. **The `enabled`/`announce` decline path must run again.** `_refuse_if_disclosure_declined_with_human_present` is a mount-time check (`__init__.py:3081-3084`). Step 3 of §6.2 re-runs it against the new backend. Skipping it would let a config that legitimately declined disclosure carry a *human-present* target it was never evaluated against.

### 10.5 Conclusion

Live re-target is a change to `__init__.py` (reset + rebuild, in a proven order) plus one action entry in `DESKTOP_ACTIONS`. **It does not require, and should not receive, a single line of change in `coexistence_guard.py`, `presence.py`, or `halt_state.py`.** If an implementation finds itself needing to modify one of them, that is a signal the ordering in §6.2 was violated — most likely by swapping the backend before disclosing — and it should stop and come back here.

---

## 11. Increment 1, and what I would defer

### Increment 1 — three changes, all read-only or additive

1. **M1** — bind the handshake at `registry.py:203`. ~3 lines.
2. **M2** — add `ops: sorted(_HANDLERS)` to the handshake at `remote_agent.py:432-442`. 1 line.
3. **M3** — `desktop(action="doctor")`: one enum entry + a renderer. No new state, no new tool, no mutation.
4. **Fix the stale docs** (`README.md:140-144`, `docs/SETUP.md:80/386-393/658/681`). Docs only — and once M2 ships, delete the table rather than correcting it, so it cannot drift a second time.

**Why this is the increment:** it closes the gap that is *currently producing wrong statements to real users* (§2.1), it is entirely read-only, and it is the prerequisite for everything else — you cannot sensibly re-target to a machine you cannot describe.

### Deferred, with reasons

| Deferred | Why |
|---|---|
| **M4 (re-target)** | It mutates the safety-critical binding. §10 says it is achievable; that is not the same as saying it should ship in the same breath as its own prerequisite. Ship `doctor`, watch what agents actually ask it, then rebind |
| **Permission re-trigger** (`CGRequestScreenCaptureAccess`) | Shows a focus-stealing dialog, so it must be explicit and agent-invoked, never implicit. And its behavior from an SSH-launched process is unverified (§12). Wanted, but it needs a probe first |
| **Persisting a target across sessions** | The user asked for "update the live/running config." Live re-target *is* that ask. Persistence is a different thing — an Amplifier settings-file concern, not a computer-use concern — and the agent already has file tools and an approval loop for it. Building a config-writer into this bundle would put a settings mutator inside a module whose blast radius is someone's desktop |
| **Any network scanning** | §7.4. Slow, invasive, and fails at the username rung anyway |
| **A `discover` tool** | §7.1. Three shell commands the agent already has. The missing thing is judgement, and judgement ships as prose |
| **Fixing `ssh://host:port`** (§2.3b) | Real, small, and unrelated to this design. Should be its own issue |
| **Wayland detection** (§2.3c) | Real, and arguably more urgent than anything here given Ubuntu 24.04's default — but it belongs to `linux_x11.probe()`, not to capability *reporting*. `doctor` should say `display_server: unverified` until it is fixed |

---

## 12. What I could not verify — stated plainly

The council should treat every item below as unproven.

1. **`CGRequestScreenCaptureAccess()` from an SSH-launched, non-GUI-owning process.** Not run. It may prompt, may silently return false, or may prompt on a session nobody is looking at. The whole "re-trigger the prompt" idea rests on this and it is untested.
2. **macOS TCC prompt caching after a denial.** Widely reported that a denied grant will not re-prompt until `tccutil reset`. Not verified on this hardware. If true, "call it again to re-trigger" fails permanently after the first denial, and only §8 step 8's manual-pane instruction works.
3. **`AXIsProcessTrustedWithOptions` with the prompt option.** Not present in this codebase, not run.
4. **`ssh user@host:2222` behavior.** The regex result is verified by execution (§2.3b); the claim that `ssh` treats it as a hostname is reasoned from `ssh`'s argument grammar and **was not run against a real non-22 host**.
5. **The `ops` handshake field's actual JSON size.** ~20 short strings; assumed negligible against a 30s connect. Not measured.
6. **Whether a re-target's overlap window (§6.4) is visually confusing on a real multi-monitor Linux desktop.** Reasoned, not observed. It is over-disclosure so it is safe, but it may look alarming.
7. **`tailscale status --json`'s exact field names and stability across versions.** Referenced but not run here. The §7.3 rule (owner ≠ username) is derived from the user's reported incident, not from reading Tailscale's schema.
8. **Whether `doctor`'s exemption from `_ensure_announced` (§5.4) survives review.** This is a judgement call, explicitly flagged. I believe the content boundary earns it; I did not test it, and if it falls, M3's placement changes.
9. **Everything about Wayland.** §2.3c is read from `linux_x11.py:202` and `:249+` plus `docs/SETUP.md:64`. No Wayland session was tested.
10. **Whether the halt-key collision (§2.3a) has ever fired in practice.** The code path is verified; the incident is not. It is reported as a latent defect with a fail-safe direction, not as an observed failure.

---

## 13. Decisions for the reviewer

1. **§5.4 — is `doctor` exempt from the disclosure gate?** My recommendation is yes, bounded by "reports about the machine, never its contents." This is the load-bearing judgement in Increment 1.
2. **§6.5 — should `retarget` refuse a target with no presence detector?** I say yes by default (a mid-session downgrade of an already-protected session is silent degradation), which is *stricter* than mount is today. That asymmetry is deliberate and worth a second opinion.
3. **§7.1 — discovery as prose, not code.** §2.1 is a live counter-example to trusting prose. I argue the split holds because discovery contains no drift-prone facts, only judgement. Disagreement here is reasonable.
4. **§11 — is deferring M4 correct**, given that live re-target was the headline ask? I think shipping the reporting surface first is right, but the user asked for the rebind.
