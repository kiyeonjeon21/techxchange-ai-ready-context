# Langflow 플로우 — Agentic RAG + KG (watsonx)

배포된 Langflow(1.8.0, ns `genai-apps`)에서 **watsonx granite Agent가 RAG 도구를 직접 골라 호출**하는
플로우. 파이썬 엔진(`integrations/rag`)이 챗 UI를 구동하고, 이 플로우는 "오케스트레이션을 시각적으로"
보여주는 **병렬 데모**다. 두 경로 모두 같은 rag-ui 도구 엔드포인트를 호출한다.

## 플로우 구성 (`agentic-rag.json`)

```
ChatInput ─┐
           ├─► Agent (IBM watsonx.ai · granite) ─► ChatOutput
IBMwatsonxModel ─► (model)        ▲ tools
                  RagVectorTool ──┤   (search_documents  → /api/tool/vector)
                  RagGraphTool ───┤   (lookup_relationships → /api/tool/graph)
                  RagSqlTool ─────┘   (query_business_data → /api/tool/sql)
```

- **Agent**: tool-calling 에이전트. 시스템 프롬프트가 "개념=vector, 관계=graph, 집계=sql" 가이드.
- **3 커스텀 툴 컴포넌트**: 각각 입력 `question`(tool 인자)을 받아 rag-ui 엔드포인트로 POST하고 결과(JSON)를
  반환. 헤더 `X-Tool-Token`. 내부 URL `http://rag-ui.genai-apps.svc.cluster.local:8080`.
- **모델**: `ibm/granite-4-h-small`. (`granite-3-8b-instruct`는 이 환경의 Langflow watsonx 컴포넌트
  지원 목록에 없어 4-h-small 사용. rag-ui 파이썬 엔진은 REST `/text/chat`로 granite-3-8b-instruct 사용 — 무관.)

## Import 방법

1. Langflow UI → **New → Import** (또는 API `POST /api/v1/flows/`)로 `agentic-rag.json` 업로드.
2. JSON의 플레이스홀더를 실제 값으로 치환(또는 import 후 노드에서 설정):
   - `IBMwatsonxModel`: `<WX_APIKEY>`(api_key), `<WX_PROJECT_ID>`(project_id) — `rag-secrets`/배포 env와 동일.
   - 3개 툴 컴포넌트 `code`의 `<TOOL_TOKEN>` → rag-ui `APP_PASSWORD`(또는 `TOOL_TOKEN`) 값.
   - 시크릿은 Langflow **Global Variable**로 빼두면 평문 노출을 피할 수 있다.
3. 저장 후 **Playground**에서 질의:
   - "위험등급이 high인 고객 수는?" → `query_business_data` 호출(text-to-SQL).
   - "전자금융거래법의 접근매체와 감독기관의 관계는?" → `lookup_relationships` 호출(KG).
   - "가명정보란?" → `search_documents` 호출(vector).

> 이 리포의 `agentic-rag.json`은 **시크릿이 스크럽된** 버전이며, 실제 동작은 검증됨(라이브 플로우
> "Agentic RAG + KG (watsonx)"에서 위 3개 질의가 도구를 올바르게 호출해 grounded 답변 생성 확인).

## rag-ui 도구 엔드포인트 (참고)

| 엔드포인트 | 입력 | 출력 |
|---|---|---|
| `POST /api/tool/vector` | `{"question": "...", "k": 8}` | `{ok, passages[], results[{title,chunk_no,score,text,source}]}` |
| `POST /api/tool/graph`  | `{"question": "..."}` | `{ok, entities[], edges[]}` (1홉 서브그래프) |
| `POST /api/tool/sql`    | `{"question": "..."}` | `{ok, sql, columns[], rows[], error}` (text-to-SQL) |

검증(클러스터 내부, 토큰 게이트):
```bash
POD=$(oc -n genai-apps get pod -l app=rag-ui -o jsonpath='{.items[0].metadata.name}')
oc -n genai-apps exec "$POD" -- python -c "import os,json,urllib.request as u; \
b=json.dumps({'question':'위험등급 high 고객 수는?'}).encode(); \
r=u.Request('http://localhost:8080/api/tool/sql',data=b,headers={'Content-Type':'application/json','X-Tool-Token':os.environ['APP_PASSWORD']}); \
print(json.load(u.urlopen(r)))"
```

## 데모 포인트
챗 UI의 단일패스 라우터와 **동일한 도구**를, Langflow에서는 **에이전트가 tool-calling으로 선택**하는
모습으로 시연 → "오케스트레이션을 눈으로". 프로덕션 경로는 루트 README의 데모↔프로덕션 표 참고.
