# オフライン実行環境の作り方

disconnected 環境の bastion など、インターネットに出られないホストで playbook を
実行するための環境を作る手順。

稼働中の環境から資材を引き継ぐ場合と、ゼロから作る場合の両方を扱う。

> この手順で作ったディレクトリは git 管理対象外（ルートの `.gitignore` で
> `rhoai-3.5-ansible_local/` と `ansible/` を除外している）。
> 秘密情報と 1GB 超のバイナリを含むため、リポジトリには入れない。

---

## 1. 構成

playbook 本体（git 管理）と、環境固有の資材（git 管理外）に分かれる。

| 種別 | 内容 | git |
|---|---|---|
| playbook 本体 | `roles/` `playbooks/` `tasks/` `site.yml` `inventory/sample/` `scripts/` `tests/` `render-manifests/` | 管理 |
| Python 実行環境 | `.venv/` `.local/` `collections/` | 管理外 |
| オフライン配布物 | `vendor/`（wheels / python / uv / collections） | 管理外 |
| 秘密情報 | `.kube/` `.credentials/` `.generated-passwords.yml` `inventory/<env>/group_vars/all/vault.yml` `.vllm-token` | 管理外 |
| 生成物 | `.maas-policies-generated.yaml` `opencode.json` `*_manifests/` | 管理外 |

この分類は `pack-playbook.sh` の `--exclude` と `unpack-playbook.py` の
`EXCLUDE_DIRS` / `EXCLUDE_FILES` に実装されている。**どちらかを変えたら両方直すこと。**

---

## 2. ゼロから作る

オンライン環境で依存物を取得し、オフライン環境へ持ち込む。

```bash
# --- オンライン側 ---
cd rhoai-3.5-ansible
bash scripts/download-deps.sh          # vendor/ に wheels / python / uv / collections を取得
tar czf /tmp/vendor.tar.gz vendor/

# --- オフライン側 ---
tar xzf /tmp/vendor.tar.gz             # playbook ディレクトリ直下に vendor/ を展開
bash scripts/setup-env.sh              # vendor/ から .venv / .local / collections を構築
```

`pyproject.toml` に以下が必要。オフラインで `uv sync` するときに `vendor/wheels`
を参照させるための設定で、**オンライン運用のリポジトリ本体には入れていない**。

```toml
[tool.uv]
find-links = ["vendor/wheels"]
```

### setup-env.sh でつまずいたとき

`vendor/python` の展開時に `share/terminfo/...: Cannot open: Permission denied` で
止まることがある（既存ファイルの権限が原因）。Python 本体の展開自体は完了しているので、
`.local/python/bin/python3.12` があれば venv 作成以降を手で続行できる。

```bash
chmod -R u+w .local 2>/dev/null; rm -rf .local .venv collections
bash scripts/setup-env.sh
```

---

## 3. 稼働中の環境から引き継ぐ

既に動いている playbook ディレクトリ（仮に `ansible/`）がある場合は、
**playbook 本体だけ新しくして、環境資材はそのまま持ち込む**のが速い。

```bash
# 1. 最新の playbook をコピー
cp -a rhoai-3.5-ansible rhoai-3.5-ansible_local

# 2. 環境資材を旧ディレクトリから持ち込む
cd rhoai-3.5-ansible_local
for d in vendor .venv .local collections .kube .credentials; do
  cp -a ../ansible/$d . 2>/dev/null
done
cp -a ../ansible/uv.lock ../ansible/.generated-passwords.yml .
cp -a ../ansible/.maas-policies-generated.yaml . 2>/dev/null
cp -a ../ansible/inventory/<env>/group_vars/all/vault.yml inventory/<env>/group_vars/all/

# 3. オフライン用の pyproject 設定を足す
cat >> pyproject.toml <<'EOF'

[tool.uv]
find-links = ["vendor/wheels"]
EOF

# 4. .envrc
cat > .envrc <<'EOF'
export KUBECONFIG="${PWD}/.kube/config"
if [ -e .venv/ ]; then
  source .venv/bin/activate
fi
EOF
```

### `.generated-passwords.yml` は必須

**これを持ち込まずに playbook を実行してはいけない。**

`roles/preflight/tasks/main.yml` がこのファイルを `include_vars`（`ignore_errors: true`）で
読み、読めなければ `maas_db_password` / `ogx_db_password` / `keycloak_db_password` を
**新規生成する**。再生成された値は以下へ無条件に適用される。

| Secret | 場所 |
|---|---|
| `maas-db-config` | `roles/rhoai/tasks/main.yml`（ガードなし。適用後 `maas-api` を rollout restart） |
| `ogx-postgres-credentials` | `roles/ogx/tasks/main.yml`（ガードなし） |

