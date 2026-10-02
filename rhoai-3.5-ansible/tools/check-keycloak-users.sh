#!/usr/bin/env bash
# Keycloak のユーザー・グループ割当を確認する
# Usage:
#   ./tools/check-keycloak-users.sh [REALM] [NAMESPACE]
#
# デフォルト: realm=maas, namespace=keycloak
set -euo pipefail

REALM="${1:-maas}"
KC_NAMESPACE="${2:-keycloak}"

CLUSTER_DOMAIN=$(oc get ingresses.config.openshift.io cluster -o jsonpath='{.spec.domain}')
KC_URL="https://keycloak-${KC_NAMESPACE}.${CLUSTER_DOMAIN}"

echo "=== Keycloak ユーザー/グループ確認 ==="
echo "  URL:       ${KC_URL}"
echo "  Realm:     ${REALM}"
echo "  Namespace: ${KC_NAMESPACE}"
echo ""

# Admin credentials 取得
echo "--- Admin トークン取得 ---"
KC_ADMIN_USER=$(oc get secret keycloak-initial-admin -n "${KC_NAMESPACE}" \
    -o jsonpath='{.data.username}' | base64 -d 2>/dev/null) || KC_ADMIN_USER="admin"

KC_ADMIN_PASS=$(oc get secret keycloak-initial-admin -n "${KC_NAMESPACE}" \
    -o jsonpath='{.data.password}' | base64 -d 2>/dev/null) || {
    echo "ERROR: Keycloak admin パスワードの取得に失敗しました"
    exit 1
}

TOKEN_RESP=$(curl -sk \
    "${KC_URL}/realms/master/protocol/openid-connect/token" \
    -d "client_id=admin-cli" \
    -d "username=${KC_ADMIN_USER}" \
    -d "password=${KC_ADMIN_PASS}" \
    -d "grant_type=password")

TOKEN=$(echo "${TOKEN_RESP}" | python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])" 2>/dev/null) || {
    echo "ERROR: Admin トークン取得に失敗しました"
    echo "${TOKEN_RESP}" | python3 -m json.tool 2>/dev/null || echo "${TOKEN_RESP}"
    exit 1
}
echo "  OK: Admin トークン取得成功"
echo ""

# グループ一覧
echo "--- グループ一覧 ---"
GROUPS_RESP=$(curl -sk \
    "${KC_URL}/admin/realms/${REALM}/groups" \
    -H "Authorization: Bearer ${TOKEN}")

echo "${GROUPS_RESP}" | python3 -c "
import json, sys
groups = json.load(sys.stdin)
if not groups:
    print('  (グループなし)')
else:
    for g in groups:
        members = g.get('memberCount', '?')
        print(f\"  - {g['name']} (members: {members})\")
        for sub in g.get('subGroups', []):
            print(f\"    - {sub['name']} (members: {sub.get('memberCount', '?')})\")
" 2>/dev/null || echo "  グループ一覧取得失敗"
echo ""

# ユーザー一覧とグループ割当
echo "--- ユーザー一覧とグループ割当 ---"
USERS_RESP=$(curl -sk \
    "${KC_URL}/admin/realms/${REALM}/users?max=100" \
    -H "Authorization: Bearer ${TOKEN}")

echo "${USERS_RESP}" | python3 -c "
import json, sys, urllib.request, ssl
users = json.load(sys.stdin)
if not users:
    print('  (ユーザーなし)')
    sys.exit(0)
for u in users:
    uid = u['id']
    username = u.get('username', '(unknown)')
    enabled = u.get('enabled', False)
    status = 'enabled' if enabled else 'disabled'
    print(f'  {username} ({status})')
" 2>/dev/null || echo "  ユーザー一覧取得失敗"
echo ""

# 各ユーザーのグループ所属を取得
echo "--- ユーザー別グループ所属 ---"
echo "${USERS_RESP}" | python3 -c "
import json, sys, subprocess
users = json.load(sys.stdin)
token = '${TOKEN}'
kc_url = '${KC_URL}'
realm = '${REALM}'
for u in users:
    uid = u['id']
    username = u.get('username', '(unknown)')
    result = subprocess.run(
        ['curl', '-sk',
         f'{kc_url}/admin/realms/{realm}/users/{uid}/groups',
         '-H', f'Authorization: Bearer {token}'],
        capture_output=True, text=True
    )
    try:
        groups = json.loads(result.stdout)
        group_names = [g['name'] for g in groups]
    except:
        group_names = ['(取得失敗)']
    print(f'  {username}: {group_names}')
" 2>/dev/null || echo "  グループ所属取得失敗"
echo ""

# OAuth IdP 確認
echo "--- OpenShift OAuth IdP 確認 ---"
IDP_NAMES=$(oc get oauth cluster -o jsonpath='{.spec.identityProviders[*].name}' 2>/dev/null) || IDP_NAMES=""
if [[ -n "${IDP_NAMES}" ]]; then
    echo "  登録済み IdP: ${IDP_NAMES}"
    if echo "${IDP_NAMES}" | grep -q "keycloak"; then
        echo "  OK: Keycloak IdP が登録されています"
    else
        echo "  WARN: Keycloak IdP が見つかりません"
    fi
else
    echo "  WARN: OAuth IdP が取得できませんでした"
fi
echo ""

echo "SUCCESS: Keycloak 確認完了"
