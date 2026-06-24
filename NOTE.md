# 참고 문서

- software hub doc — https://www.ibm.com/docs/en/software-hub/5.3.x
- watsonx.data doc — https://www.ibm.com/docs/en/watsonxdata/standard/2.3.x
- cpdcli download — https://github.com/IBM/cpd-cli/releases/download/v14.3.1.6/cpd-cli-darwin-EE-14.3.1.tgz

> 설치/구축 상세 기록은 `docs/wxdata-install-journey.md`, 아키텍처 그림은 `docs/architecture.pdf` 참고.

---

# Agentic RAG + Knowledge Graph 데모 가이드

watsonx 스택(watsonx.data + watsonx.ai) 위에 올린 **설명가능한(explainable) 에이전틱 RAG + 지식그래프** 데모.
질문마다 알맞은 검색 도구(벡터/그래프/SQL)로 자동 라우팅하고, **근거를 그대로 보여준다.**

## 접속

- **채팅 UI**: https://rag-ui-genai-apps.apps.<CLUSTER_DOMAIN>
- 네임스페이스: `genai-apps` (OCP 클러스터 `<CLUSTER_NAME>`)
- 코드: `integrations/rag/` (app.py, agent.py, ingest.py, rag_common.py, static/)

## 데모 코퍼스 (8개 문서)

한글 금융·컴플라이언스 6 + 영문 README 2. KG가 "꼭 필요해 보이는" 도메인(법령 간 교차참조)으로 선정.
**md·PDF·URL 세 가지 입력 경로가 모두 코퍼스에 섞여 있다**(문서 처리 다양성 시연).

| 문서 | 종류 |
|---|---|
| 개인정보 보호법 / 마이데이터 / 전자금융거래법 / 전자금융감독규정 | 직접 작성 **md** (`integrations/rag/corpus/`) |
| 신용정보법 / 특정금융정보법 | **PDF** 업로드(`integrations/rag/corpus_pdf/`, docling 파싱, source `file:*`) |
| Docling README / Langflow README | **URL**(GitHub raw, docling fetch) |

별도로 text-to-SQL용 **AML/금융 데이터셋**(`iceberg_data.aml`: customers·accounts·transactions·str_reports,
`integrations/rag/seed_aml.py`)을 watsonx.data에 시드해 SQL 경로가 실제 비즈니스 데이터를 질의한다.

---

## 시연 시나리오 (순서대로)

### 1. 첫 화면
한글 추천 질문 4개가 4개 경로를 각각 대표한다. 클릭하면 바로 질의된다.

### 2. 세 가지 검색 경로 (tool-trace 배지 + 근거 패널로 확인)

| 경로 | 예시 질문 | 보여줄 것 |
|---|---|---|
| **vector** (OpenSearch) | "가명정보란 무엇이고 동의 없이 쓸 수 있는 목적은?" | 답변 + 인용칩 + "Retrieved passages" 패널(청크별 유사도 바) |
| **graph** (AstraDB KG) | "전자금융거래법의 접근매체와 감독기관의 관계를 지식그래프로 보여줘" | "Knowledge subgraph" 패널(엔티티 칩 + 엣지 트리플) |
| **sql** (text-to-SQL) | "위험등급이 high인 고객의 플래그된 거래 총액을 상대국가별로?" | "Generated SQL · results" 패널(생성 SQL 코드블록 + 결과 표) |
| **hybrid** | "자금세탁 의심거래는 어디에 보고하고, VASP의 의무는?" | vector+graph 배지 동시 점등, 풍부한 답변 |

> 챗 UI(`/api/chat`)는 오케스트레이션을 **Langflow 에이전트**(watsonx granite, tool-calling)에 위임하고
> 장애 시 **파이썬 에이전트로 폴백**한다(아래 "Langflow 메인 엔진"). 답변 언어는 질문 언어를 따른다(한글→한글).

### 3. ★ 핵심: No-code 즉석 적재 → 답 변화 → 삭제 → 원복

RAG의 본질("grounding 있으면 정확, 없으면 환각")을 **UI 클릭만으로** 보여주는 하이라이트.

1. **적재 전 질문** — "클라우드 보안인증제(CSAP)는 어느 기관이 평가하나?"
   → ❌ 환각 (코퍼스에 없어 엉뚱한 기관/가짜 인용 생성)
2. **우측 "⚙ 코퍼스" 드로어 열기** → 탭(텍스트/URL/파일) 중 **텍스트** 선택
   → 제목+본문 붙여넣기 → **"추가 적재"** → "적재 완료" 토스트, 목록 8→9
3. **같은 질문 재질의** → ✅ 정확("한국인터넷진흥원(KISA)이 평가, 하/중/상 3등급") + 새 문서 인용
4. **드로어에서 🗑 삭제** → 같은 질문 → 다시 환각으로 **원복**

입력 방식 3종 모두 동작:
- **텍스트 붙여넣기** (가장 빠름, 데모 주력)
- **URL** (docling이 fetch·파싱)
- **파일 업로드(PDF/DOCX 등)** — docling `/v1/convert/file` 파싱. (검증: corpus md를 PDF로 변환해 업로드 → 정상 추출/답변)

