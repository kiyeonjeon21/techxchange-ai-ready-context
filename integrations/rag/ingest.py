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
    print(f"[done] {doc_id}")
    return {"doc_id": doc_id, "title": title, "source": source,
            "chunks": len(chunks), "entities": len(ents), "edges": len(edges), "status": "indexed"}

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
        except Exception as e: print(f"[iceberg] delete skipped: {str(e)[:100]}")
    print(f"[deleted] {doc_id}")
    return {"doc_id": doc_id, "status": "deleted"}

def list_docs():
    """Current corpus from doc_registry."""
    docs = rc.astra_find_all(rc.ASTRA_DOCS)
    return sorted(({"doc_id": d.get("_id"), "title": d.get("title"), "source": d.get("source"),
                    "chunks": d.get("chunks"), "entities": d.get("entities"), "edges": d.get("edges")}
                   for d in docs), key=lambda d: d.get("source") or "")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source"); ap.add_argument("--title", default=None); ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    print(f"[parse] {a.source}")
    ingest_source(a.source, title=a.title, force=a.force)

if __name__ == "__main__":
    main()
