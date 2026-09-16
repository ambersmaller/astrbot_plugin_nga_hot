"""消息渲染：QQ 官方 Markdown / 纯文本（FR-4）。

职责：
- 按 4.4 节白名单渲染 Markdown 榜单（## 标题、> 引用、有序列表、**加粗**、*斜体*）；
- 渲染前清除标题中的 URL 文本（http(s)://…、www.… → 「[链接]」，R4.4 / RA-3）；
- 白名单校验（R4.5）：越界语法交回调用方降级纯文本；
- 长度控制（R4.3）：总长度 ≤2000，先压缩元信息行，仍超限则提示调小 top_n，绝不静默截断；
- 纯文本渲染（send_mode=text，AC-9）：清除全部 Markdown 符号。

本模块不依赖 astrbot，可独立单测。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from .nga_client import NgaThread

__all__ = [
    "TOO_LONG_MESSAGE",
    "RenderOutput",
    "render_empty_message",
    "render_markdown",
    "render_text",
    "strip_urls",
    "truncate_title",
    "validate_markdown_whitelist",
]

_TZ = ZoneInfo("Asia/Shanghai")

# QQ 单条消息通用长度约束（R4.3 / RA-4）
MAX_MESSAGE_LEN = 2000

TOO_LONG_MESSAGE = (
    "榜单内容超过 QQ 单条消息长度限制，请在插件配置中调小 top_n 或缩短标题长度"
)

_URL_RE = re.compile(r"(?:https?://|www\.)[^\s\"'<>\[\]，。；、（）()]+", re.IGNORECASE)
_LINK_PLACEHOLDER = "[链接]"

# 白名单之外、需要整体拒发的危险语法（R4.5 / 4.4）
_FORBIDDEN_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"```"), "代码块"),
    (re.compile(r"`[^`]*`"), "行内代码"),
    (re.compile(r"\|"), "表格"),
    (re.compile(r"!?\[[^\]]*\]\([^)]*\)"), "链接语法"),
    (re.compile(r"<[^>]+>"), "HTML 标签"),
    (re.compile(r"~~[^~]*~~"), "删除线"),
    (re.compile(r"^#{1,1}\s|^#{3,}\s", re.MULTILINE), "非二级标题"),
    (re.compile(r"^>(?! )", re.MULTILINE), "非标准块引用"),
]


@dataclass(frozen=True)
class RenderOutput:
    """渲染结果。

    - ``within_limit=False`` 时 ``text`` 为提示用户调整配置的兜底文案（R4.3）。
    """

    text: str
    within_limit: bool = True
    violations: tuple[str, ...] = field(default_factory=tuple)


def strip_urls(text: str) -> str:
    """将文本中的 URL（http(s)://…、www.…）替换为「[链接]」（R4.4）。"""
    return _URL_RE.sub(_LINK_PLACEHOLDER, text)


def truncate_title(title: str, max_len: int) -> str:
    """标题超长截断并加 …（R4.2）。max_len<=0 时不截断。"""
    title = re.sub(r"\s+", " ", title).strip()
    if max_len > 0 and len(title) > max_len:
        return title[: max_len - 1].rstrip() + "…"
    return title


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=_TZ).strftime("%m-%d %H:%M")


def _filter_desc(hours: int, top_n: int) -> str:
    if hours > 0 and top_n > 0:
        return f"最近 {hours} 小时 · 评论数 Top {top_n}"
    if hours > 0:
        return f"最近 {hours} 小时 · 全部帖子"
    if top_n > 0:
        return f"全部时间 · 评论数 Top {top_n}"
    return "全部帖子"


def _safe_subject(subject: str, max_title_len: int) -> str:
    """标题净化：去 URL、压空白、防换行破坏结构、超长截断。"""
    s = strip_urls(subject)
    s = s.replace("\r", " ").replace("\n", " ").strip()
    return truncate_title(s, max_title_len)


