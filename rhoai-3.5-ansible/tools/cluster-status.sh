#!/usr/bin/env bash
# クラスタの環境サマリーを表示する（URL・認証情報・APIキー）
# Playbook の実行結果に関係なく、いつでも単独で実行できる
#
# Usage:
#   ./tools/cluster-status.sh
#   ./tools/cluster-status.sh --no-secrets   # パスワード・トークンを非表示
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

SHOW_SECRETS=true
if [[ "${1:-}" == "--no-secrets" ]]; then
    SHOW_SECRETS=false
fi

# oc login 確認
if ! oc whoami &>/dev/null; then
    echo "ERROR: oc login していません"
    exit 1
fi

CLUSTER_DOMAIN=$(oc get ingresses.config.openshift.io cluster -o jsonpath='{.spec.domain}' 2>/dev/null) || CLUSTER_DOMAIN=""

echo
echo "========================================"
echo "  環境サマリー"
echo "========================================"
echo

# --- Dashboard URLs ---
echo "--- Dashboard URLs ---"

OCP_CONSOLE=$(oc get route console -n openshift-console -o jsonpath='https://{.spec.host}' 2>/dev/null) || true
RHOAI_DASHBOARD=$(oc get route rhods-dashboard -n redhat-ods-applications -o jsonpath='https://{.spec.host}' 2>/dev/null) || true
MAAS_HOST="maas.${CLUSTER_DOMAIN}"
DS_GATEWAY_HOST=$(oc get route -n openshift-ingress -o jsonpath='{range .items[*]}{.spec.host}{"\n"}{end}' 2>/dev/null | grep "^rh-ai\." | head -1) || true

MLFLOW_READY=$(oc get mlflow mlflow -n redhat-ods-applications -o jsonpath='{.status.conditions[?(@.type=="Available")].status}' 2>/dev/null) || true

echo "  OpenShift Console:  ${OCP_CONSOLE:-(未検出)}"
echo "  OpenShift AI:       ${RHOAI_DASHBOARD:-(未デプロイ)}"
if [[ "${MLFLOW_READY}" == "True" && -n "${DS_GATEWAY_HOST}" ]]; then
    echo "  MLflow UI:          https://${DS_GATEWAY_HOST}/mlflow"
elif [[ "${MLFLOW_READY}" == "True" ]]; then
    echo "  MLflow:             (デプロイ済み — Gateway 未検出)"
else
    echo "  MLflow:             (未デプロイ)"
fi
echo

# --- Keycloak ---
echo "--- Keycloak ---"
KC_URL=$(oc get route keycloak -n keycloak -o jsonpath='https://{.spec.host}' 2>/dev/null) || true
if [[ -n "${KC_URL}" ]]; then
    echo "  URL:       ${KC_URL}"
    if [[ "${SHOW_SECRETS}" == true ]]; then
        KC_ADMIN=$(oc get secret keycloak-initial-admin -n keycloak -o jsonpath='{.data.username}' 2>/dev/null | base64 -d 2>/dev/null) || KC_ADMIN=""
        KC_PASS=$(oc get secret keycloak-initial-admin -n keycloak -o jsonpath='{.data.password}' 2>/dev/null | base64 -d 2>/dev/null) || KC_PASS=""
        echo "  Admin:     ${KC_ADMIN} / ${KC_PASS}"
    else
        echo "  Admin:     (--no-secrets で非表示)"
    fi
else
    echo "  (未デプロイ)"
fi
echo

