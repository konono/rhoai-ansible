---
name: rhoai-migration
description: 手動構築した OpenShift AI と付帯ツールを標準 Ansible 構成へ移行するため、既存値の収集、リスク診断、inventory の調整と現地 adapter の実装・検証を行う。既存 CSI・StorageClass・PV・ストレージ Operator を変更しない移行で使用する。
---

# OpenShift AI の標準構成への移行

この skill はリポジトリ同梱用です。配置は `migration/skills/rhoai-migration/`。
実行する場所は **`migration/`**、通常のインストーラはその親にあります。
まず [migration/README.md](../../README.md) の入力契約、診断コード、未対応範囲を読む。
Ansible を実行入口とし、別の Python デプロイヤーを作らない。複雑な純粋解析は既存 filter plugin へ追加する。

## 最初に確定すること

- 今回許可された操作はローカル作業・読み取り・本番適用のどこまでか。ローカル限定なら kubeconfig があっても API を呼ばない。
- 最終目標は標準 `site.yml` への収束。現在の spec の再 apply やラベル追加だけで「移行完了」としない。
- 停止・設定変更は計画できるが、既存 CSI・SC・PV・ストレージ Operator を変更しない。
  既存 PVC の UID / volumeName / data mapping を保持する。新しい claim は明示承認した用途だけ。
- 既存ストレージや外部 DB / S3 の所有者、復旧手順、停止枠、対象 namespace / tenant を確認する。
  既に得た許可は繰り返し求めない。新たに範囲を広げる変更だけを具体的に示す。

## 作業の流れ

1. `collect.yml` の結果、または提供された `baseline.yml` を private bundle に置く。
   `generate.yml` はオフラインでも使える。過去の baseline を上書きしない。
2. draft inventory を調整し `plan.yml` を実行。`blockers`、`warnings`、`changes` を調べる。
   CRD/API が無い場合と権限不足・接続失敗を区別する。Forbidden を空リストへ変換しない。
3. source role の main/tasks/templates と renderer の対応経路を読む。
   apply/SSA だけでなく、command、API、prune、再作成、Secret 生成、default 値を確認する。
4. 現地固有の差分を次のいずれかに分類し、どれを選んだか記録する:
   - inventory で表現できる設定 → 収集した値を入れる。
   - 標準化する設定 → 変更前後、停止の理由、復旧方法を記載する。
   - 既存データの対応付け → 既存 resource / Secret / PVC 名を消費する adapter を実装する。
   - 収集不能・未対応 → blocker を維持し、必要な情報・実装を示す。
5. 変更後にローカルテストと再 plan。コード／inventory が変われば古い plan の承認は使わない。
6. 本番適用が許可された工程でのみ `apply.yml` を実行。事前 gate を飛ばすために直接 site.yml を実行しない。
7. verify に加え、推論・認証・group membership・MaaS経路・監視と、ストレージ不変条件を確認する。
   有効にしていない role や未対応項目を残存一覧に書く。

## 現地で手直しする場所

|目的|実装箇所|
|---|---|
|別 API/version/kind の収集|`roles/migration/vars/main.yml` と `tasks/read.yml` / `collect_type.yml`|
|動的 inventory の逆引き|`filter_plugins/migration.py` の `infer()`|
|危険な差分・未対応条件|同ファイルの `assess()`|
|適用前の検証|`roles/migration/tasks/apply.yml`、`site_guard.yml`|
|アプリ固有の適用|親ディレクトリの source role と `render-manifests/render.py`|
|オフライン回帰|`tests/test_migration.py` と親 `tests/` の対応テスト|

### 外部 S3 adapter

現在 MLflow / OGX は `S3_ADAPTER_REQUIRED` で停止する。CSI が提供されても S3 が存在するとは限らない。
既存 endpoint、bucket、Secret reference、CA、TLS 検証方針を確認する。
role が BackingStore / BucketClass / OBC / NooBaa を作らず、既存情報を消費する分岐を実装する。
既存 DB の PVC と資格情報は別途引き継ぐ。renderer のガイドも同じ分岐にする。
「外部 S3 分岐ではストレージ管理 API を一切変更しない」ことを模擬モジュールの呼び出しで検査してから、
診断と site guard をその**具体的な分岐**に対応させる。無条件の bypass フラグを足さない。

### PVC / DB の名前が違う場合

新しい標準名の空 PVC・DB を作って元データを見失わないよう、既存 claim を参照する設定を先に実装する。
claim の削除・再作成、volumeName/StorageClass の差し替え、サイズ縮小で解決しない。
データのコピーやストレージ側変更が必要なら、現在の移行範囲では実行せず、お客様のデータ移行手順に切り出す。

### 収集した設定の意味が分からない場合

`pvc://` から HF repository、Secret 名からパスワード、DB Pod の存在からデータ移行完了を推測しない。
カスタム catalog / approval policy / installedCSV はそれぞれ別に扱う。
`startingCSV` を更新固定と誤認しない。実際に使用する Operator 版で必要な API・動作を確認する。
移行元が RHOAI 3.5 以外なら、対応 upgrade path を先に確定する。

## 検証の期待値

- 読み取り／解析だけの実行では cluster の create/update/delete が0件。
- storage ownership、未知の資格情報、PVC/data mapping の不足があれば apply より前に停止。
- cluster UID、resource UID/spec、installedCSV、inventory、実装の変更で旧 plan を拒否。
- report に Secret 値や inline env 値を出さない。baseline と render 出力自体は機密資材として扱う。
- fail-closed を外す、LVM 自動有効化を戻す、既存 password 読み込みを戻す等の変異をテストで検出する。
- `--check` や manifest diff だけで「安全」を宣言しない。非 manifest 経路と controller の副作用を別に確認する。
- 標準化で許容した差分と意図しない変更を分ける。未実施の restore、未検証の API/version は未検証と書く。

実行例・診断コード別の調査は [README](../../README.md) を使う。
追加の playbook/script も `migration/` 内に置き、普段の role の設定変更は必要最小限の共通設定にする。
通常インストールの挙動を変える場合は、移行 profile なしの回帰テストも実行する。

## 失敗したとき

失敗した task、作用した resource、適用前後、ログの保存先を記録する。部分適用を自動 rollback と偽らない。
再実行前に現在状態を再収集し、新しい plan を作る。削除・finalizer 除去・uninstall を復旧の近道にしない。
`uninstall.yml` はストレージを消し得るため、この移行 profile のロールバックとして使わない。
停止許可をストレージ破壊の許可に読み替えない。
