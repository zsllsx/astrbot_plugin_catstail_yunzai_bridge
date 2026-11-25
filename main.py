from __future__ import annotations

import asyncio
import copy
import time
from typing import Any

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

PROMPT_MARKER = "【catstail 游戏数据规则】"

SYSTEM_RULES = """【catstail 游戏数据规则】
1. 涉及原神/星铁的角色、武器、面板、圣遗物、练度、活动时，必须先调用 yz_* 工具，不得凭记忆回答。
2. 查询本人不要填写 qq 或 uid：qq 由桥接插件自动注入；用户明确给出他人 UID 时才填写 uid。
3. 工具返回 ok=false 时，按 error.hint 告知用户；没有数据就说没有，禁止推测或补全数值。
4. 回答面板时带上数据时间；stale=true 时提醒用户可先执行 #更新面板。
5. 未接入的游戏（如星铁/绝区零）直接说明暂不支持，不要假装查询。
6. 工具返回的数值是唯一事实来源；配队/词条建议必须基于这些数值。
7. 只有用户明确要求"戳一戳/贴表情/发表情包"时才调用群互动工具；一次调用只做一次，被冷却拒绝时如实告知还要等多久，不要连续重试。
8. 贴表情只能使用系统注入的 message_id，禁止编造；没有该参数就请用户重新发一条消息。"""


class FatalSyncError(Exception):
    """配置或鉴权错误，重试也不会成功。"""