# --- ユーザーアカウント ---
echo "--- ユーザーアカウント ---"
CRED_DIR="${PROJECT_DIR}/.credentials"
if [[ -d "${CRED_DIR}" ]] && ls "${CRED_DIR}"/*.password &>/dev/null 2>&1; then
    for pwfile in "${CRED_DIR}"/*.password; do
        uname=$(basename "$pwfile" .password)
        if [[ "${SHOW_SECRETS}" == true ]]; then
            pw=$(cat "$pwfile")
            echo "  ${uname}: ${pw} (初回ログイン時にパスワード変更が必要)"
        else
            echo "  ${uname}: (--no-secrets で非表示)"
        fi
    done
else
    echo "  (パスワード情報なし — ユーザーが既に存在していた場合)"
fi
echo

# --- 推論エンドポイント ---
echo "--- 推論エンドポイント ---"
if [[ -n "${CLUSTER_DOMAIN}" ]]; then
    # デプロイ済みモデルを動的に検出
    MODEL_NAMES=$(oc get llminferenceservice --all-namespaces --no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null) || MODEL_NAMES=""
    if [[ -n "${MODEL_NAMES}" ]]; then
        while IFS= read -r model; do
            echo "  Endpoint:  https://${MAAS_HOST}/llm-serving/${model}/v1/chat/completions"
        done <<< "${MODEL_NAMES}"
    else
        echo "  Endpoint:  https://${MAAS_HOST}/llm-serving/<model>/v1/chat/completions"
        echo "             (モデル未検出 — LLMInferenceService が存在しません)"
    fi

    if [[ "${SHOW_SECRETS}" == true ]]; then
        if [[ -f "${PROJECT_DIR}/.vllm-token" ]]; then
            API_KEY=$(tr -d '[:space:]' < "${PROJECT_DIR}/.vllm-token")
            echo "  API Key:   ${API_KEY}"
        else
            echo "  API Key:   (未発行 — .vllm-token が存在しません)"
        fi
    else
        echo "  API Key:   (--no-secrets で非表示)"
    fi
else
    echo "  (未デプロイ)"
fi
echo

# --- MLflow API ---
echo "--- MLflow API ---"
if [[ "${MLFLOW_READY}" == "True" ]]; then
    MLFLOW_EXTERNAL=""
    if [[ -n "${DS_GATEWAY_HOST}" ]]; then
        MLFLOW_EXTERNAL="https://${DS_GATEWAY_HOST}/mlflow"
    fi
    MLFLOW_INTERNAL=$(oc get mlflow mlflow -n redhat-ods-applications -o jsonpath='{.status.address.url}' 2>/dev/null) || true

    [[ -n "${MLFLOW_EXTERNAL}" ]] && echo "  Tracking URI (外部):  ${MLFLOW_EXTERNAL}"
    [[ -n "${MLFLOW_INTERNAL}" ]] && echo "  Tracking URI (内部):  ${MLFLOW_INTERNAL}"
    echo "  Workspace:            mlflow-workspace"
    echo "  Header:               X-MLflow-Workspace: mlflow-workspace"

    if [[ "${SHOW_SECRETS}" == true ]]; then
        MLFLOW_TOKEN=$(oc create token mlflow-api -n mlflow-workspace 2>/dev/null) || true
        if [[ -n "${MLFLOW_TOKEN}" ]]; then
            echo "  SA Token:             ${MLFLOW_TOKEN}"
        else
            echo "  SA Token:             (発行失敗 — oc create token mlflow-api -n mlflow-workspace を実行)"
        fi
    else
        echo "  SA Token:             (--no-secrets で非表示)"
    fi
    echo
    echo "  Python 設定例:"
    echo "    export MLFLOW_TRACKING_URI=\"${MLFLOW_EXTERNAL:-${MLFLOW_INTERNAL:-<未検出>}}\""
    echo "    export MLFLOW_TRACKING_INSECURE_TLS=true"
    echo "    export MLFLOW_TRACKING_TOKEN=\"<上記 SA Token>\""
else
    echo "  (未デプロイ)"
fi
echo

# --- MaaS Gateway ---
echo "--- MaaS Gateway ---"
GW_STATUS=$(oc get gateway maas-default-gateway -n openshift-ingress -o json 2>/dev/null | python3 -c "
import json,sys
data = json.load(sys.stdin)
conds = data.get('status',{}).get('conditions',[])
prog = [c for c in conds if c.get('type')=='Programmed' and c.get('status')=='True']
print('Programmed' if prog else 'NotReady')
" 2>/dev/null) || GW_STATUS=""
if [[ "${GW_STATUS}" == "Programmed" ]]; then
    echo "  MaaS Gateway: Programmed"
elif [[ -n "${GW_STATUS}" ]]; then
    echo "  MaaS Gateway: ${GW_STATUS}"
else
    echo "  MaaS Gateway: (未検出)"
fi

MAAS_HEALTH=$(curl -sk -o /dev/null -w '%{http_code}' "https://maas.${CLUSTER_DOMAIN}/maas-api/health" 2>/dev/null) || MAAS_HEALTH="000"
echo "  MaaS API health: HTTP ${MAAS_HEALTH}"
echo

echo "========================================"
