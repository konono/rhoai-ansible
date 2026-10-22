#!/usr/bin/env bash
set -euo pipefail
#
# MaaS で公開中のモデルを opencode から使えるようにする。
#
#   bash scripts/setup-opencode.sh setup     # API キー発行 + opencode.json 生成
#   bash scripts/setup-opencode.sh show      # 現在の設定を表示
#   bash scripts/setup-opencode.sh test      # 疎通確認
#
# 生成物（いずれも playbook ディレクトリ直下）:
#   .vllm-token    発行した MaaS API キー (0600)
#   opencode.json  opencode の設定
#
# 動作:
#   - クラスタから Gateway のホスト名と present なモデルを自動検出する
#   - LLMInferenceService の --max-model-len を読んで limit.context に反映する
#   - API キーは .vllm-token が既にあれば再利用する (再発行しない)
#
# 環境変数:
#   OPENCODE_API_USER   キーを発行するユーザー名 (既定: admin1)
#   OPENCODE_API_GROUPS キーに紐づけるグループ (JSON 配列。既定: inventory から
#                       そのユーザーの groups を引く。引けなければ ["maas-admins"])
#   OPENCODE_INVENTORY  グループ検索に使う inventory ディレクトリ
#   OPENCODE_MODEL      既定モデルのキー (既定: 最初に見つかった present モデル)
#
# 発行済みキーの使い回しは「同じユーザー かつ 同じグループ」のときだけ行う。
# ユーザーを切り替えて再実行すると新しいキーを発行して差し替える。
#

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ANSIBLE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TOKEN_FILE="${ANSIBLE_DIR}/.vllm-token"
# どのユーザー / グループで発行したキーかを記録する。
# これが無いと別ユーザーで再実行しても前のキーを使い回してしまう。
TOKEN_META="${ANSIBLE_DIR}/.vllm-token.meta"
CONFIG_FILE="${ANSIBLE_DIR}/opencode.json"
API_USER="${OPENCODE_API_USER:-admin1}"
GATEWAY_NS=openshift-ingress
GATEWAY_NAME=maas-default-gateway
MAAS_NS=redhat-ai-gateway-infra

die() { echo "ERROR: $*" >&2; exit 1; }

require_cluster() {
    command -v oc >/dev/null 2>&1 || die "oc が見つかりません"
    oc whoami >/dev/null 2>&1 || die "クラスタにログインしていません (oc login)"
}

# Gateway の listener hostname を取得する
get_maas_host() {
    oc get gateway "$GATEWAY_NAME" -n "$GATEWAY_NS" \
        -o jsonpath='{.spec.listeners[0].hostname}' 2>/dev/null
}

# present なモデル（LLMInferenceService が Ready なもの）を
# "<name> <namespace> <max-model-len>" の形式で列挙する
list_models() {
    oc get llminferenceservice -A -o json | python3 -c '
import sys, json, re
for r in json.load(sys.stdin)["items"]:
    st = r.get("status", {})
    ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                for c in st.get("conditions", []))
    if not ready:
        continue
    args = (r["spec"].get("template", {}).get("containers") or [{}])[0].get("args", [])
    ctx = 0
    for a in args:
        m = re.match(r"^--max-model-len[= ](\d+)$", a)
        if m:
            ctx = int(m.group(1))
    print(r["metadata"]["name"], r["metadata"]["namespace"], ctx)
'
}

# API キーを発行する（.vllm-token があれば再利用）
# 発行対象ユーザーのグループを決める。
# 明示指定 > inventory の keycloak_users > maas-admins の順。
resolve_api_groups() {
    if [ -n "${OPENCODE_API_GROUPS:-}" ]; then
        printf '%s' "$OPENCODE_API_GROUPS"
        return
    fi
    local inv="${OPENCODE_INVENTORY:-}"
    if [ -z "$inv" ]; then
        # sample 以外の inventory が 1 つだけならそれを使う
        mapfile -t _invs < <(find "${ANSIBLE_DIR}/inventory" -maxdepth 1 -mindepth 1 \
                               -type d ! -name sample -printf '%p\n' 2>/dev/null | sort)
        [ "${#_invs[@]}" -eq 1 ] && inv="${_invs[0]}"
    fi
    if [ -n "$inv" ] && [ -f "$inv/group_vars/all/components.yml" ]; then
        python3 - "$inv/group_vars/all/components.yml" "$API_USER" <<'PY'
import json, sys, yaml
try:
    d = yaml.safe_load(open(sys.argv[1])) or {}
except Exception:
    d = {}
for u in d.get("keycloak_users") or []:
    if u.get("username") == sys.argv[2]:
        g = u.get("groups") or []
        if g:
            print(json.dumps(g))
            sys.exit()
print(json.dumps(["maas-admins"]))
PY
        return
    fi
    printf '%s' '["maas-admins"]'
}

