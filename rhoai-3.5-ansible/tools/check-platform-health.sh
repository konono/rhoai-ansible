#!/usr/bin/env bash
# プラットフォーム全体のヘルスチェック
# verify.yml の infra/deps/platform/health タグ相当の確認をまとめて実行する
# Usage:
#   ./tools/check-platform-health.sh
set -euo pipefail

PASS=0
FAIL=0
WARN=0

ok()   { echo "  ✓ $1"; ((PASS++)); }
fail() { echo "  ✗ $1"; ((FAIL++)); }
warn() { echo "  ! $1"; ((WARN++)); }

check_subscription() {
    local name="$1" ns="$2"
    local csv
    csv=$(oc get subscription "${name}" -n "${ns}" -o jsonpath='{.status.installedCSV}' 2>/dev/null) || { fail "Subscription ${name} が見つかりません"; return; }
    if [[ -n "${csv}" ]]; then
        ok "Subscription ${name} → ${csv}"
    else
        fail "Subscription ${name}: installedCSV が未設定"
    fi
}

echo "=== プラットフォームヘルスチェック ==="
echo ""

# oc login 確認
echo "--- クラスタ接続 ---"
WHO=$(oc whoami 2>/dev/null) || { fail "oc login していません"; echo "FAILED: クラスタに接続できません"; exit 1; }
ok "oc login: ${WHO}"
CLUSTER_DOMAIN=$(oc get ingresses.config.openshift.io cluster -o jsonpath='{.spec.domain}')
ok "cluster domain: ${CLUSTER_DOMAIN}"
echo ""

# Operator Subscriptions
echo "--- Operator Subscriptions ---"
check_subscription lvms-operator openshift-lvm-storage
check_subscription openshift-cert-manager-operator cert-manager-operator
check_subscription servicemeshoperator3 openshift-servicemesh
check_subscription nfd openshift-nfd
check_subscription gpu-operator-certified nvidia-gpu-operator
check_subscription rhcl-operator openshift-rhcl
check_subscription kueue-operator openshift-kueue
check_subscription job-set openshift-jobset
check_subscription leader-worker-set openshift-leaderworkerset
check_subscription rhods-operator redhat-ods-operator
echo ""

# LVMCluster
echo "--- LVM ---"
LVM_STATE=$(oc get lvmcluster lvmcluster -n openshift-lvm-storage -o jsonpath='{.status.state}' 2>/dev/null) || LVM_STATE=""
if [[ "${LVM_STATE}" == "Ready" ]]; then
    ok "LVMCluster: Ready"
else
    fail "LVMCluster: ${LVM_STATE:-not found}"
fi
echo ""

# MetalLB
echo "--- MetalLB ---"
SPEAKER_COUNT=$(oc get pods -n metallb-system -l component=speaker --field-selector=status.phase=Running --no-headers 2>/dev/null | wc -l | tr -d ' ')
if [[ "${SPEAKER_COUNT}" -gt 0 ]]; then
    ok "MetalLB speakers running: ${SPEAKER_COUNT}"
else
    fail "MetalLB speaker が見つかりません"
fi
echo ""

# NooBaa
echo "--- ODF (NooBaa) ---"
NOOBAA_PHASE=$(oc get noobaa noobaa -n openshift-storage -o jsonpath='{.status.phase}' 2>/dev/null) || NOOBAA_PHASE=""
if [[ "${NOOBAA_PHASE}" == "Ready" ]]; then
    ok "NooBaa: Ready"
else
    fail "NooBaa: ${NOOBAA_PHASE:-not found}"
fi
echo ""

# DataScienceCluster
echo "--- RHOAI ---"
DSC_PHASE=$(oc get datasciencecluster default-dsc -o jsonpath='{.status.phase}' 2>/dev/null) || DSC_PHASE=""
if [[ "${DSC_PHASE}" == "Ready" ]]; then
    ok "DataScienceCluster: Ready"
else
    fail "DataScienceCluster: ${DSC_PHASE:-not found}"
fi

DASHBOARD_READY=$(oc get deployment rhods-dashboard -n redhat-ods-applications -o jsonpath='{.status.readyReplicas}' 2>/dev/null) || DASHBOARD_READY="0"
if [[ "${DASHBOARD_READY}" -gt 0 ]]; then
    ok "RHOAI Dashboard: ${DASHBOARD_READY} replicas"
else
    fail "RHOAI Dashboard: 起動していません"
fi
echo ""

