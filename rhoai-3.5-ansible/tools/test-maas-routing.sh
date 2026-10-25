#!/usr/bin/env bash
# MaaS Gateway の 2 通りのルーティングを個別に試す
#
# RHOAI 3.5 の Gateway は次の 2 方式を提供する。
#
#   body-based (推奨)
#     POST https://maas.<domain>/v1/chat/completions
#     Gateway がリクエストボディの "model" を見て宛先を決める。
#     URL にモデル名が出ないので、モデルを切り替えてもクライアント側の
#     URL を変えなくてよい。
#     ただし model は完全修飾名 publishers/<namespace>/models/<model> が必要。
#     短縮名 (例: qwen3-5-9b) を渡すと 404 になる。
#     X-Gateway-Model-Name を手で付けてもボディの内容が優先されるため、
#     ヘッダでの回避はできない。
#
#   path-based (従来)
#     POST https://maas.<domain>/<namespace>/<model>/v1/chat/completions
#     こちらは model に短縮名を使う。
#     パスは /llm/<model>/... ではなく namespace 込みであることに注意。
#
# Usage:
#   ./tools/test-maas-routing.sh <API_KEY> [MODEL_NAME] [NAMESPACE] [PROMPT]
#
# Examples:
#   ./tools/test-maas-routing.sh "$(cat .vllm-token)"
#   ./tools/test-maas-routing.sh maas-xxxx qwen3-5-9b llm-serving "日本の首都は？"
#
#   # 片方だけ試す
#   ROUTES=body ./tools/test-maas-routing.sh maas-xxxx
#   ROUTES=path ./tools/test-maas-routing.sh maas-xxxx
#
# 外部 DNS が未設定でも動く。Gateway Service の ClusterIP を引いて
# curl --resolve で解決させるため、TLS の SNI もルーティングも本番と同じ
# 経路を通る。クラスタ外から名前解決できる環境では NO_RESOLVE=1 を指定する。
#
# Ansible でまとめて検証する場合:
#   uv run ansible-playbook playbooks/llm_verify_inference.yml -i inventory/<env> \
#     -e inference_via=maas
set -euo pipefail

API_KEY="${1:?Usage: $0 <API_KEY> [MODEL_NAME] [NAMESPACE] [PROMPT]}"
MODEL="${2:-}"
NAMESPACE="${3:-llm-serving}"
PROMPT="${4:-Reply with one short sentence: why is the sky blue?}"
ROUTES="${ROUTES:-body path}"
MAX_TOKENS="${MAX_TOKENS:-64}"

CLUSTER_DOMAIN=$(oc get ingresses.config.openshift.io cluster -o jsonpath='{.spec.domain}')
MAAS_HOST="maas.${CLUSTER_DOMAIN}"

