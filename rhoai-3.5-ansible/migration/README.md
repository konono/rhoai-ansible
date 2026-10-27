# 手動構築環境からの移行

このディレクトリで収集、診断、inventory の生成、標準 playbook による移行を行います。
目的は**既存環境を最終的に標準 role の構成へ統一すること**です。アプリの停止・再作成は許容しますが、
CSI、StorageClass、PV、ストレージ Operator を導入・更新・削除することはしません。
既存 PVC のデータを引き継ぎ、新しいアプリ用 PVC が必要な場合のみ、既存 CSI に払い出しを依頼します。

これは任意の構成を無条件で移せる変換器ではありません。収集値から **draft inventory** を作り、
対応できない構成や確認が必要な値を blocker として出します。お客様固有の設定はこの診断を使って
role・renderer・テストを調整します。現地の生成 AI は [専用 skill](skills/rhoai-migration/SKILL.md) を参照してください。

## 実装の範囲

|入口|クラスタ操作|結果|
|---|---|---|
|`collect.yml`|指定 kubeconfig に対する読み取りのみ|baseline、draft inventory、初期 insights|
|`generate.yml`|なし|取得済み baseline から別ディレクトリへ draft 再生成|
|`plan.yml`|なし|実 renderer による候補生成、差分・危険経路・未解決項目の診断|
|`apply.yml`|再収集後、標準 `../site.yml` を実行|標準 role による移行、その後 `verify.yml`|

既存 spec をそのまま再 apply する独立インストーラではありません。apply は標準 role を実際に使います。
分析は Ansible のローカル filter plugin、API 収集と実行制御は Ansible playbook です。

**現時点で未対応の構成は止めます。** MLflow/OGX の既存 role は NooBaa の OBC/BackingStore を作るため、
移行モードでは有効化できません。CSI はブロック／ファイルストレージであり S3 の代わりではありません。
既存 S3 endpoint、bucket、認証情報、CA を消費する adapter を role と renderer の両方に実装してから、
`S3_ADAPTER_REQUIRED` の判定と `site_guard.yml` をその実装に合わせて更新してください。
blocker を消すためだけにフラグを false にすると、そのコンポーネントは移行対象から外れます。
**全体移行が完了したという意味にはなりません。**

## 持ち込み前の準備

リポジトリ全体または playbook 配布物を持ち込みます。`migration/` 単体では標準 role / renderer が足りません。
既存の `setup-env.sh` 等で Ansible、kubernetes.core、PyYAML と Kubernetes Python client を用意します。
`uv` が無い bastion では準備済み venv の `ansible-playbook` / `python` で実行できます。

```bash
cd rhoai-3.5-ansible/migration
uv run ansible-playbook collect.yml --syntax-check
uv run python -m unittest discover -s tests -v
```

このディレクトリの `ansible.cfg` は専用 localhost inventory、標準 role と移行 role、filter plugin を参照します。
別の `ANSIBLE_CONFIG` が設定されている場合は `ANSIBLE_CONFIG="$PWD/ansible.cfg"` を明示してください。
標準インストールの inventory や secrets を暗黙に流用しません。

## 1. 収集

新規ディレクトリを指定します。同じ名前に上書きはしません。

```bash
uv run ansible-playbook collect.yml \
  -e migration_kubeconfig=/absolute/path/to/customer-kubeconfig \
  -e migration_output_dir="$PWD/output/customer-001"
```

収集対象は `roles/migration/vars/main.yml` に列挙しています。Subscription、主要 RHOAI/GPU/Keycloak/LLM/MaaS CR、
Deployment/StatefulSet、PVC、StorageClass 等を読みます。Secret API、PVC の内容、Keycloak の管理 API は読みません。
アクセス拒否・接続失敗は「リソースが無い」と扱わず停止します。未登録の API は収集結果に記録します。
旧バージョンしか served していない CR は見落とし得るため、API discovery と role 対応表を現地で確認してください。
見つからない API は `API_NOT_DISCOVERED` で停止します。別バージョンに対象が存在する場合は収集側を対応させます。
本当に未導入と確認した API だけ `migration_absent_apis` に `apiVersion/Kind` の形式で記録できます。

出力はディレクトリ0700・ファイル0600です。**Secret オブジェクトを読まなくても、CR や Pod template の
inline env 等に秘密値が入っている場合があります。baseline と inventory は機密情報です。**
AI の会話、チケット、PR にそのまま添付しないでください。report は値を出さず、リソース名とフィールドパスを示します。
`output/` は gitignore と pack 除外対象です。任意の別ディレクトリを使う場合、その配布・Git 除外も確認します。

