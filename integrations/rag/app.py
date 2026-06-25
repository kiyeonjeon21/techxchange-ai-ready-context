"""FastAPI BFF for the agentic-RAG chat UI. Serves the static frontend at / and exposes:
  POST /api/chat        - ask the agent (vector/graph/sql routing)
  GET  /api/docs        - list the indexed corpus (doc_registry)
  POST /api/ingest      - no-code ingest from pasted text or a URL  {mode:'text'|'url', title, content}
  POST /api/ingest/file - no-code ingest from an uploaded file (multipart: file, title)
  POST /api/delete      - remove a doc from every store  {doc_id}
Single-container: backend holds all credentials (wxai/astra/opensearch/presto)."""
import os, hmac, hashlib, base64, time
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import agent    # agent.run(q) -> structured dict (in-process Python engine)
import ingest   # ingest_source / delete_doc / list_docs
import text2sql # run_text2sql(q) -> {sql, columns, rows, error}
import langflow_engine  # run(q) -> same shape, orchestrated by the deployed Langflow flow
import rag_common as rc  # OpenSearch / AstraDB clients (doc preview)

# Orchestration engine for /api/chat: "langflow" (default) routes through the Langflow agent flow
# and falls back to the in-process Python agent on error; "python" uses the agent directly.
RAG_ENGINE = os.environ.get("RAG_ENGINE", "langflow")

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
CORPUS = "/corpus"   # rag-corpus ConfigMap mount (authored md sources)
CORPUS_PDF = "/corpus-pdf"   # rag-corpus-pdf ConfigMap mount (original PDFs, for preview)
app = FastAPI(title="Agentic RAG + KG")

# ---- shared-password gate (signed session cookie) ----
# Auth is ON only when APP_PASSWORD is set; otherwise the app stays open (backward compatible).
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
_SECRET = (os.environ.get("APP_SECRET") or ("k:" + APP_PASSWORD)).encode()
COOKIE = "ragsess"
SESSION_TTL = 12 * 3600
PUBLIC_PREFIXES = ("/login", "/static/", "/healthz", "/api/login", "/favicon")
# Per-tool endpoints (/api/tool/*) for Langflow orchestration: authorized by a static token header
# (X-Tool-Token), not the browser session cookie. Defaults to APP_PASSWORD if TOOL_TOKEN unset.
TOOL_TOKEN = os.environ.get("TOOL_TOKEN") or APP_PASSWORD

def _sign(exp: int) -> str:
    sig = hmac.new(_SECRET, str(exp).encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"

def _valid(token: str) -> bool:
    try:
        exp_s, sig = token.split(".", 1)
        exp = int(exp_s)
    except Exception:
        return False
    if exp < int(time.time()):
        return False
    return hmac.compare_digest(_sign(exp), token)

@app.middleware("http")
async def auth_gate(request: Request, call_next):
    if not APP_PASSWORD:                                  # auth disabled
        return await call_next(request)
    path = request.url.path
    if any(path == p or path.startswith(p) for p in PUBLIC_PREFIXES):
        return await call_next(request)
    if path.startswith("/api/tool/"):                    # token-gated tool endpoints (Langflow)
        if TOOL_TOKEN and hmac.compare_digest(request.headers.get("X-Tool-Token", ""), TOOL_TOKEN):
            return await call_next(request)
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if _valid(request.cookies.get(COOKIE, "")):
        return await call_next(request)
    if path.startswith("/api/") or path.startswith("/corpus/"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return RedirectResponse(url="/login", status_code=302)

class LoginReq(BaseModel):
    password: str

@app.post("/api/login")
def login(req: LoginReq):
    if not APP_PASSWORD or not hmac.compare_digest(req.password, APP_PASSWORD):
        return JSONResponse({"ok": False, "error": "invalid password"}, status_code=401)
    token = _sign(int(time.time()) + SESSION_TTL)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, token, max_age=SESSION_TTL, httponly=True, secure=True, samesite="lax", path="/")
    return resp

@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp

@app.get("/login")
def login_page():
    return FileResponse(os.path.join(STATIC, "login.html"))

class ChatReq(BaseModel):
    question: str
    session_id: str | None = None   # stable per browser conversation -> Langflow multi-turn memory

class IngestReq(BaseModel):
    mode: str = "text"      # 'text' | 'url'
    title: str | None = None
    content: str            # pasted text, or the URL

class DeleteReq(BaseModel):
    doc_id: str

@app.get("/healthz")
def healthz():
    return {"status": "ok"}

@app.post("/api/chat")
def chat(req: ChatReq):
    q = req.question.strip()
    if RAG_ENGINE == "langflow":
        try:
            return {**langflow_engine.run(q, session_id=req.session_id), "engine": "langflow"}
        except Exception as e:
            print(f"[chat] langflow engine failed ({type(e).__name__}: {e}) -> python agent fallback")
    try:
        return {**agent.run(q), "engine": "python"}
    except Exception as e:
        return {"answer": f"**Error:** {type(e).__name__}: {e}", "engine": "error",
                "route": {"vector": False, "graph": False, "sql": False},
                "citations": [], "context": {"chunks": [], "kg": {"entities": [], "edges": []},
                                             "sql": {"query": None, "columns": [], "rows": []}}}

@app.get("/api/docs")
def docs():
    try:
        return {"ok": True, "docs": ingest.list_docs()}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "docs": []}