### 4. 출처(근거) 확인
청크 패널의 source는 종류별로:
- URL 문서 → **"source ↗"** (GitHub 원문 열림)
- 직접작성 md → **"문서 ↗"** (앱이 `/corpus/*.md` 서빙 → 원문 열림)
- 붙여넣기/업로드 → 비클릭 라벨(원본 URL 없음 명시)

---

## 내부 동작 (질문 들어오면 이렇게 답한다)

```
질문 → 챗 UI(/api/chat) → Langflow 에이전트(watsonx granite, tool-calling)   ※실패 시 파이썬 에이전트 폴백
     → vector: OpenSearch 하이브리드(kNN+BM25, RRF 융합, k=8)
       graph : AstraDB KG — 엔티티 벡터 시드(앱-사이드 코사인) → 1홉 이웃 조회($or/$in) → 별칭 병합
       sql   : text-to-SQL — 스키마카드+few-shot으로 granite가 Presto SELECT 생성 → 가드(SELECT-only+LIMIT)
               → 실행, 에러 시 1회 self-correction (iceberg_data.aml 비즈니스 데이터)
     → 에이전트가 도구 결과로 답변 합성 + 인용
     → UI: 답변 + tool-trace 배지 + 근거 패널(생성 SQL 포함)  ← Langflow 실행 응답에서 복원
```
(파이썬 엔진 경로는 동일하되 라우터에 관계형 graph 백스톱 적용; `RAG_ENGINE=python`으로 강제 가능)

## 문서 적재 시 (한 번에 4개 저장소, 같은 doc_id)

```
ingest_source(문서) — doc_id=sha1(source)
  ├─ OpenSearch       : 청크 + granite 임베딩(768d)   ← vector
  ├─ AstraDB kg       : granite 추출 엔티티/엣지        ← graph
  ├─ AstraDB doc_registry : 제목·소스·해시·카운트       ← 추적/증분
  └─ Iceberg corpus   : 문서 인벤토리 1행               ← sql
```
- **멱등 upsert**(doc_id 기준) + **content-hash 스킵** → 같은 문서 재적재해도 중복 없음
- **CronJob `rag-reindex`**(30분): 등록된 소스 재적재, 바뀐 것만 재처리

## 검색·KG 품질 개선 (적용 완료)

- **하이브리드 검색**: 벡터(의미) + BM25(정확매칭) RRF 융합 → 약어·법령명(STR·VASP·CISO)에 강함
- **cjk 분석기**: 한글을 bigram 토큰화(`text` 필드) → 한글 BM25 recall 대폭 개선
  (nori는 operator 관리 클러스터라 설치 불가 → 내장 cjk로 대체)
- **KG 정규화·해소**: 엔티티명 정규화 + **임베딩 코사인 기반 엔티티 해소**(별칭/표기 변형 병합),
  관계 라벨 snake_case 강제(조사 "의/는" 제거)·자기루프 제거, **엔티티 타입 온톨로지**(통제 어휘)
- **KG 벡터 시드**: 엔티티에 임베딩(`emb` 필드) 저장 → 질문과 의미적으로 가까운 엔티티를 시드로
  1홉 서브그래프 구성. (이 AstraDB는 on-disk SAI 포맷이 `aa`라 서버사이드 벡터 ANN 미지원 →
  KG가 작으므로 **코사인을 앱-사이드로 계산**. 프로덕션은 벡터 가능 스토어 + ANN.)
- **text-to-SQL**: corpus 메타데이터 고정쿼리 → `iceberg_data.aml` 실데이터에 대한 NL→SQL
  (스키마카드+few-shot, SELECT-only 가드+LIMIT, 실행 에러 self-correction 1회)

---

## 운영 치트시트

```bash
export PATH="$PWD/bin/shims:$PATH"   # oc 등 macOS 셰임

# 코드 변경 후 재배포 (이미지 빌드 없음 — ConfigMap + 롤아웃)
cd integrations/rag
oc -n genai-apps create configmap rag-code   --from-file=ingest.py --from-file=reindex.py \
  --from-file=rag_common.py --from-file=agent.py --from-file=app.py --from-file=text2sql.py \
  --from-file=seed_aml.py --from-file=langflow_engine.py --dry-run=client -o yaml | oc apply -f -
oc -n genai-apps create configmap rag-static --from-file=index.html=static/index.html \
  --from-file=app.css=static/app.css --from-file=app.js=static/app.js --dry-run=client -o yaml | oc apply -f -
oc apply -f ui.yaml
oc -n genai-apps rollout restart deploy/rag-ui

# Langflow 메인 엔진 env (ui.yaml에 포함). LANGFLOW_API_KEY는 rag-secrets에 추가 필요:
#   oc -n genai-apps patch secret rag-secrets --type merge -p "{\"stringData\":{\"LANGFLOW_API_KEY\":\"$LANGFLOW_APIKEY\"}}"
# 파이썬 엔진으로 강제: oc -n genai-apps set env deploy/rag-ui RAG_ENGINE=python

# AML text-to-SQL 데이터셋 시드 (1회; iceberg_data.aml). job yaml의 <PRESTO_HOST>/<ASTRA_HOST>/<WX_PROJECT_ID>는 실제값 치환
oc -n genai-apps delete job rag-seed-aml --ignore-not-found; oc apply -f seed-aml-job.yaml

# KG 백필(벡터 emb·엔티티 해소·온톨로지) — kg 컬렉션 drop 후 전체 force 재적재
oc -n genai-apps delete job rag-reextract --ignore-not-found; oc apply -f reextract-job.yaml

# PDF 코퍼스(신용정보법/특정금융정보법)는 UI 파일 업로드 또는 파드에서 ingest_source(file_bytes=...)로 적재
#   (source=file:*; CronJob reindex는 file:/inline:을 건너뜀)

# 상태 확인
oc -n genai-apps get pods,route -l app=rag-ui
curl -sk https://rag-ui-genai-apps.apps.<CLUSTER_DOMAIN>/api/docs
```

