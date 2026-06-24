"""Shared clients for the agentic-RAG demo: watsonx.ai (embed+LLM), OpenSearch (vectors),
AstraDB (KG/docs via Data API), Presto/Iceberg (structured, via trino).
Config from env (.env). Designed to run from laptop (OpenSearch via port-forward) or in-cluster
(set OS_HOST to the cpd service DNS)."""
import os, json, time, requests, urllib3
urllib3.disable_warnings()

# ---- config ----
WX_URL   = os.environ["WX_URL"]
WX_APIKEY= os.environ["WX_APIKEY"]
WX_PROJECT_ID = os.environ["WX_PROJECT_ID"]
EMBED_MODEL = os.environ.get("WX_EMBED_MODEL", "ibm/granite-embedding-278m-multilingual")
LLM_MODEL   = os.environ.get("WX_LLM_MODEL",   "ibm/granite-3-8b-instruct")
EMBED_DIM   = int(os.environ.get("EMBED_DIM", "768"))

OS_URL  = os.environ.get("OS_URL", "https://localhost:9200")
OS_USER = os.environ.get("OS_USER", "admin")
OS_PASS = os.environ["OS_PASS"]
OS_INDEX= os.environ.get("OS_INDEX", "rag_chunks")

ASTRA_HOST  = os.environ["ASTRA_HOST"]
ASTRA_TOKEN = os.environ["ASTRA_TOKEN"]
ASTRA_KS    = os.environ.get("ASTRA_KEYSPACE", "default_keyspace")
ASTRA_KG    = os.environ.get("ASTRA_KG_COLLECTION", "kg")          # entities + edges
ASTRA_DOCS  = os.environ.get("ASTRA_DOCS_COLLECTION", "doc_registry")  # ingest tracking

DOCLING_URL = os.environ.get("DOCLING_URL", "")  # docling-serve route (https://.../)

# ---- watsonx.ai ----
_iam = {"tok": None, "exp": 0}
def wx_token():
    if _iam["tok"] and time.time() < _iam["exp"] - 60:
        return _iam["tok"]
    r = requests.post("https://iam.cloud.ibm.com/identity/token",
        data={"grant_type": "urn:ibm:params:oauth:grant-type:apikey", "apikey": WX_APIKEY}, timeout=30).json()
    _iam["tok"] = r["access_token"]; _iam["exp"] = time.time() + r.get("expires_in", 3600)
    return _iam["tok"]

def wx_embed(texts):
    r = requests.post(f"{WX_URL}/ml/v1/text/embeddings?version=2024-05-01",
        headers={"Authorization": f"Bearer {wx_token()}"},
        json={"model_id": EMBED_MODEL, "project_id": WX_PROJECT_ID, "inputs": texts}, timeout=120).json()
    if "results" not in r: raise RuntimeError(f"embed error: {r}")
    return [x["embedding"] for x in r["results"]]

def wx_chat(messages, max_tokens=512, temperature=0):
    """messages: [{"role":"system|user|assistant","content":"..."}] -> uses instruct chat template."""
    r = requests.post(f"{WX_URL}/ml/v1/text/chat?version=2024-05-01",
        headers={"Authorization": f"Bearer {wx_token()}"},
        json={"model_id": LLM_MODEL, "project_id": WX_PROJECT_ID, "messages": messages,
              "max_tokens": max_tokens, "temperature": temperature}, timeout=180).json()
    if "choices" not in r: raise RuntimeError(f"chat error: {r}")
    return r["choices"][0]["message"]["content"]

def wx_generate(prompt, max_new_tokens=512, temperature=0, system=None):
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    return wx_chat(msgs, max_tokens=max_new_tokens, temperature=temperature)

# ---- OpenSearch ----
def os_req(method, path, body=None, ndjson=None):
    ct = "application/x-ndjson" if ndjson is not None else "application/json"
    return requests.request(method, f"{OS_URL}{path}", auth=(OS_USER, OS_PASS), verify=False,
        headers={"Content-Type": ct}, data=(ndjson if ndjson is not None else (json.dumps(body) if body is not None else None)), timeout=120)

def os_ensure_index():
    if requests.head(f"{OS_URL}/{OS_INDEX}", auth=(OS_USER, OS_PASS), verify=False).status_code == 200:
        return
    # text uses the built-in `cjk` analyzer (bigrams Korean/CJK) -> far better BM25 recall on Korean
    # than the default `standard` analyzer (which keeps whole eojeol incl. particles). No plugin needed;
    # `nori` (true morphological analysis) isn't installable on this operator-managed cluster.
    mapping = {"settings": {"index": {"knn": True}},
        "mappings": {"properties": {
            "text": {"type": "text", "analyzer": "cjk"}, "doc_id": {"type": "keyword"}, "source": {"type": "keyword"},
            "title": {"type": "keyword"}, "chunk_no": {"type": "integer"},
            "vector": {"type": "knn_vector", "dimension": EMBED_DIM,
                       "method": {"name": "hnsw", "space_type": "cosinesimil", "engine": "lucene"}}}}}
    r = os_req("PUT", f"/{OS_INDEX}", mapping)
    r.raise_for_status()

