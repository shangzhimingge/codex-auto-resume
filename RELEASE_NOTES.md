# v1.2.0: cross-platform service and transactional installer

- Added a cross-platform Python installation facade. The no-argument `npx -y github:shangzhimingge/codex-auto-resume` command is now a thin launcher for it.
- Added a persistent Python supervisor daemon that repairs missing watchdogs for active jobs after login or reboot.
- Added native per-user service adapters for Windows Task Scheduler, macOS launchd LaunchAgents, and Linux systemd user units. Windows falls back to an owned per-user Startup launcher when task creation is denied; the Linux path does not enable lingering.
- Added a secure ownership manifest, verified legacy adoption, explicit adoption for locally modified owned files, stable byte-preserving `AGENTS.md` backup, and full installation rollback.
- Added `doctor`, safe default `uninstall`, and explicit `uninstall --purge-data` behavior.
- Added English `README.md`, Simplified Chinese `README.zh-CN.md`, and English Skill interface metadata.
- Preserved the verified recovery core: exact thread UUID, Git snapshot checks, structured checkpoints, process-identity leases, and included-usage-only billing.

Validation includes the complete Windows installation/service path, simulated macOS and Linux installs, Python and Node test suites, package inspection, and a live Codex rate-limit probe.
