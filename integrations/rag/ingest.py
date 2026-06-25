"""Ingest a document into the agentic-RAG stores (idempotent upsert for incremental/CronJob/UI):
  text | file(docling) | url(docling) -> chunk -> watsonx.ai embed -> OpenSearch (vectors)
  + AstraDB doc_registry (hash-based skip) + AstraDB kg (LLM-extracted entities/edges, normalized)
  + watsonx.data Iceberg corpus (doc inventory).

Library API (used by app.py): ingest_source(...) / delete_doc(doc_id) / list_docs().
CLI: python ingest.py <url-or-localpath> [--title T] [--force]
"""
import sys, os, re, json, hashlib, argparse, requests, urllib3
import rag_common as rc
urllib3.disable_warnings()

def parse_docling(source):
    body = {"sources": [{"kind": "http", "url": source}], "options": {"to_formats": ["md"]}}
    r = requests.post(f"{rc.DOCLING_URL}/v1/convert/source", json=body, verify=False, timeout=600).json()
    doc = r.get("document", r)
    md = doc.get("md_content") or doc.get("markdown") or doc.get("text_content") or ""
    if not md:
        raise RuntimeError(f"no markdown from docling-serve: keys={list(doc.keys())}")
    return md

def parse_docling_file(file_bytes, filename):
    files = {"files": (filename, file_bytes)}
    data = {"to_formats": "md"}
    r = requests.post(f"{rc.DOCLING_URL}/v1/convert/file", files=files, data=data, verify=False, timeout=600).json()
    doc = r.get("document") or (r.get("documents") or [{}])[0] or r
    md = doc.get("md_content") or doc.get("markdown") or doc.get("text_content") or ""
    if not md:
        raise RuntimeError(f"no markdown from docling file convert: keys={list(doc.keys()) if isinstance(doc, dict) else type(doc)}")
    return md

_BINARY_EXT = (".pdf", ".docx", ".pptx", ".png", ".jpg", ".jpeg", ".html", ".htm")

def load_source(source):
    """Local file path -> read markdown directly, or convert binary (pdf/docx/...) via docling;
    otherwise treat as a URL and fetch+parse via docling."""
    if os.path.exists(source):
        if source.lower().endswith(_BINARY_EXT):
            with open(source, "rb") as f:
                return parse_docling_file(f.read(), os.path.basename(source))
        with open(source, encoding="utf-8") as f:
            return f.read()
    return parse_docling(source)

def clean_md(md):
    """Strip non-text noise from converted markdown so chunks/embeddings/preview stay clean:
    - docling image placeholders (<!-- 🖼️❌ Image not available... -->) and any HTML comments
    - markdown images ![alt](url) -> keep alt text only (drop badge/screenshot URLs)
    - leftover empty markdown links []() and runs of blank lines"""
    s = re.sub(r"<!--.*?-->", "", md or "", flags=re.S)          # HTML comments (incl. docling image placeholder)
    s = re.sub(r"!\[([^\]]*)\]\([^)]*\)", lambda m: m.group(1), s)  # images -> alt text
    s = re.sub(r"\[\s*\]\([^)]*\)", "", s)                        # empty-text links left over
    s = re.sub(r"[ \t]+\n", "\n", s)                             # trailing spaces
    return re.sub(r"\n{3,}", "\n\n", s).strip()

def chunk(md, size=640, overlap=120):   # ~<450 tokens/chunk for Korean (granite-embed cap = 512)
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    out, i = [], 0
    while i < len(md):
        out.append(md[i:i+size]); i += size - overlap
    return [c.strip() for c in out if c.strip()]

KG_RELS = ("regulates", "supervised_by", "reports_to", "based_on", "complies_with", "defines",
           "part_of", "applies_to", "issued_by", "requires", "operated_by", "oversees",
           "has_obligation", "related_to")

# Entity-type ontology (controlled vocabulary) — keeps node types consistent across documents.
KG_TYPES = ("law", "regulation", "regulator", "institution", "scheme", "obligation",
            "data_subject", "system", "service_provider", "data", "concept")
