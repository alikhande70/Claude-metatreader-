# evidence/

Artifacts that close the gates in `docs/VERIFICATION.md`.

A gate is closed by a file here, not by a report that it worked. Keep the whole output rather
than the part that succeeded — when something fails, that output is what the fix has to be
checked against, and it is usually the more useful of the two.

Naming: `G<n>-<what>.<ext>`, matching the "Saved as" line in the gate.

**Do not commit anything carrying a credential.** `ATLAS_BRIDGE_TOKEN` never needs to appear
in an artifact; account numbers and server names do appear in a `bridge-check` capture and
are fine — they identify a demo account, not a way into it. Read a file before committing it.