# MaaS Gateway
echo "--- MaaS ---"
GW_STATUS=$(oc get gateway maas-default-gateway -n openshift-ingress -o json 2>/dev/null | python3 -c "
import json,sys
data = json.load(sys.stdin)
conds = data.get('status',{}).get('conditions',[])
prog = [c for c in conds if c.get('type')=='Programmed' and c.get('status')=='True']
print('Programmed' if prog else 'NotReady')
" 2>/dev/null) || GW_STATUS=""
if [[ "${GW_STATUS}" == "Programmed" ]]; then
    ok "MaaS Gateway: Programmed"
else
    fail "MaaS Gateway: ${GW_STATUS:-not found}"
fi

MAAS_HEALTH=$(curl -sk -o /dev/null -w '%{http_code}' "https://maas.${CLUSTER_DOMAIN}/maas-api/health" 2>/dev/null) || MAAS_HEALTH="000"
if [[ "${MAAS_HEALTH}" == "200" ]]; then
    ok "MaaS API health: HTTP 200"
else
    fail "MaaS API health: HTTP ${MAAS_HEALTH}"
fi
echo ""

# Keycloak
echo "--- Keycloak ---"
KC_READY=$(oc get keycloak keycloak -n keycloak -o json 2>/dev/null | python3 -c "
import json,sys
data = json.load(sys.stdin)
conds = data.get('status',{}).get('conditions',[])
ready = [c for c in conds if c.get('type')=='Ready' and c.get('status')=='True']
print('Ready' if ready else 'NotReady')
" 2>/dev/null) || KC_READY=""
if [[ "${KC_READY}" == "Ready" ]]; then
    ok "Keycloak: Ready"
else
    fail "Keycloak: ${KC_READY:-not found}"
fi
echo ""

# MLflow
echo "--- MLflow ---"
MLFLOW_AVAIL=$(oc get mlflow mlflow -n redhat-ods-applications -o json 2>/dev/null | python3 -c "
import json,sys
data = json.load(sys.stdin)
conds = data.get('status',{}).get('conditions',[])
avail = [c for c in conds if c.get('type')=='Available' and c.get('status')=='True']
print('Available' if avail else 'NotAvailable')
" 2>/dev/null) || MLFLOW_AVAIL=""
if [[ "${MLFLOW_AVAIL}" == "Available" ]]; then
    ok "MLflow: Available"
else
    fail "MLflow: ${MLFLOW_AVAIL:-not found}"
fi
echo ""

# Route 競合チェック
echo "--- Route 競合チェック ---"
CLAIMED=$(oc get route --all-namespaces -o jsonpath='{range .items[*]}{range .status.ingress[*]}{range .conditions[*]}{.reason}{"\t"}{.host}{"\n"}{end}{end}{end}' 2>/dev/null \
    | grep "HostAlreadyClaimed" || true)
if [[ -z "${CLAIMED}" ]]; then
    ok "HostAlreadyClaimed なし"
else
    fail "HostAlreadyClaimed が見つかりました:"
    echo "${CLAIMED}" | while IFS=$'\t' read -r reason host; do
        echo "    ${host}"
    done
fi
echo ""

# 異常 Pod チェック
echo "--- 異常 Pod チェック ---"
for ns in keycloak openshift-lvm-storage openshift-storage redhat-ods-applications redhat-ai-gateway-infra llm-serving openshift-ingress; do
    BAD_PODS=$(oc get pods -n "${ns}" --no-headers --field-selector=status.phase!=Running,status.phase!=Succeeded 2>/dev/null | head -5) || BAD_PODS=""
    if [[ -z "${BAD_PODS}" ]]; then
        ok "${ns}: 全 Pod 正常"
    else
        fail "${ns}: 異常 Pod あり"
        echo "${BAD_PODS}" | while read -r line; do echo "      ${line}"; done
    fi
done
echo ""

# LLMInferenceService
echo "--- LLM InferenceService ---"
LIS_LIST=$(oc get llminferenceservice --all-namespaces --no-headers 2>/dev/null) || LIS_LIST=""
if [[ -z "${LIS_LIST}" ]]; then
    warn "LLMInferenceService が見つかりません"
else
    echo "${LIS_LIST}" | while read -r ns name rest; do
        READY=$(oc get llminferenceservice "${name}" -n "${ns}" -o json 2>/dev/null | python3 -c "
import json,sys
data = json.load(sys.stdin)
conds = data.get('status',{}).get('conditions',[])
ready = [c for c in conds if c.get('type')=='Ready' and c.get('status')=='True']
print('Ready' if ready else 'NotReady')
" 2>/dev/null) || READY="unknown"
        if [[ "${READY}" == "Ready" ]]; then
            ok "LLMInferenceService ${ns}/${name}: Ready"
        else
            fail "LLMInferenceService ${ns}/${name}: ${READY}"
        fi
    done
fi
echo ""

# サマリー
echo "========================================="
echo "  結果: ✓ ${PASS} passed / ✗ ${FAIL} failed / ! ${WARN} warnings"
echo "========================================="

if [[ "${FAIL}" -gt 0 ]]; then
    exit 1
fi