_TYPE_ALIASES = {
    "법률": "law", "법령": "law", "법": "law", "act": "law", "statute": "law",
    "규정": "regulation", "규칙": "regulation",
    "규제기관": "regulator", "감독기관": "regulator", "supervisor": "regulator", "authority": "regulator",
    "기관": "institution", "organization": "institution", "agency": "institution", "company": "institution",
    "제도": "scheme", "program": "scheme",
    "의무": "obligation", "duty": "obligation", "requirement": "obligation",
    "정보주체": "data_subject", "개인": "data_subject", "individual": "data_subject",
    "시스템": "system", "기술": "system", "technology": "system",
    "사업자": "service_provider", "provider": "service_provider", "business": "service_provider",
    "데이터": "data", "정보": "data",
}

def _clean_type(t):
    """Map a free-form entity type onto the controlled KG_TYPES vocabulary."""
    s = str(t or "").strip().lower()
    if s in KG_TYPES: return s
    s = re.sub(r"\(.*?\)", "", s).strip()
    if s in _TYPE_ALIASES: return _TYPE_ALIASES[s]
    for k, v in _TYPE_ALIASES.items():
        if k in s: return v
    return "concept"

def extract_kg(text, doc_id):
    rels = ", ".join(KG_RELS)
    types = ", ".join(KG_TYPES)
    prompt = (
        "Extract a knowledge graph from the text below. Return ONLY compact JSON (no markdown, no prose):\n"
        '{"entities":[{"name":"..","type":".."}],"edges":[{"src":"..","rel":"..","dst":".."}]}\n'
        "Rules:\n"
        "- Every edge's src and dst MUST be an entity name from the entities list (canonical, concise).\n"
        f"- rel MUST be exactly one snake_case label from this set (pick the closest meaning): {rels}.\n"
        f"- type MUST be exactly one label from this set (pick the closest meaning): {types}.\n"
        "- NEVER use a particle/postposition/conjunction/sentence-fragment as rel "
        "(e.g. not '의','는','을','로','표준 API 방식으로'). rel is always a relationship verb.\n"
        "- Use canonical, consistent entity names (e.g. '마이데이터', not '마이데이터 사업자'; "
        "'금융정보분석원', not '금융정보분석원(FIU)').\n"
        "- At most 12 entities and 12 edges.\n"
        "Text:\n" + text[:3500])
    raw = rc.wx_generate(prompt, max_new_tokens=1800)
    m = re.search(r"\{.*\}", raw, re.S)
    if not m: return [], []
    try: g = json.loads(m.group(0))
    except Exception: return [], []
    ents = [{"name": e.get("name"), "type": _clean_type(e.get("type"))}
            for e in g.get("entities", []) if e.get("name")]
    edges = []
    for e in g.get("edges", []):
        if e.get("src") and e.get("dst"):
            edges.append({"src": e["src"], "rel": _clean_rel(e.get("rel")), "dst": e["dst"]})
    return ents, edges

def _parse_rows(raw):
    """Parse an LLM extraction into a list of row dicts. Robust to the model returning either
    {"rows":[...]}, a bare [...] array, or output wrapped in a ```json fence."""
    s = (raw or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.S | re.I)
    if m:
        s = m.group(1).strip()
    try:
        obj = json.loads(s)
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict) and isinstance(obj.get("rows"), list):
            return obj["rows"]
    except Exception:
        pass
    m = re.search(r"\[.*\]", s, re.S)              # first JSON array anywhere
    if m:
        try:
            arr = json.loads(m.group(0))
            if isinstance(arr, list):
                return arr
        except Exception:
            pass
    m = re.search(r"\{.*\}", s, re.S)              # or an object with a rows key
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict) and isinstance(obj.get("rows"), list):
                return obj["rows"]
        except Exception:
            pass
    return []

