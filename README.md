# Lakehouse Assistant — Agentic RAG + Knowledge Graph on watsonx

> Explainable agentic RAG that routes each question to vector search, a knowledge graph, or SQL —
> built on **watsonx.data + watsonx.ai** and deployed on OpenShift. (TechXchange 데모)

watsonx 스택 위에 올린 **설명가능한(explainable) 에이전틱 RAG + 지식그래프** 데모입니다.
질문마다 알맞은 검색 도구(**벡터 / 그래프 / SQL**)로 자동 라우팅하고, **답의 근거를 그대로 보여줍니다.**

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
| 지식그래프 | **AstraDB** (DataStax, 비벡터 NoSQL) — 엔티티/엣지 |
| SQL / 인벤토리 | **watsonx.data** Iceberg + Presto |
| 임베딩 + LLM | **watsonx.ai** granite (embed 768d + granite-3-8b) |
| 문서 파싱 | docling-serve (텍스트/URL/PDF → markdown) |

상세 그림: [`docs/architecture.pdf`](docs/architecture.pdf) · 구축 여정: [`docs/wxdata-install-journey.md`](docs/wxdata-install-journey.md)

## 데모 코퍼스

한글 금융·컴플라이언스 6종(개인정보 보호법·신용정보법·마이데이터·전자금융거래법·전자금융감독규정·특정금융정보법)
+ 영문 README 2종. 법령 간 교차참조가 많아 **지식그래프가 의미를 갖는** 도메인으로 골랐습니다.

## 동작 방식 (요약)

```
질문 → [라우터] granite가 도구 선택 {vector, graph, sql}
     → vector : OpenSearch 하이브리드(kNN + BM25, RRF)
       graph  : AstraDB KG 로드 → 벡터시드 + 관련도 정렬 → 1홉 서브그래프
       sql    : Presto로 Iceberg corpus 집계
     → [합성] granite가 컨텍스트로 답변 생성 + 인용
     → UI: 답변 + tool-trace 배지 + 근거 패널
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

> ⚠️ 이 데모는 인증 없이 적재/삭제가 가능합니다(데모 목적). 운영에는 인증·트랜잭션 보강이 필요합니다.

## 기술 노트

- watsonx.data Presto는 **PrestoDB**(`presto-python-client` 사용, trino 아님)
- OpenSearch는 faiss 없음 → **lucene kNN**, 한글 BM25는 내장 **cjk 분석기**(nori는 operator 관리상 미설치)
- KG 추출은 granite LLM(`extract_kg`) — 관계 라벨 snake_case 정규화 + 엔티티 별칭 병합
- 내부 레지스트리 Removed → 이미지 빌드 없이 **ConfigMap + 런타임 pip-install** 패턴으로 배포

## License

MIT — see [LICENSE](LICENSE). 데모/교육 목적으로 자유롭게 사용하세요.

---

Built with IBM watsonx.data, watsonx.ai, and OpenShift · created for an **IBM TechXchange workshop in Korea (대한민국)**.

> 비공식 개인 데모 자료입니다 (IBM의 공식 산출물 아님). "watsonx", "IBM TechXchange" 등은 IBM의 상표입니다.
