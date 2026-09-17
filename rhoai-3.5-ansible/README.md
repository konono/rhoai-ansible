# RHOAI 3.5 Ansible Installer

## 1. 概要

OpenShift AI (RHOAI) 3.5 を OpenShift 環境にデプロイする Ansible Playbook です。Single Node OpenShift (SNO) およびマルチノードクラスターの両方に対応しています。

以下のコンポーネントを段階的にインストールします：

- **Infrastructure**: LVM Operator (ローカルストレージ), MetalLB (LoadBalancer)
- **Dependency Operators**: cert-manager, ServiceMesh, NFD, GPU Operator, RHCL (Authorino), Kueue, JobSet, LeaderWorkerSet
- **Storage**: ODF (NooBaa オブジェクトストレージ)
- **Platform**: OpenShift AI (RHOAI), Keycloak (IdP)
- **Integration**: Keycloak → OpenShift OAuth, Keycloak → MaaS ポリシー, AITenant OIDC
- **Workloads**: LLM Serving (vLLM), MaaS リソース, MLflow, OGX, NeMo Guardrails, Observability

---

## 2. 前提条件

### ハードウェア要件

| リソース | 最小要件 | 備考 |
|---|---|---|
| GPU | NVIDIA GPU × 1 | GPU Operator が管理。vLLM 推論に必要 |
| ストレージ | 空きブロックデバイス × 1 以上 | LVM Operator が VolumeGroup として使用。200GB 以上推奨 |
| メモリ | 64GB 以上 | SNO では全コンポーネントが同一ノードで動作。マルチノードでは分散可能 |
| CPU | 16 vCPU 以上 | |

### ソフトウェア要件

| ソフトウェア | バージョン | 用途 |
|---|---|---|
| OpenShift | 4.22 | ベースクラスター |
| Python | 3.12.x | Ansible 実行環境。wheels が cp312 向けにビルドされているため **3.12 系のみ対応** |
| `oc` | 4.22+ | OpenShift CLI（PATH に存在すること） |
| pip | 最新 | Python パッケージ管理（uv は自動インストールされるため事前準備不要） |

> **Python バージョンに関する注意**: ホストに Python 3.12 がない場合でも、`download-deps.sh` でスタンドアロン Python 3.12 をダウンロードし、`setup-env.sh` が自動でインストールします。ホストの Python には一切影響しません。

### Python パッケージ

| パッケージ | バージョン | 用途 |
|---|---|---|
| ansible-core | >= 2.17, < 3.0 | Ansible 本体 |
| ansible | >= 10.0 | Ansible コレクション |
| kubernetes | >= 29.0 | `kubernetes.core` モジュール用 |
| jmespath | >= 1.0 | `json_query` フィルター用 |

> これらは `pyproject.toml` に定義済みです。手動で pip install する必要はありません。

### Ansible Galaxy コレクション

| コレクション | バージョン | 用途 |
|---|---|---|
| kubernetes.core | >= 5.0.0 | K8s リソース管理 |
| community.general | >= 9.0.0 | 汎用フィルター・モジュール |

### クラスター要件

- `cluster-admin` 権限でログイン済み（`oc login`）
- OperatorHub (redhat-operators) にアクセス可能（Disconnected 環境では事前にミラーリング）
- `*.apps.<cluster_domain>` の DNS が解決可能

---

## 3. セットアップ

### クイックスタート（全環境共通）

```bash
# 1. オンライン環境で資材をダウンロード（初回のみ）
cd ansible
bash scripts/download-deps.sh

# 2. 環境セットアップ（オンラインでも Disconnected でも同じコマンド）
bash scripts/setup-env.sh

# 3. Playbook 実行
uv run ansible-playbook site.yml -i inventory/myenv
```

以上です。以降は詳細な説明です。

### Playbook の実行方法

本プロジェクトでは **`uv run` を標準の実行方法とします**。`source .venv/bin/activate` は不要です。

```bash
# すべての ansible コマンドに uv run を付ける
uv run ansible-playbook site.yml -i inventory/myenv
uv run ansible-playbook playbooks/verify.yml -i inventory/myenv
uv run ansible-galaxy collection list
uv run ansible --version
```

> **なぜ `uv run` か**: `uv run` は `.venv` のアクティベートなしに仮想環境内のコマンドを実行します。activate 忘れによる「ホストの Python で実行してしまった」事故を防ぎます。`pyproject.toml` の `[tool.uv] find-links` 設定により、オフラインでもローカル wheels が自動参照されます。

### Step 1: 資材ダウンロード（オンライン環境で実行）

```bash
cd ansible
bash scripts/download-deps.sh
```

以下がダウンロードされます：

| ダウンロード先 | 内容 | サイズ目安 |
|---|---|---|
| `vendor/uv/` | uv バイナリ（4 プラットフォーム分） | ~70MB |
| `vendor/python/` | Python 3.12 スタンドアロンビルド（4 プラットフォーム分） | ~200MB |
| `vendor/wheels/` | Python wheels（macOS ARM64/x86_64, Linux aarch64/x86_64） | ~160MB |
| `vendor/collections/` | Ansible Galaxy collections | ~3MB |

特定のコンポーネントだけ再ダウンロードすることもできます：

```bash
bash scripts/download-deps.sh python       # Python だけ
bash scripts/download-deps.sh wheels       # wheels だけ
bash scripts/download-deps.sh uv python    # 複数指定
bash scripts/download-deps.sh --help       # ヘルプ
```

> **GitHub API レートリミット**: Python スタンドアロンビルドのダウンロードは GitHub API を使用します。`gh` CLI が利用可能な場合は認証済み API を使うためレートリミットの問題は起きません。`gh auth login` を事前に実行しておくことを推奨します。

### Step 2: Disconnected 環境への持ち込み

`ansible/` ディレクトリごとコピーするのが最も確実です。最低限必要なファイルは以下の通りです：

