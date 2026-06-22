# 발표 자료 이미지 (`docs/img/`)

`docs/presentation.md`에서 참조하는 시각 자료입니다.

## 캡처된 데모 스크린샷 (라이브 앱, Playwright)

| 파일 | 슬라이드 | 내용 |
|---|---|---|
| `01-home.png` | 첫 화면 | 웰컴 + 추천 질문 4개(4경로 대표) |
| `02-vector.png` | 데모 1a | vector 경로 — 답변 + 인용칩 + Retrieved passages(유사도 바) |
| `03-graph.png` | 데모 1b | graph 경로 — KG 서브그래프(엔티티 칩 + 엣지 트리플) |
| `04-sql.png` | 데모 1c | sql 경로 — Presto/Iceberg 집계 + Corpus/SQL rows 패널 |
| `05-login.png` | 보안 | 공유 비밀번호 로그인 게이트 |
| `06-corpus-drawer.png` | 데모 2 | No-code 코퍼스 관리 드로어(텍스트/URL/파일 + 목록·삭제) |

## 로고 (직접 추가 필요 — 상표 자산)

공개 GitHub repo에는 IBM/제품 로고 이미지를 **커밋하지 않습니다**(상표). 발표용 PDF/PPTX를
만들 때만 아래 파일을 이 폴더에 두고 `presentation.md`의 해당 주석을 해제하세요.

| 기대 파일명 | 용도 | 슬라이드 |
|---|---|---|
| `ibm-logo.png` | IBM 8-bar 로고 | 타이틀 · 마무리 |
| `logo-opensearch.png` | OpenSearch | 데이터 스토어 |
| `logo-astradb.png` | AstraDB / DataStax | 데이터 스토어 |
| `logo-watsonx-data.png` | watsonx.data | 데이터 스토어 |
| `logo-watsonx-ai.png` | watsonx.ai | 데이터 스토어 |

> IBM 브랜드 가이드라인(8-bar 로고, 최소 여백, 배경 대비)을 준수하세요. 로고는 IBM 공식
> 브랜드 자산에서 받아 사용합니다.

## PDF/PPTX 변환

```bash
npx @marp-team/marp-cli docs/presentation.md -o docs/presentation.pdf
npx @marp-team/marp-cli docs/presentation.md --pptx -o docs/presentation.pptx
```
