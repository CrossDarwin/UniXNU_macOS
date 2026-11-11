See docs/.

## Build and device monitoring

When waiting for a build or device flash to finish, delegate read-only monitoring
to a GPT-6 Luna sub-agent with low reasoning effort (the user's "light" setting).
The monitor reports errors or completion and must not modify files, stop jobs,
restart builds, flash devices, or change device state. The primary agent waits
without further analysis or replies until that report arrives, then handles the
result.
