from __future__ import annotations

import asyncio
import copy
import hashlib
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

PROMPT_MARKER = "【catstail 游戏数据规则】"

# 插件自身目录：容器内是 bind mount 的 /AstrBot/data/plugins/astrbot_plugin_catstail_yunzai_bridge
PLUGIN_DIR = Path(__file__).resolve().parent

SYSTEM_RULES = """【catstail 游戏数据规则】
1. 涉及原神（gs）的角色、武器、面板、圣遗物、练度、活动时用 yz_gs_* 工具；涉及星铁（sr）的角色、光锥、面板、遗器、练度、活动时用 yz_sr_* 工具。必须先调用工具，不得凭记忆回答。
2. 工具名已绑定游戏，不要再给工具传 game 参数；不要用 yz_gs_* 查星铁数据，反之亦然。
3. 查询本人不要填写 qq 或 uid：qq 由桥接插件自动注入；用户明确给出他人 UID 时才填写 uid。
4. 工具返回 ok=false 时，按 error.hint 告知用户；没有数据就说没有，禁止推测或补全数值。
5. 回答面板时带上数据时间；stale=true 时提醒用户可先执行 #更新面板。
6. 未接入的游戏（如绝区零）直接说明暂不支持，不要假装查询；没有对应前缀的工具也不要用其他游戏的工具顶替。
7. 工具返回的数值是唯一事实来源；配队/词条建议必须基于这些数值。
8. 只有用户明确要求"戳一戳/贴表情/发表情包"时才调用群互动工具；一次调用只做一次，被冷却拒绝时如实告知还要等多久，不要连续重试。
9. 群互动工具里的 qq、group_id、message_id 全部由插件在调用前自动注入真实值，你不需要也无法提供：不要填写、不要编造，更不要因为"没有 message_id"就拒绝调用或让用户重新发消息，直接调用工具即可；只有工具返回消息找不到/已过期这类错误时，才请用户重新发一条。
10. 用户用别称、简写、错字或称号称呼角色（如“胡淘”“往生堂堂主”）时，先用对应游戏的 yz_*_alias_resolve 确认是哪个角色，再查面板或圣遗物；解析不到就如实说明，不要猜成其他角色。"""

# 语料工具按数据注册：Yunzai 侧没导入语料就整组不注册，规则也要跟着条件追加，
# 否则模型会去调用不存在的工具。
LORE_RULES = """【剧情与语音语料规则】
- 角色故事、语音台词、成就、名片、地理志、衣装、动物/敌人图鉴、剧情考据，先用 yz_<game>_lore_search 检索，再用 yz_<game>_lore_entry 取全文。
- 台词问题先 yz_<game>_voice_list 取标题，再用 yz_<game>_voice_line 取原文；返回 candidates（标题命中多条）时把候选交给用户选，不要自行挑一条。
- lore_entry 返回 next_offset 表示还有后续，需要完整内容就继续按 offset 分页；引用时带上 source 与 source_url，不要改写原文。
- 检索为空就如实说没有，禁止用自身记忆补全剧情、台词或出处；语料覆盖范围与版本以 yz_lore_sources 为准。"""

# 用于把 yz_<game>_* 工具名折叠成一行提示，避免 20+ 个工具名把系统提示词撑长
GAME_SEGMENTS = ("gs", "sr", "zzz")
LORE_TOOL_HINTS = ("_lore_", "_voice_", "yz_lore_")


def is_lore_tool(name: str) -> bool:
    return name.startswith("yz_") and any(hint in name for hint in LORE_TOOL_HINTS)


def _on_plugin_loaded():
    """兼容没有 on_plugin_loaded 钩子的 AstrBot 版本（缺省退化为 no-op）。"""
    deco = getattr(filter, "on_plugin_loaded", None)
    return deco() if deco else (lambda fn: fn)


class FatalSyncError(Exception):
    """配置或鉴权错误，重试也不会成功。"""


