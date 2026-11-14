See docs/.

## Push destination

When the user requests a push, default to the corresponding CrossDarwin
repository and verify the remote URL first. An explicit destination overrides
this default. Upstream pull requests use a source branch in the CrossDarwin fork.

## Build and installation ownership

The user runs production builds and all physical-device installation commands.
Provide the exact commands after validation. Do not flash, erase, repartition,
temporarily boot an image, or reboot the physical device unless the user
explicitly overrides this rule for that operation. Read-only device queries,
QEMU validation, fixes, artifact checks and handoff remain the agent's work.

## Build and device monitoring

When waiting for a build or device flash to finish, delegate read-only monitoring
to a GPT-6 Luna sub-agent with low reasoning effort (the user's "light" setting).
The monitor reports errors or completion and must not modify files, stop jobs,
restart builds, flash devices, or change device state. The primary agent waits
without further analysis or replies until that report arrives, then handles the
result.