```text
output/customer-001/
  baseline.yml                 # 観測した構成。手で書き換えない
  insights.yml                 # 初期の不足情報／危険経路
  inventory/
    hosts.yml
    group_vars/all/components.yml  # 収集値を使う、未完成の標準 inventory
  plan.yml                     # plan 実行後
  analysis-*/                  # 診断専用 renderer 出力。手順書を実行しない
  before-apply.yml              # apply 直前の再収集結果
```

既存 baseline だけを受け取ってローカルで作業する場合:

```bash
uv run ansible-playbook generate.yml \
  -e migration_snapshot_file=/absolute/path/to/baseline.yml \
  -e migration_kubeconfig=/absolute/path/to/customer-kubeconfig \
  -e migration_output_dir="$PWD/output/customer-draft-002"
```

## 2. inventory と insights の調整

自動で採用するもの: 重複しない Operator channel/catalog、単一に決められる StorageClass、
モデル名・namespace・image・args・GPU resource/count・replicas・nodeSelector・CPU/memory、Keycloak namespace。
モデル名は変更しません。PVC の URI だけから HuggingFace repository は特定できないので空のままにします。

必ず判断するもの:

- `storage_class`: 既存 CSI の SC。複数候補なら空で停止。StorageClass の新設や default 切替はしない。
- `models.*.hf_repo`: 実際のモデル取得元。独自取得元や異なる PVC/path は adapter が必要。
- `models.*.purge`: false。データ削除は移行範囲外。
- `migration_credentials_verified`: DB／OIDC／group-sync の既存資格情報を確認してから true。
  `vault.yml` に既存値を安全に引き継ぐ。移行時は repository の `.generated-passwords.yml` を読み込まず、
  新しい DB パスワードの自動生成・同ファイルへの保存もしない。
  Keycloak 使用時は `oidc_client_secret` / `maas_oidc_client_secret` / `group_sync_password` も必須です。
- `migration_identity_verified`: realm、client、user、group、OAuth の引継ぎを確認してから true。
  default admin/testuser を勝手に生成しない。既存 Keycloak API を利用した追加収集は現地の許可範囲で実装する。
- `migration_runtime_reviewed`: render 差分以外の command/API/prune/recreate を対象 role の実コードで確認してから true。
- `migration_backup_reference`: お客様側のデータ／DB 復旧手順・バックアップの参照先。
  このツールが snapshot を作ったりデータを復元したりするわけではない。
- `migration_new_pvcs` / `migration_new_statefulsets`: 意図的に新規作成する ID のリスト。
  例 `PersistentVolumeClaim/new-app/data`。既存データを新しい空 PVC に置き換えるために指定してはいけない。

`cluster.yml`、`components.yml`、`vault.yml` が計画と実行の入力です。追加 group_vars ファイルや `-e` で
role の設定を上書きせず、この3ファイルへ反映します。生成値は絶対パスを含むため、別 bastion へ移したら
`ansible_root` / kubeconfig / baseline パスも更新して plan を作り直します。
renderer は暗号化 vault を直接読めません。解析用の private bundle 内だけで復号したコピーを使い、
元の暗号化ファイルを残してください。`analysis-*` は秘密値を含み得る診断資材です。
初期生成では全設定を `components.yml` にまとめます。読込優先順位は `cluster.yml` < `components.yml` <
`vault.yml` です。後から cluster 側へ移す値は components 側からも削除し、二重定義を避けてください。

## 3. 計画

```bash
uv run ansible-playbook plan.yml -e migration_bundle="$PWD/output/customer-001"
```

plan の `blockers` が1件でもあれば apply は止まります。`warnings` と `changes` は停止時間・設定変更のレビュー材料です。
`potential-update` は target フィールドの構造比較であり、SSA / strategic merge / admission / controller の挙動を
完全に再現するものではありません。未収集 kind は `create-or-unobserved` と表示し、作成確定とは扱いません。
`RUNTIME_PLACEHOLDER` はドメイン・証明書等の実行時取得値が未解決の比較です。表示された差分がそのまま
適用されるという意味ではありません。対応する標準 role の取得・組立経路を確認してください。
render 失敗時は部分 manifest を承認に使わず `RENDER_FAILED` を記録します。

主な診断:

