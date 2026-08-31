<!-- BEGIN CODEX-AUTO-RESUME MANAGED BLOCK -->
## Codex 自动续作预检

在每个新用户 turn 与每次子代理 trigger turn 开始、执行实质工作之前，加载并遵循 `$codex-auto-resume`，且只执行一次预检：

1. 当前消息包含 `AUTO_RESUME=OFF` 或“本任务禁用自动续作”时，以 `--opt-out` 运行预检；可解析的 thread/task 会写入持久忽略记录。
2. 预检默认从可信 `CODEX_THREAD_ID` 与当前 rollout 自动解析实际 thread、`task_started.turn_id`、父子谱系、Git 根和目标。显式参数只用于覆盖，且身份必须一致。
3. 注册键是 `actual_thread_id + task_id + git_root`。同一 turn 幂等复用；同一 thread/project 的新 turn 原子 supersede 旧活动 job；子代理使用自己的实际 thread 保持独立 job。
4. 自动恢复创建的 turn 通过 `CODEX_AUTO_RESUME_JOB_ID/TASK_ID` 与固定提示标记归并回原 job，避免递归注册。
5. 任一必要上下文缺失时接受结构化 `SKIPPED` 并继续原任务。只有用户明确设置有限循环时才传 `--max-cycles`。

预检结果 `REGISTERED`、`REUSED` 或 `SKIPPED` 不改变原任务内容。
<!-- END CODEX-AUTO-RESUME MANAGED BLOCK -->
