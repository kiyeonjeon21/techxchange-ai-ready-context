# watsonx.data 설치 여정 — 기록 & 학습 노트

> 환경: IBM TechZone OCP `<CLUSTER_NAME>` (tok04) · OCP 4.18 · IBM Software Hub / CPD **5.3.1** · watsonx.data **2.3.x**
> 작업일: 2026-06-06 · 작성자 자동화: Claude Code
> 목표: 기존 CPD foundation 위에 **watsonx.data(Base)** 를 cpd-cli로 설치 + 통합(OpenSearch / OpenTelemetry / dbt)

---

## 0. 큰 그림 (개념 정리)

```
OpenShift (OCP 4.18)
  └─ IBM Software Hub (= Cloud Pak for Data 의 새 이름, 컨트롤 플레인)
       ├─ Foundation: cp-foundation (Zen/CPFS) + lite      ← cloud-pak-deployer가 설치
       └─ Services(cartridges):
            └─ watsonx.data  (의존: opensearch → ccs → analyticsengine)   ← 우리가 cpd-cli로 추가
```

- **Software Hub** 는 CPD 5.x의 새 브랜드. "control plane(Zen)" 위에 서비스(cartridge)를 얹는 구조.
- **cloud-pak-deployer** = IBM TechZone이 제공하는 자동 설치기(Ansible). foundation을 OLM(Operator)로 깔아줌.
- **watsonx.data** 는 foundation이 올라온 뒤 **cpd-cli** 로 추가 설치. 의존 서비스가 자동으로 함께 설치됨.

### 핵심 네임스페이스 / 스토리지
| 용도 | 값 |
|---|---|
| CPD 인스턴스(컨트롤 플레인) | `cpd` |
| CPD operators | `cpd-operators` |
| License Service | `ibm-licensing` |
| 파일 스토리지 (RWX) | `ocs-external-storagecluster-cephfs` |
| 블록 스토리지 (RWO) | `ocs-external-storagecluster-ceph-rbd` |

---

## 1. 사전 점검에서 배운 것

- `.env` 의 OCP/bastion 자격증명으로 `oc login` (kubeadmin).
- 처음엔 CPD namespace가 없었고 `cloud-pak-deployer` Pod이 **foundation을 설치하는 중**이었음.
  → "이미 설치됨"이 아니라 "설치 진행 중"임을 로그로 확인하는 게 중요.
- **두 종류의 deployer Pod 구분**:
  - `deployer-cp4d-ansible-runner-*` (namespace `default`) = **바깥 오케스트레이터**. 그냥
    `oc logs -f job/cloud-pak-deployer` 로 job 끝나길 폴링 → `FAILED - RETRYING: Waiting for Cloud Pak Deployer`
    가 계속 찍히지만 **정상**(재시도 예산 ~1440).
  - `cloud-pak-deployer-*` (namespace `cloud-pak-deployer`) = **실제 작업자**. 진짜 진행은 여기.

> 교훈: Ansible의 `FAILED - RETRYING: Wait until ...` 는 "조건 충족까지 재시도"이지 오류가 아니다.
> 진짜 실패는 `fatal:` 또는 PLAY RECAP의 `failed=1+`.

---

## 2. 막혔던 문제 #1 — cert-manager 준비 레이스 (foundation 단계)

**증상**: deployer가 `Wait until IBMLicensing exists and is active` 를 무한 재시도. License Service 산출물이
(namespace/operator/CRD/catalog) 전혀 안 생김.

**원인 (확정)**:
- `apply-cluster-components` 의 사전검사 `setup_singleton.sh --check-cert-manager` 가 **result=1** 로 실패하며
  오해 소지 있는 메시지 출력: `[ERROR] The IBM Certificate Manager (ibm-cert-manager) is installed...`
- 실제로는 IBM cert-manager가 **없고** Red Hat cert-manager만 정상. 문제는 **타이밍**:
  - Red Hat cert-manager webhook/controller Ready = `08:55:44`
  - 검사 실행 = `08:55:35` (9초 빠름)
  - 검사는 test Issuer/Certificate를 만들어 검증 → webhook 미준비 → 실패 → 잘못된 에러.
- 실제 로그 위치: deployer pod 내부 `/tmp/work/olm-utils-ansible-log/apply-cluster-components-*.log`
  (`/Data/cpd-status/log/*-apply-cluster-components.log` 는 0바이트 = stdout 리다이렉트일 뿐).

**해결**: `cloud-pak-deployer` Job pod 삭제 → Job이 pod 재생성 → 멱등 재개. 이번엔 cert-manager가 Ready라
검사 통과. 그 뒤 License Service → Foundational Services → Zen(`zenservice`)·`ibmcpd` 순으로 정상 설치.

