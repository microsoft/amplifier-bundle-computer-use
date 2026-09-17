# Setup

**This is not the usual Amplifier bundle install.** A typical bundle is an `--app`-level
pointer at a behavior file, and you are done. This one is not, and pretending otherwise
wastes your afternoon. It drives a *real desktop*, so the install has four moving parts
that live outside this repository:

| # | What you must get right | Fails how |
|---|---|---|
| 1 | **Upstream module versions** — `loop-streaming`, `provider-anthropic` / `provider-openai` | Bundle refuses to mount, or silently degrades to a weaker function tool |
| 2 | **An endpoint that accepts native computer use** | Server rejects the request |
| 3 | **A target machine, and its per-platform prerequisites** — Windows **requires WSL2**; macOS requires **two** TCC grants; Linux requires **X11** (not Wayland) | Backend probe fails and tools never appear — or, on macOS and Linux, they appear and fail on first use |
| 4 | **For remote targets: SSH, key auth, and `uv` on the far end** | Connect-time error (missing `uv`, untrusted host key, no key-based auth) |

Work through them in that order. Each section tells you the exact check to run. **But
first, register the bundle at all** — none of the above matters until Amplifier knows
this bundle exists.

---

## Install (do this first)

Registering this behavior is ordinary Amplifier bundle management — the unusual part
starts *after* registration, in the four rows above. The command below targets the
behavior manifest, so its registered name is `computer-use-behavior`:

1. **Register it**:

   ```bash
   amplifier bundle add git+https://github.com/microsoft/amplifier-bundle-computer-use@main#subdirectory=behaviors/computer-use.yaml --app
   ```

   Verified against this exact repo — the command reports back `Bundle's canonical name:
   computer-use-behavior`. Working from a local clone instead of GitHub? Use the same
   behavior manifest through a `file://` URI instead: `amplifier bundle add
   file:///path/to/amplifier-bundle-computer-use#subdirectory=behaviors/computer-use.yaml --app`.

2. **Use it for a session** — either per-run:

   ```bash
   amplifier run --bundle computer-use-behavior "What's on my screen right now?"
   ```

   or set it as your active bundle first, so you don't need `--bundle` every time:

   ```bash
   amplifier bundle use computer-use-behavior
   amplifier run "What's on my screen right now?"
   ```

3. **Confirm it's actually there** before you rely on it:

   ```bash
   amplifier bundle show computer-use-behavior
   ```

   Once registered this lists the tools/hooks/agents it contributes
   (`tool-computer-use`, `hook-computer-use`, `computer-use:computer-operator`) — if it
   doesn't, registration itself failed and nothing below this point will work either.

That's registration. It does **not** mean the tools will work yet — that depends on the
four moving parts below (module versions, endpoint support, a reachable target machine,
and, for remote targets, SSH). Read on for those.

---

## 0. Which paths are proven, and which are rough

Stated plainly so you can pick a path you can actually finish.

