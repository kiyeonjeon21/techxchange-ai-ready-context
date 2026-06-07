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

| 문서 | 종류 |
|---|---|
| 개인정보 보호법 / 신용정보법 / 마이데이터 / 전자금융거래법 / 전자금융감독규정 / 특정금융정보법 | 직접 작성 md (`integrations/rag/corpus/`) |
| Docling README / Langflow README | URL(GitHub raw) |

---

## 시연 시나리오 (순서대로)

### 1. 첫 화면
한글 추천 질문 4개가 4개 경로를 각각 대표한다. 클릭하면 바로 질의된다.

### 2. 세 가지 검색 경로 (tool-trace 배지 + 근거 패널로 확인)

| 경로 | 예시 질문 | 보여줄 것 |
|---|---|---|
| **vector** (OpenSearch) | "가명정보란 무엇이고 동의 없이 쓸 수 있는 목적은?" | 답변 + 인용칩 + "Retrieved passages" 패널(청크별 유사도 바) |
| **graph** (AstraDB KG) | "전자금융거래법의 접근매체와 감독기관의 관계를 지식그래프로 보여줘" | "Knowledge subgraph" 패널(엔티티 칩 + 엣지 트리플) |
| **sql** (Presto/Iceberg) | "인덱싱된 문서는 총 몇 개이고 각각 청크 수는?" | "Corpus / SQL rows" 패널(Iceberg 테이블) |
| **hybrid** | "자금세탁 의심거래는 어디에 보고하고, VASP의 의무는?" | vector+graph 배지 동시 점등, 풍부한 답변 |

> 라우팅·검색·합성은 전부 watsonx.ai granite-3-8b 직접 호출(Langflow 미사용). 답변 언어는 질문 언어를 따른다(한글→한글).

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
질문 → [라우터] granite가 도구 선택 {vector,graph,sql}
     → vector: OpenSearch 하이브리드(kNN+BM25, RRF 융합, k=8)
       graph : AstraDB KG 전체 로드 → 벡터시드+관련도 정렬 → 1홉 서브그래프
       sql   : Presto로 Iceberg corpus 테이블 집계
     → [합성] granite가 컨텍스트로 답변 생성 + 인용
     → UI: 답변 + tool-trace 배지 + 근거 패널
```

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
- **KG 정규화**: 엔티티명 정규화·별칭 병합(마이데이터=마이데이터 사업자), 관계 라벨 snake_case 강제
  (조사 "의/는" 제거), 자기루프 제거

---

## 운영 치트시트

```bash
export PATH="$PWD/bin/shims:$PATH"   # oc 등 macOS 셰임

# 코드 변경 후 재배포 (이미지 빌드 없음 — ConfigMap + 롤아웃)
cd integrations/rag
oc -n genai-apps create configmap rag-code   --from-file=ingest.py --from-file=reindex.py \
  --from-file=rag_common.py --from-file=agent.py --from-file=app.py --dry-run=client -o yaml | oc apply -f -
oc -n genai-apps create configmap rag-static --from-file=index.html=static/index.html \
  --from-file=app.css=static/app.css --from-file=app.js=static/app.js --dry-run=client -o yaml | oc apply -f -
oc apply -f ui.yaml
oc -n genai-apps rollout restart deploy/rag-ui

# 코퍼스 md 변경 후 (rag-corpus 갱신 + 전체 재적재)
oc -n genai-apps create configmap rag-corpus \
  --from-file=01_pipa.md=corpus/01_개인정보보호법.md --from-file=02_credit.md=corpus/02_신용정보법.md \
  --from-file=03_mydata.md=corpus/03_마이데이터.md --from-file=04_efta.md=corpus/04_전자금융거래법.md \
  --from-file=05_fsec.md=corpus/05_전자금융감독규정.md --from-file=06_aml.md=corpus/06_특정금융정보법.md \
  --dry-run=client -o yaml | oc apply -f -
oc -n genai-apps delete job rag-reextract --ignore-not-found; oc apply -f reextract-job.yaml

# 상태 확인
oc -n genai-apps get pods,route -l app=rag-ui
curl -sk https://rag-ui-genai-apps.apps.<CLUSTER_DOMAIN>/api/docs
```

## 알아둘 한계 (데모 수용 범위)

- **무인증**: 적재/삭제 엔드포인트 공개(데모용). 운영 시 인증·outbox 트랜잭션 필요.
- **라우터 비결정성**: 관계형 질문을 가끔 vector로만 분류 → "관계/지식그래프 엣지로" 식으로 명시하면 graph 확실히 켜짐.
- **그래프 순회 아님**: KG는 1홉 서브그래프 수준(멀티홉 경로탐색 X). AstraDB는 비벡터 NoSQL.
- **docling-graph 미사용**: KG는 granite LLM 추출. docling-graph는 스캔PDF/복잡표용 옵션(점선 표기).
- **라우트 타임아웃 120s**: graph 질의(전체 KG 로드+합성)가 기본 30s를 넘겨 `haproxy...timeout: 120s` 필요.