> 교훈: 0바이트 로그 파일을 만나면 olm-utils의 **실제 로그(`/tmp/work/olm-utils-ansible-log/`)** 를 봐라.
> deployer는 멱등이라 **pod 삭제 = 안전한 재개**.

추가로 본 정상 현상: Zen 기동 초기 `zen-core` init 컨테이너가 `zen-objstore-init` 에서 오브젝트 스토어가
다 안 채워진 시점에 먼저 떠서 CrashLoopBackOff → 스토어 채워지면 자동 해소.

---

## 3. cpd-cli 셋업에서 배운 것 (macOS)

`cpd-cli manage *` 는 **olm-utils 컨테이너를 로컬에서 실행**한다 → 로컬 컨테이너 런타임 + pty 필요.

| 문제 | 해결 |
|---|---|
| `apply-olm`/`manage` 가 podman을 찾는데 머신 고장 (gvproxy 소켓 에러) | **docker(Rancher Desktop)** 사용 |
| cpd-cli가 podman 바이너리를 우선 선택 | `bin/cpd-cli` 래퍼가 **제한된 PATH**(`bin/shims` + 시스템) 로 podman을 숨김. shims = oc/docker/kubectl/nerdctl + **docker-credential-* helper** (credsStore=osxkeychain) |
| 이미지 pull 인증 | `docker login cp.icr.io -u cp -p $IBM_ENTITLEMENT_KEY` |
| `docker exec -it` → `the input device is not a TTY` (비대화형) | 모든 호출 앞에 `script -q /dev/null` 로 의사 TTY 부여 |
| `login-to-ocp` 플래그 | oc로 그대로 전달됨 → `--insecure-skip-tls-verify=true` (NOT `--skip-tls-verify`) |

```sh
# 동작하는 로그인
script -q /dev/null ./bin/cpd-cli manage login-to-ocp \
  --server=$OCP_API_URL -u kubeadmin -p '***' --insecure-skip-tls-verify=true
```

---

## 4. watsonx.data 설치 절차 (5.3.x에서 실제로 필요한 단계)

> ⚠️ `apply-olm` / `apply-cr` 는 **5.3.0에서 deprecated** — `apply-olm`은 도움말만 찍고 EXIT 0(아무것도 안 함).
> 5.3.x는 **`install-components`** 하나로 처리.

순서대로 막히면서 알아낸 진짜 절차:

1. **patch-download** — 클러스터에 day0 patch(ID 6)가 깔려 있으면 로컬 메타데이터(ID 0)가 오래됐다며 거부.
   ```sh
   script -q /dev/null ./bin/cpd-cli manage patch-download --release=5.3.1
   ```
2. **case-download** — 5.3.x 설치는 **helm 차트 기반**이라 CASE 번들(차트)을 로컬에 받아야 함.
   watsonx_data 의 전이 의존성도 같이 받지만 **`cpd_platform`(ibm-cp-datacore)** 는 따로 받아야 했음.
   ```sh
   script -q /dev/null ./bin/cpd-cli manage case-download --release=5.3.1 --components=watsonx_data --from_oci=true
   script -q /dev/null ./bin/cpd-cli manage case-download --release=5.3.1 --components=cpd_platform --from_oci=true
   ```
3. **cluster-scoped(CRD) 차트 수동 적용** — ⭐ 가장 큰 함정 (아래 §5).
4. **install-components** — 네임스페이스 차트로 operator+CR 설치, 의존성 순서대로 reconcile 대기.
   ```sh
   script -q /dev/null ./bin/cpd-cli manage install-components \
     --license_acceptance=true --release=5.3.1 --components=watsonx_data \
     --operator_ns=cpd-operators --instance_ns=cpd \
     --block_storage_class=ocs-external-storagecluster-ceph-rbd \
     --file_storage_class=ocs-external-storagecluster-cephfs
   ```

설치 의존성 순서: **platform-config → opencontent_opensearch → ccs → analyticsengine → watsonx_data**

---

## 5. 막혔던 문제 #2 — install-components가 CRD(cluster-scoped) 차트를 안 깐다 ⭐

**증상**: install-components가 `analyticsengine` 에서 helm 렌더 에러로 중단.
```
analyticsengine/templates/03-cr.yaml ... lookup "ae.cpd.ibm.com/v1" "AnalyticsEngine" ...
  error: the server could not find the requested resource
```

**원인 (확정)**:
- 각 컴포넌트는 helm 차트가 **2개**: `<comp>-cluster-scoped`(**CRD + cluster RBAC**) + `<comp>`(operator + CR).
- 이 olm-utils 5.3.1 빌드의 `install-components` 는 **네임스페이스 차트만** 설치하고 **`-cluster-scoped`(CRD)
  차트는 설치하지 않음**. (`helm list -A` 에 cluster-scoped 릴리스가 하나도 없음으로 확인)
