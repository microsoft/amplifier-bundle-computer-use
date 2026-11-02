# Computer Use (real desktop control)

`computer` sees and controls a real Windows, macOS, or Linux desktop.

**Routing gate.** Delegate to `computer-use:computer-operator` only when the user
explicitly requests actual rendered desktop GUI or screen interaction, or a required state
or action is GUI-only and no suitable structured path is available. Prefer a browser,
mobile, CLI, API, or code/file workflow when it can complete the task. Do not launch or
explore desktop applications speculatively, or route merely because a request says “open,”
“check,” or “navigate.”

**When UI-use is available.** For an authorized desktop task, first use `ui_use`
for a bounded, observable subgoal, omitting `mode` to honor the configured action
selector. Initial screenshots and inspection may use `computer` directly. The
presence of `computer-use:computer-operator` is not a reason to skip configured
UI-use selection. Delegate to that operator or use individual computer actions
when UI-use reports unavailable/unsupported controls, unstable observations, or
needs recovery. If `ui_use` is absent, the ordinary computer operator remains the
first-class path. Verify current state before recovery; never replay unknown input.

**Targeting.** `config.target` selects the mount target (unset is this machine;
`ssh://user@host` is another). `desktop(action="retarget")` re-points an already-mounted
session; `computer_use_unavailable(action="activate")` mounts tools when none are mounted.
Ordinary actions have no host parameter; use these helpers rather than concluding
the capability is local-only.

**If unavailable.** If `computer_use_unavailable` appears instead of `computer`/`desktop`,
relay its explanation and stop; do not improvise a workaround such as remote shell control.

Coordinates from a screenshot are scaled back to physical pixels automatically. Never
guess coordinates — take a screenshot first.

**A human may share this keyboard, and keystrokes can interleave.** Verify what actually
landed before continuing.
