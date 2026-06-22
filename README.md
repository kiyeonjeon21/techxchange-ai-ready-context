# Lakehouse Assistant — Agentic RAG + Knowledge Graph on watsonx

> Explainable agentic RAG that routes each question to vector search, a knowledge graph, or SQL —
> built on **watsonx.data + watsonx.ai** and deployed on OpenShift. (IBM TechXchange Korea 워크숍 데모)

watsonx 스택 위에 올린 **설명가능한(explainable) 에이전틱 RAG + 지식그래프** 데모입니다.
질문마다 알맞은 검색 도구(**벡터 / 그래프 / SQL**)로 자동 라우팅하고, **답의 근거를 그대로 보여줍니다.**

## 왜 "에이전틱" RAG인가

- **자율적 도구 선택 (라우팅)** — 고정 파이프라인이 아니라, LLM(granite)이 질문을 읽고 **vector / graph / sql** 중 필요한 도구를 매번 스스로 골라 실행합니다.
- **다중 소스 검색·결합** — 한 질문에 여러 도구를 동시에 호출해 **OpenSearch(의미검색) + AstraDB(지식그래프) + Presto/Iceberg(SQL 집계)** 의 근거를 한 컨텍스트로 합성합니다.
- **근거 기반 응답·자기억제** — 검색된 컨텍스트만으로 답하고 **출처를 인용**하며, 근거가 부족하면 "정보 부족"이라고 **스스로 답을 보류**합니다(환각 억제).
- **투명한 추론 경로 (설명가능성)** — 어떤 도구를 썼는지 **tool-trace 배지**로, 무엇을 근거로 답했는지 **검색 청크·KG 서브그래프·SQL 결과 패널**로 그대로 노출합니다.

> 현재는 "계획 → 실행 → 합성"의 **단일 패스 라우터**입니다(결과를 보고 재검색하는 multi-step 반복 루프는 아직 아님).

## 라이브 데모

| 앱 | 링크 |
|---|---|
| RAG 챗 UI (메인) | 🔗 https://rag-ui-genai-apps.apps.itz-xe29ld.infra01-lb.tok04.techzone.ibm.com |
| Langflow (비주얼 플로우 빌더) | 🔗 https://langflow-genai-apps.apps.itz-xe29ld.infra01-lb.tok04.techzone.ibm.com |

**API 문서 (Swagger UI)**

| API | Swagger |
|---|---|
| docling-serve (문서 → markdown 변환) | 📑 https://docling-serve-genai-apps.apps.itz-xe29ld.infra01-lb.tok04.techzone.ibm.com/docs |
| Langflow | 📑 https://langflow-genai-apps.apps.itz-xe29ld.infra01-lb.tok04.techzone.ibm.com/docs |

> RAG 챗 UI는 공유 비밀번호 로그인으로 보호됩니다(`rag-secrets`의 `APP_PASSWORD`). TechZone 환경이라 접속이 안 될 수 있습니다.

## 데모 영상

https://github.com/user-attachments/assets/da2af598-677d-486a-b1b4-a5d7db979aef

## 무엇을 보여주나

- **에이전트 라우팅** — LLM(granite)이 질문을 보고 vector/graph/sql 도구를 선택, tool-trace 배지로 표시
- **설명가능성** — 답변마다 인용 + 근거 패널(검색된 청크·KG 서브그래프·SQL 결과)을 펼쳐 확인
- **No-code 즉석 적재** — UI에서 문서를 붙여넣기/URL/PDF로 추가 → **답이 즉시 바뀌고**, 삭제하면 원복
  → "grounding 있으면 정확, 없으면 환각"이라는 RAG의 본질을 클릭만으로 시연

## 아키텍처

![architecture](docs/architecture-highlevel.png)

| 역할 | 구성요소 |
|---|---|
| Chat UI + Agent | `rag-ui` (FastAPI 단일 컨테이너, OpenShift `genai-apps`) |
| 벡터 검색 | **OpenSearch** (watsonx.data) — kNN + BM25 하이브리드(RRF), 한글 cjk 분석기 |
| 지식그래프 | **AstraDB** (DataStax, 비벡터 NoSQL) — 엔티티/엣지 + 임베딩 시드·엔티티 해소 |
| SQL / text-to-SQL | **watsonx.data** Iceberg + Presto — AML 데이터셋 NL→SQL |
| 오케스트레이션(비주얼) | **Langflow** — 동일 도구를 에이전트 tool-calling으로 시연 |
| 임베딩 + LLM | **watsonx.ai** granite (embed 768d + granite-3-8b) |
| 문서 파싱 | docling-serve (텍스트/URL/PDF → markdown) |

상세 그림: [`docs/architecture.pdf`](docs/architecture.pdf) · 구축 여정: [`docs/wxdata-install-journey.md`](docs/wxdata-install-journey.md)

## 데모 코퍼스

