# amplifier-bundle-computer-use

Give an Amplifier session real control of a **desktop** — Windows (**via WSL2**), macOS,
or Linux (**X11**) — on this machine or another one across your private network, using the
**provider-neutral computer intent and canonical actions**. Providers decide whether
to compile that intent into a supported native tool or keep ordinary function support.

If a human could do it by looking at the screen and clicking, this can do it. No API, no
CLI, no browser extension required.

```
"What's on my screen right now?"
"Click the Export button in that accounting app and save it to my desktop."
"Fill this dialog in for me — I'm done pressing buttons."
```

> ## ⚠️ Read `docs/SETUP.md` before you install
>
> **This is not the usual one-line, `--app`-level pointer at a behavior bundle.** It
> drives a real desktop, so the install has four moving parts that live outside this
> repository: a **version floor on three upstream modules**, a **model that supports
> native computer use**, a **target machine with per-platform prerequisites**, and — for
> remote targets — **SSH key auth and `uv` on the far end**.
>
> **The per-platform prerequisites are hard requirements, not recommendations:**
>
> | Target | Requires |
> |---|---|
> | **Windows** | **WSL2.** A plain Windows host — including one reachable over Windows OpenSSH — is **not supported.** You SSH into the WSL2 side; every action crosses into Win32 via `powershell.exe` interop |
> | **macOS** | **Two separate TCC grants** — Screen Recording (capture) *and* Accessibility (input). Neither is checked at mount; the tools appear and then fail on first use |
> | **Linux** | **An X11 session.** Wayland is not supported, and not detected — under XWayland the probe can pass anyway |
>
> **[→ docs/SETUP.md](docs/SETUP.md)** has the exact checks, the proven-vs-rough
> matrix, the full config reference, and the operational facts (a locked screen
> cannot be driven, on any platform) that otherwise cost you an afternoon.

---

## Install

Registering this bundle is ordinary Amplifier bundle management:

```bash
# 1. Register the behavior (its name comes from behaviors/computer-use.yaml)
amplifier bundle add git+https://github.com/microsoft/amplifier-bundle-computer-use@main#subdirectory=behaviors/computer-use.yaml --app

# 2. Use it for a session
amplifier run --bundle computer-use-behavior "What's on my screen right now?"

```

Confirm it registered correctly with `amplifier bundle show computer-use-behavior` — it
should list `tool-computer-use`, `hook-computer-use`, and `computer-use:computer-operator`.
Working from a local clone instead of GitHub? `amplifier bundle add
file:///path/to/amplifier-bundle-computer-use#subdirectory=behaviors/computer-use.yaml --app` instead.

**Registering the bundle is not the same as the tools working.** See
**[docs/SETUP.md](docs/SETUP.md)** for the four things that decide whether `computer` and
`desktop` actually function once a session starts: upstream module versions, endpoint
support, a reachable target machine, and (for remote targets) SSH.

---

## The neutral ownership boundary

`ComputerTool.native_tool_spec` carries explicit `computer_use.version=1` intent
through existing `ToolSpec` extras. Its operations describe the local screenshot/input
executor; cached `image_space` dimensions are screenshot pixels, never physical monitor
pixels or a guessed display size. Unknown dimensions are omitted without discovery.
Window/geometry helpers remain ordinary functions, also available through `desktop`.

**Providers own models, endpoint support, native declarations, headers, vendor units,
safety requirements and replay.** This bundle has no model tables, vendor action readers,
or provider-private capability probes. A tool named `computer` or `screenshot` without
intent is ordinary; intent works under a local alias. Unsupported native semantics
must fall back to ordinary functions or refuse a native-required request.

Historical real desktop captures are retained in `tests/fixtures/captures/`; they
do not qualify this new boundary. Independently checked evidence lives in
`tests/fixtures/neutral-qualification.json` with its expected package bindings and
hashed receipts. Ordinary PR/main CI checks its archived **AS RECORDED** integrity,
not current bindings or freshness. The separate pre-release command
`.venv/bin/python -m pytest release_checks/ -q` rejects missing, mismatched, tampered
or expired evidence (after 30 days) and must pass both SDK rows; see `CONTRIBUTING.md`.
Native live evidence is restricted to screenshot-only declarations;
input-policy evidence covers exact approved alphanumeric input on owned Linux X11,
not general text fidelity, all native operations, or other platforms.

## The one thing that had to be solved

Amplifier's orchestrator used to stand between a mounted tool and native computer use:
tool results are collapsed to `str` before reaching the provider, so a screenshot could
never travel back as an image.

That is fixed at **one seam** — the provider's `complete()` call — by
`hook-computer-use`, which:

