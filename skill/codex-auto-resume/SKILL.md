---
name: codex-auto-resume
description: Preflight every eligible Codex user or subagent turn and resume its exact thread after included usage-window interruptions.
---

# Codex 自动续作

每个用户 turn 和子代理 trigger turn 只执行一次确定性预检。运行时仅使用 Python 标准库，并始终保持 `billing_policy=included_only`。

## 路径初始化

```powershell
$CODEX_ROOT = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
$SKILL_ROOT = Split-Path -Parent (Resolve-Path (Join-Path $CODEX_ROOT "skills/codex-auto-resume/SKILL.md"))
$THREAD_ID = "<UUID>"
$PROJECT = (Resolve-Path "<TARGET_GIT_PROJECT>").Path
$ORIGINAL_GOAL = "<ORIGINAL_GOAL>"
```

## 每 turn 预检

默认从 `CODEX_THREAD_ID` 与 `$CODEX_HOME/sessions/**/rollout-*.jsonl` 解析实际 thread、`task_started.turn_id`、cwd、目标与父子谱系：

```powershell
python "$SKILL_ROOT/scripts/preflight.py"
```

显式上下文仍受支持：

```powershell
python "$SKILL_ROOT/scripts/preflight.py" --thread-id "$THREAD_ID" --project "$PROJECT" --goal "$ORIGINAL_GOAL"
```

消息包含 `AUTO_RESUME=OFF` 或“本任务禁用自动续作”时执行：

```powershell
python "$SKILL_ROOT/scripts/preflight.py" --opt-out
```

合格预检在注册锁释放后按需启动共享 daemon；Windows 使用无窗口、脱离进程组，macOS/Linux 使用新会话。跳过、opt-out 和 `--no-start` 均不启动 daemon，其中 `--no-start` 也不启动 watchdog。daemon 内部发现直接调用注册层，不会递归触发 daemon 启动。

可解析的 opt-out 写入 `(thread, task)` tombstone。注册键为 `actual_thread_id + task_id + git_root`：同一 turn 幂等；同一 thread/project 的新 task supersede 旧活动 job；父子代理拥有独立 job。自动恢复 turn 通过 `CODEX_AUTO_RESUME_JOB_ID/TASK_ID` 和 `[CODEX_AUTO_RESUME]` 标记归并回原 job。

## 手动注册

```powershell
python "$SKILL_ROOT/scripts/register.py" --thread-id "$THREAD_ID" --project "$PROJECT" --goal "$ORIGINAL_GOAL"
```

子代理可额外传 `--task-id`、`--parent-thread-id`、`--parent-task-id`、`--root-thread-id`、`--agent-path` 与 `--rollout-path`。线程 UUID 必须是原样规范小写 UUID。

## 检查点

```powershell
python "$SKILL_ROOT/scripts/checkpoint.py" --job-id "$JOB_ID" --set "CURRENT_STATE=<STATE>" --set "NEXT_ACTION=<NEXT>"
```

完成全部目标和验证后：

```powershell
python "$SKILL_ROOT/scripts/checkpoint.py" --job-id "$JOB_ID" --set "AUTO_RESUME_STATUS=DONE"
```

## 发现、协调与恢复

daemon 对 sessions 做有界增量扫描，容忍半行、截断、轮转和单文件损坏。每个新 task 均先尝试精确 provisional 认领，再分类输入；只有存在该 turn 的匹配 launch 时，续作标记或精确内部预检才确认。无匹配 launch 的标记文本按普通用户 turn 注册，provisional 不写 seen。正常完成的历史 turn 只推进游标。usage-limit 中断的 user/child turn 进入 `WAITING_RESET`。

同项目恢复由项目锁串行，并按叶子优先：grandchild → child → parent。任务状态通过统一原子更新路径与固定锁序合并，终态不可回退。同谱系受管变更推进 lineage snapshot；等待期间的外部或不同谱系变更进入 `NEEDS_USER`。子代理持有项目 lease 时先 finalized 带 revision 的 handoff，再发布终态与 lineage，最后释放 lease；父任务按 `(path, revision)` 精确读取并仅消费一次。

preflight 与 daemon 通过同一 per-job startup lock 在锁内重检 watchdog lease。若子代理在祖先已经 claim 项目后注册，注册层会写入 descendant-pending；祖先在 spawn 前、监督周期和提交前检查该标记，退回 `WAITING_RESET` 并释放 lease。父任务提示中的 handoff path 与 revision 分行，path 可直接读取。

daemon 以 `daemon.lock` 作为运行实例权威，并用 `daemon.startup.lock` 串行化检查、启动和 PID/心跳握手。

恢复始终使用保存的实际 thread UUID，并校验首个 `thread.started`。恢复子进程持续探测 primary/secondary included window；任一耗尽即终止整个进程组并回到 `WAITING_RESET`。

## 诊断

```powershell
python "$SKILL_ROOT/scripts/auto_resume.py" status --job "$JOB_ID"
python "$SKILL_ROOT/scripts/auto_resume.py" probe-limits
python "$SKILL_ROOT/scripts/auto_resume.py" daemon status
python "$SKILL_ROOT/scripts/auto_resume.py" daemon scan
```

`daemon scan` 输出 `discovered/registered/reconciled/ignored/deferred` 与逐文件错误。终态包括 `DONE`、`SUPERSEDED`、`NEEDS_USER`、`MAX_CYCLES` 与 `ERROR`。
