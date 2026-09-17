# 運用ガイド

デプロイ後の運用操作をまとめます。初回セットアップは [README.md](../README.md) を参照してください。

> **注意**: すべてのコマンドは `ansible/` ディレクトリから、`-i inventory/myenv` を付けて実行してください。

---

## MaaS アクセス管理の全体像

MaaS (Models as a Service) では、以下の3つのリソースが連携してアクセス制御を実現しています。

```
ユーザー → Keycloak (認証) → MaaS Gateway (認可) → LLM モデル (推論)
               │                      │
          ユーザー/グループ      AuthPolicy + Subscription
          (誰がいるか)          (誰に何を許可するか)
```

| リソース | 役割 | namespace | 管理元 |
|---|---|---|---|
| Keycloak ユーザー/グループ | 認証：誰がログインできるか | keycloak (realm: maas) | `manage_maas_access.yml` |
| MaaSAuthPolicy | 認可：どのグループがどのモデルにアクセスできるか | models-as-a-service | `manage_maas_access.yml` |
| MaaSSubscription | クォータ：各グループのトークン使用量上限と優先度 | models-as-a-service | `manage_maas_access.yml` |
| MaaSModelRef | モデル登録：どの LLMInferenceService を MaaS に公開するか | llm-serving | `llm_serving` role |
| API Key | エンドユーザー認証：Bearer トークンとして使用 | (maas-api 内部) | `setup-opencode.sh` / Dashboard |
| Gateway AuthPolicy | Gateway レベルの認証方式定義 | openshift-ingress | **maas-controller が自動管理** |

> **重要**: Gateway AuthPolicy (`maas-gateway-auth`) は maas-controller が自動管理します。手動で作成・変更しないでください。手動の AuthPolicy と競合すると、API Key / OIDC 認証が壊れます。

### データの流れ

```
components.yml
  ├── keycloak_users    → Keycloak にユーザー作成、グループ割り当て
  ├── keycloak_groups   → Keycloak にグループ作成
  │                     → MaaSAuthPolicy を自動生成（どのグループがどのモデルにアクセス可能か）
  │                     → MaaSSubscription を自動生成（優先度とトークン上限）
  └── models            → モデルのデプロイ/削除/state 管理
                        → LLMInferenceService, MaaSModelRef の作成/削除
                        → AuthPolicy/Subscription の namespace 解決
```

### models 変数のリソース変換フロー

```
models (components.yml)
  │
  ├── state: absent のモデル（GPU 解放のため先に処理）
  │   ├── LLMInferenceService 削除
  │   ├── MaaSModelRef 削除
  │   ├── GPU Pod 停止待ち
  │   └── purge: true なら PVC 上のモデルデータも削除
  │
  └── state: present のモデル
      ├── Download Job → PVC にモデル保存（キャッシュ済みならスキップ）
      ├── GPU キャパシティチェック（不足なら fail）
      ├── LLMInferenceService 作成 → vLLM Pod 起動
      ├── MaaSModelRef 作成 → MaaS Gateway にモデル登録
      └── keycloak_groups 経由で:
          ├── MaaSAuthPolicy（state: present のモデルのみ）
          └── MaaSSubscription（state: present のモデルのみ）
```

---

## ユーザー管理

### ユーザーの追加

**何が起きるか**: Keycloak の `maas` realm にユーザーが作成され、指定したグループに割り当てられます。パスワードが自動生成され、`.credentials/<username>.password` に保存されます。

1. `inventory/myenv/group_vars/all/components.yml` を編集:

   ```yaml
   keycloak_users:
     - username: admin1
       email: admin1@example.com
       groups: [maas-admins]
     - username: testuser1
       email: testuser1@example.com
       groups: [maas-qwen3-06b-users]
     - username: newuser                    # ← 追加
       email: newuser@example.com
       groups: [maas-qwen3-06b-users]       # 既存グループに割り当て
   ```

2. Playbook を実行:

   ```bash
   ansible-playbook playbooks/manage_maas_access.yml -i inventory/myenv
   ```

3. パスワードを確認:

   ```bash
   cat .credentials/newuser.password
   ```

**注意**:
- 既存ユーザーのパスワードは変更されません（新規ユーザーのみ生成）
- パスワードファイルが既に存在する場合はその値が再利用されます
- `temporary: false` のため、初回ログイン時のパスワード変更は求められません

