"""Agentic RAG over the watsonx stack:
  tools = vector_search(OpenSearch) | graph_lookup(AstraDB KG) | sql_corpus(Presto/Iceberg)
  An LLM router (watsonx.ai granite) picks tools per question; another LLM call synthesizes the answer.
Usage: python agent.py "your question"
"""
import sys, json, re, rag_common as rc

def tool_vector(q, k=8):
    hits = rc.os_hybrid_search(q, k=k)   # semantic kNN + BM25 keyword, RRF-fused
    return [f"[{h['title']}#{h['chunk_no']}] {h['text']}" for h in hits], hits

def _tokens(q):
    # tokens: ASCII words >=3 chars (lowercased) + Korean runs >=2 chars
    return set(re.findall(r"[0-9a-z]{3,}|[가-힣]{2,}", q.lower()))

def _en(r, side):
    """Normalized endpoint name of an edge (uses stored *_norm, falls back to norm_name)."""
    return r.get(side + "_norm") or rc.norm_name(r.get(side))

def tool_graph(q, k=4, max_edges=16, max_ents=16):
    """Relevance-ranked, alias-merged subgraph over the FULL KG (paginated):
       score = explicit entity mention in the question (2) + semantically-seeded doc via vector (1).
       Names are normalized (norm_name) so 마이데이터 / 마이데이터 사업자 collapse to one node."""
    docs = rc.astra_find_all(rc.ASTRA_KG)                  # (1) full KG (no 20-doc page cap)
    ents = [d for d in docs if d.get("kind") == "entity"]
    edges = [d for d in docs if d.get("kind") == "edge"]
    qn = rc.norm_name(q)                                   # normalized question (spaces/parens stripped)
    toks = {t for t in (rc.norm_name(x) for x in _tokens(q)) if len(t) >= 2}

    seed_docs = set()                                     # (2) vector seed: semantically relevant docs
    try:
        for h in rc.os_vector_search(q, k=k):
            if h.get("doc_id"):
                seed_docs.add(h["doc_id"])
    except Exception:
        pass

    def name_hit(nn):                                     # nn is already normalized
        return len(nn) >= 2 and (nn in qn or any(t in nn or nn in t for t in toks))

    def score(r):                                        # (3) explicit endpoint mention (2) + doc scope (1)
        s = 0
        if name_hit(_en(r, "src")) or name_hit(_en(r, "dst")): s += 2
        if r.get("doc_id") in seed_docs: s += 1
        return s

    scored = sorted(((score(r), r) for r in edges), key=lambda sr: sr[0], reverse=True)  # stable
    kept = [r for s, r in scored if s > 0]
    if not kept:                                          # fallback: highest-degree subgraph (never arbitrary first-N)
        from collections import Counter
        deg = Counter()
        for r in edges:
            deg[_en(r, "src")] += 1; deg[_en(r, "dst")] += 1
        kept = sorted(edges, key=lambda r: deg[_en(r, "src")] + deg[_en(r, "dst")], reverse=True)[:max_edges]

    # canonical display name per normalized key (prefer the shortest variant seen)
    canon = {}
    def remember(name, nrm):
        if not nrm: return
        cur = canon.get(nrm)
        if cur is None or len(str(name)) < len(cur): canon[nrm] = name
    for e in ents:
        remember(e.get("name"), e.get("norm") or rc.norm_name(e.get("name")))
    for r in kept:
        remember(r.get("src"), _en(r, "src")); remember(r.get("dst"), _en(r, "dst"))
    etype = {(e.get("norm") or rc.norm_name(e.get("name"))): e.get("type") for e in ents}

    seen, hit_r, ent_order = set(), [], []                # dedup edges by (src_norm, rel, dst_norm)
    for r in kept:
        sn, dn = _en(r, "src"), _en(r, "dst")
        if sn == dn: continue                             # drop self-loops created by alias merge
        key = (sn, r.get("rel"), dn)
        if key in seen: continue
        seen.add(key)
        hit_r.append(f"{canon.get(sn, r.get('src'))} -[{r.get('rel')}]-> {canon.get(dn, r.get('dst'))}")
        ent_order += [sn, dn]
        if len(hit_r) >= max_edges: break

    seen_e, hit_e = set(), []                             # entities = endpoints of kept edges (dedup by norm)
    for nrm in ent_order:
        if not nrm or nrm in seen_e: continue
        seen_e.add(nrm)
        nm, ty = canon.get(nrm, nrm), etype.get(nrm)
        hit_e.append(f"{nm} ({ty})" if ty else nm)
        if len(hit_e) >= max_ents: break
    return hit_r, {"entities": hit_e, "edges": hit_r}