def validate_markdown_whitelist(text: str) -> list[str]:
    """校验消息仅包含 4.4 节白名单语法，返回越界项列表（空 = 通过）。"""
    violations: list[str] = []
    for pattern, name in _FORBIDDEN_PATTERNS:
        if pattern.search(text):
            violations.append(name)
    # 行结构校验：仅允许 二级标题 / 块引用 / 有序列表 / 斜体单行 / 空行
    for line in text.split("\n"):
        if not line.strip():
            continue
        if line.startswith("## "):
            continue
        if line.startswith("> "):
            continue
        if re.match(r"^\d+\. ", line):
            # 列表条目内 ** 必须成对，避免结构破损
            if line.count("**") % 2 != 0:
                violations.append("未闭合的加粗语法")
            continue
        if re.match(r"^\*[^*]+\*$", line):
            continue
        violations.append(f"越界行: {line[:20]!r}")
    # 消息内不得残留任何 URL 文本（R4.4）
    if _URL_RE.search(text):
        violations.append("URL 文本")
    return violations


def _build_markdown(
    board_name: str,
    posts: list[NgaThread],
    hours: int,
    top_n: int,
    max_title_len: int,
    now: float,
    compress_meta: bool,
) -> str:
    title = f"## 🔥 {board_name} · 热帖榜"
    lines = [title, ""]
    if not compress_meta:
        lines += [f"> {_filter_desc(hours, top_n)}", ""]
    for idx, post in enumerate(posts, start=1):
        subject = _safe_subject(post.subject, max_title_len)
        lines.append(
            f"{idx}. **{subject}**　💬 {post.replies} · {_fmt_time(post.postdate)}"
        )
    lines += [
        "",
        f"*数据来自 NGA · 更新于 {_fmt_time(now)}*",
    ]
    return "\n".join(lines)


def _build_text(
    board_name: str,
    posts: list[NgaThread],
    hours: int,
    top_n: int,
    max_title_len: int,
    now: float,
    compress_meta: bool,
) -> str:
    lines = [f"🔥 {board_name} · 热帖榜"]
    if not compress_meta:
        lines.append(_filter_desc(hours, top_n))
    for idx, post in enumerate(posts, start=1):
        subject = _safe_subject(post.subject, max_title_len)
        lines.append(
            f"{idx}. {subject}　💬 {post.replies} · {_fmt_time(post.postdate)}"
        )
    lines.append(f"数据来自 NGA · 更新于 {_fmt_time(now)}")
    return "\n".join(lines)


def render_markdown(
    board_name: str,
    posts: list[NgaThread],
    hours: int,
    top_n: int,
    max_title_len: int = 40,
    now: float | None = None,
) -> RenderOutput:
    """渲染 Markdown 榜单（含长度控制，R4.3）。"""
    if now is None:
        now = time.time()
    text = _build_markdown(board_name, posts, hours, top_n, max_title_len, now, False)
    if len(text) > MAX_MESSAGE_LEN:
        # 先压缩元信息行（过滤说明 + 时间戳说明）
        text = _build_markdown(
            board_name, posts, hours, top_n, max_title_len, now, True
        )
    if len(text) > MAX_MESSAGE_LEN:
        return RenderOutput(TOO_LONG_MESSAGE, within_limit=False)
    return RenderOutput(text)


def render_text(
    board_name: str,
    posts: list[NgaThread],
    hours: int,
    top_n: int,
    max_title_len: int = 40,
    now: float | None = None,
) -> RenderOutput:
    """渲染纯文本榜单（无 Markdown 符号，AC-9），同样受长度控制。"""
    if now is None:
        now = time.time()
    text = _build_text(board_name, posts, hours, top_n, max_title_len, now, False)
    if len(text) > MAX_MESSAGE_LEN:
        text = _build_text(board_name, posts, hours, top_n, max_title_len, now, True)
    if len(text) > MAX_MESSAGE_LEN:
        return RenderOutput(TOO_LONG_MESSAGE, within_limit=False)
    return RenderOutput(text)


def render_empty_message(hours: int) -> str:
    """空结果提示（FR-3.3），不返回空白消息。"""
    if hours > 0:
        return (
            f"最近 {hours} 小时内暂无可展示的热帖，"
            "试试在插件配置中调大 hours 放宽时间窗口"
        )
    return "该版面暂无可展示的帖子"
