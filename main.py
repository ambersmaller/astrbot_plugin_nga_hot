"""NGA 热帖推送插件（astrbot_plugin_nga_hot）。

在 QQ 官方机器人群聊中响应自定义指令，抓取指定 NGA 版面首页热帖
（仅标题 / 发布日期 / 评论数），按时间窗口 + 评论数 Top N 过滤后，
以 QQ 官方 Markdown 消息回复。全程无需 LLM 参与。

关键实现点：
- 指令在 initialize() 中按配置动态注册（FR-5），WebUI 新增版面配置并重载
  插件后即可使用；
- Markdown 发送走 P0 路径：经事件对象获取 qqofficial 底层 botpy 客户端，
  直接调用官方消息 API（msg_type=2, markdown.content）；失败按 R4.6 降级；
- 消息内不包含任何 URL（R4.4）；渲染前经白名单校验（R4.5）；
- Cookie 仅用于请求头，绝不写入日志或消息（R1.3）。
"""

from __future__ import annotations

import random
import re
import time
import traceback
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.star_handler import EventType, star_handlers_registry

try:  # 正常 AstrBot 加载路径（包内相对导入）
    from .core.filter import apply_filter
    from .core.nga_client import (
        NgaAuthError,
        NgaClient,
        NgaConfigError,
        NgaNetworkError,
        NgaParseError,
        parse_fid,
    )
    from .core.render import (
        render_empty_message,
        render_markdown,
        render_text,
        validate_markdown_whitelist,
    )
except ImportError:  # 兜底：模块路径直挂时
    from core.filter import apply_filter  # type: ignore[assignment]
    from core.nga_client import (  # type: ignore[assignment]
        NgaAuthError,
        NgaClient,
        NgaConfigError,
        NgaNetworkError,
        NgaParseError,
        parse_fid,
    )
    from core.render import (  # type: ignore[assignment]
        render_empty_message,
        render_markdown,
        render_text,
        validate_markdown_whitelist,
    )

# R1.1：指令名仅允许中文、英文字母、数字、下划线，长度 1–20，不得包含空格
COMMAND_NAME_RE = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9_]{1,20}$")

# 缓存版面上限（4.3）
MAX_CACHE_ENTRIES = 50

# R6.2：同类错误日志去抖窗口（秒）
LOG_DEBOUNCE_SECONDS = 300.0

_SEND_MODE_OPTIONS = ("auto", "markdown", "text")