```
ansible/
├── vendor/              ← ダウンロードした資材一式（必須）
│   ├── uv/              ← uv バイナリ
│   ├── python/          ← Python 3.12 スタンドアロンビルド
│   ├── wheels/          ← Python wheels
│   └── collections/     ← Ansible Galaxy collections
├── pyproject.toml       ← 依存定義 + uv の find-links 設定（必須）
├── scripts/             ← セットアップスクリプト（必須）
├── ansible.cfg          ← Ansible 設定（必須）
├── requirements.yml     ← Galaxy collections 定義（必須）
├── site.yml             ← メイン Playbook
├── roles/               ← 各 Role
├── inventory/           ← Inventory
└── ...
```

### Step 3: 環境セットアップ（Disconnected 環境で実行）

```bash
cd ansible
bash scripts/setup-env.sh
```

`setup-env.sh` は以下を自動で行います：

1. **uv の配置** — `vendor/uv/` からプラットフォームに合った uv バイナリを `.local/bin/` に展開
2. **Python 3.12 の配置** — `vendor/python/` からスタンドアロンビルドを `.local/python/` に展開（ホストに Python 3.12 がない場合のみ）
3. **仮想環境の作成** — `.venv/` を作成し、`vendor/wheels/` から依存パッケージをオフラインインストール
4. **Galaxy collections のインストール** — `vendor/collections/` から `collections/` にインストール

> **ホストへの影響なし**: すべてのファイルは `ansible/` ディレクトリ内に閉じます。ホストの Python、pip、グローバルパッケージには一切影響しません。
>
> | 生成先 | 内容 |
> |---|---|
> | `.venv/` | Python 仮想環境 |
> | `.local/bin/` | uv バイナリ（ホストに uv がない場合のみ） |
> | `.local/python/` | Python 3.12（ホストに 3.12 がない場合のみ） |
> | `collections/` | Ansible Galaxy collections |

### セットアップの確認

```bash
uv run ansible-playbook --version
```

以下のように Python 3.12.x、ansible-core 2.21.x が表示されれば成功です：

```
ansible-playbook [core 2.21.4]
  ...
  python version = 3.12.14 (...)
```

### トラブルシューティング

| 症状 | 原因 | 対処 |
|---|---|---|
| `uv: command not found` | uv が未インストール | `bash scripts/setup-env.sh` を再実行（vendor/uv/ から自動インストールされる） |
| `cp314` の wheel がないエラー | ホストの Python 3.14 が使われている | `vendor/python/` にスタンドアロンビルドがあるか確認。なければ `bash scripts/download-deps.sh python` で再ダウンロード |
| `uv sync` で解決不能エラー | pyproject.toml の `requires-python` とホストの Python バージョン不一致 | `requires-python = ">=3.12,<3.13"` であることを確認 |
| Galaxy collection が見つからない | `vendor/collections/` が空 | `bash scripts/download-deps.sh collections` で再ダウンロード |

---

## 4. Inventory 準備

### Inventory の作成

```bash
cp -r inventory/sample inventory/myenv
```

### 編集が必要なファイル

| ファイル | 内容 | 影響範囲 |
|---|---|---|
| `group_vars/all/cluster.yml` | クラスター共通設定（kubeconfig, StorageClass, Proxy, Operator チャネル, DB パスワード） | 複数の Role から参照される。変更は広範囲に影響 |
| `group_vars/all/components.yml` | ソリューション固有設定（LVM デバイス, モデル名, Keycloak ユーザー, 有効/無効フラグ） | 各 Role 内でのみ使用 |
| `group_vars/all/vault.yml` | HuggingFace トークン等のシークレット | LLM ダウンロードに必要 |
| `hosts.yml` | Ansible ホスト定義 | 通常変更不要（localhost 固定） |

> **注意**: `ansible.cfg` のデフォルト inventory は `inventory/sample` です。必ず `-i inventory/myenv` を指定してください。指定を忘れると sample の設定でデプロイされます。

### 最低限必要な編集