def extract_obligations(text, title):
    """LLM-extract a small structured table of legal obligations from a regulatory document.
    Returns rows: {party, obligation, article, penalty_text, penalty_krw}. Non-legal docs
    (READMEs etc.) typically yield []. This is the SQL tool's document-derived 'third form'."""
    prompt = (
        "Extract a table of legal OBLIGATIONS from the Korean regulatory text below. "
        "Return ONLY compact JSON (no markdown, no prose):\n"
        '{"rows":[{"party":"의무 주체","obligation":"의무 내용 한 문장","article":"제28조 또는 null",'
        '"penalty_text":"벌칙/과태료 원문 또는 null","penalty_krw":과태료 상한 금액 KRW 정수 또는 null}]}\n'
        "Rules:\n"
        "- party = 의무를 지는 주체(예: 금융회사, 전자금융업자, 개인정보처리자, 신용정보회사). 모르면 null.\n"
        "- obligation = 의무 내용을 한 문장으로 간결하게.\n"
        "- article = '제N조' 형태의 근거 조항, 모르면 null.\n"
        "- penalty_krw = 과태료/벌금 상한 금액이 명시된 경우에만 KRW 정수(예: '1천만원 이하' -> 10000000), "
        "추측하지 말 것, 없으면 null.\n"
        "- 의무가 없으면 \"rows\":[]. 최대 12행.\n"
        f"문서 제목: {title}\n"
        "Text:\n" + text[:3500])
    raw = rc.wx_generate(prompt, max_new_tokens=1500)
    rows = _parse_rows(raw)        # accepts {"rows":[...]} OR a bare [...] array
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        ob = str(r.get("obligation") or "").strip()
        if not ob:
            continue
        pk = r.get("penalty_krw")
        try:
            pk = float(pk) if pk not in (None, "", "null") else None
        except Exception:
            pk = None
        clean = lambda v: (str(v).strip() or None) if v not in (None, "null") else None
        out.append({"party": clean(r.get("party")), "obligation": ob[:500],
                    "article": clean(r.get("article")), "penalty_text": clean(r.get("penalty_text")),
                    "penalty_krw": pk})
        if len(out) >= 12:
            break
    return out

def _upsert_obligations(doc_id, title, md):
    """Extract + load this doc's obligations into Iceberg (best-effort; skipped if Presto off)."""
    if not rc.PRESTO_HOST:
        return 0
    try:
        obl = extract_obligations(md, title)
        rows = [{"doc_id": doc_id, "law": title, **o} for o in obl]
        rc.obligations_ensure()
        rc.obligations_upsert(doc_id, rows)
        print(f"[obligations] {len(rows)} rows")
        return len(rows)
    except Exception as e:
        print(f"[obligations] skipped: {str(e)[:120]}")
        return 0

def resolve_entities(ents, vecs):
    """Two-stage entity resolution: norm_name (1st) + embedding cosine vs the existing KG (2nd,
    computed app-side as this AstraDB lacks server-side ANN). Sets each entity's 'norm' to a
    canonical key, merging semantic duplicates across documents. Returns a remap
    {original_norm -> canonical_norm} to also rewrite this doc's edge endpoints."""
    try:
        existing = [e for e in rc.astra_find_all(rc.ASTRA_KG, {"kind": "entity"}) if e.get("emb")]
    except Exception:
        existing = []
    remap = {}
    for e, v in zip(ents, vecs):
        nn = rc.norm_name(e.get("name"))
        canon = nn
        best_s, best = 0.0, None
        for h in existing:
            s = rc.cosine(v, h.get("emb") or [])
            if s > best_s: best_s, best = s, h
        if best is not None and best_s >= 0.90:
            hnorm = best.get("norm")
            if hnorm and hnorm != nn:
                canon = hnorm                       # adopt the existing canonical node
        e["norm"] = canon
        if canon != nn:
            remap[nn] = canon
    return remap

def _clean_rel(rel):
    """Keep clean ASCII snake_case predicates (verbs); collapse Korean particles / sentence-fragments to related_to."""
    r = str(rel or "").strip().lower()
    if re.search(r"[가-힣]", r):                 # Korean fragment/postposition -> not a relationship
        return "related_to"
    r = re.sub(r"[\s\-]+", "_", r)
    r = re.sub(r"[^a-z_]", "", r).strip("_")
    return r if re.fullmatch(r"[a-z]{2,}(_[a-z]+)*", r or "") else "related_to"

