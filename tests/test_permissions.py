"""Permission model tests (pure policy + tool enforcement)."""

import asyncio
import shutil
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stub_core

stub_core.install()
import importlib

perms = importlib.import_module("kbp.permissions")
main = importlib.import_module("kbp.main")

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


class Sender:
    def __init__(self, uid):
        self.user_id = uid


class Chain:
    def __init__(self, text):
        self._text = text

    def get_plain_text(self):
        return self._text


class Msg:
    def __init__(self, uid, text="hello"):
        self.sender = Sender(uid)
        self.is_notice = False
        self.chain = Chain(text)


class Event:
    def __init__(self, users, text="hello", sid="webchat:dm:1"):
        self.messages = [Msg(u, text) for u in users]
        self.sid = sid
        self.event_id = "evt-" + "-".join(users)
        self.self_id = "bot"


class SameVector:
    def __init__(self, dim=8):
        self.dim = dim

    async def embed(self, texts):
        base = [1.0] + [0.0] * (self.dim - 1)
        return [list(base) for _ in texts]


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


ROOT = Path("/tmp/kirakb_perm_test")


def cfg_for(**perm):
    return {
        "section_basic": {"chunk_size": 200, "chunk_overlap": 20},
        "section_webui": {"enable_webui": False},
        "section_permission": {
            "owner_whitelist": perm.pop("owner_whitelist", []),
            "default_add_policy": perm.pop("default_add_policy", "open"),
            "default_edit_policy": perm.pop("default_edit_policy", "open"),
            "default_delete_policy": perm.pop("default_delete_policy", "whitelist"),
            **perm,
        },
        "section_recall": {},
    }


async def make_plugin(cfg):
    shutil.rmtree(ROOT, ignore_errors=True)
    stub_core.sys.modules["core.utils.path_utils"].set_data_dir(ROOT)
    plugin = main.KiraKBPlugin(FakeCtx(), cfg)
    await plugin.initialize()
    kb = await plugin.kb_manager.create_kb("test")
    doc_id = await kb.add_raw_document("初始内容", "知识条目.txt")
    version_id = await kb.create_version("m", 8, None, None)
    await kb.set_active_version(version_id)
    return plugin, kb, doc_id


def policy_tests():
    print("\nP0  策略解析")
    allowed, _, source = perms.decide(
        op="add", actor="ai", user_ids=["1"],
        kb_perms=None, defaults={"add": "open", "edit": "open", "delete": "whitelist"},
        global_whitelist=[],
    )
    check("默认新增放行", allowed and source == "default")

    allowed, _, _ = perms.decide(
        op="delete", actor="ai", user_ids=["2"],
        kb_perms=None, defaults=None, global_whitelist=["1"],
    )
    check("默认删除受白名单限制", allowed is False)

    allowed, _, _ = perms.decide(
        op="delete", actor="ai", user_ids=["1"],
        kb_perms=None, defaults=None, global_whitelist=["1"],
    )
    check("白名单内删除放行", allowed is True)

    allowed, reason, _ = perms.decide(
        op="edit", actor="ai", user_ids=["1"],
        entry_perms={"edit": "admin"}, kb_perms={"edit": "open"},
    )
    check("条目 admin 覆盖 KB open", allowed is False and "人工" in reason)

    allowed, _, source = perms.decide(
        op="edit", actor="ai", user_ids=["1"],
        entry_perms={"edit": "inherit"}, kb_perms={"edit": "whitelist", "allow_users": ["1"]},
    )
    check("条目 inherit 回落 KB 名单", allowed is True and source == "kb")

    allowed, _, _ = perms.decide(
        op="add", actor="ai", user_ids=["9"],
        kb_perms={"add": "whitelist", "allow_users": ["1"]},
    )
    check("KB 自定义名单拒绝外人", allowed is False)

    allowed, _, _ = perms.decide(
        op="add", actor="ai", user_ids=["9", "1"],
        kb_perms={"add": "whitelist", "allow_users": ["1"]},
    )
    check("批次内有授权用户即放行", allowed is True)

    allowed, _, _ = perms.decide(op="add", actor="human", user_ids=[],
                                 kb_perms={"add": "admin"})
    check("人工可越过 admin", allowed is True)

    allowed, _, _ = perms.decide(op="add", actor="human", user_ids=[],
                                 kb_perms={"add": "off"})
    check("off 冻结也挡人工", allowed is False)


