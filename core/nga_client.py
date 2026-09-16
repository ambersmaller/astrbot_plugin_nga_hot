"""NGA 版面数据抓取与解析。

职责：
- 从 board_url 解析 fid；
- 使用 aiohttp 异步请求版面首页（``lite=js&noprefix``，GBK 编码）；
- 解析 ``data.__T``（对象「序号 → 帖子」或数组，两种结构均兼容，见 RA-6）；
- 仅提取 tid / subject / postdate / replies 四项。

本模块不依赖 astrbot，可独立单测。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp

__all__ = [
    "NgaAuthError",
    "NgaClient",
    "NgaConfigError",
    "NgaNetworkError",
    "NgaParseError",
    "NgaThread",
    "parse_fid",
]

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


class NgaConfigError(Exception):
    """版面地址等配置错误（如无法解析出 fid）。"""


class NgaAuthError(Exception):
    """NGA 登录态失效（返回登录页或未携带 __T 数据）。"""


class NgaNetworkError(Exception):
    """网络超时或连接失败。"""


class NgaParseError(Exception):
    """响应解析失败（非预期结构）。"""


@dataclass(frozen=True)
class NgaThread:
    """版面首页的一条帖子（仅保留所需字段）。"""

    tid: int
    subject: str
    postdate: int
    replies: int

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> NgaThread | None:
        """从 __T 条目构造；字段缺失或类型不合法时返回 None（跳过该条）。"""
        try:
            tid = int(raw["tid"])
            subject = str(raw["subject"])
            postdate = int(raw["postdate"])
            replies = int(raw["replies"])
        except (KeyError, TypeError, ValueError):
            return None
        return cls(tid=tid, subject=subject, postdate=postdate, replies=replies)


_FID_RE = re.compile(r"[?&]fid=(\d+)")


def parse_fid(board_url: str) -> int:
    """从版面地址解析 fid。

    支持 ``https://bbs.nga.cn/thread.php?fid=7`` 等形态。
    解析失败抛出 :class:`NgaConfigError`。
    """
    if not board_url or not isinstance(board_url, str):
        raise NgaConfigError("版面地址为空")
    match = _FID_RE.search(board_url)
    if not match:
        # 兜底：query 里带 fid 但形态异常（如 &amp; 转义、全角字符）也尝试一次
        try:
            qs = parse_qs(urlparse(board_url.replace("&amp;", "&")).query)
            fid = int(qs["fid"][0])
        except (KeyError, ValueError, IndexError):
            raise NgaConfigError(
                f"无法从版面地址解析 fid: {board_url!r}（应为 thread.php?fid=…）"
            ) from None
        return fid
    return int(match.group(1))


def build_api_url(board_url: str) -> str:
    """构造 NGA lite=js 接口地址。"""
    if "lite=js" in board_url:
        return board_url
    if "?" not in board_url:
        return f"{board_url}?lite=js&noprefix"
    sep = "" if board_url.endswith(("?", "&")) else "&"
    return f"{board_url}{sep}lite=js&noprefix"


def parse_board_payload(payload: Any) -> list[NgaThread]:
    """解析 NGA 响应 JSON，提取帖子列表。

    - ``__T`` 为对象（序号 → 帖子）或数组均可（R2.6 / RA-6）；
    - 仅处理版面首页第一页（R2.7）；
    - ``data`` 或 ``__T`` 缺失视为登录态失效（NgaAuthError）。
    """
    if not isinstance(payload, dict):
        raise NgaParseError(f"响应不是 JSON 对象: {type(payload).__name__}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise NgaAuthError("响应缺少 data 字段（可能为登录页或登录态失效）")
    threads_raw = data.get("__T")
    if threads_raw is None:
        raise NgaAuthError("响应缺少 data.__T 字段（登录态可能失效）")

    if isinstance(threads_raw, dict):
        entries = threads_raw.values()
    elif isinstance(threads_raw, list):
        entries = threads_raw
    else:
        raise NgaParseError(f"__T 结构无法识别: {type(threads_raw).__name__}")

    threads: list[NgaThread] = []
    for entry in entries:
        if isinstance(entry, dict):
            t = NgaThread.from_raw(entry)
            if t is not None:
                threads.append(t)
    return threads


def decode_body(body: bytes) -> str:
    """按 GBK 解码响应体（容错，见 RA-8）。"""
    return body.decode("gbk", errors="replace")


class NgaClient:
    """NGA 版面客户端（aiohttp，异步）。

    Cookie 仅用于请求头，绝不写入日志或消息（R1.3）。
    """

    def __init__(self, uid: str, cid: str, timeout: int = 10) -> None:
        self._uid = (uid or "").strip()
        self._cid = (cid or "").strip()
        self._timeout = max(1, int(timeout or 10))

    @property
    def has_cookie(self) -> bool:
        return bool(self._uid and self._cid)

    async def close(self) -> None:
        """本客户端按请求创建/释放会话，无需额外清理；保留接口以兼容生命周期调用。"""

    async def fetch_board(self, board_url: str) -> list[NgaThread]:
        """抓取指定版面首页帖子列表。

        异常：
        - NgaConfigError：地址无法解析 fid；
        - NgaAuthError：登录态失效；
        - NgaNetworkError：网络超时/连接失败；
        - NgaParseError：响应结构异常。
        """
        parse_fid(board_url)  # 校验地址合法性（fid 正确性），请求仍使用完整 board_url
        url = build_api_url(board_url)
        headers = {
            "User-Agent": _UA,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://bbs.nga.cn/",
        }
        if self.has_cookie:
            headers["Cookie"] = (
                f"ngaPassportUid={self._uid}; ngaPassportCid={self._cid}"
            )

        timeout = aiohttp.ClientTimeout(total=self._timeout)
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.get(url, headers=headers) as resp,
            ):
                if resp.status >= 400:
                    raise NgaNetworkError(f"NGA HTTP {resp.status}")
                body = await resp.read()
        except NgaNetworkError:
            raise
        except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
            raise NgaNetworkError(f"请求 NGA 失败: {e!s}") from e

        text = decode_body(body)
        stripped = text.lstrip()
        # Cookie 失效时 NGA 返回登录页 HTML（FR-6：返回登录页/无 __T 数据）
        if stripped.startswith("<"):
            raise NgaAuthError(
                f"NGA 返回登录页或空数据（响应特征: {stripped[:80]!r}…）"
            )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as e:
            # 返回了 HTML 登录页但前面有空白/BOM 等情况
            if "<" in text[:200]:
                raise NgaAuthError(
                    f"NGA 返回登录页（响应特征: {stripped[:80]!r}…）"
                ) from e
            raise NgaParseError(f"GBK 解码后仍非 JSON（{e!s}）") from e
        return parse_board_payload(payload)
