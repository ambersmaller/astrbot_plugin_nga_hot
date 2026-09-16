"""插件主类离线集成测试（FR-1 / FR-4 / FR-5 / FR-6 / 4.3 缓存）。"""

from __future__ import annotations

import asyncio
import copy
import time

from astrbot_plugin_nga_hot.core.nga_client import NgaThread

NOW = int(time.time())


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _t(subject: str, replies: int, postdate: int, tid: int = 0) -> NgaThread:
    return NgaThread(tid=tid, subject=subject, postdate=postdate, replies=replies)


DEFAULT_CONFIG = {
    "boards": [
        {
            "board_url": "https://bbs.nga.cn/thread.php?fid=7",
            "board_name": "艾泽拉斯议事厅",
            "command": "nga",
            "hours": 24,
            "top_n": 5,
        }
    ],
    "nga_uid": "uid",
    "nga_cid": "cid",
    "request_timeout": 10,
    "send_mode": "auto",
    "cache_ttl": 60,
    "max_title_len": 40,
}


def _make_plugin(env, config=None):
    plugin = env.main.NgaHotPlugin(
        context=None, config=config or copy.deepcopy(DEFAULT_CONFIG)
    )
    _run(plugin.initialize())
    return plugin


def _handler_for(env, command: str):
    for name, handler in env.registered_commands:
        if name == command:
            return handler
    return None


class FakeRawMessage:
    group_openid = "GROUP_OPENID_1"


class FakeMessageObj:
    raw_message = FakeRawMessage()
    message_id = "MSG_ID_1"


class FakeBotApi:
    def __init__(self):
        self.calls: list[dict] = []
        self.attempts = 0
        self.fail_with: Exception | None = None

    async def post_group_message(self, **kwargs):
        self.attempts += 1
        if self.fail_with is not None:
            raise self.fail_with
        self.calls.append(kwargs)
        return {"id": "ret_id"}


class FakeBot:
    def __init__(self):
        self.api = FakeBotApi()


class FakeEvent:
    """模拟 qqofficial 事件对象（含底层 botpy 客户端）。"""

    def __init__(self):
        self.bot = FakeBot()
        self.message_obj = FakeMessageObj()
        self.sent: list[str] = []

    async def send(self, chain) -> None:
        text = "".join(getattr(c, "text", "") for c in chain.chain)
        self.sent.append(text)


class FakeClient:
    """模拟 NgaClient，返回固定数据并计数。"""

    def __init__(self, posts=None, error: Exception | None = None):
        self.posts = (
            posts
            if posts is not None
            else [
                _t("热帖一", 45, NOW - 60),
                _t("热帖二", 17, NOW - 120),
            ]
        )
        self.error = error
        self.calls = 0

    async def fetch_board(self, board_url):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.posts)

    async def close(self):
        pass


