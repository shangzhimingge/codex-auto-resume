# Codex 自动续作

这是一个面向 Windows 的 Codex Skill：当 ChatGPT 的 Codex 用量窗口耗尽时，本地守护进程读取官方 app-server 返回的真实重置时间；窗口恢复后，它会核对 Git 快照，并从保存的精确线程 UUID 与结构化检查点继续原任务。

## 特性

- 只恢复注册时保存的精确线程 UUID，并核对 `thread.started`。
- 使用 `initialize` → `initialized` → `account/rateLimits/read` 协议读取用量。
- 同时判断 primary 和 secondary 窗口，等待所有已耗尽窗口中最晚的重置时间。
- 续作期间持续复查订阅用量；一旦窗口耗尽或服务报告已触达限额，立即终止受管进程树并回到等待状态。
- 默认最多续作 5 次；支持原子状态写入、任务锁和 Git 工作区冲突检测。
- 只使用订阅内用量：忽略 credits，不触发额度重置消费，也不切换到 API 计费。
- 纯 Python 标准库，无第三方依赖。

## 环境要求

- Windows 10/11
- Python 3.9+
- Git
- 已登录的 Codex CLI（已验证命令形态适用于 0.144.6）

## 安装

将 `skill/codex-auto-resume` 目录复制到：

```text
%CODEX_HOME%\skills\codex-auto-resume
```

未设置 `CODEX_HOME` 时，默认位置为：

```text
%USERPROFILE%\.codex\skills\codex-auto-resume
```

重启 Codex 或新建任务后即可发现此 Skill。

## 使用

在 Codex 中明确说“为这个任务开启自动续作”。Skill 会用当前线程 UUID、Git 项目根目录和原始目标注册任务，并启动隐藏的后台守护进程。

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
