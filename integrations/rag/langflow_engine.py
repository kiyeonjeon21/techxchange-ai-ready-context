"""Chat-UI orchestration via Langflow (main engine).

POSTs the question to the deployed Langflow flow (watsonx granite Agent + RAG tool components),
then maps the agent's tool-calling run back into the structured shape the UI expects:
  {answer, route:{vector,graph,sql}, citations, context:{chunks, kg, sql}}.

The flow's tool components return our /api/tool/* JSON, and Langflow surfaces each tool's output
under content_blocks -> tool_use -> output, so the explainability panels are fully reconstructable.
app.py falls back to the in-process Python agent if this raises.
"""
import os, json, uuid, requests, urllib3
urllib3.disable_warnings()

LANGFLOW_URL = os.environ.get("LANGFLOW_URL", "http://langflow.genai-apps.svc.cluster.local:7860")
LANGFLOW_API_KEY = os.environ.get("LANGFLOW_API_KEY", "")
FLOW_NAME = os.environ.get("LANGFLOW_FLOW_NAME", "Agentic RAG + KG (watsonx)")
_FLOW_ID = os.environ.get("LANGFLOW_FLOW_ID", "")

# Langflow tool (output method) name -> UI route key
_TOOL_ROUTE = {"search_documents": "vector", "lookup_relationships": "graph", "query_business_data": "sql"}

def _hdr():
    return {"x-api-key": LANGFLOW_API_KEY, "Content-Type": "application/json"}

def _flow_id():
    global _FLOW_ID
    if _FLOW_ID:
        return _FLOW_ID
    r = requests.get(f"{LANGFLOW_URL}/api/v1/flows/?get_all=true&header_flows=true",
                     headers=_hdr(), verify=False, timeout=30).json()
    for f in r:
        if f.get("name") == FLOW_NAME:
            _FLOW_ID = f["id"]; return _FLOW_ID
    raise RuntimeError(f"Langflow flow not found: {FLOW_NAME!r}")

def _tool_output(raw):
    """A tool's output is the Message text (a JSON string from our component)."""
    if isinstance(raw, str):
        try: return json.loads(raw)
        except Exception: return {}
    return raw or {}

def run(q, session_id=None):
    fid = _flow_id()
    # session_id drives Langflow's per-conversation memory. A stable id (one per browser, sent by the
    # chat UI) gives multi-turn follow-ups; distinct ids keep users isolated. None -> fresh id (stateless).
    sid = session_id or f"ragui-{uuid.uuid4().hex}"
    body = {"input_value": q, "output_type": "chat", "input_type": "chat", "session_id": sid}
    r = requests.post(f"{LANGFLOW_URL}/api/v1/run/{fid}?stream=false", json=body,
                      headers=_hdr(), verify=False, timeout=180).json()
    if "outputs" not in r:
        raise RuntimeError(f"langflow run error: {str(r)[:200]}")
    msg = r["outputs"][0]["outputs"][0]["results"]["message"]
    answer = msg.get("text") or ""

    route = {"vector": False, "graph": False, "sql": False}
    chunks, citations = [], []
    kg = {"entities": [], "edges": []}
    sql_obj = {"query": None, "columns": [], "rows": []}

    for blk in (msg.get("content_blocks") or []):
        for c in blk.get("contents", []):
            if c.get("type") != "tool_use":
                continue
            rk = _TOOL_ROUTE.get(c.get("name"))
            if not rk:
                continue
            route[rk] = True
            out = _tool_output(c.get("output"))
            # agent may call a tool several times -> keep the richest result
            if rk == "vector":
                res = out.get("results") or []
                if len(res) > len(chunks):
                    chunks = res
                    citations = [{"title": h.get("title"), "chunk_no": h.get("chunk_no")} for h in res]
            elif rk == "graph":
                ent, edg = out.get("entities") or [], out.get("edges") or []
                if len(ent) + len(edg) > len(kg["entities"]) + len(kg["edges"]):
                    kg = {"entities": ent, "edges": edg}
            elif rk == "sql":
                rows = out.get("rows") or []
                if len(rows) > len(sql_obj["rows"]) or (sql_obj["query"] is None and out.get("sql")):
                    sql_obj = {"query": out.get("sql"), "columns": out.get("columns", []), "rows": rows}

    return {"answer": answer, "route": route, "citations": citations,
            "context": {"chunks": chunks, "kg": kg, "sql": sql_obj}}