async def tool_tests():
    print("\nP1  工具层权限")
    plugin, kb, doc_id = await make_plugin(cfg_for(owner_whitelist=["1"]))
    evt_u1 = Event(["1"])
    evt_u2 = Event(["2"])

    msg = await plugin.knowledge_update_entry(evt_u2, "第二条", "第二条")
    check("默认新增允许（旧行为）", "已新增" in msg, msg)

    msg = await plugin.knowledge_delete_entry(evt_u2, "知识条目")
    check("默认删除拒绝非白名单", "权限不足" in msg, msg)
    msg = await plugin.knowledge_delete_entry(evt_u1, "知识条目")
    check("白名单删除成功", "已软删除" in msg, msg)

    await kb.set_permissions({"add": "admin"})
    msg = await plugin.knowledge_update_entry(evt_u1, "第三条", "第三条")
    check("KB add=admin 拒绝 AI", "权限不足" in msg and "人工" in msg, msg)

    await kb.set_permissions({"add": "whitelist", "allow_users": ["7"]})
    msg = await plugin.knowledge_update_entry(evt_u1, "第四条", "第四条")
    check("KB 名单不含当前用户则拒绝", "权限不足" in msg, msg)
    msg = await plugin.knowledge_update_entry(Event(["7"]), "第四条", "第四条")
    check("KB 名单内用户允许", "已新增" in msg, msg)

    # Entry-level override
    await kb.set_permissions({"add": "open", "edit": "open"})
    doc = kb.find_document("第四条", include_deleted=False)
    await kb.update_document_settings(doc["doc_id"], {"permissions": {"edit": "admin"}})
    msg = await plugin.knowledge_update_entry(evt_u1, "改写后的内容", "第四条")
    check("条目 edit=admin 拒绝 AI", "权限不足" in msg, msg)
    check("被拒后内容未变", (await kb.get_raw_document(doc["doc_id"])) == "第四条")

    # Batch-level authorization
    await kb.update_document_settings(doc["doc_id"], {"permissions": {"edit": "whitelist",
                                                                    "allow_users": ["5"]}})
    msg = await plugin.knowledge_update_entry(Event(["9", "5"]), "批次授权内容", "第四条")
    check("批次内有授权用户时允许编辑", "已更新" in msg, msg)

    # Revive bypass: delete then recreate must honour entry edit policy
    await kb.set_permissions({"delete": "open"})
    await plugin.knowledge_delete_entry(evt_u1, "第四条")
    await kb.update_document_settings(doc["doc_id"], {"permissions": {"edit": "admin"}})
    msg = await plugin.knowledge_update_entry(evt_u1, "复活内容", "第四条")
    check("同名复活按条目 edit 判定", "权限不足" in msg, msg)
    still = kb.find_document("第四条", include_deleted=True)
    check("被拒后条目仍为软删", still is not None and still.get("deleted") is True)

    # Restore permission uses delete policy
    msg = await plugin.knowledge_restore_entry(evt_u1, "第四条")
    check("恢复受删除权限约束（当前 open）", "已恢复" in msg, msg)

    # Extra tools registered
    check("额外只读工具已注册", "knowledge_list_entries" in plugin.ctx.tool_mgr.tools
          and "knowledge_get_entry" in plugin.ctx.tool_mgr.tools)
    listed = await plugin.knowledge_list_entries(evt_u1)
    check("条目列表可读", "知识条目" in listed or "第二条" in listed, listed[:80])

    # Read paths: search / list / get
    await plugin.knowledge_update_entry(evt_u1, "独特关键词 ZXQ 的内容", "检索测试条目")
    found = await plugin.knowledge_search(evt_u1, "ZXQ")
    check("检索命中并返回显示名", "检索测试条目" in found, found[:80])
    found_all = await plugin.knowledge_search(evt_u1, "ZXQ", kb_id="all")
    check("全库检索可用", "检索测试条目" in found_all, found_all[:80])
    got = await plugin.knowledge_get_entry(evt_u1, "检索测试条目")
    check("读取条目全文", "ZXQ" in got, got[:60])
    bases = await plugin.list_knowledge_bases(evt_u1)
    check("知识库列表含条目数", "个条目" in bases, bases[:80])

    await plugin.terminate()


async def main_async():
    policy_tests()
    await tool_tests()

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
