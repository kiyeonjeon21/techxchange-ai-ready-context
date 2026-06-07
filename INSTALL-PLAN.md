# watsonx.data 2.3.x on IBM Software Hub 5.3.1 — Install Plan

> Cluster: `<CLUSTER_NAME>` (IBM TechZone, tok04) · OCP 4.18 · CPD/SWH 5.3.1
> Tooling: `oc` 4.18.14, `cpd-cli` 14.3.1 (`./bin/cpd-cli`, SWH release 5.3.1)

## 0. Current state (as of setup)
- The IBM **Cloud Pak Deployer** (`cloud-pak-deployer` ns) is **actively installing the CPD base**.
  - Config (`cm/cpd-configuration`): project `cpd`, operators project `cpd-operators`,
    cartridges = `cp-foundation` (Software Hub control plane) + `lite` only.
  - Also installing: OpenShift AI (`fast`), GPU operator.
  - Stage observed: `apply-cluster-components` (cert-manager / license service). CRDs
    `zenservice`/`ibmcpd` not present yet → **foundation not ready**.
- **watsonx.data is NOT in the deployer config** → we add it ourselves after foundation is up.

### Namespaces (from deployer config)
| Purpose | Namespace |
|---|---|
| CPD instance (control plane) | `cpd` |
| CPD operators | `cpd-operators` |

### Storage classes (from deployer config)
| Type | Class |
|---|---|
| File (RWX) | `ocs-external-storagecluster-cephfs` |
| Block (RWO) | `ocs-external-storagecluster-ceph-rbd` |

## 1. Gate: wait for foundation
Proceed only when:
```sh
oc get ibmcpd -n cpd          # Completed
oc get zenservice -n cpd      # Completed / Ready
./bin/cpd-cli manage get-cr-status --cpd_instance_ns=cpd
```

## 2. cpd-cli component identifiers (authoritative — from bundled `config/health/global.yml` + `info/nfr_metadata.yml`)
| Component (`--components`) | Kind | Operator pkg | Notes |
|---|---|---|---|
| `watsonx_data` | `WxdAddon` (`wxdaddons.lakehouse.ibm.com`) | `ibm-lakehouse-operator` | base edition |
| `watsonx_data_premium` | — | premium | adds Milvus/extra engines |
| `ibm_wxd_opensearch` (addon) / dep `opencontent_opensearch` | — | — | **OpenSearch** — pulled in by watsonx.data |
| `watsonx_dataintegration` | `WatsonxDataIntegration` | `ibm-cpd-watsonx-dataintegration-operator` | optional (StreamSets) |

`watsonx_data` **component_dependencies**: `analyticsengine`, `ccs`, `opencontent_opensearch`.

## 3. Install method (cpd-cli 5.3.x)
> NOTE: `apply-olm`/`apply-cr` are DEPRECATED in 5.3.0 (apply-olm just prints help, exit 0, does nothing).
> 5.3.x uses a single `install-components` that does catalog source + subscription + CR.
> macOS runtime: podman broken → repo `bin/cpd-cli` wrapper forces docker via shims; prefix calls
> with `script -q /dev/null` for the pty. login flag is `--insecure-skip-tls-verify=true`.

```sh
script -q /dev/null ./bin/cpd-cli manage install-components \
  --license_acceptance=true --release=5.3.1 --components=watsonx_data \
  --operator_ns=cpd-operators --instance_ns=cpd \
  --block_storage_class=ocs-external-storagecluster-ceph-rbd \
  --file_storage_class=ocs-external-storagecluster-cephfs
script -q /dev/null ./bin/cpd-cli manage get-cr-status --cpd_instance_ns=cpd
```

### (legacy) Install method (cpd-cli apply-olm/apply-cr — pre-5.3, kept for reference)
```sh
export OC_URL=https://api.<CLUSTER_DOMAIN>:6443
export CPD_INSTANCE_NS=cpd
export CPD_OPERATOR_NS=cpd-operators
export STG_BLOCK=ocs-external-storagecluster-ceph-rbd
export STG_FILE=ocs-external-storagecluster-cephfs
export VERSION=5.3.1

./bin/cpd-cli manage login-to-ocp --server=$OC_URL -u kubeadmin -p '***'

# 3a. operators (catalog source + CSV + subscription)
./bin/cpd-cli manage apply-olm \
  --release=$VERSION --cpd_operator_ns=$CPD_OPERATOR_NS \
  --components=watsonx_data

# 3b. custom resource (provision the service)
./bin/cpd-cli manage apply-cr \
  --release=$VERSION --components=watsonx_data \
  --cpd_instance_ns=$CPD_INSTANCE_NS \
  --block_storage_class=$STG_BLOCK --file_storage_class=$STG_FILE \
  --license_acceptance=true

# 3c. watch
./bin/cpd-cli manage get-cr-status --cpd_instance_ns=$CPD_INSTANCE_NS --components=watsonx_data
```

## 4. Integrations (post-install — watsonx.data runtime, NOT separate CPD services)
- **OpenSearch** — comes from `opencontent_opensearch`/`ibm_wxd_opensearch`; verify the OpenSearch
  add-on/engine is provisioned in the watsonx.data console; used for semantic/data search.
- **OpenTelemetry** — configured *inside* watsonx.data: enable OTel export from the Presto(Trino)
  engine to an OTel collector (engine config, not a cluster service install).
- **dbt** — client-side: `dbt-trino` adapter pointed at the watsonx.data Presto endpoint. No cluster
  install; needs connection details (host, port 443, catalog/schema, API key/JWT).

## Open decisions (confirm before step 3)
1. **Edition**: base `watsonx_data` (sufficient for OpenSearch/OTel/dbt) vs `watsonx_data_premium`.
2. **Install route**: cpd-cli (this plan) vs adding `watsonx.data` cartridge to the deployer config.
3. Install `watsonx_dataintegration` (StreamSets) too, or just base watsonx.data?
