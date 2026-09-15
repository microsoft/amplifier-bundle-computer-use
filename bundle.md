---
bundle:
  name: computer-use
  version: 0.1.0
  description: See and control a real desktop - Windows, macOS, or Linux, local or across your network - with the LLM's native computer-use tool

includes:
  - bundle: computer-use:behaviors/computer-use
---

# Computer Use

This session can see a real desktop and control its mouse and keyboard, using the LLM
provider's built-in computer-use tool. The desktop may be Windows, macOS, or Linux, and
it may be this machine or another one reachable over your private network.

Use desktop control only when the user explicitly requests actual rendered desktop GUI or
screen interaction, or a required state or action is GUI-only and no suitable structured
path exists. Prefer a browser, mobile, CLI, API, or code/file workflow when it can complete
the task. Do not launch or explore desktop applications speculatively, or route merely
because a request says “open,” “check,” or “navigate.” This includes legacy or proprietary
desktop UI when no structured path is available.

@computer-use:context/computer-use-awareness.md