_OS_SRC = ["text", "source", "title", "chunk_no", "doc_id"]

def _os_hits(body):
    res = os_req("POST", f"/{OS_INDEX}/_search", body).json()
    return res.get("hits", {}).get("hits", [])

def os_vector_search(query, k=4):
    qv = wx_embed([query])[0]
    hits = _os_hits({"size": k, "query": {"knn": {"vector": {"vector": qv, "k": k}}}, "_source": _OS_SRC})
    return [{"score": h["_score"], **h["_source"]} for h in hits]

def os_hybrid_search(query, k=4, rrf_k=60, pool=None):
    """Hybrid retrieval = semantic kNN (granite vectors) + BM25 keyword, fused with
    Reciprocal Rank Fusion. RRF needs no score normalization (it ranks, not scores),
    so it is robust to the two engines' incomparable score scales. BM25 sharpens rare
    tokens (약어/법령명: STR, VASP, 전자금융감독규정) that dense vectors blur."""
    pool = pool or max(k * 3, 10)
    qv = wx_embed([query])[0]
    knn = _os_hits({"size": pool, "query": {"knn": {"vector": {"vector": qv, "k": pool}}}, "_source": _OS_SRC})
    bm25 = _os_hits({"size": pool, "query": {"match": {"text": query}}, "_source": _OS_SRC})
    fused = {}
    for hits in (knn, bm25):
        for rank, h in enumerate(hits):
            e = fused.setdefault(h["_id"], {"rrf": 0.0, "src": h["_source"]})
            e["rrf"] += 1.0 / (rrf_k + rank + 1)
    ranked = sorted(fused.values(), key=lambda e: e["rrf"], reverse=True)[:k]
    return [{"score": round(e["rrf"], 5), **e["src"]} for e in ranked]

# ---- AstraDB Data API ----
def astra(cmd, coll=None):
    url = f"{ASTRA_HOST}/api/json/v1/{ASTRA_KS}" + (f"/{coll}" if coll else "")
    return requests.post(url, headers={"Token": ASTRA_TOKEN, "Content-Type": "application/json"},
        json=cmd, timeout=60).json()

def astra_find_all(coll, flt=None):
    """Page through the Data API (default 20 docs/page) collecting every doc."""
    out, ps = [], None
    while True:
        opts = {} if ps is None else {"pageState": ps}
        r = astra({"find": {"filter": flt or {}, "options": opts}}, coll).get("data", {})
        out += r.get("documents", [])
        ps = r.get("nextPageState")
        if not ps:
            break
    return out

# ---- KG entity-name normalization (lightweight alias merge) ----
import re as _re
_NORM_SUFFIX = ("사업자", "회사")            # conservative: 마이데이터 사업자 -> 마이데이터
_ALIASES = {                                  # manual canonicalization (normalized key -> canonical)
    "fiu": "금융정보분석원", "금융정보분석원fiu": "금융정보분석원",
    "vasp": "가상자산사업자", "가상자산사업자vasp": "가상자산사업자",
    "kisa": "한국인터넷진흥원", "한국인터넷진흥원kisa": "한국인터넷진흥원",
    "ciso": "정보보호최고책임자", "정보보호최고책임자ciso": "정보보호최고책임자",
    "마이데이터본인신용정보관리업": "마이데이터", "본인신용정보관리업": "마이데이터",
    "본인신용정보관리회사": "마이데이터",
}
def norm_name(s):
    """Normalize an entity name for matching/dedup: lowercase, drop (parentheticals) & spaces,
    strip a few business suffixes, then map known aliases to a canonical form."""
    n = (s or "").strip().lower()
    n = _re.sub(r"\(.*?\)", "", n)           # drop parentheticals e.g. (FIU)
    n = _re.sub(r"\s+", "", n)               # drop whitespace
    for suf in _NORM_SUFFIX:
        if n.endswith(suf) and len(n) > len(suf) + 1:
            n = n[: -len(suf)]
    return _ALIASES.get(n, n)

def astra_delete_all(coll, flt, max_loops=50):
    """Data API deleteMany removes <=20 docs/call -> loop until none match (else large KGs leave orphans)."""
    deleted = 0
    for _ in range(max_loops):
        astra({"deleteMany": {"filter": flt}}, coll)
        r = astra({"find": {"filter": flt, "options": {"limit": 1}}}, coll).get("data", {})
        if not r.get("documents"):
            break
        deleted += 1
    return deleted

def cosine(a, b):
    """Cosine similarity of two equal-length float vectors (app-side; KG is small)."""
    if not a or not b: return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0