ensure_api_key() {
    local groups
    groups="$(resolve_api_groups)"

    if [ -s "$TOKEN_FILE" ] && [ -f "$TOKEN_META" ]; then
        local prev_user prev_groups
        prev_user="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("user",""))' "$TOKEN_META" 2>/dev/null)"
        prev_groups="$(python3 -c 'import json,sys;print(json.dumps(json.load(open(sys.argv[1])).get("groups")))' "$TOKEN_META" 2>/dev/null)"
        if [ "$prev_user" = "$API_USER" ] && [ "$prev_groups" = "$(python3 -c 'import json,sys;print(json.dumps(json.loads(sys.argv[1])))' "$groups")" ]; then
            echo "既存の API キーを使用します (${API_USER}): ${TOKEN_FILE}" >&2
            cat "$TOKEN_FILE"
            return
        fi
        echo "発行時のユーザー/グループが異なるため再発行します" >&2
        echo "  前回: ${prev_user} ${prev_groups}" >&2
        echo "  今回: ${API_USER} ${groups}" >&2
    elif [ -s "$TOKEN_FILE" ]; then
        # 旧バージョンが作った meta 無しのキーは所有者が不明なので再発行する
        echo "発行時のユーザーが不明なキーがあるため再発行します: ${TOKEN_FILE}" >&2
    fi

    local pod
    pod="$(oc get pods -n "$MAAS_NS" -l app.kubernetes.io/name=maas-api \
            -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)"
    [ -n "$pod" ] || die "maas-api Pod が見つかりません (namespace: ${MAAS_NS})"

    local resp key
    resp="$(oc exec -n "$MAAS_NS" "$pod" -- curl -skfS \
        -X POST https://localhost:8443/v1/api-keys \
        -H "X-MaaS-Username: ${API_USER}" \
        -H "X-MaaS-Group: ${groups}" \
        -H 'Content-Type: application/json' \
        -d "{\"name\":\"opencode-${API_USER}\"}" 2>/dev/null)" \
        || die "API キーの発行に失敗しました"

    key="$(printf '%s' "$resp" | python3 -c 'import sys,json; print(json.load(sys.stdin)["key"])')" \
        || die "API キーの応答を解析できませんでした: ${resp}"

    ( umask 077; printf '%s' "$key" > "$TOKEN_FILE" )
    ( umask 077; python3 -c 'import json,sys;json.dump({"user":sys.argv[1],"groups":json.loads(sys.argv[2])},open(sys.argv[3],"w"))' \
        "$API_USER" "$groups" "$TOKEN_META" )
    echo "API キーを発行しました: ${TOKEN_FILE} (user=${API_USER} groups=${groups})" >&2
    printf '%s' "$key"
}

