# Codex 自动续作

这是一个面向 Windows 的 Codex Skill：它在每个任务开始时执行确定性预检；符合条件的任务会自动注册。当 ChatGPT 的 Codex 用量窗口耗尽时，本地守护进程读取官方 app-server 返回的真实重置时间；窗口恢复后，它会核对 Git 快照，并从保存的精确线程 UUID 与结构化检查点继续原任务。

## 特性

- 只恢复注册时保存的精确线程 UUID，并核对 `thread.started`。
- 使用 `initialize` → `initialized` → `account/rateLimits/read` 协议读取用量。
- 同时判断 primary 和 secondary 窗口，等待所有已耗尽窗口中最晚的重置时间。
- 续作期间持续复查订阅用量；一旦窗口耗尽或服务报告已触达限额，立即终止受管进程树并回到等待状态。
- 支持原子状态写入、任务锁和 Git 工作区冲突检测。
- 只使用订阅内用量：忽略 credits，不触发额度重置消费，也不切换到 API 计费。
- 纯 Python 标准库，无第三方依赖。
- 默认无限续作；可用正整数 `--max-cycles` 显式设置有限循环。
- 按 `THREAD_ID + PROJECT` 幂等去重，并发注册只生成一个任务；复用有效守护进程并重启失效租约。
- 守护进程通过 nonce、心跳和进程创建身份完成启动握手；只有证明锁所有者已失效后才恢复遗留锁。

## 环境要求

- Windows 10/11
- Python 3.9+
- Git
- 已登录的 Codex CLI（已验证命令形态适用于 0.144.6）

## 安装

在仓库根目录运行安装器：

```powershell
.\scripts\install.ps1
```

安装器会把 Skill 复制到 `%CODEX_HOME%\skills\codex-auto-resume`，并只更新全局 `%CODEX_HOME%\AGENTS.md` 中自己的托管块；原有内容及 Sol–Luna 托管块保持不变。首次变更托管块前，会创建字节一致且后续不覆盖的 `AGENTS.md.codex-auto-resume.backup`。未设置 `CODEX_HOME` 时使用 `%USERPROFILE%\.codex`。

安装 Skill 但关闭默认每任务预检：

```powershell
.\scripts\install.ps1 -DisableDefaultActivation
```

重启 Codex 或新建任务后即可发现此 Skill。

## 使用

默认激活后，Codex 会在每个任务开始时预检一次。若当前任务不需要自动续作，在用户消息中加入 `AUTO_RESUME=OFF` 或“本任务禁用自动续作”。缺少精确线程 UUID、Git 根目录或目标时，预检返回 `SKIPPED`，不会猜测或追问。

符合条件时，Skill 会用当前线程 UUID、Git 项目根目录和原始目标注册任务，并启动隐藏的后台守护进程。

也可手动注册：

```powershell
python "$HOME\.codex\skills\codex-auto-resume\scripts\register.py" `
  --thread-id <UUID> `
  --project <PROJECT_ROOT> `
  --goal "<ORIGINAL_GOAL>"
```

每个关键里程碑后更新检查点：

```powershell
python "$HOME\.codex\skills\codex-auto-resume\scripts\checkpoint.py" `
  --job-id <JOB_ID> `
  --set "COMPLETED=<COMPLETED>" `
  --set "CURRENT_STATE=<CURRENT_STATE>" `
  --set "NEXT_ACTION=<NEXT_ACTION>"
```

任务完整完成并通过最终验证后：

```powershell
python "$HOME\.codex\skills\codex-auto-resume\scripts\checkpoint.py" `
  --job-id <JOB_ID> `
  --set "AUTO_RESUME_STATUS=DONE"
```

查看任务状态或实时探测用量：

```powershell
python "$HOME\.codex\skills\codex-auto-resume\scripts\watchdog.py" status --job <JOB_ID>
python "$HOME\.codex\skills\codex-auto-resume\scripts\watchdog.py" probe-limits
```

## 状态文件

运行数据保存在 `%CODEX_HOME%\auto-resume`：

```text
auto-resume/
├── jobs/<JOB_ID>.json
└── checkpoints/<JOB_ID>.md
```

任务状态包括 `REGISTERED`、`RUNNING`、`WAITING_RESET`、`RESUMING`、`DONE`、`NEEDS_USER`、`MAX_CYCLES` 和 `ERROR`。

v1 任务在读取时会原子迁移到 schema v2：旧默认 `max_cycles=5` 转为无限，其他正整数保持有限设置；畸形任务关闭处理。

## 保护机制

- 外部修改导致 Git 快照与检查点不一致时，任务进入 `NEEDS_USER`。
- 额度响应缺失、畸形或已耗尽窗口缺少重置时间时，任务进入 `ERROR`。
- 系统休眠导致重置时间已过时后，守护进程会短暂等待并重新读取官方状态，不会依据旧时间继续执行。
- 恢复出的线程 ID 与保存的 UUID 不一致时立即终止子进程并进入 `NEEDS_USER`。
- 守护进程不自动批准权限、不覆盖意外更改、不强制重置 Git、不强制推送，也不删除意外文件。

## 测试

```powershell
python -m unittest discover -s tests -v
python -m compileall -q skill tests
```

## 许可证

MIT
