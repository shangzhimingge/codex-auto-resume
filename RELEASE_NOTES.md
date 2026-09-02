# 1.5.0

- Preflights every root and subagent turn, including questions and non-Git tasks.
- Adds `git`, `directory`, and per-thread `managed` workspaces with deterministic resolution and constant-time directory identity snapshots.
- Preserves independent child thread/task jobs across different workspaces; only shared workspaces share recovery leases.
- Migrates schema-v3 jobs to schema v4 in place while preserving legacy Git job IDs and compatibility mirrors.
- Starts the on-demand daemon and watchdog after every successful `REGISTERED` or `REUSED` preflight.

# 1.4.0

- Start the daemon only after a qualified automatic preflight registers or reuses a task.
- Serialize on-demand startup with a dedicated lock and PID/heartbeat handshake.
- Launch detached and hidden on Windows and in a new session on POSIX, with all stdio discarded.
- Remove legacy Windows, macOS, and Linux login-start registrations during install, upgrade, and uninstall.
- Record `backend=on_demand`; `--no-start` now suppresses both watchdog and daemon startup.

# 1.3.0

- Added per-turn registration from trusted rollout metadata, v1/v2-to-v3 state migration, and atomic superseding.
- Added parent/child/grandchild lineage, leaf-first serialized recovery, managed snapshots, and durable one-time handoffs.
- Added opt-out tombstones, self-resume reconciliation markers, bounded session cursors, and discovery diagnostics.
- Scanner registration now waits for observed turn input and records resume launches, preventing the `task_started`/marker write race.
- Resume launches now use reversible provisional claims: unobserved input is never marked seen, ordinary user input releases the claim, and only a marker or exact internal preflight confirms it.
- Unified job-state mutation under canonical locks, made terminal states absorbing, and atomically merged registration, watcher, daemon, checkpoint, and lineage updates.
- Child handoffs are invisible until finalized, carry immutable revisions, and are consumed by exact `(path, revision)` receipt once; child terminal publication completes before its project lease is released.
- Scanner cursors refresh persisted launch claims under cursor-to-launch lock order, including exact confirmed turns whose input remains encrypted when a limit event arrives.
- Preflight and daemon startup now share a per-job check-and-launch lock; descendant registration preempts an already claimed ancestor before spawn, during supervision, or before commit.
- Handoff paths and revisions now use separate resume-prompt lines, preserving a directly readable filesystem path.
- Every newly observed task now attempts exact launch provisioning before input classification; a resume-marker string is internal only when that same turn owns a matching launch claim.
- Jobs persist fork timestamps and association provenance; authoritative fork-time lineage repairs earlier active-parent heuristics.
- Unconsumed handoffs merge late child text, event summaries, and artifacts idempotently before one-time parent consumption.
- Preserved exact UUID verification, included-window-only billing, checkpoint safety, external-change protection, and Windows lock retries.

# v1.2.1: unified runtime and deeper diagnostics

- Added the shared `scripts/auto_resume.py` Python entrypoint for preflight, job execution, daemon control, rate-limit probing, and status while retaining all legacy wrappers.
- Expanded `doctor` with bounded Codex login, read-only app-server, daemon lease/heartbeat, and runtime write checks. Errors and warnings are reported separately; Linux linger is queried only and is never enabled automatically.
- Standardized persistent runtime directories under `jobs`, `checkpoints`, `logs`, and `state`, with compatible migration of v1.2.0 root-level daemon files.
- Created the complete runtime directory layout before service activation and fixed macOS LaunchAgent log paths for first installation.

# v1.2.0: cross-platform service and transactional installer

- Added a cross-platform Python installation facade. The no-argument `npx -y github:shangzhimingge/codex-auto-resume` command is now a thin launcher for it.
- Added a persistent Python supervisor daemon that repairs missing watchdogs for active jobs after login or reboot.
- Added native per-user service adapters for Windows Task Scheduler, macOS launchd LaunchAgents, and Linux systemd user units. Windows falls back to an owned per-user Startup launcher when task creation is denied; the Linux path does not enable lingering.
- Added a secure ownership manifest, verified legacy adoption, explicit adoption for locally modified owned files, stable byte-preserving `AGENTS.md` backup, and full installation rollback.
- Added `doctor`, safe default `uninstall`, and explicit `uninstall --purge-data` behavior.
- Added English `README.md`, Simplified Chinese `README.zh-CN.md`, and English Skill interface metadata.
- Preserved the verified recovery core: exact thread UUID, Git snapshot checks, structured checkpoints, process-identity leases, and included-usage-only billing.

Validation includes the complete Windows installation/service path, simulated macOS and Linux installs, Python and Node test suites, package inspection, and a live Codex rate-limit probe.
