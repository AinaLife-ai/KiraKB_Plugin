"""Passive recall tests: trigger modes, filters, budget, dedupe, injection."""

import asyncio
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stub_core

stub_core.install()
import importlib

recall = importlib.import_module("kbp.recall")
main = importlib.import_module("kbp.main")

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


class FakeVersion:
    def __init__(self, hits, model_uuid="m"):
        self.hits = hits
        self.model_uuid = model_uuid
        self.status = "ready"
        self.calls = []

    async def embed_texts(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    async def search_cosine(self, query, top_k=3, allow_doc_ids=None, query_embedding=None):
        self.calls.append(set(allow_doc_ids) if allow_doc_ids is not None else None)
        rows = [
            hit for hit in self.hits
            if allow_doc_ids is None or str(hit["metadata"]["doc_id"]) in allow_doc_ids
        ]
        return rows[:top_k]


class FakeKB:
    def __init__(self, kb_id, name, docs, version, enabled=True):
        self.kb_id = kb_id
        self.display_name = name
        self._docs = docs
        self._version = version
        self._enabled = enabled

    def get_recall_settings(self):
        return {"enabled": self._enabled}

    def list_documents(self, include_deleted=False):
        return self._docs

    @property
    def active_version(self):
        return self._version


def doc(doc_id, name, enabled=None, keywords=None):
    return {
        "doc_id": doc_id,
        "name": name,
        "deleted": False,
        "recall": {"enabled": enabled, "keywords": keywords or []},
    }


def hit(doc_id, content, score=0.9):
    return {"metadata": {"doc_id": doc_id}, "content": content, "score": score,
            "doc_name": doc_id + ".txt"}


BASE = {
    "enabled": True, "mode": "hybrid", "top_k": 3, "min_score": 0.35,
    "strong_score": 0.60, "max_chars": 1200, "timeout_ms": 1500,
    "dedupe_turns": 2, "max_kbs": 3, "min_query_chars": 2,
}


def make_engine(kbs, **overrides):
    cfg = dict(BASE)
    cfg.update(overrides)
    return recall.RecallEngine(lambda: cfg, lambda: kbs, logger=None)


class Event:
    def __init__(self, messages, sid="webchat:dm:1"):
        self.messages = messages
        self.sid = sid
        self.event_id = f"evt-{sid}-{messages[0].text}"
        self.self_id = "bot"


class Sender:
    def __init__(self, uid):
        self.user_id = uid


class Chain:
    def __init__(self, text):
        self._text = text

    def get_plain_text(self):
        return self._text


class Msg:
    def __init__(self, text, uid="1"):
        self.chain = Chain(text)
        self.sender = Sender(uid)
        self.is_notice = False
        self.text = text


async def pure_tests():
    print("\nR0  纯函数")
    cfg = recall.normalise_config({"top_k": 99, "min_score": "x", "mode": "nope"})
    check("top_k 被钳制", cfg["top_k"] == 10, str(cfg["top_k"]))
    check("非法分数回退", cfg["min_score"] == 0.35, str(cfg["min_score"]))
    check("非法模式回退 hybrid", cfg["mode"] == "hybrid")

    entries = [doc("d1", "安装指南.txt"), doc("d2", "API 文档", keywords=["token"])]
    hits = recall.lexical_hits("请问安装指南在哪", entries)
    check("标题命中", hits == ["d1"], str(hits))
    hits = recall.lexical_hits("怎么拿 token", entries)
    check("关键词命中", hits == ["d2"], str(hits))
    check("无命中", recall.lexical_hits("今天天气不错", entries) == [])

    block = recall.render_block(
        [{"kb_id": "kb", "kb_name": "库", "doc_name": "指南", "content": "内容", "score": 0.8}],
        500,
    )
    check("注入块有包裹", block.startswith("<knowledge_recall>") and block.endswith("</knowledge_recall>"))
    check("注入块提示不被当指令", "不是指令" in block, block[:60])
    check("注入块无表情符号", all(ord(ch) < 0x1F000 for ch in block))


async def engine_tests():
    print("\nR1  触发模式")
    docs = [doc("d1", "安装指南"), doc("d2", "部署手册")]
    v = FakeVersion([hit("d1", "安装步骤"), hit("d2", "部署步骤")])
    kb = FakeKB("kb1", "知识库一", docs, v)
    engine = make_engine({"kb1": kb})

    got = await engine.collect(event=Event([Msg("嗯")]), query="嗯", sid="s1", turn_key="t1")
    check("太短不触发", got == [])

    got = await engine.collect(event=Event([Msg("/help")]), query="/help", sid="s1", turn_key="t2")
    check("命令不触发", got == [])

    got = await engine.collect(event=Event([Msg("安装指南")]), query="安装指南", sid="s1", turn_key="t3")
    check("mention 命中只查该条目", v.calls[-1] == {"d1"}, str(v.calls[-1]))
    check("返回命中内容", len(got) == 1 and got[0]["doc_id"] == "d1")
    check("条目名被还原", got[0]["doc_name"] == "安装指南", got[0]["doc_name"])

    v2 = FakeVersion([hit("d1", "安装步骤")])
    kb2 = FakeKB("kb2", "知识库二", docs, v2)
    engine_m = make_engine({"kb2": kb2}, mode="mention")
    got = await engine_m.collect(event=Event([Msg("今天天气不错")]), query="今天天气不错", sid="s2", turn_key="t4")
    check("mention 无词面命中不检索", got == [] and v2.calls == [])

    engine_s = make_engine({"kb2": kb2}, mode="semantic")
    got = await engine_s.collect(event=Event([Msg("随便聊聊")]), query="随便聊聊", sid="s3", turn_key="t5")
    check("semantic 每轮检索", v2.calls and got and got[0]["doc_id"] == "d1")

    # hybrid: long message without lexical hit -> all docs + strong threshold
    v3 = FakeVersion([hit("d1", "安装步骤", 0.7), hit("d2", "部署步骤", 0.4)])
    kb3 = FakeKB("kb3", "知识库三", docs, v3)
    engine_h = make_engine({"kb3": kb3}, mode="hybrid", strong_score=0.6)
    got = await engine_h.collect(event=Event([Msg("这个系统应该怎么开始使用呢")]),
                                 query="这个系统应该怎么开始使用呢", sid="s4", turn_key="t6")
    check("hybrid 长句走语义且用强门槛", len(got) == 1 and got[0]["doc_id"] == "d1", str(got))

    got = await engine_h.collect(event=Event([Msg("短句")]), query="短句", sid="s4b", turn_key="t6b")
    check("hybrid 短句无命中不检索", got == [])

    print("\nR2  条目三态与知识库开关")
    v4 = FakeVersion([hit("d1", "A"), hit("d2", "B")])
    docs_off = [doc("d1", "安装指南", enabled=False), doc("d2", "部署手册")]
    kb4 = FakeKB("kb4", "库四", docs_off, v4, enabled=True)
    engine4 = make_engine({"kb4": kb4}, mode="semantic")
    got = await engine4.collect(event=Event([Msg("看看")]), query="看看", sid="s5", turn_key="t7")
    check("条目 false 被排除", [h["doc_id"] for h in got] == ["d2"], str([h["doc_id"] for h in got]))

    v5 = FakeVersion([hit("d1", "A")])
    docs_force = [doc("d1", "安装指南", enabled=True)]
    kb5 = FakeKB("kb5", "库五", docs_force, v5, enabled=False)
    engine5 = make_engine({"kb5": kb5}, mode="semantic")
    got = await engine5.collect(event=Event([Msg("看看")]), query="看看", sid="s6", turn_key="t8")
    check("条目 true 可越过 KB 关闭", len(got) == 1 and got[0]["doc_id"] == "d1", str(got))

    print("\nR3  门槛 / 合并 / 预算 / 去重")
    v6 = FakeVersion([hit("d1", "低分", 0.2)])
    kb6 = FakeKB("kb6", "库六", [doc("d1", "安装指南")], v6, enabled=True)
    engine6 = make_engine({"kb6": kb6}, mode="semantic", min_score=0.35)
    got = await engine6.collect(event=Event([Msg("看看")]), query="看看", sid="s7", turn_key="t9")
    check("低于门槛被丢弃", got == [])

    va = FakeVersion([hit("a1", "A1", 0.9), hit("a2", "A2", 0.8)])
    vb = FakeVersion([hit("b1", "B1", 0.7)])
    kba = FakeKB("ka", "甲", [doc("a1", "甲一"), doc("a2", "甲二")], va, enabled=True)
    kbb = FakeKB("kb", "乙", [doc("b1", "乙一")], vb, enabled=True)
    engine7 = make_engine({"ka": kba, "kb": kbb}, mode="semantic", top_k=3)
    got = await engine7.collect(event=Event([Msg("看看")]), query="看看", sid="s8", turn_key="t10")
    check("多库名次轮转", [h["kb_id"] for h in got] == ["ka", "kb", "ka"], str([h["kb_id"] for h in got]))

    words = "长" * 60
    v8 = FakeVersion([hit("d1", words, 0.9), hit("d2", words, 0.8)])
    kb8 = FakeKB("kb8", "库八", [doc("d1", "一"), doc("d2", "二")], v8, enabled=True)
    engine8 = make_engine({"kb8": kb8}, mode="semantic", max_chars=100)
    got = await engine8.collect(event=Event([Msg("看看")]), query="看看", sid="s9", turn_key="t11")
    check("字符预算截断", len(got) == 1, str(len(got)))

    v9 = FakeVersion([hit("d9", "唯一内容", 0.9)])
    kb9 = FakeKB("kb9", "库九", [doc("d9", "唯一条目")], v9, enabled=True)
    engine9 = make_engine({"kb9": kb9}, mode="semantic")
    first = await engine9.collect(event=Event([Msg("看看")]), query="看看", sid="s9", turn_key="t12")
    second = await engine9.collect(event=Event([Msg("看看")]), query="看看", sid="s9", turn_key="t13")
    check("同会话去重窗口生效", len(first) == 1 and second == [], f"{len(first)}/{len(second)}")

    engine9b = make_engine({"kb9": kb9}, mode="semantic", dedupe_turns=0)
    await engine9b.collect(event=Event([Msg("看看")]), query="看看", sid="s10", turn_key="t14")
    got3 = await engine9b.collect(event=Event([Msg("看看")]), query="看看", sid="s10", turn_key="t15")
    check("dedupe_turns=0 允许重复", len(got3) == 1, str(len(got3)))

    engine10 = make_engine({"kb8": kb8}, mode="semantic", enabled=False)
    got4 = await engine10.collect(event=Event([Msg("看看")]), query="看看", sid="s11", turn_key="t15")
    check("总开关关闭不触发", got4 == [])


class SameVector:
    def __init__(self, dim=8):
        self.dim = dim

    async def embed(self, texts):
        return [[1.0] + [0.0] * (self.dim - 1) for _ in texts]


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


ROOT = Path("/tmp/kirakb_recall_test")


async def hook_tests():
    print("\nR4  钩子注入")
    shutil.rmtree(ROOT, ignore_errors=True)
    stub_core.sys.modules["core.utils.path_utils"].set_data_dir(ROOT)
    cfg = {
        "section_basic": {"chunk_size": 200, "chunk_overlap": 20,
                          "auto_install_dependencies": False},
        "section_webui": {"enable_webui": False},
        "section_permission": {},
        "section_recall": {"recall_enabled": True, "recall_mode": "semantic",
                           "recall_min_score": 0.3, "recall_timeout_ms": 800},
    }
    plugin = main.KiraKBPlugin(FakeCtx(), cfg)
    await plugin.initialize()
    kb = await plugin.kb_manager.create_kb("test")
    await kb.add_raw_document("安装步骤：先安装依赖，再启动。", "安装指南")
    vid = await kb.create_version("m", 8, None, None)
    await kb.set_active_version(vid)
    await kb.set_recall_settings({"enabled": True})

    req = main.LLMRequest()
    from core.prompt_manager import Prompt
    req.system_prompt.append(Prompt("SYSTEM_SENTINEL"))
    req.messages.append({"role": "system", "content": "SYSTEM_SENTINEL"})
    req.user_prompt.append(Prompt("用户正文"))
    event = Event([Msg("请问安装指南说了什么")])
    await plugin.inject_passive_recall(event, req)
    check("系统提示未被改写", req.system_prompt[0].content == "SYSTEM_SENTINEL",
          req.system_prompt[0].content)
    check("历史消息未被改写", req.messages == [{"role": "system", "content": "SYSTEM_SENTINEL"}],
          str(req.messages))
    check("注入了请求块", len(req.user_prompt) == 2, str(len(req.user_prompt)))
    block = req.user_prompt[0]
    check("注入块在最前", getattr(block, "name", "") == "kirakb_recall", getattr(block, "name", ""))
    check("注入块不持久化", getattr(block, "persist", True) is False)
    check("注入内容含条目名", "安装指南" in block.content, block.content[:80])
    check("原用户消息仍在", req.user_prompt[-1].content == "用户正文")

    req2 = main.LLMRequest()
    req2.user_prompt.append(Prompt("用户正文"))
    await plugin.inject_passive_recall(Event([Msg("请问安装指南又说了什么")]), req2)
    check("去重后第二轮不注入", len(req2.user_prompt) == 1, str(len(req2.user_prompt)))

    plugin.recall_config["enabled"] = False
    req3 = main.LLMRequest()
    req3.user_prompt.append(Prompt("用户正文"))
    await plugin.inject_passive_recall(Event([Msg("安装指南")], sid="s3"), req3)
    check("总开关关闭不注入", len(req3.user_prompt) == 1)

    plugin.recall_config["enabled"] = True
    plugin.recall_config["timeout_ms"] = 200
    original = plugin.recall_engine.collect

    async def slow(*args, **kwargs):
        await asyncio.sleep(0.6)
        return []

    plugin.recall_engine.collect = slow
    req4 = main.LLMRequest()
    req4.user_prompt.append(Prompt("用户正文"))
    start = time.monotonic()
    await plugin.inject_passive_recall(Event([Msg("安装指南")], sid="s4"), req4)
    elapsed = time.monotonic() - start
    check("超时快速跳过", elapsed < 0.5 and len(req4.user_prompt) == 1, f"{elapsed:.2f}s")
    plugin.recall_engine.collect = original

    await plugin.terminate()


async def main_async():
    await pure_tests()
    await engine_tests()
    await hook_tests()

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
