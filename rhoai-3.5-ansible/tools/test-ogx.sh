#!/usr/bin/env bash
# OGX Server (Llama Stack) の機能テスト
#
# verify.yml は OGXServer CR が Ready かどうかしか見ない。「Ready なのに
# API が動かない」「vLLM に繋がっておらずモデルが 0 件」は検出できないため、
# 実際に API を叩いて確認する。
#
# 確認する内容:
#   - Pod の存在と health
#   - 登録済み Providers
#   - 登録済み Models (0 件なら vLLM 接続を疑う)
#   - Chat Completion
#   - Conversations の作成・削除
#   - Files のアップロード・削除
#
# Usage:
#   ./tools/test-ogx.sh [MODEL_NAME] [NAMESPACE]
#
# Examples:
#   ./tools/test-ogx.sh                      # モデルは自動検出
#   ./tools/test-ogx.sh qwen3-5-9b
#
# テストで作ったリソース (Conversation / File) はその場で削除する。
set -euo pipefail

MODEL="${1:-}"
NS="${2:-redhat-ods-applications}"
OGX_URL="http://localhost:8321"

pass=0
fail=0
ok()   { echo "  [OK]   $*"; pass=$((pass + 1)); }
ng()   { echo "  [NG]   $*"; fail=$((fail + 1)); }
warn() { echo "  [WARN] $*"; }

echo "=== OGX Server 動作確認 ==="
echo "  Namespace: ${NS}"
echo ""

# grep が一致しないと pipefail で代入が失敗し、set -e で下の案内に辿り着かない
POD=$(oc get pods -n "${NS}" --no-headers 2>/dev/null \
    | grep ogx-server | grep -v operator | grep Running | awk '{print $1}' | head -1) || true
if [[ -z "${POD}" ]]; then
    echo "  OGX Server Pod が Running ではありません (namespace: ${NS})" >&2
    oc get pods -n "${NS}" --no-headers 2>/dev/null | grep -i ogx || true
    exit 1
fi
echo "  Pod: ${POD}"
echo ""

ogx_curl() {
    oc exec "${POD}" -n "${NS}" -- curl -sk --max-time 120 "$@" 2>/dev/null || true
}

echo "--- Step 1: health ---"
if ogx_curl "${OGX_URL}/v1/health" | grep -q '"status"'; then
    ok "health OK"
else
    ng "health check 失敗"
    echo ""
    echo "  Pod のログを確認してください:"
    echo "    oc logs -n ${NS} ${POD} --tail=50"
    exit 1
fi
echo ""

echo "--- Step 2: Providers ---"
ogx_curl "${OGX_URL}/v1/providers" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("  (取得失敗)"); raise SystemExit(1)
for p in d.get("data", []):
    print("    [%s] %s (%s)" % (p.get("api"), p.get("provider_id"), p.get("provider_type")))
' && ok "Providers 取得" || ng "Providers 取得失敗"
echo ""

echo "--- Step 3: Models ---"
MODELS_JSON=$(ogx_curl "${OGX_URL}/v1/models")
MODEL_COUNT=$(printf '%s' "${MODELS_JSON}" \
    | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("data",[])))' 2>/dev/null || echo 0)
if [[ "${MODEL_COUNT}" -gt 0 ]]; then
    ok "Models: ${MODEL_COUNT} 件登録済み"
    printf '%s' "${MODELS_JSON}" | python3 -c '
import json, sys
for m in json.load(sys.stdin).get("data", []):
    print("    - %s" % (m.get("identifier") or m.get("id")))
' 2>/dev/null || true
else
    # モデルが 1 件も無ければ推論できない。OGX の中心機能が死んでいるので失敗にする
    ng "Models: 0 件 — vLLM 接続を疑ってください"
    echo "    oc get secret ogx-vllm-connection -n ${NS}"
    echo "    oc logs -n ${NS} ${POD} --tail=50"
fi
echo ""