class TestDynamicRegistration:
    def test_valid_boards_registered(self, astrbot_env):
        config = dict(DEFAULT_CONFIG)
        config["boards"] = [
            {
                "board_url": "https://bbs.nga.cn/thread.php?fid=7",
                "board_name": "艾泽拉斯议事厅",
                "command": "nga",
                "hours": 24,
                "top_n": 5,
            },
            {
                "board_url": "https://bbs.nga.cn/thread.php?fid=650",
                "command": "dnf",
                "hours": 12,
                "top_n": 3,
            },
        ]
        plugin = _make_plugin(astrbot_env, config)
        names = [n for n, _ in astrbot_env.registered_commands]
        assert names == ["nga", "dnf"]
        # R5.2：docstring 供 /help 展示
        h1 = _handler_for(astrbot_env, "nga")
        h2 = _handler_for(astrbot_env, "dnf")
        assert "拉取艾泽拉斯议事厅热帖榜" in (h1.__doc__ or "")
        assert "拉取" in (h2.__doc__ or "") and "fid=650" in (h2.__doc__ or "")
        # 函数名唯一（热重载/多指令不串）
        assert h1.__name__ != h2.__name__
        # 启动自检汇总日志
        summary = [
            m
            for lv, m in astrbot_env.logger.records
            if lv == "info" and "已注册 2 条版面指令，跳过 0 条" in m
        ]
        assert summary
        _run(plugin.terminate())

    def test_invalid_commands_skipped(self, astrbot_env):
        config = dict(DEFAULT_CONFIG)
        config["boards"] = [
            {
                "board_url": "https://bbs.nga.cn/thread.php?fid=7",
                "command": "ok",
                "hours": 24,
                "top_n": 5,
            },
            {
                "board_url": "https://bbs.nga.cn/thread.php?fid=8",
                "command": "has space",
            },  # 含空格
            {
                "board_url": "https://bbs.nga.cn/thread.php?fid=9",
                "command": "bad!char",
            },  # 非法字符
            {
                "board_url": "https://bbs.nga.cn/thread.php?fid=10",
                "command": "x" * 21,
            },  # 超长
            {"board_url": "https://bbs.nga.cn/thread.php?fid=11", "command": ""},  # 空
            {"board_url": "https://bbs.nga.cn/index.php", "command": "nofid"},  # 无 fid
        ]
        plugin = _make_plugin(astrbot_env, config)
        names = [n for n, _ in astrbot_env.registered_commands]
        assert names == ["ok"]
        summary = [
            m
            for lv, m in astrbot_env.logger.records
            if "已注册 1 条版面指令，跳过 5 条" in m
        ]
        assert summary
        _run(plugin.terminate())

    def test_duplicate_command_in_plugin_skips_later(self, astrbot_env):
        config = dict(DEFAULT_CONFIG)
        config["boards"] = [
            {
                "board_url": "https://bbs.nga.cn/thread.php?fid=7",
                "command": "dup",
                "hours": 1,
                "top_n": 1,
            },
            {
                "board_url": "https://bbs.nga.cn/thread.php?fid=8",
                "command": "dup",
                "hours": 2,
                "top_n": 2,
            },
        ]
        plugin = _make_plugin(astrbot_env, config)
        names = [n for n, _ in astrbot_env.registered_commands]
        assert names == ["dup"]
        assert "重复" in " ".join(
            m for lv, m in astrbot_env.logger.records if lv == "warning"
        )
        _run(plugin.terminate())

    def test_conflict_with_external_command_skipped(self, astrbot_env):
        env = astrbot_env
        conflict_md = type(
            "MD",
            (),
            {
                "handler_module_path": "data.plugins.other_plugin.main",
                "event_filters": [env.main.CommandFilter("help")],
                "handler_full_name": "data.plugins.other_plugin.main_help",
            },
        )()
        env.registry.external.append(conflict_md)
        config = dict(DEFAULT_CONFIG)
        config["boards"] = [
            {
                "board_url": "https://bbs.nga.cn/thread.php?fid=7",
                "command": "help",
                "hours": 24,
                "top_n": 5,
            },
            {
                "board_url": "https://bbs.nga.cn/thread.php?fid=8",
                "command": "mine",
                "hours": 24,
                "top_n": 5,
            },
        ]
        plugin = _make_plugin(env, config)
        names = [n for n, _ in env.registered_commands]
        assert names == ["mine"]
        _run(plugin.terminate())


