# Codex 自动续作

## v1.5 任意工作区预检

v1.5 会预检每个用户与子代理 turn，包括普通问答和非 Git 工作。工作区依次按显式路径、实际 cwd Git 根、rollout cwd Git 根、实际目录、rollout 目录、thread 托管目录解析。注册键为实际线程 UUID、`task_started.turn_id` 与工作区根，子代理始终使用自己的 thread/task 注册独立 job。

恢复按叶子优先，只有共享工作区的 job 才共用 lease 串行。父子 job 可位于不同工作区并保持谱系与 handoff 关联。Git 工作区保留 HEAD、porcelain 与文件摘要校验；目录和托管工作区只记录根目录 stat 身份，不递归扫描内容。

session 扫描器会在分类每个新 turn 前先尝试精确、可撤销的 provisional 启动认领，即使 task 与首条输入在同批扫描中出现；仅有匹配 launch 时，续作标记或精确内部预检才会确认认领。无匹配 launch 的标记字符串仍按普通用户输入注册。provisional turn 不写 seen。

preflight 与 daemon 共用每任务 startup lock，并在锁内重检持久 watchdog lease。同项目的子代理若在祖先认领项目后注册，会给祖先 lease 标记 descendant pending；祖先在启动前、监督周期和提交前检查并退回 `WAITING_RESET`，让叶子任务先运行。handoff 路径与 revision 在提示中分行，路径可直接读取。

[![CI](https://github.com/shangzhimingge/codex-auto-resume/actions/workflows/ci.yml/badge.svg)](https://github.com/shangzhimingge/codex-auto-resume/actions/workflows/ci.yml)

> **在 ChatGPT 用量窗口重置后，安全地继续长时间运行的 Codex 任务。**

[English](./README.md)

![Version](https://img.shields.io/badge/version-v1.5.1-2563eb)
![License](https://img.shields.io/badge/license-MIT-16a34a)
![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-111827)

Codex 自动续作由 Codex Skill 和本地服务组成，适合任何可能跨越 ChatGPT 订阅用量窗口的任务。它保存精确的 Codex thread/task 身份、工作区快照和结构化检查点，读取 Codex app-server 报告的真实重置时间，并在订阅包含用量恢复后继续同一线程。

它不会消耗付费 credits、调用额度重置消费接口、切换到 API 计费、猜测线程、自动批准权限、强制重置 Git，或覆盖仓库中的意外变更。

## 安装

Node.js 只负责启动；事务安装器和全部安装决策均由 Python 实现。

```bash
npx -y github:shangzhimingge/codex-auto-resume
```

无参数命令会安装或升级 Skill、创建全局激活块、写入稳定且字节保真的 `AGENTS.md` 备份、清理旧版登录自启项，并在所有权清单中记录 `on_demand` 后端。

常用命令：

```bash
npx -y github:shangzhimingge/codex-auto-resume doctor
npx -y github:shangzhimingge/codex-auto-resume install --disable-default-activation
npx -y github:shangzhimingge/codex-auto-resume install --adopt-existing
npx -y github:shangzhimingge/codex-auto-resume uninstall
npx -y github:shangzhimingge/codex-auto-resume uninstall --purge-data
```

默认卸载会保留任务和检查点；只有显式使用 `--purge-data` 才会清理运行数据。两种卸载方式都会保留稳定的 `AGENTS.md.codex-auto-resume.backup`。

## 按需隐藏启动 daemon

安装器不再注册登录自启。只有合格的自动预检在完成任务注册并释放全部注册锁后，才启动共享 daemon。`daemon.lock` 仍是运行实例的权威，`daemon.startup.lock` 串行化检查、脱离终端启动及 PID/心跳握手；并发预检最终只产生一个 daemon。

Windows 使用 `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`；macOS 与 Linux 使用新会话。daemon 的标准输入输出均指向空设备，并固定 `shell=False`。跳过或 opt-out 的预检不启动 daemon；`--no-start` 同时禁止 watchdog 与 daemon。自动恢复产生的 turn 会先归并回既有 job，再检查 daemon，因此不会通过 daemon 发现路径递归启动。

安装和升级会幂等清理旧版 Windows 任务计划/“启动”目录、macOS launchd 与 Linux systemd 用户自启项；卸载复用同一清理逻辑。首次合格任务前 daemon 处于未运行状态，`doctor` 将其视为正常。

## 限额探测子进程安全

限额探测在 Windows 使用 `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP`，在 macOS/Linux 使用新会话，且不继承终端句柄。无论探测成功、响应畸形、超时，还是启动或通信失败，都会清理完整 app-server 进程树、关闭管道并等待读取线程结束。陈旧锁仅在 PID 与进程创建身份确认失效且 compare-before-unlink 校验通过后自愈；仍存活的锁所有者与真实权限错误会保持原状。daemon 获取 `daemon.lock` 后会在首次扫描前立即发布已验证的 PID、身份与初始心跳，避免大型运行状态耗尽启动握手窗口。

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
任意用户或子代理 turn 开始
  -> 确定性预检注册精确 thread/task 与工作区
  -> 原子写入检查点和工作区快照
  -> 预检在注册后按需隐藏启动唯一 daemon
  -> watchdog 读取 account/rateLimits/read
  -> 所有已耗尽窗口到达真实重置时间
  -> Git 工作区检查内容，目录工作区检查根身份
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
├── workspaces/<THREAD_ID>/
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
- Git（可选；用于完整仓库快照）
- 已登录的 Codex CLI
- Windows 10/11、macOS 或 Linux

## 验证

```bash
python -m unittest discover -s tests -v
node --test tests/node/launcher.test.mjs
python -m compileall -q installer skill tests
npm pack --dry-run --json
```

## 许可证

MIT © 2026 shangzhimingge
