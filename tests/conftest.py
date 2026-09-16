"""离线测试公共配置：注入 astrbot 桩模块，使 main.py 无需真实 AstrBot 环境即可导入测试。"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT.parent))


def _build_astrbot_stubs() -> dict:
    """构造最小可用的 astrbot 模块桩集合。"""
    stubs: dict[str, types.ModuleType] = {}

    def _mod(name: str) -> types.ModuleType:
        m = types.ModuleType(name)
        stubs[name] = m
        return m

    # astrbot.api.logger
    class _Logger:
        def __init__(self) -> None:
            self.records: list[tuple[str, str]] = []

        def _log(self, level: str, msg: str) -> None:
            self.records.append((level, msg))

        def info(self, msg: str) -> None:
            self._log("info", str(msg))

        def warning(self, msg: str) -> None:
            self._log("warning", str(msg))

        def error(self, msg: str) -> None:
            self._log("error", str(msg))

        def debug(self, msg: str) -> None:
            self._log("debug", str(msg))

    logger = _Logger()

    api = _mod("astrbot.api")
    api.logger = logger  # type: ignore[attr-defined]

    # astrbot.api.star
    class Star:
        def __init__(self, context=None, config=None) -> None:
            self.context = context

    class Context: ...

    star_mod = _mod("astrbot.api.star")
    star_mod.Star = Star  # type: ignore[attr-defined]
    star_mod.Context = Context  # type: ignore[attr-defined]

    # astrbot.api.message_components
    class Plain:
        def __init__(self, text: str = "") -> None:
            self.text = text

    mc = _mod("astrbot.api.message_components")
    mc.Plain = Plain  # type: ignore[attr-defined]

    # astrbot.api.event（AstrMessageEvent / MessageChain / filter）
    class AstrMessageEvent: ...

    class MessageChain:
        def __init__(self) -> None:
            self.chain: list = []

        def message(self, text: str) -> MessageChain:
            self.chain.append(Plain(text))
            return self

    class CommandFilter:
        """桩：与真实 CommandFilter 同名属性，用于冲突查重。"""

        def __init__(self, command_name: str, alias=None) -> None:
            self.command_name = command_name
            self.alias = alias or set()

        def get_complete_command_names(self):
            return [self.command_name] + list(self.alias)

    registered_commands: list[tuple[str, object]] = []

    class _FilterFacade:
        """桩 filter：记录动态注册（模拟 register_command 行为）。"""

        @staticmethod
        def command(command_name: str):
            def decorator(awaitable):
                registered_commands.append((command_name, awaitable))
                return awaitable

            return decorator

    event_mod = _mod("astrbot.api.event")
    event_mod.AstrMessageEvent = AstrMessageEvent  # type: ignore[attr-defined]
    event_mod.MessageChain = MessageChain  # type: ignore[attr-defined]
    event_mod.filter = _FilterFacade  # type: ignore[attr-defined]

    # astrbot.core.star.filter.command
    _mod("astrbot.core")
    _mod("astrbot.core.star")
    _mod("astrbot.core.star.filter")
    fcmd = _mod("astrbot.core.star.filter.command")
    fcmd.CommandFilter = CommandFilter  # type: ignore[attr-defined]

    # astrbot.core.star.star_handler
    class _HandlerMd:
        def __init__(self, module_path: str, filters) -> None:
            self.handler_module_path = module_path
            self.event_filters = filters
            self.handler_full_name = f"{module_path}_external"

    class _Registry:
        """桩 registry：可注入「外部插件已注册指令」。"""

        def __init__(self) -> None:
            self.external: list[_HandlerMd] = []

        def get_handlers_by_event_type(self, event_type, only_activated=True):
            return list(self.external)

    class EventType:
        AdapterMessageEvent = object()

    sh = _mod("astrbot.core.star.star_handler")
    sh.EventType = EventType  # type: ignore[attr-defined]
    sh.star_handlers_registry = _Registry()  # type: ignore[attr-defined]

    # astrbot / botpy 顶层包
    astrbot = _mod("astrbot")
    astrbot.api = api  # type: ignore[attr-defined]

    # botpy（_send_markdown_direct 内部延迟导入）
    _mod("botpy")
    _mod("botpy.types")
    botpy_msg = _mod("botpy.types.message")

    class MarkdownPayload:
        def __init__(self, content: str = "") -> None:
            self.content = content

    botpy_msg.MarkdownPayload = MarkdownPayload  # type: ignore[attr-defined]

    return {
        "modules": stubs,
        "logger": logger,
        "registered_commands": registered_commands,
        "registry": sh.star_handlers_registry,
        "Plain": Plain,
        "MarkdownPayload": MarkdownPayload,
    }


@pytest.fixture()
def astrbot_env(monkeypatch):
    """注入 astrbot 桩并导入插件 main 模块（每个测试独立，避免注册表串扰）。"""
    env = _build_astrbot_stubs()
    for name, mod in env["modules"].items():
        monkeypatch.setitem(sys.modules, name, mod)

    for mod_name in list(sys.modules):
        if mod_name == "astrbot_plugin_nga_hot" or mod_name.startswith(
            "astrbot_plugin_nga_hot."
        ):
            monkeypatch.delitem(sys.modules, mod_name, raising=False)

    main_mod = importlib.import_module("astrbot_plugin_nga_hot.main")
    importlib.reload(main_mod)

    class Env:
        pass

    e = Env()
    e.main = main_mod
    e.logger = env["logger"]
    e.registered_commands = env["registered_commands"]
    e.registry = env["registry"]
    e.MarkdownPayload = env["MarkdownPayload"]
    return e