class CatstailYunzaiBridge(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._sync_task: asyncio.Task[None] | None = None
        self._sync_lock = asyncio.Lock()
        self._heal_lock = asyncio.Lock()
        self._watch_task: asyncio.Task[None] | None = None
        self._last_heal: float = 0.0
        self._last_probe: float = 0.0
        self._last_error: str | None = None
        self._last_success: float | None = None
        self._last_resync: float = 0.0
        self._warned_loopback: bool = False
        self._manifest: dict[str, Any] | None = None
        self._tools_cache: tuple[float, list[str]] | None = None

    # ------------------------------------------------------------------ config

    def _get(self, key: str, default: Any = None) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _as_int(self, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(self._get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    @property
    def enabled(self) -> bool:
        return bool(self._get("enabled", True))

    @property
    def server_name(self) -> str:
        return str(self._get("server_name", "catstail-yunzai")).strip() or "catstail-yunzai"

    @property
    def tool_prefix(self) -> str:
        return str(self._get("tool_prefix", "yz_")).strip() or "yz_"

    def _base_url(self) -> str:
        """MCP 基础地址。

        优先级：环境变量 `CATSTAIL_MCP_URL` > 插件配置。

        环境变量放最高，是为了覆盖"清空 data 目录重装后配置回到 schema 默认值
        127.0.0.1"的情况——容器里 127.0.0.1 指向容器自身，必然连不上，而
        `_config.json` 里的默认值又会把空配置填满，只靠配置项永远纠正不了。
        """
        env = os.environ.get("CATSTAIL_MCP_URL", "").strip()
        base = (env or str(self._get("base_url", "")).strip()).rstrip("/")
        if not base:
            raise FatalSyncError(
                "未配置 base_url：请在插件配置中填写，"
                "或为容器设置环境变量 CATSTAIL_MCP_URL（形如 http://172.20.0.1:2536）"
            )
        if not base.startswith(("http://", "https://")):
            raise FatalSyncError("base_url 必须以 http:// 或 https:// 开头")
        return base

    @staticmethod
    def _base_url_source() -> str:
        return "环境变量" if os.environ.get("CATSTAIL_MCP_URL", "").strip() else "配置"

    def _mcp_url(self) -> str:
        path = str(self._get("mcp_path", "/mcp")).strip() or "/mcp"
        if not path.startswith("/"):
            path = "/" + path
        return self._base_url() + path

    def _manifest_url(self) -> str:
        return self._mcp_url().rstrip("/") + "/manifest"

    def _effective_token(self) -> str:
        """实际使用的 Token：插件配置优先，缺失时回退环境变量。

        AstrBot 在插件安装失败、或在面板里删除重装时会连 `_config.json` 一起删掉，
        配置项回到空值；此时若没有兜底，任何一次重装都会永久失去连接能力。
        """
        token = str(self._get("token", "")).strip()
        if token:
            return token
        return os.environ.get("CATSTAIL_MCP_TOKEN", "").strip()

    def _token_source(self) -> str:
        if str(self._get("token", "")).strip():
            return "配置"
        return "环境变量" if os.environ.get("CATSTAIL_MCP_TOKEN", "").strip() else "未配置"

    def _token(self) -> str:
        token = self._effective_token()
        if not token:
            raise FatalSyncError(
                "未配置 MCP Token：请在插件配置中填写 Yunzai 生成的 Token，"
                "或为容器设置环境变量 CATSTAIL_MCP_TOKEN"
            )
        return token

    def _server_config(self) -> dict[str, Any]:
        return {
            "transport": "streamable_http",
            "url": self._mcp_url(),
            "headers": {"Authorization": f"Bearer {self._token()}"},
            "timeout": self._as_int("connect_timeout", 30, 3, 120),
            "sse_read_timeout": self._as_int("sse_read_timeout", 1800, 30, 1800),
            "active": True,
        }

    @staticmethod
    def _header_auth(server_cfg: Any) -> str:
        """取 MCP 配置里真正会发出去的 Authorization 串。"""
        if not isinstance(server_cfg, dict):
            return ""
        headers = server_cfg.get("headers")
        if not isinstance(headers, dict):
            return ""
        return str(headers.get("Authorization") or "")

    @staticmethod
    def _fingerprint(auth_header: str) -> str:
        """Token 指纹，只用于比对，不泄漏原值。"""
        token = str(auth_header or "").split(" ", 1)[-1].strip()
        if not token:
            return "未配置"
        return hashlib.sha256(token.encode()).hexdigest()[:8]

    def _warn_loopback(self) -> None:
        """容器内用 127.0.0.1 一定连不到宿主机上的 Yunzai，提前说清楚。"""
        if self._warned_loopback:
            return
        self._warned_loopback = True
        in_container = os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")
        if not in_container:
            return
        try:
            host = (urlparse(self._mcp_url()).hostname or "").lower()
        except Exception:
            return
        if host in ("127.0.0.1", "localhost", "::1"):
            self.logger.warning(
                f"base_url 指向 {host}，在容器里这是容器自身，连不到宿主机上的 Yunzai。"
                "请改成 host.docker.internal 或宿主机内网 IP"
                "（Linux Docker 需为容器加 --add-host=host.docker.internal:host-gateway）"
            )

    def _runtime_entry(self) -> Any:
        """取本服务在 AstrBot 运行时表里的条目（可能为 None）。"""
        try:
            runtime = self.context.get_llm_tool_manager().mcp_server_runtime_view or {}
            return runtime.get(self.server_name)
        except Exception:
            return None

    def _connected(self) -> bool:
        """运行时是否真的可用。

        不能只看「名字在 mcp_server_runtime_view 里」：生命周期任务已经结束、
        但条目尚未清理时仍会命中，于是 `_apply` 误判为“已连接且配置未变”而跳过
        重连，连接就永久坏在“已连接”状态。
        """
        entry = self._runtime_entry()
        if entry is None:
            return False
        task = getattr(entry, "lifecycle_task", None)
        done = getattr(task, "done", None)
        if task is not None and callable(done) and done():
            return False
        return True

    # ------------------------------------------------------------------ manifest

    async def _fetch_manifest(self) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(
            total=self._as_int("connect_timeout", 30, 3, 120)
        )
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Accept": "application/json",
        }
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(self._manifest_url(), headers=headers) as response:
                    if response.status in (401, 403):
                        raise FatalSyncError(
                            "MCP Token 无效或权限不足，请检查 Yunzai 插件配置"
                        )
                    if response.status != 200:
                        raise RuntimeError(
                            f"manifest HTTP {response.status}: {response.reason}"
                        )
                    data = await response.json(content_type=None)
        except FatalSyncError:
            raise
        except asyncio.TimeoutError as exc:
            raise RuntimeError("manifest 请求超时") from exc
        except aiohttp.ClientError as exc:
            raise RuntimeError(f"manifest 请求失败: {exc}") from exc
        except ValueError as exc:
            raise RuntimeError(f"manifest 返回的不是合法 JSON: {exc}") from exc

        if not isinstance(data, dict):
            raise RuntimeError("manifest 返回格式异常")
        return data

    async def _remember_manifest(self, manifest: dict[str, Any]) -> None:
        self._manifest = manifest
        try:
            await self.put_kv_data(
                "manifest",
                {
                    "toolsHash": manifest.get("toolsHash"),
                    "tools": manifest.get("tools") or [],
                    "games": manifest.get("games") or [],
                    "time": time.time(),
                },
            )
        except Exception as exc:
            self.logger.debug(f"写入 manifest 缓存失败: {exc}")

    async def _restore_manifest(self) -> None:
        """启动时回填上次的清单。

        manifest 只在联网拉取成功时才有，若 Yunzai 尚未启动，提示词里的工具清单会为空。
        读回缓存后，至少能按上次的工具名提示模型，状态页也能看到 hash。
        """
        if self._manifest:
            return
        try:
            cached = await self.get_kv_data("manifest", None)
        except Exception as exc:
            self.logger.debug(f"读取 manifest 缓存失败: {exc}")
            return
        if not isinstance(cached, dict):
            return
        tools = cached.get("tools")
        if not isinstance(tools, list) or not tools:
            return
        self._manifest = {
            "tools": tools,
            "toolsHash": cached.get("toolsHash"),
            "games": cached.get("games") or [],
        }
        self.logger.debug(f"已载入 manifest 缓存: tools={len(tools)}")

    # ------------------------------------------------------------------ mcp sync

    async def _apply(self, force: bool = False) -> None:
        if not self.enabled:
            return
        self._warn_loopback()

        async with self._sync_lock:
            server_cfg = self._server_config()
            name = self.server_name

            manifest: dict[str, Any] | None = None
            try:
                manifest = await self._fetch_manifest()
            except FatalSyncError:
                raise
            except Exception as exc:
                self.logger.warning(f"读取 manifest 失败，仍尝试连接 MCP: {exc}")

            if manifest is not None:
                await self._remember_manifest(manifest)

            if not bool(self._get("auto_register", True)):
                self.logger.info("auto_register 已关闭，跳过 MCP 配置写入")
                return

            tool_mgr = self.context.get_llm_tool_manager()
            for method in ("load_mcp_config", "save_mcp_config", "enable_mcp_server"):
                if not hasattr(tool_mgr, method):
                    raise FatalSyncError(
                        f"当前 AstrBot 版本缺少 FunctionToolManager.{method}，"
                        "无法自动写入 MCP 配置；请升级到 >=4.23.1 或在面板手工配置"
                    )
            current = tool_mgr.load_mcp_config()
            if not isinstance(current, dict):
                current = {}
            current = copy.deepcopy(current)
            servers = current.setdefault("mcpServers", {})
            if not isinstance(servers, dict):
                servers = {}
                current["mcpServers"] = servers

            existing = servers.get(name)
            if not isinstance(existing, dict):
                existing = None
            same = existing == server_cfg
            connected = self._connected()

            # 磁盘配置里的 Authorization 才是真正发出去的 token（不是插件配置项）。
            # 两者不一致时强制重写并重连：否则会出现「配置改了、重启了，
            # 发出去的还是旧 Token」的 401。
            disk_auth = self._header_auth(existing)
            want_auth = self._header_auth(server_cfg)
            if existing is not None and disk_auth != want_auth:
                self.logger.warning(
                    f"MCP 实际使用 Token 与插件配置不一致（磁盘 {self._fingerprint(disk_auth)}"
                    f" → 期望 {self._fingerprint(want_auth)}），强制重写并重连"
                )
                force = True

            if same and connected and not force:
                self.logger.debug(f"MCP 配置未变化且已连接: {name}")
                self._last_success = time.time()
                return

            if not bool(self._get("overwrite_existing", True)) and connected and not force:
                self.logger.info("overwrite_existing=false，保留现有连接")
                return

            servers[name] = server_cfg
            if not tool_mgr.save_mcp_config(current):
                raise RuntimeError("写入 data/mcp_server.json 失败")

            # 只要有残留条目就尝试关闭：生命周期任务已结束但条目未清理时，
            # enable 同一个名字可能直接冲突，这里顺手清掉。
            if self._runtime_entry() is not None:
                try:
                    await tool_mgr.disable_mcp_server(name, timeout=10)
                except Exception as exc:
                    self.logger.warning(f"关闭旧 MCP 连接失败（继续重建）: {exc}")

            timeout = self._as_int("enable_timeout", 30, 3, 120)
            try:
                await tool_mgr.enable_mcp_server(name, server_cfg, timeout=timeout)
            except Exception as exc:
                raise RuntimeError(f"启用 MCP 服务失败: {exc}") from exc

            if not self._connected():
                raise RuntimeError(
                    "MCP 服务未出现在运行时列表，或生命周期任务已结束（可能仍在初始化）"
                )

            self._tools_cache = None
            tools = self._mcp_tools()
            self._last_error = None
            self._last_success = time.time()
            self.logger.info(
                f"MCP 已连接: {name}, tools={len(tools)}"
                + (f", hash={manifest.get('toolsHash')}" if manifest else "")
            )

    @staticmethod
    def _unwrap_tool(item: Any) -> Any:
        """剥掉框架的权限代理包装，找回原始工具对象。"""
        seen: set[int] = set()
        while True:
            nxt = getattr(item, "_wrapped", None)
            if nxt is None or id(item) in seen:
                return item
            seen.add(id(item))
            item = nxt

    _TOOLS_CACHE_TTL = 3.0

    def _mcp_tools(self) -> list[str]:
        """列出属于本 MCP 服务的工具名（带短缓存）。

        inject_identity / guard_connection / inject_rules 会在每次工具调用与 LLM
        请求时调用；上游 func_list 可能有上百个工具，逐个解包包装层是纯浪费，
        因此缓存 3 秒，配置变更/重连后主动失效。
        """
        cached = self._tools_cache
        if cached is not None and time.time() - cached[0] < self._TOOLS_CACHE_TTL:
            return list(cached[1])
        names = self._scan_mcp_tools()
        self._tools_cache = (time.time(), names)
        return list(names)

    def _scan_mcp_tools(self) -> list[str]:
        """扫描 func_list 得到本服务的工具名。

        框架可能把工具包装成 _PermissionGuardedTool，包装层只复制 name 而丢掉
        mcp_server_name，因此先解包再比对；解包后仍无法判定归属时退回工具名前缀。
        """
        try:
            func_list = getattr(self.context.get_llm_tool_manager(), "func_list", [])
        except Exception:
            return []

        server = self.server_name
        prefix = self.tool_prefix
        names: list[str] = []
        for item in func_list:
            name = str(getattr(item, "name", "") or "")
            if not name:
                continue
            owner = getattr(self._unwrap_tool(item), "mcp_server_name", None)
            if owner == server or (owner is None and name.startswith(prefix)):
                names.append(name)
        return names

    async def _background_sync(self) -> None:
        attempts = self._as_int("retry_max", 10, 1, 60)
        interval = self._as_int("retry_interval", 15, 2, 300)
        for attempt in range(1, attempts + 1):
            try:
                await self._apply(force=attempt > 1)
                return
            except FatalSyncError as exc:
                self._last_error = str(exc)
                self.logger.error(f"桥接配置错误: {exc}")
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)
                self.logger.warning(
                    f"同步 MCP 失败（{attempt}/{attempts}）: {exc}"
                )
                if attempt < attempts:
                    await asyncio.sleep(interval)

    # ------------------------------------------------------------------ self-heal

    def _health_url(self) -> str:
        return self._mcp_url().rstrip("/") + "/health"

    async def _health_ok(self) -> bool:
        """Yunzai 侧是否可达（/mcp/health 免鉴权，开销极小）。"""
        try:
            timeout = aiohttp.ClientTimeout(
                total=self._as_int("probe_timeout", 8, 2, 30)
            )
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(self._health_url()) as response:
                    return response.status == 200
        except Exception:
            return False

    def _our_client(self) -> Any:
        """取本服务对应的 MCPClient。

        优先用官方兼容视图 mcp_client_dict；拿不到就从工具对象上的 mcp_client
        反查，避免依赖某个版本的内部字典结构。
        """
        entry = self._runtime_entry()
        if entry is not None:
            client = getattr(entry, "client", None)
            if client is not None:
                return client
        try:
            mgr = self.context.get_llm_tool_manager()
        except Exception:
            return None
        try:
            clients = getattr(mgr, "mcp_client_dict", None)
            client = clients.get(self.server_name) if clients else None
            if client is not None:
                return client
        except Exception:
            pass
        for item in getattr(mgr, "func_list", None) or []:
            name = str(getattr(item, "name", "") or "")
            if not name.startswith(self.tool_prefix):
                continue
            wrapped = self._unwrap_tool(item)
            owner = getattr(wrapped, "mcp_server_name", None)
            if owner not in (None, self.server_name):
                continue
            client = getattr(wrapped, "mcp_client", None)
            if client is not None:
                return client
        return None

    async def _probe(self, client: Any) -> bool:
        """用一次轻量 MCP 请求判断会话是否还活着。

        断线时这里会收到 McpError("Session terminated") / 404 Session not found /
        ClosedResourceError，全部按“不可用”处理。顺带刷新服务端会话的 lastSeen，
        避免 Yunzai 侧 30 分钟会话 TTL 把空闲会话清掉。
        """
        session = getattr(client, "session", None)
        if session is None:
            return False
        try:
            await asyncio.wait_for(
                session.list_tools(),
                timeout=self._as_int("probe_timeout", 8, 2, 30),
            )
            return True
        except Exception as exc:
            self.logger.debug(f"MCP 会话探针失败: {exc}")
            return False

    async def _reconnect_client(self, client: Any) -> bool:
        """原地重连：保留已注册的工具对象，只换底层 session。"""
        reconnect = getattr(client, "_reconnect", None)
        if reconnect is None:
            return False
        try:
            await reconnect()
        except Exception as exc:
            self.logger.warning(f"MCP 原地重连失败: {exc}")
            return False
        return await self._probe(client)

    def _mark_ok(self) -> None:
        self._last_error = None
        self._last_success = time.time()
        self._tools_cache = None

    _RESYNC_INTERVAL = 300.0

    def _resync_if_due(self) -> None:
        """没有运行时连接时，定期重跑一次完整同步（带 5 分钟节流）。

        Token 是后续补上的、或某次 enable 失败后，运行时会一直“无连接”，
        只靠 `_heal` 的原地重连救不回来（它需要已存在的 client）。
        没有这个兜底就只能重启 AstrBot。
        """
        if self._sync_task and not self._sync_task.done():
            return
        now = time.time()
        if now - self._last_resync < self._RESYNC_INTERVAL:
            return
        self._last_resync = now
        self.logger.info("MCP 仍未连接，重新触发一次同步")
        self._sync_task = asyncio.create_task(
            self._background_sync(), name="catstail-yunzai-sync"
        )

    async def _heal(self, reason: str, force: bool = False) -> bool:
        """修复 MCP 连接。

        先尝试 client._reconnect()（便宜、不重注册工具），失败再退回
        disable+enable 的官方路径。带冷却，避免连续失败时反复重连把 AstrBot 卡住。
        """
        if not bool(self._get("auto_heal", True)):
            return False
        async with self._heal_lock:
            now = time.time()
            cooldown = self._as_int("heal_cooldown", 10, 1, 300)
            if not force and now - self._last_heal < cooldown:
                return False
            self._last_heal = now
            self.logger.info(f"MCP 连接自愈开始: {reason}")

            client = self._our_client()
            if client is not None and await self._reconnect_client(client):
                self._mark_ok()
                self.logger.info(f"MCP 连接自愈成功（原地重连）: {reason}")
                return True

            try:
                await self._apply(force=True)
            except Exception as exc:
                self._last_error = str(exc)
                self.logger.warning(f"MCP 自愈失败（重注册工具）: {exc}")
                return False

            client = self._our_client()
            if client is not None and await self._probe(client):
                self._mark_ok()
                self.logger.info(f"MCP 连接自愈成功（重注册工具）: {reason}")
                return True

            self.logger.warning(f"MCP 自愈未生效: {reason}")
            return False

    async def _ensure_alive(self) -> None:
        """调用工具前确认会话可用；探针有 TTL，正常路径几乎零开销。"""
        now = time.time()
        if now - self._last_probe < self._as_int("probe_interval", 15, 1, 300):
            return
        self._last_probe = now
        client = self._our_client()
        if client is None:
            # 没有运行时连接（Token 刚补上 / 上次 enable 失败），触发一次带节流的重建
            self._resync_if_due()
            return
        if await self._probe(client):
            return
        await self._heal("调用前探针失败")

    async def _watch_once(self) -> None:
        if not self.enabled or not bool(self._get("auto_heal", True)):
            return
        if self._sync_task and not self._sync_task.done():
            return
        if not await self._health_ok():
            return
        client = self._our_client()
        if client is None:
            self._resync_if_due()
            return
        if not await self._probe(client):
            await self._heal("后台巡检探针失败")

    async def _watch_loop(self) -> None:
        interval = self._as_int("health_interval", 60, 15, 600)
        while True:
            await asyncio.sleep(interval)
            try:
                await self._watch_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.debug(f"MCP 健康巡检异常: {exc}")

    # ------------------------------------------------------------------ hooks

    def _start_background(self) -> None:
        """启动后台同步与巡检；幂等，热重载后可安全重复调用。"""
        if not (self._watch_task and not self._watch_task.done()):
            self._watch_task = asyncio.create_task(
                self._watch_loop(), name="catstail-yunzai-health"
            )
        if self._sync_task and not self._sync_task.done():
            return
        self._sync_task = asyncio.create_task(
            self._background_sync(), name="catstail-yunzai-sync"
        )

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        if not self.enabled:
            self.logger.info("桥接已禁用")
            return
        await self._restore_manifest()
        if bool(self._get("auto_register", True)) and not self._effective_token():
            # 放在启动日志第一屏，避免用户只看到“MCP 没自动注册”却不知原因
            self.logger.error(
                "MCP Token 未配置，无法自动注册 MCP 服务：请在插件配置中填写 Yunzai 的 Token，"
                "或为容器设置环境变量 CATSTAIL_MCP_TOKEN 后重启"
            )
        self._start_background()

    @_on_plugin_loaded()
    async def on_plugin_loaded(self, metadata: Any = None) -> None:
        """插件重载后重新同步一次。

        `on_astrbot_loaded` 只在 AstrBot 进程启动时派发一次；WebUI 改完 Token
        触发的插件重载不会跑同步，`data/mcp_server.json` 就会一直停留在旧 Token，
        表现为「配置改了、插件也重载了，发出去的还是旧值」。
        """
        if not self.enabled:
            return
        if not self._is_self_metadata(metadata):
            return
        await self._restore_manifest()
        self._start_background()

    @staticmethod
    def _is_self_metadata(metadata: Any) -> bool:
        """on_plugin_loaded 是全局派发的，只处理本插件自己的加载。

        `root_dir_name` 比 `metadata.name` 更可靠：AstrBot 用目录名定位
        `<dir>_config.json`，两者不一致时插件自身就会出问题。
        """
        if metadata is None:
            return True
        root = str(getattr(metadata, "root_dir_name", "") or "").lower()
        if root:
            return root == PLUGIN_DIR.name.lower()
        name = str(getattr(metadata, "name", "") or "").lower()
        return not name or name == PLUGIN_DIR.name.lower() or "catstail" in name

    @filter.on_using_llm_tool()
    async def guard_connection(
        self,
        event: AstrMessageEvent,
        tool: Any,
        tool_args: dict | None,
    ) -> None:
        """调用前确认 MCP 会话可用。

        AstrBot 只在 anyio.ClosedResourceError 时自动重连（mcp_client.py
        call_tool_with_reconnect），而 Yunzai 会话超时/重启抛的是
        McpError("Session terminated") 或 404 Session not found —— 不会触发重连，
        于是一次失败之后就一直失败，直到重启 AstrBot。这里补上兜底。
        """
        if not bool(self._get("auto_heal", True)):
            return
        name = str(getattr(tool, "name", "") or "")
        if not name.startswith(self.tool_prefix):
            return
        owned = self._mcp_tools()
        if owned and name not in owned:
            return
        if self._sync_task and not self._sync_task.done():
            return
        try:
            await asyncio.wait_for(
                self._ensure_alive(),
                timeout=self._as_int("probe_timeout", 8, 2, 30) + 10,
            )
        except Exception as exc:
            self.logger.debug(f"连接自愈跳过: {exc}")

    @filter.on_llm_request()
    async def inject_rules(self, event: AstrMessageEvent, req: Any) -> None:
        if not bool(self._get("inject_prompt", True)):
            return
        owned = self._mcp_tools()
        if not owned:
            return
        prompt = getattr(req, "system_prompt", "") or ""
        if PROMPT_MARKER in prompt:
            return
        extra = SYSTEM_RULES
        if any(is_lore_tool(name) for name in owned):
            extra += "\n\n" + LORE_RULES
        tools = self._manifest_tool_names()
        if tools:
            extra += "\n当前可用工具：" + self._tool_summary(tools) + "。"
        try:
            req.system_prompt = (prompt + "\n\n" + extra).strip()
        except Exception:
            self.logger.debug("追加系统提示失败", exc_info=True)

    @staticmethod
    def _tool_summary(names: list[str]) -> str:
        """把 yz_<game>_<action> 折叠成 `yz_gs_*: a/b/c`，控制提示词长度。"""
        groups: dict[str, list[str]] = {}
        plain: list[str] = []
        for name in names:
            head, _, rest = name.partition("_")
            game, _, action = rest.partition("_")
            if head == "yz" and action and game in GAME_SEGMENTS:
                groups.setdefault(game, []).append(action)
            else:
                plain.append(name)
        summary = [f"yz_{game}_*: {'/'.join(actions)}" for game, actions in groups.items()]
        if plain:
            summary.append("/".join(plain))
        return "；".join(summary)

    def _manifest_tool_names(self) -> list[str]:
        if not self._manifest:
            return []
        tools = self._manifest.get("tools") or []
        return [
            str(item.get("name"))
            for item in tools
            if isinstance(item, dict) and item.get("name")
        ]

    @filter.on_using_llm_tool()
    async def inject_identity(
        self,
        event: AstrMessageEvent,
        tool: Any,
        tool_args: dict | None,
    ) -> None:
        """把模型无法得知的真实上下文注入工具参数。

        qq 用于确认查询对象与操作发起人，group_id/message_id 供群互动工具使用。
        只注入工具声明过的参数，避免给严格校验参数的服务端塞多余字段。
        """
        if not bool(self._get("inject_identity", True)):
            return
        if tool_args is None or not isinstance(tool_args, dict):
            return
        name = str(getattr(tool, "name", "") or "")
        if not name.startswith(self.tool_prefix):
            return
        # 只处理本服务注册的工具：其他插件也可能有 yz_* 工具，不应给它们塞身份参数
        owned = self._mcp_tools()
        if owned and name not in owned:
            return

        params = self._tool_params(tool)

        def accepts(key: str) -> bool:
            # 拿不到参数定义时退回旧行为：只注入 qq
            return key == "qq" if params is None else key in params

        def put(key: str, value: str, force: bool = False, keep_digits: bool = False) -> bool:
            """写入注入值，返回是否真的写了。

            force=True 时真实上下文优先于模型填的值：qq/message_id 模型不可能知道，
            模型给出的只可能是空串或编造值，必须覆盖，否则会串号或贴到不存在的消息上。
            keep_digits=True 时保留模型给出的合法数字（例如用户明确要求操作别的群），
            只在其缺失或不合法时用真实值覆盖。
            """
            if not value or not value.isdigit() or not accepts(key):
                return False
            if key in tool_args:
                if not force:
                    return False
                existing = str(tool_args.get(key) or "").strip()
                if keep_digits and existing.isdigit():
                    return False
            tool_args[key] = value
            return True

        sender = self._safe_str(getattr(event, "get_sender_id", None))
        group_id = self._safe_str(getattr(event, "get_group_id", None))
        message_id = self._message_id(event)

        injected: list[str] = []
        if put("qq", sender, force=True):
            injected.append("qq")
        elif not sender.isdigit() and accepts("qq"):
            # 非 QQ 平台或取不到发送者时，清掉模型可能编造的 qq
            tool_args.pop("qq", None)
        if put("group_id", group_id, force=True, keep_digits=True):
            injected.append("group_id")
        if put("message_id", message_id, force=True):
            injected.append("message_id")
        elif accepts("message_id"):
            # 拿不到真实消息 ID 时清掉模型的编造值，让服务端返回明确的“缺少 message_id”
            tool_args.pop("message_id", None)

        if injected:
            shown = [("qq=" + sender[:4] + "***") if key == "qq" else key for key in injected]
            self.logger.debug(f"{name}: 注入 {', '.join(shown)}")

    @staticmethod
    def _safe_str(getter: Any) -> str:
        if not callable(getter):
            return ""
        try:
            return str(getter() or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _message_id(event: AstrMessageEvent) -> str:
        """取平台原始消息 ID；部分适配器只给 uuid，则视为不可用。"""
        obj = getattr(event, "message_obj", None)
        raw = getattr(obj, "raw_message", None)
        for source in (raw, obj):
            value = str(getattr(source, "message_id", "") or "").strip()
            if value.isdigit():
                return value
        return ""

    @staticmethod
    def _tool_params(tool: Any) -> set[str] | None:
        """读取工具声明的参数名；无法解析时返回 None。"""
        params = getattr(tool, "parameters", None)
        if isinstance(params, dict):
            props = params.get("properties")
            if isinstance(props, dict):
                return {str(key) for key in props}
            return None
        if isinstance(params, list):
            names = {
                str(item["name"])
                for item in params
                if isinstance(item, dict) and item.get("name")
            }
            return names or None
        return None

    # ------------------------------------------------------------------ commands

    @filter.command_group("yz_mcp")
    def yz_mcp(self):
        """Catstail MCP 桥接管理"""
        pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @yz_mcp.command("status")
    async def yz_mcp_status(self, event: AstrMessageEvent):
        """查看 MCP 桥接状态"""
        yield event.plain_result(self._status_text())

    @filter.permission_type(filter.PermissionType.ADMIN)
    @yz_mcp.command("reload")
    async def yz_mcp_reload(self, event: AstrMessageEvent):
        """重新写入配置并重连 MCP"""
        yield event.plain_result("开始同步 MCP 配置…")
        try:
            await self._apply(force=True)
            yield event.plain_result("同步完成\n" + self._status_text())
        except Exception as exc:
            self._last_error = str(exc)
            self.logger.exception("手动重载 MCP 失败")
            yield event.plain_result(f"同步失败: {exc}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @yz_mcp.command("reconnect")
    async def yz_mcp_reconnect(self, event: AstrMessageEvent):
        """立即自愈 MCP 连接（不重写配置）"""
        yield event.plain_result("开始自愈 MCP 连接…")
        ok = await self._heal("手动触发", force=True)
        yield event.plain_result(("自愈成功\n" if ok else "自愈未成功\n") + self._status_text())

    @filter.permission_type(filter.PermissionType.ADMIN)
    @yz_mcp.command("tools")
    async def yz_mcp_tools(self, event: AstrMessageEvent):
        """列出已连接的 yz_* 工具"""
        tools = self._mcp_tools()
        if not tools:
            yield event.plain_result("当前没有已注册的 MCP 工具")
            return
        yield event.plain_result("已注册工具:\n" + "\n".join(f"- {name}" for name in tools))

    # ------------------------------------------------------------------ plugin update

    async def _run_git(self, *args: str, timeout: int = 120) -> tuple[int, str]:
        """在插件目录执行 git。

        容器里插件目录是 Windows bind mount，git 会因属主不一致拒绝操作，
        所以统一加 `-c safe.directory=*`。
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-c",
                "safe.directory=*",
                "-C",
                str(PLUGIN_DIR),
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            return -1, "容器内没有 git，无法自动更新"
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return -1, "git 执行超时"
        return proc.returncode, (out or b"").decode("utf-8", "replace").strip()

    async def _restart_after_update(self) -> None:
        """延迟重启 AstrBot 进程，让新代码生效。"""
        await asyncio.sleep(1)
        try:
            from astrbot.core.process_restart import restart_process
        except Exception as exc:
            self.logger.error(f"无法加载重启模块，请手动重启容器: {exc}")
            return
        try:
            await asyncio.to_thread(restart_process, 2)
        except Exception as exc:
            self.logger.error(f"自动重启失败，请手动执行 docker restart astrbot: {exc}")

    @staticmethod
    def _https_fallback(remote_url: str) -> str:
        """git@github.com:owner/repo.git → https://github.com/owner/repo.git

        容器镜像里通常没有 ssh 客户端，origin 又是 SSH 地址，所以 fetch 失败时用 HTTPS 兜底。
        """
        url = str(remote_url or "").strip()
        if url.startswith("git@"):
            host, _, path = url[len("git@"):].partition(":")
            if host and path:
                return f"https://{host}/{path}"
        if url.startswith("ssh://git@"):
            rest = url[len("ssh://git@"):]
            host, _, path = rest.partition("/")
            if host and path:
                return f"https://{host}/{path}"
        return ""

    @filter.permission_type(filter.PermissionType.ADMIN)
    @yz_mcp.command("update")
    async def yz_mcp_update(self, event: AstrMessageEvent):
        """拉取插件最新代码（默认自动重启生效；加 no-restart 只拉取）"""
        text = str(getattr(event, "message_str", "") or "")
        pull_only = ("no-restart" in text) or ("仅拉取" in text)

        if not (PLUGIN_DIR / ".git").exists():
            yield event.plain_result("插件目录不是 git 仓库，无法自动更新（请在面板里更新）")
            return

        code, branch = await self._run_git("rev-parse", "--abbrev-ref", "HEAD", timeout=20)
        branch = branch.strip() if code == 0 else ""
        if not branch or branch == "HEAD":
            yield event.plain_result(f"读取当前分支失败:\n{branch}")
            return

        yield event.plain_result(f"正在检查 {branch} 分支更新…")

        remote_ref = f"origin/{branch}"
        code, out = await self._run_git("fetch", "--prune", "origin", branch)
        if code != 0:
            # 容器内没有 ssh：把 origin 换成 HTTPS 再试一次
            code_url, remote_url = await self._run_git("remote", "get-url", "origin", timeout=20)
            fallback = self._https_fallback(remote_url) if code_url == 0 else ""
            if not fallback:
                yield event.plain_result(f"git fetch 失败:\n{out[:500]}")
                return
            yield event.plain_result("origin 拉取失败，改用 HTTPS 重试…")
            code, out = await self._run_git("fetch", "--prune", fallback, branch)
            if code != 0:
                yield event.plain_result(f"HTTPS 拉取失败:\n{out[:500]}")
                return
            remote_ref = "FETCH_HEAD"

        code_a, head = await self._run_git("rev-parse", "--short", "HEAD", timeout=20)
        code_b, remote = await self._run_git("rev-parse", "--short", remote_ref, timeout=20)
        if code_a != 0 or code_b != 0:
            yield event.plain_result("读取提交号失败，已中止")
            return
        head = head.strip()
        remote = remote.strip()
        if head == remote:
            yield event.plain_result(f"已是最新版本（{head}）")
            return

        if pull_only:
            yield event.plain_result(
                f"发现新版本 {head} → {remote}，仅检查未应用（no-restart）\n"
                "去掉 no-restart 再执行一次即可更新"
            )
            return

        code, out = await self._run_git("merge", "--ff-only", remote_ref)
        if code != 0:
            yield event.plain_result(
                f"更新失败（本地有改动或分支已分叉）:\n{out[:600]}\n"
                "请在服务器上手动处理后再试"
            )
            return

        yield event.plain_result(f"更新完成 {head} → {remote}\n{out[:600]}")
        yield event.plain_result("3 秒后自动重启 AstrBot 使新代码生效…")
        asyncio.create_task(self._restart_after_update())

    # ------------------------------------------------------------------ status

    def _status_text(self) -> str:
        lines = ["Catstail MCP 桥接"]
        lines.append(f"启用: {'是' if self.enabled else '否'}")
        try:
            lines.append(f"地址: {self._mcp_url()}（来源: {self._base_url_source()}）")
        except Exception as exc:
            lines.append(f"地址: 配置异常（{exc}）")

        token = self._effective_token()
        lines.append(f"Token: {self._mask(token)}（来源: {self._token_source()}）")
        lines.append(self._token_line(token))
        lines.append(f"服务名: {self.server_name}")

        connected = self._connected()
        lines.append(f"连接: {'已连接' if connected else '未连接'}")

        tools = self._mcp_tools()
        lines.append(f"工具: {len(tools)} 个")
        lore_tools = [name for name in tools if is_lore_tool(name)]
        lines.append(
            f"语料: {len(lore_tools)} 个工具"
            if lore_tools
            else "语料: 未注册（Yunzai 侧未导入 data/lore）"
        )

        if self._manifest:
            lines.append(
                f"清单: {len(self._manifest.get('tools') or [])} 个工具"
                f", hash={str(self._manifest.get('toolsHash') or '')[:16]}…"
            )
        lines.append(
            "自愈: {state}（巡检 {interval}s，探针 {probe}s，冷却 {cooldown}s）".format(
                state="开启" if bool(self._get("auto_heal", True)) else "关闭",
                interval=self._as_int("health_interval", 60, 15, 600),
                probe=self._as_int("probe_interval", 15, 1, 300),
                cooldown=self._as_int("heal_cooldown", 10, 1, 300),
            )
        )
        if self._last_success:
            lines.append(
                "上次同步: " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._last_success))
            )
        if self._last_heal:
            lines.append(
                "上次自愈: " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._last_heal))
            )
        if self._last_error:
            lines.append(f"最近错误: {self._last_error}")
        return "\n".join(lines)

    def _token_line(self, token: str) -> str:
        """比对「插件配置里的 Token」与「真正发出去的 Token」。

        模型/用户看到的配置项不一定是 MCP 客户端在用的那个值：真正发出去的是
        data/mcp_server.json 里写入的 Authorization。两者不一致时，Yunzai 侧
        必然 401，而改配置、重装都看不出原因。
        """
        want = self._fingerprint(f"Bearer {token}")
        try:
            disk = self.context.get_llm_tool_manager().load_mcp_config() or {}
            entry = (disk.get("mcpServers") or {}).get(self.server_name) or {}
            actual = self._fingerprint(self._header_auth(entry))
        except Exception:
            actual = "读取失败"
        flag = "" if want == actual else "  ← 不一致，执行 /yz_mcp reload"
        return f"Token 指纹: 配置 {want} / 磁盘 {actual}{flag}"

    @staticmethod
    def _mask(token: str) -> str:
        """只露前 4 位 + 长度：够核对，又不把后 4 位（熵最高的部分之一）带进群聊。"""
        if not token:
            return "未配置"
        if len(token) <= 8:
            return f"****（{len(token)} 位）"
        return f"{token[:4]}****（{len(token)} 位）"

    async def terminate(self) -> None:
        task = self._sync_task
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._sync_task = None
        watch = self._watch_task
        if watch and not watch.done():
            watch.cancel()
            await asyncio.gather(watch, return_exceptions=True)
        self._watch_task = None
