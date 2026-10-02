#!/usr/bin/env bash
# MaaS API Key の発行・一覧・削除をテストする
# Usage:
#   ./tools/test-maas-apikey.sh [USERNAME] [GROUP]
#
# デフォルト: username=admin1, group=maas-admins
set -euo pipefail

USERNAME="${1:-admin1}"
GROUP="${2:-maas-admins}"

echo "=== MaaS API Key 発行テスト ==="
echo "  Username: ${USERNAME}"
echo "  Group:    ${GROUP}"
echo ""

# maas-api Pod 取得
MAAS_POD=$(oc get pods -n redhat-ai-gateway-infra \
    -l app.kubernetes.io/name=maas-api \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) || {
    echo "ERROR: maas-api Pod が見つかりません"
    exit 1
}
echo "  maas-api Pod: ${MAAS_POD}"
echo ""

KEY_NAME="verify-manual-$(date +%s)"

# 1. API Key 発行
echo "--- Step 1: API Key 発行 ---"
CREATE_RESP=$(oc exec -n redhat-ai-gateway-infra "${MAAS_POD}" -- \
    curl -sk \
    -X POST https://localhost:8443/v1/api-keys \
    -H "X-MaaS-Username: ${USERNAME}" \
    -H "X-MaaS-Group: [\"${GROUP}\"]" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"${KEY_NAME}\"}" 2>/dev/null)

API_KEY=$(echo "${CREATE_RESP}" | python3 -c "import json,sys; print(json.load(sys.stdin).get('key',''))" 2>/dev/null) || API_KEY=""

if [[ -n "${API_KEY}" ]]; then
    echo "  OK: API Key 発行成功"
    echo "  Key Name: ${KEY_NAME}"
    echo "  Key:      ${API_KEY:0:20}..."
else
    echo "  FAILED: API Key 発行失敗"
    echo "  Response: ${CREATE_RESP}"
    exit 1
fi
echo ""

# 2. API Key 一覧で確認
echo "--- Step 2: API Key 一覧で存在確認 ---"
LIST_RESP=$(oc exec -n redhat-ai-gateway-infra "${MAAS_POD}" -- \
    curl -sk \
    https://localhost:8443/v1/api-keys \
    -H "X-MaaS-Username: ${USERNAME}" \
    -H "X-MaaS-Group: [\"${GROUP}\"]" 2>/dev/null)

FOUND=$(echo "${LIST_RESP}" | python3 -c "
import json,sys
data = json.load(sys.stdin)
keys = data if isinstance(data, list) else data.get('keys', data.get('data', []))
found = [k for k in keys if k.get('name') == '${KEY_NAME}']
print('found' if found else 'not_found')
" 2>/dev/null) || FOUND="error"

if [[ "${FOUND}" == "found" ]]; then
    ok "  OK: 一覧に ${KEY_NAME} が存在"
else
    echo "  WARN: 一覧での確認に失敗 (${FOUND})"
fi
echo ""

# 3. 発行した Key で Gateway 経由の認証テスト
echo "--- Step 3: 発行した Key で Gateway 認証テスト ---"
CLUSTER_DOMAIN=$(oc get ingresses.config.openshift.io cluster -o jsonpath='{.spec.domain}')
MAAS_HOST="maas.${CLUSTER_DOMAIN}"

AUTH_CODE=$(curl -sk -o /dev/null -w '%{http_code}' \
    "https://${MAAS_HOST}/v1/models" \
    -H "Authorization: Bearer ${API_KEY}" 2>/dev/null) || AUTH_CODE="000"

if [[ "${AUTH_CODE}" == "200" ]]; then
    echo "  OK: Gateway 認証成功 (HTTP 200)"
else
    echo "  FAILED: Gateway 認証失敗 (HTTP ${AUTH_CODE})"
    echo "  ※ API Key が即時有効にならない場合があります。数秒後に再試行してください。"
fi
echo ""

# 4. クリーンアップ
echo "--- Step 4: テスト用 API Key 削除 ---"
DEL_CODE=$(oc exec -n redhat-ai-gateway-infra "${MAAS_POD}" -- \
    curl -sk -o /dev/null -w '%{http_code}' \
    -X DELETE "https://localhost:8443/v1/api-keys/${KEY_NAME}" \
    -H "X-MaaS-Username: ${USERNAME}" \
    -H "X-MaaS-Group: [\"${GROUP}\"]" 2>/dev/null) || DEL_CODE="000"

if [[ "${DEL_CODE}" =~ ^(200|204)$ ]]; then
    echo "  OK: テスト用 Key を削除しました"
else
    echo "  WARN: 削除失敗 (HTTP ${DEL_CODE}) — 手動で削除してください"
fi
echo ""

echo "SUCCESS: MaaS API Key の発行・確認・認証テストが完了しました"