cmd_setup() {
    require_cluster

    local host
    host="$(get_maas_host)"
    [ -n "$host" ] || die "Gateway ${GATEWAY_NAME} の hostname を取得できません"

    local models
    models="$(list_models)"
    [ -n "$models" ] || die "Ready な LLMInferenceService がありません"

    ensure_api_key >/dev/null

    # opencode.json を生成する。
    # python3 - にヒアドキュメントでプログラムを渡すと stdin が占有されるため、
    # モデル一覧はパイプではなく一時ファイル経由で渡す。
    local models_file
    models_file="$(mktemp)"
    trap 'rm -f "$models_file"' RETURN
    printf '%s\n' "$models" > "$models_file"

    python3 - "$host" "$CONFIG_FILE" "${OPENCODE_MODEL:-}" "$models_file" <<'PYEOF'
import sys, json, os

host, config_path, default_model, models_file = sys.argv[1:5]

providers = {}
first = None
with open(models_file) as _f:
    _lines = _f.read().splitlines()
for line in _lines:
    if not line.strip():
        continue
    name, ns, ctx = line.split()
    ctx = int(ctx) or 32768
    if first is None:
        first = name
    # MaaS はモデルごとにパスが分かれるため、プロバイダもモデルごとに作る
    # vLLM の --max-model-len は「入力 + 出力」の合計上限。
    # opencode の limit.context は入力のみ、limit.output は生成分なので、
    # context + output が --max-model-len を超えないように配分する。
    out = 32768 if ctx >= 131072 else max(2048, ctx // 4)
    out = min(out, ctx // 2)
    providers[name] = {
        "npm": "@ai-sdk/openai-compatible",
        "name": f"RHOAI MaaS ({name})",
        "options": {
            # .vllm-token は opencode.json と同じディレクトリに置く
            "baseURL": f"https://{host}/{ns}/{name}/v1",
            "apiKey": "{file:./.vllm-token}",
        },
        "models": {
            name: {
                "name": name,
                "limit": {"context": ctx - out, "output": out},
                # --reasoning-parser qwen3 により思考は reasoning_content に分離される
                "reasoning": True,
                "interleaved": {"field": "reasoning_content"},
            }
        },
    }

sel = default_model or first
if sel not in providers:
    sys.exit(f"ERROR: モデル '{sel}' が見つかりません。候補: {', '.join(providers)}")

cfg = {
    "$schema": "https://opencode.ai/config.json",
    "model": f"{sel}/{sel}",
    "provider": providers,
}

# 既存の設定があれば provider / model 以外のユーザー設定は保持する
if os.path.exists(config_path):
    try:
        with open(config_path) as f:
            old = json.load(f)
        for k, v in old.items():
            if k not in ("$schema", "model", "provider"):
                cfg[k] = v
    except (json.JSONDecodeError, OSError):
        pass

with open(config_path, "w") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
    f.write("\n")

print(f"opencode.json を生成しました: {config_path}")
print(f"  既定モデル: {cfg['model']}")
for n, p in providers.items():
    print(f"  {n}: {p['options']['baseURL']}  (context={list(p['models'].values())[0]['limit']['context']})")
PYEOF

    cat <<EOF

使い方:
  cd ${ANSIBLE_DIR}
  opencode                       # このディレクトリで起動すると opencode.json を読む
  opencode run "こんにちは"       # 単発実行

モデルを切り替える:
  opencode 内で /models

別の場所から使う場合は設定をグローバルへコピーする:
  mkdir -p ~/.config/opencode
  cp ${CONFIG_FILE} ~/.config/opencode/opencode.json
  cp ${TOKEN_FILE}  ~/.config/opencode/.vllm-token
EOF
}

cmd_show() {
    [ -f "$CONFIG_FILE" ] || die "${CONFIG_FILE} がありません。先に setup を実行してください"
    python3 -m json.tool "$CONFIG_FILE"
    echo
    if [ -s "$TOKEN_FILE" ]; then
        echo "API キー: ${TOKEN_FILE} ($(wc -c < "$TOKEN_FILE") bytes)"
        [ -f "$TOKEN_META" ] && echo "  発行先: $(cat "$TOKEN_META")"
    else
        echo "API キー: 未発行"
    fi
}

cmd_test() {
    require_cluster
    [ -f "$CONFIG_FILE" ] || die "${CONFIG_FILE} がありません。先に setup を実行してください"
    [ -s "$TOKEN_FILE" ] || die "${TOKEN_FILE} がありません。先に setup を実行してください"

    local base model
    read -r model base < <(python3 -c '
import json, sys
cfg = json.load(open(sys.argv[1]))
pid, mid = cfg["model"].split("/", 1)
print(mid, cfg["provider"][pid]["options"]["baseURL"])
' "$CONFIG_FILE")

    echo "モデル: ${model}"
    echo "エンドポイント: ${base}"
    echo "--- /v1/models ---"
    curl -sk "${base}/models" -H "Authorization: Bearer $(cat "$TOKEN_FILE")" \
        | python3 -m json.tool || true
    echo "--- /v1/chat/completions ---"
    curl -sk "${base}/chat/completions" \
        -H "Authorization: Bearer $(cat "$TOKEN_FILE")" \
        -H 'Content-Type: application/json' \
        -d "{\"model\":\"${model}\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":16,\"chat_template_kwargs\":{\"enable_thinking\":false}}" \
        -w '\nHTTP %{http_code}\n'
}

case "${1:-setup}" in
    setup) cmd_setup ;;
    show)  cmd_show ;;
    test)  cmd_test ;;
    *) echo "Usage: bash scripts/setup-opencode.sh {setup|show|test}" >&2; exit 1 ;;
esac
