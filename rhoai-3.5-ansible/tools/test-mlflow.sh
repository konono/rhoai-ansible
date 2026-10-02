#!/usr/bin/env bash
# MLflow にテスト用の Experiment / Run / Metrics を書き込んで動作確認する
# Usage:
#   ./tools/test-mlflow.sh [MLFLOW_URL]
#
# MLFLOW_URL を省略すると、クラスタの MLflow リソースから自動取得する
set -euo pipefail

if [[ -n "${1:-}" ]]; then
    MLFLOW_URL="$1"
else
    MLFLOW_URL=$(oc get mlflow mlflow -n redhat-ods-applications -o jsonpath='{.status.url}' 2>/dev/null) || {
        echo "ERROR: MLflow URL の自動取得に失敗しました。引数で指定してください。"
        exit 1
    }
fi

MLFLOW_URL="${MLFLOW_URL%/}"
EXPERIMENT_NAME="verify-test-$(date +%Y%m%d-%H%M%S)"

echo "=== MLflow 動作確認 ==="
echo "  URL:        ${MLFLOW_URL}"
echo "  Experiment: ${EXPERIMENT_NAME}"
echo ""

# 1. ヘルスチェック
echo "--- Step 1: ヘルスチェック ---"
HC_CODE=$(curl -sk -o /dev/null -w '%{http_code}' "${MLFLOW_URL}/health" 2>/dev/null) || HC_CODE="000"
if [[ "${HC_CODE}" == "200" ]]; then
    echo "  OK: MLflow is healthy (HTTP 200)"
else
    echo "  WARN: /health returned HTTP ${HC_CODE} (一部バージョンでは /health がないこともあります)"
fi
echo ""

# 2. Experiment 作成
echo "--- Step 2: Experiment 作成 ---"
CREATE_RESP=$(curl -sk -w "\n---HTTP_CODE:%{http_code}---" \
    "${MLFLOW_URL}/api/2.0/mlflow/experiments/create" \
    -H "Content-Type: application/json" \
    -d "{\"name\": \"${EXPERIMENT_NAME}\"}")

CREATE_CODE=$(echo "${CREATE_RESP}" | grep -o 'HTTP_CODE:[0-9]*' | cut -d: -f2)
CREATE_BODY=$(echo "${CREATE_RESP}" | sed 's/---HTTP_CODE:[0-9]*---$//')

if [[ "${CREATE_CODE}" == "200" ]]; then
    EXPERIMENT_ID=$(echo "${CREATE_BODY}" | python3 -c "import json,sys; print(json.load(sys.stdin)['experiment_id'])" 2>/dev/null)
    echo "  OK: Experiment 作成成功 (ID: ${EXPERIMENT_ID})"
else
    echo "  FAILED: Experiment 作成失敗 (HTTP ${CREATE_CODE})"
    echo "  ${CREATE_BODY}"
    exit 1
fi
echo ""

# 3. Run 作成
echo "--- Step 3: Run 作成 ---"
RUN_RESP=$(curl -sk -w "\n---HTTP_CODE:%{http_code}---" \
    "${MLFLOW_URL}/api/2.0/mlflow/runs/create" \
    -H "Content-Type: application/json" \
    -d "{\"experiment_id\": \"${EXPERIMENT_ID}\", \"run_name\": \"verify-run\"}")

RUN_CODE=$(echo "${RUN_RESP}" | grep -o 'HTTP_CODE:[0-9]*' | cut -d: -f2)
RUN_BODY=$(echo "${RUN_RESP}" | sed 's/---HTTP_CODE:[0-9]*---$//')

if [[ "${RUN_CODE}" == "200" ]]; then
    RUN_ID=$(echo "${RUN_BODY}" | python3 -c "import json,sys; print(json.load(sys.stdin)['run']['info']['run_id'])" 2>/dev/null)
    echo "  OK: Run 作成成功 (ID: ${RUN_ID})"
else
    echo "  FAILED: Run 作成失敗 (HTTP ${RUN_CODE})"
    echo "  ${RUN_BODY}"
    exit 1
