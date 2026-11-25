# astrbot_plugin_catstail_yunzai_bridge

把 Yunzai 侧的 `catstail-yunzai-mcp` 接入 AstrBot，让 AI 在回答原神/星铁角色、武器（光锥）、面板、圣遗物（遗器）、练度和活动问题时，基于真实数据而不是模型记忆。

## 功能

- 启动时自动写入/更新 AstrBot 的 MCP 配置（`data/mcp_server.json`）
- 自动重连：Yunzai 后启动或重启时按重试间隔自动连接；Yunzai 侧工具清单变化后执行 `/yz_mcp reload` 刷新
- 上下文注入：调用 `yz_*` 工具前把当前 QQ、群号、消息 ID 写进对应参数（只注入工具声明过的字段）
- 固定提示词：要求先查工具、缺失数据如实说明、面板数据带时间；角色别称/简写/错字先走 `yz_*_alias_resolve`
- 剧情/语音语料：`yz_*_lore_search` / `lore_entry` / `voice_list` / `voice_line` 与 `yz_lore_sources`；Yunzai 侧未导入语料时整组不注册，插件也不会追加语料规则
- 随插件提供 Skill：`game-data-advisor`
- 管理指令：`/yz_mcp status|reload|tools`

## 前置条件

1. Yunzai 已安装并启用 `catstail-yunzai-mcp`。
2. 从 `plugins/catstail-yunzai-mcp/config/config.yaml` 获取自动生成的 Token。
3. AstrBot 能访问 Yunzai 的 HTTP 地址（同机用 `127.0.0.1`，Docker 用宿主机 IP / 服务名）。
4. 需要剧情/语音能力时，Yunzai 侧先导入语料（`node plugins/catstail-yunzai-mcp/scripts/lore-import.mjs --from genshin-db --game gs`），否则语料工具不会注册。

## 安装

从 GitHub 安装时，AstrBot 会按 `metadata.yaml` 中的 `name` 创建插件目录，仓库名不影响安装结果。

手动安装时，目录名必须是合法 Python 标识符，不能带连字符：

```text
<AstrBot>/data/plugins/astrbot_plugin_catstail_yunzai_bridge/
```

> 本地开发目录 `astrbot-plugins` 带连字符，不能直接作为插件目录名；复制时请重命名为上面的名称。

重启 AstrBot，或在 WebUI 的插件页重载。插件不需要额外 pip 依赖（使用 AstrBot 自带的 `aiohttp`）。

## 配置

WebUI → 插件 → `Catstail 游戏数据桥接`。

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 总开关 |
| `base_url` | `http://127.0.0.1:2536` | Yunzai 地址，不带 `/mcp` |
| `mcp_path` | `/mcp` | MCP 路由路径 |
| `token` | 空 | Yunzai 插件生成的 Token |
| `server_name` | `catstail-yunzai` | AstrBot 中的 MCP 服务名 |
| `auto_register` | `true` | 自动写入并启用 MCP 配置 |
| `overwrite_existing` | `true` | 配置存在时也重写并重连，使新 Token/地址生效 |
| `inject_identity` | `true` | 调用 `yz_*` 前注入当前 QQ / 群号 / 消息 ID |
| `inject_prompt` | `true` | 追加游戏数据规则到系统提示词 |
| `tool_prefix` | `yz_` | 需要注入身份的工具前缀 |
| `connect_timeout` | `30` | 连接/清单请求超时（秒） |
| `enable_timeout` | `30` | 动态启用 MCP 超时（秒） |
| `sse_read_timeout` | `300` | MCP 读取超时（秒） |
| `retry_max` | `10` | 启动连接失败的最大尝试次数 |
| `retry_interval` | `15` | 重试间隔（秒） |

### 跨公网 / Docker

- AstrBot 在容器内、Yunzai 在宿主机时，`base_url` 填宿主机可达地址，例如 `http://192.168.1.10:2536` 或 `http://host.docker.internal:2536`。
- 跨公网建议使用 HTTPS 反向代理，并在 Yunzai 侧配置 `publicBaseUrl` 与 `allowedIps`。
- 不要把 Token 写进公开仓库或截图。

## 身份规则

- 群聊中 `qq` 取当前发言人，不取被 @ 的人，因此只能查“自己”。
- 用户明确给出他人 UID 时，模型可以填 `uid`；Yunzai 侧显式 UID 优先。
- 非 QQ 平台或取不到发送者时，会移除模型可能编造的 `qq`，让 Yunzai 返回 `UID_NOT_BOUND`。
- 本插件不做权限审计；MCP Token 等同于游戏数据读取权限。

## 指令

| 指令 | 权限 | 作用 |
| --- | --- | --- |
| `/yz_mcp status` | 管理员 | 查看地址、Token 掩码、连接状态、工具数、语料工具数、自愈参数、最近错误 |
| `/yz_mcp reload` | 管理员 | 重新写入配置并重连 MCP |
| `/yz_mcp reconnect` | 管理员 | 立即自愈连接（不重写配置） |
| `/yz_mcp tools` | 管理员 | 列出已注册的 `yz_*` 工具 |
| `/yz_mcp update` | 管理员 | 拉取插件最新代码，然后自动重启 AstrBot 生效 |
| `/yz_mcp update no-restart` | 管理员 | 只检查/拉取，不重启（想自己挑时间重启时用） |

## 插件自更新

容器里管理面板更新不便时，可以直接在群里拉代码：

