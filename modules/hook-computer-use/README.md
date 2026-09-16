# hook-computer-use

The `provider:request` handler prepares native computer integration only when
the real `computer` tool is mounted. A headless session with only
`computer_use_unavailable`, or a child session without computer access, skips
provider lookup and probing. This prevents an irrelevant native-passthrough
warning during ordinary file/function work.

Tool presence is checked again each turn, not cached: activating a desktop later
in the same session enables the normal probe on the next request. A failed tool
lookup still warns and proceeds with the existing provider compatibility checks;
it is not treated as confirmed absence.

When `computer` is present, the existing native capability probes, compatibility
checks, and unsupported-provider warnings remain unchanged. Skipping a probe
does not assert that a provider supports native computer use.
