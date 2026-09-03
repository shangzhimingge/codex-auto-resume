# Codex Auto Resume

## v1.5 any-workspace preflight

Version 1.5 preflights every user and subagent turn, including questions and non-Git work. A workspace resolves in this order: explicit path, actual-cwd Git root, rollout-cwd Git root, actual directory, rollout directory, then a managed per-thread directory. Registration uses `(actual thread UUID, task_started.turn_id, workspace root)`, and child-agent threads always remain independent jobs.

Recovery is leaf-first and serialized only when jobs share a workspace. Parent and child jobs may use different workspaces while retaining lineage and handoff links. Git workspaces preserve full HEAD/porcelain/content snapshot checks; directory and managed workspaces use a constant-time root identity snapshot with no recursive scan. Self-created resume turns reconcile into the original job instead of recursively creating jobs.

The session scanner attempts an exact reversible provisional launch claim before classifying every new turn, even when its first input arrives in the same scan. It confirms only a matching claim after the resume marker or exact internal preflight is visible, and releases it when ordinary user input wins the race. A marker string without a matching launch remains ordinary user input. Provisional turns are never marked seen.

Job documents have one atomic update path and canonical lock order. Terminal states are absorbing. A child finalizes its handoff and lineage while its project lease is still held, then publishes terminal state and releases the lease, so a parent cannot run ahead of an incomplete result.

Preflight and daemon recovery share a per-job startup lock and recheck the durable watchdog lease inside it. If a descendant registers after an ancestor has claimed the project, registration marks that lease pending; the ancestor checks before spawn, during supervision, and before commit, yields to `WAITING_RESET`, and releases the project for leaf-first recovery. Handoff paths and revisions occupy separate prompt lines, so every path remains directly readable.

