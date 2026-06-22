"""Agentic RAG over the watsonx stack:
  tools = vector_search(OpenSearch) | graph_lookup(AstraDB KG) | sql_corpus(Presto/Iceberg)
  An LLM router (watsonx.ai granite) picks tools per question; another LLM call synthesizes the answer.
Usage: python agent.py "your question"
"""
import sys, json, re, rag_common as rc
import text2sql

def tool_vector(q, k=8):
    hits = rc.os_hybrid_search(q, k=k)   # semantic kNN + BM25 keyword, RRF-fused
    return [f"[{h['title']}#{h['chunk_no']}] {h['text']}" for h in hits], hits

def _tokens(q):
    # tokens: ASCII words >=3 chars (lowercased) + Korean runs >=2 chars
    return set(re.findall(r"[0-9a-z]{3,}|[가-힣]{2,}", q.lower()))

def _en(r, side):
    """Normalized endpoint name of an edge (uses stored *_norm, falls back to norm_name)."""
    return r.get(side + "_norm") or rc.norm_name(r.get(side))

def tool_graph(q, k=8, max_edges=16, max_ents=16):
    """Vector-seeded, alias-merged 1-hop subgraph over the AstraDB KG:
       (1) seed entities by semantic similarity ($vector), (2) expand to their 1-hop edges,
       (3) rank by explicit mention (2) + seed membership (1). Falls back to the full edge set.
       Names are normalized (norm_name + entity resolution) so aliases collapse to one node."""
    qn = rc.norm_name(q)                                   # normalized question (spaces/parens stripped)
    toks = {t for t in (rc.norm_name(x) for x in _tokens(q)) if len(t) >= 2}

    ents = rc.astra_find_all(rc.ASTRA_KG, {"kind": "entity"})   # entity catalog (small; names/types + emb)
    seed_norms = set()                                     # (1) vector seed: semantically similar entities
    try:
        qv = rc.wx_embed([q])[0]                            # app-side cosine (this AstraDB lacks ANN)
        for e in rc.kg_vector_seed(qv, ents, k=k):
            if e.get("norm"): seed_norms.add(e["norm"])
    except Exception:
        pass

    edges = []
    if seed_norms:                                         # (2) 1-hop: edges touching a seed entity
        S = list(seed_norms)
        edges = rc.astra_find_all(rc.ASTRA_KG,
            {"kind": "edge", "$or": [{"src_norm": {"$in": S}}, {"dst_norm": {"$in": S}}]})
    if not edges:                                          # fallback: whole edge set, then rank
        edges = rc.astra_find_all(rc.ASTRA_KG, {"kind": "edge"})

    def name_hit(nn):                                     # nn is already normalized
        return len(nn) >= 2 and (nn in qn or any(t in nn or nn in t for t in toks))

    def score(r):                                        # (3) explicit endpoint mention (2) + seed scope (1)
        s = 0
        if name_hit(_en(r, "src")) or name_hit(_en(r, "dst")): s += 2
        if _en(r, "src") in seed_norms or _en(r, "dst") in seed_norms: s += 1
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

def _sql_obj(query, columns, rows):
    return {"query": query, "columns": columns, "rows": [list(r) for r in rows]}

def _corpus_inventory():
    """Fallback SQL tool: inventory of the indexed RAG corpus (when text2sql yields nothing)."""
    t = f"{rc.PRESTO_CATALOG}.{rc.PRESTO_SCHEMA}.{rc.PRESTO_TABLE}"
    sql = f"SELECT title, source, chunks, entities, edges FROM {t} ORDER BY chunks DESC LIMIT 10"
    cols, rows = rc.presto_query(sql)
    n = rc.presto_exec(f"SELECT count(*), sum(chunks) FROM {t}")
    ctx = [f"corpus: {n[0][0]} docs, {n[0][1]} chunks total"] + \
          [f"- {r[0]} (chunks={r[2]}, entities={r[3]}, edges={r[4]})" for r in rows[:10]]
    return ctx, _sql_obj(sql, cols, rows)

def tool_sql(q):
    """text-to-SQL over the AML business dataset; falls back to corpus inventory on empty/error."""
    if not rc.PRESTO_HOST: return ["(presto disabled)"], None
    try:
        r = text2sql.run_text2sql(q)
    except Exception as e:
        r = {"sql": None, "columns": [], "rows": [], "error": str(e)[:200]}
    if r.get("error") is None and r.get("sql"):       # valid query (show it even with 0 rows)
        cols, rows = r.get("columns", []), r.get("rows", [])
        preview = [", ".join(f"{c}={v}" for c, v in zip(cols, row)) for row in rows[:30]]
        ctx = [f"SQL: {r['sql']}", f"({len(rows)} rows; columns: {', '.join(cols)})"] + \
              ([f"- {p}" for p in preview] or ["(no rows returned)"])
        return ctx, _sql_obj(r["sql"], cols, rows)
    try:                                              # fallback only on error: corpus inventory
        ctx, obj = _corpus_inventory()
        ctx = [f"(text2sql failed: {r.get('error')} — showing corpus inventory)"] + ctx
        return ctx, obj
    except Exception as e:
        return [f"(sql error: {str(e)[:120]})"], None

def route(q):
    msg = [{"role": "system", "content": "You route a question to retrieval tools. Reply ONLY JSON like "
            '{"vector":true,"graph":false,"sql":false}. Multiple tools may be true.\n'
            "- vector: meaning/definition/content of the regulatory documents (laws, regulations, "
            "concepts such as 가명정보, 접근매체, 의심거래보고). Default ON for any document/concept question.\n"
            "- graph: relationships or connections between entities (e.g. 'X와 Y의 관계', 감독기관·보고 "
            "대상·적용 대상이 무엇인지, 누가 누구를 규제/감독하는지).\n"
            "- sql: ONLY the AML business DATASET — customers, accounts, transactions, suspicious-"
            "transaction reports (STR): counts, sums, amounts, risk ratings, flagged transactions, "
            "counterparty countries. Do NOT use sql for questions about laws or concepts.\n"
            "When in doubt, set vector true."},
           {"role": "user", "content": q}]
    try:
        j = json.loads(re.search(r"\{.*\}", rc.wx_chat(msg, max_tokens=60), re.S).group(0))
    except Exception:
        j = {}
    j.setdefault("vector", True)  # always ground in docs
    # backstop: granite under-selects graph -> force it for relationship/structure-style questions
    if _GRAPH_CUES.search(q):
        j["graph"] = True
    return j

_GRAPH_CUES = re.compile(r"관계|관련|연결|감독|규제|적용\s*대상|보고\s*대상|소관|의무|책임|누가|상호|체계")

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
        c, sql_obj = tool_sql(q); ctx += ["# SQL (watsonx.data / Iceberg)"] + c; sql_raw = sql_obj
    context = "\n".join(ctx)[:9000]   # fits ~8 hybrid chunks + graph/sql sections for granite-3-8b
    msg = [{"role": "system", "content": "Answer the question using ONLY the provided context "
            "(document passages, knowledge graph, SQL results). Cite passage titles like [title#n]. "
            "If the context is insufficient, say so. Present only values that appear in the context — "
            "NEVER invent or pad with placeholder rows (if fewer results exist than asked, show only those). "
            "Use concise markdown. "
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
                "sql": sql_raw or {"query": None, "columns": [], "rows": []},
            }}

def answer(q):
    return run(q)["answer"]

if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "What is Docling and what formats does it support?"
    print(f"Q: {q}\n")
    print(answer(q))