- unwraps the orchestrator's `ToolResult` envelope and expands screenshot markers into
  real image blocks,
- keeps only the **N most recent** screenshots inline so long sessions stay affordable,
- preserves complete error/progress/reference text without vendor-specific user turns.

(`ToolSpec` used to be built from `name`/`description`/`parameters` only, so a tool
couldn't declare itself a server-side tool type either — `hook-computer-use` used to
promote it and inject the beta header itself. That is now handled upstream:
`loop-streaming` preserves a tool's `native_tool_spec` through its own `ToolSpec`
construction. Providers consume the neutral intent during request assembly;
`hook-computer-use` checks the known orchestrator's extra preservation, not provider
capabilities. It refuses an unsupported public `stream()` path loudly.)

Without the hook, marker paths are not expanded into image history. Native declarations
remain the provider's responsibility. Nothing is monkey-patched on disk.

## Components

| Piece | Role |
|---|---|
| `modules/tool-computer-use` | Neutral intent, canonical execution, scaling and desktop safety |
| `modules/hook-computer-use` | Generic copied image/error history plus write/halt policy |
| `agents/computer-operator.md` | Operating discipline for driving a live machine |
| `bridge.ps1` | WSL2 → Windows execution via `powershell.exe` + Win32 |

### Tools

**`computer`** — the canonical action set: `screenshot`, `zoom`, `cursor_position`,
`mouse_move`, `left_click`, `right_click`, `middle_click`, `double_click`,
`triple_click`, `left_mouse_down`, `left_mouse_up`, `left_click_drag`, `scroll`, `key`,
`hold_key`, `type`, `wait`, plus `screen_info`, `list_windows`, `focus_window`. `key` also
accepts an optional integer `repeat` from 1 to 100 (default 1); each press is independently
guarded, and a target or policy change stops the remaining presses.

Flat calls remain supported. V1 batches use the same action/parameter vocabulary:

```json
{"version": 1, "actions": [
  {"action": "mouse_move", "coordinate": [426, 87]},
  {"action": "key", "text": "ctrl+s", "repeat": 1}
], "capture_after": true}
```

The entire batch is validated before disclosure/input. Batch durations are bounded
to 0–60 seconds; scroll amounts are wheel notches. Nested writes cannot evade approval.
Execution stops on denial, failure, cancellation, halt or binding/policy change.
Progress reports completed actions and uncertain in-flight input; no rollback or replay
is implied. `capture_after` takes one full final screenshot unless the terminal action
already produced one. Earlier screenshots and cropped zooms never satisfy it.

**`desktop`** — what the native schema cannot express: `list_windows`, `focus_window`,
`screen_info`, `get_clipboard`, `set_clipboard`, `list_monitors`, `select_monitor`.
Focus a window before typing into it; use the clipboard to pull exact text out of an app;
use `select_monitor` to switch which monitor the model sees, mid-session.

> **Every action above works against a remote (`ssh://`) target**, including
> `left_mouse_down`/`left_mouse_up`, `left_click_drag`, `scroll`, `hold_key`, and
> all four `desktop` window/clipboard actions. Each is a real entry in
> `RemoteAgent._HANDLERS` (`remote_agent.py:760`); `left_click_drag` crosses the
> wire as one atomic `drag` call rather than a decomposed
> mouse_down/move/mouse_up sequence, so a link failure mid-drag cannot strand a
> held button. See `docs/SETUP.md` §5.

## Coordinates

The display is captured at full physical resolution, downscaled to a model-friendly size
(default 1280 long edge, aspect ratio preserved), and coordinates the model emits are
scaled back to physical pixels. On a 3840×2160 display that is an exact 3× factor.

Anthropic's guidance is explicit: sending screenshots above WXGA is both slower **and**
less accurate. Raising `max_edge` is usually the wrong lever.

## Configuration

The keys that matter most. **Full reference in [`docs/SETUP.md`](docs/SETUP.md) §6.**

```yaml
tools:
  - module: tool-computer-use
    config:
      target: ssh://user@host     # omit entirely to drive THIS machine
      max_edge: 1280              # long edge of the image the model sees
      enable_zoom: true
      read_only: false            # true = screenshots only, all input blocked
      # Model/native wire settings belong in the provider, not this module.

hooks:
  - module: hook-computer-use
    config:
      max_inline_screenshots: 3   # older screenshots collapse to text
      # unattended_writes_ok: true   # explicit, logged opt-out — see Safety
```

**Remote defaults are stricter than local**, because a remote machine is by definition one
you are not looking at:

