# 日常運用ガイド

RHOAI / MaaS 環境の管理者が日々行う作業の手順。
利用者からの依頼（ユーザー追加、quota 変更、モデル追加など）を受けてから
完了までを、依頼の種類ごとにまとめている。

- デプロイ後の一般的な運用操作: [operations.md](operations.md)
- 障害時の切り分け: [troubleshooting.md](troubleshooting.md)
- オフライン実行環境の構築・移行: [offline-environment.md](offline-environment.md)

---

## 0. 作業を始める前に（毎回）

```bash
cd /root/rhoai-ansible/rhoai-3.5-ansible_local

# 1. クラスタにログインしていることを確認する
#    playbook は自動 oc login しない
oc whoami
oc config current-context

# 2. パスワードファイルがあることを確認する（重要）
ls -l .generated-passwords.yml
```

> **`.generated-passwords.yml` が無い状態で playbook を実行してはいけない。**
> `roles/preflight/tasks/main.yml` がこのファイルを読めないと `maas_db_password` /
> `ogx_db_password` / `keycloak_db_password` を**新規生成**し、Secret だけが書き換わって
> Postgres 本体のパスワードと食い違い、MaaS と OGX が認証エラーになる。
> 詳細は「8. 秘密情報の保管場所」を参照。

---

## 1. 依頼の種類と対応表

