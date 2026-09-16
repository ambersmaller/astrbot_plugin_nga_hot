"""渲染模块离线测试（FR-3.3 / FR-4 / AC-2 / AC-9 / AC-10 / R4.1~R4.5）。"""

from __future__ import annotations

import re
import time

from astrbot_plugin_nga_hot.core.nga_client import NgaThread
from astrbot_plugin_nga_hot.core.render import (
    MAX_MESSAGE_LEN,
    TOO_LONG_MESSAGE,
    render_empty_message,
    render_markdown,
    render_text,
    strip_urls,
    truncate_title,
    validate_markdown_whitelist,
)

NOW = int(time.time())


def _t(subject: str, replies: int, postdate: int, tid: int = 0) -> NgaThread:
    return NgaThread(tid=tid, subject=subject, postdate=postdate, replies=replies)


def _posts() -> list[NgaThread]:
    return [
        _t("这赛季集合石大米高层尝试了查分组人", 45, NOW - 300),
        _t("他来了他来了，他带着三个球来了", 17, NOW - 900),
        _t("冷门讨论", 3, NOW - 7200),
    ]


class TestStripUrls:
    def test_http(self):
        assert strip_urls("看 https://a.com/x 这里") == "看 [链接] 这里"

    def test_https(self):
        assert strip_urls("https://b.example.com/path?p=1。") == "[链接]。"

    def test_www(self):
        assert strip_urls("访问 www.foo.com/bar 即可") == "访问 [链接] 即可"

    def test_no_url_untouched(self):
        s = "普通标题：魔兽世界！"
        assert strip_urls(s) == s


class TestTruncate:
    def test_truncates_with_ellipsis(self):
        out = truncate_title("一二三四五六七八九十", 6)
        assert out.endswith("…") and len(out) <= 6

    def test_short_untouched(self):
        assert truncate_title("短标题", 40) == "短标题"

    def test_zero_means_no_limit(self):
        s = "很" * 100
        assert truncate_title(s, 0) == s

    def test_newlines_flattened(self):
        assert "\n" not in truncate_title("a\nb\rc", 40)


class TestMarkdownRender:
    def test_template_structure(self):
        out = render_markdown("艾泽拉斯议事厅", _posts(), 24, 5, 40, now=NOW)
        assert out.within_limit
        text = out.text
        assert text.startswith("## 🔥 艾泽拉斯议事厅 · 热帖榜")
        assert "> 最近 24 小时 · 评论数 Top 5" in text
        lines = text.split("\n")
        item_lines = [l for l in lines if re.match(r"^\d+\. ", l)]
        assert len(item_lines) == 3
        assert (
            "**这赛季集合石大米高层尝试了查分组人**" in item_lines[0]
        )  # 40 字内不截断
        assert "💬 45 ·" in item_lines[0]
        assert re.search(r"\d{2}-\d{2} \d{2}:\d{2}", item_lines[0])
        assert text.rstrip().endswith("*")
        assert "*数据来自 NGA · 更新于" in text

    def test_filter_desc_variants(self):
        posts = _posts()
        assert "全部时间" in render_markdown("x", posts, 0, 5, now=NOW).text
        assert "全部帖子" in render_markdown("x", posts, 24, 0, now=NOW).text

    def test_whitelist_clean_for_template(self):
        """AC-13：正常渲染结果仅含白名单语法。"""
        out = render_markdown("版面", _posts(), 24, 5, now=NOW)
        assert validate_markdown_whitelist(out.text) == []

    def test_titles_with_urls_stripped(self):
        """AC-10：标题自带网址 → [链接]，消息无 URL。"""
        posts = [_t("攻略 https://a.com/x 与 www.b.com/y  end", 9, NOW)]
        out = render_markdown("版面", posts, 24, 5, now=NOW)
        assert "http" not in out.text
        assert "www." not in out.text
        assert "[链接]" in out.text
        assert "end" in out.text  # URL 后文本保留

    def test_long_title_truncated(self):
        posts = [_t("很" * 80, 9, NOW)]
        out = render_markdown("版面", posts, 24, 5, 40, now=NOW)
        assert "…" in out.text

    def test_length_limit_fallback(self):
        """R4.3：超长先压缩元信息行，仍超长则提示调小 top_n。"""
        posts = [_t("标题" * 30, i, NOW - i) for i in range(60)]
        out = render_markdown("版面", posts, 24, 0, 40, now=NOW)
        # 60 条 × ~50 字符必然超 2000
        assert not out.within_limit
        assert out.text == TOO_LONG_MESSAGE
        assert len(out.text) <= MAX_MESSAGE_LEN

    def test_length_compression_removes_meta_line(self):
        """R4.3 压缩档：先压缩元信息行（块引用），仍超限时才提示调小 top_n。"""
        from astrbot_plugin_nga_hot.core.render import _build_markdown

        rescued = None
        for n in range(20, 80):
            posts = [_t(f"{i:03d}" + "热" * 29, 1000 - i, NOW - i) for i in range(n)]
            full = _build_markdown("版面", posts, 24, 0, 40, NOW, False)
            comp = _build_markdown("版面", posts, 24, 0, 40, NOW, True)
            if len(full) > MAX_MESSAGE_LEN and len(comp) <= MAX_MESSAGE_LEN:
                rescued = posts
                break
        assert rescued is not None, "测试构造失败：未找到触发压缩档的数据规模"
        out = render_markdown("版面", rescued, 24, 0, 40, now=NOW)
        assert out.within_limit
        assert "> " not in out.text  # 元信息行已被压缩
        assert validate_markdown_whitelist(out.text) == []

    def test_never_exceeds_limit(self):
        posts = [_t("很" * 100 + str(i), i, NOW - i) for i in range(100)]
        for mode in ("md", "text"):
            if mode == "md":
                out = render_markdown("版面", posts, 0, 0, 0, now=NOW)
            else:
                out = render_text("版面", posts, 0, 0, 0, now=NOW)
            assert len(out.text) <= MAX_MESSAGE_LEN