### ユーザーの削除

Playbook ではユーザー削除をサポートしていません。Keycloak の管理コンソールから手動で削除してください。

```
https://keycloak-keycloak.<cluster_domain>/admin/master/console/#/maas/users
```

---

## グループ管理

### グループの役割

Keycloak のグループは MaaS のアクセス制御に直結します。グループごとに以下が自動生成されます：

| 設定項目 | 生成されるリソース | 効果 |
|---|---|---|
| `models` | MaaSAuthPolicy | このグループのユーザーがアクセスできるモデル一覧 |
| `priority` | MaaSSubscription | リクエスト競合時の優先度（数値が大きいほど優先） |
| `quota_tokens_24h` | MaaSSubscription | 24時間あたりのトークン使用上限 |

### グループの追加

**何が起きるか**:
1. Keycloak に新しいグループが作成される
2. `models-as-a-service` namespace に MaaSAuthPolicy が作成される（モデルへのアクセス許可）
3. `models-as-a-service` namespace に MaaSSubscription が作成される（クォータ設定）

```yaml
keycloak_groups:
  maas-admins:
    models: ["*"]               # "*" は全モデルへのアクセスを許可
    priority: 20
    quota_tokens_24h: 5000000
  maas-qwen3-06b-users:
    models: ["qwen3-06b"]       # 特定モデルのみ許可
    priority: 10
    quota_tokens_24h: 1000000
  maas-researchers:              # ← 追加
    models: ["qwen3-06b"]
    priority: 15                 # admins より低く、一般ユーザーより高い
    quota_tokens_24h: 3000000
```

```bash
ansible-playbook playbooks/manage_maas_access.yml -i inventory/myenv
```

### 生成されるリソースの具体例

上記の `maas-researchers` グループから以下が生成されます：

**MaaSAuthPolicy** (`maas-researchers-auth-policy`):
```yaml
apiVersion: maas.opendatahub.io/v1alpha1
kind: MaaSAuthPolicy
metadata:
  name: maas-researchers-auth-policy
  namespace: models-as-a-service
spec:
  subjects:
    groups:
      - name: maas-researchers
  modelRefs:
    - name: qwen3-06b
      namespace: llm-serving
```

**MaaSSubscription** (`maas-researchers-subscription`):
```yaml
apiVersion: maas.opendatahub.io/v1alpha1
kind: MaaSSubscription
metadata:
  name: maas-researchers-subscription
  namespace: models-as-a-service
spec:
  owner:
    groups:
      - name: maas-researchers
  priority: 15
  modelRefs:
    - name: qwen3-06b
      namespace: llm-serving
      tokenRateLimits:
        - limit: 3000000
          window: 24h
```

### 命名規則

モデル名・グループ名は K8s リソース名に使用されるため、**RFC 1123 準拠**（小文字英数字、ハイフン、ドットのみ）である必要があります。

- OK: `qwen3-06b`, `deepseek-r1`, `llama3.1-8b`
- NG: `Qwen3-0.6B` (大文字), `qwen3_5` (アンダースコア)

モデルアクセス用グループは `maas-<モデル名>-users` の形式を推奨します。

整合が必要な箇所：
- `keycloak_groups` の dict キー（グループ名）
- `keycloak_users[].groups` の値（ユーザーが属するグループ）
- `keycloak_groups[].models` の値（`models` の dict キーと一致）
- `models` の dict キー（K8s リソース名として RFC 1123 準拠が必要）

---

## モデル追加

### 概要

新しい LLM モデルを MaaS に追加するには、以下の 3 つの作業が必要です：

1. **モデルのデプロイ**: HuggingFace からダウンロード → PVC に保存 → LLMInferenceService 作成 → vLLM Pod 起動
2. **MaaS への登録**: MaaSModelRef を作成して、MaaS Gateway からモデルにルーティングできるようにする
3. **アクセス権の設定**: AuthPolicy / Subscription を作成して、誰がどの条件でモデルにアクセスできるかを定義

`	` Playbook はこの 3 ステップを一括実行します。

### 作成されるリソース

