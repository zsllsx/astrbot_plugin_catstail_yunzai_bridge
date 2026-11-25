# AGENTS.md

本文件记录当前的工作方式、约定与待办，供后续 AI/协作者快速接手。

## 这是什么

AstrBot 插件 `astrbot_plugin_catstail_yunzai_bridge`：把 Yunzai 侧的 `catstail-yunzai-mcp`（Streamable HTTP MCP Server）接入 AstrBot，
并在模型调用 `yz_*` 工具前注入当前 QQ、群号、消息 ID，避免模型询问 UID 或串号。

- 上游仓库：`git@github.com:zsllsx/astrbot_plugin_catstail_yunzai_bridge.git`（分支 `main`）
- 服务端仓库：`catstail-yunzai` 中的 `plugins/catstail-yunzai-mcp`
- 运行环境：Docker 容器 `astrbot`，`soulter/astrbot:latest`

## 工作目录已迁移（2026-11-25）

| 项 | 位置 |
| --- | --- |
| **当前唯一工作目录** | `D:\docker\astrbot\data\plugins\astrbot_plugin_catstail_yunzai_bridge`（本目录） |
| 容器内路径 | `/AstrBot/data/plugins/astrbot_plugin_catstail_yunzai_bridge` |
| 已废弃 | `D:\workspace\catstail-project\astrbot-plugins`（内容已发布并同步，勿再编辑） |
| 一键脚本 | `D:\workspace\catstail-project\update-astrbot-plugin.ps1` / `.cmd` |

本目录是宿主目录与容器数据卷的同一份文件（bind mount），改完重启容器即生效，无需复制。

## 当前工作内容

### 已完成：对接 Yunzai 侧语料工具

Yunzai 侧新增 5 个语料工具（`yz_<game>_lore_search` / `lore_entry` / `voice_list` / `voice_line` / `yz_lore_sources`），
且**只在导入了 `data/lore` 时才注册**。本插件的对应改动：

1. `main.py`
   - 新增 `LORE_RULES`：剧情/台词必须先检索再取全文、`next_offset` 分页、检索为空不得编造。
   - 规则**条件追加**：仅当已连接的工具里存在 `lore_*` / `voice_*` / `yz_lore_sources` 时才拼进系统提示（`is_lore_tool()`）。
   - `_tool_summary()`：把工具名按 `yz_<game>_*` 折叠成一行，避免 20+ 个工具名撑长提示词。
   - `/yz_mcp status` 增加“语料: N 个工具 / 未注册（Yunzai 侧未导入 data/lore）”。
2. `skills/game-data-advisor/SKILL.md`：意图表补语料工具，补语料差异与禁止项。
3. `README.md` / `metadata.yaml`：工具数、前置条件（需先导入语料）、排障与功能描述同步。

### 待办 / 可继续做

- [ ] 语义（向量）召回：目前依赖 Yunzai 侧 BM25，本插件无需改动。
- [ ] 语料工具的调用失败话术：被限流 / 语料缺失时引导用户说什么，可在 `LORE_RULES` 继续细化。
- [ ] 把本文件纳入版本管理（当前为未跟踪文件）。

## 开发流程

```powershell
# 1. 直接编辑本目录下的文件

# 2. 让改动生效（二选一）
docker restart astrbot
D:\workspace\catstail-project\update-astrbot-plugin.ps1 -NoRestart   # 只同步不重启

# 3. 群里发测试消息验证（见 README 与下方清单）

# 4. 发布
git -C "D:\docker\astrbot\data\plugins\astrbot_plugin_catstail_yunzai_bridge" add -A
git -C "D:\docker\astrbot\data\plugins\astrbot_plugin_catstail_yunzai_bridge" commit -m "feat: ..."
git -C "D:\docker\astrbot\data\plugins\astrbot_plugin_catstail_yunzai_bridge" push
# 或：update-astrbot-plugin.ps1 -Push
```

## 目录与关键文件

```text
main.py                  # 插件主体：MCP 配置同步、身份注入、/yz_mcp 指令
metadata.yaml            # AstrBot 插件元信息（name 必须与目录名一致）
_conf_schema.json        # WebUI 配置项（enabled/base_url/token/...）
README.md                # 安装、配置、排障
skills/game-data-advisor/SKILL.md   # 随插件分发的 Skill
```

## 约定与坑

- **身份注入**：`inject_identity` 只写入工具 schema 里声明过的参数；工具拿不到参数定义时退回只注入 `qq`。
- **自更新**（2026-11-26）：`/yz_mcp update` = fetch → 对比 → `merge --ff-only` → `restart_process` 自动重启。容器镜像**没有 ssh 客户端**，而 origin 是 SSH 地址，所以必须能回退到 HTTPS（`_https_fallback`），否则 fetch 一定失败。插件目录是 bind mount，容器内 merge 等于改宿主机工作区。
- **注入优先级**（2026-11-26）：`qq` / `message_id` 强制覆盖模型填的值（模型不可能知道，编造即串号或贴错消息）；`group_id` 仅在模型给的**不是纯数字**或缺失时覆盖，纯数字保留（允许显式操作其他群）。拿不到真实 `message_id` 时主动清空模型值，让服务端返回“缺少 message_id”，而不是让它贴到随机消息上。
- **`message_id` 在 Yunzai 侧 schema 必须是 optional**：声明为 required 时模型无法提供，会直接拒绝调用工具并回复“没有 message_id”，用户侧表现为贴表情不可用。
- **连接自愈**（2026-11-26）：AstrBot 核心只在 `anyio.ClosedResourceError` 时重连，而 Yunzai 侧会话 30 分钟 TTL 被回收后抛的是 `McpError("Session terminated")` / `404 Session not found`，导致“失败一次就一直失败”。插件补了兜底：调用前探针（`on_using_llm_tool`，15s TTL 缓存）+ 后台巡检（60s，先查 `/mcp/health` 再探会话）+ `client._reconnect()` 原地重连 + disable/enable 降级 + 10s 冷却；`/yz_mcp reconnect` 可手动强制。巡检同时刷新服务端 `lastSeen`，顺带保活。<br>**注意**：`_reconnect` 是 AstrBot 内部 API，升级要回归；`probe` 用 `session.list_tools()`，别改成不存在的 `send_ping`（本版本 SDK 没有）。
- **提示词追加不覆盖**：用 `PROMPT_MARKER` 去重，`system_prompt` 可能被其他插件/人格改写。
- **工具清单必须来自 manifest**：本地缓存只用于 Yunzai 未启动时的提示，不能写死工具名。
- **语料工具按数据注册**：Yunzai 侧没导入语料就不存在这些工具，提示里也不能提。
- **编码**：本目录中文文件统一 UTF-8；`.ps1` 必须带 BOM（Windows PowerShell 5.1 才能正确读中文）。
- **不要提交** `__pycache__/`、`.env`、Token。

## 验证清单

- [ ] `/yz_mcp status`：显示“连接: 已连接”，工具数 14（`gs` 全套 + 语料）或 9（未导入语料）。
- [ ] `/yz_mcp tools`：能看到 `yz_gs_lore_search` / `yz_gs_voice_line` / `yz_lore_sources`。
- [ ] 群聊“胡桃的语音都有哪些？”→ 走 `voice_list`，返回标题而非编造台词。
- [ ] 群聊“可莉传说任务第三幕的逐句对白”→ 明确说没有该语料，不编造。
- [ ] 群聊给 `uid` 查群友面板可用；不填 `qq`/`uid` 查本人由插件注入。
- [ ] 重启容器后 AstrBot 日志出现 `MCP 已连接: catstail-yunzai, tools=N`。
