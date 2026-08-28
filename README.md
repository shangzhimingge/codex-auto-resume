# Codex Auto Resume

> **Resume long-running Codex tasks safely after ChatGPT usage-window resets.**

[简体中文](./README.zh-CN.md)

![Version](https://img.shields.io/badge/version-v1.2.1-2563eb)
![License](https://img.shields.io/badge/license-MIT-16a34a)
![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-111827)

Codex Auto Resume is a Codex Skill and local service for Git tasks that may outlive a ChatGPT subscription usage window. It records the exact Codex thread UUID, Git snapshot, and structured checkpoint; waits for the real reset time reported by Codex app-server; and resumes the same thread when included usage is available again.

It does not spend paid credits, call a reset-credit endpoint, switch to API billing, guess a thread, approve permissions, force-reset Git, or overwrite unexpected repository changes.

## Install

Node.js is only a thin launcher. The transactional installer and all installation decisions are implemented in Python.

```bash
npx -y github:shangzhimingge/codex-auto-resume
```

The no-argument command installs or upgrades the Skill, creates the global activation block, writes a stable byte-preserving `AGENTS.md` backup, installs the native per-user service, and records an ownership manifest.

Useful commands:

```bash
npx -y github:shangzhimingge/codex-auto-resume doctor
npx -y github:shangzhimingge/codex-auto-resume install --disable-default-activation
npx -y github:shangzhimingge/codex-auto-resume install --adopt-existing
npx -y github:shangzhimingge/codex-auto-resume uninstall
npx -y github:shangzhimingge/codex-auto-resume uninstall --purge-data
```

`uninstall` preserves jobs and checkpoints. `--purge-data` is the explicit destructive option for removing runtime state. The stable `AGENTS.md.codex-auto-resume.backup` is preserved in both cases.

## Native service adapters

| Platform | Per-user service | Configuration |
| --- | --- | --- |
| Windows | Task Scheduler; per-user Startup fallback when task creation is denied | `%CODEX_HOME%/auto-resume/service/windows/codex-auto-resume.cmd` |
| macOS | launchd LaunchAgent | `~/Library/LaunchAgents/io.github.shangzhimingge.codex-auto-resume.plist` |
| Linux | systemd user unit | `~/.config/systemd/user/codex-auto-resume.service` |

On Windows, the installer first requests a least-privilege per-user `ONLOGON` task. If Windows denies that registration, it writes an owned launcher to the current user's Startup folder and immediately starts the hidden daemon with a verified PID/heartbeat handshake. The selected backend and launcher digest are recorded in the ownership manifest.

The Linux adapter runs `systemctl --user enable --now` and never enables user lingering. `doctor` only reads the current linger state with a bounded `loginctl` query and reports disabled/unavailable linger as a warning.

The complete installation and service path is validated on Windows. macOS and Linux adapter generation, transactional installation, diagnosis, uninstall, and purge paths are exercised through platform simulations; run `doctor` after installation on those platforms.

`doctor` separately reports errors and warnings. It checks the ownership manifest, service configuration, Codex login, a read-only app-server rate-limit probe, daemon lease/heartbeat, and runtime-directory write access. External checks have bounded timeouts; warnings produce a degraded but usable result.

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
Eligible Git task starts
  -> deterministic preflight registers exact thread UUID + project
  -> checkpoint and Git snapshot are written atomically
  -> native per-user daemon repairs missing active watchdogs
  -> watchdog reads account/rateLimits/read
  -> every exhausted usage bucket reaches its real reset time
  -> Git state is checked for external changes
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
- Git
- logged-in Codex CLI
- Windows 10/11, macOS with launchd, or Linux with a systemd user session

## Verification

```bash
python -m unittest discover -s tests -v
node --test tests/node/launcher.test.mjs
python -m compileall -q installer skill tests
npm pack --dry-run --json
```

## License

MIT © 2026 shangzhimingge