1. **vault.yml** — HuggingFace トークンの設定（LLM ダウンロードに必須）:
   ```bash
   vi inventory/myenv/group_vars/all/vault.yml
   ```
   ```yaml
   hf_token: "hf_xxxxxxxxxxxxxxxxxxxxx"
   ```
   > HuggingFace の [Access Tokens](https://huggingface.co/settings/tokens) ページで「Read」権限のトークンを作成してください。

2. **cluster.yml** — kubeconfig パスの設定（環境による）:
   ```bash
   vi inventory/myenv/group_vars/all/cluster.yml
   ```
   `KUBECONFIG` 環境変数が設定済みなら編集不要です。

3. **components.yml** — LVM デバイスパス・モデル設定の調整:
   ```bash
   vi inventory/myenv/group_vars/all/components.yml
   ```
   `lvm_device_paths` を実環境のデバイスに合わせてください。

---

## 5. パラメーターリファレンス

### 5.1 cluster.yml — クラスター共通設定

これらのパラメーターは複数の Role から参照されます。変更は広範囲に影響します。

#### パス解決

| 変数 | 説明 | デフォルト | 顧客変更 |
|---|---|---|---|
| `ansible_root` | Ansible プロジェクトルート（`ansible/`）の絶対パス | `playbook_dir` から自動計算 | **変更不要** — `site.yml` と `playbooks/*.yml` のどちらから実行しても正しいパスに解決される |

> `ansible_root` は role やテンプレートからファイルを参照する際の基準パスです。`playbook_dir` はエントリポイントにより変わるため、直接使わず `ansible_root` を使用してください。

#### Kubeconfig

| 変数 | 説明 | デフォルト | 顧客変更 |
|---|---|---|---|
| `k8s_kubeconfig` | kubeconfig ファイルのパス | 未設定（`KUBECONFIG` 環境変数 → `~/.kube/config` の順でフォールバック） | **環境による** — `oc login` 後に `KUBECONFIG` 環境変数が設定されていれば不要。明示的に指定したい場合のみ設定 |

#### ストレージ

| 変数 | 説明 | デフォルト | 顧客変更 |
|---|---|---|---|
| `storage_class` | 全 PVC で使用する StorageClass 名 | `lvms-vg1` | **必要に応じて** — LVM を使わない場合（例: EBS 直接使用）は `gp3-csi` 等に変更。`lvms-*` を指定すると `install_lvm` が自動的に有効化される |

#### Proxy

| 変数 | 説明 | デフォルト | 顧客変更 |
|---|---|---|---|
| `cluster_proxy.http_proxy` | HTTP プロキシ URL | `""` (空 = スキップ) | **Proxy 環境のみ** |
| `cluster_proxy.https_proxy` | HTTPS プロキシ URL | `""` | **Proxy 環境のみ** |
| `cluster_proxy.no_proxy` | プロキシ除外リスト | `"localhost,127.0.0.1,.cluster.local,.svc"` | **Proxy 環境のみ** — `.apps.<cluster_domain>` を含めないよう注意。含めると MaaS Gateway の通信に影響する |

> **Proxy の注意**: 設定すると `rhoai` ロールが `kube-auth-proxy`, `rhods-dashboard`, `maas-ui` に環境変数として注入します。空の場合はスキップされます。

#### Operator チャネル

| 変数 | 説明 | デフォルト | 顧客変更 |
|---|---|---|---|
| `operator_channels.lvm` | LVM Operator のチャネル | `stable-4.22` | OpenShift バージョンに合わせる |
| `operator_channels.metallb` | MetalLB のチャネル | `stable` | 通常変更不要 |
| `operator_channels.cert_manager` | cert-manager のチャネル | `stable-v1` | 通常変更不要 |
| `operator_channels.servicemesh` | ServiceMesh のチャネル | `stable` | 通常変更不要 |
| `operator_channels.nfd` | NFD のチャネル | `stable` | 通常変更不要 |
| `operator_channels.gpu` | GPU Operator のチャネル | `stable` | 通常変更不要 |
| `operator_channels.rhcl` | RHCL (Kuadrant/Authorino) のチャネル | `stable` | 通常変更不要 |
| `operator_channels.kueue` | Kueue のチャネル | `stable-v1.4` | 通常変更不要 |
| `operator_channels.jobset` | JobSet のチャネル | `stable-v1.0` | 通常変更不要 |
| `operator_channels.leaderworkerset` | LeaderWorkerSet のチャネル | `stable-v1.0` | 通常変更不要 |
| `operator_channels.coo` | Cluster Observability Operator のチャネル | `stable` | 通常変更不要 |
| `operator_channels.opentelemetry` | OpenTelemetry のチャネル | `stable` | 通常変更不要 |
| `operator_channels.odf` | ODF のチャネル | `stable-4.22` | OpenShift バージョンに合わせる |
| `operator_channels.rhoai` | RHOAI のチャネル | `stable-3.5` | RHOAI バージョンに合わせる |
| `operator_channels.keycloak` | Keycloak (RHBK) のチャネル | `stable-v26` | 通常変更不要 |
| `operator_channels.group_sync` | Group Sync Operator のチャネル | `alpha` | 通常変更不要 |

> **Operator チャネルの注意**: OpenShift のメジャー/マイナーバージョンを変更した場合、`lvm` と `odf` のチャネルも合わせて更新してください。

#### DB パスワード

| 変数 | 説明 | デフォルト | 顧客変更 |
|---|---|---|---|
| `maas_db_password` | MaaS PostgreSQL のパスワード | `""` (空 = 自動生成) | **通常不要** — 空にしておけば初回デプロイ時に自動生成され `.generated-passwords.yml` に保存される |
| `ogx_db_password` | OGX PostgreSQL のパスワード | `""` | 同上 |
| `keycloak_db_password` | Keycloak PostgreSQL のパスワード | `""` | 同上 |

> **パスワードの自動生成**: 初回デプロイ時に `preflight` ロールが 20 文字のランダムパスワードを生成し、`ansible/.generated-passwords.yml` に保存します。2回目以降はこのファイルから読み込まれるため、再生成されません。

### 5.2 components.yml — ソリューション固有設定

これらのパラメーターは各 Role 内でのみ使用されます。

#### LVM 設定

| 変数 | 説明 | デフォルト | 顧客変更 |
|---|---|---|---|
| `lvm_device_paths` | LVMS VolumeGroup に使用するブロックデバイスのパス（リスト） | `["/dev/disk/by-path/pci-0000:34:00.0-nvme-1"]` | **必須** — 環境ごとにデバイスが異なる |

デバイスパスには 3 つの形式が使用可能です：

| 形式 | 例 | 安定性 | 推奨 |
|---|---|---|---|
| デバイス名 | `/dev/nvme1n1` | 低（再起動で番号が変わる場合あり） | △ |
| PCI パス | `/dev/disk/by-path/pci-0000:34:00.0-nvme-1` | 高（PCI スロット固定） | **◎ 推奨** |
| デバイス ID | `/dev/disk/by-id/nvme-Amazon_EC2_...` | 高（インスタンス固有） | ○ |

**デバイス確認コマンド**:

```bash
# デバイス一覧（サイズ・モデルで用途を判別）
oc debug node/<node-name> -- chroot /host lsblk -d -o NAME,SIZE,TYPE,MODEL

# デバイス名と PCI パスの対応表
oc debug node/<node-name> -- chroot /host bash -c \
  'for d in $(lsblk -dn -o NAME | grep nvme); do \
    BP=$(find /dev/disk/by-path -lname "*/$d" -printf "%f" 2>/dev/null); \
    printf "%-12s %-8s %-40s %s\n" "/dev/$d" \
      "$(lsblk -dn -o SIZE /dev/$d)" \
      "$(lsblk -dn -o MODEL /dev/$d)" \
      "${BP:-(none)}"; \
  done'
```

出力例:

```
/dev/nvme0n1  300G    Amazon Elastic Block Store               pci-0000:00:04.0-nvme-1
/dev/nvme1n1 419.1G   Amazon EC2 NVMe Instance Storage         pci-0000:34:00.0-nvme-1
/dev/nvme2n1  200G    Amazon Elastic Block Store               pci-0000:23:00.0-nvme-1
```

> **EC2 Instance Storage の注意**: インスタンス再起動でデバイス番号が変わります（例: nvme14n1 → nvme1n1）。**必ず PCI パス形式を使用してください。** LVMS は symlink を解決してからデバイスを使用するため、by-path パスが正しく動作します。

#### MetalLB 設定

| 変数 | 説明 | デフォルト | 顧客変更 |
|---|---|---|---|
| `metallb_ip_range` | MetalLB で使用する IP アドレス範囲 | `""` (空 = ノード IP から自動計算) | **通常不要** — 空ならノードの InternalIP と同じアドレスを使用。カスタム範囲が必要な場合のみ設定（例: `"192.168.1.100-192.168.1.110"`） |

#### LLM モデル設定

モデル管理は `models` 変数で一元化されています。デフォルト値は `roles/llm_serving/defaults/main.yml` の `model_defaults` で定義され、モデルごとに上書きしたいフィールドだけ `models` に記述します。

```yaml
models:
  qwen3-06b:
    hf_repo: Qwen/Qwen3-0.6B       # (必須) HuggingFace リポジトリ名
    state: present                   # present=デプロイ, absent=削除
    # 以下はデフォルト値があるため省略可能
    # vllm_image: vllm/vllm-openai:v0.28.0
    # tool_call_parser: qwen3_coder
    # reasoning_parser: ""
    # chat_template_configmap: qwen3-chat-template
    # gpu_count: 1
    # vllm_extra_args: ["--max-model-len=4096", "--enable-auto-tool-choice"]
    # purge: false                   # true なら PVC 上のモデルデータも削除
```

| フィールド | 説明 | デフォルト |
|---|---|---|
| `hf_repo` | HuggingFace リポジトリ名（必須） | — |
| `state` | `present` でデプロイ、`absent` で削除 | `present` |
| `namespace` | デプロイ先 namespace | `llm-serving` |
| `vllm_image` | vLLM コンテナイメージ | `vllm/vllm-openai:v0.28.0` |
| `tool_call_parser` | `--tool-call-parser` | `qwen3_coder` |
| `reasoning_parser` | `--reasoning-parser`（空なら省略） | `""` |
| `chat_template_configmap` | チャットテンプレート ConfigMap 名 | `qwen3-chat-template` |
| `gpu_count` | 要求 GPU 数 | `1` |
| `vllm_extra_args` | vLLM 追加引数（リスト） | `["--max-model-len=4096", "--enable-auto-tool-choice"]` |
| `purge` | `state: absent` 時に PVC キャッシュも削除するか | `false` |

> **dict キーがモデル名になります。** K8s リソース名に使われるため RFC 1123 準拠（小文字英数字・ハイフン・ドットのみ）が必要です。

#### state による動作

| state | 動作 |
|---|---|
| `present` | モデルをダウンロード（キャッシュ済みならスキップ）→ LLMInferenceService + MaaSModelRef 作成 → AuthPolicy/Subscription に含める |
| `absent` | LLMInferenceService + MaaSModelRef 削除（GPU 解放）→ AuthPolicy/Subscription から除外。PVC キャッシュは残る |
| `absent` + `purge: true` | 上記に加え、PVC 上のモデルデータも削除 |

> **処理順序**: site.yml 実行時、`absent` モデルが先に処理されます（GPU 解放のため）。その後 `present` モデルがデプロイされます。
>
> **GPU キャパシティチェック**: `present` モデルの合計 `gpu_count` がクラスタの GPU 数を超える場合、デプロイ前にエラーで停止します。

#### Keycloak 設定

| 変数 | 説明 | デフォルト | 顧客変更 |
|---|---|---|---|
| `keycloak_namespace` | Keycloak をデプロイする namespace | `keycloak` | 通常変更不要 |
| `keycloak_realm` | Keycloak realm 名 | `maas` | 通常変更不要 |
| `keycloak_users` | 作成するユーザーのリスト | admin1 + testuser1 | **必要に応じて** — ユーザー名、メール、グループ割り当てを環境に合わせる |
| `keycloak_groups` | MaaS アクセス制御グループの定義 | maas-admins + maas-qwen3-06b-users | **モデル変更時に必須** — グループ名は `maas-<モデル名>-users` 形式 |

> **命名規則**: グループ名は K8s リソース名に使用されるため RFC 1123 準拠が必要です。`keycloak_groups` の dict キー、`keycloak_users[].groups` の値、`keycloak_groups[].models` の値が整合する必要があります。

> **アクセス制御**: `keycloak_groups` の各グループから MaaSAuthPolicy（アクセス許可）と MaaSSubscription（クォータ）が自動生成されます。`keycloak_groups[].models` は `models` 変数の dict キーを参照します。`state: present` のモデルのみポリシーに含まれます。`priority` はリクエスト競合時の優先度（大きいほど優先）、`quota_tokens_24h` は 24 時間あたりのトークン上限です。詳細は [docs/operations.md](docs/operations.md#アクセス制御とクォータ) を参照。

#### Role 有効/無効フラグ

| 変数 | 対象 Role | デフォルト | 顧客変更 |
|---|---|---|---|
| `install_lvm` | LVM Operator | `true` | `storage_class` が `lvms-*` 以外なら `false` に |
| `install_metallb` | MetalLB | `true` | ベアメタルや LoadBalancer が不要な場合 `false` |
| `install_cert_manager` | cert-manager | `true` | 通常変更不要（rhoai の依存） |
| `install_servicemesh` | ServiceMesh | `true` | 通常変更不要（rhoai の依存） |
| `install_nfd` | Node Feature Discovery | `true` | 通常変更不要（rhoai/gpu の依存） |
| `install_gpu_operator` | GPU Operator | `true` | GPU なし環境では `false` |
| `install_rhcl` | RHCL (Kuadrant/Authorino) | `true` | 通常変更不要（rhoai の依存） |
| `install_kueue` | Kueue | `true` | 通常変更不要（rhoai の依存） |
| `install_jobset` | JobSet | `true` | 通常変更不要（rhoai の依存） |
| `install_leaderworkerset` | LeaderWorkerSet | `true` | 通常変更不要（rhoai の依存） |
| `install_odf` | ODF (NooBaa) | `true` | MLflow / OGX を使わない場合 `false` |
| `install_rhoai` | OpenShift AI | `true` | 通常変更不要（中核コンポーネント） |
| `install_keycloak` | Keycloak | `true` | 認証不要なら `false`（ただし MaaS ポリシーも無効になる） |
| `install_llm_serving` | LLM Serving | `true` | LLM 推論が不要なら `false` |
| `install_maas_resources` | MaaS リソース | `true` | 通常変更不要（llm_serving の依存） |
| `install_mlflow` | MLflow | `true` | 実験管理が不要なら `false` |
| `install_ogx` | OGX Server | `true` | OGX が不要なら `false` |
| `install_guardrails` | NeMo Guardrails | `true` | Guardrails が不要なら `false` |
| `install_observability` | Observability | `true` | 監視が不要なら `false` |

> **依存関係の自動解決**: 親 Role を有効にすると、依存 Role が自動的に有効化されます。例えば `install_rhoai: true` にすると `cert_manager`, `servicemesh`, `rhcl`, `kueue`, `jobset`, `leaderworkerset`, `nfd`, `gpu_operator` が自動的に有効になります。また `storage_class: lvms-*` の場合、`install_lvm` が自動的に有効化されます。

#### Integration 有効/無効フラグ

| 変数 | 対象 | デフォルト | 顧客変更 |
|---|---|---|---|
| `integrate_keycloak_oauth` | Keycloak → OpenShift OAuth IdP 登録 | `true` | Keycloak を OpenShift のログインに使わない場合 `false` |
| `integrate_keycloak_maas` | Keycloak → MaaS AuthPolicy/Subscription 生成 | `true` | 通常変更不要 |
| `integrate_rhoai_oidc` | AITenant への OIDC 設定 | `true` | 通常変更不要 |

### 5.3 vault.yml — シークレット

| 変数 | 説明 | 顧客変更 |
|---|---|---|
| `hf_token` | HuggingFace アクセストークン | **必須** — https://huggingface.co/settings/tokens から取得。モデルダウンロードに必要 |

---

## 6. デプロイフロー

### 6.1 Role 一覧と実行順序

| # | Phase | Role 名 | タグ | フラグ | デプロイ内容 | 依存 Role |
|---|---|---|---|---|---|---|
| 1 | Preflight | `preflight` | `always` | — | oc ログイン確認, パスワード生成, 変数検証, 依存解決 | — |
| 2 | Infrastructure | `lvm` | `infra, lvm` | `install_lvm` | LVM Operator, LVMCluster, default StorageClass 設定 | — |
| 3 | Infrastructure | `metallb` | `infra, metallb` | `install_metallb` | MetalLB Operator, IPAddressPool, L2Advertisement | — |
| 4 | Dependencies | `cert_manager` | `deps, cert_manager` | `install_cert_manager` | cert-manager Operator | — |
| 5 | Dependencies | `servicemesh` | `deps, servicemesh` | `install_servicemesh` | ServiceMesh Operator | — |
| 6 | Dependencies | `nfd` | `deps, nfd` | `install_nfd` | NFD Operator, NodeFeatureDiscovery CR | — |
| 7 | Dependencies | `gpu_operator` | `deps, gpu` | `install_gpu_operator` | GPU Operator, ClusterPolicy | — |
| 8 | Dependencies | `rhcl` | `deps, rhcl` | `install_rhcl` | RHCL Operator, Kuadrant CR | — |
| 9 | Dependencies | `kueue` | `deps, kueue` | `install_kueue` | Kueue Operator | — |
| 10 | Dependencies | `jobset` | `deps, jobset` | `install_jobset` | JobSet Operator, JobSetOperator CR | — |
| 11 | Dependencies | `leaderworkerset` | `deps, leaderworkerset` | `install_leaderworkerset` | LeaderWorkerSet Operator | — |
| 12 | Storage | `odf` | `storage, odf` | `install_odf` | ODF Operator, NooBaa CR | — |
| 13 | Platform | `rhoai` | `platform, rhoai` | `install_rhoai` | RHOAI Operator, DataScienceCluster, MaaS PostgreSQL, MaaS Gateway, HardwareProfile, Authorino TLS, NetworkPolicy | cert_manager, servicemesh, rhcl, kueue, jobset, leaderworkerset, nfd, gpu_operator |
| 14 | Platform | `keycloak` | `platform, keycloak` | `install_keycloak` | RHBK Operator, PostgreSQL, Keycloak CR, Realm Import, Group Sync Operator | lvm |
| 15 | Integration | `integration_keycloak_oauth` | `integration, keycloak_oauth` | `integrate_keycloak_oauth` | OpenShift OAuth IdP 登録, keycloak-ca ConfigMap, OIDC client secret | keycloak |
| 16 | Integration | `integration_keycloak_maas` | `integration, keycloak_maas` | `integrate_keycloak_maas` | Keycloak ユーザー/グループ同期, MaaS AuthPolicy, MaaS Subscription | keycloak |
| 17 | Integration | `integration_rhoai_oidc` | `integration, rhoai_oidc` | `integrate_rhoai_oidc` | AITenant OIDC パッチ, maas-oidc-client-secret | keycloak, rhoai |
| 18 | Workload | `llm_serving` | `workload, llm` | `install_llm_serving` | llm-serving Namespace, hf-token Secret, PVC, ClusterStorageContainer, ChatTemplate ConfigMap, Download Job, LLMInferenceService (Ready 待ち最大20分) | rhoai |
| 19 | Workload | `maas_resources` | `workload, maas` | `install_maas_resources` | Dashboard RBAC | rhoai |
| 20 | Workload | `mlflow` | `workload, mlflow` | `install_mlflow` | mlflow-workspace Namespace, OBC, MLflow CR | rhoai, odf |
| 21 | Workload | `ogx` | `workload, ogx` | `install_ogx` | OGX PostgreSQL, OGXServer CR, vLLM connection Secret | rhoai, odf |
| 22 | Workload | `guardrails` | `workload, guardrails` | `install_guardrails` | NeMo Guardrails ConfigMap, NemoGuardrails CR | llm_serving |
| 23 | Workload | `observability` | `workload, observability` | `install_observability` | COO Operator, OpenTelemetry Operator, Perses, PrometheusRule, PodMonitor, ScrapeConfig, Dashboard | rhoai |
| 24 | Post-deploy | — | `post_deploy, opencode` | — | OpenCode 設定 (API Key 発行 + opencode.json 生成) | — |

### 6.2 各 Role のスコープ

#### `preflight` (always)

| 処理 | 詳細 |
|---|---|
| oc ログイン確認 | `oc whoami` で接続確認 |
| cluster_domain 取得 | `ingresses.config.openshift.io/cluster` から `.spec.domain` を取得 |
| パスワード生成 | `maas_db_password`, `ogx_db_password`, `keycloak_db_password` が空なら自動生成 |
| パスワード永続化 | `.generated-passwords.yml` に保存（再実行時に読み込み） |
| LVM 自動有効化 | `storage_class` が `lvms-*` なら `install_lvm` を自動 true |
| models バリデーション | `models` 変数が定義されていること、各キーが RFC 1123 準拠であること、各モデルに `hf_repo` が定義されていることを検証 |
| RFC 1123 バリデーション | `models` のキーと `keycloak_groups` のキーが K8s 名として有効か検証 |
| 必須変数検証 | `storage_class`, `lvm_device_paths`。LLM Serving 有効時のみ: `hf_token`, `llm_storage_initializer_image` |
| 依存関係解決 | 親 Role が有効なら依存 Role を自動有効化 |

#### `lvm`

| 作成リソース | namespace |
|---|---|
| Namespace `openshift-storage` | — |
| Subscription `lvms-operator` | openshift-lvm-storage |
| LVMCluster `lvmcluster` | openshift-lvm-storage |
| StorageClass `lvms-vg1` をデフォルトに設定 | — |

**Wait**: LVMCluster `status.state == Ready`
**使用変数**: `lvm_device_paths`, `operator_channels.lvm`

#### `metallb`

| 作成リソース | namespace |
|---|---|
| Subscription `metallb-operator` | metallb-system |
| MetalLB CR | metallb-system |
| IPAddressPool `default-pool` | metallb-system |
| L2Advertisement `default-l2` | metallb-system |

**Wait**: MetalLB speaker Pod が Running
**使用変数**: `metallb_ip_range`, `operator_channels.metallb`

#### `rhoai`

| 作成リソース | namespace |
|---|---|
| Subscription `rhods-operator` | redhat-ods-operator |
| PVC `maas-postgres-data` | redhat-ods-applications |
| Deployment `maas-postgres` | redhat-ods-applications |
| Secret `maas-db-config` | redhat-ods-applications, redhat-ai-gateway-infra |
| DataScienceCluster `default-dsc` | — |
| HardwareProfile `nvidia-gpu-1` | redhat-ods-applications |
| Gateway `maas-default-gateway` | openshift-ingress |
| Namespace `llm-serving` | — |
| NetworkPolicy `maas-authorino-allow-rhcl` | redhat-ai-gateway-infra |
| Gateway ConfigMap `maas-gateway-options` | openshift-ingress |

**Wait**: Gateway `Programmed`, AITenant 存在, rhods-dashboard Deployment Ready
**使用変数**: `storage_class`, `maas_db_password`, `operator_channels.rhoai`, `cluster_proxy.*`

#### `keycloak`

| 作成リソース | namespace |
|---|---|
| Namespace `keycloak` | — |
| Subscription `rhbk-operator` | keycloak |
| StatefulSet `postgres` (PostgreSQL) | keycloak |
| Keycloak CR | keycloak |
| KeycloakRealmImport `maas-realm` | keycloak |
| Route `keycloak` (reencrypt) | keycloak |
| Subscription `group-sync-operator` | group-sync-operator |
| GroupSync CR | group-sync-operator |

**Wait**: Keycloak Ready, realm import 完了
**使用変数**: `keycloak_namespace`, `keycloak_realm`, `keycloak_db_password`, `keycloak_users`, `keycloak_groups`, `operator_channels.keycloak`, `operator_channels.group_sync`

#### `llm_serving`

| 作成リソース | namespace |
|---|---|
| Namespace `llm-serving` | — |
| Secret `hf-token` | llm-serving |
| PVC `hf-model-cache` (200Gi) | llm-serving |
| ClusterStorageContainer `default` | — (Cluster-scoped) |
| ConfigMap `<chat_template_configmap>` | llm-serving |
| Job `download-<model_name>` (per model) | llm-serving |
| LLMInferenceService `<model_name>` (per model) | llm-serving |
| MaaSModelRef `<model_name>` (per model) | llm-serving |

**処理順序**: `state: absent` のモデルを先に削除（GPU 解放）→ `state: present` のモデルをデプロイ
**Wait**: Download Job 完了, LLMInferenceService Ready (最大 20 分、モデルごと)
**使用変数**: `models`, `model_defaults`, `llm_storage_initializer_image`, `hf_token`, `storage_class`

---

## 7. デプロイ実行

```bash
# フルデプロイ
uv run ansible-playbook site.yml -i inventory/myenv

# 特定フェーズのみ実行
uv run ansible-playbook site.yml -i inventory/myenv --tags platform
uv run ansible-playbook site.yml -i inventory/myenv --tags workload

# コンポーネントスキップ
uv run ansible-playbook site.yml -i inventory/myenv -e install_ogx=false -e install_guardrails=false

# 使用可能なタグ一覧
# always, infra, lvm, metallb, deps, cert_manager, servicemesh, nfd, gpu,
# rhcl, kueue, jobset, leaderworkerset, storage, odf, platform, rhoai,
# keycloak, integration, keycloak_oauth, keycloak_maas, rhoai_oidc,
# workload, llm, maas, mlflow, ogx, guardrails, observability, post_deploy, opencode
```

### Operator インストールの共通パターン

全 Operator は `roles/common/tasks/install_operator.yml` を使用して以下の手順でインストールされます：

1. Namespace 作成
2. OperatorGroup 作成（存在しない場合のみ）
3. Subscription 作成
4. InstalledCSV の出現を待機（最大 10 分）
5. CSV の `Succeeded` を待機（最大 10 分）

---

## 8. 検証

```bash
uv run ansible-playbook playbooks/verify.yml -i inventory/myenv
```

### チェック項目

| カテゴリ | チェック内容 |
|---|---|
| Infrastructure | LVM Operator CSV Succeeded, LVMCluster Ready, LVMCluster VolumeGroupsReady, MetalLB speaker Running |
| Dependencies | 8 Operator (cert-manager, ServiceMesh, NFD, GPU, RHCL, Kueue, JobSet, LeaderWorkerSet) の CSV Succeeded |
| Storage | NooBaa Ready |
| Platform | RHOAI Operator CSV, DataScienceCluster Ready, MaaS Gateway Programmed, RHOAI Dashboard Ready, Keycloak Ready |
| Integration | Keycloak ユーザー/グループ割り当て, OAuth IdP 登録, AITenant 存在 |
| Workloads | LLMInferenceService Ready, MaaS API healthy, MaaSModelRef 存在, MaaS Subscription Active, **API Key 発行テスト**, MLflow Available, OGX Ready, Guardrails Ready |
| Health | Route HostAlreadyClaimed 検出, 管理対象 namespace の異常 Pod 検出 |
| Namespaces | 全管理対象 namespace の存在確認 |

verify 完了時に環境サマリーが表示されます（Console URL, Dashboard URL, Keycloak 管理者情報, MaaS エンドポイント, ユーザーパスワード等）。

### デプロイ後のアクセス情報

#### サービス URL

デプロイ後、各サービスには `https://<service>.<cluster_domain>` でアクセスできます。

| サービス | URL 形式 | 用途 |
|---|---|---|
| OpenShift Console | `https://console-openshift-console.apps.<cluster_domain>` | クラスター管理 |
| RHOAI Dashboard | `https://rhods-dashboard-redhat-ods-applications.apps.<cluster_domain>` | AI プラットフォーム管理 |
| Keycloak 管理コンソール | `https://keycloak-keycloak.apps.<cluster_domain>` | IdP 管理 |
| MaaS Gateway | `https://maas.apps.<cluster_domain>` | LLM 推論 API |
| MLflow | `https://rh-ai.apps.<cluster_domain>/mlflow` | 実験管理 |

URL を確認するコマンド:

```bash
# 全 Route の一覧
oc get route --all-namespaces -o custom-columns='SERVICE:.metadata.name,URL:.spec.host'

# 個別確認
oc get route console -n openshift-console -o jsonpath='https://{.spec.host}'
oc get route rhods-dashboard -n redhat-ods-applications -o jsonpath='https://{.spec.host}'
oc get route keycloak -n keycloak -o jsonpath='https://{.spec.host}'
oc get route maas-gateway -n openshift-ingress -o jsonpath='https://{.spec.host}'
```

#### アカウント情報

| アカウント | 場所 | 説明 |
|---|---|---|
| OpenShift cluster-admin | `oc login` 時に指定 | クラスター管理者。`oc whoami` で確認 |
| Keycloak 管理者 | Secret `keycloak-initial-admin` (namespace: keycloak) | Keycloak 管理コンソールのログイン |
| MaaS ユーザー (admin1, testuser1 等) | `.credentials/<username>.password` | MaaS Dashboard / API Key 発行用 |
| MaaS API Key | `.vllm-token` | LLM 推論リクエストの Bearer トークン |

認証情報を確認するコマンド:

```bash
# Keycloak 管理者
oc get secret keycloak-initial-admin -n keycloak \
  -o jsonpath='username: {.data.username} / password: {.data.password}' | \
  xargs -I{} sh -c 'echo {} | sed "s/username: //" | cut -d/ -f1 | base64 -d; echo -n " / "; echo {} | sed "s/.*password: //" | base64 -d; echo'

# MaaS ユーザーパスワード
cat .credentials/admin1.password
cat .credentials/testuser1.password

# MaaS API Key
cat .vllm-token
```

#### 認証情報のファイル配置

```
rhoai-3.5-ansible/
├── .credentials/              ← Keycloak ユーザーのパスワード (gitignore 対象)
│   ├── admin1.password
│   └── testuser1.password
├── .vllm-token                ← MaaS API Key (gitignore 対象)
├── .generated-passwords.yml   ← DB パスワード (gitignore 対象)
```

> **注意**: これらのファイルは `.gitignore` 対象です。cleanup → 再デプロイすると API Key は無効になりますが、`.credentials/` のパスワードファイルが残っていれば同じパスワードで再作成されます。

---

## 9. 顧客環境で必ず変更すべき設定

以下は環境ごとに**必ず確認・変更が必要**なパラメーターです。

### 必須変更

| パラメーター | ファイル | 説明 |
|---|---|---|
| `lvm_device_paths` | components.yml | LVM に使用するデバイスパス。上記のデバイス確認コマンドで特定 |
| `hf_token` | vault.yml | HuggingFace トークン。モデルダウンロードに必要 |

### 環境に応じて変更

| パラメーター | ファイル | 条件 |
|---|---|---|
| `k8s_kubeconfig` | cluster.yml | `KUBECONFIG` 環境変数が未設定の場合 |
| `storage_class` | cluster.yml | LVM 以外のストレージを使う場合（例: `gp3-csi`） |
| `metallb_ip_range` | components.yml | 自動計算が不適切な場合 |
| `cluster_proxy.*` | cluster.yml | Proxy 環境の場合 |
| `operator_channels.lvm` | cluster.yml | OpenShift バージョンが 4.22 以外の場合 |
| `operator_channels.odf` | cluster.yml | 同上 |
| `install_gpu_operator` | components.yml | GPU なし環境の場合 (`false` に) |
| `keycloak_users` | components.yml | 実環境のユーザーに変更 |
| `models` | components.yml | 異なるモデルを使う場合（hf_repo, state 等を定義） |

### 変更不要（自動処理）

| パラメーター | 理由 |
|---|---|
| `maas_db_password` / `ogx_db_password` / `keycloak_db_password` | 空にしておけば自動生成 |
| 依存 Operator のフラグ | `install_rhoai: true` で自動有効化 |
| `install_lvm` | `storage_class: lvms-*` で自動有効化 |

---

## 10. Disconnected 環境でのデプロイ

### 概要

Disconnected（インターネット接続なし）環境では以下の事前準備が必要です：

1. **Python パッケージ**: `vendor/wheels/` に事前ダウンロード
2. **Ansible Galaxy collections**: `vendor/collections/` に事前ダウンロード
3. **Operator カタログ**: OpenShift の OperatorHub をミラーリング
4. **コンテナイメージ**: vLLM 等のイメージをミラーレジストリに配置
5. **HuggingFace モデル**: モデルファイルを事前にダウンロード

### Python / Ansible のオフラインインストール

```bash
# オンライン環境で実行
cd ansible
bash scripts/download-deps.sh

# Disconnected 環境で実行
cd ansible
bash scripts/setup-env.sh
# 以降は uv run でコマンド実行（activate 不要）
uv run ansible-playbook site.yml -i inventory/myenv
```

### HuggingFace モデルの事前ダウンロード

Disconnected 環境では HuggingFace からのダウンロードができないため、モデルを事前にダウンロードして PVC にコピーする必要があります。

```bash
# オンライン環境で
pip install huggingface_hub
huggingface-cli download Qwen/Qwen3-0.6B --local-dir ./qwen3-06b

# Disconnected 環境でモデルを PVC にコピー
oc rsync ./qwen3-06b/ <pod-name>:/models/qwen3-06b/ -n llm-serving
```

### Operator カタログのミラーリング

`oc-mirror` を使用して必要な Operator をミラーリングしてください。必要な Operator 一覧は「6.1 Role 一覧と実行順序」を参照。

### コンテナイメージ

以下のイメージがミラーレジストリに必要です：

- `vllm/vllm-openai:v0.28.0` (LLM Serving)
- `quay.io/modh/kserve-storage-initializer:rhoai-2.22` (KServe)
- `registry.access.redhat.com/ubi9/python-311:latest` (モデルダウンロード Job)
- `registry.access.redhat.com/ubi9/ubi-minimal:latest` (PVC チェック)

---

## 11. ディレクトリ構成

```
ansible/
├── site.yml                      # メイン Playbook
├── ansible.cfg                   # Ansible 設定（collections_paths, default inventory 等）
├── pyproject.toml                # Python 依存定義 (uv 対応, find-links = vendor/wheels)
├── requirements.yml              # Ansible Galaxy collections
├── inventory/
│   ├── sample/                   # テンプレート（コピーして使う）
│   │   └── group_vars/all/
│   │       ├── cluster.yml.sample
│   │       ├── components.yml.sample
│   │       └── vault.yml.sample
│   └── myenv/                    # 環境固有の設定 (.gitignore 対象)
├── playbooks/
│   ├── verify.yml                # デプロイ検証 + 環境サマリー
│   ├── manage_maas_access.yml    # MaaS アクセス管理 (ユーザー + ポリシー)
│   ├── llm_add_model.yml         # モデル追加 (deploy + MaaS + Keycloak)
│   └── maas_create_apikey.yml    # API Key 発行
├── scripts/
│   ├── cleanup-all.sh            # 全リソース削除
│   ├── setup-opencode.sh         # OpenCode 設定生成 (API Key + opencode.json)
│   ├── download-deps.sh          # Disconnected 用資材ダウンロード
│   └── setup-env.sh              # Disconnected 環境セットアップ
├── tasks/
│   ├── resolve_dependencies.yml  # 依存関係自動解決
│   └── _resolve_one.yml
├── roles/
│   ├── preflight/                # 事前チェック + ファクト収集
│   │   └── tasks/
│   │       ├── main.yml          # フル preflight
│   │       └── light.yml         # 軽量 preflight（運用 Playbook 用）
│   ├── common/                   # Operator install/wait 共通タスク
│   │   └── tasks/
│   │       ├── install_operator.yml
│   │       ├── create_if_absent.yml
│   │       ├── wait_for_crd.yml
│   │       ├── wait_for_field.yml
│   │       └── wait_for_resource.yml
│   ├── lvm/                      # LVM Operator
│   ├── metallb/                  # MetalLB
│   ├── cert_manager/             # cert-manager
│   ├── servicemesh/              # ServiceMesh 3
│   ├── nfd/                      # Node Feature Discovery
│   ├── gpu_operator/             # GPU Operator
│   ├── rhcl/                     # RHCL (Kuadrant/Authorino)
│   ├── kueue/                    # Kueue
│   ├── jobset/                   # JobSet
│   ├── leaderworkerset/          # LeaderWorkerSet
│   ├── odf/                      # ODF (NooBaa)
│   ├── rhoai/                    # OpenShift AI
│   ├── keycloak/                 # Keycloak + Group Sync
│   ├── integration_keycloak_oauth/   # Keycloak → OpenShift OAuth
│   ├── integration_keycloak_maas/    # Keycloak → MaaS ポリシー
│   ├── integration_rhoai_oidc/       # AITenant OIDC
│   ├── llm_serving/              # LLM モデルデプロイ
│   ├── maas_resources/           # MaaS Dashboard RBAC
│   ├── mlflow/                   # MLflow
│   ├── ogx/                      # OGX Server
│   ├── guardrails/               # NeMo Guardrails
│   └── observability/            # Monitoring / Dashboards
├── docs/
│   ├── operations.md             # 運用ガイド（モデル追加・ユーザー管理等）
│   └── troubleshooting.md        # トラブルシューティング
├── vendor/                       # Disconnected 用資材 (.gitignore 対象)
│   ├── wheels/                   # Python wheels
│   └── collections/              # Ansible Galaxy collections
└── .venv/                        # Python 仮想環境 (.gitignore 対象)
```

---

## 12. 関連ドキュメント

| ドキュメント | 内容 |
|---|---|
| [docs/operations.md](docs/operations.md) | 運用ガイド — モデル追加手順、ユーザー/グループ管理、MaaS アクセス制御、API Key 発行、クォータ設定、削除手順 |
| [docs/troubleshooting.md](docs/troubleshooting.md) | トラブルシューティング — 過去のトラブル事例、デプロイ前チェックリスト、監視方法、失敗時の対処フロー、各リソースの正常状態確認コマンド集、Disconnected 環境固有の注意点 |