## Langflow 메인 엔진 (챗 UI 오케스트레이션)

챗 UI `/api/chat`가 **Langflow(1.8.0) 에이전트에 오케스트레이션을 위임**한다(`langflow_engine.py`). Langflow의
watsonx granite Agent가 rag-ui 도구 엔드포인트를 tool-calling으로 호출하고, **실행 응답에서 각 도구 출력을
꺼내 근거 패널(청크·KG·SQL)을 그대로 복원**한다. 장애 시 in-process 파이썬 에이전트로 폴백(`RAG_ENGINE`).

도구 엔드포인트(토큰 게이트, 내부 URL `http://rag-ui.genai-apps.svc.cluster.local:8080`):

| 엔드포인트 | 용도 |
|---|---|
| `POST /api/tool/vector` | OpenSearch 하이브리드 검색 |
| `POST /api/tool/graph`  | AstraDB KG 1홉 서브그래프 |
| `POST /api/tool/sql`    | text-to-SQL (iceberg_data.aml) |

- 헤더 `X-Tool-Token: <APP_PASSWORD>`(미설정 시 `TOOL_TOKEN`).
- rag-ui env: `RAG_ENGINE=langflow`, `LANGFLOW_URL`(내부 svc), `LANGFLOW_FLOW_NAME`(이름으로 resolve),
  `LANGFLOW_API_KEY`(`rag-secrets`). **대화별 session_id**(브라우저당 uuid, UI가 전송)로 **멀티턴** 메모리
  유지 + 사용자 격리. "새 대화" 버튼이 새 session_id로 리셋. `session_id` 미전송 시 호출마다 새 id(무상태).

**플로우**: `ChatInput → Agent(IBM watsonx.ai, granite-4-h-small) → ChatOutput`, Agent가 3개 커스텀 툴
(`search_documents`/`lookup_relationships`/`query_business_data`)을 tool-calling으로 선택. 라이브 검증:
"high 고객 수?"→query_business_data→"7명", 관계 질문→KG 16엣지, 개념 질문→vector. 시크릿 스크럽본:
[`integrations/genai/flows/agentic-rag.json`](integrations/genai/flows/agentic-rag.json) (import 후
`<WX_APIKEY>`/`<WX_PROJECT_ID>`/`<TOOL_TOKEN>` 치환). 재생성 스크립트 `flows/build_flow.py`, 가이드
[`flows/README.md`](integrations/genai/flows/README.md).
> granite-3-8b-instruct는 이 환경 Langflow watsonx 컴포넌트 모델목록에 없어 **granite-4-h-small** 사용
> (rag-ui 파이썬 엔진은 REST chat으로 granite-3-8b-instruct 유지 — 무관).

## 알아둘 한계 (데모 수용 범위)

- **무인증**: 적재/삭제 엔드포인트 공개(데모용). 운영 시 인증·outbox 트랜잭션 필요.
- **라우터 비결정성**: granite가 graph를 과소선택 → 관계/감독/규제 등 키워드 백스톱으로 보강. 그래도
  애매하면 "관계/지식그래프 엣지로" 식으로 명시.
- **그래프 순회 아님**: KG는 1홉 서브그래프 수준(멀티홉 경로탐색 X). AstraDB는 비벡터 NoSQL(서버사이드 ANN X)
  → 벡터 시드는 앱-사이드 코사인. 스케일·트래버설은 프로덕션 경로(CQL 파티션/그래프DB).
- **text-to-SQL 범위**: 생성 SQL은 read-only(SELECT-only+LIMIT 가드). 복잡 조인은 few-shot로 유도(완전 일반화 X).
- **docling-graph 미사용**: KG는 granite LLM 추출. docling-graph는 스캔PDF/복잡표용 옵션(점선 표기).
- **라우트 타임아웃 120s**: graph 질의(전체 KG 로드+합성)가 기본 30s를 넘겨 `haproxy...timeout: 120s` 필요.