|コード|対応|
|---|---|
|`STORAGE_OWNERSHIP` / `STORAGE_MUTATION`|ストレージ管理 role / resource が残っている。消費だけに分離する|
|`CSI_SELECTION`|既存 SC を特定。CSI の導入・SC 修正で解決しない|
|`PVC_IMMUTABLE` / `PVC_SIZE_REVIEW`|既存 PVC の class/accessMode/size と不一致。既存 claim の再利用へ修正|
|`PVC_MAPPING_REQUIRED` / `STATEFUL_IDENTITY` / `STATEFUL_CLAIMS`|既存 DB・PVC 名と標準名の関係を確認。空 DB の新規作成をデータ移行と誤認しない|
|`S3_ADAPTER_REQUIRED`|MLflow/OGX の外部 S3 adapter が必要。CSI だけでは解消しない|
|`CREDENTIAL_HANDOFF` / `IDENTITY_HANDOFF`|既存認証・所属を収集して引継ぎ。確認フラグだけを変更しない|
|`MODEL_STORAGE_MAPPING` / `MODEL_SOURCE_UNKNOWN`|既存モデルのデータ場所・取得元を mapping する|
|`MODEL_RECREATE` / `MODEL_REMOVAL`|停止を計画。role の GPU 判定と削除対象を確認|
|`RHOAI_VERSION_PATH`|既存版から3.5への対応 upgrade path を別途確認。channel を書き換えるだけにしない|
|`IDENTITY_MISMATCH`|名前／namespace の異なる singleton。競合 CR を新設しない|
|`NON_MANIFEST_ACTIONS`|API、Secret、prune、条件付き削除を source role で追跡|

同じ baseline に対して target を何度調整しても構いません。plan はコードと inventory の hash を持つため、
role・script・inventory を修正したら必ず再生成します。source の spec / UID / installedCSV が変わったら再収集します。
新しい baseline から draft を生成し直した場合、既にレビューした target 設定を diff して引き継ぎます。

## 4. 適用（お客様環境での実施工程）

停止枠と復旧手順を確認し、blocker が0件になった plan の `plan_digest` を指定します。
今回の開発ではこの実機工程は実施していません。

```bash
uv run ansible-playbook apply.yml \
  -e migration_bundle="$PWD/output/customer-001" \
  -e migration_approved_digest=PLAN_DIGEST_FROM_REVIEWED_REPORT
```

apply は承認 hash、inventory、実装、クラスタ UID を確認し、読み取りで取得した現在の spec を baseline と照合します。
差分があれば停止。合致した場合にだけ標準 `site.yml` と `verify.yml` を実行します。
生成 inventory を直接 `site.yml` に渡すと、Secret 生成などの副作用より前に migration guard で止まります。
`--tags` / `--skip-tags` / `--check` による apply 部分実行は許可しません。

Kubernetes 全体のトランザクションではありません。再確認から role 実行までの競合、途中失敗、Operator 自身の動作を
完全にはロックできません。同時変更を止めた停止枠で実行し、失敗したらその時点の状態を再収集・再計画します。
`uninstall.yml` はロールバックに使いません。移行 profile での uninstall は PVC 削除の危険があるため拒否します。
**再実行は「前の plan のまま無条件に再開」ではありません。**

## 完了条件とトラブルシュート

- 標準 role で収束、verify 成功、直接推論・MaaS body/path・Guardrails・監視等の実機能を確認。
- 前後で既存 PVC UID / volumeName / SC、StorageClass UID / provisioner が変わっていないことを比較。
- 変更対象外のデータ・CSI・Storage Operator に変更が無いこと、承認した新規 claim だけが増えたことを確認。
- 無効化した role、未収集 API、version drift、S3 adapter 未完了を残存一覧に記録。「全移行済み」と混同しない。

接続エラーは `migration_kubeconfig` と読み取り権限を確認します。Forbidden を empty list に変える修正は禁止です。
Ansible filter が見つからなければ実行ディレクトリと `ANSIBLE_CONFIG` を確認します。
render 失敗は private `analysis-*/render.log` を確認し、必須設定・モデル recipe・暗号化 vault を点検します。
PVC の immutable error、Multi-Attach、Pending は、claim を削除して解決しません。停止対象 Pod、accessMode、
CSI attachment、binding mode、topology を読み取りで調査し、お客様側の storage 管理者と調整します。
DB 認証失敗は元の資格情報・Secret reference と PVC を確認し、パスワード再生成や空 DB 作成へ逃げないでください。

拡張ポイントと現地 AI の作業手順は [SKILL.md](skills/rhoai-migration/SKILL.md) にあります。
