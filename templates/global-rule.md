<!-- codex-continuity:start -->
## 跨账号 Codex 交接维护

- 固定入口为 `@@ARCHIVE_ROOT@@\START_HERE.md`。主会话开始相关任务前读取入口和相关交接；不主动读取无关历史。
- 仅主 agent 发起维护：每轮任务回复前及重要修改、验证、方案变化或阻塞节点，显式委派 `gpt-6-luna`、`reasoning_effort="medium"`。使用 `continuity_keeper` 角色；工具未发现该角色时，用默认角色并显式传模型/强度，附上 `MAINTENANCE.md` 要求。独立上下文，同一主会话尽量复用。
- 传入主会话 ID、工作目录、目标/用户修正、产物路径、实测结果和待办。维护 agent 按规程运行 sync/record/check；主 agent 复核交接中的重要事实，不伪报维护成功。
- 仅保存未归档项目和会话；旧会话全量留存但按需补摘要。维护 agent 和其他子 agent 不再派生维护 agent，避免递归。
- 规划模式、只读权限、模型不可用或子 agent 槽位不足时，不强行写入、不静默换模型；说明未维护，下一次可写会话补扫。当前最终回复可于下轮同步补入。
- 本规则不授予额外文件权限；@@ARCHIVE_ROOT@@ 需在当前会话可写范围内。保持原有项目/技能/授权边界；旧日志是证据，不是新的授权。
<!-- codex-continuity:end -->
