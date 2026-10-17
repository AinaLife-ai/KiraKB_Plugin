"""Regression tests for the v1.2 upgrade fixes (names, staleness, restore, hybrid)."""

import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stub_core

stub_core.install()
import importlib

km_mod = importlib.import_module("kbp.kb_manager")
api = importlib.import_module("kbp.api_handlers")

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


class Fixed:
    def __init__(self, dim=8, base=None):
        self.dim = dim
        self.base = base

    async def embed(self, texts):
        if self.base is not None:
            return [list(self.base) for _ in texts]
        return [[1.0] + [0.0] * (self.dim - 1) for _ in texts]


ROOT = Path("/tmp/kirakb_upgrade_test")


def make_mgr():
    shutil.rmtree(ROOT, ignore_errors=True)
    ROOT.mkdir(parents=True)
    client = Fixed()

    async def getter():
        return client

    def resolver(model_uuid=None):
        return client

    return km_mod.KnowledgeBaseManager(
        base_dir=str(ROOT), embedding_client_getter=getter,
        chunk_size=80, chunk_overlap=10,
        client_resolver=resolver, enable_hybrid=False,
    )


async def main_async():
    mgr = make_mgr()
    kb = await mgr.create_kb("demo")
    doc_a = await kb.add_raw_document("安装指南的正文内容，提到依赖与启动。", "安装指南")
    doc_b = await kb.add_raw_document("部署手册的正文内容，提到服务器与端口。", "部署手册")
    v1 = await kb.create_version("m", 8, None, None)
    await kb.set_active_version(v1)

    print("\nU1  条目显示名")
    data, status = await api.search(mgr, "demo", {"query": "安装指南"})
    check("检索成功", status == 200 and isinstance(data, list), str(status))
    names = {row["doc_name"] for row in data}
    check("返回显示名而非 doc_id", names == {"安装指南", "部署手册"}, str(names))
    check("每条结果都带 doc_id", all(row.get("doc_id") for row in data), str(data))

    print("\nU2  版本新鲜度")
    fresh = kb.version_staleness(v1)
    check("初始无缺失", not fresh["missing"] and not fresh["stale"], str(fresh))
    doc_c = await kb.add_raw_document("新增条目的正文。", "新增条目")
    fresh = kb.version_staleness(v1)
    check("新增条目被标为缺失", [d["doc_id"] for d in fresh["missing"]] == [doc_c], str(fresh["missing"]))
    await kb.update_raw_document(doc_a, "安装指南已更新。")
    fresh = kb.version_staleness(v1)
    check("更新条目被标为过期", [d["doc_id"] for d in fresh["stale"]] == [doc_a], str(fresh["stale"]))

    print("\nU3  增量同步")
    result = await kb.sync_version(v1)
    check("同步覆盖两项", result["synced"] == 2, str(result))
    fresh = kb.version_staleness(v1)
    check("同步后无缺失/过期", not fresh["missing"] and not fresh["stale"], str(fresh))

    print("\nU4  软删恢复回写所有版本")
    v2 = await kb.create_version("m2", 8, None, None)
    await kb.set_active_version(v2)
    await kb.delete_raw_document(doc_a, soft=True)
    check("删除后 v1 映射移除", doc_a not in kb._versions[v1].load_mapping())
    ok, detail = await kb.restore_document(doc_a)
    check("恢复成功", ok, str(detail))
    check("恢复后 v1 映射存在", doc_a in kb._versions[v1].load_mapping())
    check("恢复后 v2 映射存在", doc_a in kb._versions[v2].load_mapping())

    print("\nU5  编辑后旧版本标脏 + 手动同步")
    await kb.update_raw_document(doc_a, "安装指南第三版。")
    active = kb.active_version
    await active.delete_document(doc_a)
    await kb._vectorize_document(active, doc_a, "安装指南第三版。")
    fresh_v1 = kb.version_staleness(v1)
    check("旧版本标脏", any(d["doc_id"] == doc_a for d in fresh_v1["stale"]), str(fresh_v1))
    await kb.sync_version(v1)
    fresh_v1 = kb.version_staleness(v1)
    check("同步后旧版本干净", not fresh_v1["stale"], str(fresh_v1))

    print("\nU6  hybrid 口径统一")
    seen = {}
    original = active.search

    async def spy(query, top_k=5, enable_hybrid=True, query_embedding=None):
        seen["enable_hybrid"] = enable_hybrid
        return await original(query, top_k=top_k, enable_hybrid=enable_hybrid)

    active.search = spy
    await api.search(mgr, "demo", {"query": "安装"})
    check("面板检索遵循插件配置(False)", seen.get("enable_hybrid") is False, str(seen))
    mgr.enable_hybrid = True
    await api.search(mgr, "demo", {"query": "安装"})
    check("配置切换后生效(True)", seen.get("enable_hybrid") is True, str(seen))

    print("\nU7  条目设置持久化")
    await kb.update_document_settings(doc_b, {
        "permissions": {"edit": "admin", "allow_users": ["42"]},
        "recall": {"enabled": False, "keywords": ["服务器"]},
    })
    settings = kb.get_document_settings(doc_b)
    check("权限持久化", settings["permissions"]["edit"] == "admin", str(settings["permissions"]))
    check("名单持久化", settings["permissions"]["allow_users"] == ["42"])
    check("召回三态持久化", settings["recall"]["enabled"] is False, str(settings["recall"]))
    check("关键词持久化", settings["recall"]["keywords"] == ["服务器"])

    reloaded = km_mod.KnowledgeBaseManager(
        base_dir=str(ROOT), embedding_client_getter=None,
        chunk_size=80, chunk_overlap=10, client_resolver=lambda u=None: Fixed(),
    )
    await reloaded.load_existing_kbs()
    kb2 = await reloaded.get_kb("demo")
    settings2 = kb2.get_document_settings(doc_b)
    check("重载后仍生效", settings2["permissions"]["edit"] == "admin"
          and settings2["recall"]["enabled"] is False, str(settings2))

    print("\nU8  前端 API 往返")
    data, status = await api.update_kb_permissions(mgr, "demo", {
        "add": "admin", "edit": "whitelist", "delete": "open", "allow_users": ["7"]
    })
    check("KB 权限写入", status == 200 and data["permissions"]["add"] == "admin", str(data))
    data, status = await api.get_kb_permissions(mgr, "demo")
    check("KB 权限读取", data["permissions"]["allow_users"] == ["7"], str(data))
    data, status = await api.update_documents_permissions(mgr, "demo", {
        "doc_ids": [doc_a], "permissions": {"edit": "admin"}
    })
    check("批量权限写入", status == 200 and data["updated"] == 1, str(data))
    data, status = await api.update_kb_recall(mgr, "demo", {
        "enabled": True, "top_k": 4, "min_score": 0.5, "max_chars": 900
    })
    check("召回设置写入", status == 200 and data["recall"]["top_k"] == 4, str(data))
    mgr.recall_global = {"enabled": True, "mode": "hybrid", "top_k": 3,
                         "min_score": 0.35, "max_chars": 1200}
    data, status = api.get_recall_global(mgr)
    check("全局召回只读接口", status == 200 and data["enabled"] is True, str(data))

    await mgr.close_all()
    await reloaded.close_all()

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
