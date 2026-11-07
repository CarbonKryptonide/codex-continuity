# 维护 agent 工作规程

仅主 agent 发起维护。维护 agent 和其他子 agent 不自动再派生维护 agent。

## 调用方式

模型必须显式指定 `gpt-6-luna`，推理强度必须显式指定 `medium`。
如果可用，选择自定义角色 `continuity_keeper`；当前工具未刷新角色目录时，用默认角色并显式传入模型、强度及本规程。
使用独立上下文（fork_turns=none）；同一主会话尽量复用已有维护 agent。
输入必须提供：主会话 ID、工作目录、本轮目标/用户修正、产物路径、实测结果、未完成内容。
维护 agent 不修改业务文件、用户全局配置或原有记忆源，不联网发布资料，不读取登录凭据。

## 时机

1. 新的主会话第一次允许写入时：读取 START_HERE 和相关旧任务交接，运行一次 sync 补漏。
2. 每轮回复前：同步当前会话并更新交接；长任务完成重要修改、验证、方案改变或阻塞时补记。
3. 规划模式或只读会话：不写入、不以维护名义申请扩大权限，暂记未维护；下次可写会话补扫。
4. 模型不可用/无可用子 agent 槽位：说明维护未完成，保留源日志；不得静默替换模型或伪报成功。

## 最小操作流程

1. 运行 `continuity.ps1 sync --session <主会话ID> --current-session <主会话ID>`。
2. 读取该会话 meta.json、已有 handoff.json、最近新增原文与主 agent 提供的事实。
3. 起草并保存 `requests/<主会话ID>.json`，字段如下。数组内必须是字符串：

```json
{
  "session_id": "主会话ID",
  "author_model": "gpt-6-luna",
  "author_effort": "medium",
  "goal": "当前目标",
  "constraints": [],
  "decisions": [],
  "completed": [],
  "verified": [],
  "unverified": [],
  "failures": [],
  "next_steps": [],
  "artifacts": [],
  "status": "in_progress"
}
```

状态只能为 `in_progress`、`awaiting_user`、`blocked`、`complete`、`unreviewed`。
只有明确满足目标且验证充分时，才能写 complete。不得把“命令成功”写成“任务验收通过”。
产物和验证写明文件位置、检查内容及限制；缺少证据的内容列在 unverified。

4. 主 agent 复核重要决定与实测结论后，运行
   `continuity.ps1 record --session <ID> --input <输入JSON路径>`，再运行 `check`。
5. 返回：实际更新文件、覆盖截止点、尚未整理部分、失败原因。最终对用户是否提及维护由主 agent 按相关性决定。

索引只由脚本生成；多个会话不直接编辑 INDEX.md，也不覆盖其他会话的交接。
归档会话和归档项目不进入活动索引；不知道归档状态的旧文件不擅自当作活动任务导入。
源记录格式变化或校验失败时留下明确错误，保留原数据，停止声称该记录已完整导出。