| Path | Status |
|---|---|
| Anthropic provider driving a real desktop | **Proven live.** `claude-sonnet-4-5` / `claude-sonnet-5` / `claude-opus-5` verified against the real API (`providers.py` `ANTHROPIC.models`) |
| OpenAI provider driving a real desktop | **Proven live.** `gpt-5.5` verified end-to-end through this bundle against a real remote desktop, 2026-08-03 (`providers.py` `OPENAI.models`) |
| Windows target (WSL2 interop, local) | **Proven.** Capture and input both verified end to end — see `BACKLOG.md` |
| macOS target — capture, `key`, `focus_window` | **Proven** on real hardware |
| macOS target — `type_text` | **Proven** on real hardware. Was broken (keycode-0 events); fixed in `ccf0913`, re-verified 2026-09-15 on macOS 26.6.2 |
| Linux/X11 target (local) | Backend implemented; presence guard measured (`GUARD_MEASURED["linux-x11"] = True`) |
| Remote target over SSH | Full action set is implemented and dispatched, including `left_mouse_down`/`up`, `left_click_drag`, `scroll`, `hold_key`, and all four `desktop` window/clipboard actions — see [Remote action coverage](#remote-action-coverage) |
| Gemini | A dialect record exists in `providers.py` (`gemini-2.5-computer-use` → `computer_use`), built from captured traffic. **No live end-to-end run through this bundle is claimed.** |

A whole-session, end-to-end run with the hook, native tool promotion, screenshot
rewriting and the write gate all executing *together* is still listed as not done in
`BACKLOG.md` ("Run it as a real Amplifier session, end to end"). Component-level proof
is not product-level proof.

---

## 1. Upstream module version floor

Three Amplifier modules must carry recent changes, or this bundle either refuses to
mount or quietly degrades. **You do not need to check this up front** — see below.

**The package version numbers are useless as a floor.** All three still declare
`version = "1.0.0"` in their `pyproject.toml` and did not bump for these changes, so
this bundle does not ask you to pin anything by commit before you start. Instead:

**How the bundle checks.** It does *not* trust a version string — manifests can lie and a
shallow clone may not have one. `hook-computer-use` drives the actually-installed code
with throwaway probes and reads the real output:

- `_provider_derives_native_tool_betas` — calls the provider's own
  `_derive_native_tool_betas([{"type": "computer_20251124", "name": "computer"}])`
  and checks the returned betas mention computer-use. (Anthropic convention.)
- `_provider_recognizes_bare_computer_tool` — first calls OpenAI's pure,
  argument-free `get_native_computer_tool_spec()` seam and accepts only an
  exact `{"type": "computer"}` result. For a legacy provider without a usable
  seam, it calls `_convert_tools_from_request(...)` only when that provider
  explicitly reports `tool_search_mode == "off"`; namespaced conversion is
  stateful and is never probed. (OpenAI convention.)
- `_orchestrator_preserves_native_tool_spec` — drives loop-streaming's own
  `_build_tool_spec()` against a stub and checks the native `type` survives.

If the orchestrator check fails, mount raises
`ComputerUseNativeToolPassthroughUnsupportedError` and names the commits above. If the
*provider* check fails, the hook wraps nothing and logs which integration points it
tried — it does not raise, because a failed provider probe cannot distinguish "this
build predates the fix" from "this provider was never meant to support computer-use."

**Honest limitation, from the code's own docstring:** those two cases look identical from
outside. If your provider probe fails, check the commit — the probe cannot tell you which
it is.

---

## 2. The endpoint must support native computer use

The hook verifies only wire plumbing, not endpoint availability. It selects Anthropic's
dated tool dialect when beta derivation recognizes its canonical probe and OpenAI's bare
`computer` dialect when conversion preserves its canonical probe. No alias or version
table is a capability gate; the server remains authoritative for acceptance.

### OpenAI

The converter's successful bare `computer` probe selects the dialect for every alias or
future model identifier. It does not predict which identifiers the server will accept.

### Anthropic

Anthropic's tool type is versioned per model generation, and a model paired with the wrong
type is rejected with an HTTP 400 on *every* turn. `provider-anthropic` resolves it:

| Family | Version | `computer_use_tool_type` |
|---|---|---|
| opus | 4.6+ | `computer_20251124` |
| opus | 4.1 – 4.5 | `computer_20250124` |
| opus | below 4.1 | `None` — unsupported |
| sonnet | 4.6+ | `computer_20251124` |
| sonnet | 4.5 | `computer_20250124` |
| sonnet | below 4.5 | `None` — unsupported |
| haiku | 4.5+ | `computer_20250124` |
| haiku | below 4.5 | `None` — unsupported |

This bundle keeps its own independently-verified table in
`modules/tool-computer-use/.../providers.py`, keyed on the *undated* generation prefix:

```
claude-sonnet-4-5  -> computer_20250124
claude-sonnet-5    -> computer_20251124
claude-opus-5      -> computer_20251124
gpt-5.5            -> computer
```

A model **absent** from that table is *unverified*, not unsupported. The bundle never
invents a compatibility guess for it. Consequences at mount time
(`tool_versions.require_static_pairing`):

- You set `config.model` to a **known** model and a *conflicting* `tool_version` →
  `ToolVersionError`, mount fails loud with the correct value in the message.
- You set `config.model` to an **unverified** model with no `tool_version` →
  `ToolVersionError`. Set `tool_version` explicitly to unblock.
- You set neither → falls back to `computer_20251124`, then self-corrects at request
  time from the live model (`resolve_tool_version`, never raises).

**Recommendation: set neither `model` nor `tool_version`.** Request-time resolution reads
the model actually about to receive the request and always wins over stale config — which
also covers `provider-anthropic`'s own mid-session model fallback.

---

## 3. Pick a target

One config key decides everything downstream: `target` on the **tool** module.

```yaml
tools:
  - module: tool-computer-use
    config:
      target: ssh://user@host   # omit entirely for "this machine"
```

| `target` | Behaviour |
|---|---|
| absent | Local backends are probed **in order**: Windows(WSL2) → Linux X11 → macOS. First one available wins. If none is available, **nothing is mounted**, the reason is logged, and the session continues without the tools. |
| `ssh://user@host` or `ssh://host` | `RemoteBackend` is the **only** candidate. No fall-through to local. An unreachable target raises `RemoteTargetUnavailable`, which mount does **not** catch — deliberately, so an agent can never silently drive your own desktop when you asked for someone else's. |
| anything else (e.g. `user@host`) | Parse error. Logged at **ERROR**, tool not mounted. This used to escape silently and a session improvised its own `ssh` + `screencapture` workaround instead. |

---

## 4. Per-platform prerequisites

Each backend's `probe()` is cheap and never raises — it reports a reason. That reason is
what you will see in the log when the tools do not appear.

**Read your platform's hard requirement first.** Only one of the three is caught at mount
time; the other two let the tools mount and then fail on first use.

| Platform | Hard requirement | Enforced at mount? |
|---|---|---|
| Windows | **WSL2 on the Windows machine.** Native Windows is **not supported** | **Yes** — probe fails, nothing mounts |
| macOS | **Two separate TCC grants** — Screen Recording *and* Accessibility | **No** — mounts with neither; fails on first capture / first input |
| Linux | **An X11 session.** Wayland is not supported | **No** — and there is no Wayland check at all. See below |

### Windows — WSL2 is required. A plain Windows host is not supported.

> **You do not drive Windows directly. You drive it *from WSL2*, across the interop
> boundary.** `windows.py`'s own first line is `"""WSL2 -> Windows desktop backend."""`
> Every action crosses into Win32 through `powershell.exe` + `bridge.ps1`. There is **no
> native-Windows code path in this bundle.**
>
> A Windows machine without WSL2 — including one reachable over **Windows OpenSSH** —
> **cannot be a target today.** This is a missing implementation, not a config flag; it is
> tracked in `BACKLOG.md` under *Native Windows (no WSL2)*.

| Requirement | Probe check | Exact failure text |
|---|---|---|
| Running under WSL2 | `wslpath` on `PATH` | `wslpath not on PATH (not running under WSL2?)` |
| WSL↔Windows interop enabled (on by default in WSL2) | `powershell.exe` resolvable | `powershell.exe not found (tried: ...); WSL<->Windows interop must be enabled (on by default in WSL2), or set tool config 'powershell_path'` |

If you are on a normal Windows box and see `wslpath not on PATH`, nothing is misconfigured
— you have hit the boundary of what is built. Install WSL2 (and, for remote use, an SSH
server *inside* WSL — see §5), or drive that machine from a different controller.

`powershell.exe` is resolved **without depending on `PATH`** — a non-login SSH shell's
`PATH` does not include `/mnt/c/...`. Override with tool config `powershell_path`.

Every action spawns a fresh `powershell.exe` running `bridge.ps1` (Win32 P/Invoke via
`Add-Type`). This is a known latency cost, tracked in `BACKLOG.md` under Performance.

### macOS — both TCC grants are required, and neither is checked at mount

> **A successful mount tells you nothing about whether either permission exists.**
> `probe()` checks three things only: `sys.platform == "darwin"`, that Quartz imports, and
> that at least one display is active. **Both TCC grants are checked lazily, on first
> use** — so `computer` and `desktop` will appear in your session, look healthy, and then
> fail on the first screenshot or the first click.

| Requirement | Where enforced | Exact failure text |
|---|---|---|
| `sys.platform == "darwin"` | `probe()` | `not macOS (sys.platform='linux')` |
| `pyobjc-framework-Quartz` importable | `probe()` — installed by the tool module's `sys_platform == 'darwin'` marker | `pyobjc-framework-Quartz is not importable (...); install it to enable the macOS backend` |
| At least one active display | `probe()` | `zero active displays (screen may be asleep, or this is a clamshell-closed Mac with no external display attached)` |
| **Screen Recording** TCC grant | **Lazy, on first capture** | `CGDisplayCreateImage(N) returned no image: Screen Recording permission is NOT granted to this process (CGPreflightScreenCaptureAccess() == False)` |
| **Accessibility** TCC grant | **Lazy, on first input** | `Accessibility permission not granted: this process is not trusted to control the computer (AXIsProcessTrusted() == False)` |

Screen Recording and Accessibility are **two separate grants**, in two separate panes, and
granting one does not grant the other. A process can capture the screen perfectly while
every click and keystroke is silently discarded by WindowServer with no exception raised —
which is exactly why the Accessibility check exists rather than letting input no-op.

Grant both, before your first run: System Settings → Privacy & Security → **Screen
Recording**, and again under **Accessibility**, for the process actually running this code
(Terminal, the sshd-launched login shell, or the `python` binary itself — whichever the
Privacy pane lists after the first attempted action).

> **Do not run two agent processes against the same macOS target.** Two concurrent agents
> corrupt each other's Screen Recording grant: `CGDisplayCreateImage` then returns `None`
> for **both** — including the one that was capturing successfully a moment earlier — with
> no exception on either side. The transport is refcounted and shared per
> `(ssh_path, host)` specifically to prevent this; do not route around it.

### Linux — X11 only. Wayland is not supported, and not detected.

> **This backend speaks X11 and nothing else.** Its name is `linux-x11`; there is no
> Wayland backend in `registry.BACKEND_FACTORIES`.
>
> **The probe has no Wayland check.** It tests `DISPLAY`, an X connection, and XTEST — all
> three of which XWayland satisfies. On a Wayland desktop running XWayland, `probe()` can
> therefore report **available** and the tools will mount. What that reaches is the
> XWayland server, not the Wayland compositor. This bundle has **never verified input or
> capture under XWayland**, and makes no claim about it. Use a real X11 session.

| Requirement | Probe check | Exact failure text |
|---|---|---|
| `python-xlib` installed **in the running interpreter** | `probe()` | `python-xlib is not installed in the running interpreter (...); this backend cannot drive X11 without it` |
| `DISPLAY` set | `probe()` | `no DISPLAY set; no local X11 session to talk to` |
| **`DISPLAY` matches this user's own login session** (only when `DISPLAY` was picked up blindly from the environment, not set via explicit tool config `display`) | `probe()` | `DISPLAY=':99' (picked up from this process's environment) does not match the display this user's own login session has registered (':1', from systemctl --user show-environment); ...` |
| X server connectable | `probe()` | `cannot connect to X server ':0': ...` |
| XTEST extension present | `probe()` | `X server does not support the XTEST extension` |