class CatstailYunzaiBridge(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._sync_task: asyncio.Task[None] | None = None
        self._sync_lock = asyncio.Lock()
        self._last_error: str | None = None
        self._last_success: float | None = None
        self._manifest: dict[str, Any] | None = None

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
        base = str(self._get("base_url", "")).strip().rstrip("/")
        if not base:
            raise FatalSyncError("未配置 base_url")
        if not base.startswith(("http://", "https://")):
            raise FatalSyncError("base_url 必须以 http:// 或 https:// 开头")
        return base

    def _mcp_url(self) -> str:
        path = str(self._get("mcp_path", "/mcp")).strip() or "/mcp"
        if not path.startswith("/"):
            path = "/" + path
        return self._base_url() + path

    def _manifest_url(self) -> str:
        return self._mcp_url().rstrip("/") + "/manifest"

    def _token(self) -> str:
        token = str(self._get("token", "")).strip()
        if not token:
            raise FatalSyncError("未配置 MCP Token，请填写 Yunzai 插件生成的 Token")
        return token

    def _server_config(self) -> dict[str, Any]:
        return {
            "transport": "streamable_http",
            "url": self._mcp_url(),
            "headers": {"Authorization": f"Bearer {self._token()}"},
            "timeout": self._as_int("connect_timeout", 30, 3, 120),
            "sse_read_timeout": self._as_int("sse_read_timeout", 300, 30, 1800),
            "active": True,
        }

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
                    "time": manifest.get("time"),
                },
            )
        except Exception as exc:
            self.logger.debug(f"写入 manifest 缓存失败: {exc}")

    # ------------------------------------------------------------------ mcp sync

    async def _apply(self, force: bool = False) -> None:
        if not self.enabled:
            return

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
            current = tool_mgr.load_mcp_config()
            if not isinstance(current, dict):
                current = {}
            current = copy.deepcopy(current)
            servers = current.setdefault("mcpServers", {})
            if not isinstance(servers, dict):
                servers = {}
                current["mcpServers"] = servers

            existing = servers.get(name)
            same = existing == server_cfg
            runtime_view = getattr(tool_mgr, "mcp_server_runtime_view", {})
            connected = name in runtime_view

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

            if connected:
                try:
                    await tool_mgr.disable_mcp_server(name, timeout=10)
                except Exception as exc:
                    raise RuntimeError(f"关闭旧 MCP 连接失败: {exc}") from exc

            timeout = self._as_int("enable_timeout", 30, 3, 120)
            try:
                await tool_mgr.enable_mcp_server(name, server_cfg, timeout=timeout)
            except Exception as exc:
                raise RuntimeError(f"启用 MCP 服务失败: {exc}") from exc

            runtime_view = getattr(tool_mgr, "mcp_server_runtime_view", {})
            if name not in runtime_view:
                raise RuntimeError("MCP 服务未出现在运行时列表，可能仍在初始化")

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

    def _mcp_tools(self) -> list[str]:
        """列出属于本 MCP 服务的工具名。

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

    # ------------------------------------------------------------------ hooks

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        if not self.enabled:
            self.logger.info("桥接已禁用")
            return
        if not bool(self._get("auto_register", True)) and not self._get("token", ""):
            return
        if self._sync_task and not self._sync_task.done():
            return
        self._sync_task = asyncio.create_task(
            self._background_sync(), name="catstail-yunzai-sync"
        )

    @filter.on_llm_request()
    async def inject_rules(self, event: AstrMessageEvent, req: Any) -> None:
        if not bool(self._get("inject_prompt", True)):
            return
        if not self._mcp_tools():
            return
        prompt = getattr(req, "system_prompt", "") or ""
        if PROMPT_MARKER in prompt:
            return
        extra = SYSTEM_RULES
        tools = self._manifest_tool_names()
        if tools:
            extra += "\n当前可用工具：" + "、".join(tools) + "。"
        try:
            req.system_prompt = (prompt + "\n\n" + extra).strip()
        except Exception:
            self.logger.debug("追加系统提示失败", exc_info=True)

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

        params = self._tool_params(tool)

        def accepts(key: str) -> bool:
            # 拿不到参数定义时退回旧行为：只注入 qq
            return key == "qq" if params is None else key in params

        def put(key: str, value: str) -> bool:
            if not value or not value.isdigit() or not accepts(key):
                return False
            if key in tool_args:
                return False
            tool_args[key] = value
            return True

        sender = self._safe_str(getattr(event, "get_sender_id", None))
        group_id = self._safe_str(getattr(event, "get_group_id", None))
        message_id = self._message_id(event)

        injected: list[str] = []
        if put("qq", sender):
            injected.append("qq")
        elif not sender.isdigit() and accepts("qq"):
            # 非 QQ 平台或取不到发送者时，清掉模型可能编造的 qq
            tool_args.pop("qq", None)
        if put("group_id", group_id):
            injected.append("group_id")
        if put("message_id", message_id):
            injected.append("message_id")

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
    @yz_mcp.command("tools")
    async def yz_mcp_tools(self, event: AstrMessageEvent):
        """列出已连接的 yz_* 工具"""
        tools = self._mcp_tools()
        if not tools:
            yield event.plain_result("当前没有已注册的 MCP 工具")
            return
        yield event.plain_result("已注册工具:\n" + "\n".join(f"- {name}" for name in tools))

    # ------------------------------------------------------------------ status

    def _status_text(self) -> str:
        lines = ["Catstail MCP 桥接"]
        lines.append(f"启用: {'是' if self.enabled else '否'}")
        try:
            lines.append(f"地址: {self._mcp_url()}")
        except Exception as exc:
            lines.append(f"地址: 配置异常（{exc}）")

        token = str(self._get("token", "")).strip()
        lines.append(f"Token: {self._mask(token)}")
        lines.append(f"服务名: {self.server_name}")

        connected = False
        try:
            connected = self.server_name in self.context.get_llm_tool_manager().mcp_server_runtime_view
        except Exception:
            pass
        lines.append(f"连接: {'已连接' if connected else '未连接'}")

        tools = self._mcp_tools()
        lines.append(f"工具: {len(tools)} 个")

        if self._manifest:
            lines.append(
                f"清单: {len(self._manifest.get('tools') or [])} 个工具"
                f", hash={str(self._manifest.get('toolsHash') or '')[:16]}…"
            )
        if self._last_success:
            lines.append(
                "上次同步: " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._last_success))
            )
        if self._last_error:
            lines.append(f"最近错误: {self._last_error}")
        return "\n".join(lines)

    @staticmethod
    def _mask(token: str) -> str:
        if not token:
            return "未配置"
        if len(token) <= 8:
            return "****"
        return token[:4] + "****" + token[-4:]

    async def terminate(self) -> None:
        task = self._sync_task
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._sync_task = None