- analyticsengine 차트의 `03-cr.yaml` 은 렌더 시점에 `lookup` 으로 CRD를 조회 → CRD 미등록 시 helm이
  **하드 에러**. (opensearch/ccs 차트는 부드럽게 넘어가서 operator만 깔리고 CR은 생성 안 됨)
- ansible-operator 자체도 CRD를 자가 등록하지 않음: 로그 `if kind is a CRD, it should be installed before calling Start`.

**해결**: watsonx_data 체인의 cluster-scoped 차트 4개를 helm으로 직접 적용 → CRD 등록 → install-components 재실행.
```sh
WORK=cpd-cli-workspace/olm-utils-workspace/work
OVR=$(ls -t $WORK/olm-utils-ansible-log/override_file_*.yaml | head -1)
for n in opencontent-opensearch ccs analyticsengine watsonx-data; do
  chart=$(find $WORK/offline -name "${n}-cluster-scoped-*.tgz" | head -1)
  helm upgrade --install -n cpd "${n}-cluster-scoped" "$chart" -f "$OVR"
done
```
등록된 CRD: `analyticsengines.ae.cpd.ibm.com`, `ccs.ccs.cpd.ibm.com`,
`clusters/nodepools.opensearch.cloudpackopen.ibm.com`,
`wxds / wxdaddons / wxdengines / wxdaddonpremiums.watsonxdata.ibm.com`.

> 교훈: helm `lookup` 은 **렌더 시점**에 라이브 클러스터를 조회한다. 같은 차트가 설치할 CRD를 lookup하면
> 첫 설치에서 깨진다(chicken-and-egg). CPD 컴포넌트는 CRD를 별도 `*-cluster-scoped` 차트로 분리해 두며,
> 이 차트를 먼저 적용해야 한다.

---

## 6. 통합(integration) 개념 정리

| 통합 | 정체 | 설치 방식 |
|---|---|---|
| **OpenSearch** | watsonx.data 의 **의존 서비스**(`opencontent_opensearch`) | watsonx.data 설치 시 자동 포함 |
| **OpenTelemetry** | 별도 서비스 아님 — watsonx.data **엔진(Presto/Trino)의 OTel export** 런타임 설정 | 설치 후 엔진/콘솔에서 구성 |
| **dbt** | 클러스터 설치 X — 클라이언트 측 **`dbt-trino` 어댑터**로 Presto 엔드포인트 연결 | 로컬/클라이언트에서 구성 |

---

## 7. 현재 상태 (2026-06-06 기준 — watsonx.data 설치 완료 ✅)

- ✅ foundation(`ibmcpd`, `zenservice`) Completed — Software Hub 콘솔:
  `https://cpd-cpd.apps.<CLUSTER_DOMAIN>`
- ✅ cpd-cli 런타임/로그인, patch-download, case-download 완료
- ✅ cluster-scoped CRD 4개 등록
- ✅ **install-components 성공 (EXIT=0, 20:22 UTC)** — platform-config → opensearch → ccs →
  analyticsengine(reconciled 5.3.6) → **watsonx_data(2.3.4)** 순서로 설치
- ✅ **watsonx.data 인스턴스 `wxd/lakehouse` = Completed**, 서비스 pod **14개 Running**
  (`ibm-lh-lakehouse-*`: mds-rest/thrift 메타스토어, minio 오브젝트스토어, ces-0/1/2 컴퓨트엔진,
  cas, qhmm, validator, postgres-edb x3)
- ✅ **콘솔 등록 `watsonx-data-extension` = Completed** → CPD 콘솔 좌측 메뉴에 watsonx.data 노출
- ⬜ 통합 구성 (OpenSearch 동작 확인 / OpenTelemetry — `wxd` CR에 `OPENTELEMETRY` 필드 내장 / dbt-trino)

> 참고: 기본 설치엔 별도 `wxdengine`(Presto 엔진) CR이 아직 없음 — 콘솔/API에서 엔진을 생성해 사용.
> `wxdaddon` 은 post-install 마무리(80% → Completed로 수렴 중)지만 인스턴스/콘솔은 이미 사용 가능.

---

## 7.6 통합 실행 결과 (task #5) — 2026-06-06

> watsonx.data REST API는 **v3**, 경로에 instanceId 포함: `/lakehouse/api/v3/<iid>/...`,
> 헤더 `Authorization: Bearer` + `LhInstanceId`. 토큰: `POST $CPD/icp4d-api/v1/authorize`.
> (v2 경로는 502 — 사용 금지.) 상세는 메모리 `wxdata-rest-api-v3` 참고.