def tool_sql(q):
    if not rc.PRESTO_HOST: return ["(presto disabled)"], None
    t = f"{rc.PRESTO_CATALOG}.{rc.PRESTO_SCHEMA}.{rc.PRESTO_TABLE}"
    rows = rc.presto_exec(f"SELECT title, source, chunks, entities, edges FROM {t} ORDER BY chunks DESC")
    n = rc.presto_exec(f"SELECT count(*), sum(chunks) FROM {t}")
    ctx = [f"corpus: {n[0][0]} docs, {n[0][1]} chunks total"] + [f"- {r[0]} (chunks={r[2]}, entities={r[3]}, edges={r[4]})" for r in rows[:10]]
    return ctx, rows

def route(q):
    msg = [{"role": "system", "content": "You route a question to retrieval tools. Reply ONLY JSON like "
            '{"vector":true,"graph":false,"sql":false}. vector=semantic doc search; '
            "graph=entity/relationship questions; sql=counts/inventory of the indexed corpus."},
           {"role": "user", "content": q}]
    try:
        j = json.loads(re.search(r"\{.*\}", rc.wx_chat(msg, max_tokens=60), re.S).group(0))
    except Exception:
        j = {}
    j.setdefault("vector", True)  # always ground in docs
    return j

def run(q):
    """Structured agentic answer for the API/UI."""
    plan = route(q)
    ctx, chunks_raw, kg_raw, sql_raw = [], [], None, None
    citations = []
    if plan.get("vector"):
        c, hits = tool_vector(q); ctx += ["# Document passages"] + c
        chunks_raw = hits
        citations = [{"title": h["title"], "chunk_no": h["chunk_no"]} for h in hits]
    if plan.get("graph"):
        c, kg = tool_graph(q); ctx += ["# Knowledge graph"] + c; kg_raw = kg
    if plan.get("sql"):
        c, rows = tool_sql(q); ctx += ["# Corpus (SQL/Iceberg)"] + c; sql_raw = rows
    context = "\n".join(ctx)[:9000]   # fits ~8 hybrid chunks + graph/sql sections for granite-3-8b
    msg = [{"role": "system", "content": "Answer the question using ONLY the provided context "
            "(document passages, knowledge graph, corpus stats). Cite passage titles like [title#n]. "
            "If the context is insufficient, say so. Use concise markdown. "
            "Reply in the same language as the question; if the question is in Korean, answer in Korean (한국어)."},
           {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {q}"}]
    ans = rc.wx_chat(msg, max_tokens=500)
    return {"answer": ans,
            "route": {"vector": bool(plan.get("vector")), "graph": bool(plan.get("graph")), "sql": bool(plan.get("sql"))},
            "citations": citations,
            "context": {
                "chunks": [{"title": h["title"], "chunk_no": h["chunk_no"], "score": round(h["score"], 3),
                            "text": h["text"], "source": h.get("source", "")} for h in chunks_raw],
                "kg": kg_raw or {"entities": [], "edges": []},
                "sql": [list(r) for r in (sql_raw or [])],
            }}

def answer(q):
    return run(q)["answer"]

if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "What is Docling and what formats does it support?"
    print(f"Q: {q}\n")
    print(answer(q))
