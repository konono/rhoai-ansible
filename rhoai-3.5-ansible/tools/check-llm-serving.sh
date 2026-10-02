#!/usr/bin/env bash
# LLM モデルサービングの状態を詳細確認する
# Usage:
#   ./tools/check-llm-serving.sh [NAMESPACE]
#
# デフォルト namespace: llm-serving
set -euo pipefail

NS="${1:-llm-serving}"

echo "=== LLM Serving 状態確認 ==="
echo "  Namespace: ${NS}"
echo ""

# LLMInferenceService 一覧
echo "--- LLMInferenceService ---"
LIS_JSON=$(oc get llminferenceservice -n "${NS}" -o json 2>/dev/null) || {
    echo "  LLMInferenceService が見つかりません (namespace: ${NS})"
    echo ""
    echo "  他の namespace を確認中..."
    oc get llminferenceservice --all-namespaces --no-headers 2>/dev/null || echo "  全 namespace でも見つかりません"
    exit 1
}

echo "${LIS_JSON}" | python3 -c "
import json, sys
data = json.load(sys.stdin)
items = data.get('items', [])
if not items:
    print('  (LLMInferenceService なし)')
    sys.exit(0)
for item in items:
    name = item['metadata']['name']
    conds = item.get('status', {}).get('conditions', [])
    ready = any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in conds)
    status = 'Ready' if ready else 'NotReady'
    symbol = '✓' if ready else '✗'
    print(f'  {symbol} {name}: {status}')
    for c in conds:
        ctype = c.get('type', '')
        cstatus = c.get('status', '')
        msg = c.get('message', '')
        print(f'      {ctype}={cstatus}' + (f' ({msg})' if msg else ''))
"
echo ""

# MaaSModelRef 確認
echo "--- MaaSModelRef ---"
REFS=$(oc get maasmodelref -n "${NS}" --no-headers 2>/dev/null) || REFS=""
if [[ -z "${REFS}" ]]; then
    echo "  (MaaSModelRef なし)"
else
    echo "${REFS}" | while read -r line; do echo "  ${line}"; done
fi
echo ""

# MaaSSubscription 確認
echo "--- MaaSSubscription ---"
SUBS_JSON=$(oc get maassubscription -n models-as-a-service -o json 2>/dev/null) || SUBS_JSON=""
if [[ -z "${SUBS_JSON}" ]]; then
    echo "  (MaaSSubscription なし)"
else
    echo "${SUBS_JSON}" | python3 -c "
import json, sys
data = json.load(sys.stdin)
items = data.get('items', [])
if not items:
    print('  (MaaSSubscription なし)')
    sys.exit(0)
for item in items:
    name = item['metadata']['name']
    phase = item.get('status', {}).get('phase', 'Unknown')
    symbol = '✓' if phase == 'Active' else '✗'
    print(f'  {symbol} {name}: {phase}')
"
fi
echo ""

# AITenant 確認
echo "--- AITenant ---"
TENANTS=$(oc get aitenant --all-namespaces --no-headers 2>/dev/null) || TENANTS=""
if [[ -z "${TENANTS}" ]]; then
    echo "  (AITenant なし)"
else
    echo "${TENANTS}" | while read -r line; do echo "  ${line}"; done
fi
echo ""

# Serving Pod の状態
echo "--- Serving Pods (${NS}) ---"
oc get pods -n "${NS}" --no-headers 2>/dev/null | while read -r line; do
    echo "  ${line}"
done
echo ""

# GPU 割当確認
echo "--- GPU リソース割当 ---"
oc get pods -n "${NS}" -o json 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
for pod in data.get('items', []):
    name = pod['metadata']['name']
    phase = pod.get('status', {}).get('phase', 'Unknown')
    for c in pod.get('spec', {}).get('containers', []):
        gpu = c.get('resources', {}).get('limits', {}).get('nvidia.com/gpu', '0')
        if gpu != '0':
            print(f'  {name} ({phase}): {gpu} GPU(s) - container: {c[\"name\"]}')
" 2>/dev/null || echo "  GPU 情報の取得に失敗しました"
echo ""

echo "確認完了"
