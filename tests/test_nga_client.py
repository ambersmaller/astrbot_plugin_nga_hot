"""NGA 抓取解析模块离线测试（R2 / RA-6 / RA-8 / FR-6）。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from astrbot_plugin_nga_hot.core import nga_client
from astrbot_plugin_nga_hot.core.nga_client import (
    NgaAuthError,
    NgaClient,
    NgaConfigError,
    NgaNetworkError,
    NgaParseError,
    build_api_url,
    decode_body,
    parse_board_payload,
    parse_fid,
)

FIXTURE = Path(__file__).parent / "fixtures" / "fid7_sample.json"


def _load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class TestParseFid:
    def test_standard_url(self):
        assert parse_fid("https://bbs.nga.cn/thread.php?fid=7") == 7

    def test_url_with_extra_params(self):
        assert parse_fid("https://bbs.nga.cn/thread.php?fid=650&foo=bar") == 650

    def test_no_fid(self):
        with pytest.raises(NgaConfigError):
            parse_fid("https://bbs.nga.cn/thread.php")

    def test_empty(self):
        with pytest.raises(NgaConfigError):
            parse_fid("")

    def test_non_digit_fid(self):
        with pytest.raises(NgaConfigError):
            parse_fid("https://bbs.nga.cn/thread.php?fid=abc")


class TestBuildApiUrl:
    def test_appends_lite_js(self):
        url = build_api_url("https://bbs.nga.cn/thread.php?fid=7")
        assert "lite=js" in url and "noprefix" in url
        assert url.startswith("https://bbs.nga.cn/thread.php?fid=7&")

    def test_idempotent(self):
        url = build_api_url("https://bbs.nga.cn/thread.php?fid=7&lite=js&noprefix")
        assert url.count("lite=js") == 1


class TestDecodeBody:
    def test_gbk_bytes(self):
        text = json.dumps({"k": "中文标题"}, ensure_ascii=False)
        body = text.encode("gbk")
        assert decode_body(body) == text

    def test_gbk_with_errors_replaced(self):
        body = b"\xff\xfeabc"
        assert decode_body(body) == "��abc"


class TestParseBoardPayload:
    def test_object_form(self):
        threads = parse_board_payload(_load_fixture())
        # 2 条残缺记录被跳过（缺 replies / tid 非法）
        assert len(threads) == 5
        subjects = [t.subject for t in threads]
        assert "[战报帖] 乌拉特克的诅咒" in subjects[0]
        assert all("字段残缺" not in s for s in subjects)
        assert all("tid 非法" not in s for s in subjects)
        first = threads[0]
        assert first.tid == 47344551
        assert first.postdate == 1786338953
        assert first.replies == 4426

    def test_array_form_compatible(self):
        """RA-6：__T 若变为数组结构同样可解析。"""
        payload = _load_fixture()
        payload["data"]["__T"] = list(payload["data"]["__T"].values())
        threads = parse_board_payload(payload)
        assert len(threads) == 5

    def test_missing_data_is_auth_error(self):
        with pytest.raises(NgaAuthError):
            parse_board_payload({"errno": 0})

    def test_missing_threads_is_auth_error(self):
        with pytest.raises(NgaAuthError):
            parse_board_payload({"data": {}})

    def test_non_dict_payload(self):
        with pytest.raises(NgaParseError):
            parse_board_payload(["not", "a", "dict"])


class _FakeResp:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def read(self) -> bytes:
        return self._body


class _FakeSession:
    """模拟 aiohttp.ClientSession（异步上下文管理器）。"""

    next_body: bytes | Exception = b""
    next_status: int = 200
    last_headers: dict | None = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def get(self, url, headers=None):
        type(self).last_headers = headers or {}
        body = type(self).next_body
        if isinstance(body, Exception):
            raise body
        status = type(self).next_status
        return _FakeResp(body, status=status)


@pytest.fixture()
def fake_session(monkeypatch):
    monkeypatch.setattr(nga_client.aiohttp, "ClientSession", _FakeSession)
    return _FakeSession


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestFetchBoard:
    def test_gbk_json_success(self, fake_session):
        payload = json.dumps(_load_fixture(), ensure_ascii=False)
        fake_session.next_body = payload.encode("gbk")
        client = NgaClient("uid123", "cid456", timeout=5)
        threads = _run(client.fetch_board("https://bbs.nga.cn/thread.php?fid=7"))
        assert len(threads) == 5
        # R2.3：请求头携带 Cookie 与 UA
        headers = fake_session.last_headers
        assert headers["Cookie"] == "ngaPassportUid=uid123; ngaPassportCid=cid456"
        assert "Mozilla" in headers["User-Agent"]
        # 异常信息/日志路径不携带 Cookie
        assert "uid123" not in str(threads)

    def test_login_page_is_auth_error(self, fake_session):
        fake_session.next_body = "<html><body>请先登录</body></html>".encode("gbk")
        client = NgaClient("uid", "cid")
        with pytest.raises(NgaAuthError):
            _run(client.fetch_board("https://bbs.nga.cn/thread.php?fid=7"))

    def test_timeout_is_network_error(self, fake_session):
        fake_session.next_body = asyncio.TimeoutError()
        client = NgaClient("uid", "cid")
        with pytest.raises(NgaNetworkError):
            _run(client.fetch_board("https://bbs.nga.cn/thread.php?fid=7"))

    def test_bad_config_url(self, fake_session):
        client = NgaClient("uid", "cid")
        with pytest.raises(NgaConfigError):
            _run(client.fetch_board("https://bbs.nga.cn/index.php"))

    def test_garbage_is_parse_error(self, fake_session):
        fake_session.next_body = "@@##不是JSON也不是HTML".encode("gbk")
        client = NgaClient("uid", "cid")
        with pytest.raises(NgaParseError):
            _run(client.fetch_board("https://bbs.nga.cn/thread.php?fid=7"))

    def test_http_status_error(self, fake_session):
        fake_session.next_body = b"error"
        fake_session.next_status = 500
        client = NgaClient("uid", "cid")
        with pytest.raises(NgaNetworkError):
            _run(client.fetch_board("https://bbs.nga.cn/thread.php?fid=7"))
