# Codex 自动续作

> **在 ChatGPT 用量窗口重置后，安全地继续长时间运行的 Codex 任务。**

[English](./README.md)

![Version](https://img.shields.io/badge/version-v1.2.1-2563eb)
![License](https://img.shields.io/badge/license-MIT-16a34a)
![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-111827)

Codex 自动续作由 Codex Skill 和本地服务组成，适合可能跨越 ChatGPT 订阅用量窗口的 Git 任务。它保存精确的 Codex 线程 UUID、Git 快照和结构化检查点，读取 Codex app-server 报告的真实重置时间，并在订阅包含用量恢复后继续同一线程。

它不会消耗付费 credits、调用额度重置消费接口、切换到 API 计费、猜测线程、自动批准权限、强制重置 Git，或覆盖仓库中的意外变更。

## 安装

Node.js 只负责启动；事务安装器和全部安装决策均由 Python 实现。

```bash
npx -y github:shangzhimingge/codex-auto-resume
```

无参数命令会安装或升级 Skill、创建全局激活块、写入稳定且字节保真的 `AGENTS.md` 备份、安装当前平台的用户级服务，并记录所有权清单。

常用命令：

```bash
npx -y github:shangzhimingge/codex-auto-resume doctor
npx -y github:shangzhimingge/codex-auto-resume install --disable-default-activation
npx -y github:shangzhimingge/codex-auto-resume install --adopt-existing
npx -y github:shangzhimingge/codex-auto-resume uninstall
npx -y github:shangzhimingge/codex-auto-resume uninstall --purge-data
```

默认卸载会保留任务和检查点；只有显式使用 `--purge-data` 才会清理运行数据。两种卸载方式都会保留稳定的 `AGENTS.md.codex-auto-resume.backup`。

## 原生服务适配器

| 平台 | 用户级服务 | 配置位置 |
| --- | --- | --- |
| Windows | 任务计划程序；创建被拒绝时回退到当前用户“启动”目录 | `%CODEX_HOME%/auto-resume/service/windows/codex-auto-resume.cmd` |
| macOS | launchd LaunchAgent | `~/Library/LaunchAgents/io.github.shangzhimingge.codex-auto-resume.plist` |
| Linux | systemd 用户单元 | `~/.config/systemd/user/codex-auto-resume.service` |

Windows 安装器会先申请最低权限的当前用户 `ONLOGON` 任务；若系统拒绝注册，则写入由清单托管的当前用户“启动”目录启动器，并通过 PID 与心跳握手确认隐藏守护进程已经启动。最终选用的后端及启动器摘要会写入所有权清单。

Linux 适配器执行 `systemctl --user enable --now`，不会开启用户 lingering。`doctor` 只通过带超时的 `loginctl` 查询读取当前 linger 状态；未开启或查询不可用仅作为警告报告。

完整安装与服务链路已在 Windows 上实际验证。macOS 和 Linux 的服务生成、事务安装、诊断、卸载与清理路径通过平台模拟验证；在这些平台安装后请执行 `doctor`。

`doctor` 会分别报告错误与警告，并检查所有权清单、服务配置、Codex 登录状态、只读 app-server 限额探针、守护进程 lease/心跳，以及运行目录写权限。所有外部检查均有超时；仅有警告时状态为可用但退化。

## 事务与所有权安全

Python 安装器会：

- 先暂存新 Skill，再替换已安装副本；
- 仅在现有目录满足 Codex 自动续作的严格签名时自动接管旧版安装；
- 在 `%CODEX_HOME%/auto-resume/install-manifest.json` 中记录文件树摘要、路径、激活状态、备份摘要和服务身份；
- 检测托管文件的本地修改，并要求显式使用 `--adopt-existing` 后才替换；
- 拒绝覆盖 Skill 目标路径中的无关目录；
- 任一步骤失败时恢复先前的 Skill、`AGENTS.md`、备份、服务配置和清单；
- 卸载时只移除清单明确拥有的内容。

## 运行流程

```text
符合条件的 Git 任务开始
  -> 确定性预检注册精确线程 UUID 与项目
  -> 原子写入检查点和 Git 快照
  -> 原生用户级守护服务修复缺失的活动 watchdog
  -> watchdog 读取 account/rateLimits/read
  -> 所有已耗尽窗口到达真实重置时间
  -> 检查 Git 是否有外部变更
  -> codex exec resume 使用保存的 UUID
  -> 核对首个 thread.started UUID
  -> 从 NEXT_ACTION 继续
```

守护进程只是轻量监督器。每个任务原有的锁、随机 nonce、心跳、PID 和进程创建身份仍共同保护 watchdog 所有权，避免重启后的重复接管和 PID 复用错误。

## 运行数据

```text
%CODEX_HOME%/auto-resume/
├── install-manifest.json
├── jobs/<JOB_ID>.json
├── checkpoints/<JOB_ID>.md
├── logs/{daemon.stdout.log,daemon.stderr.log}
└── state/{daemon-state.json,daemon.lock}
```

升级时会兼容迁移 v1.2.0 位于根目录的 daemon 状态、锁和日志文件；任务与检查点路径保持不变。

任务状态包括 `REGISTERED`、`RUNNING`、`WAITING_RESET`、`RESUMING`、`DONE`、`NEEDS_USER`、`MAX_CYCLES` 和 `ERROR`。默认续作循环次数无限；可显式设置正整数 `--max-cycles`。

## 手动命令

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

原有的 `preflight.py`、`daemon.py`、`watchdog.py`、`register.py` 和 `checkpoint.py` 入口继续兼容。

PowerShell 包装器继续保留：

```powershell
.\scripts\install.ps1
```

## 环境要求

- Node.js 18+，用于 `npx` 启动器
- Python 3.9+
- Git
- 已登录的 Codex CLI
- Windows 10/11、带 launchd 的 macOS，或带 systemd 用户会话的 Linux

## 验证

```bash
python -m unittest discover -s tests -v
node --test tests/node/launcher.test.mjs
python -m compileall -q installer skill tests
npm pack --dry-run --json
```

## 许可证

MIT © 2026 shangzhimingge