| フェーズ | リソース | namespace | 説明 |
|---|---|---|---|
| モデルデプロイ | PVC `hf-model-cache` | llm-serving | モデルファイルの永続ストレージ（初回のみ作成） |
| モデルデプロイ | Job `download-<model>` | llm-serving | HuggingFace からモデルをダウンロード |
| モデルデプロイ | ConfigMap `<chat-template>` | llm-serving | チャットテンプレート |
| モデルデプロイ | LLMInferenceService | llm-serving | vLLM Pod + ルーティング + オートスケール |
| MaaS 登録 | MaaSModelRef | llm-serving | MaaS Gateway へのモデル登録 |
| アクセス権 | MaaSAuthPolicy | models-as-a-service | グループ → モデルのアクセス許可 |
| アクセス権 | MaaSSubscription | models-as-a-service | クォータ（トークン上限・優先度） |

### 手順

#### Step 1: components.yml を更新

```yaml
# 1. models にモデルを追加（必須）
# デフォルト値は roles/llm_serving/defaults/main.yml の model_defaults を参照
models:
  qwen3-06b:
    hf_repo: Qwen/Qwen3-0.6B
    state: present
  qwen3-8b:                    # ← 追加
    hf_repo: Qwen/Qwen3-8B
    state: present

# 2. アクセス用グループを追加（必須）
# このグループに属するユーザーが新モデルにアクセスできる
keycloak_groups:
  maas-admins:
    models: ["*"]              # "*" なので自動的に全 present モデルにアクセス可能
    priority: 20
    quota_tokens_24h: 5000000
  maas-qwen3-06b-users:
    models: ["qwen3-06b"]
    priority: 10
    quota_tokens_24h: 1000000
  maas-qwen3-8b-users:         # ← 追加
    models: ["qwen3-8b"]
    priority: 10
    quota_tokens_24h: 1000000

# 3. ユーザーをグループに割り当て（必須）
keycloak_users:
  - username: testuser1
    email: testuser1@example.com
    groups: [maas-qwen3-06b-users, maas-qwen3-8b-users]  # ← グループ追加
```

#### Step 2: Playbook を実行

```bash
ansible-playbook playbooks/llm_add_model.yml -i inventory/myenv
```

このコマンドで実行される処理：

1. **Preflight (light)** — oc ログイン確認、cluster_domain 取得
2. **llm_serving** — absent モデル削除（GPU 解放）→ present モデルのダウンロード・デプロイ・MaaSModelRef 作成 → **Ready 待ち（最大20分）**
3. **manage_maas_access** — Keycloak グループ同期 → AuthPolicy/Subscription 生成・適用

#### Step 3: 動作確認

```bash
# LLMInferenceService の Ready 確認
oc get llminferenceservice -n llm-serving

# API Key で推論テスト
API_KEY=$(cat .vllm-token)
curl -sk "https://maas.<cluster_domain>/llm-serving/qwen3-8b/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3-8b","messages":[{"role":"user","content":"Hello"}],"max_tokens":20}'
```

### 非 Qwen モデルの追加

vLLM のパーサーとチャットテンプレートはモデルファミリーごとに異なります。

#### 非 Qwen モデルの追加（パーサー変更が必要な場合）

`models` にモデル固有の設定を記述します：

```yaml
models:
  deepseek-r1:
    hf_repo: deepseek-ai/DeepSeek-R1-0528
    state: present
    tool_call_parser: hermes
    reasoning_parser: deepseek_r1
```

#### チャットテンプレートの変更が必要な場合

1. テンプレートファイルを配置:
   ```bash
   cp my-template.jinja manifests/07-llm-serving/chat_template.jinja
   ```

2. `models` に ConfigMap 名を指定:
   ```yaml
   models:
     my-model:
       hf_repo: org/my-model
       state: present
       chat_template_configmap: my-model-chat-template
   ```

> **注意**: 現状、チャットテンプレートファイルのパス (`manifests/07-llm-serving/chat_template.jinja`) は固定です。異なるテンプレートが必要な場合は、このファイルを差し替えてから実行してください。

#### モデル追加時に更新が必要な設定一覧

| 設定 | 場所 | 必須 | 説明 |
|---|---|---|---|
| `models` | components.yml | はい | モデルの hf_repo, state, パーサー等を定義 |
| `keycloak_groups` | components.yml | はい | アクセス制御グループ（AuthPolicy + Subscription の元データ） |
| `keycloak_users[].groups` | components.yml | はい | ユーザーへのグループ割り当て |

