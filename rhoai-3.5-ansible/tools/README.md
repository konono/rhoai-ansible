# tools/ — 手動検証スクリプト

`playbooks/verify.yml` で自動検証している内容を、個別に手動で確認するためのシェルスクリプト集です。

## 前提条件

- `oc login` 済みであること
- `python3` が利用可能であること
- `curl` が利用可能であること

## スクリプト一覧

### check-platform-health.sh

プラットフォーム全体のヘルスチェック。Operator CSV、LVM/MetalLB/NooBaa/RHOAI/Keycloak/MLflow の状態、Route 競合、異常 Pod をまとめて確認します。

```bash
./tools/check-platform-health.sh
```

### test-maas-inference.sh

MaaS Gateway 経由で LLM の推論をテストします。API Key を渡してモデル一覧の取得と chat/completions の実行を行います。

```bash
# API Key を指定して実行（モデルは自動選択）
./tools/test-maas-inference.sh $(cat .vllm-token)

# モデルとプロンプトを指定
./tools/test-maas-inference.sh $(cat .vllm-token) my-model "日本の首都は？"
```

### test-maas-apikey.sh

MaaS API Key のライフサイクル（発行 → 一覧確認 → Gateway 認証 → 削除）をテストします。

```bash
# デフォルト（admin1 / maas-admins）
./tools/test-maas-apikey.sh

# ユーザーとグループを指定
./tools/test-maas-apikey.sh user1 maas-users
```

### test-mlflow.sh

MLflow に Experiment / Run / Metrics / Params を書き込み、読み戻して動作確認します。テストデータは自動で削除されます。

```bash
# MLflow URL を自動取得
./tools/test-mlflow.sh

# URL を指定
./tools/test-mlflow.sh https://mlflow.example.com
```

### check-keycloak-users.sh

Keycloak のユーザー一覧、グループ割当、OpenShift OAuth IdP の登録状態を確認します。

```bash
# デフォルト（realm=maas, namespace=keycloak）
./tools/check-keycloak-users.sh

# realm と namespace を指定
./tools/check-keycloak-users.sh maas keycloak
```

### check-llm-serving.sh

LLMInferenceService / MaaSModelRef / MaaSSubscription / AITenant の状態と GPU 割当を確認します。

```bash
# デフォルト（namespace=llm-serving）
./tools/check-llm-serving.sh

# namespace を指定
./tools/check-llm-serving.sh my-namespace
```

### cluster-status.sh

クラスタの環境サマリーを表示します。Dashboard URL、Keycloak 認証情報、ユーザーアカウント、推論エンドポイント・API Key、MLflow API 情報をまとめて確認できます。Playbook の実行結果に依存せず、いつでも単独で実行できます。

```bash
# 全情報を表示（パスワード・トークン含む）
./tools/cluster-status.sh

# パスワード・トークンを非表示
./tools/cluster-status.sh --no-secrets
```

## pack / unpack ツール

リポジトリルートの `pack-playbook.sh` / `unpack-playbook.sh` で、git が使えない顧客環境への差分適用ができます。

```bash
# 開発側: tarball を作成
bash pack-playbook.sh rhoai-3.5-ansible

# 顧客環境: 差分を確認（tarball から）
python3 unpack-playbook.py --diff-only tarball.tar.gz

# 顧客環境: 展開済みディレクトリ同士の比較
python3 unpack-playbook.py --diff-only /path/to/new/rhoai-3.5-ansible

# 顧客環境: 比較先を指定
python3 unpack-playbook.py --diff-only -d /path/to/existing tarball.tar.gz

# 顧客環境: 差分を確認して適用
python3 unpack-playbook.py tarball.tar.gz
```

## verify.yml との対応

| verify.yml タグ | 対応スクリプト |
|---|---|
| `infra` (LVM, MetalLB) | `check-platform-health.sh` |
| `deps` (Operator CSV) | `check-platform-health.sh` |
| `storage` (NooBaa) | `check-platform-health.sh` |
| `platform` (RHOAI, Keycloak) | `check-platform-health.sh` |
| `health` (Pod, Route, LVM VG) | `check-platform-health.sh` |
| `workload/maas` (API Key, health) | `test-maas-apikey.sh`, `test-maas-inference.sh` |
| `workload/mlflow` | `test-mlflow.sh` |
| `workload/llm` | `check-llm-serving.sh` |
| `integration/keycloak_users` | `check-keycloak-users.sh` |
| `integration/keycloak_oauth` | `check-keycloak-users.sh` |
| (環境サマリー全体) | `cluster-status.sh` |