class NgaHotPlugin(Star):
    """NGA 热帖推送插件主类。"""

    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self.config = config if config is not None else {}
        self._client: NgaClient | None = None
        self._client_key: tuple[str, str, int] | None = None
        # fid -> (timestamp, threads)
        self._cache: dict[str, tuple[float, list]] = {}
        # 指令名 -> 注册时版面配置快照（仅作兜底，触发时仍以最新配置为准）
        self._boards_snapshot: dict[str, dict] = {}
        self._registered_attrs: list[str] = []
        self._debounce: dict[str, float] = {}
        self._handler_seq = 0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """按 boards 配置动态注册指令（FR-5 / R5.1）。"""
        registered = 0
        skipped = 0
        try:
            boards = self.config.get("boards", []) or []
            seen_commands: set[str] = set()
            for board in boards:
                try:
                    ok = self._validate_board(board, seen_commands)
                    if not ok:
                        skipped += 1
                        continue
                    command = str(board["command"]).strip()
                    seen_commands.add(command)
                    self._register_command(command, board)
                    registered += 1
                except Exception:  # noqa: BLE001 - R6.1 单条配置失败不影响整体
                    skipped += 1
                    logger.warning(
                        f"[NGA热帖] 版面配置注册失败，已跳过: {self._safe_board_desc(board)}",
                    )
                    logger.debug(traceback.format_exc())
        except Exception:  # noqa: BLE001 - R6.1
            logger.error("[NGA热帖] initialize 出现异常")
            logger.error(traceback.format_exc())
        logger.info(
            f"[NGA热帖] 已注册 {registered} 条版面指令，跳过 {skipped} 条无效配置"
        )

    async def terminate(self) -> None:
        """插件禁用/重载时清理资源（R5.3）。"""
        self._cache.clear()
        self._debounce.clear()
        self._boards_snapshot.clear()
        for attr in self._registered_attrs:
            try:
                delattr(self, attr)
            except AttributeError:
                pass
        self._registered_attrs.clear()
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:  # noqa: BLE001 - 清理阶段静默失败
                logger.debug(traceback.format_exc())
            self._client = None
            self._client_key = None

    # ------------------------------------------------------------------
    # 指令注册（R5.1 / R1.1 / R1.2）
    # ------------------------------------------------------------------

    def _validate_board(self, board: Any, seen_commands: set[str]) -> bool:
        """加载时自检（FR-5 注册校验表），全部通过才注册。"""
        if not isinstance(board, dict):
            logger.warning("[NGA热帖] 版面配置不是对象，已跳过")
            return False

        command = str(board.get("command", "") or "").strip()
        if not COMMAND_NAME_RE.match(command):
            logger.warning(
                "[NGA热帖] 指令名非法（仅中文/字母/数字/下划线，长度1-20，不含空格）"
                f"，已跳过该条目: {command!r}"
            )
            return False
        if command in seen_commands:
            logger.warning(f"[NGA热帖] 指令名在插件内重复: {command!r}，已跳过后注册者")
            return False
        conflict = self._find_external_command_conflict(command)
        if conflict is not None:
            logger.warning(
                f"[NGA热帖] 指令名 {command!r} 与其他已注册指令冲突"
                f"（{conflict}），已跳过该条目"
            )
            return False

        board_url = str(board.get("board_url", "") or "").strip()
        try:
            parse_fid(board_url)
        except NgaConfigError as e:
            logger.warning(f"[NGA热帖] {e!s}，已跳过该条目（指令 {command!r}）")
            return False
        return True

    def _find_external_command_conflict(self, command: str) -> str | None:
        """与其他插件已注册指令查重（R1.2）。"""
        own_module = __name__
        try:
            handlers = star_handlers_registry.get_handlers_by_event_type(
                EventType.AdapterMessageEvent, only_activated=True
            )
        except Exception:  # noqa: BLE001 - 查重失败不阻断注册
            return None
        for md in handlers:
            if md.handler_module_path == own_module:
                continue
            for f in md.event_filters:
                if isinstance(f, CommandFilter) and (
                    f.command_name == command or command in (f.alias or set())
                ):
                    return md.handler_full_name
        return None

    def _register_command(self, command: str, board: dict) -> None:
        """运行时注入模式动态注册指令（参考 AstrBot issue #2975）。"""
        self._handler_seq += 1
        handler = self._make_handler(command, board)
        # 唯一函数名：避免 registry 中以 module_name 复用旧 handler
        attr = f"_nga_hot_{self._handler_seq}_{command}"
        handler.__name__ = attr
        handler.__qualname__ = f"{type(self).__name__}.{attr}"
        handler.__doc__ = f"拉取{self._board_display_name(board)}热帖榜"
        setattr(self, attr, handler)
        self._registered_attrs.append(attr)
        filter.command(command)(handler)
        self._boards_snapshot[command] = dict(board)
        logger.info(
            f"[NGA热帖] 指令 /{command} 已注册 → {self._board_display_name(board)}"
        )

    def _make_handler(self, command: str, board: dict):
        plugin = self

        async def _nga_hot_handler(event: AstrMessageEvent) -> None:
            await plugin._handle_board_command(event, command)

        return _nga_hot_handler

    # ------------------------------------------------------------------
    # 指令处理
    # ------------------------------------------------------------------

    async def _handle_board_command(
        self, event: AstrMessageEvent, command: str
    ) -> None:
        try:
            board = self._find_board(command)
            if board is None:
                await self._send_text(event, "该指令对应的版面配置已不存在，请重载插件")
                return

            uid = str(self.config.get("nga_uid", "") or "").strip()
            cid = str(self.config.get("nga_cid", "") or "").strip()
            if not uid or not cid:
                self._log_debounced(
                    "cookie_missing", "warning", "[NGA热帖] NGA Cookie 未配置"
                )
                await self._send_text(event, "请先在插件配置中填写 NGA Cookie")
                return

            board_url = str(board.get("board_url", "") or "").strip()
            fid = parse_fid(board_url)  # 兜底校验（R2.2），正常在加载自检已拦截

            client = self._get_client(uid, cid)
            threads = self._cache_get(str(fid))
            if threads is None:
                threads = await client.fetch_board(board_url)
                self._cache_put(str(fid), threads)

            hours = self._coerce_int(board.get("hours", 24), 24)
            top_n = self._coerce_int(board.get("top_n", 5), 5)
            posts = apply_filter(threads, hours, top_n)
            if not posts:
                await self._send_text(event, render_empty_message(hours))
                return

            board_name = self._board_display_name(board)
            max_title_len = self._coerce_int(self.config.get("max_title_len", 40), 40)
            mode = str(self.config.get("send_mode", "auto") or "auto").strip()
            if mode not in _SEND_MODE_OPTIONS:
                mode = "auto"

            if mode == "text":
                out = render_text(board_name, posts, hours, top_n, max_title_len)
                await self._send_text(event, out.text)
                return

            out = render_markdown(board_name, posts, hours, top_n, max_title_len)
            if not out.within_limit:
                await self._send_text(event, out.text)
                return

            violations = validate_markdown_whitelist(out.text)
            if violations:
                # R4.5：越界语法立即降级纯文本发送
                self._log_debounced(
                    "md_whitelist",
                    "warning",
                    f"[NGA热帖] Markdown 越界语法 {violations}，已降级纯文本发送",
                )
                text_out = render_text(board_name, posts, hours, top_n, max_title_len)
                await self._send_text(event, text_out.text)
                return

            ok, err = await self._send_markdown_direct(event, out.text)
            if ok:
                return
            if err and self._is_rate_limited(err):
                self._log_debounced(
                    "rate_limited", "warning", f"[NGA热帖] 发送被频控: {err}"
                )
                await self._send_text(event, "发送过于频繁，请稍后再试")
                return
            if mode == "markdown":
                self._log_debounced(
                    "md_send_fail", "warning", f"[NGA热帖] Markdown 发送失败: {err}"
                )
                await self._send_text(
                    event,
                    "热帖榜发送失败（Markdown 模式），"
                    "可在插件配置中将 send_mode 改为 auto 或 text",
                )
                return
            # auto：Markdown 失败自动降级 text 重发一次（R4.6）
            self._log_debounced(
                "md_degrade",
                "warning",
                f"[NGA热帖] Markdown 发送失败，降级纯文本: {err}",
            )
            text_out = render_text(board_name, posts, hours, top_n, max_title_len)
            if await self._send_text(event, text_out.text):
                return
            self._log_debounced("send_fail", "error", "[NGA热帖] 纯文本降级发送仍失败")
            await self._send_text(event, "热帖榜发送失败，请稍后再试")

        except NgaConfigError as e:
            self._log_debounced(
                "cfg_err", "warning", f"[NGA热帖] 版面地址配置错误: {e!s}"
            )
            await self._send_text(event, "版面地址配置错误，应为 thread.php?fid=…")
        except NgaAuthError as e:
            self._log_debounced(
                "auth_err", "warning", f"[NGA热帖] NGA 登录态失效: {e!s}"
            )
            await self._send_text(event, "NGA 登录态失效，请更新 Cookie")
        except NgaNetworkError as e:
            self._log_debounced(
                "net_err", "error", f"[NGA热帖] 请求 NGA 网络错误: {e!s}"
            )
            await self._send_text(event, "NGA 请求超时/失败，请稍后再试")
        except NgaParseError as e:
            self._log_debounced(
                "parse_err", "error", f"[NGA热帖] NGA 数据解析失败: {e!s}"
            )
            await self._send_text(event, "NGA 数据解析失败，请稍后再试")
        except Exception:  # noqa: BLE001 - R6.1 兜底，任何异常不得崩溃主进程
            logger.error("[NGA热帖] 插件内部错误")
            logger.error(traceback.format_exc())
            await self._send_text(event, "插件内部错误，请查看日志")

    # ------------------------------------------------------------------
    # 发送链路（FR-4）
    # ------------------------------------------------------------------

    async def _send_markdown_direct(
        self, event: AstrMessageEvent, text: str
    ) -> tuple[bool, str | None]:
        """P0：经事件对象获取 qqofficial 底层 botpy 客户端直发 Markdown。

        返回 (是否成功, 错误信息)。底层路径不可用时返回失败，由调用方按
        send_mode 降级（R4.6 / RA-1）。
        """
        bot = getattr(event, "bot", None)
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        group_openid = getattr(raw, "group_openid", None)
        if bot is None or not group_openid:
            return False, "当前环境无法访问 qqofficial 底层客户端"
        try:
            from botpy.types.message import MarkdownPayload

            msg_id = getattr(getattr(event, "message_obj", None), "message_id", None)
            await bot.api.post_group_message(
                group_openid=group_openid,
                msg_type=2,
                markdown=MarkdownPayload(content=text),
                msg_id=msg_id,
                msg_seq=random.randint(1, 10000),
            )
            return True, None
        except Exception as e:  # noqa: BLE001 - 发送异常统一兜底
            return False, str(e)

    async def _send_text(self, event: AstrMessageEvent, text: str) -> bool:
        """发送纯文本消息（适配器通用路径）。"""
        try:
            await event.send(MessageChain().message(text))
            return True
        except Exception as e:  # noqa: BLE001
            self._log_debounced(
                "send_text_fail", "warning", f"[NGA热帖] 纯文本发送失败: {e!s}"
            )
            return False

    @staticmethod
    def _is_rate_limited(err: str) -> bool:
        low = err.lower()
        return (
            "频繁" in err
            or "rate limit" in low
            or "ratelimit" in low
            or "too many requests" in low
            or "11252" in err
        )

    # ------------------------------------------------------------------
    # 配置 / 缓存 / 工具
    # ------------------------------------------------------------------

    def _find_board(self, command: str) -> dict | None:
        """按指令名查找版面配置（触发时读取最新配置，R1.4）。"""
        boards = self.config.get("boards", []) or []
        for board in boards:
            if (
                isinstance(board, dict)
                and str(board.get("command", "") or "").strip() == command
            ):
                return board
        return self._boards_snapshot.get(command)

    @staticmethod
    def _coerce_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _get_client(self, uid: str, cid: str) -> NgaClient:
        timeout = self._coerce_int(self.config.get("request_timeout", 10), 10)
        key = (uid, cid, timeout)
        if self._client is None or self._client_key != key:
            self._client = NgaClient(uid, cid, timeout)
            self._client_key = key
        return self._client

    def _cache_get(self, key: str) -> list | None:
        ttl = self._coerce_int(self.config.get("cache_ttl", 60), 60)
        if ttl <= 0:
            return None
        item = self._cache.get(key)
        if item is None:
            return None
        ts, data = item
        if time.time() - ts > ttl:
            self._cache.pop(key, None)
            return None
        return data

    def _cache_put(self, key: str, data: list) -> None:
        ttl = self._coerce_int(self.config.get("cache_ttl", 60), 60)
        if ttl <= 0:
            return
        while len(self._cache) >= MAX_CACHE_ENTRIES:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = (time.time(), data)

    def _board_display_name(self, board: dict) -> str:
        name = str(board.get("board_name", "") or "").strip()
        if name:
            return name
        try:
            return f"NGA 版面 fid={parse_fid(str(board.get('board_url', '') or ''))}"
        except NgaConfigError:
            return "NGA 版面"

    @staticmethod
    def _safe_board_desc(board: Any) -> str:
        if isinstance(board, dict):
            return f"指令={board.get('command')!r}"
        return repr(board)[:60]

    def _log_debounced(self, key: str, level: str, msg: str) -> None:
        """同类错误 5 分钟内不重复刷日志（R6.2）。"""
        now = time.time()
        last = self._debounce.get(key, 0.0)
        if now - last < LOG_DEBOUNCE_SECONDS:
            return
        self._debounce[key] = now
        log_fn = getattr(logger, level, None) or logger.warning
        log_fn(msg)
