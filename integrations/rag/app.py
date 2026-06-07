"""FastAPI BFF for the agentic-RAG chat UI. Serves the static frontend at / and exposes:
  POST /api/chat        - ask the agent (vector/graph/sql routing)
  GET  /api/docs        - list the indexed corpus (doc_registry)
  POST /api/ingest      - no-code ingest from pasted text or a URL  {mode:'text'|'url', title, content}
  POST /api/ingest/file - no-code ingest from an uploaded file (multipart: file, title)
  POST /api/delete      - remove a doc from every store  {doc_id}
Single-container: backend holds all credentials (wxai/astra/opensearch/presto)."""
import os
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import agent    # agent.run(q) -> structured dict
import ingest   # ingest_source / delete_doc / list_docs

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
CORPUS = "/corpus"   # rag-corpus ConfigMap mount (authored md sources)
app = FastAPI(title="Agentic RAG + KG")

class ChatReq(BaseModel):
    question: str

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
    try:
        return agent.run(req.question.strip())
    except Exception as e:
        return {"answer": f"**Error:** {type(e).__name__}: {e}",
                "route": {"vector": False, "graph": False, "sql": False},
                "citations": [], "context": {"chunks": [], "kg": {"entities": [], "edges": []}, "sql": []}}

@app.get("/api/docs")
def docs():
    try:
        return {"ok": True, "docs": ingest.list_docs()}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "docs": []}

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

@app.get("/corpus/{fname}")
def corpus_file(fname: str):
    """Serve an authored corpus markdown so its citation 'source' link opens the real document."""
    p = os.path.join(CORPUS, os.path.basename(fname))   # basename: no path traversal
    if not os.path.isfile(p):
        raise HTTPException(status_code=404, detail="not found")
    with open(p, encoding="utf-8") as f:
        return PlainTextResponse(f.read(), media_type="text/plain; charset=utf-8")

@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))

app.mount("/static", StaticFiles(directory=STATIC), name="static")
