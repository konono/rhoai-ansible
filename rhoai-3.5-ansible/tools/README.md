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

### test-ogx.sh

OGX Server (Llama Stack) の機能テスト。`verify.yml` は OGXServer CR が Ready か
しか見ないため、「Ready なのに API が動かない」「vLLM に繋がっておらずモデルが
0 件」は検出できません。実際に API を叩いて確認します。

health / Providers / Models / Chat Completion / Conversations の作成・削除 /
Files のアップロード・削除を確認し、作ったテストデータはその場で削除します。

```bash
./tools/test-ogx.sh                 # モデルは OGX の登録済みから自動選択
./tools/test-ogx.sh qwen3-5-9b
```

Models が 0 件の場合は vLLM 接続 (`ogx-vllm-connection` Secret) を疑ってください。

### test-guardrails.sh

NeMo Guardrails の機能テスト。`verify.yml` は NemoGuardrails CR が Ready か
しか見ないため、**「Ready なのに rail が効いておらず有害なプロンプトが素通りする」**
は検出できません。実際にブロックされるかを確認します。

```bash
./tools/test-guardrails.sh                 # モデルは自動検出
./tools/test-guardrails.sh qwen3-5-9b

# rail のパイプラインは複数回 LLM を呼ぶため遅い。必要なら延ばす
TIMEOUT=600 ./tools/test-guardrails.sh

# ブロック判定の文言が環境と違う場合
BLOCK_PATTERN='cannot help|declined' ./tools/test-guardrails.sh
```

ブロックされなかった場合は**失敗**として報告します（rail が効いているかを確認する
テストのため）。判定文言が環境と違うだけの場合は `BLOCK_PATTERN` で調整してください。

### test-maas-routing.sh

MaaS Gateway の 2 通りのルーティングを個別に試します。RHOAI 3.5 では
body-based が推奨ですが、従来の path-based も残っているため両方を確認できます。

| 方式 | エンドポイント | ボディの `model` |
|---|---|---|
| body-based（推奨） | `POST /v1/chat/completions` | `publishers/<namespace>/models/<model>` |
| path-based（従来） | `POST /<namespace>/<model>/v1/chat/completions` | `<model>` |

**body-based では完全修飾名が必要です。** 短縮名だと 404 になります。
`X-Gateway-Model-Name` を手で付けてもボディの内容が優先されるため、
ヘッダでの回避はできません。

```bash
# 両方試す（モデル未指定なら Ready なものを自動で選ぶ）
./tools/test-maas-routing.sh "$(cat .vllm-token)"

# モデルと namespace を指定
./tools/test-maas-routing.sh maas-xxxx qwen3-5-9b llm-serving "日本の首都は？"

# 片方だけ
ROUTES=body ./tools/test-maas-routing.sh maas-xxxx
ROUTES=path ./tools/test-maas-routing.sh maas-xxxx
```

外部 DNS が未設定でも動きます。Gateway Service の ClusterIP を引いて
`curl --resolve` で解決させるため、TLS の SNI もルーティングも本番と同じ経路を通ります。
クラスタ外から名前解決できる環境では `NO_RESOLVE=1` を指定してください。

Ansible でまとめて検証する場合は `playbooks/llm_verify_inference.yml` を
`-e inference_via=maas` で実行すると、同じ 2 経路を検証して失敗時に落とします。

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
