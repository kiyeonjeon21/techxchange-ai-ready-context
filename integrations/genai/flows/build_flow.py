import os, json, ssl, gzip, urllib.request, urllib.error, copy

LF = os.environ["LF"]; KEY = os.environ["LANGFLOW_APIKEY"]
WX_APIKEY = os.environ["WX_APIKEY"]; WX_PROJECT_ID = os.environ["WX_PROJECT_ID"]
WX_URL = os.environ.get("WX_URL", "https://us-south.ml.cloud.ibm.com")
TOOL_TOKEN = os.environ["APP_PASSWORD"]
RAG = "http://rag-ui.genai-apps.svc.cluster.local:8080"
ctx = ssl._create_unverified_context()

def api(path, payload=None, method="GET"):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(LF+path, data=data, method=method)
    req.add_header("x-api-key", KEY)
    if data: req.add_header("Content-Type", "application/json")
    try:
        r = urllib.request.urlopen(req, context=ctx, timeout=180); d = r.read()
        if d[:2] == b"\x1f\x8b": d = gzip.decompress(d)
        return r.status, (json.loads(d) if d else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:600].decode("utf-8", "replace")

# Fetch fresh component templates from the live Langflow (self-contained, reproducible)
_s, _all = api("/api/v1/all")
if _s != 200:
    raise SystemExit(f"failed to fetch /api/v1/all: {_s}")
def _find(name):
    for _cat, _comps in _all.items():
        if name in _comps:
            return copy.deepcopy(_comps[name])
    raise SystemExit(f"component not found: {name}")
templates = {n: _find(n) for n in ("IBMwatsonxModel", "Agent", "ChatInput", "ChatOutput")}

# ---- handle encoding (Langflow uses œ in place of quotes in edge handle ids) ----
def enc_nospace(d):
    return json.dumps(d, ensure_ascii=False, separators=(",", ":")).replace('"', 'œ')

# Build spaced handle strings exactly like Langflow: {œkœ: œvœ, ...}
def spaced(d):
    parts = []
    for k, v in d.items():
        if isinstance(v, list):
            inner = ", ".join("œ"+str(x)+"œ" for x in v)
            parts.append(f"œ{k}œ: [{inner}]")
        else:
            parts.append(f"œ{k}œ: œ{v}œ")
    return "{" + ", ".join(parts) + "}"

def edge2(src_id, src_type, out_name, out_types, tgt_id, field, in_types, ttype):
    sh = {"dataType": src_type, "id": src_id, "name": out_name, "output_types": out_types}
    th = {"fieldName": field, "id": tgt_id, "inputTypes": in_types, "type": ttype}
    sh_s, th_s = spaced(sh), spaced(th)
    eid = f"xy-edge__{src_id}{enc_nospace(sh)}-{tgt_id}{enc_nospace(th)}"
    return {"data": {"sourceHandle": sh, "targetHandle": th}, "id": eid,
            "source": src_id, "target": tgt_id, "sourceHandle": sh_s, "targetHandle": th_s,
            "animated": False, "className": ""}

# ---- 1) custom tool components ----
TOOLS = [
    ("RagVectorTool", "RAG · Vector Search", "/api/tool/vector", "search_documents",
     "Semantic + keyword (hybrid) passage retrieval over the regulatory document corpus (privacy/credit/e-finance/AML laws). Use for definitions, concepts, and 'what does X say' questions."),
    ("RagGraphTool", "RAG · Knowledge Graph", "/api/tool/graph", "lookup_relationships",
     "1-hop knowledge-graph subgraph (entities + relationships) from AstraDB. Use for relationship/structure questions: who regulates/supervises whom, what an entity's obligations or connections are."),
    ("RagSqlTool", "RAG · text-to-SQL", "/api/tool/sql", "query_business_data",
     "Generate and run a read-only SQL query over the watsonx.data AML business dataset (customers, accounts, transactions, suspicious-transaction reports). Use for counts, sums, amounts, risk ratings, flagged transactions, countries."),
]
CODE_TMPL = '''from langflow.custom import Component
from langflow.io import MessageTextInput, Output
from langflow.schema.message import Message
import requests, json

class {cls}(Component):
    display_name = "{disp}"
    description = "{desc}"
    name = "{cls}"
    icon = "search"
    inputs = [MessageTextInput(name="question", display_name="Question",
                               info="The user's natural-language question", tool_mode=True)]
    outputs = [Output(name="{method}", display_name="Result", method="{method}")]
    def {method}(self) -> Message:
        """{desc}"""
        url = "{rag}{path}"
        try:
            r = requests.post(url, json={{"question": self.question}},
                              headers={{"X-Tool-Token": "{tok}"}}, timeout=120)
            return Message(text=json.dumps(r.json(), ensure_ascii=False))
        except Exception as e:
            return Message(text=json.dumps({{"error": str(e)}}))
'''

