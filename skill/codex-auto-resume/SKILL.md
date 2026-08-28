---
name: codex-auto-resume
description: Preserve and automatically resume long-running Codex coding tasks across ChatGPT usage-window interruptions. Use when a user explicitly asks to enable automatic continuation, auto-resume, 自动续作, 自动续跑, or when a named long task must continue after the Codex quota resets.
---

# Codex 自动续作

仅在用户明确启用自动续作时注册任务。运行环境只使用 Python 标准库。

## 初始化路径

区分两个目录：`PROJECT` 是需要继续工作的目标 Git 仓库；`SKILL_ROOT` 是已安装 Skill 中 `SKILL.md` 所在目录。允许从任意当前工作目录执行命令。先在 PowerShell 中设置：

```powershell
$CODEX_ROOT = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
$SKILL_ROOT = Split-Path -Parent (Resolve-Path (Join-Path $CODEX_ROOT "skills\codex-auto-resume\SKILL.md"))
$THREAD_ID = "<UUID>"
$PROJECT = (Resolve-Path "<TARGET_GIT_PROJECT>").Path
$ORIGINAL_GOAL = "<ORIGINAL_GOAL>"
```

始终引用 `$SKILL_ROOT` 下的脚本，并用双引号包围脚本路径、目标路径、目标文本和任务 ID。

## 注册

1. 获取当前线程的精确 UUID、项目 Git 根目录和原始目标。
2. 在任意目录执行：

```powershell
python "$SKILL_ROOT\scripts\register.py" --thread-id "$THREAD_ID" --project "$PROJECT" --goal "$ORIGINAL_GOAL"
```

3. 保存命令返回的 `job_id`。让本地 Windows 守护进程在后台等待真实用量窗口重置。

始终使用 `billing_policy=included_only`。忽略付费 credits 和 earned reset credits；不调用任何额度重置消费接口，不使用 API key 计费回退。

## 维护检查点

在每个关键里程碑后，以及长构建、大型测试、迁移和批量编辑前，执行：

```powershell
python "$SKILL_ROOT\scripts\checkpoint.py" --job-id "$JOB_ID" `
  --set "COMPLETED=<COMPLETED>" `
  --set "CURRENT_STATE=<CURRENT_STATE>" `
  --set "FILES_CHANGED=<FILES_CHANGED>" `
  --set "TEST_RESULTS=<TEST_RESULTS>" `
  --set "NEXT_ACTION=<NEXT_ACTION>" `
  --set "DO_NOT_REPEAT=<DO_NOT_REPEAT>"
```

记录 `FAILED_ATTEMPTS`、`LAST_COMMAND`、`LAST_RESULT` 和 `FAILURE_REASON`，避免恢复后重复扫描仓库、重新规划或重跑已确认阶段。检查点更新会同时保存 Git HEAD、工作区状态及可见变更文件的内容哈希。

## 完成

完整目标满足且最终验证通过后，执行：

```powershell
python "$SKILL_ROOT\scripts\checkpoint.py" --job-id "$JOB_ID" --set "AUTO_RESUME_STATUS=DONE"
```

## 查看状态与诊断

```powershell
python "$SKILL_ROOT\scripts\watchdog.py" status --job "$JOB_ID"
python "$SKILL_ROOT\scripts\watchdog.py" probe-limits
```

守护进程严格执行 app-server 的 `initialize` → `initialized` → `account/rateLimits/read` 握手，优先读取 `rateLimitsByLimitId.codex`，并同时判断 primary 与 secondary 窗口。额度数据缺失或畸形时按关闭状态处理。

续作子进程运行期间持续重新读取用量。任一窗口达到 100%，或 `rateLimitReachedType` 为非空值时，终止整个受管进程组并返回 `WAITING_RESET`；不得让续作转入 credits 计费。每次等待不超过配置的轮询间隔，系统休眠或旧重置时间只触发重新探测。

恢复时只使用保存的线程 UUID。先确认 Git 快照无冲突，再在原项目目录启动恢复命令；读取首个 `thread.started` 并核对 UUID。若仓库被外部修改、线程身份不匹配、达到最大循环次数或状态异常，将任务标记为 `NEEDS_USER`、`MAX_CYCLES` 或 `ERROR`。