### ✅ dbt — 완료 (엔드투엔드 동작)
- **Presto 엔진 생성**: `POST /v3/<iid>/presto_engines`, 핵심값 **`node_type: "bx2.4x16"`**
  (콘솔 생성 요청을 Playwright로 캡처해 확보). 엔진 `lakehouse-<PRESTO_ENGINE>` (starter, 단일노드) RUNNING.
  - 외부 엔드포인트: `ibm-lh-lakehouse-<PRESTO_ENGINE>-presto-svc.apps.<domain>` :443 https.
- **어댑터**: `dbt-watsonx-presto` (profile `type: watsonx_presto`), repo `integrations/dbt/` (venv py3.12).
- **인증(중요)**: `user: ibmlhapikey_cpadmin` + `password: <CPD apikey>` (`user: cpadmin` 단독은 AMS 401).
  apikey 생성: `POST $CPD/usermgmt/v1/user/apiKey`.
- 결과: `dbt debug` = OK, `dbt run` → `iceberg_data.dbt_demo.example_model` 뷰 생성 성공.
- 실행: `export WXD_API_KEY=...; integrations/dbt/.venv/bin/dbt run --profiles-dir integrations/dbt --project-dir integrations/dbt`

### ✅ Spark 잡 테스트 — Spark→Iceberg→Presto (검증 완료)
Spark 엔진 `spark-demo`로 PySpark 잡 제출 → `iceberg_data.demo_db.spark_iceberg_test` Iceberg 테이블
생성/INSERT(3행) → **Presto/dbt로 동일 테이블 조회 성공**. 스크립트 `integrations/spark/iceberg_demo.py`,
제출 `integrations/spark/submit-iceberg-job.sh`. 상세·트러블슈팅은 메모리 `wxdata-spark-job-blocker`.
핵심(자가 제출 잡이 카탈로그에 접근하려면 3가지 필요 — watsonx 내장 signer의 비공개 apiKey를 직접 MinIO creds로 우회):
1. 앱 .py는 **비카탈로그 버킷**(`spark-apps`)에 두고 Simple S3 creds로 fetch (카탈로그 버킷은 Watsonx signer가 막음).
2. 메타스토어 PLAIN 인증: `spark.hive.metastore.client.plain.username=ibmlhapikey_cpadmin` + password=apikey
   (`cpadmin`만 쓰면 MDS 500 "unable to find instance").
3. `spark-apps`+`iceberg-bucket` 둘 다 `aws.credentials.provider=SimpleAWSCredentialsProvider`+MinIO 키로
   override하고 주입된 Watsonx signer(`s3.signing-algorithm`/`signing-algorithm`/`custom.signers`)를 비움.

### ✅ OpenTelemetry → OCP Prometheus 실수집 (검증 완료)
파이프라인: `presto JMX(:9090) → watsonx.data OTel collector(prometheus receiver→exporter :8889)
→ OCP UWM Prometheus(ServiceMonitor) → OCP Thanos(콘솔 Observe>Metrics)`. 상세는 메모리
`wxdata-otel-ocp-prometheus`. 요약:
1. UWM 활성화: `cluster-monitoring-config`(ns openshift-monitoring)에 `enableUserWorkload: true` 추가(기존 ACM 설정 보존).
2. OTel collector cm `ibm-lh-otel-collector-config-cm`에 prometheus exporter(:8889)+metrics 파이프라인 추가
   후 엔진 pod 재시작 → :8889/metrics ~800 시리즈(jvm/jmx + OTel 속성). (cm은 WxdEngine 소유→reconcile 시 되돌아갈 수 있음)
3. `integrations/otel/servicemonitor-wxd-presto.yaml`: Service(:8889)+ServiceMonitor → UWM 스크레이프.
4. 검증: Thanos에서 `jmx_scrape_duration_seconds{namespace="cpd"}` 조회됨.
> 콘솔 확인: OCP 콘솔 > Observe > Metrics → `jvm{namespace="cpd"}` 등 쿼리.

### (참고) 초기 OTel collector 배포 + watsonx.data OTel 활성화
- 표준 OTel Collector 데모 배포: `integrations/otel/otel-collector-demo.yaml` (cpd ns, OTLP 4317/4318,
  debug exporter) → Running, 수신 대기.
- `wxd/lakehouse` CR: `enable_opentelemetry_collector=true`, `event_listener_properties.tracing-enabled=true`,
  `traces/metrics_exporters=["otlp"]` 적용. watsonx.data 자체 OTel collector(`spark-hb-otel-collector`)도 가동.
- 한계: watsonx.data의 `otel_configuration`은 export 대상이 **Prometheus/Instana endpoint** 중심이라,
  임의 OTLP collector로 직접 보내는 필드가 없음. 데모 collector로 실제 수집을 끝까지 연결하려면
  watsonx.data OTel collector의 downstream(Prometheus remote-write/Instana) 설정 또는 별도 백엔드 필요.

