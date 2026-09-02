---
name: codex-auto-resume
description: Use at the start of every Codex user turn, automatic resume turn, and subagent trigger turn, including questions, non-Git work, and tasks whose working directory changes.
---

# Codex 自动续作

每个 turn 在实质工作前只运行一次确定性预检。始终保持 `billing_policy=included_only`。

## 每 turn 预检

```powershell
$CODEX_ROOT = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
$SKILL_ROOT = Split-Path -Parent (Resolve-Path (Join-Path $CODEX_ROOT "skills/codex-auto-resume/SKILL.md"))
python "$SKILL_ROOT/scripts/preflight.py"
```

消息包含 `AUTO_RESUME=OFF` 或“本任务禁用自动续作”时运行：

```powershell
python "$SKILL_ROOT/scripts/preflight.py" --opt-out
```

默认从可信环境和 rollout 解析实际 thread、`task_started.turn_id`、目标与谱系。工作区解析顺序固定为：显式 `--project`；实际 cwd Git 根；rollout cwd Git 根；实际目录；rollout 目录；`$CODEX_HOME/auto-resume/workspaces/<thread>` 托管目录。目录工作区快照只记录规范根目录与目录 stat 身份，不递归读取内容。Git 工作区继续保存 HEAD、porcelain 与变更文件摘要。

普通根任务缺少可见目标时使用固定 continuation 目标。无 cwd 的子代理继承唯一父工作区；父任务不唯一时使用自己的托管目录。每个子代理仍以自己的实际 thread/task 注册独立 job，父子可关联到不同工作区，只有共享工作区才共享 lease。

注册键为 `actual_thread_id + task_id + workspace_root`。同一 turn 幂等；同一 thread/workspace 的新 task supersede 旧活动 job。自动恢复 turn 通过 `CODEX_AUTO_RESUME_JOB_ID/TASK_ID` 和 `[CODEX_AUTO_RESUME]` 归并回原 job。身份缺失或冲突、显式 opt-out、运行环境损坏才产生 `SKIPPED`。

成功注册或复用后，在注册锁释放后按需启动共享 daemon 与 watchdog。`--no-start` 同时禁止两者。

## 手动注册与检查点

```powershell
$THREAD_ID = "<UUID>"
$PROJECT = (Resolve-Path "<TARGET_WORKSPACE>").Path
$ORIGINAL_GOAL = "<ORIGINAL_GOAL>"
python "$SKILL_ROOT/scripts/register.py" --thread-id "$THREAD_ID" --project "$PROJECT" --goal "$ORIGINAL_GOAL"
python "$SKILL_ROOT/scripts/checkpoint.py" --job-id "$JOB_ID" --set "CURRENT_STATE=<STATE>" --set "NEXT_ACTION=<NEXT>"
```

完成全部目标和验证后，将 `AUTO_RESUME_STATUS` 设置为 `DONE`。线程 UUID 必须保持规范小写原值。

## 恢复与诊断

daemon 对 sessions 做有界增量扫描，容忍半行、截断、轮转和单文件损坏。恢复按叶子优先；同工作区由 lease 串行，不同工作区可保持父子关联。任务状态经原子更新与固定锁序合并，终态不可回退。handoff 按 `(path, revision)` 发布并仅消费一次。

恢复只使用保存的实际 thread UUID并校验首个 `thread.started`。恢复进程持续探测 primary/secondary included window；任一耗尽即终止进程组并回到 `WAITING_RESET`。

```powershell
python "$SKILL_ROOT/scripts/auto_resume.py" status --job "$JOB_ID"
python "$SKILL_ROOT/scripts/auto_resume.py" probe-limits
python "$SKILL_ROOT/scripts/auto_resume.py" daemon status
python "$SKILL_ROOT/scripts/auto_resume.py" daemon scan
```