| Key | Local default | Remote (`ssh://`) default |
|---|---|---|
| `read_only` | `false` | **`true`** |
| `gate_writes` | off | **on**, whenever `read_only` is off |
| `clipboard_read_policy` | `allow` | **`redact`** |

## Requirements

**See [`docs/SETUP.md`](docs/SETUP.md) — this is the part that is more involved than a
typical bundle.** In brief:

- **An orchestrator preserving ToolSpec extras** — the hook checks the known
  loop-streaming builder rather than trusting its version string, and refuses when
  it drops explicit neutral intent. See `docs/SETUP.md` §1.
- **A provider that consumes neutral v1 intent** — native model/endpoint support,
  fallback, wire translation and replay are provider-owned. The hook does not probe
  them. §2 of the setup doc explains the boundary.
- **A target machine** — local or remote over SSH — meeting its platform's **hard**
  prerequisites. These are not optional, and two of the three are not caught at mount:
  - **Windows: WSL2 is required.** There is no native-Windows code path. `windows.py` is a
    *"WSL2 -> Windows desktop backend"*; every action crosses into Win32 via
    `powershell.exe` interop. A plain Windows host — including one reachable over **Windows
    OpenSSH** — is **not supported**; the probe reports `wslpath not on PATH (not running
    under WSL2?)` and nothing mounts. For a remote Windows target, the SSH server must run
    **inside WSL**. Tracked in `BACKLOG.md`.
  - **macOS: two separate TCC grants** — **Screen Recording** (capture) *and*
    **Accessibility** (input). Granting one does not grant the other, and **neither is
    checked at mount** — the tools appear healthy and fail on first use.
  - **Linux: an X11 session** + `python-xlib`. **Wayland is not supported, and not
    detected** — the probe tests only `DISPLAY`/XTEST, which XWayland satisfies, so it can
    report available on a Wayland desktop. Unverified territory; use real X11.
- **For remote targets** — key-based SSH (`BatchMode=yes`; a passphrase-locked key will
  not prompt, it will fail), a trusted host key, and **`uv` on the far end** (mandatory —
  the connection resolves `uv` before anything else and fails if it is absent). The
  target's own per-platform prerequisites above still apply; SSH does not bypass them.
- Pillow (installed with the tool module).

**No agent installed on the target**, and no admin rights, extra service, or new listening
port: our files ship down the SSH pipe and normally remove themselves at session end —
nothing to update or uninstall. A crash can leave an incomplete scratch directory; a later
agent only reclaims a valid v2 lease after a 24-hour minimum retention period and only when
it is unlocked. That protects live long-running agents: age is not treated as idleness.
Legacy, unknown, incomplete, and unsupported-locking cases are retained conservatively.
That is a claim about *our agent*, **not** a claim that the target needs no setup. It still
needs everything listed above.

## Safety

Four distinct mechanisms. Be clear which one you are relying on:

| Control | Kind | Strength |
|---|---|---|
| `read_only: true` | Code | **Enforced.** Every mutating action rejected in `execute()` before anything reaches the desktop. Screenshots still work. |
| Write gate (`gate_writes`) | Code + human | **Enforced.** Per-action `ask_user` approval, default **deny**, across 15 mutating actions. On by default for a remote, non-read-only target. |
| Presence guard / halt | Code | **Enforced and unconditional.** Halts before the next write the moment a human is detected at the machine. No config key can disable the halt once a guard exists; the halt is durable across sessions and cleared only by a human running `scripts/resume_after_halt.py`. |
| Stop Conditions in `agents/computer-operator.md` | Prompt | **Model judgment.** Nothing inspects the screen or blocks an action. |

The agent is instructed to stop and ask when it sees credential prompts, CAPTCHAs,
destructive confirmations, anything that would send or publish on your behalf, or a screen
that twice fails to match expectations. That instruction is followed well in practice —
but it is guidance to a model, not a gate. Treat it as such.

**`unattended_writes_ok` is a deliberate opt-out, not a convenience toggle.** With no TTY,
the write gate denies rather than crashing on an unanswerable prompt. Setting
`unattended_writes_ok: true` allows those writes with no human confirmation — always
logged at WARNING, never a default, never inferred from the environment. If prompts are
merely tedious in an interactive session, you want `read_only: true` instead.

**A locked screen cannot be driven — on any platform.** macOS and Windows both switch to a
secure session that refuses synthetic input by design. The requirement is an **unlocked,
logged-in GUI session**, not merely "the screen is on." The tool now detects this and
fails loud; previously it handed a lock-screen capture to the model as if it were the
desktop. `docs/SETUP.md` §8 has the measurements and the three distinct macOS diagnoses.