```text
/yz_mcp update
```

1. `git fetch` 当前分支 —— 容器镜像通常没装 ssh，而 `origin` 是 SSH 地址，会自动改用 HTTPS 重试（公开仓库无需凭据）；
2. 对比 `HEAD` 与远端提交，相同就直接回「已是最新版本」；
3. 有更新则 `git merge --ff-only`（不做合并提交，本地有改动/分叉会直接失败并回报）；
4. 回复 `旧提交 → 新提交` 与变更文件，再调用 AstrBot 内置 `restart_process` 自动重启，新代码即时生效。

注意：插件目录是 bind mount，容器内 `git` 操作会直接改宿主机工作区，与在宿主机执行等价；重启期间 AstrBot 会有几秒不可用。

## 连接自愈

AstrBot 只会在 `anyio.ClosedResourceError` 时自动重连 MCP，而 Yunzai 侧会话 30 分钟无请求会被回收，之后抛的是 `McpError("Session terminated")` / `404 Session not found` —— 不会触发重连，表现为**失败一次之后就一直失败，直到重启 AstrBot**。

本插件补了兜底（`auto_heal` 默认开启）：

- **调用前探针**：调用 `yz_*` 前用 `session.list_tools()` 探一次会话（默认 15s 内只探一次），失效就先重连再执行。
- **后台巡检**：默认 60s 一次，先 `GET /mcp/health` 判断 Yunzai 是否可达，不可达直接跳过；可达则探会话并自愈。巡检会刷新服务端 `lastSeen`，等于顺带保活。
- **两层重连**：优先 `client._reconnect()` 原地换 session（工具对象不变，用户无感）；失败再退回 `disable + enable` 重注册工具。
- **冷却**：`heal_cooldown` 默认 10s，断网期间不会反复重连把 AstrBot 拖住。

可调项：`auto_heal` / `health_interval` / `probe_interval` / `probe_timeout` / `heal_cooldown`，建议 `sse_read_timeout` 保持 ≥ 900 秒。

## 工作流程

```text
AstrBot 启动
  └─ on_astrbot_loaded
       ├─ GET /mcp/manifest 校验 Token / 获取工具清单
       ├─ 写入 data/mcp_server.json 的 catstail-yunzai 节点
       ├─ disable + enable MCP（刷新工具）
       └─ 失败则按 retry_interval 重试

用户提问
  ├─ on_llm_request：追加固定规则；存在语料工具时再追加语料规则
  ├─ 模型调用 yz_gs_player_profile（不填 qq）或 yz_gs_lore_search
  ├─ on_using_llm_tool：tool_args["qq"] = 当前 QQ（仅注入工具声明过的参数）
  └─ MCP 返回数据，模型基于数据回答
```

## 排障

| 现象 | 排查 |
| --- | --- |
| `/yz_mcp status` 未连接 | 检查 `base_url` 是否可达、Yunzai 是否启动、Token 是否一致 |
| 401 / Token 无效 | 重新复制 `config/config.yaml` 中的 Token，保存后 `/yz_mcp reload` |
| 工具列表为空 | 查看 AstrBot 日志中的 `MCP 已连接`，确认 Yunzai 侧工具已注册（`gs` 下完整为 14 个） |
| 只有 9 个工具、没有 `lore_*` | 语料组按数据注册：Yunzai 侧未导入 `data/lore` 时属正常现象；已导入则执行 `/yz_mcp reload` 刷新清单 |
| 模型不调用工具 | 检查人格是否启用了 MCP 工具；确认 `inject_prompt` 未被关闭 |
| 查不到数据 | 用户是否绑定 UID、是否更新过面板；`stale=true` 时先 `#更新面板` |
| 剧情/台词答“没有查到” | 语料是否导入、问题是否超出语料范围；用 `yz_lore_sources` 确认版本与覆盖类型 |
| Docker 连不上宿主机 | 用宿主机内网 IP，或配置 `host.docker.internal` / 同一 compose 网络 |
| 工具一直报 `Session terminated` / `MCP session is not available` | 会话已失效但 AstrBot 不会自愈老逻辑；本插件会自动重建，也可 `/yz_mcp reconnect` 立即修复 |
| 自愈一直失败 | 看 `/yz_mcp status` 的「最近错误」：`/mcp/health` 不通说明 Yunzai 没起来；Token 失效会在自愈回退路径中报 401 |
| 空闲半小时后第一次调用失败 | 服务端会话 30 分钟 TTL 已回收；确认 `health_interval` < 1800s 且 `auto_heal` 未被关闭 |

## 兼容性

- AstrBot `>=4.23.1`（`on_using_llm_tool` 钩子）。
- 依赖 AstrBot 内部 API：`get_llm_tool_manager()`、`load_mcp_config/save_mcp_config`、`enable_mcp_server/disable_mcp_server`、`mcp_server_runtime_view`。升级 AstrBot 后需回归。
- 仅处理 `yz_*` 工具；Yunzai 侧已接入原神（`gs`）与星铁（`sr`），绝区零（`zzz`）未接入。
- 语料工具（`lore_*` / `voice_*` / `yz_lore_sources`）按数据注册，未导入 `data/lore` 时不出现，提示词里的语料规则也会自动省略。
- 提示词中的工具清单按 `yz_<game>_*` 折叠成一行（如 `yz_gs_*: character_list/.../voice_line`），随 manifest 变化自动刷新。