class TestHandleCommand:
    def _plugin_with_client(self, env, client, config=None):
        plugin = _make_plugin(env, config)
        plugin._client = client
        plugin._client_key = ("uid", "cid", 10)
        return plugin

    def test_markdown_send_p0(self, astrbot_env):
        self._plugin_with_client(astrbot_env, FakeClient())
        event = FakeEvent()
        handler = _handler_for(astrbot_env, "nga")

        _run(handler(event))

        # P0：底层 botpy API 以 msg_type=2 + markdown.content 调用
        assert len(event.bot.api.calls) == 1
        call = event.bot.api.calls[0]
        assert call["msg_type"] == 2
        assert call["group_openid"] == "GROUP_OPENID_1"
        assert call["msg_id"] == "MSG_ID_1"
        assert isinstance(call["markdown"], astrbot_env.MarkdownPayload)
        md = call["markdown"].content
        assert md.startswith("## 🔥 艾泽拉斯议事厅 · 热帖榜")
        assert "**热帖一**" in md
        assert "http" not in md and "www." not in md
        # 未走通用文本发送
        assert event.sent == []

    def test_auto_degrades_to_text_on_markdown_failure(self, astrbot_env):
        self._plugin_with_client(astrbot_env, FakeClient())
        event = FakeEvent()
        event.bot.api.fail_with = RuntimeError("不允许发送原生 markdown")
        handler = _handler_for(astrbot_env, "nga")

        _run(handler(event))

        assert event.bot.api.attempts == 1  # markdown 尝试过一次
        assert len(event.sent) == 1  # 降级纯文本重发一次
        text = event.sent[0]
        assert "**" not in text and "##" not in text
        assert "热帖一" in text
        # WARN 日志记录降级
        assert any(
            "降级" in m for lv, m in astrbot_env.logger.records if lv == "warning"
        )

    def test_markdown_mode_failure_reports_error(self, astrbot_env):
        config = dict(DEFAULT_CONFIG)
        config["send_mode"] = "markdown"
        self._plugin_with_client(astrbot_env, FakeClient(), config)
        event = FakeEvent()
        event.bot.api.fail_with = RuntimeError("server error")
        handler = _handler_for(astrbot_env, "nga")

        _run(handler(event))

        assert len(event.sent) == 1
        assert "发送失败" in event.sent[0]

    def test_text_mode_sends_plain(self, astrbot_env):
        config = dict(DEFAULT_CONFIG)
        config["send_mode"] = "text"
        self._plugin_with_client(astrbot_env, FakeClient(), config)
        event = FakeEvent()
        handler = _handler_for(astrbot_env, "nga")

        _run(handler(event))

        assert event.bot.api.calls == []
        assert len(event.sent) == 1
        assert "**" not in event.sent[0]

    def test_empty_result_message(self, astrbot_env):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["boards"][0]["hours"] = 1
        self._plugin_with_client(
            astrbot_env, FakeClient(posts=[_t("老帖", 9, NOW - 7200)]), config
        )
        event = FakeEvent()
        handler = _handler_for(astrbot_env, "nga")

        _run(handler(event))

        assert "最近 1 小时内暂无可展示的热帖" in event.sent[0]

    def test_missing_cookie_prompt(self, astrbot_env):
        config = dict(DEFAULT_CONFIG)
        config["nga_uid"] = ""
        self._plugin_with_client(astrbot_env, FakeClient(), config)
        event = FakeEvent()
        handler = _handler_for(astrbot_env, "nga")

        _run(handler(event))

        assert "请先在插件配置中填写 NGA Cookie" in event.sent[0]
        assert event.bot.api.calls == []

    def test_auth_error_prompt(self, astrbot_env):
        from astrbot_plugin_nga_hot.core.nga_client import NgaAuthError

        self._plugin_with_client(astrbot_env, FakeClient(error=NgaAuthError("登录页")))
        event = FakeEvent()
        handler = _handler_for(astrbot_env, "nga")

        _run(handler(event))

        assert "NGA 登录态失效，请更新 Cookie" in event.sent[0]

    def test_network_error_prompt(self, astrbot_env):
        from astrbot_plugin_nga_hot.core.nga_client import NgaNetworkError

        self._plugin_with_client(
            astrbot_env, FakeClient(error=NgaNetworkError("timeout"))
        )
        event = FakeEvent()
        handler = _handler_for(astrbot_env, "nga")

        _run(handler(event))

        assert "NGA 请求超时/失败，请稍后再试" in event.sent[0]

    def test_unknown_error_caught_and_prompted(self, astrbot_env):
        """R6.1：任何异常不得导致主进程崩溃。"""
        self._plugin_with_client(astrbot_env, FakeClient(error=RuntimeError("boom")))
        event = FakeEvent()
        handler = _handler_for(astrbot_env, "nga")

        _run(handler(event))  # 不抛出

        assert "插件内部错误，请查看日志" in event.sent[0]

    def test_rate_limit_prompt(self, astrbot_env):
        self._plugin_with_client(astrbot_env, FakeClient())
        event = FakeEvent()
        event.bot.api.fail_with = RuntimeError("请求过于频繁(11252)")
        handler = _handler_for(astrbot_env, "nga")

        _run(handler(event))

        assert "发送过于频繁，请稍后再试" in event.sent[0]

    def test_config_error_prompt(self, astrbot_env):
        """board_url 在加载后被改坏 → 兜底提示（FR-6）。"""
        plugin = self._plugin_with_client(astrbot_env, FakeClient())
        plugin.config["boards"][0]["board_url"] = "https://bbs.nga.cn/index.php"
        event = FakeEvent()
        handler = _handler_for(astrbot_env, "nga")

        _run(handler(event))

        assert "版面地址配置错误" in event.sent[0]

    def test_whitelist_violation_degrades_to_text(self, astrbot_env):
        """R4.5：标题带越界语法（如表格符）→ 强制纯文本。"""
        self._plugin_with_client(
            astrbot_env,
            FakeClient(posts=[_t("标题|带|表格符", 9, NOW)]),
        )
        event = FakeEvent()
        handler = _handler_for(astrbot_env, "nga")

        _run(handler(event))

        assert event.bot.api.calls == []  # markdown 未发送
        assert len(event.sent) == 1
        assert "标题|带|表格符" in event.sent[0]

    def test_multi_board_no_cross_talk(self, astrbot_env):
        """AC-4：多版面指令各取各的数据。"""
        config = dict(DEFAULT_CONFIG)
        config["boards"] = [
            {
                "board_url": "https://bbs.nga.cn/thread.php?fid=7",
                "board_name": "议事厅",
                "command": "nga",
                "hours": 0,
                "top_n": 5,
            },
            {
                "board_url": "https://bbs.nga.cn/thread.php?fid=650",
                "board_name": "DNF",
                "command": "dnf",
                "hours": 0,
                "top_n": 5,
            },
        ]
        client7 = FakeClient(posts=[_t("魔兽热帖", 5, NOW)])
        client650 = FakeClient(posts=[_t("地下城热帖", 7, NOW)])
        plugin = _make_plugin(astrbot_env, config)
        plugin._client = client7  # 第一个指令
        plugin._client_key = ("uid", "cid", 10)
        event1 = FakeEvent()
        _run(_handler_for(astrbot_env, "nga")(event1))
        assert "魔兽热帖" in event1.bot.api.calls[0]["markdown"].content
        plugin._client = client650
        plugin._client_key = ("uid", "cid", 10)
        event2 = FakeEvent()
        _run(_handler_for(astrbot_env, "dnf")(event2))
        assert "地下城热帖" in event2.bot.api.calls[0]["markdown"].content
        _run(plugin.terminate())


