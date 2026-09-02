<!-- BEGIN CODEX-AUTO-RESUME MANAGED BLOCK -->
## Codex 自动续作预检

在每个新用户 turn、自动恢复 turn 与每次子代理 trigger turn 开始、执行实质工作之前，加载并遵循 `$codex-auto-resume`，且只执行一次预检：

1. 当前消息包含 `AUTO_RESUME=OFF` 或“本任务禁用自动续作”时，以 `--opt-out` 运行预检；可解析的 thread/task 会写入持久忽略记录。
2. 默认从可信 `CODEX_THREAD_ID` 与当前 rollout 解析实际 thread、`task_started.turn_id`、目标和父子谱系；禁止猜测或规范化 thread/task 身份。
3. 工作区按显式路径 → 实际 cwd 的 Git 根 → rollout cwd 的 Git 根 → 实际目录 → rollout 目录 → thread 托管目录解析。普通问答与非 Git 任务同样注册；无 cwd 的子代理继承唯一父工作区，否则使用自己的托管目录。
4. 注册键是 `actual_thread_id + task_id + workspace_root`。同一 turn 幂等复用；同一 thread/workspace 的新 turn 原子 supersede 旧活动 job；每个子代理使用自己的实际 thread/task 注册独立 job，父子可位于不同工作区。
5. 自动恢复创建的 turn 通过 `CODEX_AUTO_RESUME_JOB_ID/TASK_ID` 与固定提示标记归并回原 job。只在显式退出、身份缺失/冲突或运行环境损坏时接受结构化 `SKIPPED`。
6. `REGISTERED` 或 `REUSED` 后按需隐藏启动共享 daemon 与 job watchdog；`--no-start` 同时禁止两者。只有用户明确设置有限循环时才传 `--max-cycles`。

预检结果不改变原任务内容。
<!-- END CODEX-AUTO-RESUME MANAGED BLOCK -->