---

## モデルの停止と再開

### モデルの停止（GPU 解放）

`models` の該当モデルの `state` を `absent` に変更して site.yml を実行します。

```yaml
# components.yml
models:
  qwen3-06b:
    hf_repo: Qwen/Qwen3-0.6B
    state: absent    # ← present から absent に変更
```

```bash
uv run ansible-playbook site.yml -i inventory/myenv --tags workload
```

実行される処理：
1. LLMInferenceService を削除（vLLM Pod が停止）
2. GPU Pod の停止を待機
3. MaaSModelRef を削除
4. AuthPolicy / Subscription からモデルが除外される

> **PVC キャッシュは残ります。** ダウンロード済みのモデルデータは PVC 上に保持されるため、再開時にダウンロードは不要です。PVC データも削除する場合は `purge: true` を追加してください。

### モデルの再開

`state` を `present` に戻して site.yml を実行します。PVC キャッシュが残っていればダウンロードはスキップされます。

```bash
uv run ansible-playbook site.yml -i inventory/myenv --tags workload
```

### GPU キャパシティチェック

`state: present` のモデルの合計 `gpu_count` がクラスタの GPU 数を超える場合、site.yml は以下のエラーで停止します：

```
GPU 不足: present モデルの合計 GPU 要求数 (2) が
クラスタの GPU 数 (1) を超えています。
一部のモデルの state を absent に変更するか、gpu_count を調整してください。
```

---

## アクセス制御とクォータ

### AuthPolicy と Subscription の関係

```
keycloak_groups の設定
    │
    ├── models: ["qwen3-06b"]     → MaaSAuthPolicy  (「このグループはこのモデルにアクセスできる」)
    ├── priority: 10              → MaaSSubscription (「リクエスト競合時の優先度」)
    └── quota_tokens_24h: 1000000 → MaaSSubscription (「24時間あたりのトークン上限」)
```

### 生成されるリソースの対応表

| keycloak_groups のキー | 生成される AuthPolicy | 生成される Subscription | namespace |
|---|---|---|---|
| `maas-admins` | `admin-auth-policy` (全モデル) | `maas-admins-subscription` | models-as-a-service |
| `maas-qwen3-06b-users` | `maas-qwen3-06b-users-auth-policy` | `maas-qwen3-06b-users-subscription` | models-as-a-service |

### priority の設計指針

| priority | 用途 | 説明 |
|---|---|---|
| 20+ | 管理者 | 常にリクエストが優先される |
| 10-19 | 一般ユーザー | 通常の優先度 |
| 1-9 | 低優先度 | 他のリクエストがない時のみ処理 |

### quota の設計指針

- `quota_tokens_24h: 5000000` — 管理者向け（約5Mトークン/日）
- `quota_tokens_24h: 1000000` — 一般ユーザー向け（約1Mトークン/日）
- 超過するとリクエストが 429 Too Many Requests で拒否される

### クォータの変更

`components.yml` の `quota_tokens_24h` を変更して再適用:

```bash
ansible-playbook playbooks/manage_maas_access.yml -i inventory/myenv
```

MaaSSubscription が更新され、即座に反映されます。

---

## MaaS API Key

### API Key の発行方法

#### 方法 1: setup-opencode.sh（推奨）

```bash
bash ../scripts/setup-opencode.sh setup
```

`admin1` ユーザーの権限で API Key を発行し、`.vllm-token` と `opencode.json` を生成します。`oc exec` で maas-api Pod に直接リクエストするため、port-forward は不要です。

> `setup-opencode.sh` は ansible/ の親ディレクトリ（`rhoai-3.5-sno-4.22/scripts/`）にあります。

#### 方法 2: Playbook

```bash
ansible-playbook playbooks/maas_create_apikey.yml -i inventory/myenv
```

#### 方法 3: RHOAI Dashboard

RHOAI Dashboard (`https://rhods-dashboard-redhat-ods-applications.<cluster_domain>`) にログインし、MaaS セクションから API Key を発行できます。

### API Key の仕組み

- API Key は `sk-oai-` プレフィックスの文字列
- MaaS Gateway の AuthPolicy (`maas-gateway-auth`) が `Bearer sk-oai-*` パターンのリクエストを API Key 認証として処理
- API Key は MaaSSubscription に紐づき、クォータの消費対象になる
- 有効期限はデフォルト 90 日（`API_KEY_MAX_EXPIRATION_DAYS`）