Postgres 本体のパスワードは PVC 上のデータに残るため、Secret だけ新しくなって
**MaaS と OGX が認証エラーになる**。Keycloak の DB は StatefulSet 既存チェックがあるため
比較的安全だが、OIDC クライアントシークレットは再生成される。

### `.credentials/` は必須ではない

`roles/keycloak/tasks/manage_maas_access.yml` は Keycloak に存在しないユーザーにしか
パスワードを発行しない（`when: item.json | length == 0`）。既存ユーザーの
パスワードが再生成されることはないので、参照用に持っておけばよい。

---

## 4. venv の絶対パス書き換え（重要）

`.venv` には**作成時のパスが焼き込まれている**。ディレクトリ名や配置先が変わると動かない。

```bash
head -1 .venv/bin/ansible-playbook
#!/root/old/path/ansible/.venv/bin/python   ← 旧パス
grep ^home .venv/pyvenv.cfg
```

配置先に合わせて書き換える。

```bash
cd /path/to/rhoai-3.5-ansible_local
OLD='/root/old/path/ansible'
NEW="$PWD"

# 通常ファイル（pyvenv.cfg + bin/ の console script / activate 各種）
find .venv .local collections vendor -type f -print0 2>/dev/null \
  | xargs -0 grep -Il "$OLD" 2>/dev/null \
  | xargs sed -i "s#${OLD}#${NEW}#g"

# python のシンボリックリンク
ln -sfn "${NEW}/.local/python/bin/python3" .venv/bin/python

# 残存確認（0 件になること）
find . -lname "*${OLD}*" 2>/dev/null
find .venv .local collections vendor -type f -print0 2>/dev/null \
  | xargs -0 grep -Il "$OLD" 2>/dev/null | wc -l
```

> `grep -r` はリポジトリの `.gitignore` を尊重する実装（ripgrep 系）のことがあり、
> `.venv/.gitignore` が `*` のため `.venv` 配下を丸ごとスキップする。
> 上記のように `find` + `xargs grep` を使うこと。

アーキテクチャが違う場合（macOS で作った venv を Linux に持ち込む等）は
書き換えでは直らない。`vendor/` から `bash scripts/setup-env.sh` で作り直す。
`vendor/python` には linux-aarch64 / linux-x86_64 / macOS 両アーキが入っている。

---

## 5. 動作確認

```bash
cd /path/to/rhoai-3.5-ansible_local
ls -l .generated-passwords.yml          # -rw------- であること
source .venv/bin/activate && ansible-playbook --version
oc whoami                               # playbook は自動 login しない
```

### `Failed to spawn: ansible / Permission denied (os error 13)`

`uv run` が `.venv/bin/ansible` を見つけたうえで `execve` に失敗している。原因は4つ。

| 確認 | 原因 | 対処 |
|---|---|---|
| `ls -l .venv/bin/ansible` が `-rw-` | 転送で実行ビットが落ちた | `chmod +x .venv/bin/* .local/bin/* .local/python/bin/*` |
| `.venv/bin/python -V` が落ちる | interpreter 側が実行不可 / dangling | 上記の書き換え、`chmod +x` |
| `findmnt -no TARGET,OPTIONS -T "$PWD"` に `noexec` | マウントオプション | `mount -o remount,exec <mp>` か別の場所へ |
| `getenforce` が Enforcing かつ `ls -Z` が `bin_t` 以外 | SELinux | `restorecon -Rv .venv .local` |

---

## 6. 更新の配布

playbook 本体だけを更新するときは tarball を使う。環境資材と秘密情報は除外される。

```bash
# 送信側
bash pack-playbook.sh rhoai-3.5-ansible_local -o /tmp/pb.tar.gz   # 約 200KB

# 受信側
cd <playbook の親ディレクトリ>
python3 unpack-playbook.py --diff-only /tmp/pb.tar.gz   # 差分確認
python3 unpack-playbook.py /tmp/pb.tar.gz               # 適用
```

> `pack-playbook.sh` は `uv.lock` も除外する（環境固有のため）。
> `[tool.uv] find-links` を入れた `pyproject.toml` は**除外されない**ので、
> オフライン用ディレクトリからパックすること。

tar 経由だと実行ビットが落ちることがある。適用後に確認する。

```bash
chmod +x scripts/*.sh
```

---

## 7. inventory の注意

`inventory/sample` は `.sample` 拡張子のため、**Ansible の group_vars として読まれない**。

```bash
ansible-playbook site.yml -i inventory/sample   # 変数が空になる
```

新しい環境を作るときは拡張子を外してコピーする。

```bash
mkdir -p inventory/myenv/group_vars/all
cp inventory/sample/hosts.yml inventory/myenv/
for f in cluster components vault; do
  cp inventory/sample/group_vars/all/$f.yml.sample inventory/myenv/group_vars/all/$f.yml
done
```