def kg_vector_seed(qvec, ents, k=8):
    """Rank KG entity docs (each with an 'emb' field) by cosine vs qvec; return top-k docs.
    NOTE: this AstraDB instance's on-disk SAI format predates vector indexes, so semantic
    seeding is computed app-side (the KG is small). Production: a vector-capable store + ANN."""
    scored = [(cosine(qvec, e.get("emb") or []), e) for e in ents if e.get("emb")]
    scored.sort(key=lambda se: se[0], reverse=True)
    return [e for s, e in scored[:k]]

def astra_ensure():
    for c in (ASTRA_KG, ASTRA_DOCS):
        astra({"createCollection": {"name": c}})  # plain collections (no server-side vector index here)

def astra_drop(coll):
    return astra({"deleteCollection": {"name": coll}})

# ---- watsonx.data Presto / Iceberg (structured corpus table + SQL tool) ----
PRESTO_HOST = os.environ.get("PRESTO_HOST", "")
PRESTO_USER = os.environ.get("PRESTO_USER", "ibmlhapikey_cpadmin")
PRESTO_PW   = os.environ.get("PRESTO_PW", os.environ.get("WXD_API_KEY", ""))
PRESTO_CATALOG = os.environ.get("PRESTO_CATALOG", "iceberg_data")
PRESTO_SCHEMA  = os.environ.get("PRESTO_SCHEMA", "rag")
PRESTO_TABLE   = "corpus"

def presto_conn():
    # watsonx.data is PrestoDB (X-Presto-User header) -> use presto-python-client, NOT trino
    import prestodb
    conn = prestodb.dbapi.connect(host=PRESTO_HOST, port=443, http_scheme="https",
        user=PRESTO_USER, auth=prestodb.auth.BasicAuthentication(PRESTO_USER, PRESTO_PW),
        catalog=PRESTO_CATALOG, schema=PRESTO_SCHEMA)
    try: conn._http_session.verify = False
    except Exception: pass
    return conn

def presto_exec(sql):
    cur = presto_conn().cursor(); cur.execute(sql)
    try: return cur.fetchall()
    except Exception: return []

def presto_query(sql):
    """Run a SELECT and return (columns, rows) — column names from cursor.description (for text2sql)."""
    cur = presto_conn().cursor(); cur.execute(sql)
    rows = cur.fetchall()
    cols = [d[0] for d in (cur.description or [])]
    return cols, rows

def iceberg_ensure():
    presto_exec(f"CREATE SCHEMA IF NOT EXISTS {PRESTO_CATALOG}.{PRESTO_SCHEMA}")
    presto_exec(f"""CREATE TABLE IF NOT EXISTS {PRESTO_CATALOG}.{PRESTO_SCHEMA}.{PRESTO_TABLE} (
        doc_id varchar, title varchar, source varchar, chunks integer, entities integer, edges integer
    ) WITH (format='PARQUET')""")

def iceberg_upsert_doc(doc_id, title, source, chunks, entities, edges):
    t = f"{PRESTO_CATALOG}.{PRESTO_SCHEMA}.{PRESTO_TABLE}"
    esc = lambda x: str(x).replace("'", "''")
    presto_exec(f"DELETE FROM {t} WHERE doc_id='{esc(doc_id)}'")
    presto_exec(f"INSERT INTO {t} VALUES ('{esc(doc_id)}','{esc(title)}','{esc(source)}',{int(chunks)},{int(entities)},{int(edges)})")

# ---- document-derived structured table (the SQL tool's "third form" of an ingested doc) ----
# Each ingested regulatory doc yields a small obligations table (party/obligation/article/penalty),
# extracted by the LLM at ingest time. text2sql can then query it alongside the AML dataset, so the
# same document is genuinely available as vector (OpenSearch), graph (KG) AND structured rows (here).
OBLIG_TABLE = "obligations"
_OBLIG_COLS = ("doc_id", "law", "party", "obligation", "article", "penalty_text", "penalty_krw")

def obligations_ensure():
    presto_exec(f"CREATE SCHEMA IF NOT EXISTS {PRESTO_CATALOG}.{PRESTO_SCHEMA}")
    presto_exec(f"""CREATE TABLE IF NOT EXISTS {PRESTO_CATALOG}.{PRESTO_SCHEMA}.{OBLIG_TABLE} (
        doc_id varchar, law varchar, party varchar, obligation varchar,
        article varchar, penalty_text varchar, penalty_krw double
    ) WITH (format='PARQUET')""")

def obligations_upsert(doc_id, rows):
    """Idempotent per-doc upsert: delete this doc's rows, then bulk-insert the new ones."""
    t = f"{PRESTO_CATALOG}.{PRESTO_SCHEMA}.{OBLIG_TABLE}"
    esc = lambda x: str(x).replace("'", "''")
    presto_exec(f"DELETE FROM {t} WHERE doc_id='{esc(doc_id)}'")
    if not rows:
        return
    def lit(v):
        if v is None or v == "":
            return "NULL"
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return str(v)
        return "'" + esc(v) + "'"
    vals = ",".join("(" + ",".join(lit(r.get(c)) for c in _OBLIG_COLS) + ")" for r in rows)
    presto_exec(f"INSERT INTO {t} VALUES {vals}")
