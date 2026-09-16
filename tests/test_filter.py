"""热帖过滤模块离线测试（FR-3 / AC-3 / AC-7）。"""

from __future__ import annotations

import time

from astrbot_plugin_nga_hot.core.filter import EMPTY_RESULT_MESSAGE, apply_filter
from astrbot_plugin_nga_hot.core.nga_client import NgaThread


def _t(subject: str, replies: int, postdate: int, tid: int = 0) -> NgaThread:
    return NgaThread(tid=tid, subject=subject, postdate=postdate, replies=replies)


NOW = int(time.time())  # 测试中用相对时间，避免过期


class TestTimeFilter:
    def setup_method(self):
        self.now = NOW

    def test_hours_window_keeps_recent(self):
        threads = [
            _t("new", 1, self.now - 3600),
            _t("old", 999, self.now - 48 * 3600),
        ]
        result = apply_filter(threads, hours=24, top_n=0, now=self.now)
        assert [t.subject for t in result] == ["new"]

    def test_hours_zero_skips_time_filter(self):
        threads = [_t("old", 1, self.now - 100 * 24 * 3600)]
        result = apply_filter(threads, hours=0, top_n=0, now=self.now)
        assert len(result) == 1

    def test_boundary_inclusive(self):
        threads = [_t("edge", 1, self.now - 24 * 3600)]
        result = apply_filter(threads, hours=24, top_n=0, now=self.now)
        assert len(result) == 1


class TestTopN:
    def test_replies_desc_and_top_n(self):
        threads = [
            _t("a", 5, NOW),
            _t("b", 50, NOW),
            _t("c", 30, NOW),
            _t("d", 10, NOW),
        ]
        result = apply_filter(threads, hours=0, top_n=2, now=NOW)
        assert [t.subject for t in result] == ["b", "c"]

    def test_top_n_zero_keeps_all(self):
        threads = [_t(str(i), i, NOW, tid=i) for i in range(10)]
        result = apply_filter(threads, hours=0, top_n=0, now=NOW)
        assert len(result) == 10

    def test_tie_break_by_postdate_new_first(self):
        """AC-7：评论数相同时按 postdate 新→旧稳定排序。"""
        threads = [_t("older", 42, NOW - 3600), _t("newer", 42, NOW)]
        result = apply_filter(threads, hours=0, top_n=0, now=NOW)
        assert [t.subject for t in result] == ["newer", "older"]
        # 与输入顺序无关，结果稳定
        result2 = apply_filter(list(reversed(threads)), hours=0, top_n=0, now=NOW)
        assert [t.subject for t in result2] == ["newer", "older"]

    def test_empty_result(self):
        result = apply_filter([], hours=24, top_n=5, now=NOW)
        assert result == []


class TestEmptyMessage:
    def test_message_mentions_hours(self):
        assert "3 小时" in EMPTY_RESULT_MESSAGE.format(hours=3)