### ✅ OpenSearch — 애드온 설치 + 서비스 생성 (Presto와 달리 2단계)
> OpenSearch는 Presto처럼 바로 create가 안 되고 **install → create-service** 2단계 (IBM 문서
> opensearch-installing / opensearch-creating-service). 상세·gotcha는 메모리 `wxdata-opensearch-install`.
1. **install (애드온)**: `case-download --components=ibm_wxd_opensearch` → cluster-scoped CRD 차트
   (`opensearch.opster.io`) helm 적용 → `install-components --components=ibm_wxd_opensearch`.
   (`cr_internal: true` → install은 **operator만** 설치)
2. **이미지 gotcha**: operator/클러스터 이미지가 `icr.io/cp/cpd/...`인데 pull secret엔 `cp.icr.io`만 있어
   ImagePullBackOff → operator deployment 4개 + OpenSearchCluster CR의 image 3개를 `icr.io/`→`cp.icr.io/`로 패치.
3. **SCC gotcha**: SA `wxd-opensearch-sa`(init runAsUser:0) → `oc adm policy add-scc-to-user anyuid -z wxd-opensearch-sa -n cpd`.
4. **create-service**: 콘솔 Add component>Services>OpenSearch (operator 설치 후 노출) 또는
   `POST /v3/<iid>/opensearch {display_name, origin:native, tshirt_size:"small", data_nodes:3, data_disk:"50Gi",
   storage_class_name:...}` → 201. **size 필드 = `tshirt_size`**. OpenSearchCluster `opensearchNNN` 생성
   (small=3 masters+3 data+3 coordinating+dashboards). 노드 readiness 후 HEALTH=green.

---

## 7.5 통합(integration) 계획 & 방법 (task #5)

> watsonx.data 2.3.x 문서 기준. 실행은 `wxdaddon` 이 **Completed** 된 뒤 진행.

### (A) OpenSearch — 내장, 동작 확인 위주
- watsonx.data는 OpenSearch를 내장(`opencontent_opensearch` 의존)하고 **OpenSearch API**로 접근 가능.
  주 용도: qhmm(Query History Monitoring & Management) 등 내부 검색/관측.
- 별도 cluster CR(`clusters.opensearch.cloudpackopen.ibm.com`)이 보이지 않으면 wxdaddon이 reconcile
  완료 시 생성/연결하는지 확인. 콘솔의 Infrastructure manager에서 OpenSearch 컴포넌트 확인.
- 확인 명령:
  ```sh
  oc get clusters.opensearch.cloudpackopen.ibm.com -A
  oc get pods -n cpd | grep -iE 'opensearch'
  oc get svc -n cpd | grep -iE 'opensearch|search'
  ```
- 참고: "Accessing the OpenSearch API" (watsonx.data 2.3.x 문서).

### (B) OpenTelemetry — `wxd` CR에 내장 설정
`wxd/lakehouse` CR spec에 OTel 설정이 이미 존재(기본 off):
```yaml
spec:
  enable_opentelemetry_collector: false      # → true 로 켜기
  otel_configuration:
    collector_properties:
      prometheus_endpoint: ""                  # Prometheus remote-write/scrape 대상
      prometheus_tls: false
      instana_endpoint: ""                     # Instana OTLP endpoint (선택)
      metrics_exporters: []                    # 예: ["prometheus"] / ["otlp"]
      metrics_receivers: []                    # 예: ["otlp"]
      traces_exporters: []                     # 예: ["otlp"]
      traces_receivers: []                     # 예: ["otlp"]
    event_listener_properties:
      tracing-enabled: false                   # Presto event listener 트레이싱
    milvus_properties: { trace_exporter: stdout }
```
활성화(예시):
```sh
oc -n cpd patch wxd lakehouse --type merge -p '{"spec":{"enable_opentelemetry_collector":true,
  "otel_configuration":{"collector_properties":{"metrics_exporters":["otlp"],"metrics_receivers":["otlp"],
  "traces_exporters":["otlp"],"traces_receivers":["otlp"]},"event_listener_properties":{"tracing-enabled":true}}}}'
```
→ watsonx.data 내장 OTel collector가 떠서 Presto/Spark 텔레메트리를 OTLP/Prometheus/Instana로 export.
대상 collector/백엔드(Prometheus·Grafana 등)는 별도 준비. (단순 실습은 `metrics_exporters:["logging"]`/stdout로 확인)