def ingest_source(source=None, *, title=None, text=None, file_bytes=None, filename=None, force=False):
    """Ingest one document from text / file bytes / url-or-path. Idempotent upsert by doc_id.
    Returns {doc_id,title,source,chunks,entities,edges,status}."""
    # resolve logical source + title + raw markdown
    if text is not None:
        if not title: raise ValueError("title required for text ingest")
        source = source or f"inline:{title}"
        md = text
    elif file_bytes is not None:
        if not filename: raise ValueError("filename required for file ingest")
        source = source or f"file:{filename}"
        title = title or filename
        md = parse_docling_file(file_bytes, filename)
    elif source is not None:
        md = load_source(source)
    else:
        raise ValueError("provide one of: text, file_bytes, source")
    md = clean_md(md)                       # strip image placeholders / HTML comments before all stores
    title = title or source.rsplit("/", 1)[-1]
    doc_id = hashlib.sha1(source.encode()).hexdigest()[:16]

    rc.os_ensure_index(); rc.astra_ensure()
    if rc.PRESTO_HOST:
        try: rc.iceberg_ensure()
        except Exception as e: print(f"[iceberg] ensure skipped: {str(e)[:80]}")

    content_hash = hashlib.sha256(md.encode()).hexdigest()[:16]
    reg = rc.astra({"findOne": {"filter": {"_id": doc_id}}}, rc.ASTRA_DOCS).get("data", {}).get("document")
    if reg and reg.get("hash") == content_hash and not force:
        print(f"[skip] unchanged ({doc_id})")
        return {"doc_id": doc_id, "title": title, "source": source,
                "chunks": reg.get("chunks"), "entities": reg.get("entities"), "edges": reg.get("edges"),
                "status": "unchanged"}
    print(f"[changed] indexing {doc_id} ({title})")

    chunks = chunk(md)
    print(f"[chunk] {len(chunks)} chunks")
    vecs = rc.wx_embed(chunks)

    # upsert vectors: delete old chunks of this doc, then bulk index
    rc.os_req("POST", f"/{rc.OS_INDEX}/_delete_by_query?refresh=true", {"query": {"term": {"doc_id": doc_id}}})
    bulk = "".join(
        json.dumps({"index": {"_index": rc.OS_INDEX, "_id": f"{doc_id}-{i}"}}) + "\n" +
        json.dumps({"text": ch, "doc_id": doc_id, "source": source, "title": title, "chunk_no": i, "vector": v}) + "\n"
        for i, (ch, v) in enumerate(zip(chunks, vecs)))
    err = rc.os_req("POST", "/_bulk?refresh=true", ndjson=bulk).json().get("errors")
    print(f"[opensearch] indexed {len(chunks)} chunks, errors={err}")

    # KG extraction -> AstraDB. Entities carry an 'emb' field (for app-side semantic seed search +
    # resolution) and a canonical 'norm' from two-stage resolution (norm_name + embedding cosine).
    ents, edges = extract_kg(md, doc_id)
    if ents or edges:
        rc.astra_delete_all(rc.ASTRA_KG, {"doc_id": doc_id})   # remove this doc's old KG first
        ent_vecs = rc.wx_embed([f"{e.get('name')} ({e.get('type')})" for e in ents]) if ents else []
        remap = resolve_entities(ents, ent_vecs)               # sets e['norm']; returns edge remap
        rn = lambda name: remap.get(rc.norm_name(name), rc.norm_name(name))
        kg_docs = (
            [{"_id": f"{doc_id}:e:{i}", "kind": "entity", "doc_id": doc_id,
              "norm": e.get("norm"), "name": e.get("name"), "type": e.get("type"),
              "emb": ent_vecs[i]} for i, e in enumerate(ents)] +
            [{"_id": f"{doc_id}:r:{i}", "kind": "edge", "doc_id": doc_id,
              "src_norm": rn(ed.get("src")), "dst_norm": rn(ed.get("dst")), **ed}
             for i, ed in enumerate(edges)])
        rc.astra({"insertMany": {"documents": kg_docs}}, rc.ASTRA_KG)
    print(f"[kg] entities={len(ents)} edges={len(edges)}")

    rc.astra({"findOneAndReplace": {"filter": {"_id": doc_id},
        "replacement": {"_id": doc_id, "title": title, "source": source, "hash": content_hash,
                        "chunks": len(chunks), "entities": len(ents), "edges": len(edges)},
        "options": {"upsert": True}}}, rc.ASTRA_DOCS)
    if rc.PRESTO_HOST:
        try: rc.iceberg_upsert_doc(doc_id, title, source, len(chunks), len(ents), len(edges)); print("[iceberg] corpus upserted")
        except Exception as e: print(f"[iceberg] upsert skipped: {str(e)[:100]}")
    obligations = _upsert_obligations(doc_id, title, md)   # document-derived structured rows -> Iceberg (text-to-SQL)
    print(f"[done] {doc_id}")
    return {"doc_id": doc_id, "title": title, "source": source,
            "chunks": len(chunks), "entities": len(ents), "edges": len(edges),
            "obligations": obligations, "status": "indexed"}