# モデル名の省略時は Ready な LLMInferenceService から拾う
if [[ -z "${MODEL}" ]]; then
    # oc の jsonpath はネストしたフィルタに対応しないため python で絞り込む
    MODEL=$(oc get llminferenceservice -n "${NAMESPACE}" -o json 2>/dev/null | python3 -c '
import json, sys
try:
    items = json.load(sys.stdin).get("items", [])
except Exception:
    sys.exit(0)
for it in items:
    conds = (it.get("status") or {}).get("conditions") or []
    if any(c.get("type") == "Ready" and c.get("status") == "True" for c in conds):
        print(it["metadata"]["name"])
        break
')
    if [[ -z "${MODEL}" ]]; then
        echo "Ready なモデルが ${NAMESPACE} に見つかりません。モデル名を指定してください。" >&2
        echo "  oc get llminferenceservice -n ${NAMESPACE}" >&2
        exit 1
    fi
    echo "モデル未指定のため ${MODEL} を使います"
fi

# 外部 DNS に頼らず Gateway Service の ClusterIP へ解決させる
RESOLVE_ARGS=()
if [[ -z "${NO_RESOLVE:-}" ]]; then
    GW_IP=$(oc get svc -n openshift-ingress \
        -l gateway.networking.k8s.io/gateway-name=maas-default-gateway \
        -o jsonpath='{.items[0].spec.clusterIP}' 2>/dev/null || true)
    if [[ -n "${GW_IP}" ]]; then
        RESOLVE_ARGS=(--resolve "${MAAS_HOST}:443:${GW_IP}")
    fi
fi

# クラスタ内から叩く。Gateway の ClusterIP はクラスタ外からは見えないため、
# maas-api Pod を踏み台にする。
MAAS_POD=$(oc get pods -n redhat-ai-gateway-infra \
    -l app.kubernetes.io/name=maas-api \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
if [[ -z "${MAAS_POD}" ]]; then
    echo "maas-api Pod が見つかりません" >&2
    exit 1
fi

echo "host      : ${MAAS_HOST}"
echo "model     : ${MODEL} (namespace: ${NAMESPACE})"
echo "routes    : ${ROUTES}"
echo

rc_total=0

run_route() {
    local label="$1" url="$2" body_model="$3"
    local payload out http body

    payload=$(python3 -c '
import json, sys
print(json.dumps({
    "model": sys.argv[1],
    "messages": [{"role": "user", "content": sys.argv[2]}],
    "max_tokens": int(sys.argv[3]),
    "temperature": 0,
    "chat_template_kwargs": {"enable_thinking": False},
}))' "${body_model}" "${PROMPT}" "${MAX_TOKENS}")

    echo "--- ${label}"
    echo "    URL        : ${url}"
    echo "    body.model : ${body_model}"

    out=$(oc exec -n redhat-ai-gateway-infra "${MAAS_POD}" -- \
        curl -sk "${RESOLVE_ARGS[@]}" \
        -w '\n__HTTP__%{http_code} ttfb=%{time_starttransfer} total=%{time_total}' \
        -X POST "${url}" \
        -H "Authorization: Bearer ${API_KEY}" \
        -H 'Content-Type: application/json' \
        -d "${payload}" 2>&1) || true

    http=$(printf '%s' "${out}" | sed -n 's/.*__HTTP__\([0-9]*\).*/\1/p' | tail -1)
    body=$(printf '%s' "${out}" | sed 's/\n*__HTTP__.*//')

    if [[ "${http}" == "200" ]]; then
        echo "    結果       : OK ($(printf '%s' "${out}" | sed -n 's/.*__HTTP__[0-9]* //p' | tail -1))"
        printf '%s' "${body}" | python3 -c '
import json, sys
raw = sys.stdin.read()
raw = raw.split("\n__HTTP__")[0]
try:
    d = json.loads(raw)
except Exception:
    print("    応答       : (JSON として解釈できません)"); raise SystemExit
c = d.get("choices", [{}])[0]
msg = (c.get("message") or {}).get("content") or "(空)"
usage = d.get("usage") or {}
print("    finish     : %s" % c.get("finish_reason"))
print("    tokens     : %s" % usage.get("completion_tokens"))
print("    応答       : %s" % msg.strip()[:200])
' || true
    else
        rc_total=1
        echo "    結果       : NG (HTTP ${http:-不明})"
        printf '%s\n' "${body}" | head -3 | sed 's/^/    /'
        if [[ "${http}" == "404" && "${label}" == body-based* ]]; then
            echo "    ヒント     : body-based は model に完全修飾名が必要です。"
            echo "                 publishers/${NAMESPACE}/models/${MODEL}"
        fi
        if [[ "${http}" == "404" && "${label}" == path-based* ]]; then
            echo "    ヒント     : 実際のパスを HTTPRoute で確認してください。"
            echo "                 oc get httproute ${MODEL}-kserve-route -n ${NAMESPACE} \\"
            echo "                   -o jsonpath='{range .spec.rules[*].matches[*]}{.path.value}{\"\\n\"}{end}'"
        fi
    fi
    echo
}

for r in ${ROUTES}; do
    case "${r}" in
        body)
            run_route "body-based (推奨)" \
                "https://${MAAS_HOST}/v1/chat/completions" \
                "publishers/${NAMESPACE}/models/${MODEL}"
            ;;
        path)
            run_route "path-based (従来)" \
                "https://${MAAS_HOST}/${NAMESPACE}/${MODEL}/v1/chat/completions" \
                "${MODEL}"
            ;;
        *)
            echo "不明なルート: ${r} (body / path のいずれか)" >&2
            exit 1
            ;;
    esac
done

if [[ "${rc_total}" -eq 0 ]]; then
    echo "すべての経路で推論に成功しました"
else
    echo "失敗した経路があります" >&2
fi
exit "${rc_total}"
