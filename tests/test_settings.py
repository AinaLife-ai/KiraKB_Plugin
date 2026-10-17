"""Hot runtime settings tests (no plugin reload)."""

import asyncio
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stub_core

stub_core.install()
import importlib

main = importlib.import_module("kbp.main")
api = importlib.import_module("kbp.api_handlers")

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


class SameVector:
    async def embed(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


class FakeToolMgr:
    def __init__(self):
        self.tools = {}

    def register_tool(self, name, description, parameters, func):
        self.tools[name] = func

    def unregister_tool(self, name):
        self.tools.pop(name, None)


class FakeConfig:
    def get_config(self, key, default=None):
        return default


class FakeProviderMgr:
    def get_default_vlm(self):
        raise RuntimeError("no vlm")

    def get_default_rerank(self):
        raise RuntimeError("no rerank")

    def get_all_providers(self):
        return {}


class FakeCtx:
    def __init__(self):
        self.client = SameVector()
        self.provider_mgr = FakeProviderMgr()
        self.config = FakeConfig()
        self.tool_mgr = FakeToolMgr()

    def get_default_embedding_client(self):
        return self.client

    def get_embedding_client(self, model_uuid, default=False):
        return self.client


ROOT = Path("/tmp/kirakb_settings_test")


def base_cfg():
    return {
        "section_basic": {"chunk_size": 500, "chunk_overlap": 100},
        "section_webui": {"enable_webui": False},
        "section_permission": {},
        "section_recall": {},
    }


async def main_async():
    shutil.rmtree(ROOT, ignore_errors=True)
    stub_core.sys.modules["core.utils.path_utils"].set_data_dir(ROOT)
    plugin = main.KiraKBPlugin(FakeCtx(), base_cfg())
    await plugin.initialize()
    await plugin.kb_manager.create_kb("test")

    print("\nS1  快照")
    snap = plugin._settings_snapshot()
    check("包含分区", all(k in snap for k in ("search", "entries", "permission", "recall", "restart")), str(snap.keys()))
    check("默认历史保留 5", snap["entries"]["entry_history_keep"] == 5, str(snap["entries"]))

    print("\nS2  应用热设置")
    result = plugin._apply_settings({
        "search": {"enable_hybrid_search": True, "default_top_k": 9,
                   "chunk_size": 700, "chunk_overlap": 120},
        "entries": {"entry_title_max_chars": 32, "entry_history_keep": 8,
                    "extra_tools_enabled": False, "auto_sync_versions": True},
        "permission": {"owner_whitelist": ["1", "2"],
                       "default_add_policy": "admin",
                       "default_edit_policy": "whitelist",
                       "default_delete_policy": "open"},
        "recall": {"recall_enabled": True, "recall_mode": "semantic",
                   "recall_top_k": 4, "recall_min_score": 0.44,
                   "recall_strong_score": 0.71, "recall_max_chars": 900,
                   "recall_timeout_ms": 800, "recall_dedupe_turns": 3,
                   "recall_max_kbs": 2, "recall_min_query_chars": 4},
    })
    check("应用成功", result["ok"] is True, str(result.get("warnings")))
    check("混合检索生效", plugin.enable_hybrid and plugin.kb_manager.enable_hybrid)
    check("默认 top_k", plugin.default_top_k == 9, str(plugin.default_top_k))
    check("KB 分块参数同步", plugin.kb_manager.kbs["test"].chunk_size == 700,
          str(plugin.kb_manager.kbs["test"].chunk_size))
    check("标题长度", plugin.entry_title_max_chars == 32)
    check("历史保留", plugin.history_keep == 8 and plugin.kb_manager.history_keep == 8)
    check("自动同步", plugin.auto_sync_versions is True)
    check("名单更新", plugin.owner_whitelist == ["1", "2"], str(plugin.owner_whitelist))
    check("默认策略更新", plugin.default_policies["add"] == "admin"
          and plugin.default_policies["delete"] == "open", str(plugin.default_policies))
    check("管理器策略同步", plugin.kb_manager.default_policies["add"] == "admin")
    check("召回开关", plugin.recall_config["enabled"] is True)
    check("召回模式", plugin.recall_config["mode"] == "semantic", str(plugin.recall_config["mode"]))
    check("强门槛可调", plugin.recall_config["strong_score"] == 0.71)
    check("管理器召回快照同步", plugin.kb_manager.recall_global["enabled"] is True)

    print("\nS3  只读工具热开关")
    check("关闭后不再注册", "knowledge_list_entries" not in plugin.ctx.tool_mgr.tools
          and "knowledge_get_entry" not in plugin.ctx.tool_mgr.tools)
    plugin._apply_settings({"entries": {"extra_tools_enabled": True}})
    check("开启后重新注册", "knowledge_list_entries" in plugin.ctx.tool_mgr.tools
          and "knowledge_get_entry" in plugin.ctx.tool_mgr.tools)

    print("\nS4  持久化到插件配置")
    cfg_path = ROOT / "config" / "plugins" / "kiraKB.json"
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    check("基础节已写入", saved["section_basic"]["enable_hybrid_search"] is True
          and saved["section_basic"]["entry_history_keep"] == 8, str(saved["section_basic"]))
    check("权限节已写入", saved["section_permission"]["default_add_policy"] == "admin",
          str(saved["section_permission"]))
    check("召回节已写入", saved["section_recall"]["recall_strong_score"] == 0.71,
          str(saved["section_recall"]))

    print("\nS5  非法值回退")
    plugin._apply_settings({"recall": {"recall_mode": "nope"}})
    check("非法模式回退 hybrid", plugin.recall_config["mode"] == "hybrid")
    plugin._apply_settings({"search": {"chunk_size": 8000, "chunk_overlap": 9999}})
    check("重叠被钳制", plugin.chunk_overlap < plugin.chunk_size,
          f"{plugin.chunk_overlap}/{plugin.chunk_size}")

    print("\nS6  API 往返")
    data, status = api.get_settings(plugin.kb_manager)
    check("GET settings", status == 200 and data["recall"]["mode"] == "hybrid", str(status))
    data, status = api.update_settings(plugin.kb_manager, {"recall": {"recall_top_k": 6}})
    check("PUT settings", status == 200 and data["settings"]["recall"]["top_k"] == 6, str(status))

    await plugin.terminate()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    print("\n" + "=" * 60)
    print(f"TOTAL: {passed}/{len(RESULTS)} passed")
    for name, detail in failed:
        print(f"  FAIL {name}  {detail}")
    print("=" * 60)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async()))