### (C) dbt — 클라이언트 측 어댑터 (`dbt-watsonx-presto` = dbt-trino 기반)
1. 콘솔에서 **Presto 엔진**을 먼저 생성(기본 설치엔 엔진 CR 없음).
2. 로컬에 어댑터 설치: `pip install dbt-watsonx-presto` (Spark 엔진용은 `dbt-watsonx-spark`).
3. 연결 정보: Presto host(콘솔 Connect 정보), port `443`, catalog/schema, 사용자 `ibmlhapikey` + CPD API key.
4. `~/.dbt/profiles.yml` 템플릿은 repo `integrations/dbt/profiles.example.yml` 참고.
5. `dbt debug` 로 연결 확인 → `dbt run`.

---

## 8. 유용한 점검 명령 모음

```sh
# foundation
oc get ibmcpd,zenservice -n cpd

# 컴포넌트 CR 상태
oc get ccs,analyticsengine -n cpd
oc get wxdaddon,wxd,wxdengine -n cpd

# CRD 등록 확인
oc get crd | grep -iE 'ae.cpd|ccs.cpd|opensearch|watsonxdata'

# helm 릴리스
helm list -n cpd

# install-components CR 상태 요약
script -q /dev/null ./bin/cpd-cli manage get-cr-status --cpd_instance_ns=cpd

# olm-utils 실제 로그
ls cpd-cli-workspace/olm-utils-workspace/work/olm-utils-ansible-log/
```

---

## 8.5 Chat UI (Agentic RAG + KG) — 단일 컨테이너 FastAPI

에이전틱 RAG/KG 결과를 사람이 보는 **설명가능한(explainable) 채팅 UI**. SPA+BFF로 쪼개지
않고 **단일 FastAPI 컨테이너**가 백엔드(`/api/chat` → `agent.run`)와 정적 프론트엔드(`/`, `/static`)를
함께 서빙. 백엔드만 자격증명을 쥐므로 브라우저로 키가 새지 않음.

- `integrations/rag/app.py` — FastAPI BFF. `POST /api/chat {question}` → `agent.run(q)`의
  구조화 응답 `{answer, route{vector,graph,sql}, citations, context{chunks,kg,sql}}` 반환. `/healthz`.
- `integrations/rag/static/{index.html,app.css,app.js}` — 바닐라(빌드 스텝 없음). IBM Plex
  (Sans/Mono/Serif) + 잉크 다크 테마, 도구별 색상 **vector=teal / graph=amber / sql=violet**.
  답변 마크다운(marked.js) + 인용 칩 + tool-trace 배지 + 접이식 컨텍스트 패널(RAG 청크 점수바 /
  KG 엔티티·엣지 / SQL 표)로 **근거를 그대로 노출**.
- 배포: 내부 레지스트리가 **Removed**라 빌드 없이 **런타임 pip-install 패턴**.
  `rag-code`(py) + `rag-static`(프론트) ConfigMap을 읽기전용 마운트 → `/tmp/app`로 복사(루트 `/`는
  restricted SCC라 쓰기 불가) → `uvicorn app:app`. env/secret은 `rag-reindex` CronJob과 동일하게
  `rag-secrets` + 클러스터 내부 URL 재사용. `integrations/rag/ui.yaml`(Deployment+Service+Route edge TLS).
- 검증: Route로 `/`, `/static/*`, `/healthz` 200; `/api/chat` 3개 라우트 모두 E2E 동작
  (vector=OpenSearch, graph=AstraDB KG, sql=Presto/Iceberg). Playwright 스크린샷으로 렌더 확인.

```bash
# 접속 (Route)
oc -n genai-apps get route rag-ui -o jsonpath='{.spec.host}'
# https://rag-ui-genai-apps.apps.<CLUSTER_DOMAIN>

# 코드 변경 후 재배포 = ConfigMap 갱신 + 롤아웃 재시작 (이미지 빌드 불필요)
cd integrations/rag
oc -n genai-apps create configmap rag-code   --from-file=ingest.py --from-file=reindex.py \
  --from-file=rag_common.py --from-file=agent.py --from-file=app.py --dry-run=client -o yaml | oc apply -f -
oc -n genai-apps create configmap rag-static --from-file=index.html=static/index.html \
  --from-file=app.css=static/app.css --from-file=app.js=static/app.js --dry-run=client -o yaml | oc apply -f -
oc -n genai-apps rollout restart deploy/rag-ui
```

---

## 8.6 데모 코퍼스 — 한글 금융·핀테크 컴플라이언스

영문 README 2개만으로는 **KG 경로가 빈약**해 에이전트 강점이 안 드러남 → 한글 + 기술 + tangible로
**금융 컴플라이언스 도메인** 채택. 핵심은 *"답이 한 문서에 없고 엔티티를 타고 가야 나오는"* 질문이 자연스러운 도메인.