tool_nodes = []
positions = [(-50, 80), (-50, 360), (-50, 640)]
for i, (cls, disp, path, method, desc) in enumerate(TOOLS):
    code = CODE_TMPL.format(cls=cls, disp=disp, desc=desc.replace('"', '\\"'),
                            rag=RAG, path=path, tok=TOOL_TOKEN, method=method)
    s, node = api("/api/v1/custom_component", {"code": code}, "POST")
    if s != 200:
        print("custom_component FAIL", cls, s, node); raise SystemExit(1)
    nd = node["data"]
    # ensure description/display_name set on the node
    nd["display_name"] = disp
    # enable TOOL MODE: replace outputs with the inherited toolset output (Component.to_toolkit)
    nd["tool_mode"] = True
    nd["outputs"] = [{
        "allows_loop": False, "cache": True, "display_name": "Toolset",
        "group_outputs": False, "hidden": False, "method": "to_toolkit",
        "name": "component_as_tool", "selected": "Tool", "tool_mode": True,
        "types": ["Tool"], "value": "__UNDEFINED__", "options": None, "required_inputs": None,
    }]
    outs = [(o.get("name"), o.get("types")) for o in nd.get("outputs", [])]
    print(f"tool {cls}: outputs={outs} tool_mode={nd.get('tool_mode')}")
    node_id = f"{cls}-{i}"
    tool_nodes.append({
        "id": node_id, "type": "genericNode",
        "position": {"x": positions[i][0], "y": positions[i][1]},
        "data": {"id": node_id, "type": cls, "node": nd, "showNode": True},
    })

# ---- core nodes from fresh templates (avoids 'outdated component' warnings) ----
def make_node(ntype, nid, pos):
    tmpl = copy.deepcopy(templates[ntype])
    return {"id": nid, "type": "genericNode", "position": {"x": pos[0], "y": pos[1]},
            "data": {"id": nid, "type": ntype, "node": tmpl, "showNode": True}}

ci_id, ag_id, co_id, wx_id = "ChatInput-1", "Agent-1", "ChatOutput-1", "IBMwatsonxModel-1"
ci = make_node("ChatInput", ci_id, (300, 360))
co = make_node("ChatOutput", co_id, (1200, 320))
ag = make_node("Agent", ag_id, (760, 250))
wx_node = make_node("IBMwatsonxModel", wx_id, (300, -150))

# watsonx model config (literal secret for the live flow; scrubbed on export)
t = wx_node["data"]["node"]["template"]
t["api_key"]["value"] = WX_APIKEY; t["api_key"]["load_from_db"] = False
t["project_id"]["value"] = WX_PROJECT_ID
t["base_url"]["value"] = WX_URL
WX_MODEL = "ibm/granite-4-h-small"   # granite-3-8b-instruct not in this env's langchain model list
t["model_name"]["value"] = WX_MODEL
t["model_name"]["options"] = [WX_MODEL]

# agent config
agt = ag["data"]["node"]["template"]
agt["system_prompt"]["value"] = (
    "You are an agentic RAG assistant over the watsonx stack. You have three tools: "
    "RAG Vector Search (document passages), RAG Knowledge Graph (entity relationships), and "
    "RAG text-to-SQL (the AML business dataset). Choose the tool(s) that fit the question — use "
    "vector for definitions/concepts, the knowledge graph for relationship/structure questions, and "
    "text-to-SQL for counts/sums/amounts over customers, transactions, or suspicious-transaction reports. "
    "You may call multiple tools. Ground your answer ONLY in tool results and cite what you used. "
    "Answer in the user's language (Korean question -> Korean answer).")

nodes = [ci, wx_node, ag, co] + tool_nodes

# ---- 4) edges ----
edges = [
    edge2(ci_id, "ChatInput", "message", ["Message"], ag_id, "input_value", ["Message"], "str"),
    edge2(wx_id, "IBMwatsonxModel", "model_output", ["LanguageModel"], ag_id, "model", ["LanguageModel"], "other"),
    edge2(ag_id, "Agent", "response", ["Message"], co_id, "input_value", ["Data", "DataFrame", "Message"], "str"),
]
for tn in tool_nodes:
    edges.append(edge2(tn["id"], tn["data"]["type"], "component_as_tool", ["Tool"], ag_id, "tools", ["Tool"], "other"))

flow = {
    "name": "Agentic RAG + KG (watsonx)",
    "description": "Visual orchestration: a watsonx granite Agent selects RAG tools (vector / knowledge-graph / text-to-SQL) backed by the rag-ui service. Parallel demo to the Python engine.",
    "data": {"nodes": nodes, "edges": edges, "viewport": {"x": 0, "y": 0, "zoom": 0.7}},
    "is_component": False,
}

# delete any existing flows with the same name (idempotent rebuild)
s, existing = api("/api/v1/flows/?get_all=true&header_flows=true")
if s == 200 and isinstance(existing, list):
    for f in existing:
        if f.get("name") == flow["name"]:
            ds, _ = api(f"/api/v1/flows/{f['id']}", method="DELETE")
            print("deleted old flow", f["id"], ds)

s, res = api("/api/v1/flows/", flow, "POST")
print("create flow ->", s)
if s not in (200, 201):
    print(res); raise SystemExit(1)
flow_id = res["id"]
print("flow_id:", flow_id, "| name:", res["name"])
open("/tmp/flow_id.txt", "w").write(flow_id)

# ---- export + scrub secrets -> repo JSON ----
s, full = api(f"/api/v1/flows/{flow_id}")
if s == 200:
    for k in ("id", "user_id", "folder_id", "created_at", "updated_at", "webhook", "endpoint_name"):
        full.pop(k, None)
    txt = json.dumps(full, ensure_ascii=False)
    txt = txt.replace(WX_APIKEY, "<WX_APIKEY>").replace(TOOL_TOKEN, "<TOOL_TOKEN>")
    if WX_PROJECT_ID:
        txt = txt.replace(WX_PROJECT_ID, "<WX_PROJECT_ID>")
    assert WX_APIKEY not in txt and TOOL_TOKEN not in txt, "secret leak in export!"
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agentic-rag.json")
    open(out_path, "w").write(json.dumps(json.loads(txt), ensure_ascii=False, indent=2))
    print("exported scrubbed flow ->", out_path)