class TestCache:
    def test_cache_hit_within_ttl(self, astrbot_env):
        """AC-8：TTL 内同版面二次触发只请求一次 NGA。"""
        client = FakeClient()
        plugin = _make_plugin(astrbot_env)
        plugin._client = client
        plugin._client_key = ("uid", "cid", 10)
        handler = _handler_for(astrbot_env, "nga")

        _run(handler(FakeEvent()))
        _run(handler(FakeEvent()))
        assert client.calls == 1
        _run(plugin.terminate())

    def test_cache_expires(self, astrbot_env):
        client = FakeClient()
        plugin = _make_plugin(astrbot_env)
        plugin._client = client
        plugin._client_key = ("uid", "cid", 10)

        real_time = time.time

        class _FakeTime:
            t = real_time()

            @classmethod
            def now(cls):
                return cls.t

        astrbot_env.main.time.time = _FakeTime.now
        try:
            handler = _handler_for(astrbot_env, "nga")
            _run(handler(FakeEvent()))
            _FakeTime.t += 120  # 超过默认 60s TTL
            _run(handler(FakeEvent()))
            assert client.calls == 2
        finally:
            astrbot_env.main.time.time = real_time
        _run(plugin.terminate())

    def test_cache_disabled_with_zero_ttl(self, astrbot_env):
        config = dict(DEFAULT_CONFIG)
        config["cache_ttl"] = 0
        client = FakeClient()
        plugin = _make_plugin(astrbot_env, config)
        plugin._client = client
        plugin._client_key = ("uid", "cid", 10)
        handler = _handler_for(astrbot_env, "nga")

        _run(handler(FakeEvent()))
        _run(handler(FakeEvent()))
        assert client.calls == 2

    def test_cache_cleared_on_terminate(self, astrbot_env):
        plugin = _make_plugin(astrbot_env)
        plugin._cache_put("7", [_t("x", 1, NOW)])
        assert plugin._cache
        _run(plugin.terminate())
        assert plugin._cache == {}
        assert plugin._boards_snapshot == {}
        assert plugin._registered_attrs == []

    def test_cache_upper_bound(self, astrbot_env):
        plugin = _make_plugin(astrbot_env)
        for i in range(60):
            plugin._cache_put(str(i), [_t("x", i, NOW)])
        assert len(plugin._cache) <= 50
        _run(plugin.terminate())
