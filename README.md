# Codex Continuity

为本机 Codex 保存可跨账号读取的会话原文、交接摘要与环境快照。

**自动化由全局 `AGENTS.md` 约定触发：主 agent 在每轮任务回复前和关键节点调用
`gpt-6-luna / medium` 维护 agent。** 新对话需要加载规则、能写入备份目录，并能使用指定模型。
强制退出、只读模式、规划模式或模型不可用时，交接可能延后；下次允许写入时补扫。

## 功能

- 保存未归档的主会话及关联子 agent/辅助记录，排除已归档项目、会话及其后代。
- 同时保存本机 JSONL、压缩日志和分页历史；源文件只读，使用可校验的私有数据库/WAL 副本。
- 生成项目/会话索引、原文摘录、按需交接摘要，保留覆盖截止点和摘要修订。
- 支持增量同步、并发写入、原始记录哈希检查、压缩日志解压校验。
- 快照包含规则、脱敏配置、用户技能、两套记忆和插件本地代码；运行环境和项目源文件保留原位。
- 新账号从 `START_HERE.md` 定位任务，无需恢复旧聊天界面或复制旧登录凭据。

不安装后台守护服务、hooks 或系统计划任务。模型维护会使用当前账号的模型额度。

## 环境与兼容范围

- Windows、PowerShell，Python **3.11+**；Python 部分只依赖标准库。
- 已有本地 Codex 数据。当前读取器适配 `state_5.sqlite`、`thread_history_1.sqlite` 及其分页表结构；
  这些是客户端内部结构，版本变化可能需要适配。不会尝试写回源数据库。
- 可选：支持 `node:zlib.createZstdDecompress` 的 Node.js，用于解压验证 `.jsonl.zst`。
  优先 PATH，其次复用已记录的 Codex Node 路径；没有时保留哈希检查，并明确报告未解压验证。
- 默认维护模型为 `gpt-6-luna / medium`。模型不可用时不会静默替换。

测试使用合成数据，无需登录 Codex、API key 或访问真实会话。
CI 配置覆盖 Windows/Linux 上的 Python 测试；实际 Codex 数据读取和 PowerShell 安装以 Windows 为主要验证环境。

## 安装

将源码检出到一个目录，**将私人备份保存到另一个目录**。普通用户 PowerShell 即可；仍须具有目标目录与个人 Codex 配置的写权限。

```powershell
# 在源码目录运行；先确认 python 指向 Python 3.11+
.\INSTALL.ps1 -Root 'E:\Codex-Continuity'
```

可指定 Python、Codex 数据目录或现有 Node：

```powershell
.\INSTALL.ps1 -Root 'E:\Codex-Continuity' `
  -Python 'C:\Path\To\python.exe' `
  -CodexHome "$env:USERPROFILE\.codex" `
  -NodePath 'C:\Path\To\node.exe'
```

安装器会复制代码/阅读入口，备份并合并当前生效的全局指令，添加专用角色和额外可写目录；
随后初始化本地备份、生成环境快照并深度检查。保留默认模型、推理强度和无关配置。
若全局 `AGENTS.override.md` 正在生效，规则写入该文件。`-SkipInitialBackup` 只安装配置，不读取会话历史。

仅预览改动、不写文件：

```powershell
python install.py --root 'E:\Codex-Continuity' --configure --dry-run
```

安装后重开 Codex 会话。配置变更不会自动扩大已有会话的沙箱权限；
确保备份目录在实际可写范围内。CLI 可以用 `codex --add-dir 'E:\Codex-Continuity'`；
桌面/受管环境还受当前会话和组织策略约束。无需关闭沙箱。

## 日常操作

```powershell
& 'E:\Codex-Continuity\continuity.ps1' sync
& 'E:\Codex-Continuity\continuity.ps1' check --deep
& 'E:\Codex-Continuity\continuity.ps1' prepare-switch
```

`prepare-switch` 刷新本地记录并生成环境快照。旧会话不自动逐篇调用模型补摘要。
维护 agent 按生成的 `MAINTENANCE.md` 操作，主 agent 复核事实后通过 `record` 提交摘要。
`examples/handoff.example.json` 是无个人数据的输入示例。

首次没有提供 `--current-session` 时，初始化范围是这份本地数据中的所有未归档会话，
不根据网页登录状态猜测账号。若需限定账号，可先使用 `-SkipInitialBackup` 安装，再执行
`continuity.ps1 init --current-session <该账号的会话ID>`。后续同步保留明确登记的来源账号。

## 换账号后

保持本机源文件和环境，在新账号的可写会话中发送：

> 读取 E:\Codex-Continuity\START_HERE.md，定位【项目或任务】，核对交接和当前文件后继续执行。

旧备份保持可读；显式提供新账号当前会话 ID 的同步会将该账号纳入后续维护。
登录、模型权限、插件与外部服务连接需要在新账号核验。仅存云端或其他设备的历史不由此工具自动获取。

## 数据与发布边界

**本仓库只发布代码和模板，实际备份目录应始终留在本地。**

原始对话、工具输出和共享技能中可能包含敏感内容。配置字段脱敏不是通用秘密扫描器，
也不删除原始对话中的内容；不要公开会话、记忆、快照、环境清单、报告或安装回退材料。
`.gitignore` 使用允许列表并补充运行数据排除规则。安装器拒绝将备份目录放在源码检出目录内。

已经备份的会话随后被归档，会从活动索引移除，但原有副本保留以便回退，不自动删除。

## 测试与撤销

```powershell
python -m unittest discover -s tests -v
```

安装前的材料保存在备份目录的 `tools/install-backups/`。按其中 `ROLLBACK.md` 定向撤销，
不要整体覆盖后来修改的配置，也不要用脱敏配置替换有效凭据。

## 官方资料与许可证

- [Codex AGENTS.md 加载方式](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
- [Codex 子 agent 与模型配置](https://learn.chatgpt.com/docs/agent-configuration/subagents)
- [MIT License](LICENSE)

发布步骤见 [PUBLISH.md](PUBLISH.md)。