# モデル未指定なら OGX に登録されているものを使う
if [[ -z "${MODEL}" && "${MODEL_COUNT}" -gt 0 ]]; then
    MODEL=$(printf '%s' "${MODELS_JSON}" | python3 -c '
import json, sys
d = json.load(sys.stdin).get("data", [])
if d:
    print(d[0].get("identifier") or d[0].get("id") or "")
' 2>/dev/null || true)
    [[ -n "${MODEL}" ]] && echo "  モデル未指定のため ${MODEL} を使います" && echo ""
fi

echo "--- Step 4: Chat Completion ---"
if [[ "${MODEL_COUNT}" -gt 0 && -n "${MODEL}" ]]; then
    CHAT=$(ogx_curl -X POST "${OGX_URL}/v1/chat/completions" \
        -H "Content-Type: application/json" \
        -d "$(python3 -c '
import json, sys
# Qwen3 系は reasoning モデルで、思考だけで max_tokens を使い切ると
# content が空のまま finish_reason=length で返る。検証では思考を切る。
print(json.dumps({
    "model": sys.argv[1],
    "messages": [{"role": "user", "content": "Say hello in one short sentence."}],
    "max_tokens": 64,
    "chat_template_kwargs": {"enable_thinking": False},
}))' "${MODEL}")")
    if printf '%s' "${CHAT}" | grep -q '"choices"'; then
        ANS=$(printf '%s' "${CHAT}" | python3 -c '
import json, sys
d = json.load(sys.stdin)
c = d["choices"][0]
msg = (c["message"].get("content") or "").strip()
if not msg:
    r = (d.get("usage") or {}).get("completion_tokens_details") or {}
    msg = "(空) finish_reason=%s reasoning_tokens=%s" % (
        c.get("finish_reason"), r.get("reasoning_tokens"))
print(msg[:160])
' 2>/dev/null || echo "")
        ok "Chat Completion: ${ANS}"
    else
        ng "Chat Completion 失敗: $(printf '%s' "${CHAT}" | head -c 200)"
    fi
else
    # Models 0 件は上で NG 済み。ここでスキップしても成功にはしない
    ng "Models が 0 件のため Chat Completion を実行できません"
fi
echo ""

echo "--- Step 5: Conversations (作成 → 削除) ---"
CONV_ID=$(ogx_curl -X POST "${OGX_URL}/v1/conversations" \
    -H "Content-Type: application/json" -d '{"name":"verify-test"}' \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id",""))' 2>/dev/null || true)
if [[ -n "${CONV_ID}" ]]; then
    ok "Conversation 作成: ${CONV_ID}"
    ogx_curl -X DELETE "${OGX_URL}/v1/conversations/${CONV_ID}" >/dev/null
    # 削除できたかは「もう引けないこと」で確かめる。応答を見ずに OK とすると
    # テストデータが残り続ける
    if ogx_curl -o /dev/null -w '%{http_code}' "${OGX_URL}/v1/conversations/${CONV_ID}" \
         | grep -qE '^(404|400)$'; then
        ok "Conversation 削除"
    else
        ng "Conversation が削除されていません: ${CONV_ID}"
    fi
else
    ng "Conversation 作成失敗"
fi
echo ""

echo "--- Step 6: Files (アップロード → 削除) ---"
FILE_ID=$(oc exec "${POD}" -n "${NS}" -- bash -c '
    f=$(mktemp /tmp/ogx-verify-XXXXXX.txt)
    echo "Test document for verification." > "$f"
    curl -sk --max-time 60 -X POST '"${OGX_URL}"'/v1/files \
      -F "file=@$f" -F "purpose=assistants"
    rm -f "$f"
' 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id",""))' 2>/dev/null || true)
if [[ -n "${FILE_ID}" ]]; then
    ok "File アップロード: ${FILE_ID}"
    ogx_curl -X DELETE "${OGX_URL}/v1/files/${FILE_ID}" >/dev/null
    if ogx_curl -o /dev/null -w '%{http_code}' "${OGX_URL}/v1/files/${FILE_ID}" \
         | grep -qE '^(404|400)$'; then
        ok "File 削除"
    else
        ng "File が削除されていません: ${FILE_ID}"
    fi
else
    ng "File アップロード失敗"
fi
echo ""

echo "=== 結果: OK ${pass} / NG ${fail} ==="
[[ "${fail}" -eq 0 ]]