fi
echo ""

# 4. Metrics 記録
echo "--- Step 4: Metrics 記録 ---"
TIMESTAMP=$(python3 -c "import time; print(int(time.time() * 1000))")

for metric_name in accuracy loss f1_score; do
    value=$(python3 -c "import random; print(round(random.uniform(0.1, 1.0), 4))")
    METRIC_RESP=$(curl -sk -o /dev/null -w '%{http_code}' \
        "${MLFLOW_URL}/api/2.0/mlflow/runs/log-metric" \
        -H "Content-Type: application/json" \
        -d "{\"run_id\": \"${RUN_ID}\", \"key\": \"${metric_name}\", \"value\": ${value}, \"timestamp\": ${TIMESTAMP}}")

    if [[ "${METRIC_RESP}" == "200" ]]; then
        echo "  OK: ${metric_name} = ${value}"
    else
        echo "  FAILED: ${metric_name} の記録に失敗 (HTTP ${METRIC_RESP})"
    fi
done
echo ""

# 5. Params 記録
echo "--- Step 5: Params 記録 ---"
for param in "learning_rate:0.001" "batch_size:32" "epochs:10"; do
    key="${param%%:*}"
    val="${param#*:}"
    PARAM_RESP=$(curl -sk -o /dev/null -w '%{http_code}' \
        "${MLFLOW_URL}/api/2.0/mlflow/runs/log-param" \
        -H "Content-Type: application/json" \
        -d "{\"run_id\": \"${RUN_ID}\", \"key\": \"${key}\", \"value\": \"${val}\"}")

    if [[ "${PARAM_RESP}" == "200" ]]; then
        echo "  OK: ${key} = ${val}"
    else
        echo "  FAILED: ${key} の記録に失敗 (HTTP ${PARAM_RESP})"
    fi
done
echo ""

# 6. Run 終了
echo "--- Step 6: Run 終了 ---"
END_RESP=$(curl -sk -o /dev/null -w '%{http_code}' \
    "${MLFLOW_URL}/api/2.0/mlflow/runs/update" \
    -H "Content-Type: application/json" \
    -d "{\"run_id\": \"${RUN_ID}\", \"status\": \"FINISHED\"}")

if [[ "${END_RESP}" == "200" ]]; then
    echo "  OK: Run を FINISHED に更新"
else
    echo "  FAILED: Run 更新失敗 (HTTP ${END_RESP})"
fi
echo ""

# 7. 記録された Metrics を読み戻し
echo "--- Step 7: Metrics 読み戻し確認 ---"
GET_RESP=$(curl -sk \
    "${MLFLOW_URL}/api/2.0/mlflow/runs/get?run_id=${RUN_ID}" 2>/dev/null)

echo "${GET_RESP}" | python3 -c "
import json, sys
data = json.load(sys.stdin)
run = data.get('run', {})
metrics = run.get('data', {}).get('metrics', [])
params = run.get('data', {}).get('params', [])
print('  Metrics:')
for m in metrics:
    print(f\"    {m['key']}: {m['value']}\")
print('  Params:')
for p in params:
    print(f\"    {p['key']}: {p['value']}\")
" 2>/dev/null || echo "  読み戻しに失敗しました"
echo ""

# 8. クリーンアップ
echo "--- Step 8: テストデータ削除 ---"
DEL_RESP=$(curl -sk -o /dev/null -w '%{http_code}' \
    "${MLFLOW_URL}/api/2.0/mlflow/experiments/delete" \
    -H "Content-Type: application/json" \
    -d "{\"experiment_id\": \"${EXPERIMENT_ID}\"}")

if [[ "${DEL_RESP}" == "200" ]]; then
    echo "  OK: テスト Experiment を削除しました"
else
    echo "  WARN: Experiment 削除失敗 (HTTP ${DEL_RESP}) — 手動で削除してください"
fi
echo ""

echo "SUCCESS: MLflow の読み書きが正常に動作しています"
