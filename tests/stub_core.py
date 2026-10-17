"""Minimal ``core`` stubs for running KiraKB sources without KiraAI.

Only the surfaces the plugin actually touches are implemented. The stubs are
deliberately permissive: tests drive the real plugin code paths instead of
re-implementing them.
"""

import sys
import types
from pathlib import Path


class _Logger:
    def __init__(self):
        self.errors = []
        self.warnings = []
        self.infos = []

    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        self.infos.append(a[0] if a else "")

    def warning(self, *a, **k):
        self.warnings.append(a[0] if a else "")

    def error(self, *a, **k):
        self.errors.append(a[0] if a else "")


LOG = _Logger()


class _Register:
    def tool(self, name, description, params):
        def deco(func):
            return func
        return deco

    def api(self, method, path, auth=True, **kwargs):
        def deco(func):
            return func
        return deco

    def page(self, route, auth=True, menu=None):
        def deco(func):
            return func
        return deco


class _On:
    def llm_request(self, priority=0):
        def deco(func):
            return func
        return deco

    def __getattr__(self, name):
        def deco_factory(*args, **kwargs):
            def deco(func):
                return func
            return deco
        return deco_factory


class PageMenu:
    def __init__(self, label=None, icon=None, order=100):
        self.label = label
        self.icon = icon
        self.order = order


class PluginPage:
    @classmethod
    def from_folder(cls, path):
        return cls()


class BasePlugin:
    def __init__(self, ctx, cfg):
        self.ctx = ctx
        self.plugin_cfg = cfg
        self.logger = LOG


class Prompt:
    def __init__(self, content, name=None, source=None, end="\n", persist=True,
                 render_template=False, **kwargs):
        self.content = content
        self.name = name
        self.source = source
        self.end = end
        self.persist = persist
        self.render_template = render_template
        self.kwargs = kwargs

    def to_string(self):
        return self.content + (self.end or "")


class LLMRequest:
    def __init__(self):
        self.messages = []
        self.user_prompt = []
        self.system_prompt = []


def install(plugin_dir: Path | str = None) -> Path:
    """Install the stub package into ``sys.modules`` and return the plugin path."""
    plugin_dir = Path(plugin_dir or Path(__file__).resolve().parent.parent)
    core = types.ModuleType("core")
    core.__path__ = []

    logging_manager = types.ModuleType("core.logging_manager")
    logging_manager.get_logger = lambda name, color=None: LOG
    core.logging_manager = logging_manager

    plugin = types.ModuleType("core.plugin")
    plugin.BasePlugin = BasePlugin
    plugin.logger = LOG
    plugin.on = _On()
    plugin.register = _Register()
    plugin.PluginPage = PluginPage
    plugin.PageMenu = PageMenu
    plugin.Priority = types.SimpleNamespace(LOW=-50, MEDIUM=0, HIGH=50)
    core.plugin = plugin

    chat = types.ModuleType("core.chat")
    chat.__path__ = []
    message_utils = types.ModuleType("core.chat.message_utils")

    class KiraMessageBatchEvent:
        pass

    message_utils.KiraMessageBatchEvent = KiraMessageBatchEvent
    chat.message_utils = message_utils

    provider = types.ModuleType("core.provider")
    provider.LLMRequest = LLMRequest

    prompt_manager = types.ModuleType("core.prompt_manager")
    prompt_manager.Prompt = Prompt

    utils = types.ModuleType("core.utils")
    utils.__path__ = []
    path_utils = types.ModuleType("core.utils.path_utils")
    data_state = {"dir": Path("/tmp/kirakb_test_data")}
    path_utils.get_data_path = lambda: data_state["dir"]
    path_utils.get_config_path = lambda: data_state["dir"] / "config"
    path_utils.set_data_dir = lambda p: data_state.__setitem__("dir", Path(p))
    utils.path_utils = path_utils

    for name, mod in {
        "core": core,
        "core.logging_manager": logging_manager,
        "core.plugin": plugin,
        "core.chat": chat,
        "core.chat.message_utils": message_utils,
        "core.provider": provider,
        "core.prompt_manager": prompt_manager,
        "core.utils": utils,
        "core.utils.path_utils": path_utils,
    }.items():
        sys.modules[name] = mod

    pkg = types.ModuleType("kbp")
    pkg.__path__ = [str(plugin_dir)]
    sys.modules["kbp"] = pkg
    return plugin_dir
