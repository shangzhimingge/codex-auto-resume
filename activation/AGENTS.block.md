<!-- BEGIN CODEX-AUTO-RESUME MANAGED BLOCK -->
## Codex 自动续作预检

在每个任务开始、执行实质工作之前，加载并遵循 `$codex-auto-resume`，且只执行一次预检：

1. 若当前用户消息明确包含 `AUTO_RESUME=OFF` 或“本任务禁用自动续作”，以 `--opt-out` 运行预检并接受 `SKIPPED`，本任务不注册。
2. 仅从可信的当前会话元数据取得原样、规范小写的精确 Codex 线程 UUID；不猜测、不转换，也不使用最近线程。
3. 将当前 Git 仓库根目录作为 `PROJECT`，将用户的完整任务要求作为 `ORIGINAL_GOAL`。
4. 当 UUID、Git 根目录和目标均存在时，运行已安装 Skill 的 `scripts/preflight.py`。它会按 `THREAD_ID + PROJECT` 幂等注册一次，并复用现有任务或有效守护进程。
5. 任一条件缺失、UUID 不规范或目录不是 Git 仓库时，不询问补充信息；运行缺少对应参数的预检并接受 `SKIPPED`，继续原任务。

预检结果 `REGISTERED`、`REUSED` 或 `SKIPPED` 不改变原任务内容。只有用户明确设置有限循环时才传 `--max-cycles`；默认无限续作。
<!-- END CODEX-AUTO-RESUME MANAGED BLOCK -->
