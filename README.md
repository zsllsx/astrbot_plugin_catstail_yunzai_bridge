# astrbot_plugin_catstail_yunzai_bridge

把 Yunzai 侧的 `catstail-yunzai-mcp` 接入 AstrBot，让 AI 在回答原神角色、武器、面板、圣遗物、练度和活动问题时，基于真实数据而不是模型记忆。

## 功能

- 启动时自动写入/更新 AstrBot 的 MCP 配置（`data/mcp_server.json`）
- 自动重连：Yunzai 后启动、重启或工具清单变化时可重试
- 身份注入：调用 `yz_*` 工具前把当前 QQ 写进 `qq` 参数
- 固定提示词：要求先查工具、缺失数据如实说明、面板数据带时间
- 随插件提供 Skill：`game-data-advisor`
- 管理指令：`/yz_mcp status|reload|tools`

## 前置条件

1. Yunzai 已安装并启用 `catstail-yunzai-mcp`。
2. 从 `plugins/catstail-yunzai-mcp/config/config.yaml` 获取自动生成的 Token。
3. AstrBot 能访问 Yunzai 的 HTTP 地址（同机用 `127.0.0.1`，Docker 用宿主机 IP / 服务名）。

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
| `inject_identity` | `true` | 调用 `yz_*` 前注入当前 QQ |
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
| `/yz_mcp status` | 管理员 | 查看地址、Token 掩码、连接状态、工具数、最近错误 |
| `/yz_mcp reload` | 管理员 | 重新写入配置并重连 MCP |
| `/yz_mcp tools` | 管理员 | 列出已注册的 `yz_*` 工具 |

## 工作流程

```text
AstrBot 启动
  └─ on_astrbot_loaded
       ├─ GET /mcp/manifest 校验 Token / 获取工具清单
       ├─ 写入 data/mcp_server.json 的 catstail-yunzai 节点
       ├─ disable + enable MCP（刷新工具）
       └─ 失败则按 retry_interval 重试

用户提问
  ├─ on_llm_request：追加固定规则
  ├─ 模型调用 yz_player_profile（不填 qq）
  ├─ on_using_llm_tool：tool_args["qq"] = 当前 QQ
  └─ MCP 返回数据，模型基于数据回答
```

## 排障

| 现象 | 排查 |
| --- | --- |
| `/yz_mcp status` 未连接 | 检查 `base_url` 是否可达、Yunzai 是否启动、Token 是否一致 |
| 401 / Token 无效 | 重新复制 `config/config.yaml` 中的 Token，保存后 `/yz_mcp reload` |
| 工具列表为空 | 查看 AstrBot 日志中的 `MCP 已连接`，确认 Yunzai 侧 8 个工具已注册 |
| 模型不调用工具 | 检查人格是否启用了 MCP 工具；确认 `inject_prompt` 未被关闭 |
| 查不到数据 | 用户是否绑定 UID、是否更新过面板；`stale=true` 时先 `#更新面板` |
| Docker 连不上宿主机 | 用宿主机内网 IP，或配置 `host.docker.internal` / 同一 compose 网络 |

## 兼容性

- AstrBot `>=4.23.1`（`on_using_llm_tool` 钩子）。
- 依赖 AstrBot 内部 API：`get_llm_tool_manager()`、`load_mcp_config/save_mcp_config`、`enable_mcp_server/disable_mcp_server`、`mcp_server_runtime_view`。升级 AstrBot 后需回归。
- 仅处理 `yz_*` 工具；Yunzai 侧目前只支持原神（`gs`）。
