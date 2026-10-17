"""Lexical retrieval tests: title OR content, short queries, time switch."""

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
lex_mod = importlib.import_module("kbp.lexical")

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


class Fixed:
    async def embed(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


ROOT = Path("/tmp/kirakb_lexical_test")


async def main_async():
    shutil.rmtree(ROOT, ignore_errors=True)
    ROOT.mkdir(parents=True)

    async def getter():
        return Fixed()

    mgr = km_mod.KnowledgeBaseManager(
        base_dir=str(ROOT), embedding_client_getter=getter,
        chunk_size=120, chunk_overlap=20,
        client_resolver=lambda u=None: Fixed(),
    )
    kb = await mgr.create_kb("demo")
    d1 = await kb.add_raw_document("先装依赖，再启动。", "安装指南")
    d2 = await kb.add_raw_document("检查端口与磁盘。", "部署手册")
    vid = await kb.create_version("m", 4, None, None)
    await kb.set_active_version(vid)

    print("\nL1  标题 OR 内容")
    rows = await kb.lexical_search("安装", min_content_score=2)
    check("短词命中标题（安装 → 安装指南）",
          [r["doc_id"] for r in rows] == [d1], str(rows))
    check("标题命中标记", rows[0]["title_score"] > 0 and rows[0]["content_score"] == 0,
          str(rows[0]))
    rows = await kb.lexical_search("端口", min_content_score=2)
    check("内容命中（端口 → 部署手册）",
          [r["doc_id"] for r in rows] == [d2], str(rows))
    check("内容命中标记", rows[0]["title_score"] == 0 and rows[0]["content_score"] >= 2,
          str(rows[0]))
    rows = await kb.lexical_search("安装端口", min_content_score=2)
    check("标题/内容取并集（OR）", {r["doc_id"] for r in rows} == {d1, d2}, str(rows))
    rows = await kb.lexical_search("上次说的部署怎么弄", min_content_score=2)
    check("半截话命中", [r["doc_id"] for r in rows] == [d2], str(rows))
    rows = await kb.lexical_search("怎么什么", min_content_score=2)
    check("停用词查询不命中", rows == [], str(rows))

    print("\nL2  忽略时间开关")
    (kb.raw_docs_dir / "aaa.txt").write_text("内容", encoding="utf-8")
    (kb.raw_docs_dir / "bbb.txt").write_text("内容", encoding="utf-8")
    kb.write_doc_meta("aaa", {"original_name": "条目甲", "created_at": 300,
                              "updated_at": 300, "deleted": False})
    kb.write_doc_meta("bbb", {"original_name": "条目乙", "created_at": 100,
                              "updated_at": 100, "deleted": False})
    await kb.lexical.rebuild()
    rows = await kb.lexical_search("条目", ignore_time=True)
    check("默认忽略时间（同分不按更新时间）",
          [r["doc_id"] for r in rows[:2]] == ["bbb", "aaa"], str([r["doc_id"] for r in rows[:2]]))
    rows = await kb.lexical_search("条目", ignore_time=False)
    check("关闭忽略后新条目优先",
          [r["doc_id"] for r in rows[:2]] == ["aaa", "bbb"], str([r["doc_id"] for r in rows[:2]]))

    print("\nL3  索引随写入更新")
    d3 = await kb.add_raw_document("旧词内容", "杂项")
    check("新增后可搜内容", bool(await kb.lexical_search("旧词")))
    await kb.update_raw_document(d3, "新词内容")
    check("编辑后旧词消失", await kb.lexical_search("旧词") == [])
    check("编辑后新词出现", [r["doc_id"] for r in await kb.lexical_search("新词")] == [d3])
    await kb.update_raw_document(d3, "新词内容", original_name="改后的标题")
    check("重命名后新标题可搜", [r["doc_id"] for r in await kb.lexical_search("改后的标题")] == [d3],
          str(await kb.lexical_search("改后的标题")))
    await kb.delete_raw_document(d3, soft=True)
    check("软删后不可搜", await kb.lexical_search("新词") == [])
    await kb.restore_document(d3)
    check("恢复后可搜", [r["doc_id"] for r in await kb.lexical_search("新词")] == [d3])

    print("\nL4  历史回退同步词法")
    rows = kb.list_history(d3)
    original = rows[-1]["rev_id"]
    await kb.restore_revision(d3, original, actor="human")
    check("回退后旧内容重新可搜", [r["doc_id"] for r in await kb.lexical_search("旧词")] == [d3],
          str(await kb.lexical_search("旧词")))
    check("回退后新内容消失", await kb.lexical_search("新词") == [])

    print("\nL5  被动召回：标题/内容 OR")
    await kb.set_recall_settings({"enabled": True})
    cfg = {
        "enabled": True, "mode": "hybrid", "top_k": 3, "min_score": 0.35,
        "strong_score": 0.6, "max_chars": 1200, "timeout_ms": 1500,
        "dedupe_turns": 2, "max_kbs": 3, "min_query_chars": 2,
        "lexical_enabled": True, "lexical_min_score": 2, "ignore_time": True,
    }
    recall = importlib.import_module("kbp.recall")

    class Ev:
        messages = []
        self_id = "bot"

        def __init__(self, sid):
            self.sid = sid
            self.event_id = "e-" + sid

    eng = recall.RecallEngine(lambda: cfg, lambda: {"demo": kb})
    got = await eng.collect(event=Ev("s1"), query="安装", sid="s1", turn_key="t1")
    check("短词通过标题召回", any(h["doc_id"] == d1 for h in got), str(got))
    got = await eng.collect(event=Ev("s2"), query="端口", sid="s2", turn_key="t2")
    check("内容命中召回", any(h["doc_id"] == d2 for h in got), str(got))
    got = await eng.collect(event=Ev("s3"), query="今天天气", sid="s3", turn_key="t3")
    check("无信号不召回", got == [], str(got))

    print("\nL6  内存回退")
    mem = lex_mod.LexicalIndex(
        ROOT / "demo" / "lexical_mem.db", kb.raw_docs_dir,
        (str(Path("stopwords_default.txt")),), logger=None, force_memory=True,
    )
    ok = await mem.ensure_ready()
    check("内存索引可用", ok)
    hits = await mem.search("端口", min_content_score=2)
    check("内存索引内容命中", [h["doc_id"] for h in hits] == [d2], str(hits))

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