- 직접 작성한 한글 문서 6종(`integrations/rag/corpus/`): 개인정보보호법·신용정보법·마이데이터·전자금융거래법·
  전자금융감독규정·특정금융정보법. **서로 교차참조**하도록 써서 멀티홉 KG 형성(~79 엔티티/76 엣지:
  마이데이터→신용정보법, STR→FIU, 전자금융업자→전자금융감독규정→금융보안원, VASP→특정금융정보법 …).
- 한글 위키백과는 **부적합**: docling http 소스가 30x 리다이렉트를 안 따라가 정식명 외에는 404,
  본문 네비 잡텍스트도 임베딩 오염. 깨끗한 md를 직접 쓰는 게 KG 품질·통제 면에서 우월.
- `ingest.py`에 `load_source()` 추가: **인자가 로컬 경로면 파일을 바로 읽고(docling 우회)**, URL이면 기존대로 fetch.
- 배포: `rag-corpus` ConfigMap(키는 ASCII `01_pipa.md` 등으로 매핑 — cm 키에 한글 불가) → `/corpus` 마운트.
  일회성 `ingest-job.yaml`(Job)으로 인제스트, `rag-reindex` CronJob에도 같은 마운트 추가(증분 유지).
- `agent.py` 합성 프롬프트에 "질문 언어로 답(한국어면 한국어)" 추가. rag-ui 라우트로 **한글 3경로 검증 완료**.
- **graph 경로 수정**: 기존 `tool_graph`는 (1) `astra(find {})`가 KG **첫 20개만** 읽고(전체 ~216), (2) 정규식이
  ASCII만 매칭해 한글에선 무작위 `edges[:8]` 반환. → `astra_find_all()`(페이지네이션) + 관련도 점수(질문 내 엔티티
  명시 언급 2점 + 벡터 시드 문서 1점, `os_vector_search`에 `doc_id` 추가)로 정렬·상위 16개. 허브 과확장 제거,
  on-topic 서브그래프만 노출. 검증: "전자금융거래법 접근매체·감독기관" → 전자금융거래법 서브그래프 정확 반환.
- docling sync 변환은 OCP **라우트에서 ~30초 타임아웃**("Task result not found") → 인제스트는 반드시 in-cluster.

```bash
# 코퍼스 재인제스트(코드/문서 변경 후)
cd integrations/rag
oc -n genai-apps create configmap rag-corpus \
  --from-file=01_pipa.md=corpus/01_개인정보보호법.md --from-file=02_credit.md=corpus/02_신용정보법.md \
  --from-file=03_mydata.md=corpus/03_마이데이터.md --from-file=04_efta.md=corpus/04_전자금융거래법.md \
  --from-file=05_fsec.md=corpus/05_전자금융감독규정.md --from-file=06_aml.md=corpus/06_특정금융정보법.md \
  --dry-run=client -o yaml | oc apply -f -
oc -n genai-apps delete job rag-ingest-kr --ignore-not-found; oc apply -f ingest-job.yaml
```

---

## 8.7 No-code 즉석 적재/삭제 + 경량 KG 정규화

"UI에서 즉석으로 문서를 넣었다 지웠다 → 답이 바뀌는" 데모(인증 없음). 채팅 UI에 **코퍼스 관리 드로어** 추가.

- `ingest.py` 를 **라이브러리화**: `ingest_source(source=None,*,title,text,file_bytes,filename,force)` /
  `delete_doc(doc_id)` / `list_docs()`. 본문 추출 분기: 텍스트 그대로 / 파일→docling `/v1/convert/file`(multipart) /
  URL·로컬경로→`load_source`. doc_id = sha1(논리 source: `inline:<제목>`/`file:<이름>`/url). CLI `main()`도 이를 호출.
- `app.py` 엔드포인트: `GET /api/docs`, `POST /api/ingest`(JSON text|url), `POST /api/ingest/file`(multipart),
  `POST /api/delete`. 동기 `def` → FastAPI 스레드풀(채팅 동시성 유지). `ui.yaml` pip에 **python-multipart** 추가(필수).
- 프론트(`static/`): topbar "⚙ 코퍼스" → 우측 드로어(탭 텍스트/URL/파일 + 제목·본문, "추가 적재", 문서목록+🗑삭제, 토스트).
  ⚠️ 드로어 CSS 함정: `.drawer{display:flex}`가 `[hidden]`(UA display:none)을 이겨 항상 보임 → `.drawer[hidden]{display:none!important}` 필요.
- **경량 KG 정규화**: `rag_common.norm_name()`(소문자·괄호·공백 제거 + 접미사 `사업자/회사` + 별칭맵 FIU/VASP/마이데이터…).
  ingest가 KG 문서에 `norm`/`src_norm`/`dst_norm` 저장(없으면 agent가 즉석 계산→기존 데이터 재적재 불필요).
  `tool_graph`가 정규화 기준 매칭 + (src_norm,rel,dst_norm) dedup + 별칭 노드 병합 + **자기루프 제거**.
