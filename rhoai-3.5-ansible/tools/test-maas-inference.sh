#!/usr/bin/env bash
# MaaS Gateway 経由で LLM 推論をテストする
# Usage:
#   ./tools/test-maas-inference.sh <API_KEY> [MODEL_NAME] [PROMPT]
#
# Examples:
#   ./tools/test-maas-inference.sh maas-xxxxxx
#   ./tools/test-maas-inference.sh maas-xxxxxx my-model "日本の首都は？"
#
# API Key は以下で取得可能:
#   cat .vllm-token
#   または MaaS Dashboard から発行
set -euo pipefail

API_KEY="${1:?Usage: $0 <API_KEY> [MODEL_NAME] [PROMPT]}"
PROMPT="${3:-Hello, this is a test. Reply in one sentence.}"

CLUSTER_DOMAIN=$(oc get ingresses.config.openshift.io cluster -o jsonpath='{.spec.domain}')
MAAS_HOST="maas.${CLUSTER_DOMAIN}"

if [[ -z "${2:-}" ]]; then
    echo "=== 利用可能なモデル一覧を取得中... ==="
    MODELS_RESPONSE=$(curl -sk \
        "https://${MAAS_HOST}/v1/models" \
        -H "Authorization: Bearer ${API_KEY}" 2>/dev/null) || true

    if echo "${MODELS_RESPONSE}" | python3 -m json.tool >/dev/null 2>&1; then
        echo "${MODELS_RESPONSE}" | python3 -c "
import json, sys
data = json.load(sys.stdin)
models = data.get('data', [])
if not models:
    print('  (モデルが見つかりません)')
    sys.exit(0)
for m in models:
    print(f\"  - {m.get('id', '(unknown)')}\")
"
    else
        echo "  モデル一覧の取得に失敗しました"
        echo "  レスポンス: ${MODELS_RESPONSE}"
    fi

    MODEL_NAME=$(curl -sk \
        "https://${MAAS_HOST}/v1/models" \
        -H "Authorization: Bearer ${API_KEY}" 2>/dev/null \
        | python3 -c "import json,sys; data=json.load(sys.stdin); print(data['data'][0]['id'])" 2>/dev/null) || {
        echo "ERROR: モデル一覧の取得に失敗しました。API Key が正しいか確認してください。"
        exit 1
    }
    echo ""
    echo "=== 最初のモデル '${MODEL_NAME}' を使用します ==="
else
    MODEL_NAME="$2"
fi

echo ""
echo "=== 推論リクエスト送信 ==="
echo "  Endpoint: https://${MAAS_HOST}/v1/chat/completions"
echo "  Model:    ${MODEL_NAME}"
echo "  Prompt:   ${PROMPT}"
echo ""

RESPONSE=$(curl -sk -w "\n---HTTP_CODE:%{http_code}---" \
    "https://${MAAS_HOST}/v1/chat/completions" \
    -H "Authorization: Bearer ${API_KEY}" \
    -H "Content-Type: application/json" \
    -d "$(python3 -c "
import json
print(json.dumps({
    'model': '${MODEL_NAME}',
    'messages': [{'role': 'user', 'content': $(python3 -c "import json; print(json.dumps('${PROMPT}'))")}],
    'max_tokens': 256,
    'temperature': 0.7
}))
")")

HTTP_CODE=$(echo "${RESPONSE}" | grep -o 'HTTP_CODE:[0-9]*' | cut -d: -f2)
BODY=$(echo "${RESPONSE}" | sed 's/---HTTP_CODE:[0-9]*---$//')

echo "=== レスポンス (HTTP ${HTTP_CODE}) ==="
if [[ "${HTTP_CODE}" == "200" ]]; then
    echo "${BODY}" | python3 -c "
import json, sys
data = json.load(sys.stdin)
choice = data.get('choices', [{}])[0]
msg = choice.get('message', {}).get('content', '(no content)')
usage = data.get('usage', {})
print(f'回答: {msg}')
print()
print(f'トークン使用量:')
print(f'  prompt:     {usage.get(\"prompt_tokens\", \"N/A\")}')
print(f'  completion: {usage.get(\"completion_tokens\", \"N/A\")}')
print(f'  total:      {usage.get(\"total_tokens\", \"N/A\")}')
" 2>/dev/null || echo "${BODY}"
    echo ""
    echo "SUCCESS: 推論が正常に完了しました"
else
    echo "${BODY}" | python3 -m json.tool 2>/dev/null || echo "${BODY}"
    echo ""
    echo "FAILED: HTTP ${HTTP_CODE}"
    exit 1
fi