[![CI](https://github.com/shangzhimingge/codex-auto-resume/actions/workflows/ci.yml/badge.svg)](https://github.com/shangzhimingge/codex-auto-resume/actions/workflows/ci.yml)

> **Resume long-running Codex tasks safely after ChatGPT usage-window resets.**

[简体中文](./README.zh-CN.md)

![Version](https://img.shields.io/badge/version-v1.5.4-2563eb)
![License](https://img.shields.io/badge/license-MIT-16a34a)
![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-111827)

Codex Auto Resume is a Codex Skill and local service for any task that may outlive a ChatGPT subscription usage window. It records the exact Codex thread/task identity, workspace snapshot, and structured checkpoint; waits for the real reset time reported by Codex app-server; and resumes the same thread when included usage is available again.

It does not spend paid credits, call a reset-credit endpoint, switch to API billing, guess a thread, approve permissions, force-reset Git, or overwrite unexpected repository changes.

## Install

Node.js is only a thin launcher. The transactional installer and all installation decisions are implemented in Python.

```bash
npx -y github:shangzhimingge/codex-auto-resume
```

The no-argument command installs or upgrades the Skill, creates the global activation block, writes a stable byte-preserving `AGENTS.md` backup, removes legacy login-start registrations, and records an ownership manifest with the `on_demand` backend.

Useful commands:

```bash
npx -y github:shangzhimingge/codex-auto-resume doctor
npx -y github:shangzhimingge/codex-auto-resume install --disable-default-activation
npx -y github:shangzhimingge/codex-auto-resume install --adopt-existing
npx -y github:shangzhimingge/codex-auto-resume uninstall
npx -y github:shangzhimingge/codex-auto-resume uninstall --purge-data
```

`uninstall` preserves jobs and checkpoints. `--purge-data` is the explicit destructive option for removing runtime state. The stable `AGENTS.md.codex-auto-resume.backup` is preserved in both cases.

## On-demand hidden daemon

No daemon is registered at login. A qualified automatic preflight starts the shared supervisor only after task registration has completed and all registration locks are released. `daemon.lock` remains the running-instance authority, while `daemon.startup.lock` serializes the check, detached launch, and PID/heartbeat handshake. Concurrent preflights therefore converge on one daemon.

Windows uses `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`; macOS and Linux use a new session. All daemon stdio is redirected to the null device and `shell=False` is enforced. Skipped or opted-out preflights do not launch it, and `--no-start` disables both watchdog and daemon startup. Resume-created turns merge into their existing job before checking the daemon, so startup does not recurse through daemon discovery.

Rate-limit probes also use a hidden Windows process group or a new POSIX session and never inherit terminal handles. On Windows, every probe is attached to a kill-on-close Job Object; a PID plus creation-identity descendant snapshot provides the fallback for children created before attachment or when assignment is rejected. Cleanup terminates the Job first, applies hidden tree and identity-safe process termination, waits for the root and every captured descendant to disappear, and closes the Job handle last. Success, malformed responses, timeouts, and startup or communication failures all close pipes and join reader threads, while a cleanup failure never replaces the original RPC failure. Every lock-file participant serializes create, stale-owner classification, removal, and immediate rebuild through a canonical path-derived OS acquisition gate: an automatically released named Mutex on Windows or an advisory lock on POSIX. Persisted owners are classified as absent, live with matching identity, or unknown/identity-mismatched; permission and identity uncertainty fail closed. Recovery snapshots both content and stable file identity, removes only a confirmed-absent unchanged owner, retries every disappearance immediately even with zero timeout, and preserves replacements and new-owner nonces. After taking `daemon.lock`, the daemon publishes its verified PID, identity, and initial heartbeat before the first scan, so a large runtime state cannot exhaust the startup-handshake window.

Install and upgrade idempotently remove legacy Windows Task Scheduler/Startup, macOS launchd, and Linux systemd user registrations. Uninstall reuses the same cleanup. `doctor` treats an inactive daemon as healthy before the first qualified task.

## Transaction and ownership safety

The Python installer:

- stages the new Skill before replacing the installed copy;
- accepts automatic legacy adoption only when the existing directory has the verified Codex Auto Resume signature;
- records the installed tree digest, paths, activation state, backup digest, and service identity in `%CODEX_HOME%/auto-resume/install-manifest.json`;
- detects edits to owned files and requires explicit `--adopt-existing` before replacing them;
- rejects an unrelated directory at the managed Skill path;
- restores the prior Skill, `AGENTS.md`, backup, service configuration, and manifest if an installation step fails;
- removes only manifest-owned artifacts during uninstall.

## How runtime recovery works

```text
Any user or subagent turn starts
  -> deterministic preflight registers exact thread/task + workspace
  -> checkpoint and workspace snapshot are written atomically
  -> preflight starts one hidden on-demand daemon after registration
  -> watchdog reads account/rateLimits/read
  -> every exhausted usage bucket reaches its real reset time
  -> Git workspaces check content; directory workspaces check root identity
  -> codex exec resume uses the saved UUID
  -> first thread.started UUID is verified
  -> work continues from NEXT_ACTION
```

The daemon is a lightweight supervisor. Existing per-job watchdog ownership remains protected by a lock, random nonce, heartbeat, PID, and process-creation identity. This prevents duplicate takeover and PID-reuse mistakes after a reboot or stale process record.

## Runtime state

```text
%CODEX_HOME%/auto-resume/
├── install-manifest.json
├── jobs/<JOB_ID>.json
├── checkpoints/<JOB_ID>.md
├── workspaces/<THREAD_ID>/
├── logs/{daemon.stdout.log,daemon.stderr.log}
└── state/{daemon-state.json,daemon.lock}
```

Upgrades migrate the v1.2.0 root-level daemon state, lock, and log files when present. Jobs and checkpoints keep their existing paths.

Job states are `REGISTERED`, `RUNNING`, `WAITING_RESET`, `RESUMING`, `DONE`, `NEEDS_USER`, `MAX_CYCLES`, and `ERROR`. Resume cycles are unlimited by default; a positive `--max-cycles` may be set explicitly.

## Manual facade and runtime commands

```bash
python installer.py install
python installer.py doctor
python installer.py uninstall
```

```bash
python ~/.codex/skills/codex-auto-resume/scripts/auto_resume.py preflight --opt-out
python ~/.codex/skills/codex-auto-resume/scripts/auto_resume.py daemon status
python ~/.codex/skills/codex-auto-resume/scripts/auto_resume.py probe-limits
python ~/.codex/skills/codex-auto-resume/scripts/auto_resume.py status --job JOB_ID
```

The earlier `preflight.py`, `daemon.py`, `watchdog.py`, `register.py`, and `checkpoint.py` entrypoints remain compatible.

The PowerShell wrapper remains available:

```powershell
.\scripts\install.ps1
```

## Requirements

- Node.js 18+ for the `npx` launcher
- Python 3.9+
- Git (optional; enables full repository snapshots)
- logged-in Codex CLI
- Windows 10/11, macOS, or Linux

## Verification

```bash
python -m unittest discover -s tests -v
node --test tests/node/launcher.test.mjs
python -m compileall -q installer skill tests
npm pack --dry-run --json
```

## License

MIT © 2026 shangzhimingge
