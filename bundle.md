---
bundle:
  name: computer-use
  version: 0.1.0
  description: See and control a real desktop - Windows, macOS, or Linux, local or across your network - with the LLM's native computer-use tool

includes:
  - bundle: git+https://github.com/microsoft/amplifier-foundation@main
  - bundle: computer-use:behaviors/computer-use
---

# Computer Use

This session can see a real desktop and control its mouse and keyboard, using the LLM
provider's built-in computer-use tool. The desktop may be Windows, macOS, or Linux, and
it may be this machine or another one reachable over your private network.

Reach for it whenever a task has no API, no CLI, and no extension — a legacy desktop
program, a proprietary UI, a dialog box, an installer, a settings panel. If a human could
do it by looking at the screen and clicking, this can do it.

@computer-use:context/computer-use-awareness.md
