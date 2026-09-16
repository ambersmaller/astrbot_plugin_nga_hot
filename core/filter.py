"""热帖过滤：时间窗口 + 评论数 Top N（FR-3）。

执行顺序固定：
1. 时间过滤：保留 postdate >= now - hours×3600；hours=0 跳过；
2. 评论数降序，取前 top_n；top_n=0 保留全部；同值按 postdate 新→旧稳定排序；
3. 空结果由调用方处理（渲染层给出提示文案）。

本模块不依赖 astrbot，可独立单测。
"""

from __future__ import annotations

import time

from .nga_client import NgaThread

__all__ = ["EMPTY_RESULT_MESSAGE", "apply_filter"]


EMPTY_RESULT_MESSAGE = (
    "最近 {hours} 小时内暂无可展示的热帖，试试调大 hours 放宽时间窗口"
)


def apply_filter(
    threads: list[NgaThread],
    hours: int,
    top_n: int,
    now: float | None = None,
) -> list[NgaThread]:
    """按时间窗口与评论数 Top N 过滤。

    参数：
    - threads: 版面首页帖子列表；
    - hours: 「最近 N 小时」，0 表示不过滤；
    - top_n: 评论数 Top N 截取数，0 表示全部；
    - now: 当前 Unix 时间戳（测试可注入）。
    """
    if now is None:
        now = time.time()
    hours = max(0, int(hours or 0))
    top_n = max(0, int(top_n or 0))

    result = list(threads)

    if hours > 0:
        cutoff = now - hours * 3600
        result = [t for t in result if t.postdate >= cutoff]

    # 评论数降序；同值按 postdate 新→旧，保证结果稳定（AC-7）
    result.sort(key=lambda t: (-t.replies, -t.postdate))

    if top_n > 0:
        result = result[:top_n]

    return result
