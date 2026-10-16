"""知识库工具：运维手册（Runbook）的 BM25 检索与全文取回。

为什么检索要独立成两只工具（kb_search / runbook_get）：
search 只给摘要与命中分，get 才给完整处置步骤。规划器因此能表达
"先找到手册，再把手册的执行步骤取回来"这种两步意图，
而不是把整本手册塞进上下文——这是省 token 与可控性的共同要求。
"""
from __future__ import annotations

import re
from typing import Any

from .. import data_store
from ..algos import BM25Index, tokenize
from ..config import get_settings
from .ops import result

_INDEX: BM25Index | None = None
_INDEX_SIZE: int = -1


def _index() -> BM25Index:
    global _INDEX, _INDEX_SIZE
    books = data_store.runbooks()
    if _INDEX is not None and len(books) == _INDEX_SIZE:
        return _INDEX
    idx = BM25Index(k1=get_settings().BM25_K1, b=get_settings().BM25_B)
    for b in books:
        rid = b.get("id", b["file"])
        text = " ".join([
            str(b.get("title", "")), str(b.get("symptom", "")), str(b.get("cause", "")),
            str(b.get("tags", "")), str(b.get("severity", "")), b.get("body", "")[:4000],
        ])
        idx.add(rid, text, meta=b)
    _INDEX, _INDEX_SIZE = idx, len(books)
    return _INDEX


def reset_index() -> None:
    """数据集重新生成后由 seed 脚本/测试调用，避免用旧索引打分。"""
    global _INDEX, _INDEX_SIZE
    _INDEX, _INDEX_SIZE = None, -1


def _steps_of(body: str) -> list[str]:
    out = []
    for line in body.splitlines():
        m = re.match(r"^\s*(?:\d+[.)]|[-*])\s+(.*\S)\s*$", line)
        if m:
            out.append(m.group(1))
    return out


def kb_search(query: str, top_k: int) -> dict[str, Any]:
    """在运维手册里检索，返回带来源文件的命中清单（摘要 + 分数 + 处置步骤条数）。"""
    s = get_settings()
    k = max(1, min(int(top_k), 8)) if top_k else s.RAG_TOP_K
    idx = _index()
    if idx.size() == 0:
        return result("知识库为空（先运行 scripts/seed_demo.py）", "doc", "kb:empty", ok=False, error="empty_kb")
    scored = idx.score(query)[:k]
    refs = []
    for doc_id, score in scored:
        meta = idx.meta(doc_id)
        body = meta.get("body", "")
        snippet = re.sub(r"\s+", " ", body.strip())[:180]
        refs.append({
            "id": doc_id,
            "title": meta.get("title", doc_id),
            "source": f"runbooks/{meta.get('file', doc_id)}",
            "score": round(score, 3),
            "severity": meta.get("severity", ""),
            "symptom": meta.get("symptom", ""),
            "steps_count": len(_steps_of(body)),
            "snippet": snippet,
        })
    if not refs:
        return result(f"知识库无匹配「{query}」", "doc", "kb:none", total=0, refs=[], terms=len(tokenize(query)))
    lead = refs[0]
    summary = (f"运维手册命中 {len(refs)} 篇，最相关「{lead['title']}」（{lead['source']}，"
               f"score={lead['score']}，含 {lead['steps_count']} 步处置）")
    return result(summary, "doc", f"kb:{query[:40]}", total=len(refs), refs=refs,
                   terms=len(tokenize(query)), corpus=idx.size())


def runbook_get(runbook_id: str) -> dict[str, Any]:
    """按 id 取回手册全文处置步骤（供执行阶段照做，或写进报告/工单）。"""
    idx = _index()
    meta = idx.meta(runbook_id)
    if not meta:
        avail = [b.get("id", b["file"]) for b in data_store.runbooks()]
        return result(f"手册 {runbook_id!r} 不存在（可用：{', '.join(avail)}）",
                      "doc", f"runbook:{runbook_id}", ok=False, error="unknown_runbook")
    steps = _steps_of(meta.get("body", ""))
    summary = (f"《{meta.get('title', runbook_id)}》处置步骤 {len(steps)} 条，"
               f"适用症状：{meta.get('symptom', '-')[:50]}")
    return result(summary, "doc", f"runbooks/{meta.get('file', runbook_id)}",
                   id=runbook_id, title_meta=meta.get("title", ""),
                   severity=meta.get("severity", ""), symptom=meta.get("symptom", ""),
                   cause=meta.get("cause", ""), steps=steps)
