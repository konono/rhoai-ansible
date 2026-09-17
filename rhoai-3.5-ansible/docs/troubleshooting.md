# トラブルシューティングガイド

RHOAI 3.5 on SNO Ansible Installer のトラブルシューティング完全ガイド。
過去のデプロイ実績から得たトラブル事例、環境制約、診断コマンドを網羅的に記載しています。

> **対象読者**: 顧客環境（特に Disconnected / Private Subnet）でデプロイを行うエンジニア

---

## 目次

1. [デプロイフロー詳細（ロール別）](#1-デプロイフロー詳細ロール別)
2. [過去のトラブル事例](#2-過去のトラブル事例)
3. [環境制約・前提条件](#3-環境制約前提条件)
4. [デプロイ前チェックリスト](#4-デプロイ前チェックリスト)
5. [デプロイ中の監視方法](#5-デプロイ中の監視方法)
6. [デプロイ失敗時の対処フロー](#6-デプロイ失敗時の対処フロー)
7. [各リソースの正常状態確認コマンド集](#7-各リソースの正常状態確認コマンド集)
8. [Disconnected 環境固有の注意点](#8-disconnected-環境固有の注意点)

---

## 1. デプロイフロー詳細（ロール別）

site.yml は以下の順序でロールを実行します。各ロールで作成されるリソース、待機条件、よくある失敗パターンを記載します。

### 1.1 Preflight

**処理内容**:
- `oc login` の確認
- クラスタードメイン取得
- DB パスワード生成・永続化（`.generated-passwords.yml`）
- `models` の dict キーの RFC 1123 バリデーション
- `keycloak_groups` キーのバリデーション
- `storage_class: lvms-*` 時の `install_lvm` 自動有効化
- 依存関係の自動解決（`resolve_dependencies.yml`）
- 必須変数のバリデーション

**よくある失敗**:
- `oc login` のトークン期限切れ → 再ログイン
- `hf_token` 未設定 → `vault.yml` に HuggingFace トークンを設定
- `lvm_device_paths` 未設定 → `components.yml` にデバイスパスを追加

**確認コマンド**:
```bash
oc whoami
oc whoami --show-server
oc get ingresses.config.openshift.io cluster -o jsonpath='{.spec.domain}'
```

---

### 1.2 LVM Operator (`lvm` ロール)

**作成されるリソース**:
| リソース | Namespace | 名前 |
|---|---|---|
| Namespace | - | `openshift-storage` |
| Subscription | `openshift-lvm-storage` | `lvms-operator` |
| LVMCluster | `openshift-lvm-storage` | `lvmcluster` |
| StorageClass | - | `lvms-vg1` (default) |

**待機条件**:
- CSV の Succeeded
- LVMCluster の `status.state == Ready`

**よくある失敗**:
1. **デバイスが見つからない**: `lvm_device_paths` のパスがノード上に存在しない
2. **VolumeGroupsReady: False**: LVM thin pool の作成に失敗
3. **デバイス番号の変更**: EC2 再起動で NVMe デバイス番号が変わる（後述）

**確認コマンド**:
```bash
# LVMCluster の状態
oc get lvmcluster -n openshift-lvm-storage -o yaml | grep -A5 'status:'

# VolumeGroup の状態
oc get lvmcluster lvmcluster -n openshift-lvm-storage \
  -o jsonpath='{.status.conditions[?(@.type=="VolumeGroupsReady")].status}'

# topolvm-node Pod
oc get pods -n openshift-lvm-storage

# vg-manager のログ（デバイス検出エラー）
oc logs -n openshift-lvm-storage -l app.kubernetes.io/name=vg-manager --tail=20

# ノード上のデバイス確認
oc debug node/<node-name> -- chroot /host lsblk -d -o NAME,SIZE,TYPE,MODEL

# デバイス名と PCI パスの対応
oc debug node/<node-name> -- chroot /host bash -c \
  'for d in $(lsblk -dn -o NAME | grep nvme); do \
    BP=$(find /dev/disk/by-path -lname "*/$d" -printf "%f" 2>/dev/null); \
    printf "%-12s %-8s %-40s %s\n" "/dev/$d" \
      "$(lsblk -dn -o SIZE /dev/$d)" \
      "$(lsblk -dn -o MODEL /dev/$d)" \
      "${BP:-(none)}"; \
  done'

# StorageClass の確認
oc get sc
```

---

### 1.3 MetalLB (`metallb` ロール)

**作成されるリソース**:
| リソース | Namespace | 名前 |
|---|---|---|
| Subscription | `metallb-system` | `metallb-operator` |
| MetalLB | `metallb-system` | `metallb` |
| IPAddressPool | `metallb-system` | `sno-pool` |
| L2Advertisement | `metallb-system` | `sno-l2` |

**待機条件**:
- MetalLB speaker Pod が Running

**よくある失敗**:
1. **OperatorGroup モード不正**: MetalLB は `AllNamespaces` が必要
2. **IP 範囲の不整合**: ノード IP と異なるサブネットの IP を指定

**確認コマンド**:
```bash
oc get pods -n metallb-system -l component=speaker
oc get ipaddresspool -n metallb-system -o yaml
oc get l2advertisement -n metallb-system
```

---

### 1.4 Dependency Operators

以下の Operator を `install_operator.yml` 共通タスクでインストールします。

| Operator | Namespace | チャネル |
|---|---|---|
| cert-manager | `openshift-cert-manager-operator` | `stable-v1` |
| ServiceMesh 3 | `openshift-servicemesh` | `stable` |
| NFD | `openshift-nfd` | `stable` |
| GPU Operator | `nvidia-gpu-operator` | `stable` |
| RHCL (Authorino) | `openshift-rhcl` | `stable` |
| Kueue | `openshift-kueue` | `stable-v1.4` |
| JobSet | `openshift-jobset` | `stable-v1.0` |
| LeaderWorkerSet | `openshift-leaderworkerset` | `stable-v1.0` |

**待機条件（共通）**:
- Subscription の `installedCSV` が設定される
- CSV の `phase == Succeeded`

**よくある失敗**:
1. **OLM 依存解決失敗**: 1つの Subscription のチャネルが不正だと、全 Subscription の解決が停止する
2. **GPU ドライバービルド失敗**: GPU Operator のバージョンとカーネルの非互換
3. **チャネル名の変更**: OCP バージョンアップで Operator チャネル名が変わる

**確認コマンド**:
```bash
# 全 Subscription の状態
oc get subscription -A -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,CSV:.status.installedCSV,STATE:.status.state

# OLM 依存解決エラーの確認
oc get subscription -A -o json | jq '.items[] | select(.status.conditions[]?.type=="ResolutionFailed") | {name: .metadata.name, message: .status.conditions[].message}'

# 利用可能なチャネルの確認
oc get packagemanifest <operator-name> -o jsonpath='{.status.channels[*].name}'

# GPU Operator 固有
oc get pods -n nvidia-gpu-operator
oc get clusterpolicy gpu-cluster-policy -o jsonpath='{.status.state}'
oc get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}'

# GPU ドライバービルドログ
oc logs -n nvidia-gpu-operator -l app=nvidia-driver-daemonset -c openshift-driver-toolkit-ctr --tail=30
```

---

### 1.5 ODF Object Storage (`odf` ロール)

**作成されるリソース**:
| リソース | Namespace | 名前 |
|---|---|---|
| Subscription | `openshift-storage` | `odf-operator` |
| NooBaa | `openshift-storage` | `noobaa` |

**待機条件**:
- NooBaa の `status.phase == Ready`（最大15分）

**よくある失敗**:
1. **NooBaa DB の PVC マウント失敗**: LVM が EBS ボリュームを掴む（後述）
2. **NooBaa Ready に時間がかかる**: DB, Core, Endpoint の起動順序

**確認コマンド**:
```bash
oc get noobaa -n openshift-storage -o jsonpath='{.items[0].status.phase}'
oc get pods -n openshift-storage
oc get pvc -n openshift-storage

# NooBaa の詳細状態
oc get noobaa noobaa -n openshift-storage -o yaml | grep -A20 'status:'
```

---

### 1.6 OpenShift AI (`rhoai` ロール)

**作成されるリソース**:
| リソース | Namespace | 名前 |
|---|---|---|
| Subscription | `redhat-ods-operator` | `rhods-operator` |
| PVC | `redhat-ods-applications` | `maas-postgres-data` |
| Deployment | `redhat-ods-applications` | `maas-postgres` |
| Secret | `redhat-ods-applications` | `maas-db-config` |
| DataScienceCluster | - | `default-dsc` |
| HardwareProfile | `redhat-ods-applications` | `nvidia-gpu-1` |
| Gateway | `openshift-ingress` | `maas-default-gateway` |
| ConfigMap | `openshift-ingress` | `maas-gateway-options` |
| NetworkPolicy | `redhat-ai-gateway-infra` | `maas-authorino-allow-rhcl` |
| Namespace | - | `llm-serving` |

**待機条件**:
- `redhat-ods-applications` Namespace の作成
- `rhods-dashboard` Deployment の Ready
- MaaS Gateway の `Programmed == True`
- AITenant の作成

**よくある失敗**:
1. **Gateway Programmed: False**: MetalLB 未導入で LoadBalancer IP が割り当てられない
2. **loadBalancerIP のハードコード**: Istio コントローラーが ClusterIP をハードコードすることがある → `Remove loadBalancerIP` タスクで自動除去
3. **AITenant が作成されない**: `aigateway.modelsAsAService` が Managed でない場合
4. **maas-api CrashLoopBackOff**: `maas-db-config` Secret に `DB_CONNECTION_URL` キーがない場合

**確認コマンド**:
```bash
# DSC の状態
oc get datasciencecluster default-dsc -o jsonpath='{.status.phase}'

# Gateway の状態
oc get gateway maas-default-gateway -n openshift-ingress \
  -o jsonpath='{.status.conditions[?(@.type=="Programmed")].status}'

# Gateway Service の IP 確認
oc get svc -n openshift-ingress -l gateway.networking.k8s.io/gateway-name=maas-default-gateway

# AITenant の確認
oc get aitenants.maas.opendatahub.io -A

# maas-api の状態
oc get pods -n redhat-ai-gateway-infra
oc logs -n redhat-ai-gateway-infra -l app.kubernetes.io/name=maas-api --tail=20

# Dashboard の状態
oc get deployment rhods-dashboard -n redhat-ods-applications -o jsonpath='{.status.readyReplicas}'

# Route の確認
oc get route -n openshift-ingress
```

---

### 1.7 Keycloak (`keycloak` ロール)

**作成されるリソース**:
| リソース | Namespace | 名前 |
|---|---|---|
| Namespace | - | `keycloak` |
| Subscription | `keycloak` | `rhbk-operator` |
| StatefulSet | `keycloak` | `postgres` |
| Keycloak CR | `keycloak` | `keycloak` |
| Route (reencrypt) | `keycloak` | `keycloak` |
| Secret (realm) | `keycloak` | `keycloak-maas-realm` |
| Subscription | `group-sync-operator` | `group-sync-operator` |
| GroupSync CR | `group-sync-operator` | `keycloak-group-sync` |

**待機条件**:
- PostgreSQL StatefulSet の Ready
- Keycloak CR の `Ready == True`
- Realm import の完了

**よくある失敗**:
1. **PostgreSQL PVC マウント失敗**: LVMCluster が Failed の場合（ストレージ問題）
2. **Keycloak CrashLoopBackOff**: PostgreSQL が起動していない
3. **Realm import 失敗**: Keycloak が Ready になる前に import が開始

**確認コマンド**:
```bash
# Keycloak Pod の状態
oc get pods -n keycloak

# Keycloak Ready 確認
oc get keycloak keycloak -n keycloak \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}'

# PostgreSQL のログ
oc logs -n keycloak postgres-0 --tail=20

# Keycloak のログ
oc logs -n keycloak keycloak-0 --tail=30

# Route の確認
oc get route -n keycloak

# Realm の確認（Keycloak 起動後）
curl -sk https://<keycloak-host>/realms/maas/.well-known/openid-configuration | jq .issuer
```

---

### 1.8 Integration ロール

#### integration_keycloak_oauth
OpenShift OAuth に Keycloak OIDC IdP を登録。

**確認コマンド**:
```bash
oc get oauth cluster -o jsonpath='{.spec.identityProviders[*].name}'
oc get pods -n openshift-authentication
```

#### integration_keycloak_maas
Keycloak グループ定義から MaaS AuthPolicy / Subscription を生成・適用。

**確認コマンド**:
```bash
oc get maasauthpolicy -n redhat-ods-applications
oc get maassubscription -n redhat-ods-applications
```

#### integration_rhoai_oidc
AITenant に Keycloak OIDC 設定をパッチ。

**確認コマンド**:
```bash
oc get aitenant -A -o jsonpath='{.items[0].spec.oidc}'
```

---

### 1.9 LLM Serving (`llm_serving` ロール)

**作成されるリソース**:
| リソース | Namespace | 名前 |
|---|---|---|
| Namespace | - | `llm-serving` |
| Secret | `llm-serving` | `hf-token` |
| PVC | `llm-serving` | `hf-model-cache` |
| ClusterStorageContainer | - | `default` |
| ConfigMap | `llm-serving` | `<chat_template_configmap>` |
| ServiceAccount | `llm-serving` | `llm-serving-sa` |
| Job | `llm-serving` | `download-<model_name>` (per model) |
| LLMInferenceService | `llm-serving` | `<model_name>` (per model) |
| MaaSModelRef | `llm-serving` | `<model_name>` (per model) |

**待機条件**:
- モデルダウンロード Job の完了（最大1時間）
- LLMInferenceService の `Ready == True`（最大20分）

**よくある失敗**:
1. **CUDA OOM**: モデルサイズが GPU VRAM を超過（後述）
2. **ダウンロード失敗**: HF トークンが無効、ネットワーク不通
3. **PVC マウント失敗**: ストレージの問題
4. **vLLM 起動タイムアウト**: モデルロードに時間がかかる（startupProbe: 120回 x 10秒 = 20分）

**確認コマンド**:
```bash
# ダウンロード Job
oc get job -n llm-serving
oc logs -n llm-serving job/download-<model-name> --tail=20

# LLMInferenceService
oc get llminferenceservice -n llm-serving
oc get llminferenceservice <model-name> -n llm-serving \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}'

# vLLM Pod
oc get pods -n llm-serving
oc logs -n llm-serving -l serving.kserve.io/llminferenceservice=<model-name> --tail=30

# PVC のサイズと使用量
oc get pvc -n llm-serving

# GPU の割り当て
oc get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}GPU allocatable: {.status.allocatable.nvidia\.com/gpu}{"\n"}{end}'
```

---

### 1.10 MaaS Resources (`maas_resources` ロール)

**作成されるリソース**:
| リソース | Namespace | 名前 |
|---|---|---|
| ClusterRoleBinding | - | `maas-admins-dashboard` |

> MaaSModelRef は `llm_serving` ロールの `deploy_model.yml` / `remove_model.yml` で管理されます。

**よくある失敗**:
1. **MaaSModelRef が Pending**: LLMInferenceService と異なる namespace に作成した場合

**確認コマンド**:
```bash
oc get maasmodelref -n llm-serving
oc get maasmodelref <model-name> -n llm-serving -o jsonpath='{.status.phase}'
```

> **重要**: MaaSModelRef は LLMInferenceService と**同じ namespace** (`llm-serving`) に作成する必要があります。異なる namespace に作成すると `Waiting for HTTPRoute to be created` で Pending になります。

---

### 1.11 MLflow (`mlflow` ロール)

**作成されるリソース**:
| リソース | Namespace | 名前 |
|---|---|---|
| Namespace | - | `mlflow-workspace` |
| ObjectBucketClaim | `mlflow-workspace` | `mlflow-obc` |
| ServiceAccount | `mlflow-workspace` | `mlflow-api` |
| ConfigMap | `mlflow-workspace` | `noobaa-ca-bundle` |
| Secret | `redhat-ods-applications` | `mlflow-s3-config` |
| MLflow | `redhat-ods-applications` | `mlflow` |

**待機条件**:
- OBC の `phase == Bound`

**よくある失敗**:
1. **OBC の ConfigMap 未生成**: OBC は Bound だが ConfigMap `mlflow-obc` が遅れて生成される → リトライで解決
2. **MLflow migration の SSL エラー**: PostgreSQL が SSL 未対応の場合（現在は SQLite を使用）

**確認コマンド**:
```bash
oc get obc -n mlflow-workspace
oc get cm mlflow-obc -n mlflow-workspace
oc get mlflow mlflow -n redhat-ods-applications -o jsonpath='{.status.conditions[?(@.type=="Available")].status}'
oc get pods -n redhat-ods-applications -l app.kubernetes.io/name=mlflow
```

---

### 1.12 OGX (`ogx` ロール)

**作成されるリソース**:
| リソース | Namespace | 名前 |
|---|---|---|
| Secret | `redhat-ods-applications` | `ogx-vllm-connection` |
| Secret | `redhat-ods-applications` | `ogx-postgres-credentials` |
| PVC | `redhat-ods-applications` | `ogx-postgres-data` |
| StatefulSet | `redhat-ods-applications` | `ogx-postgres` |
| Service | `redhat-ods-applications` | `ogx-postgres` |
| BackingStore | `openshift-storage` | `ogx-pv-backing-store` |
| BucketClass | `openshift-storage` | `ogx-bucket-class` |
| OBC | `redhat-ods-applications` | `ogx-obc` |
| OGXServer | `redhat-ods-applications` | `ogx-server` |

**確認コマンド**:
```bash
oc get ogxserver ogx-server -n redhat-ods-applications -o jsonpath='{.status.phase}'
oc get pods -n redhat-ods-applications -l app.kubernetes.io/name=ogx-server
oc get pods -n redhat-ods-applications -l app=ogx-postgres
```

---

### 1.13 NeMo Guardrails (`guardrails` ロール)

**作成されるリソース**:
| リソース | Namespace | 名前 |
|---|---|---|
| ConfigMap | `redhat-ods-applications` | `nemo-guardrails-config` |
| NemoGuardrails | `redhat-ods-applications` | `nemo-guardrails` |

**確認コマンド**:
```bash
oc get nemoguardrails nemo-guardrails -n redhat-ods-applications -o jsonpath='{.status.phase}'
```

---

### 1.14 Observability (`observability` ロール)

**作成されるリソース**:
| リソース | Namespace | 名前 |
|---|---|---|
| Subscription | `openshift-cluster-observability-operator` | `cluster-observability-operator` |
| Subscription | `openshift-opentelemetry-operator` | `opentelemetry-product` |
| ConfigMap | `openshift-user-workload-monitoring` | `user-workload-monitoring-config` |
| PrometheusRule | `nvidia-gpu-operator` | `nvidia-dcgm-exporter` |
| ClusterPolicy | - | `gpu-cluster-policy` |
| PodMonitor, ScrapeConfig, PrometheusRule 等 | 各 namespace | 各種 |

**確認コマンド**:
```bash
oc get csv -n openshift-cluster-observability-operator
oc get pods -n openshift-cluster-observability-operator
```

---

## 2. 過去のトラブル事例

過去のデプロイで実際に発生した全トラブルを記載します。

### 2.1 LVMCluster の VolumeGroup 作成失敗（デバイス消失）

**症状**:
- LVMCluster の status が `Failed`
- `VolumeGroupsReady: False`
- 複数の Pod が `ContainerCreating` のまま（PVC マウント待ち）

**原因**:
EC2 インスタンス再起動後に NVMe デバイス番号が変わった。`/dev/nvme14n1` → `/dev/nvme1n1` のようにカーネルの検出順で番号が再割り当てされる。LVMCluster が旧番号を参照しているため `no such file or directory` でデバイスが見つからない。

**確認コマンド**:
```bash
# LVMCluster の状態
oc get lvmcluster lvmcluster -n openshift-lvm-storage -o yaml | grep -A30 'status:'

# エラーメッセージの確認
oc get lvmcluster lvmcluster -n openshift-lvm-storage \
  -o jsonpath='{.status.deviceClassStatuses[0].nodeStatus[0].reason}'

# 現在のデバイス一覧
oc debug node/<node-name> -- chroot /host lsblk -d -o NAME,SIZE,TYPE,MODEL
```

**解決策**:
1. `components.yml` の `lvm_device_paths` を `/dev/disk/by-path/pci-*` 形式に変更
2. LVMCluster をパッチ:
   ```bash
   oc patch lvmcluster lvmcluster -n openshift-lvm-storage --type=merge \
     -p '{"spec":{"storage":{"deviceClasses":[{"name":"vg1","default":true,"deviceSelector":{"paths":["/dev/disk/by-path/pci-0000:34:00.0-nvme-1","/dev/disk/by-path/pci-0000:33:00.0-nvme-1"]},"thinPoolConfig":{"name":"thin-pool-1","sizePercent":90,"overprovisionRatio":10}}]}}}'
   ```
3. 既存の stuck Pod を削除して再スケジュール

**再発防止**: `lvm_device_paths` には常に `/dev/disk/by-path/pci-*` 形式を使用する。

---

### 2.2 LVM が EBS ボリュームを掴む

**症状**:
- Pod が `ContainerCreating` のまま
- Events: `mount failed: wrong fs type, bad superblock on /dev/nvmeXn1`
- `lsblk` で EBS ボリュームの FSTYPE が `LVM2_member`

**原因**:
LVMCluster に `deviceSelector` が未設定の場合、`deviceDiscoveryPolicy=Static` が初回 VG 作成時に全ての空ディスクを自動取得する。EBS CSI が新ボリュームをアタッチすると LVM が先に掴む。

**確認コマンド**:
```bash
oc debug node/<node-name> -- chroot /host pvs
oc debug node/<node-name> -- chroot /host lsblk
```

**解決策**:
```bash
# PVC 削除→再作成で新しいボリュームを割り当て
oc delete pvc <pvc-name> -n <namespace>
# Operator が自動的に新しい PVC を作成

# 既存 VG からデバイスを解放（root で実行）
oc debug node/<node-name> -- chroot /host vgreduce vg1 /dev/nvmeXn1
oc debug node/<node-name> -- chroot /host pvremove /dev/nvmeXn1
```

**再発防止**: LVMCluster 作成時に `deviceSelector.paths` を明示的に設定して対象デバイスを限定する。

---

### 2.3 Route の HostAlreadyClaimed

**症状**:
- `oc get route --all-namespaces` で特定 Route の HOST/PORT 列が `HostAlreadyClaimed`
- 該当 Route が Admitted されず無効

**原因**:
同じホスト名を持つ Route が2つ存在し、先に作成された Route がホスト名を占有。Gateway コントローラが自動生成する Route と手動作成の Route が重複するケースが典型的。

**確認コマンド**:
```bash
# HostAlreadyClaimed の Route を検索
oc get route --all-namespaces -o jsonpath='{range .items[*]}{range .status.ingress[*]}{range .conditions[*]}{.reason}{"\t"}{.host}{"\t"}{.message}{"\n"}{end}{end}{end}' | grep HostAlreadyClaimed

# 特定ホスト名の Route を全て表示
oc get route --all-namespaces | grep '<hostname>'
```

**解決策**: 重複する Route を削除。

**再発防止**: Gateway リソースを使う場合、コントローラが Route を自動生成するため、同じホスト名の手動 Route を作成しない。

---

### 2.4 OLM 依存解決の全体停止

**症状**:
- 全 Operator の Subscription が `ResolutionFailed`
- 特定の Operator だけチャネルを間違えたのに、関係ない Operator も影響を受ける

**原因**:
OLM の依存解決は全 Subscription を一括で解決するため、1つでも解決不可能な Subscription があると全体が停止する。

**確認コマンド**:
```bash
# どの Subscription が問題か特定
oc get subscription -A -o json | jq -r '.items[] | select(.status.conditions[]?.type=="ResolutionFailed") | "\(.metadata.namespace)/\(.metadata.name): \(.status.conditions[] | select(.type=="ResolutionFailed") | .message)"'

# 利用可能なチャネルの確認
oc get packagemanifest <operator-name> -o jsonpath='{.status.channels[*].name}'
```

**解決策**: 問題の Subscription のチャネルを修正:
```bash
oc patch subscription <name> -n <namespace> --type=merge \
  -p '{"spec":{"channel":"<correct-channel>"}}'
```

---

### 2.5 GPU ドライバービルドの失敗

**症状**:
- `nvidia-driver-daemonset` Pod が CrashLoopBackOff
- ドライバービルドログに `too few arguments to function` エラー

**原因**:
GPU Operator のバージョンと RHCOS カーネルの非互換。特に NVIDIA ドライバー v550 (GPU Operator v24.9) が新しい DRM API 変更と非互換。

**確認コマンド**:
```bash
oc get pods -n nvidia-gpu-operator -l app=nvidia-driver-daemonset
oc logs -n nvidia-gpu-operator -l app=nvidia-driver-daemonset -c openshift-driver-toolkit-ctr --tail=50

# GPU が allocatable か確認
oc get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}GPU: {.status.allocatable.nvidia\.com/gpu}{"\n"}{end}'
```

**解決策**: GPU Operator を `stable` チャネルにアップグレード:
```bash
oc patch subscription gpu-operator-certified -n nvidia-gpu-operator \
  --type=merge -p '{"spec":{"channel":"stable"}}'
```

**再発防止**: GPU Operator はバージョン固定せず `stable` チャネルを使用する。

---

### 2.6 CUDA OOM（モデルサイズ超過）

**症状**:
- vLLM Pod が CrashLoopBackOff
- ログ: `CUDA out of memory. GPU 0 has a total capacity of 22.04 GiB`

**原因**:
モデルサイズが GPU VRAM を超過。

**GPU ごとのモデルサイズ目安**:

| GPU | VRAM | 最大モデルサイズ（FP16） | 最大モデルサイズ（FP8） |
|---|---|---|---|
| L4 | 22 GB | ~14B | ~27B |
| A10G | 24 GB | ~14B | ~27B |
| A100 | 40/80 GB | ~27B / ~70B | ~54B / ~140B |

**確認コマンド**:
```bash
# vLLM のメモリ使用量
oc logs -n llm-serving -l serving.kserve.io/llminferenceservice=<model-name> | grep 'Model loading took'

# GPU VRAM
oc debug node/<node-name> -- chroot /host nvidia-smi
```

**解決策**: より小さいモデルに変更するか、FP8/GPTQ 量子化モデルを使用する。

---

### 2.7 Private Subnet での Pod → *.apps DNS 問題

**症状**:
- Dashboard/maas-ui から MaaS API へのリクエストがタイムアウト
- `context deadline exceeded` エラー
- Pod 内から `*.apps.*` が外部 IP に解決される

**原因**:
Pod 内の DNS で `maas.apps.<domain>` が外部 IP（NLB の IP）に解決されるが、Private Subnet の Pod からその外部 IP への接続はタイムアウトする（NAT ヘアピニング不可）。

**確認コマンド**:
```bash
# Pod 内の DNS 解決確認
oc exec -n redhat-ods-applications deploy/rhods-dashboard -- getent hosts maas.apps.<domain>

# 外部 IP への到達性
oc exec -n redhat-ods-applications deploy/rhods-dashboard -- curl -sk --connect-timeout 5 https://maas.apps.<domain>/health
```

**解決策**:
rhoai ロールの Proxy 設定ブロック（Step 11）が自動的に HostAliases と Proxy 環境変数を設定します。手動で設定する場合:

```bash
# Gateway Service の ClusterIP を取得
MAAS_IP=$(oc get svc maas-default-gateway-data-science-gateway-class -n openshift-ingress -o jsonpath='{.spec.clusterIP}')

# maas-ui に HostAliases を追加
oc patch deploy maas-ui -n redhat-ods-applications --type=json \
  -p "[{\"op\":\"add\",\"path\":\"/spec/template/spec/hostAliases\",\"value\":[{\"ip\":\"${MAAS_IP}\",\"hostnames\":[\"maas.apps.<domain>\"]}]}]"
```

---

### 2.8 kube-auth-proxy の NO_PROXY 設定問題

**症状**:
- Dashboard にログインすると 403 エラー
- kube-auth-proxy ログ: `Error redeeming code during OAuth2 callback: context canceled`

**原因**:
kube-auth-proxy は OAuth サーバー (`oauth-openshift.apps...`) に token redemption リクエストを送信する。NO_PROXY に `.apps.*` を含めると OAuth サーバーもバイパスされ、外部 IP に直接接続しようとしてタイムアウトする。

**重要なルール**:
- `kube-auth-proxy`: NO_PROXY に `.apps.*` を**含めてはいけない**（OAuth サーバーは Proxy 経由でアクセス）
- `core-bff`, `maas-ui`: NO_PROXY に `.apps.*` を**含めてよい**（HostAliases で内部 IP に解決するため）

**確認コマンド**:
```bash
oc exec -n openshift-ingress deploy/kube-auth-proxy -- env | grep -i proxy
oc exec -n openshift-ingress deploy/kube-auth-proxy -- curl -sk --connect-timeout 5 https://oauth-openshift.apps.<domain>/
```

---

### 2.9 DSC の aigateway 未有効化

**症状**:
- AITenant が Failed または存在しない
- maas-controller Pod が存在しない

**原因**:
RHOAI 3.5 で `kserve.modelsAsService` が deprecated になり `aigateway.modelsAsAService` に移行。DSC で明示的に `aigateway.modelsAsAService: Managed` を設定しないと maas-controller がデプロイされない。

**確認コマンド**:
```bash
oc get datasciencecluster default-dsc -o jsonpath='{.spec.components.aigateway}'
oc get pods -n redhat-ods-applications -l app.kubernetes.io/name=maas-controller
oc get aitenants.maas.opendatahub.io -A
```

---

### 2.10 MaaS Gateway の LoadBalancer 作成不可

**症状**:
- MaaS Gateway が `Programmed: False`
- Gateway Service が `<pending>` のまま IP が割り当てられない

**原因**:
Private Subnet の AWS 環境で ELB が作成できない。

**確認コマンド**:
```bash
oc get gateway maas-default-gateway -n openshift-ingress -o yaml | grep -A5 'Programmed'
oc get svc -n openshift-ingress -l gateway.networking.k8s.io/gateway-name=maas-default-gateway
oc describe svc <gateway-svc-name> -n openshift-ingress | grep -A5 Events
```

**解決策**: MetalLB を事前にデプロイして L2 モードで IP を提供する。

---

### 2.11 OBC の ConfigMap 遅延生成

**症状**:
- MLflow ロールで `configmaps "mlflow-obc" not found` エラー
- OBC は `Bound` だが ConfigMap がまだ作成されていない

**原因**:
NooBaa が OBC を処理してから ConfigMap / Secret を生成するまでにタイムラグがある（通常30-60秒）。

**確認コマンド**:
```bash
oc get obc -n mlflow-workspace
oc get cm mlflow-obc -n mlflow-workspace
oc get secret mlflow-obc -n mlflow-workspace
```

**解決策**: site.yml を再実行するか、`--tags workload` で workload フェーズのみ再実行。

---

## 3. 環境制約・前提条件

### 3.1 EC2 Instance Storage（エフェメラル NVMe）

- EC2 の Instance Storage（NVMe SSD）は**インスタンス停止で消失**する
- 再起動のみの場合はデータは保持されるが、**デバイス番号が変わる可能性**がある
- `lvm_device_paths` には `/dev/disk/by-path/pci-*` 形式を使用すること
- Instance Storage 上のデータ（LVM VG）はインスタンス停止で**完全に消失**する

### 3.2 LVM デバイスパスの不安定性

- `/dev/nvmeXnY` はカーネルの検出順で決まるため、再起動で変わる
- `/dev/disk/by-path/pci-*` は PCI スロットに紐づくため安定
- `/dev/disk/by-id/nvme-*` はデバイスのシリアル番号に紐づくため安定
- LVMCluster の `deviceSelector.paths` は symlink 解決をサポートしている

### 3.3 RHPDS Sandbox の制約

- **有効期限**: デフォルト2週間（延長不可の場合あり）
- **Public IP**: NLB 経由で付与されるが、Private Subnet の Pod からは直接到達不可
- **Proxy**: install-config で設定された Proxy が各コンポーネントに影響
- **GPU**: g6.8xlarge (L4 x1, 22GB VRAM) が一般的
- **ストレージ**: EBS (gp3-csi) + Instance Storage (NVMe)

### 3.4 Proxy 環境の注意点

- クラスター Proxy 設定の確認: `oc get proxy cluster -o jsonpath='{.spec}'`
- **kube-auth-proxy**: NO_PROXY に `.apps.*` を含めない（OAuth 通信に必要）
- **core-bff, maas-ui**: NO_PROXY に `.apps.*` を含めてよい（HostAliases 使用時）
- **rhods-dashboard**: HostAliases で Gateway Service ClusterIP を設定
- rhoai ロールの Step 11 (Proxy configuration) が自動的に検出・設定する

### 3.5 GPU Operator の前提条件

- NFD (Node Feature Discovery) が先にインストールされている必要がある
- GPU Operator は `stable` チャネルを使用（バージョン固定はカーネル非互換のリスク）
- ClusterPolicy の `defaultRuntime` は `crio`（`crun` ではない）
- ドライバービルドには数分かかる（初回）

### 3.6 SNO 固有の制約

- 全 Pod が1ノードで稼働するため、リソース制約に注意
- replicas は基本的に 1
- etcd のパフォーマンスがボトルネックになりうる
- Node drain / upgrade 中は全サービスが停止する

---

## 4. デプロイ前チェックリスト

### 4.1 クラスター接続

```bash
# 1. oc login 確認
oc whoami
# 期待: cluster-admin

# 2. クラスターバージョン
oc get clusterversion
# 期待: 4.22.x

# 3. ノード数とステータス
oc get nodes
# 期待: 1 node, Ready

# 4. GPU の存在
oc get nodes -o jsonpath='{range .items[*]}{.metadata.labels.nvidia\.com/gpu\.product}{"\n"}{end}'
# 期待: NVIDIA-L4 等
```

### 4.2 ストレージ

```bash
# 5. デバイスの確認（LVM 用）
oc debug node/<node-name> -- chroot /host bash -c \
  'for d in $(lsblk -dn -o NAME | grep nvme); do \
    BP=$(find /dev/disk/by-path -lname "*/$d" -printf "%f" 2>/dev/null); \
    printf "%-12s %-8s %-40s %s\n" "/dev/$d" \
      "$(lsblk -dn -o SIZE /dev/$d)" \
      "$(lsblk -dn -o MODEL /dev/$d)" \
      "${BP:-(none)}"; \
  done'

# 6. StorageClass の確認
oc get sc
```

### 4.3 ネットワーク

```bash
# 7. Proxy 設定の確認
oc get proxy cluster -o jsonpath='{.spec}'

# 8. DNS 解決の確認
oc debug node/<node-name> -- chroot /host nslookup registry.redhat.io

# 9. レジストリへの到達性（Disconnected 環境用）
oc debug node/<node-name> -- chroot /host curl -sk https://registry.redhat.io/v2/ -o /dev/null -w '%{http_code}'
```

### 4.4 Inventory 設定

```bash
# 10. components.yml の lvm_device_paths が実デバイスと一致するか
# 11. vault.yml の hf_token が有効か
# 12. keycloak_groups のキーが RFC 1123 準拠か（小文字、ハイフン、ドットのみ）
# 13. models のモデルが GPU VRAM に収まるか（present モデルの合計 gpu_count ≤ クラスタ GPU 数）
```

---

## 5. デプロイ中の監視方法

### 5.1 Ansible の進行状況

Ansible の出力をリアルタイムで監視します。各ロールの開始・完了が表示されます。

### 5.2 フェーズ別の監視コマンド

**Infrastructure フェーズ**（LVM, MetalLB）:
```bash
watch -n5 'oc get lvmcluster -n openshift-lvm-storage -o jsonpath="{.items[0].status.state}" 2>/dev/null; echo; oc get pods -n metallb-system --no-headers 2>/dev/null'
```

**Operator インストールフェーズ**:
```bash
watch -n10 'oc get csv -A --no-headers 2>/dev/null | grep -v Succeeded'
```

**Platform フェーズ**（RHOAI, Keycloak）:
```bash
watch -n10 'echo "=== DSC ==="; oc get datasciencecluster -o jsonpath="{.items[0].status.phase}" 2>/dev/null; echo; echo "=== Gateway ==="; oc get gateway -n openshift-ingress -o jsonpath="{.items[0].status.conditions[?(@.type==\"Programmed\")].status}" 2>/dev/null; echo; echo "=== Keycloak ==="; oc get keycloak -n keycloak -o jsonpath="{.items[0].status.conditions[?(@.type==\"Ready\")].status}" 2>/dev/null; echo'
```

**Workload フェーズ**（LLM, MLflow, OGX）:
```bash
watch -n10 'echo "=== LLM ==="; oc get llminferenceservice -n llm-serving 2>/dev/null; echo "=== MLflow ==="; oc get mlflow -n redhat-ods-applications 2>/dev/null; echo "=== OGX ==="; oc get ogxserver -n redhat-ods-applications 2>/dev/null'
```

### 5.3 スタック検知

デプロイが長時間止まっている場合:

```bash
# 全 namespace の異常 Pod
oc get pods -A --no-headers | grep -vE 'Running|Completed|Succeeded'

# Pending の PVC
oc get pvc -A | grep -v Bound

# 全 Subscription のエラー
oc get subscription -A -o json | jq -r '.items[] | select(.status.conditions[]?.type=="ResolutionFailed") | .metadata.name'

# Events（直近5分）
oc get events -A --sort-by='.lastTimestamp' | tail -20
```

---

## 6. デプロイ失敗時の対処フロー

### 6.1 判断フロー

```
site.yml が失敗
│
├─ Preflight で失敗
│   └─ oc login / 変数設定を修正 → 再実行
│
├─ Operator インストールで失敗
│   ├─ OLM 依存解決エラー → チャネル名を修正 → 再実行
│   └─ CSV Succeeded にならない → Operator ログ確認 → 再実行
│
├─ リソース作成で失敗
│   ├─ PVC mount 失敗 → LVM / ストレージ確認 → 修正後再実行
│   ├─ CRD 未存在 → Operator が起動完了していない → リトライで解決
│   └─ Webhook denial → リソース仕様の確認
│
├─ 待機タイムアウト
│   ├─ LLMInferenceService Ready → vLLM Pod のログ確認
│   ├─ Keycloak Ready → PostgreSQL の状態確認
│   ├─ Gateway Programmed → MetalLB / Service の状態確認
│   └─ NooBaa Ready → Pod / PVC の状態確認
│
└─ API Key / OpenCode 失敗
    └─ 手動で scripts/setup-opencode.sh setup を実行
```

### 6.2 リトライ vs クリーンアップ再デプロイ

**リトライが有効な場合**:
- CRD 未作成のタイミング問題
- OBC ConfigMap の遅延
- 一時的なネットワーク問題
- Operator の起動完了待ち

→ `ansible-playbook site.yml -i inventory/myenv` を再実行

**クリーンアップが必要な場合**:
- LVMCluster のデバイスパス変更
- Operator チャネルの変更（OLM のキャッシュ問題）
- 大量のリソースが不整合な状態

→ `bash scripts/cleanup-all.sh --yes all && ansible-playbook site.yml -i inventory/myenv`

### 6.3 特定フェーズからの再開

```bash
# Infrastructure のみ
ansible-playbook site.yml -i inventory/myenv --tags infra

# Platform のみ
ansible-playbook site.yml -i inventory/myenv --tags platform

# Workload のみ
ansible-playbook site.yml -i inventory/myenv --tags workload

# 特定ロールのみ
ansible-playbook site.yml -i inventory/myenv --tags llm
ansible-playbook site.yml -i inventory/myenv --tags keycloak
ansible-playbook site.yml -i inventory/myenv --tags mlflow
```

---

## 7. 各リソースの正常状態確認コマンド集

### 7.1 Infrastructure

```bash
# LVMCluster
oc get lvmcluster lvmcluster -n openshift-lvm-storage -o jsonpath='{.status.state}'
# 期待: Ready

oc get lvmcluster lvmcluster -n openshift-lvm-storage \
  -o jsonpath='{.status.conditions[?(@.type=="VolumeGroupsReady")].status}'
# 期待: True

# MetalLB
oc get pods -n metallb-system -l component=speaker --no-headers | grep -c Running
# 期待: 1

# StorageClass
oc get sc lvms-vg1 -o jsonpath='{.metadata.annotations.storageclass\.kubernetes\.io/is-default-class}'
# 期待: true
```

### 7.2 Operators

```bash
# 全 Operator の状態
oc get csv -A --no-headers | awk '{print $1, $2, $NF}'
# 期待: 全て Succeeded

# GPU
oc get clusterpolicy gpu-cluster-policy -o jsonpath='{.status.state}'
# 期待: ready

oc get nodes -o jsonpath='{range .items[*]}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}'
# 期待: 1 以上
```

### 7.3 Storage

```bash
# NooBaa
oc get noobaa noobaa -n openshift-storage -o jsonpath='{.status.phase}'
# 期待: Ready
```

### 7.4 Platform

```bash
# RHOAI DSC
oc get datasciencecluster default-dsc -o jsonpath='{.status.phase}'
# 期待: Ready

# MaaS Gateway
oc get gateway maas-default-gateway -n openshift-ingress \
  -o jsonpath='{.status.conditions[?(@.type=="Programmed")].status}'
# 期待: True

# AITenant
oc get aitenants.maas.opendatahub.io -A -o jsonpath='{.items[0].status.conditions[?(@.type=="Ready")].status}'
# 期待: True

# Keycloak
oc get keycloak keycloak -n keycloak \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}'
# 期待: True

# Dashboard
oc get deployment rhods-dashboard -n redhat-ods-applications -o jsonpath='{.status.readyReplicas}'
# 期待: 1 以上
```

### 7.5 Workloads

```bash
# LLM
oc get llminferenceservice -n llm-serving -o jsonpath='{.items[0].status.conditions[?(@.type=="Ready")].status}'
# 期待: True

# MaaSModelRef
oc get maasmodelref -n llm-serving -o jsonpath='{.items[0].status.phase}'
# 期待: Ready

# MaaS API ヘルス
curl -sk https://maas.<domain>/maas-api/health
# 期待: healthy を含むレスポンス

# MLflow
oc get mlflow mlflow -n redhat-ods-applications \
  -o jsonpath='{.status.conditions[?(@.type=="Available")].status}'
# 期待: True

# OGX
oc get ogxserver ogx-server -n redhat-ods-applications -o jsonpath='{.status.phase}'
# 期待: Ready

# NeMo Guardrails
oc get nemoguardrails nemo-guardrails -n redhat-ods-applications -o jsonpath='{.status.phase}'
# 期待: Ready
```

### 7.6 Route / ネットワーク

```bash
# Route の一覧と状態
oc get route --all-namespaces

# HostAlreadyClaimed の確認
oc get route --all-namespaces -o jsonpath='{range .items[*]}{range .status.ingress[*]}{range .conditions[*]}{.reason}{"\t"}{.host}{"\n"}{end}{end}{end}' | grep HostAlreadyClaimed
# 期待: 出力なし

# 異常 Pod の一覧
oc get pods -A --no-headers | grep -vE 'Running|Completed|Succeeded'
# 期待: 出力なし（または一時的な Job のみ）
```

### 7.7 一括確認（verify Playbook）

```bash
ansible-playbook playbooks/verify.yml -i inventory/myenv
# 期待: All verification checks passed
```

---

## 8. Disconnected 環境固有の注意点

### 8.1 イメージ Pull 問題

Disconnected 環境では、以下のイメージレジストリへのアクセスが必要です:

| レジストリ | 用途 |
|---|---|
| `registry.redhat.io` | RHOAI, ODF, Keycloak 等の Red Hat Operator イメージ |
| `quay.io` | kserve-storage-initializer, NooBaa 等 |
| `docker.io` / `ghcr.io` | vLLM イメージ |
| `nvcr.io` | NVIDIA GPU ドライバー |
| `registry.access.redhat.com` | UBI ベースイメージ（ダウンロード Job） |

**Mirror Registry を使用する場合**:
```bash
# ImageContentSourcePolicy または ImageDigestMirrorSet を確認
oc get imagecontentsourcepolicy
oc get imagedigestmirrorset

# ノードの registries.conf を確認
oc debug node/<node-name> -- chroot /host cat /etc/containers/registries.conf
```

**イメージ Pull 失敗の診断**:
```bash
# Pod の Events を確認
oc describe pod <pod-name> -n <namespace> | grep -A5 Events

# 典型的なエラー
# - ImagePullBackOff: レジストリに到達できない
# - ErrImagePull: イメージが見つからない or 認証失敗

# Pull Secret の確認
oc get secret/pull-secret -n openshift-config -o jsonpath='{.data.\.dockerconfigjson}' | base64 -d | jq '.auths | keys'
```

### 8.2 証明書信頼

Disconnected 環境では自己署名証明書を使うケースが多いです。

```bash
# クラスターの CA バンドルを確認
oc get configmap user-ca-bundle -n openshift-config -o yaml

# Proxy の CA 証明書を確認
oc get proxy cluster -o jsonpath='{.spec.trustedCA.name}'

# ノードの CA 証明書
oc debug node/<node-name> -- chroot /host ls /etc/pki/ca-trust/source/anchors/
```

**Keycloak の証明書問題**:
- `service-ca-bundle` ConfigMap が CA 証明書を含む必要がある
- reencrypt Route は `destinationCACertificate` が正しい CA を参照する必要がある

```bash
# Keycloak の CA 確認
oc get cm service-ca-bundle -n keycloak -o jsonpath='{.data.service-ca\.crt}' | head -5
```

### 8.3 Proxy 設定

Disconnected 環境で Proxy を使用する場合:

```bash
# クラスター Proxy 設定
oc get proxy cluster -o yaml

# 各コンポーネントの Proxy 環境変数
oc exec -n openshift-ingress deploy/kube-auth-proxy -- env | grep -i proxy
oc exec -n redhat-ods-applications deploy/maas-ui -- env | grep -i proxy 2>/dev/null
```

**注意事項**:
- `kube-auth-proxy` の NO_PROXY に `.apps.*` を含めない
- rhoai ロールの Proxy 設定ブロックが自動的に検出・設定する
- `cluster_proxy` が `components.yml` で空の場合、クラスター Proxy 設定がフォールバックとして使用される

### 8.4 Python / Ansible のオフラインインストール

```bash
# 事前にダウンロード（オンライン環境）
bash ansible/scripts/download-deps.sh

# Disconnected 環境でインストール
cd ansible
bash scripts/setup-env.sh
source .venv/bin/activate
```

### 8.5 HuggingFace モデルのオフラインダウンロード

Disconnected 環境ではモデルダウンロード Job がインターネットに到達できません。事前にモデルを PVC にコピーする必要があります。

```bash
# オンライン環境でモデルをダウンロード
pip install huggingface_hub
huggingface-cli download <model-repo> --local-dir ./model-cache/<model-name>

# PVC にコピー（oc cp or rsync）
oc cp ./model-cache/<model-name> <pod>:/models/<model-name> -n llm-serving
```

または、PVC を事前にモデルデータで初期化した状態で、`deploy_model.yml` の PVC チェックがモデルの存在を検出してダウンロードをスキップします。