- 검증(E2E): "CSAP 평가기관?" 적재 전 환각 → 텍스트 붙여넣기 적재 → 정확답+인용 → 삭제 → 환각 원복.
  파일 업로드(.md) 9엔티티 추출 성공. graph에서 `마이데이터`/`마이데이터 사업자` 단일 노드 병합 확인.

> 한계(데모 수용): 적재/삭제 **무인증**(공개 라우트), 동기 적재 ~5–20s.

### KG 관계 라벨 품질 개선 (후속)
- `extract_kg` 프롬프트에 "rel은 snake_case 관계 술어, 조사/문장조각 금지" + 통제어휘 힌트. 후처리 `_clean_rel()`이
  깨끗한 ASCII snake_case는 보존하고 한글 조각(의/는/점검·검사…)만 `related_to`로 강등.
  → 코퍼스 전체: **한글 rel 0, related_to ~2%(이전 49%), 의미 술어 52종**(regulates·supervised_by·reports_to·
  has_obligation·investigated_by·legal_basis…). 전체 강제 재추출(`reextract-job.yaml`)로 반영.
- **버그 발견·수정**: AstraDB `deleteMany`는 **호출당 ≤20개만 삭제**(페이지) → KG가 20개 넘는 문서 재적재 시 옛
  엣지 잔존. `rc.astra_delete_all()`(find로 빌 때까지 반복 deleteMany) 추가, `ingest_source`/`delete_doc`에 적용.
  재추출 잡은 고아 KG(레지스트리에 없는 doc_id)도 정리.

### OpenSearch 하이브리드 검색 (벡터 + BM25, RRF)
- `text` 필드는 원래부터 `{"type":"text"}`(BM25 색인 존재) — **쿼리만 벡터 단독**이었음(의도가 아니라 미구현).
  `rag_common.os_hybrid_search()` 추가: kNN + `match`(BM25)를 각각 날려 **RRF**(1/(60+rank), 점수 정규화 불필요)로 융합.
- `tool_vector`가 하이브리드 사용(**k=8**), `run()` 컨텍스트 상한 6000→**9000**자(정답 청크가 ~7위일 때 합성 전 잘림 방지).
- 효과: 약어·법령명(STR·CTR·VASP·CISO)은 BM25 정확매칭이 강함. 검증 — "STR/CTR 보고처" 답변이
  **금융정보분석원(FIU)** 으로 교정됨(이전엔 금감원·금보원으로 환각). 회귀(가명정보·VASP) 정상.
- 한계였던 한글 BM25 → **cjk 분석기로 개선**(아래).

### 한글 분석기: nori 불가 → 내장 cjk 채택
- **nori 불가**: `_cat/plugins` 확인 결과 watsonx.data opensearch530 번들에 `analysis-nori` 없음. operator 관리
  클러스터라 수동 플러그인 설치는 되돌려지고/비영속/egress 불확실 → 깨끗이 설치 불가.
- **대안 = 내장 `cjk` 분석기**(Lucene 코어, 플러그인 불필요): `text` 매핑을 `{"type":"text","analyzer":"cjk"}`로.
  cjk는 한글을 bigram으로("전자금융거래법의"→전자/자금/금융/…) → 조사 붙은 통토큰만 만들던 `standard` 대비 한글 BM25
  recall 급상승. 적용: 인덱스 DELETE → `os_ensure_index`가 새 매핑으로 재생성 → 재추출 잡으로 전체 재색인.
- 검증: "전자금융거래법 감독기관"→전자금융거래법#0, "의심거래보고 보고기관"→특정금융정보법#0(FIU) 각 1위;
  답변 정확(STR/CTR→FIU, 유출 통지→72h/개인정보보호위·KISA). 진짜 형태소 nori는 커스텀 OpenSearch 이미지 필요(보류).

---

## 9. 한 줄 요약 교훈

1. deployer의 `FAILED - RETRYING` 은 대부분 정상 폴링. 진짜 로그는 olm-utils workspace 안에.
2. CPD 설치 실패는 상당수가 **타이밍 레이스**(cert-manager, CRD 등록) → 멱등 재시도/재시작으로 해소.
3. macOS cpd-cli = **docker shim + `script` TTY** 필요.
4. 5.3.x는 `install-components`(helm 기반). **CRD(`*-cluster-scoped`) 차트를 먼저 깔아야** 한다.
5. 내부 레지스트리 Removed → 앱 배포는 **ConfigMap + ubi9 런타임 pip-install**. 쓰기는 `/tmp` 아래(restricted SCC).