| 利用者からの依頼 | 手順 | サービス停止 | 目安時間 |
|---|---|---|---|
| 新しいユーザーを追加してほしい | [2.1](#21-ユーザーを追加する) | なし | 2分 |
| パスワードを教えてほしい | [2.2](#22-発行済みパスワードを確認する) | なし | 1分 |
| パスワードを忘れた / 再発行してほしい | [2.3](#23-既存ユーザーのパスワードをリセットする) | なし | 5分 |
| ユーザーを無効化・削除してほしい | [2.4](#24-ユーザーを無効化削除する) | なし | 5分 |
| quota（トークン上限）を増やしてほしい | [3.1](#31-quota-を変更する) | なし | 3分 |
| 使えるモデルを増やしてほしい | [3.2](#32-利用できるモデルを増やす) | なし | 3分 |
| MaaSModelRef が Pending のまま | [7](#7-状態確認) の「よくある起動失敗」 | なし | 3分 |
| 新しいグループを作ってほしい | [3.3](#33-グループを新設する) | なし | 5分 |
| 新しいモデルを追加してほしい | [4.1](#41-新しいモデルを追加する) | **あり**（GPU 1枚構成） | DL時間＋30分 |
| モデルのダウンロードだけ先にやっておきたい | [4.2](#42-モデルのダウンロードだけ実行する) | なし | モデル次第 |
| 動いているモデルを入れ替えてほしい | [4.3](#43-モデルを入れ替える) | **あり** | 30分 |
| モデルを削除してほしい | [4.4](#44-モデルを削除する) | 該当モデルのみ | 10分 |
| vLLM のパラメータを調整したい / 試したい | [4.5](#45-vllm-引数を調整する) | 該当モデルのみ再起動 | 5分 |
| ユーザーの利用できるモデルを減らしてほしい | [3.4](#34-利用できるモデルを減らす) | なし | 3分 |
| API キーを発行してほしい | [5.1](#51-api-キーを発行する) | なし | 2分 |
| API キーの有効期限を延ばしてほしい | [5.2](#52-api-キーの有効期限について) | — | 要調査 |
| Keycloak の管理者パスワードが分からない | [6.1](#61-keycloak-管理者パスワードを確認する) | なし | 1分 |
| Keycloak にログインできなくなった | [6.2](#62-keycloak-管理者を復旧する) | **あり** | 15分 |
| 全体が正常か確認してほしい | [7](#7-状態確認) | なし | 5分 |
| opencode から使えるようにしてほしい | [5.3](#53-opencode-から使う) | なし | 2分 |

---

## 2. ユーザー管理

ユーザーは `inventory/<env>/group_vars/all/components.yml` の `keycloak_users` で宣言的に管理する。
Keycloak の管理コンソールで直接作ると inventory と乖離するので、原則 inventory を編集する。

### 2.1 ユーザーを追加する

**Step 1.** `components.yml` の `keycloak_users` に追記する。

```yaml
keycloak_users:
  - username: ta-suzuki                 # Keycloak のログイン ID
    email: ta-suzuki@example.com
    firstName: Taro
    lastName: Suzuki
    groups: [maas-qwen3-06b-users, maas-qwen3-8-27b-users]
```

`groups` に書いたグループは `keycloak_groups` に定義済みである必要がある。
未定義のグループ名を書いても無視されるだけでエラーにならないので注意。

**Step 2.** 適用する。

```bash
uv run ansible-playbook playbooks/manage_maas_access.yml -i inventory/<env>
```

**Step 3.** 発行されたパスワードを確認して本人に渡す。

```bash
cat .credentials/ta-suzuki.password
```

**仕組み**（`roles/keycloak/tasks/manage_maas_access.yml`）:
- Keycloak に同名ユーザーが**存在しない場合のみ**、12文字の英数字パスワードを生成してユーザーを作成する
- 生成したパスワードを `.credentials/<username>.password`（パーミッション 0600）に保存する
- `temporary: false` で作成するため、初回ログイン時のパスワード変更は要求されない
- 既に存在するユーザーには**一切触れない**（パスワードは再生成されない）
- グループ割当は差分のみ適用する（既に所属していればスキップ）

> **グループ割当は宣言的に同期される。** inventory の `groups` からグループ名を外して再実行すると、
> Keycloak 側の所属も解除される。解除対象は `keycloak_groups` に定義済みのグループだけで、
> Keycloak 上で手動作成した管理外のグループには触れない。
> 解除を止めたい場合は `-e keycloak_prune_group_membership=false` を付ける。
>
> **`groups` を書いていないユーザーは解除対象外**（「所属ゼロ」ではなく「管理対象外」の扱い）。
> 所属を全部外したい場合は `groups: []` と明示する。書き忘れで全解除されるのを避けるため。
>
> **ユーザー自体は inventory から削除しても消えない。** 削除は [2.4](#24-ユーザーを無効化削除する) を参照。

### 2.2 発行済みパスワードを確認する

```bash
# 個別
cat .credentials/<username>.password

# 一覧
ls -l .credentials/
for f in .credentials/*.password; do
  printf "%-20s %s\n" "$(basename "$f" .password)" "$(cat "$f")"
done
```

`playbooks/verify.yml` の環境サマリーにも、保存済み資格情報の一覧が `cat` コマンド付きで表示される。

> `.credentials/` に無いユーザーは、**この playbook 以外の経路で作られた**か、
> **別の作業ディレクトリで作成された**。後者の場合は旧ディレクトリの `.credentials/` を探す。

### 2.3 既存ユーザーのパスワードをリセットする

**playbook にはリセット機能が無い。** 新規ユーザーにしかパスワードを設定しないため、
既存ユーザーのパスワード変更は Keycloak の管理 API または管理コンソールで行う。

#### 方法 A: 管理コンソール（GUI）

```bash
# URL
echo "https://keycloak-keycloak.$(oc get ingresses.config/cluster -o jsonpath='{.spec.domain}')"

# 管理者パスワード
oc get secret keycloak-initial-admin -n keycloak -o jsonpath='{.data.username}' | base64 -d; echo
oc get secret keycloak-initial-admin -n keycloak -o jsonpath='{.data.password}' | base64 -d; echo
```

`maas` realm → Users → 対象ユーザー → Credentials → Reset password
→ Temporary を **Off** にして Save。

#### 方法 B: 管理 REST API（CLI）

```bash
cd /root/rhoai-ansible/rhoai-3.5-ansible_local

CLUSTER_DOMAIN=$(oc get ingresses.config/cluster -o jsonpath='{.spec.domain}')
KC_URL="https://keycloak-keycloak.${CLUSTER_DOMAIN}"
KC_USER=$(oc get secret keycloak-initial-admin -n keycloak -o jsonpath='{.data.username}' | base64 -d)
KC_PASS=$(oc get secret keycloak-initial-admin -n keycloak -o jsonpath='{.data.password}' | base64 -d)

# 管理トークン取得（master realm の admin-cli クライアント）
TOKEN=$(curl -sk -X POST "${KC_URL}/realms/master/protocol/openid-connect/token" \
  -d client_id=admin-cli -d grant_type=password \
  -d "username=${KC_USER}" -d "password=${KC_PASS}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

# 対象ユーザーの ID を引く
TARGET=ta-suzuki
UID=$(curl -sk -H "Authorization: Bearer ${TOKEN}" \
  "${KC_URL}/admin/realms/maas/users?username=${TARGET}&exact=true" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["id"])')

# 新しいパスワードを生成して設定
NEWPASS=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 12)
curl -sk -X PUT -H "Authorization: Bearer ${TOKEN}" -H 'Content-Type: application/json' \
  "${KC_URL}/admin/realms/maas/users/${UID}/reset-password" \
  -d "{\"type\":\"password\",\"value\":\"${NEWPASS}\",\"temporary\":false}"

# ローカルの記録も更新しておく（playbook の出力と整合させるため）
umask 077 && printf '%s' "$NEWPASS" > ".credentials/${TARGET}.password"
echo "new password: $NEWPASS"
```

> 最後の `.credentials/` 更新を忘れると、次に誰かが `cat` したときに古いパスワードを渡してしまう。必ず更新する。

### 2.4 ユーザーを無効化・削除する

**推奨は無効化**（削除すると API キーやトークン履歴との対応が追えなくなる）。

```bash
# 上記 2.3 の TOKEN / UID 取得まで済ませた状態で

# 無効化
curl -sk -X PUT -H "Authorization: Bearer ${TOKEN}" -H 'Content-Type: application/json' \
  "${KC_URL}/admin/realms/maas/users/${UID}" -d '{"enabled":false}'

# 削除（戻せない）
curl -sk -X DELETE -H "Authorization: Bearer ${TOKEN}" \
  "${KC_URL}/admin/realms/maas/users/${UID}"
```

あわせて `components.yml` の `keycloak_users` から該当エントリを削除し、
`.credentials/<username>.password` も削除しておく（inventory と実態を合わせるため）。

---

## 3. グループ・quota 管理

グループは `components.yml` の `keycloak_groups` で管理する。
ここを変更すると、Keycloak のグループと **MaaSAuthPolicy / MaaSSubscription** の両方が再生成される。

```yaml
keycloak_groups:
  maas-qwen3-8-27b-users:
    models: ["qwen3-8-27b"]      # アクセスを許すモデル。["*"] で全 present モデル
    priority: 10                  # 競合時の優先度。大きいほど優先
    quota_tokens_24h: 5000000     # 24時間あたりのトークン上限
    # cluster_role: cluster-admin # 指定すると ClusterRoleBinding も作る（管理者グループのみ）
```

### 3.1 quota を変更する

> 依頼例:「quota に引っかかったので増やしてほしい」

**Step 1.** 現在の消費状況を確認する。

```bash
# グループに紐づく Subscription の現在値
oc get maassubscription -A -o custom-columns=\
'NAME:.metadata.name,PRIORITY:.spec.priority,LIMIT:.spec.modelRefs[*].tokenRateLimits[*].limit,WINDOW:.spec.modelRefs[*].tokenRateLimits[*].window'

# 誰がどのグループか
grep -A3 "username: <ユーザー名>" inventory/<env>/group_vars/all/components.yml
```

**Step 2.** `components.yml` の該当グループの `quota_tokens_24h` を変更する。

```yaml
  maas-qwen3-8-27b-users:
    models: ["qwen3-8-27b"]
    priority: 10
    quota_tokens_24h: 20000000    # 5,000,000 → 20,000,000
```

**Step 3.** 適用する。

```bash
uv run ansible-playbook playbooks/manage_maas_access.yml -i inventory/<env>
```

**Step 4.** 反映を確認する。

```bash
oc get maassubscription maas-qwen3-8-27b-users-subscription -n models-as-a-service \
  -o jsonpath='{.spec.modelRefs[*].tokenRateLimits[*]}{"\n"}'
```

> **quota はグループ単位**であって個人単位ではない。特定の1名だけ増やしたい場合は、
> 専用グループ（例 `maas-qwen3-8-27b-heavy-users`）を作ってそのユーザーだけ所属させる（[3.3](#33-グループを新設する)）。
> サービス停止は発生しない。Subscription の更新のみ。

### 3.2 利用できるモデルを増やす

> 依頼例:「27B も使えるようにしてほしい」

既存グループに追加するか、既存の別グループに所属させるかの2通り。

**A. ユーザーを既存グループに追加する**（推奨・影響が小さい）

```yaml
keycloak_users:
  - username: ta-suzuki
    email: ta-suzuki@example.com
    groups: [maas-qwen3-06b-users, maas-qwen3-8-27b-users]   # ← 追加
```

**B. グループが扱えるモデルを増やす**（そのグループ全員に影響する）

```yaml
keycloak_groups:
  maas-qwen3-06b-users:
    models: ["qwen3-06b", "qwen3-8-27b"]   # ← 追加
```

どちらも適用は同じ。

```bash
uv run ansible-playbook playbooks/manage_maas_access.yml -i inventory/<env>
```

> `models` に書いたモデルが `state: absent` だと、そのモデルは AuthPolicy / Subscription から
> 自動的に除外される（エラーにはならない）。モデルがまだ無い場合は先に [4.1](#41-新しいモデルを追加する)。

### 3.3 グループを新設する

```yaml
keycloak_groups:
  maas-qwen3-8-27b-heavy-users:
    models: ["qwen3-8-27b"]
    priority: 15
    quota_tokens_24h: 50000000

keycloak_users:
  - username: ta-suzuki
    email: ta-suzuki@example.com
    groups: [maas-qwen3-8-27b-heavy-users]
```

```bash
uv run ansible-playbook playbooks/manage_maas_access.yml -i inventory/<env>
```

命名は `maas-<モデルキー>-users` に合わせる。グループ名は K8s リソース名になるため
RFC 1123 準拠（小文字英数・ハイフン・ドット）が必須で、`roles/preflight` が検証する。

### 3.4 利用できるモデルを減らす

> 依頼例:「このユーザーはもう 27B を使わないので外してほしい」

`keycloak_users` の該当ユーザーの `groups` からグループ名を削除して適用する。

```yaml
keycloak_users:
  - username: ta-suzuki
    email: ta-suzuki@example.com
    groups: [maas-qwen3-06b-users]          # maas-qwen3-8-27b-users を削除
```

```bash
uv run ansible-playbook playbooks/manage_maas_access.yml -i inventory/<env>
```

`keycloak_prune_group_membership`（既定 `true`）により、Keycloak 側の所属も解除される。
解除されるのは `keycloak_groups` に定義済みのグループだけで、Keycloak 上で手動作成した
管理外のグループには触れない。

```bash
# 解除の確認（出力に該当グループが含まれないこと）
# 2.3 の TOKEN 取得を済ませた状態で
curl -sk -H "Authorization: Bearer ${TOKEN}" \
  "${KC_URL}/admin/realms/maas/users/${UID}/groups" | python3 -m json.tool
```

一時的に解除を止めたい場合:

```bash
uv run ansible-playbook playbooks/manage_maas_access.yml -i inventory/<env> \
  -e keycloak_prune_group_membership=false
```

---

## 4. モデル管理

モデルは `components.yml` の `models` で管理する。dict のキーがモデル名になる。

### モデルキーの命名規約（重要）

`hf_repo` のモデル名部分を小文字化し、`.` を `-` に置き換える。

| hf_repo | キー |
|---|---|
| `Qwen/Qwen3.8-27B` | `qwen3-8-27b` |
| `Qwen/Qwen3.8-27B-FP8` | `qwen3-8-27b-fp8` |
| `deepseek-ai/DeepSeek-R1-0528` | `deepseek-r1-0528` |

キーは PVC 上のディレクトリ名 `/models/<キー>`、LLMInferenceService 名、
vLLM の `--served-model-name`（＝利用者が API で指定するモデル名）、
Keycloak グループ名 `maas-<キー>-users` に**変換なしで**使われる。
**一度決めたキーは変更しない。** 改名すると PVC キャッシュがヒットせず再ダウンロードになり、
利用者側の API 呼び出しも壊れる。

### GPU 枚数の制約

`roles/llm_serving/tasks/main.yml` は `state: present` なモデルの `gpu_count` を合計し、
クラスタの GPU 数を超えると **fail する**。

```bash
oc get nodes -o jsonpath='{range .items[*]}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}'
```

GPU 1枚の構成では `present` にできるモデルは常に1つだけ。

### 4.1 新しいモデルを追加する

> 依頼例:「Qwen3.8-27B を使えるようにしてほしい」

**Step 1.** `components.yml` の `models` にエントリを追加する（この時点では `state: present` のまま追加してよい。GPU が足りなければ後続のデプロイ実行時に fail するだけで、既存サービスには影響しない）。

```yaml
models:
  qwen3-8-27b:
    hf_repo: Qwen/Qwen3.8-27B
    state: present

    # vLLM 引数はここに生の文字列で書く。
    # role が自動付与するのは --chat-template だけ
    # (custom_chat_template を指定したモデルのみ) なので、
    # vLLM のバージョンアップでフラグが増減してもここを直すだけでよい。
    # 使えるフラグの確認:
    #   oc run -it --rm vllm-help --image=vllm/vllm-openai:v0.28.0 -- vllm serve --help
    vllm_args:
      - "--max-model-len=262144"
      - "--gpu-memory-utilization=0.92"
      - "--dtype=bfloat16"
      - "--max-num-seqs=64"
      - "--enable-prefix-caching"
      - "--tool-call-parser=qwen3_coder"
      - "--reasoning-parser=qwen3"
      - "--enable-auto-tool-choice"
      - '--speculative-config={"method":"mtp","num_speculative_tokens":1}'

    gpu_count: 1
    resources:
      requests: {cpu: "8",  memory: 48Gi}
      limits:   {cpu: "16", memory: 96Gi}
    startup_probe_failure_threshold: 180   # ×10秒 = 30分まで起動を待つ
```

> **モデル側で `vllm_args` を書くと `model_defaults.vllm_args` は完全に置き換わる。**
> 既定の `--enable-prefix-caching` 等が必要なら明示的に書くこと。
> 一方 `resources` は再帰マージされるので、`requests.cpu` だけ指定すれば `limits` は既定が残る。

**Step 2.** グループとユーザー割当を追加する（[3.3](#33-グループを新設する)）。

**Step 3.** GPU が空いているかで分岐する。

- GPU に空きがある → [Step 4](#step-4-デプロイする) へ
- GPU 1枚ですでに別モデルが動いている → [4.3 モデルを入れ替える](#43-モデルを入れ替える) へ

#### Step 4. デプロイする

```bash
uv run ansible-playbook playbooks/llm_add_model.yml -i inventory/<env>
```

> **`site.yml --tags workload,llm` ではなくこちらを使う。**
> `--tags workload,llm` は `llm_serving` だけを実行するため、Keycloak のグループ・
> ユーザー同期と MaaS ポリシー適用が走らない。`llm_add_model.yml` は
> llm_serving → manage_maas_access → MaaSModelRef の Ready 待ち、の順で一通り行う。
>
> **MaaSModelRef は MaaSAuthPolicy / MaaSSubscription の適用後でないと Ready にならない。**
> このため Ready 待ちは `llm_serving` の中ではなく `manage_maas_access` の後に置いてある。
> もし MaaSModelRef が Pending のまま止まったら、ポリシーだけ先に流せば解ける:
>   `uv run ansible-playbook playbooks/manage_maas_access.yml -i inventory/<env>`

実行される処理:
1. preflight（light）
2. llm_serving Phase 1 — `state: absent` なモデルの削除（GPU 解放）
3. llm_serving Phase 2 — ダウンロード → LLMInferenceService 適用 → Ready 待ち
4. manage_maas_access — Keycloak グループ/ユーザー同期 → AuthPolicy / Subscription 適用

**Step 5.** 確認する（[7](#7-状態確認)）。

### 4.2 モデルのダウンロードだけ実行する

> 用途: GPU が空く前に大きいモデルを PVC に落としておきたい / disconnected 環境でダウンロードだけ先に通したい

```bash
uv run ansible-playbook playbooks/llm_download.yml -i inventory/<env>

# 特定のモデルだけ
uv run ansible-playbook playbooks/llm_download.yml -i inventory/<env> \
  -e '{"models": {"qwen3-8-27b": {"hf_repo": "Qwen/Qwen3.8-27B", "state": "present"}}}'

# site.yml 経由でも同じ
uv run ansible-playbook site.yml -i inventory/<env> --tags llm -e download_only=true
```

`download_only=true` のとき、以下がすべてスキップされる:
- GPU 容量チェック（**両方のモデルが present のままでも実行できる**）
- `state: absent` なモデルの削除
- LLMInferenceService / MaaSModelRef の作成と Ready 待ち

実行されるのは namespace / hf-token Secret / `hf-model-cache` PVC / ClusterStorageContainer /
chat template ConfigMap / SA・RoleBinding の作成と、download Job の実行・完了待ちのみ。

**稼働中のモデルの Pod には一切触れないので、サービス停止は発生しない。**

進捗確認:

```bash
oc get job -n llm-serving download-qwen3-8-27b
oc logs -n llm-serving job/download-qwen3-8-27b --tail=20 -f
oc get pvc -n llm-serving hf-model-cache
```

> `-e` で特定モデルだけ指定するときのキーは、`components.yml` に書くキーと**完全に一致**させること。
> キャッシュ判定は `/models/<キー>/config.json` の存在で行うため、違うと再ダウンロードになる。
> playbook 側の待機は `oc wait --timeout=3600s`。1時間を超えた場合は再実行すれば走行中の Job を待ち直す。

### 4.3 モデルを入れ替える

> GPU 1枚の構成で、動いているモデルを別のモデルに差し替える手順。
> **サービス停止が発生する。** 停止時間を最小化するためダウンロードを先行させる。

**Step 1.** 新モデルを先にダウンロードする（現行モデルは稼働したまま）。

```bash
uv run ansible-playbook playbooks/llm_download.yml -i inventory/<env>
```

**Step 2.** 完了を確認する。

```bash
oc get job -n llm-serving download-qwen3-8-27b     # COMPLETIONS 1/1
```

**Step 3.** `components.yml` で state を入れ替える。

```yaml
models:
  qwen3-06b:
    state: absent      # ← present から変更
  qwen3-8-27b:
    state: present
```

`purge` は触らない（既定 `false` = PVC 上のデータを残す。戻すときに再ダウンロード不要）。

**Step 4.** 入れ替えを実行する。**ここから停止する。**

```bash
uv run ansible-playbook playbooks/llm_add_model.yml -i inventory/<env>
```

Phase 1 で旧モデルの LLMInferenceService が削除されて GPU が解放され、Phase 2 で新モデルが起動する。

**Step 5.** 確認する（[7](#7-状態確認)）。

**戻したいとき** — state を逆に書き換えて Step 4 を再実行する。PVC にデータが残っているので再ダウンロードは発生しない。

> `absent` にしたモデルは MaaS の AuthPolicy / Subscription からも自動的に外れる。
> グループ自体は残るが、参照先モデルが無いためそのグループの Subscription は生成されなくなる。

### 4.4 モデルを削除する

```yaml
models:
  qwen3-4b:
    hf_repo: Qwen/Qwen3-4B
    state: absent
    purge: false     # true にすると PVC 上の /models/qwen3-4b も削除する
```

```bash
uv run ansible-playbook playbooks/llm_add_model.yml -i inventory/<env>
```

`purge: true` は PVC の容量が逼迫したときだけ使う。`hf-model-cache` は 200Gi。

```bash
# PVC 使用量の確認
oc exec -n llm-serving deploy/<any-pod> -- df -h /models 2>/dev/null || \
oc run pvc-du --rm -it --restart=Never -n llm-serving \
  --image=registry.access.redhat.com/ubi9/ubi-minimal:latest \
  --overrides='{"spec":{"containers":[{"name":"du","image":"registry.access.redhat.com/ubi9/ubi-minimal:latest","command":["du","-sh","/models"],"volumeMounts":[{"name":"c","mountPath":"/models"}]}],"volumes":[{"name":"c","persistentVolumeClaim":{"claimName":"hf-model-cache"}}]}}'
```

### 4.5 vLLM 引数を調整する

> 依頼例:「コンテキスト長を変えて試したい」「起動しないのでパラメータを変えたい」
> `oc edit` で直接いじる代わりに、専用の playbook / スクリプトを使う。

#### 対話的に編集する（`oc edit` 相当・推奨）

```bash
bash scripts/vllm-args.sh qwen3-8-27b -i inventory/<env>
```

1. 稼働中の LLMInferenceService から現在の引数を取得する
2. `--chat-template`（role が自動付与する唯一の引数）を除いた一覧を `$EDITOR` で開く
3. 保存して閉じると適用され、Pod が再作成される
4. Ready を待ち、**inventory に貼るスニペット**を表示する

1行に1引数のプレーンテキストなので、行を足す・消す・コメントアウト（`#`）するだけでよい。
変更せずに閉じるか、中身を空にすると中止する。

```bash
EDITOR=vim bash scripts/vllm-args.sh qwen3-8-27b -i inventory/<env>   # エディタを指定
EDIT_ARGS_WAIT=0 bash scripts/vllm-args.sh qwen3-8-27b -i inventory/<env>  # Ready を待たない
```

#### Ansible が適用する manifest を事前に見る

クラスタに触れずに、`roles/llm_serving` が実際に適用する LLMInferenceService を
レンダリングして確認できる。oc ログインは不要。

```bash
# present な全モデル
uv run ansible-playbook playbooks/llm_show_manifest.yml -i inventory/<env>

# モデル指定
uv run ansible-playbook playbooks/llm_show_manifest.yml -i inventory/<env> \
  -e model=qwen3-8-27b-fp8

# absent も含めて全部 / ファイルにも出力
uv run ansible-playbook playbooks/llm_show_manifest.yml -i inventory/<env> \
  -e all_models=true -e dest=/tmp/llm-manifests
```

role と同じ変数解決（`model_defaults` → `_fallback` → `model_recipes` → inventory）と
同じテンプレートを使うため、実際に適用されるものと一致する。

> MaaSModelRef は role のタスク内で直接定義しているためこの出力には含まれない。
> Secret / PVC / ConfigMap まで含めた全リソースを見たい場合は
> `python3 render-manifests/render.py -c inventory/<env> -o /tmp/out` を使う。

> `-i inventory/sample` では変数が読み込まれない（`.sample` 拡張子は Ansible の
> group_vars として認識されない）。sample を試すときは拡張子を外してコピーする。

#### 現在の引数を確認するだけ

```bash
uv run ansible-playbook playbooks/llm_edit_args.yml -i inventory/<env> -e model=qwen3-8-27b
```

クラスタ上の現在値と inventory の `vllm_args` の両方が表示される。

#### 引数を直接指定して適用する

```bash
uv run ansible-playbook playbooks/llm_edit_args.yml -i inventory/<env> \
  -e model=qwen3-8-27b \
  -e '{"vllm_args": ["--max-model-len=32768", "--gpu-memory-utilization=0.85", "--enforce-eager"]}'
```

`oc patch --type=json` で `spec.template.containers[0].args` だけを置き換える。
ダウンロード・GPU 調整・MaaS ポリシー適用は走らないので、`llm_add_model.yml` より大幅に速い。

> **この変更は inventory には保存されない。**
> 次に `llm_add_model.yml` / `site.yml` を流すと inventory の内容に戻る。
> 試した結果を確定するときは、表示されたスニペットを `components.yml` の
> `models.<キー>.vllm_args` に貼ること。

#### チューニングの流れ

```bash
# 1. 試す（何度でも繰り返す）
bash scripts/vllm-args.sh qwen3-8-27b -i inventory/<env>

# 2. 起動ログを見る
oc logs -n llm-serving -l app.kubernetes.io/name=qwen3-8-27b -c main --tail=100 -f

# 3. 良い値が決まったら inventory に反映して確定させる
vi inventory/<env>/group_vars/all/components.yml
uv run ansible-playbook playbooks/llm_add_model.yml -i inventory/<env>
```

> `oc edit llminferenceservice` でも同じことはできるが、
> role が自動付与する2引数を手で書く必要があり、inventory スニペットも出ない。
> 上記の方法を使うほうが戻し忘れを防げる。

---

## 5. API キー

### 5.1 API キーを発行する

```bash
uv run ansible-playbook playbooks/maas_create_apikey.yml -i inventory/<env> \
  -e api_key_user=ta-suzuki \
  -e api_key_name=ta-suzuki-cli
```

実行すると `API Key: <値>` が表示される。**この表示が唯一の取得機会**なので、その場で控えて本人に渡す。

**仕組み**（`playbooks/maas_create_apikey.yml`）:
- `redhat-ai-gateway-infra` namespace の maas-api Pod に `oc exec` し、
  `POST https://localhost:8443/v1/api-keys` を叩く
- `X-MaaS-Username` ヘッダに `api_key_user`（省略時は実行ユーザー名）を渡す
- `X-MaaS-Group` ヘッダは **`["maas-admins"]` が固定**で入っている

> 発行したキーは playbook では保存されない。
> 運用で管理者用キーを使い回す場合、`.vllm-token` に置いておくと `playbooks/verify.yml` の
> サマリーに `cat .vllm-token` として表示される（`umask 077` で作成すること）。

動作確認:

```bash
MAAS_URL=$(oc get gateway maas-default-gateway -n openshift-ingress -o jsonpath='https://{.spec.listeners[0].hostname}')
curl -sk "${MAAS_URL}/llm-serving/qwen3-8-27b/v1/chat/completions" \
  -H "Authorization: Bearer <API_KEY>" \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-8-27b","messages":[{"role":"user","content":"hello"}],"max_tokens":16}'
```

### 5.2 API キーの有効期限について

> 依頼例:「API キーの有効期限を90日以上に設定できるようにしてほしい」

**この Ansible リポジトリには有効期限を設定する箇所が無い。**
`playbooks/maas_create_apikey.yml` が送るリクエストボディは `{"name": "..."}` のみで、
期限に関するパラメータを渡していない。期限は maas-api 側の既定値で決まる。

対応するには、まず maas-api の仕様を確認する必要がある。

```bash
# 1. maas-api が期限パラメータを受け付けるか調べる
POD=$(oc get pods -n redhat-ai-gateway-infra -l app.kubernetes.io/name=maas-api -o jsonpath='{.items[0].metadata.name}')
oc exec -n redhat-ai-gateway-infra "$POD" -- curl -sk https://localhost:8443/openapi.json 2>/dev/null | head -c 2000
# もしくは
oc exec -n redhat-ai-gateway-infra "$POD" -- curl -sk https://localhost:8443/docs 2>/dev/null | head -c 2000

# 2. maas-api の環境変数に期限設定が無いか
oc set env deploy/maas-api -n redhat-ai-gateway-infra --list | grep -iE 'expir|ttl|key'

# 3. MaasTenantConfig の spec に期限フィールドが無いか
oc explain maastenantconfig.spec --recursive 2>/dev/null | grep -iE 'expir|ttl|duration'
oc get maastenantconfig default-tenant -n redhat-ods-applications -o yaml
```

**判明した場合の反映先:**

- リクエストボディで指定できる → `playbooks/maas_create_apikey.yml` の
  `-d '{"name":"..."}'` に期限フィールドを追加し、`api_key_expiry` のような変数で外出しする
- maas-api の環境変数 → `roles/rhoai/tasks/main.yml` の maas-api 関連タスクに `oc set env` を追加する
- MaasTenantConfig の spec → `roles/keycloak/tasks/manage_maas_access.yml` の
  「Ensure MaasTenantConfig exists」タスク（現在 `spec: {}`）と
  `roles/keycloak/templates/maas-policies.yml.j2` の MaasTenantConfig を更新する

いずれの場合も `render-manifests/render.py` の対応箇所も同時に更新すること（CLAUDE.md 参照）。

### 5.3 opencode から使う

```bash
cd /root/rhoai-ansible/rhoai-3.5-ansible_local
bash scripts/setup-opencode.sh setup
```

クラスタから Gateway のホスト名と Ready なモデルを自動検出し、以下を生成する。

| 生成物 | 内容 |
|---|---|
| `.vllm-token` | MaaS API キー (0600)。既にあれば再発行せず再利用する |
| `opencode.json` | opencode の設定。モデルごとに provider を1つ作る |

MaaS はモデルごとに URL のパスが分かれる（`/<namespace>/<モデルキー>/v1`）ため、
**1 プロバイダ = 1 モデル**の構成にしている。`limit.context` は
LLMInferenceService の `--max-model-len` から読み取る。
`--reasoning-parser qwen3` に合わせて `reasoning: true` と
`interleaved.field: reasoning_content` を設定済み。

```bash
bash scripts/setup-opencode.sh show   # 現在の設定を表示
bash scripts/setup-opencode.sh test   # /v1/models と /v1/chat/completions で疎通確認
```

使い方:

```bash
cd /root/rhoai-ansible/rhoai-3.5-ansible_local
opencode                      # このディレクトリで起動すると opencode.json を読む
opencode run "こんにちは"
# モデル切り替えは opencode 内で /models
```

別の場所から使う場合はグローバル設定へコピーする。

```bash
mkdir -p ~/.config/opencode
cp opencode.json ~/.config/opencode/opencode.json
cp .vllm-token   ~/.config/opencode/.vllm-token
```

オプション:

```bash
OPENCODE_API_USER=testuser1 bash scripts/setup-opencode.sh setup   # キーの発行ユーザー
OPENCODE_MODEL=qwen3-06b    bash scripts/setup-opencode.sh setup   # 既定モデルの指定
OPENCODE_API_GROUPS='["maas-admins"]' bash scripts/setup-opencode.sh setup  # グループを明示
OPENCODE_INVENTORY=inventory/<env>    bash scripts/setup-opencode.sh setup  # グループ検索先
```

API キーに紐づくグループは inventory の `keycloak_users` から
そのユーザーの `groups` を引く。**引けない場合はキーを発行せずエラーになる**
（未登録ユーザーに管理者グループのキーを発行しないため、既定値は設けていない）。
inventory に無いユーザーへ発行するときは `OPENCODE_API_GROUPS` で明示する。

発行済みキーの使い回しは **同じユーザーかつ同じグループのときだけ**行う。
発行先は `.vllm-token.meta` に記録され、`setup-opencode.sh show` で確認できる。
ユーザーを切り替えて再実行すると新しいキーを発行して差し替えるので、
管理者のキーが一般ユーザー向けの設定に残ることはない。

> モデルを入れ替えたら再実行すること。`opencode.json` の provider はモデルキーに
> 紐づくため、キーが変わると古い設定のままでは繋がらない。
> `model` / `provider` 以外のユーザー設定（`agent`、`mcp` 等）は再実行しても保持される。

---

## 6. Keycloak 管理者

### 6.1 Keycloak 管理者パスワードを確認する

```bash
oc get secret keycloak-initial-admin -n keycloak -o jsonpath='{.data.username}' | base64 -d; echo
oc get secret keycloak-initial-admin -n keycloak -o jsonpath='{.data.password}' | base64 -d; echo

# 管理コンソール URL
echo "https://keycloak-keycloak.$(oc get ingresses.config/cluster -o jsonpath='{.spec.domain}')"
```

### 6.2 Keycloak 管理者を復旧する

> 症状: playbook が `Keycloak rejected the initial-admin Secret (HTTP 401)` で止まる、
> または管理コンソールにログインできない。
> PostgreSQL の PVC を作り直したときなどに、Secret と DB の資格情報が乖離すると発生する。

> **`keycloak-initial-admin` Secret を削除したり Pod を再起動してもパスワードはリセットされない。**
> master realm が既に存在する場合、Keycloak は Secret を読み直さない。自己流の操作で状況を悪化させないこと。

```bash
uv run ansible-playbook playbooks/recover_keycloak_admin.yml -i inventory/<env> \
  -e keycloak_admin_recovery=true
```

`-e keycloak_admin_recovery=true` は必須（安全装置）。**この playbook は Keycloak を一時停止させることがある。**

復旧 role は、まず現在の Secret で認証を試し、通るなら何も変更しない
（`roles/keycloak_admin_recovery/tasks/existing.yml`）。乖離が確認できた場合のみ修復する。

---

## 7. 状態確認

### 全体

```bash
uv run ansible-playbook playbooks/verify.yml -i inventory/<env>
```

末尾に環境サマリー（Console / Dashboard / Keycloak / MaaS API エンドポイント / MLflow /
保存済み資格情報の一覧）が表示される。

### 個別

```bash
# モデル
oc get llminferenceservice -n llm-serving
oc get maasmodelref -n llm-serving
oc get pods -n llm-serving

# MaaS ポリシー
oc get maasauthpolicy -A
oc get maassubscription -A
oc get maastenantconfig -A

# vLLM のログ（起動失敗の調査）
oc logs -n llm-serving -l app.kubernetes.io/name=qwen3-8-27b -c main --tail=100

# GPU
oc get nodes -o jsonpath='{range .items[*]}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}'
oc get pods -A -o json | python3 -c "
import sys,json
d=json.load(sys.stdin)
for p in d['items']:
  for c in p['spec'].get('containers',[]):
    g=int(c.get('resources',{}).get('requests',{}).get('nvidia.com/gpu',0) or 0)
    if g: print(p['metadata']['namespace'], p['metadata']['name'], g)
"
```

### よくある起動失敗

| ログの内容 | 原因 | 対処 |
|---|---|---|
| `No available memory for the cache blocks` | KV キャッシュ不足 | `--max-model-len` か `--max-num-seqs` を下げる |
| `speculative` / `mtp` 関連のエラー | 起動時に設定が拒否された可能性 | 下記の B1 診断で Pod 引数と起動ログを切り分ける |
| CUDA graph 関連で起動しない | CUDA graph の問題 | 一時的に `--enforce-eager` を `vllm_args` に足す |
| `config.json not found` | ダウンロード失敗 | download Job のログを見る。`hf_repo` の綴りを確認 |
| GPU 不足で role が fail | present モデルの合計 > GPU 数 | いずれかを `state: absent` にする |
| MaaSModelRef が Pending のまま | MaaSAuthPolicy / MaaSSubscription が未適用 | `playbooks/manage_maas_access.yml` を実行する |
| transformers の `... is part of ...InitKwargs, but not documented` | 単なる警告（stderr に出るだけ） | 無視してよい |

### 7.x 推論の疎通と MTP の動作確認

`playbooks/verify.yml` は「リソースが Ready か」しか見ないため、
**Ready なのに推論が返らない**、**speculative decoding を設定したのに効いていない**
は検出できません。実際にリクエストを流して確認する専用 playbook を使います。

```bash
# present な全モデルに推論を投げ、MTP の受理率まで出す
uv run ansible-playbook playbooks/llm_verify_inference.yml -i inventory/<env>

# モデルを指定
... -e model=qwen3-5-9b

# MaaS Gateway 経由で認証込みの end-to-end を確認する
... -e inference_via=maas -e maas_api_user=admin1

# 受理率の測定精度を上げる
... -e inference_requests=10 -e inference_max_tokens=1024
```

出力例:

```
model            : qwen3-5-9b (llm-serving)
requests         : 3 x 512 tokens (via direct)
generated tokens : 1536
draft tokens     : 828
accepted tokens  : 708
acceptance rate  : 0.855
MTP: 有効 (ドラフトが生成され検証されています)
```

判定のしかた:

| 結果 | 意味 |
|---|---|
| `draft tokens` が 0 | MTP が効いていない。`--speculative-config` が vLLM に届いていない |
| `acceptance rate` が極端に低い | ドラフトがほぼ棄却されている。`num_speculative_tokens` を見直す |
| `generated tokens` が 0 | 推論自体が返っていない。Pod のログを見る |

CI や定期確認で閾値を割ったら落としたい場合は `-e inference_min_acceptance_rate=0.3`
のように指定します（既定 0 = 判定しない）。

**経路の違い**

- `inference_via=direct` (既定): Pod 内から `localhost:8000` を叩く。外部 DNS も
  API キーも不要なので、Route53 未設定の検証クラスタでも動く。MaaS の認可は通らない
- `inference_via=maas`: API キー発行 → Gateway 経由で推論 → キー失効まで実施する。
  Gateway Service の ClusterIP に `curl --resolve` で当てるため、外部 DNS 不要で
  本番と同じ TLS SNI / ルーティングを通る

**reasoning モデルでの注意**

Qwen3 系は既定で思考に数百トークン使うため、`max_tokens` が小さいと
`finish_reason=length` のまま `content` が空で返ります。この playbook は既定で
`chat_template_kwargs={"enable_thinking": false}` を送って短く確定的に応答させます。
thinking 込みで見たい場合は `-e inference_chat_template_kwargs={}` を指定します。

---

## 8. 秘密情報の保管場所

すべて `/root/rhoai-ansible/rhoai-3.5-ansible_local/` 配下。`.gitignore` で除外済み。

| パス | 内容 | 失うとどうなるか |
|---|---|---|
| `.generated-passwords.yml` | maas / ogx / keycloak の DB パスワード、OIDC クライアントシークレット、group_sync_password | **playbook 実行時に再生成され、MaaS と OGX が認証エラーになる** |
| `.credentials/<user>.password` | playbook が発行したユーザーの初期パスワード | 個別にリセットが必要（[2.3](#23-既存ユーザーのパスワードをリセットする)） |
| `.kube/config` | kubeconfig | 再取得すればよい |
| `inventory/<env>/group_vars/all/vault.yml` | `hf_token` | HuggingFace から再取得 |
| `.vllm-token` | 運用用の API キー（任意） | 再発行すればよい |
| `.maas-policies-generated.yaml` | 直近生成した MaaS ポリシー | playbook 実行で再生成される |
| Keycloak 管理者 | クラスタ上の Secret `keycloak-initial-admin`（ローカルには無い） | [6.2](#62-keycloak-管理者を復旧する) |

### バックアップ

作業ディレクトリごと退避するのが確実。

```bash
cd /root/rhoai-ansible
tar czf ~/rhoai-secrets-$(date +%Y%m%d).tar.gz \
  rhoai-3.5-ansible_local/.generated-passwords.yml \
  rhoai-3.5-ansible_local/.credentials \
  rhoai-3.5-ansible_local/inventory/*/group_vars/all/vault.yml
chmod 600 ~/rhoai-secrets-$(date +%Y%m%d).tar.gz
```

---

## 9. やってはいけないこと

| 禁止事項 | 理由 |
|---|---|
| `.generated-passwords.yml` が無い状態で playbook を実行する | DB パスワードが再生成され、MaaS / OGX が壊れる |
| モデルキーを後から改名する | PVC キャッシュが無効化され、API のモデル名と Keycloak グループ名も変わる |
| `keycloak-initial-admin` Secret を削除する / Keycloak Pod を再起動してパスワードをリセットしようとする | リセットされない。[6.2](#62-keycloak-管理者を復旧する) の手順を使う |
| GPU 1枚で2つのモデルを `present` にしたまま `--tags llm` を実行する | GPU 容量チェックで fail する（ただし既存サービスは無事。`download_only=true` なら実行可） |
| Keycloak 管理コンソールでユーザー / グループを直接作る | inventory と乖離し、次回 playbook 実行時に意図しない差分が出る |
| `uninstall.yml` を安易に実行する | クラスタ上のリソースを削除する。バックアップは取られるが復旧は手作業 |
| 作業ディレクトリ名を変更する | venv に絶対パスが焼き込まれている。変更する場合は `scripts/setup-env.sh` で再構築が必要 |

---

## 10. チートシート

```bash
cd /root/rhoai-ansible/rhoai-3.5-ansible_local

# ユーザー / グループ / quota / モデル割当の変更を反映
uv run ansible-playbook playbooks/manage_maas_access.yml -i inventory/<env>

# モデルのダウンロードだけ（停止なし）
uv run ansible-playbook playbooks/llm_download.yml   -i inventory/<env>

# モデルのデプロイ + MaaS/Keycloak 反映
uv run ansible-playbook playbooks/llm_add_model.yml  -i inventory/<env>

# vLLM 引数を対話的に編集して即適用（oc edit 相当）
bash scripts/vllm-args.sh <model> -i inventory/<env>

# vLLM 引数を直接指定して即適用
uv run ansible-playbook playbooks/llm_edit_args.yml -i inventory/<env> \
  -e model=<model> -e '{"vllm_args": ["--max-model-len=32768"]}'

# API キー発行
uv run ansible-playbook playbooks/maas_create_apikey.yml -i inventory/<env> \
  -e api_key_user=<user> -e api_key_name=<name>

# 全体確認
uv run ansible-playbook playbooks/verify.yml -i inventory/<env>

# Keycloak 管理者復旧（Keycloak が一時停止する）
uv run ansible-playbook playbooks/recover_keycloak_admin.yml -i inventory/<env> \
  -e keycloak_admin_recovery=true

# パスワード確認
cat .credentials/<user>.password
oc get secret keycloak-initial-admin -n keycloak -o jsonpath='{.data.password}' | base64 -d; echo
```