class TestWhitelistValidation:
    def test_rejects_table(self):
        assert validate_markdown_whitelist("1. **a**\n| x | y |") != []

    def test_rejects_code_fence(self):
        assert validate_markdown_whitelist("1. **a**\n```\ncode\n```") != []

    def test_rejects_link_syntax(self):
        assert validate_markdown_whitelist("1. [x](http://a)") != []

    def test_rejects_h1_and_h3(self):
        assert validate_markdown_whitelist("# 一级标题") != []
        assert validate_markdown_whitelist("### 三级标题") != []

    def test_rejects_html(self):
        assert validate_markdown_whitelist("1. <b>粗</b>") != []

    def test_rejects_image(self):
        assert validate_markdown_whitelist("1. ![img](x)") != []

    def test_rejects_url_text(self):
        assert validate_markdown_whitelist("1. 看看 www.a.com") != []

    def test_rejects_unclosed_bold(self):
        assert validate_markdown_whitelist("1. **未闭合") != []

    def test_rejects_non_whitelist_line(self):
        assert validate_markdown_whitelist("普通一行") != []

    def test_accepts_canonical_template(self):
        text = (
            "## 🔥 版面 · 热帖榜\n\n"
            "> 最近 24 小时 · 评论数 Top 5\n\n"
            "1. **标题**　💬 45 · 09-16 14:00\n"
            "2. **标题二**　💬 17 · 09-16 13:40\n\n"
            "*数据来自 NGA · 更新于 09-16 14:05*"
        )
        assert validate_markdown_whitelist(text) == []

    def test_accepts_link_placeholder_in_title(self):
        """R4.4 占位符「[链接]」是允许出现的（非链接语法）。"""
        text = "1. **攻略[链接]已更新**　💬 1 · 09-16 14:00"
        assert validate_markdown_whitelist(text) == []


class TestTextRender:
    def test_no_markdown_symbols(self):
        """AC-9：纯文本无 Markdown 符号。"""
        out = render_text("版面", _posts(), 24, 5, now=NOW)
        text = out.text
        assert "**" not in text
        assert "##" not in text
        assert "> " not in text
        assert not re.search(r"^\*[^*]+\*$", text, re.MULTILINE)
        assert "1. " in text  # 纯文本序号保留
        assert "💬" in text

    def test_text_also_strips_urls(self):
        posts = [_t("看 https://a.com/x", 1, NOW)]
        out = render_text("版面", posts, 24, 5, now=NOW)
        assert "http" not in out.text
        assert "[链接]" in out.text

    def test_text_length_control(self):
        posts = [_t("标题" * 30, i, NOW - i) for i in range(60)]
        out = render_text("版面", posts, 24, 0, 40, now=NOW)
        assert not out.within_limit
        assert out.text == TOO_LONG_MESSAGE


class TestEmptyMessage:
    def test_hours_positive(self):
        msg = render_empty_message(3)
        assert "最近 3 小时内暂无可展示的热帖" in msg

    def test_hours_zero(self):
        msg = render_empty_message(0)
        assert "暂无可展示" in msg