**On-screen content can try to manipulate the agent.** Anything the agent can read, it can
be influenced by: a dialog, a web page, or a document saying "click OK to confirm" or
"enter the password" is indistinguishable from a legitimate UI. This is an inherent risk of
computer use, not a defect in this bundle. Do not point it at untrusted screens
unsupervised, and use `read_only: true` when you only need it to look.

**The clipboard goes to your model provider.** `desktop.get_clipboard` returns the target's
clipboard as tool output, which becomes part of the conversation sent to the API and lands
in durable logs. The default is `allow` locally and `redact` (length + digest, never the
text) on a remote target; `clipboard_read_policy: block` refuses outright. If you have just
copied a password or token, clear the clipboard before letting the agent read it.

**Screenshots touch disk briefly.** Captures land in `%TEMP%\amplifier-computer-use\` on a
Windows target (cleaned after 30 minutes) and in a per-session subdirectory of
`~/.amplifier/computer-use/shots/` on the controller (cleaned after 2 hours). Directories
are created `0700` and files `0600`.

## Known issues

- ~~**macOS `type_text` silently no-ops while returning success.**~~ **FIXED** in
  `ccf0913` (2026-08-04) — the cause was a keycode-0 `CGEventKeyboardSetUnicodeString`
  event, which macOS accepts and then discards; it now posts real, non-zero keycodes, the
  same mechanism `key` uses. The "posts to a specific app rather than the system-wide
  event tap" hypothesis this entry used to carry was wrong. Re-verified on real hardware
  2026-09-15 (macOS 26.6.2, remote over SSH): Spotlight received
  `amplifier typing test` verbatim.
- **No whole-session end-to-end run** of the hook, native promotion, screenshot rewriting
  and the write gate all executing together (`BACKLOG.md`).
- **The Windows on-desktop indicator overlay is not built** (Linux and macOS announce are).

## Troubleshooting

Full symptom→cause→fix table in [`docs/SETUP.md`](docs/SETUP.md) §10.

Set a trace path and you get a plain-text record of what the hook actually did:

```bash
AMPLIFIER_COMPUTER_USE_TRACE=/tmp/cu-trace.log amplifier run --bundle computer-use-behavior "..."
```

```
MOUNTED max_inline=3
WRAPPED provider=AnthropicProvider module=amplifier_module_provider_anthropic
complete: markers=1 messages_with_blocks=3
```

No `markers=` line → screenshots are not reaching the model. A mount-time
`ComputerUseNativeToolPassthroughUnsupportedError` means the installed `loop-streaming`
does not yet carry `computer`'s native tool form to the wire on its own — see that
error's message for the orchestrator upgrade. Provider-native refusal/fallback diagnostics
come from the provider; this hook performs no wire capability probes.

## Tests

```bash
python -m pytest tests/test_neutral_computer.py tests/test_native_screenshot_preflight.py tests/test_provider_neutral_composition.py -q
```

Asserts the real bytes: screenshot markers expanded to image blocks that survive
`model_dump()` without an API-rejected `visibility` key, recency window enforced, and
graceful degradation when a screenshot file is gone. Composition tests require the
qualified provider wheels and their real SDKs; HTTP is intercepted and no live requests
are made. They test native subsets and ordinary fallback, not release acceptance.

---

## Credits

Originally created by [@ckrabach617](https://github.com/ckrabach617) as a
Windows-from-WSL2 bundle. That original work is preserved in this repository's
git history and its copyright is retained in `LICENSE`.

This fork extends it to a platform-backend architecture (Windows, macOS, Linux
X11), per-monitor targeting, and remote operation over a private network. See
`BACKLOG.md` for what is known and not yet done, and `docs/` for the
design record.

## Contributing

> [!NOTE]
> This project is not currently accepting external contributions, but we're actively working toward opening this up. We value community input and look forward to collaborating in the future. For now, feel free to fork and experiment!

Most contributions require you to agree to a
Contributor License Agreement (CLA) declaring that you have the right to, and actually do, grant us
the rights to use your contribution. For details, visit [Contributor License Agreements](https://cla.opensource.microsoft.com).

When you submit a pull request, a CLA bot will automatically determine whether you need to provide
a CLA and decorate the PR appropriately (e.g., status check, comment). Simply follow the instructions
provided by the bot. You will only need to do this once across all repos using our CLA.

This project has adopted the [Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
For more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/) or
contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional questions or comments.

## Trademarks

This project may contain trademarks or logos for projects, products, or services. Authorized use of Microsoft
trademarks or logos is subject to and must follow
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must not cause confusion or imply Microsoft sponsorship.
Any use of third-party trademarks or logos are subject to those third-party's policies.