한글 금융·컴플라이언스 6종(개인정보 보호법·신용정보법·마이데이터·전자금융거래법·전자금융감독규정·특정금융정보법)
+ 영문 README 2종. **md·PDF·URL 세 입력 경로가 섞여** 있습니다(md 4 + PDF 2 + URL 2). 법령 간 교차참조가
많아 **지식그래프가 의미를 갖는** 도메인으로 골랐습니다. SQL 경로는 별도 **AML/금융 데이터셋**(`iceberg_data.aml`:
거래·고객·의심거래보고)을 text-to-SQL로 질의합니다.

## 데모 ↔ 프로덕션 경로

데모는 무대에서 명료하게 동작하도록 단순화했고, 각 도구의 **프로덕션 강화 경로**를 분명히 합니다.

| 도구 | 데모(현재) | 프로덕션 경로 |
|---|---|---|
| 오케스트레이션 | 단일패스 라우터(granite) + Langflow 비주얼 플로우 | LangGraph plan→reflect→replan 루프, 체크포인트·HITL |
| SQL | text-to-SQL(스키마카드+few-shot, SELECT-only 가드, self-correction) on AML 시드 | 시맨틱 레이어/dbt, 행수준 보안·권한, 쿼리 검증·비용가드 |
| KG | Data API + 엔티티 임베딩 시드(앱-사이드 코사인) + 1홉 + 엔티티 해소·온톨로지 | 벡터 스토어 서버사이드 ANN, CQL 파티션 이웃조회(스케일), 그래프DB 트래버설·GraphRAG |
| 문서 | md/PDF/URL + docling 파싱 | 대용량 배치·OCR·표/레이아웃 추출 파이프라인 |

## 동작 방식 (요약)

```
질문 → [라우터] granite가 도구 선택 {vector, graph, sql} (관계형 질문은 graph 백스톱)
     → vector : OpenSearch 하이브리드(kNN + BM25, RRF)
       graph  : AstraDB KG — 엔티티 임베딩 시드 → 1홉 이웃 조회 → 별칭 병합·엔티티 해소
       sql    : text-to-SQL — granite가 Presto SELECT 생성 → 가드(SELECT-only+LIMIT) → 실행
                (실패 시 self-correction; iceberg_data.aml 비즈니스 데이터)
     → [합성] granite가 컨텍스트로 답변 생성 + 인용
     → UI: 답변 + tool-trace 배지 + 근거 패널(생성 SQL 포함)
```

문서 적재 시 한 번의 호출로 **4개 저장소에 동시 반영**(같은 doc_id):
OpenSearch(벡터) · AstraDB(KG) · doc_registry(추적) · Iceberg(인벤토리). 멱등 upsert + content-hash 증분.

## 저장소 구조

```
integrations/rag/      에이전트 + UI (app.py, agent.py, ingest.py, rag_common.py, static/, *.yaml)
integrations/rag/corpus/   한글 데모 문서(md)
integrations/{dbt,otel,spark,genai}/   dbt·OpenTelemetry·Spark·genai 앱 통합
docs/                  아키텍처 그림 + 구축 여정 기록
NOTE.md                데모 가이드 + 운영 치트시트
```

## 시작하기

1. `.env.example` → `.env` 복사 후 본인 값 입력 (watsonx.ai/AstraDB/watsonx.data 자격증명)
2. `integrations/rag/*.yaml`의 플레이스홀더(`<CLUSTER_DOMAIN>`, `<WX_PROJECT_ID>`, `<ASTRA_HOST>`,
   `<PRESTO_HOST>` 등)를 본인 클러스터 값으로 치환
3. 배포 명령은 [`NOTE.md`](NOTE.md)의 운영 치트시트 참고

> 🔐 채팅 UI는 **공유 비밀번호 로그인**으로 보호됩니다(서명된 세션 쿠키). `rag-secrets`의 `APP_PASSWORD`
> 키로 비번을 설정하며, 키가 없으면 인증 없이 열립니다. 적재/삭제는 로그인 후 누구나 가능(데모 목적)이라
> 운영에는 사용자별 인증·트랜잭션 보강이 필요합니다.

## 기술 노트

- watsonx.data Presto는 **PrestoDB**(`presto-python-client` 사용, trino 아님)
- OpenSearch는 faiss 없음 → **lucene kNN**, 한글 BM25는 내장 **cjk 분석기**(nori는 operator 관리상 미설치)
- KG 추출은 granite LLM(`extract_kg`) — 관계 라벨 snake_case 정규화 + 엔티티 별칭 병합
- 내부 레지스트리 Removed → 이미지 빌드 없이 **ConfigMap + 런타임 pip-install** 패턴으로 배포

## License

MIT — see [LICENSE](LICENSE). 데모/교육 목적으로 자유롭게 사용하세요.

---

Built with IBM watsonx.data, watsonx.ai, and OpenShift.

Created by an AI Engineer at **IBM Client Engineering, Korea** for an **IBM TechXchange Korea** workshop.
IBM Client Engineering Korea의 AI Engineer가 **IBM TechXchange Korea** 워크숍을 위해 제작했습니다.

> "watsonx", "IBM TechXchange" 등은 IBM의 상표입니다. 데모 코드는 MIT 라이선스로 제공됩니다.