### API Key が無効になるケース

- cleanup → 再デプロイ（MaaS DB がリセットされるため）
- Keycloak realm の再作成
- 有効期限の超過

---

## 検証

```bash
ansible-playbook playbooks/verify.yml -i inventory/myenv
```

### チェック項目

| カテゴリ | チェック内容 |
|---|---|
| Infrastructure | LVM Operator CSV, LVMCluster Ready, MetalLB speaker |
| Dependencies | 8 Operator の CSV Succeeded |
| Storage | NooBaa Ready |
| Platform | RHOAI Operator CSV, DataScienceCluster Ready, MaaS Gateway Programmed, Dashboard Deployment, Keycloak Ready |
| Integration | Keycloak ユーザーグループ割り当て, OAuth IdP 登録, AITenant 存在 |
| Workloads | LLMInferenceService Ready, MaaS API healthy, MaaSModelRef, MaaS Subscription Active, **API Key 発行テスト**, MLflow, OGX, Guardrails |
| Health | Route HostAlreadyClaimed 検出, 異常 Pod 検出, LVMCluster VolumeGroupsReady |

---

## 削除

Ansible での削除は未実装です。シェルスクリプトを使用してください:

```bash
# 全フェーズ削除（Phase 12 → 01）— ansible/ ディレクトリから実行
bash ../scripts/cleanup-all.sh all

# 特定フェーズのみ削除
bash ../scripts/cleanup-all.sh phase-07   # LLM Serving のみ

# 各フェーズのリソース確認（削除なし）
bash ../scripts/cleanup-all.sh status
```

削除順序は Phase 12（Observability）→ Phase 01（LVM）の逆順で実行されます。

---

## トラブルシューティング

詳細なトラブルシューティング手順は [troubleshooting.md](troubleshooting.md) を参照してください。

> **uv のオフラインインストール**: Disconnected 環境に uv がない場合、オンライン環境で `curl -LsSf https://astral.sh/uv/install.sh | sh` でインストール後、`~/.local/bin/uv` バイナリを持ち込んでください。uv はシングルバイナリで依存がありません。

### よくある問題

#### LVMCluster が Failed

EC2 インスタンス再起動後に NVMe デバイス番号が変わった可能性があります。

```bash
# デバイスと PCI パスの対応を確認
oc debug node/<node-name> -- chroot /host bash -c \
  'for d in $(lsblk -dn -o NAME | grep nvme); do \
    BP=$(find /dev/disk/by-path -lname "*/$d" -printf "%f" 2>/dev/null); \
    printf "%-12s %-8s %-40s %s\n" "/dev/$d" \
      "$(lsblk -dn -o SIZE /dev/$d)" \
      "$(lsblk -dn -o MODEL /dev/$d)" \
      "${BP:-(none)}"; \
  done'
```

`/dev/disk/by-path/pci-*` 形式のパスを `components.yml` の `lvm_device_paths` に指定すると、再起動後も安定します。

#### Route が HostAlreadyClaimed

```bash
oc get route --all-namespaces | grep HostAlreadyClaimed
```

不要な Route を削除してください。

#### API Key 発行に失敗する

MaaSSubscription が `models-as-a-service` namespace に Active 状態で存在するか確認:

```bash
oc get maassubscription -n models-as-a-service
```

存在しない場合は `manage_maas_access.yml` を再実行してください。

#### Keycloak が起動しない

Keycloak の PostgreSQL が PVC をマウントできていない場合、LVMCluster の状態を確認してください。

---

## クリーンインストール（cleanup → 再デプロイ）

全コンポーネントを削除して初期状態から再構築する手順です。

```bash
# 1. 全リソース削除（ansible/ ディレクトリから実行）
bash ../scripts/cleanup-all.sh --yes all

# 2. 再デプロイ
uv run ansible-playbook site.yml -i inventory/myenv

# 3. 検証
uv run ansible-playbook playbooks/verify.yml -i inventory/myenv
```

> `cleanup-all.sh` は `scripts/` ディレクトリ（ansible/ の親の `rhoai-3.5-sno-4.22/scripts/`）にあります。ansible/ ディレクトリから実行する場合は `../scripts/cleanup-all.sh` を指定してください。
