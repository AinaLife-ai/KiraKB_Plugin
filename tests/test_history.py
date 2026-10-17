"""Revision history tests: original retention, pruning, restore, frozen policy."""

import asyncio
import json
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
    async def embed(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


ROOT = Path("/tmp/kirakb_history_test")


async def make_mgr():
    shutil.rmtree(ROOT, ignore_errors=True)
    ROOT.mkdir(parents=True)

    async def getter():
        return Fixed()

    def resolver(model_uuid=None):
        return Fixed()

    return km_mod.KnowledgeBaseManager(
        base_dir=str(ROOT), embedding_client_getter=getter,
        chunk_size=80, chunk_overlap=10,
        client_resolver=resolver, history_keep=5,
    )


async def main_async():
    mgr = await make_mgr()
    kb = await mgr.create_kb("demo")

    print("\nH1  创建即记录原始版本")
    doc_id = await kb.add_raw_document("原始内容", "条目", actor="human")
    rows = kb.list_history(doc_id)
    check("初始 1 个版本", len(rows) == 1, str(len(rows)))
    check("标记为原始", rows[0]["is_original"] and rows[0]["is_current"], str(rows[0]))
    check("原始内容可读", kb.get_revision(doc_id, rows[0]["rev_id"])["content"] == "原始内容")
    original_rev = rows[0]["rev_id"]

    print("\nH2  编辑保留历史并标记来源")
    await kb.update_raw_document(doc_id, "第二版", actor="ai")
    await kb.update_raw_document(doc_id, "第三版", actor="human")
    rows = kb.list_history(doc_id)
    check("共 3 个版本", len(rows) == 3, str(len(rows)))
    check("最新在前", rows[0]["actor"] == "human", str(rows[0]))
    check("AI 来源被记录", any(r["actor"] == "ai" for r in rows))
    check("原始版本仍在", any(r["rev_id"] == original_rev and r["is_original"] for r in rows))
    check("edited 标记", kb.get_document_settings(doc_id)["history"]["edited"] is True)
    check("AI 编辑标记持久", kb.get_document_settings(doc_id)["history"]["ai_edited"] is True)
    ai_doc = await kb.add_raw_document("AI 创建的内容", "AI 条目", actor="ai")
    check("AI 创建不算编辑", kb.get_document_settings(ai_doc)["history"]["ai_edited"] is False)

    print("\nH3  回退到最初版本（不新增、不挤占备份）")
    before = len(kb.list_history(doc_id))
    content = await kb.restore_revision(doc_id, original_rev, actor="human")
    check("回退内容正确", content == "原始内容", str(content))
    check("当前内容已替换", (await kb.get_raw_document(doc_id)) == "原始内容")
    rows = kb.list_history(doc_id)
    check("回退不新增修订", len(rows) == before, f"{before}->{len(rows)}")
    current = [r for r in rows if r["is_current"]]
    check("当前指针移到目标版本", len(current) == 1 and current[0]["rev_id"] == original_rev,
          str(current))
    check("回退事件被记录", current and current[0]["last_restored_at"] and current[0]["restore_count"] == 1,
          str(current[0] if current else None))
    check("原始版本永久保留", any(r["rev_id"] == original_rev for r in rows))
    check("内容回到原始后 edited=false", kb.get_document_settings(doc_id)["history"]["edited"] is False)
    check("AI 编辑标记仍保留", kb.get_document_settings(doc_id)["history"]["ai_edited"] is True)

    print("\nH4  裁剪保留最近 N 份")
    await kb.set_history_keep(2)
    for i in range(5):
        await kb.update_raw_document(doc_id, f"批量编辑 {i}", actor="ai")
    rows = kb.list_history(doc_id)
    check("最多 原始+2 份", len(rows) <= 3, str(len(rows)))
    check("原始仍在", any(r["is_original"] for r in rows), str(len(rows)))
    check("当前仍在", any(r["is_current"] for r in rows), str(len(rows)))
    check("KB 覆盖值生效", kb.get_history_keep() == 2, str(kb.get_history_keep()))

    print("\nH5  旧条目首次编辑补记原始版本")
    legacy_id = "legacy01"
    (kb.raw_docs_dir / f"{legacy_id}.txt").write_text("旧条目内容", encoding="utf-8")
    (kb.raw_docs_dir / f"{legacy_id}.meta.json").write_text(json.dumps({
        "original_name": "旧条目", "created_at": 1, "updated_at": 1, "deleted": False
    }), encoding="utf-8")
    await kb.update_raw_document(legacy_id, "旧条目改过", actor="ai")
    rows = kb.list_history(legacy_id)
    check("旧条目补出原始版本", len(rows) == 2, str(len(rows)))
    check("原始内容为改前内容",
          kb.get_revision(legacy_id, rows[-1]["rev_id"])["content"] == "旧条目内容",
          str(rows[-1]))

    print("\nH6  API 往返与冻结策略")
    data, status = await api.list_history(mgr, "demo", doc_id)
    check("历史列表 API", status == 200 and len(data["revisions"]) >= 2, str(status))
    rev = data["revisions"][-1]["rev_id"]
    data, status = await api.get_history_revision(mgr, "demo", doc_id, rev)
    check("读取历史 API", status == 200 and data.get("content"), str(status))
    before = len(kb.list_history(doc_id))
    data, status = await api.restore_history_revision(mgr, "demo", doc_id, rev)
    check("回退 API", status == 200 and data.get("ok"), str(data))
    check("API 回退不新增修订", len(kb.list_history(doc_id)) == before,
          f"{before}->{len(kb.list_history(doc_id))}")

    await kb.set_permissions({"edit": "off"})
    data, status = await api.update_document(mgr, "demo", doc_id, {"content": "冻结后编辑"})
    check("冻结后人工编辑被拒", status == 403, str(status))
    data, status = await api.restore_history_revision(mgr, "demo", doc_id, rev)
    check("冻结后回退被拒", status == 403, str(status))

    print("\nH7  硬删除清理历史目录")
    history_dir = kb._history_dir(doc_id)
    check("历史目录存在", history_dir.exists())
    await kb.delete_raw_document(doc_id, soft=False)
    check("硬删后历史目录移除", not history_dir.exists())

    await mgr.close_all()

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