def delete_doc(doc_id):
    """Remove a document from every store by doc_id (OpenSearch chunks, AstraDB kg + registry, Iceberg row)."""
    try: rc.os_req("POST", f"/{rc.OS_INDEX}/_delete_by_query?refresh=true", {"query": {"term": {"doc_id": doc_id}}})
    except Exception as e: print(f"[os] delete skipped: {str(e)[:80]}")
    rc.astra_delete_all(rc.ASTRA_KG, {"doc_id": doc_id})       # loop-delete all KG docs for this doc
    rc.astra({"deleteMany": {"filter": {"_id": doc_id}}}, rc.ASTRA_DOCS)
    if rc.PRESTO_HOST:
        try:
            t = f"{rc.PRESTO_CATALOG}.{rc.PRESTO_SCHEMA}.{rc.PRESTO_TABLE}"
            rc.presto_exec(f"DELETE FROM {t} WHERE doc_id='{doc_id}'")
            ot = f"{rc.PRESTO_CATALOG}.{rc.PRESTO_SCHEMA}.{rc.OBLIG_TABLE}"
            rc.presto_exec(f"DELETE FROM {ot} WHERE doc_id='{doc_id}'")
        except Exception as e: print(f"[iceberg] delete skipped: {str(e)[:100]}")
    print(f"[deleted] {doc_id}")
    return {"doc_id": doc_id, "status": "deleted"}

def list_docs():
    """Current corpus from doc_registry."""
    docs = rc.astra_find_all(rc.ASTRA_DOCS)
    return sorted(({"doc_id": d.get("_id"), "title": d.get("title"), "source": d.get("source"),
                    "chunks": d.get("chunks"), "entities": d.get("entities"), "edges": d.get("edges")}
                   for d in docs), key=lambda d: d.get("source") or "")

def _doc_text_from_os(doc_id, limit=30):
    """Reconstruct a document's text from its OpenSearch chunks (ordered by chunk_no) — lets us
    backfill obligations for already-indexed docs without re-fetching the original source."""
    hits = rc._os_hits({"size": limit, "query": {"term": {"doc_id": doc_id}},
                        "_source": ["text", "chunk_no"]})
    hits = sorted(hits, key=lambda h: h["_source"].get("chunk_no", 0))
    return "".join(h["_source"].get("text", "") for h in hits)

def backfill_obligations():
    """Extract obligations for every already-indexed doc (idempotent). Run once after deploy."""
    rc.obligations_ensure()
    total = 0
    for d in list_docs():
        doc_id, title = d["doc_id"], d.get("title") or ""
        txt = _doc_text_from_os(doc_id)
        if not txt:
            print(f"[backfill] {title}: no text in OpenSearch, skip"); continue
        n = _upsert_obligations(doc_id, title, txt)
        total += n
        print(f"[backfill] {title}: {n} obligations")
    print(f"[backfill] done — {total} rows across corpus")
    return total

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source", nargs="?"); ap.add_argument("--title", default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--backfill-obligations", action="store_true", help="extract obligations for all indexed docs")
    a = ap.parse_args()
    if a.backfill_obligations:
        backfill_obligations(); return
    if not a.source:
        ap.error("source required (or use --backfill-obligations)")
    print(f"[parse] {a.source}")
    ingest_source(a.source, title=a.title, force=a.force)

if __name__ == "__main__":
    main()