@app.get("/api/doc/{doc_id}")
def doc_detail(doc_id: str):
    """Preview an ingested doc for the corpus drawer: its chunks (text, from OpenSearch) + KG
    entity names (from AstraDB). Lets users see what's actually indexed, not just the title."""
    try:
        hits = rc._os_hits({"size": 60, "query": {"term": {"doc_id": doc_id}},
                            "_source": ["text", "chunk_no", "title", "source"]})
        hits = sorted(hits, key=lambda h: h["_source"].get("chunk_no", 0))
        src = hits[0]["_source"] if hits else {}
        chunks = [{"chunk_no": h["_source"].get("chunk_no"), "text": h["_source"].get("text", "")} for h in hits]
        ents = []
        try:
            kg = rc.astra_find_all(rc.ASTRA_KG, {"kind": "entity", "doc_id": doc_id})
            ents = sorted({(e.get("name") or "").strip() for e in kg if e.get("name")})
        except Exception:
            pass
        # if the original PDF is available (mounted from rag-corpus-pdf), expose a viewer URL
        source = src.get("source") or ""
        pdf_url = None
        if source.lower().endswith(".pdf"):
            base = os.path.basename(source.split(":", 1)[-1])
            if os.path.isfile(os.path.join(CORPUS_PDF, base)):
                pdf_url = f"/corpus-pdf/{base}"
        return {"ok": True, "doc_id": doc_id, "title": src.get("title"), "source": source,
                "chunks": chunks, "entities": ents, "pdf_url": pdf_url}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

@app.post("/api/ingest")
def ingest_text_or_url(req: IngestReq):
    try:
        if req.mode == "url":
            r = ingest.ingest_source(req.content.strip(), title=req.title or None, force=True)
        else:
            if not (req.title and req.title.strip()):
                return {"ok": False, "error": "title required for text ingest"}
            r = ingest.ingest_source(text=req.content, title=req.title.strip(), force=True)
        return {"ok": True, **r}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

@app.post("/api/ingest/file")
def ingest_file(file: UploadFile = File(...), title: str = Form(None)):
    try:
        data = file.file.read()
        r = ingest.ingest_source(file_bytes=data, filename=file.filename,
                                 title=(title.strip() if title and title.strip() else None), force=True)
        return {"ok": True, **r}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

@app.post("/api/delete")
def delete(req: DeleteReq):
    try:
        return {"ok": True, **ingest.delete_doc(req.doc_id)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

class ToolReq(BaseModel):
    question: str
    k: int | None = None

@app.post("/api/tool/vector")
def tool_vector(req: ToolReq):
    """Vector/keyword passage retrieval (OpenSearch hybrid). For Langflow orchestration."""
    try:
        ctx, hits = agent.tool_vector(req.question.strip(), k=req.k or 8)
        return {"ok": True, "passages": ctx,
                "results": [{"title": h["title"], "chunk_no": h["chunk_no"],
                             "score": round(h.get("score", 0), 4), "text": h["text"],
                             "source": h.get("source", "")} for h in hits]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

@app.post("/api/tool/graph")
def tool_graph(req: ToolReq):
    """Knowledge-graph 1-hop subgraph (AstraDB). For Langflow orchestration."""
    try:
        _, kg = agent.tool_graph(req.question.strip())
        return {"ok": True, "entities": kg["entities"], "edges": kg["edges"]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

@app.post("/api/tool/sql")
def tool_sql(req: ToolReq):
    """text-to-SQL over the AML dataset (watsonx.data/Presto). For Langflow orchestration."""
    try:
        return {"ok": True, **text2sql.run_text2sql(req.question.strip())}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

@app.get("/corpus/{fname}")
def corpus_file(fname: str):
    """Serve an authored corpus markdown so its citation 'source' link opens the real document."""
    p = os.path.join(CORPUS, os.path.basename(fname))   # basename: no path traversal
    if not os.path.isfile(p):
        raise HTTPException(status_code=404, detail="not found")
    with open(p, encoding="utf-8") as f:
        return PlainTextResponse(f.read(), media_type="text/plain; charset=utf-8")

@app.get("/corpus-pdf/{fname}")
def corpus_pdf(fname: str):
    """Serve an original corpus PDF (rag-corpus-pdf mount) for inline preview in the drawer."""
    p = os.path.join(CORPUS_PDF, os.path.basename(fname))   # basename: no path traversal
    if not os.path.isfile(p):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(p, media_type="application/pdf")

@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))

app.mount("/static", StaticFiles(directory=STATIC), name="static")