The `python-xlib` message is deliberate: this used to surface as `'NoneType' object has no
attribute 'Display'`, which reads like an X connection fault and sends you looking at
`DISPLAY`/`xhost` instead of at the missing package.

**The session-match check exists because a healthy, reachable, XTEST-capable X server can
still be the *wrong* one.** A stray `Xvfb` left running from an earlier ship-gate session
(see `CONTRIBUTING.md`) satisfies every other check above identically to the real desktop
- `probe()` used to accept it, and every action would then "succeed" against a display
nobody is looking at, with zero errors. The check compares a blindly-picked-up `DISPLAY`
against `systemctl --user show-environment`'s own record of this user's session; it is
skipped entirely when `config.display` is set explicitly (a deliberate target, such as
`scripts/verify_coexistence.py`'s own `Xvfb`, is trusted as named). On a headless CI box or
a container with no systemd `--user` session at all, there is nothing to compare against,
so the blindly-picked-up `DISPLAY` is still trusted - this is the legitimate no-session
case, not the accidental one.

`XAUTHORITY` is resolved and set if absent (`~/.Xauthority`, then
`/run/user/<uid>/gdm/Xauthority`, then `/run/user/<uid>/.mutter-Xwaylandauth`).

**One more Linux requirement is also lazy, not probed:** no other X client may hold an
exclusive pointer/keyboard grab. Capture and `mouse_move` work regardless, so failing the
whole backend at mount would discard real capability — instead the first click/key/type
raises:

```
discrete input (click/key/type_text/scroll/drag) cannot reach application windows on
this X11 session: the root window's pointer and/or keyboard is already exclusively
grabbed by another client (XGrabPointer=1, XGrabKeyboard=1; 0 means available, nonzero
means already held elsewhere)
```

Most common cause, and verified on this backend's own reference machine:
`gnome-remote-desktop` / mutter holding an exclusive grab for a headless virtual seat —
independent of whether an RDP client is actually connected.

---

## 5. Remote targets over SSH

The claim is "if you can SSH to the box, you can drive its desktop." Concretely that means:

| Requirement | Detail |
|---|---|
| **Key-based auth** | The transport runs `ssh -T -o BatchMode=yes`. `BatchMode` disables every interactive prompt — a passphrase-locked or password-only key **will fail to connect**, it will not prompt. |
| **Host key already trusted** | `StrictHostKeyChecking=accept-new`. Never `no`. A host-key mismatch on a machine that types your passwords is refused. |
| **`uv` on the target — mandatory** | `SshTransport.connect()` calls `_resolve_uv_command()` **unconditionally**, before anything else. If `uv` is not found the connection raises `SshConnectError: could not find 'uv' on <host> (tried: ['uv', '$HOME/.local/bin/uv', '/opt/homebrew/bin/uv', '/usr/local/bin/uv'])`. There is **no `python3`-only path** — the `python3 -c` branch (`with_pillow: false`) is reached only *after* `uv` has already been resolved. `uv` is located by absolute path, never trusting `PATH`, because a non-login SSH shell's `PATH` is not guaranteed. |
| **Python 3.11+ on the target** | The remote agent is executed as `python3 -c <stub>` either way; the `python3` actually selected for that stub must be Python 3.11 or newer. Older Python exits with `remote agent requires Python >= 3.11` before the stub reads the payload or creates a scratch directory. |
| **The target's own per-platform prerequisites still apply** | SSH does not bypass §4. The remote agent runs the same `registry.select_backend()` on the far end, so a remote Windows target still needs WSL2, a remote Mac still needs both TCC grants, a remote Linux box still needs X11. |
| **No agent installed on the target** | The bundle's own files are tarred and pushed over the same stdin pipe as the protocol, and normally removed when the session ends. A crash can leave an incomplete scratch directory; a later v2 agent only reclaims a valid, unlocked lease after a 24-hour minimum retention period. Legacy, unknown, incomplete, or unsupported-locking cases stay untouched. No daemon, no new listening port, nothing to update or uninstall. **This is a claim about our agent, not about your setup** — the target still needs the prerequisites in the rows above and in §4. |
| **A network** | Tailscale/WireGuard is the tested arrangement; native `sshd` on port 22, no new port opened. |

One persistent `ssh -T` subprocess per target, shared and refcounted across every consumer
in the controller process.

**Remote defaults are deliberately stricter than local** (a remote machine is by
definition one you are not looking at):

| Key | Local default | Remote default |
|---|---|---|
| `read_only` | `false` | **`true`** |
| `gate_writes` | off | **on**, whenever `read_only` is off |
| `clipboard_read_policy` | `allow` | **`redact`** (length + digest, never the text) |

Turning `read_only` off on a remote target therefore cannot silently produce "full write
access, no gate" — the gate switches on in the same step unless you explicitly disable it,
which is logged at WARNING.

### Windows target reached over SSH — you SSH into WSL, not into Windows

> **The SSH server must be running *inside WSL2* on the Windows machine.** A remote
> Windows target is SSH → **the WSL2 side** → `powershell.exe` interop → Win32. Connecting
> to **Windows OpenSSH** lands you in a native Windows shell where `wslpath` does not
> exist, `probe()` returns `wslpath not on PATH (not running under WSL2?)`, and no tools
> mount. See §4.

So the full prerequisite list for a remote Windows desktop is: **WSL2 installed**, an
**SSH server running inside WSL**, **WSL↔Windows interop enabled** (default), and **`uv`
available to the SSH user**.

Once you are on the WSL side, `shutil.which("powershell.exe")` still **fails**, because a
non-login SSH shell's `PATH` does not contain `/mnt/c/...`. The absolute-path resolution
in `windows.py` is load-bearing, not a wart. If you have a custom WSL mount root, set
`powershell_path`.

### Remote action coverage

The full action set is implemented over the remote wire. Verified in
`remote_backend.py`/`remote_agent.py` — every action below is a real entry in
`RemoteAgent._HANDLERS` (`remote_agent.py:760`), not a client-side approximation:

| Action | Remote |
|---|---|
| `screenshot` / `zoom`, `mouse_move`, clicks, `type`, `key`, `cursor_position`, monitor selection | Works |
| `left_mouse_down`, `left_mouse_up`, `left_click_drag`, `scroll`, `hold_key` | Works |
| `desktop.list_windows`, `desktop.focus_window`, `desktop.get_clipboard`, `desktop.set_clipboard` | Works |

`left_mouse_down`/`left_mouse_up` are tracked in the remote agent's held-input ledger, so a
link death between the two calls still releases the button. `left_click_drag` crosses the
wire as one atomic `drag` call — never decomposed into mouse_down/move/mouse_up — so a link
failure mid-drag cannot strand a held button either.

---

## 6. Configuration reference

Every key below is read by the code. Defaults are the code's actual defaults, not
aspirations. `behaviors/computer-use.yaml` ships a minimal subset.

### `tool-computer-use`

| Key | Default | Meaning |
|---|---|---|
| `target` | *(absent)* | `ssh://user@host` for a remote desktop. Absent = probe local backends |
| `max_edge` | `1280` | Long edge of the image the model sees |
| `max_pixels` | `1150000` | Pixel-count ceiling, applied with `max_edge` |
| `enable_zoom` | `true` | Advertise the `zoom` action in the native spec |
| `read_only` | `false` local / **`true` remote** | Enforced in code — every mutating action is rejected before anything reaches the desktop. Screenshots still work |
| `gate_writes` | `is_remote and not read_only` | Per-action human approval for mutating actions |
| `tool_version` | *(auto)* | Native tool type override. Leave unset — see §2 |
| `model` | *(unset)* | Static model hint used to validate `tool_version` at mount |
| `target_monitor` | `"primary"` | A monitor id, `"primary"`, or `monitors.VIRTUAL_DESKTOP` for the whole bounding box |
| `clipboard_read_policy` | `allow` local / `redact` remote | `allow` \| `redact` \| `block` |
| `type_pacing_ms` | *(auto)* | Inter-character delay. Auto = wide enough to keep the presence guard unmasked when one is active; `0` forces full speed (logged at WARNING) |
| `coexistence.enabled` | `true` | **Legacy alias of `coexistence.announce`** (declines session-start disclosure only). No longer affects whether the halt/pause/target-binding/exclusion guard is built - that guard is unconditional whenever the backend supports presence detection. Setting `false` refuses to *mount* if a human is currently detected present (logged, `docs/designs/coexistence.md` §7.6); proceeds without disclosure, loudly, if nobody is |
| `coexistence.announce` | `true` | Decline session-start disclosure only. Same gated behavior as `enabled` above - never affects the halt invariant |
| `coexistence.drive_anyway` | `false` | Permit *beginning* to drive when a human is already detected present. Logged |
| `powershell_path` | *(auto)* | Windows backend override |
| `ssh_path` | `"ssh"` | Remote only |
| `connect_timeout` | `30.0` | Remote only |
| `deadman_seconds` | `5.0` | Remote agent self-terminates if the controller goes away |
| `with_pillow` | `true` | Remote only — provision Pillow on the target via `uv` |

### `hook-computer-use`

| Key | Default | Meaning |
|---|---|---|
| `max_inline_screenshots` | `3` | Most-recent screenshots kept inline; older ones collapse to text so a long session stays affordable |
| `priority` | `50` | Hook registration priority |
| `unattended_writes_ok` | `false` | See below. Explicit, logged, never inferred. Also satisfies `tool-computer-use`'s `gate_writes` for `focus_window`/`set_clipboard` — see §7 |

---

## 7. The safety model

Four distinct mechanisms. Know which one you are relying on.

| Mechanism | Kind | Strength |
|---|---|---|
| `read_only: true` | Code | **Enforced.** Every mutating action rejected in `execute()` before it reaches the desktop |
| Write gate (`gate_writes`) | Code + human | **Enforced.** Per-action `ask_user` approval, default **deny**, for 15 mutating actions |
| Presence guard / halt | Code | **Enforced and unconditional.** No config key can disable the halt once a guard exists |
| Stop Conditions in `agents/computer-operator.md` | Prompt | **Model judgment.** Nothing inspects the screen or blocks an action |

### The write gate

On a remote, non-read-only target, every one of these prompts for approval before it runs:
`mouse_move`, `left_click`, `right_click`, `middle_click`, `double_click`, `triple_click`,
`left_mouse_down`, `left_mouse_up`, `left_click_drag`, `scroll`, `key`, `hold_key`, `type`,
`focus_window`, `set_clipboard`.

"Destructive" is undecidable from a click — Delete looks like every other click — so the
only two honest options are gate-every-write or gate-none. This gates every write.

**If stdin is not a TTY** (a backgrounded run, a piped stdin, a service with no controlling
terminal) the gate **denies** with a named reason and the write is not sent. It does *not*
hand the prompt to the approval system, because that system's own `input()` hits immediate
EOF and surfaces as `Tool computer failed: EOF when reading a line` — a message that names
nothing, and which was once misread as "the remote write path was never wired up." It was
not; writes work fine.

### `unattended_writes_ok` — read this before you set it

```yaml
hooks:
  - module: hook-computer-use
    config:
      unattended_writes_ok: true    # deliberate, logged, never a default
```

This is the **explicit opt-out** for a run you launched on purpose, against a target you
already named, with nobody at the keyboard. It only changes the one path that used to
crash instead of asking: no TTY available. The interactive path is unchanged and still
prompts. Every auto-allow is logged at WARNING naming the tool, the action, and the
backend.

It is not a convenience toggle to make prompts go away. If you are running interactively
and finding the prompts tedious, you want `read_only: true` (look, don't touch) or
`gate_writes: false` (also logged at WARNING) — not this.

**`unattended_writes_ok` and `gate_writes` are two answers to the same policy question,
not two independent gates.** `gate_writes` (`tool-computer-use`'s config) decides *whether*
a mutating desktop action (`focus_window`, `set_clipboard`) needs approval at all;
`unattended_writes_ok` (this hook's config, above) is *how* that approval can be granted
when nobody is at a terminal to answer a prompt. This hook syncs its live
`unattended_writes_ok` value onto the mounted `ComputerTool` on every call, so
`tool-computer-use`'s own fail-safe check agrees with this hook's decision instead of
silently re-denying an action this hook already approved. Concretely: set
`unattended_writes_ok: true` here and `focus_window`/`set_clipboard` work on a gated
remote target with no human confirmation — a deliberate, logged choice, not a default. Set
nothing (no hook, no `unattended_writes_ok`, no explicit `gate_writes: false`) and those
two actions stay refused, with the refusal message naming all three ways out.

### Presence guard and halt

The guard reconciles the target's own idle-time counter against the agent's own injection
timestamps, per elementary event, and **halts before the next write** the moment a human is
detected at the machine. There is no configuration key that can disable that halt.

A guard is only built for a backend that exposes `presence_idle_ms()` **and** resolves to a
platform with a measured guard band. Never on a guessed number:

| Platform | Guard band | Measured? |
|---|---|---|
| `linux-x11` | 5.0 ms | Yes — 98 samples, zero false positives |
| `macos` | 10.0 ms | Yes — 300 samples on real hardware, 0/300 false positives |
| `windows-wsl2` | 20.0 ms | Yes — 900 samples (3×300) on a live Win11 desktop, 0/900 false positives |

For a remote target the guard uses the *remote machine's* measured band, from its own
handshake — network latency is never folded into it.

Two honest caveats recorded in the code:

- Intra-`type_text` detection is **not viable on Windows** at any of these bands (masked
  fraction 20/60 = 33% at production cadence).
- An open question is recorded in `presence.py` about one live Windows halt at 297 ms idle
  that may have been a false positive rather than a detection. It proves the halt *path*
  executes; it is not yet proof of human detection on Windows.

### Halt is durable across sessions

Once halted, the guard is a **one-way latch** — nothing on the class can clear it. But an
orchestrator can start a *new* session with a *new* guard that has no memory of the halt.
That was observed for real: a sub-agent halted five times, control returned to the parent
session, and its first click succeeded 80 s later, entirely automatically, with no human
choosing to resume.

So the halt is also written to disk and consulted whenever a new guard is built. It is
cleared by exactly one path — a human running:

```bash
python scripts/resume_after_halt.py            # list halted backends
python scripts/resume_after_halt.py linux-x11  # clear one
python scripts/resume_after_halt.py --all      # clear every backend
```

There is **no time-based expiry**, on purpose. Resume requires an explicit signal, not the
mere passage of time. Nothing on the automated tool-call path ever clears it.

Separately, `hook-computer-use` injects a standing system reminder on every subsequent
tool call for the rest of a session in which a halt fired, so the model cannot close out a
turn reporting clean success without acknowledging the interruption.

### Two risks that no mechanism here closes

**On-screen content can manipulate the agent.** Anything the agent can read, it can be
influenced by. A dialog, web page, or document saying "click OK to confirm" is
indistinguishable from legitimate UI. Inherent to computer use, not a defect in this
bundle. Do not point it at untrusted screens unsupervised.

**The clipboard goes to your model provider.** `desktop.get_clipboard` returns content as
tool output, which becomes part of the conversation sent to the API and lands in durable
logs. Default policy is `allow` locally. If you just copied a secret, clear the clipboard
first, or set `clipboard_read_policy: redact` / `block`.

---

## 8. Operational facts that cost real debugging time

### A locked screen cannot be driven. On any platform.

This is not a bug and it is not fixable. macOS and Windows both switch to a secure session
that refuses synthetic input by design.

**The requirement is an unlocked, logged-in GUI session — not merely "the screen is on."**
A sleeping *display* is usually fine. A sleeping or locked *system* is not.

The dangerous part is that a locked screen does not look like a failure. Measured against a
real Mac (commit `7d98701`):

```
LOCKED    ioreg CGSSessionScreenIsLocked -> True    screencapture   144,435 bytes
UNLOCKED  same                           -> False   screencapture   690,038 bytes
```

Both are real, plausible images. The tool previously handed that 144 KB **lock screen** to
a model as if it were the desktop, and accepted keystrokes macOS silently discards —
reporting success both times.

**It now fails loud.** Detection:

| Platform | Signal |
|---|---|
| macOS | `ioreg -n Root -d1 -a` → `CGSSessionScreenIsLocked` and/or `IOConsoleLocked`. Three states are distinguished: `locked`, `no_gui_session` (nobody logged in at the console), `unknown` |
| Windows | `LogonUI.exe` process presence, dispatched inside the existing per-action bridge call. `bridge.ps1` throws `SESSION_LOCKED` for capture/write actions |

An `ioreg` timeout returns `unknown` and **refuses** rather than guessing either way —
neither silently "unlocked" (which lets a lock screen straight through) nor silently
"locked" (which would block a healthy desktop on a transient hiccup).

The Windows *unlocked* case is verified on real hardware (`LogonUI=0` while unlocked). The
Windows *locked* case is **not verified on Windows hardware** — stated plainly rather than
claimed.

There is no lock check on the Linux/X11 backend.

### A locked session and a missing macOS permission grant look identical

This is the reason the check above exists. From outside, with no check:

- `CGDisplayCreateImage` returns a real, plausible-looking image when locked — not `None`,
  not an error.
- `CGEventPost` silently drops every click and keystroke sent to a locked session, exactly
  the way it drops them when Accessibility is not granted.

That ambiguity produced a confidently-wrong diagnosis — *"the signature of Accessibility
TCC not granted"* — which stood in the record as fact for days. It was a locked screen.

The tool now checks, on **both** the capture and the input path, and the **lock check runs
before the Accessibility check**, so a locked-and-untrusted session is diagnosed as
LOCKED. The error text names the state, names the host, says what a human must do, and
explicitly warns that a lock-screen capture is a real, plausible-looking image.

When you see a macOS failure here, read which of the three it says. They are different
problems with different fixes:

| Diagnosis | Fix |
|---|---|
| `LOCKED` | Unlock the screen (sign back in) on the target host |
| `no GUI session` | Log in at the physical console, or via Screen Sharing |
| `Screen Recording permission is NOT granted` | Grant it in System Settings, to the process actually running this code |
| `Accessibility permission not granted` | Same, under Accessibility |

---

### macOS capture fallback: per-display, and a multi-display compositor

If native `CGDisplayCreateImage` returns `None`, capture may make one bounded
`/usr/sbin/screencapture` attempt **per display**. When exactly one display is active it
uses `-m` and requires that display to remain the sole main display with unchanged physical
geometry. With several displays active it uses `-D <1-based ordinal>` into the active
display list, and requires that display to still be present with unchanged geometry and the
list itself not reordered — the ordinal is an index into that list, so a reorder would
silently retarget the capture.

Both forms take the same guards: a fresh positive Screen Recording preflight and an unlocked
session before the child, the unlocked state and display identity rechecked after it, the
private temporary PNG decoded into memory, and unexpected dimensions rejected. The temporary
directory and file are private; cleanup is attempted on every path, and a cleanup failure is
reported explicitly because private capture data may remain.

Whole-virtual-desktop capture with several displays active composites those per-display
captures into one canvas — the point-space bounding box of every active display at the
**largest** backing scale among them, which is the geometry
`CGWindowListCreateImage(CGRectInfinite, ...)` itself produces. Before the composite is
accepted, every placement input is re-read and compared against the snapshot the canvas was
built from: the active ID list and its order, each display's backing scale, and each
display's full bounds. A display that moves or resizes mid-composite invalidates it.

**Native first, everywhere. All of the above is the exception path.**

`CGDisplayCreateImage` answers a per-display or region capture, and
`CGWindowListCreateImage` answers a whole-virtual-desktop capture, whenever they are
healthy. On a macOS where they are, none of the `screencapture` machinery above ever runs
and it costs nothing. This is deliberate and it is why there is no OS-version check
anywhere in this backend:

| | macOS 26.6.2 (25G83) | macOS 26.7 (25G229) |
|---|---|---|
| `CGDisplayCreateImage` | ~5.0s → **NULL** | 0.02–0.08s → real image |
| `CGWindowListCreateImage` | **30.04s** → a correct image | 0.07s → a correct image |

Same machine, measured either side of one OS update. A version table would have encoded
those two observations as a rule, and been wrong about 26.0–26.5 (never measured) and about
whatever Apple does next.

**What makes native-first safe when native is broken** is that a call which behaved
pathologically once is never attempted again in that process. Note the two signatures
differ, and only one looks like a failure: `CGDisplayCreateImage` returns `NULL`, while
`CGWindowListCreateImage` returns *exactly the right image*, just far too late to use — 30s
is also the SSH transport's per-op timeout, so over the wire it does not return a slow
image, it drops the connection. Only the **duration** catches the second one. A native call
cannot be cancelled once started; it can be refused a second time, and that is the whole
mechanism.

A session whose very first capture is a whole-desktop capture has nothing learned yet. It
settles the question with `CGDisplayCreateImage` (~5s worst case) rather than
`CGWindowListCreateImage` (~30s), so **nothing ever pays 30 seconds to discover that
something costs 30 seconds**. That inference — a healthy per-display call means a healthy
whole-desktop call — is the one soft spot: they are different calls and could in principle
diverge, in which case the first whole-desktop capture pays once and the session never pays
again.

The degraded fact lives on the backend instance and is **never persisted**. The remote agent
is one process per session, so an OS update takes effect on the next session with no cache
to invalidate — which is not a hypothetical: the update in the table above landed mid-review
of this change.

**Failure policy.** A guard that refuses — permission, session, topology, budget, or a
cleanup failure — is reported to the caller. It is never answered by trying a different
capture: doing so would return an image taken after an explicit refusal, or report success
while a private capture file remained on disk.

If the compositor itself cannot be set up — for example a pyobjc without the bitmap-context
symbols — that is **reported, not retried**. There is deliberately no fallback to
`CGWindowListCreateImage` at that point: the native call was either skipped as degraded or
returned `None`. Re-attempting a call known to be pathological costs ~30s on
the macOS where that is true, which is also the SSH transport's per-op timeout. The "retry"
would drop the connection rather than produce an image.

The session state is re-read at **two** points where wall-clock has passed since the entry
check: after the health probe and before the real native capture, and again before
compositing. A native capture call is not free, and the time it consumes is time in which a
screen can lock — after which a locked screen returns a real, plausible-looking image.

This is a conservative fallback, not a permission prompt or reset. A positive preflight does
not establish that the utility has the same TCC attribution, and none of these checks can
make topology or permission use atomic — the final whole-layout revalidation is a best-effort
consistency check, not atomic topology access, and the layout can move again immediately
after it passes. The 20-second fallback budget includes prior capture and setup work; the
child receives only time remaining. It leaves a usual 10-second margin below the default
30-second wire timeout for encoding, but does not guarantee a hard wall time. Existing
diagnostics are unchanged and this does not claim to diagnose or fix a physical display
condition.

Offline logic tests cover this adaptation, and it **has** now been verified on real macOS
hardware: [exact-head report on PR #13](https://github.com/microsoft/amplifier-bundle-computer-use/pull/13#issuecomment-5688667363).
That run drove **macOS 26.6.2 (25G83)** with a **single active 5120x1440 display**, over the
SSH production path (`registry.select_backend({"target": "ssh://..."}) -> RemoteBackend
.connect() -> capture_scaled()`), and exercised **both full-screen and region capture**; no
guard refused spuriously across four consecutive runs. On that machine the native
`CGDisplayCreateImage` returned `None` in ~5.0s and `screencapture` completed in ~0.23s.

The multi-display paths were verified on the same machine with a second display attached, a
genuinely mixed-DPI pair — a 2x built-in (1728x1117 points at the origin) beside the 1x
5120x1440 ultrawide. Whole-desktop capture returned the same 13696x2880 canvas as
`CGWindowListCreateImage` in 0.47s against its 30.04s, and region capture — which fails
outright on macOS 26.6.2 without the per-display form — returned an exact 800x600 crop.

**Not** covered by any of those runs, stated so it is not inferred: three or more displays
and non-top-aligned display arrangements.

**Known limits, accepted deliberately rather than left ambiguous:**

- **Mixed-DPI screenshot/input coordinate mismatch** — inherited from the pre-existing
  backend, not introduced here. A virtual-desktop screenshot spans displays at the largest
  backing scale, while input coordinates map through a single scale factor. Fixing it means
  per-display coordinate mapping in the *input* path and its own hardware verification, so
  it is out of scope for this capture work.
- **No hard native-call deadline.** CoreGraphics offers no way to cancel a capture call in
  flight. The 20-second budget is elapsed-time accounting for the child process; a
  pathological native call is bounded by *never being repeated*, not by being interrupted.
- **Three or more displays, and secondary `-D` ordering there**, are unverified. `-D`
  ordinals index `CGGetActiveDisplayList`, whose contract puts main first; that contract is
  relied upon rather than re-checked.
- **Non-top-aligned arrangements** are unverified.
- A positive preflight still does not establish that the `screencapture` utility has the same
  TCC attribution, and the topology and permission checks remain non-atomic.

The capture-alternative lead was reported by
[@colombod in PR #11](https://github.com/microsoft/amplifier-bundle-computer-use/pull/11).

---

## 9. Known issues

### macOS `type_text` silently no-ops while returning success — FIXED

**Status: fixed** in `ccf0913` (2026-08-04). This section described it as open for six
weeks after the fix landed, because that commit changed `macos.py` and its tests and
never touched this file or the README. The stale label is itself part of the record — an
agent reading it will tell a user the feature is broken.

**What the defect actually was.** `type_text` posted a keycode-**0** event carrying the
string via `CGEventKeyboardSetUnicodeString` — Apple's documented "arbitrary Unicode, no
layout table" technique. `CGEventPost` accepts that event, signals nothing, and macOS
delivers nothing. It was confirmed at both `kCGHIDEventTap` and `kCGSessionEventTap`, in
process and over the remote-agent wire.

**The hypothesis this section used to carry — "the type path posts events to a specific
app rather than the system-wide event tap" — was wrong.** Both paths used the same tap;
only the keycode differed. It is left named here rather than deleted, because it is the
kind of plausible, evidence-shaped guess that cost this defect two wrong retractions.

**The fix.** Every character resolves to a real, non-zero keycode (plus Shift where
needed) through the same US-ANSI table `key()` already depends on, and `CGEventSetFlags`
is called unconditionally — skipping it for the no-modifier case left plain lowercase and
digits undelivered while shifted characters landed. A character with no keycode on that
layout raises `BackendError` naming every unsupported character, typing nothing at all,
rather than silently falling back to the technique measured to deliver nothing.

**Re-verified 2026-09-15** on macOS 26.6.2 (25G83), driven remotely over SSH, using this
project's own Spotlight method — reading the pixels back, not just checking that no
exception was raised:

```
key("cmd+space")                     -> Spotlight opened
type_text("amplifier typing test")
capture                              -> Spotlight field reads: amplifier typing test
                                        (and returned live results for that query)
key("escape")                        -> dismissed
```

**Impact:** none outstanding. `type` on macOS works. See also `BACKLOG.md`'s
"RETRACTED 2026-08-03" entry, which is *also* wrong — it retracted a real defect as a
locked-screen artifact, on a re-test that never compared pixel content.

### Other stated gaps

- No end-to-end whole-session run of all mechanisms together (`BACKLOG.md`).
- Windows on-desktop indicator overlay is not built (Linux and macOS announce are).
- The held-input ledger has no release path if the agent process is `SIGKILL`ed or OOMs.

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `computer` / `desktop` tools absent, no error | No backend available. Mount catches `NoBackendAvailable`, logs the reason, mounts nothing | Read the log line — it lists every candidate's own probe reason plus remediation |
| `wslpath not on PATH (not running under WSL2?)` | You are on native Windows, or SSH'd into **Windows OpenSSH** rather than into WSL | Not a misconfiguration — native Windows is unsupported. Install WSL2 and (for remote) run sshd inside it. §4 |
| `could not find 'uv' on <host>` | `uv` missing on the remote target. It is **mandatory**, not optional | Install `uv` for the SSH user on the target. §5 |
| Tools mount on macOS, then first screenshot or first click fails | Neither TCC grant is checked at mount — both are lazy | Grant Screen Recording **and** Accessibility. §4 |
| Tools mount on a Wayland desktop, input does not behave | The probe has no Wayland check; it attached to XWayland | Use a real X11 session. §4 |
| `tool-computer-use: NOT MOUNTING - invalid configuration` | Malformed `target` (e.g. `user@host` instead of `ssh://user@host`) | Fix the config. Logged at ERROR because it is a mistake someone can fix |
| `ComputerUseNativeToolPassthroughUnsupportedError` at mount | `loop-streaming` predates PR #36 | Upgrade — the error names the exact commit |
| `ComputerUseHookIncompatibleProviderError` | Provider gained a `stream()` method. The hook only wraps `complete()`, and the orchestrator prefers `stream()` whenever present — wrapping would silently do nothing | Refuses to operate rather than degrade invisibly. Wrap both, or use an orchestrator that does not prefer `stream()` |
| Log: `no provider found to wrap` | Provider lookup failed this turn; screenshots will not inline | Check the provider is mounted |
| `'NoneType' object has no attribute 'Display'` | *(fixed)* Should now read "python-xlib is not installed" | For a `uv tool` installation, run `uv tool install amplifier --with python-xlib`; from a source checkout, run `uv pip install python-xlib` in its environment |
| Tool works, targeting is noticeably poor | Native promotion silently degraded to a plain function tool | Check the trace (below) for a `markers=` line; check module commits |
| `Tool computer failed: EOF when reading a line` | *(fixed)* Approval prompt with no TTY | Run interactively, or set `unattended_writes_ok: true` |
| PowerShell banner text where JSON was expected | `bridge.ps1` missing from the deployed payload | Should not occur — it is in `PAYLOAD_MODULES`. File an issue |
| `SESSION_LOCKED` / `this macOS session is LOCKED` | Target is locked | Unlock it. §8 |
| `ToolVersionError` at mount | `model` / `tool_version` conflict, or an unverified model with no override | §2 |

### Trace

```bash
AMPLIFIER_COMPUTER_USE_TRACE=/tmp/cu-trace.log amplifier run --bundle computer-use-behavior "..."
```

```
MOUNTED max_inline=3
WRAPPED provider=AnthropicProvider module=amplifier_module_provider_anthropic
complete: markers=1 messages_with_blocks=3
```

No `markers=` line → screenshots are not reaching the model.

---

## 11. Minimal working config

These are complete, copy-pasteable **behavior files** — every module needs a `source:` key
naming where to fetch it from, which a bare `tools:`/`hooks:` fragment (no filename, no
path) cannot show. Save either one as e.g. `my-computer-use.yaml` and register it with
`amplifier bundle add file:///path/to/my-computer-use.yaml --app` (see §0 above), or copy the
`tools:`/`hooks:` blocks into your own existing behavior file.

Local desktop, look-only — the safest first run (`my-computer-use.yaml`):

```yaml
bundle:
  name: my-computer-use
  version: 0.1.0
  description: Minimal local, look-only computer-use config

tools:
  - module: tool-computer-use
    source: git+https://github.com/microsoft/amplifier-bundle-computer-use.git@main#subdirectory=modules/tool-computer-use
    config:
      read_only: true

hooks:
  - module: hook-computer-use
    source: git+https://github.com/microsoft/amplifier-bundle-computer-use.git@main#subdirectory=modules/hook-computer-use
    config:
      max_inline_screenshots: 3
```

Remote desktop, interactive, gated writes (`my-computer-use.yaml`):

```yaml
bundle:
  name: my-computer-use
  version: 0.1.0
  description: Minimal remote, gated-write computer-use config

tools:
  - module: tool-computer-use
    source: git+https://github.com/microsoft/amplifier-bundle-computer-use.git@main#subdirectory=modules/tool-computer-use
    config:
      target: ssh://user@host
      read_only: false        # gate_writes turns on automatically in the same step

hooks:
  - module: hook-computer-use
    source: git+https://github.com/microsoft/amplifier-bundle-computer-use.git@main#subdirectory=modules/hook-computer-use
    config:
      max_inline_screenshots: 3
```

Run this one from a real terminal. Without a TTY every write is denied — by design.

If you just want the bundle's own defaults with no customization, you do not need either
of these — register the bundle itself (§0) and use `behaviors/computer-use.yaml` as shipped.

---

## See also

- `CONTRIBUTING.md` — development environment, test suite, the evidence standard
- the design notes — the SSH transport design and its threat model
- the design notes — presence detection, halt invariant, target binding
- `BACKLOG.md` — what is known, wanted, and deliberately not done yet
