# RHOAI Ansible

OpenShift AI (RHOAI) 3.5 の自動デプロイ/アンインストール用 Ansible playbook。

## プロジェクト構成

```
rhoai-3.5-ansible/
├── site.yml                  # メインエントリポイント (localhost, connection: local)
├── playbooks/                # 個別操作用 playbook (verify, uninstall 等)
├── roles/                    # 機能単位の role
│   ├── common/tasks/         # 共通タスク (install_operator, wait_for_*, create_if_absent)
│   ├── preflight/            # 入力バリデーション
│   └── <role>/               # 各コンポーネント
├── inventory/
│   ├── sample/group_vars/all/  # サンプル設定 (.sample 拡張子)
│   └── <env>/group_vars/all/   # 環境別設定
├── render-manifests/         # manifest レンダリングツール (後述)
├── pack-playbook.sh          # tar.gz パック
└── unpack-playbook.py        # アンパック + venv 構築
```

## コマンド

```bash
# Ansible 実行 (通常の方法)
cd rhoai-3.5-ansible
uv run ansible-playbook site.yml -i inventory/<env>

# 特定 role のみ
uv run ansible-playbook site.yml -i inventory/<env> --tags infra,lvm

# manifest レンダリング (Ansible が使えない環境向け)
python3 render-manifests/render.py -c inventory/<env> -o output/
```

## 設定ファイル (inventory/<env>/group_vars/all/)

| ファイル | 内容 |
|---------|------|
| `cluster.yml` | クラスター共通設定 (storage_class, operator_channels, catalog_sources) |
| `components.yml` | Role 有効/無効フラグ (install_*), モデル定義 (models), Keycloak 設定 |
| `vault.yml` | シークレット (hf_token 等)。ansible-vault で暗号化推奨 |

## Manifest レンダリングモード (`render-manifests/`)

Ansible 実行環境が構築できない場合（socks5h proxy 非対応、bastion 権限なし等）に使用する代替手段。

### 仕組み

`render.py` は Ansible playbook のロジックを Python で再実装し、以下を生成する:

1. **`output/manifests/`** — role 単位の YAML manifest ファイル (番号付きサブディレクトリ)
2. **`output/manual-deploy-guide.md`** — ステップバイステップの手動適用手順書
3. **`output/generated-passwords.yml`** — 自動生成されたパスワード

### 使い方

```bash
python3 render-manifests/render.py -c inventory/<env> -o output/
# → output/ 以下に manifests/ と manual-deploy-guide.md が生成される
# → 手順書に沿って oc コマンドで順次適用
```

### 依存関係

- Python 3.9+
- PyYAML (`pip install pyyaml`)

### vault.yml の扱い

render.py は `yaml.safe_load` で設定ファイルを読むため、ansible-vault で暗号化された vault.yml はそのまま読めない。
事前に `ansible-vault decrypt vault.yml` で平文に戻すか、平文のコピーを配置してから実行する。
出力ディレクトリは 0700、秘密情報を含むファイルは 0600 で作成される。

### メンテナンスガイド (生成 AI 向け)

**render.py と Ansible role の対応関係:**

render.py の各 `render_*()` メソッドは、対応する Ansible role の `tasks/main.yml` を忠実に再現している。
role を変更した場合、以下の手順で render.py も更新する:

1. 変更した role の `tasks/main.yml` を読み、k8s module に渡している definition を確認
2. `render.py` の対応する `render_*()` メソッドの manifest dict を更新
3. oc コマンドや待機手順が変わった場合、`self._step()` の手順書テキストも更新
4. テスト実行で生成物を検査する（下記「変更後の検証手順」参照）

**変更後の検証手順:**

role を変更した後は、必ず以下のコマンドで生成物を検査する:

```bash
python3 render-manifests/render.py -c inventory/sample -o /tmp/test-output
# 確認項目:
# 1. エラーなく完了すること
# 2. 変更した role に対応する manifest が正しく生成されていること
# 3. 手順書 (manual-deploy-guide.md) の該当ステップが正しいこと
# 4. Subscription の spec.source が catalog_sources の設定と一致すること
# 5. 秘密情報を含むファイルが 0600 であること
```

**特に注意が必要なパターン:**

- **動的値** (`<CLUSTER_DOMAIN>`, `<CERT_NAME>`): manifest に placeholder として埋め込み、手順書で `sed` 置換する
- **Proxy 設定**: inventory に proxy URL を置かず、手順書では `oc get proxy cluster` の `.status.httpProxy`、`.status.httpsProxy`、`.status.noProxy` から実効値を取得する。download Job manifest には `oc set env --local` で反映する
- **Keycloak API 操作**: manifest では表現できないため、手順書に curl コマンドとして記載。ユーザー作成、グループ割当、MaasTenantConfig 適用を含む
- **OBC credentials** (mlflow, ogx): OBC binding 後に動的取得するため、手順書にインラインで `oc get` → manifest 生成の手順を記載
- **条件分岐** (proxy 設定等): 手順書のシェルスクリプトで `if` 文として記載
- **待機処理**: `oc wait` や `while` ループとして手順書に変換
- **CatalogSource**: `render_operator()` 呼び出し時に必ず `source=self.catalogs[...]` を渡す。disconnected 環境ではミラーカタログ名に変わるため
- **モデル state: absent**: 削除手順を手順書に含める（スキップしない）
- **chat_template_configmap**: モデルごとに異なる名前の可能性がある

**新しい role を追加した場合:**

1. `render_<role名>()` メソッドを追加
2. `render_all()` の適切な位置（site.yml の role 順序に合わせる）で呼び出し
3. manifest の番号プレフィックスを site.yml の実行順序に合わせる
4. role 内の `tasks/main.yml` だけでなく、`templates/` や `manage_*.yml` 等の追加ファイルも確認する

**role の when 条件 (install_* フラグ):**

render.py では `render_all()` 内で `self.cfg.get("install_*")` チェックとして実装済み。
新しい install フラグを追加した場合は components.yml.sample と render_all() の両方を更新する。

## Role 一覧と依存関係

site.yml の実行順序:

1. **Infrastructure**: lvm → metallb
2. **Dependencies**: cert_manager → servicemesh → nfd → gpu_operator → rhcl → kueue → jobset → leaderworkerset
3. **Storage**: odf
4. **Platform**: rhoai → keycloak
5. **Integration**: keycloak_oauth → keycloak_maas → rhoai_oidc
6. **Workloads**: maas_resources → llm_serving → mlflow → ogx → guardrails → observability

## 共通パターン

### Operator インストール (common/tasks/install_operator.yml)
Namespace → OperatorGroup → Subscription → CSV Succeeded 待機

### リソース作成 (common/tasks/create_if_absent.yml)
k8s_info で存在確認 → 存在しなければ k8s で作成 (immutable リソース用)
