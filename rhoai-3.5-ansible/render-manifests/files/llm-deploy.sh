#!/usr/bin/env bash
# Generated manual deployment uses bash/oc/jq; no custom Python executor.
set -euo pipefail
umask 077
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
fail() { echo "ERROR: $*" >&2; exit 1; }
query() { jq -L "$script_dir" "$@"; }
read_nodes() {
    oc get nodes -o json > "$work/nodes.json"
    query -e 'include "llm-gpu"; validate_nodes' "$work/nodes.json" > /dev/null
}

case "${1:-}" in
preflight)
    read_nodes
    query -e --slurpfile models "$script_dir/gpu-models.json" \
        'include "llm-gpu"; preflight($models[0])' "$work/nodes.json"
    exit 0
    ;;
deploy) [[ $# == 2 ]] || fail 'Usage: llm-deploy.sh deploy MODEL-deploy.yml' ;;
*) fail 'Usage: llm-deploy.sh preflight | deploy MODEL-deploy.yml' ;;
esac
manifest=$2
lis_resource=llminferenceservices.v1alpha2.serving.kserve.io
# Let oc parse the exact YAML that will be applied. --dry-run=client does not write.
# It still requires a configured cluster and discovery for both manifest CRDs.
oc create --dry-run=client --validate=false -f "$manifest" -o json > "$work/manifest.json"
# oc prints a JSON stream for multi-document YAML, not necessarily a List.
query -se '[.[] | if .kind == "List" then .items[] else . end |
           select(.kind == "LLMInferenceService")] |
          if length == 1 then .[0] else error("Expected one LLMInferenceService") end' \
    "$work/manifest.json" > "$work/desired.json"
name=$(query -er '.metadata.name | select(type == "string" and length > 0)' "$work/desired.json")
namespace=$(query -er '.metadata.namespace | select(type == "string" and length > 0)' "$work/desired.json")
query -e 'include "llm-gpu"; config' "$work/desired.json" > "$work/config.json"
# --ignore-not-found suppresses only NotFound. Authorization/transport failures stop.
oc get "$lis_resource" "$name" -n "$namespace" --ignore-not-found -o json > "$work/live.json"
if [[ ! -s "$work/live.json" ]]; then printf 'null\n' > "$work/live.json"; fi
query -e ' . == null or (.kind == "LLMInferenceService" and (.spec | type) == "object")' \
    "$work/live.json" > /dev/null
if query -e --slurpfile desired "$work/desired.json" \
    'include "llm-gpu"; . != null and (normalized_spec == ($desired[0] | normalized_spec))' \
    "$work/live.json" > /dev/null; then
    action=unchanged
else
    read_nodes
    action=$(query -er --slurpfile desired "$work/desired.json" --slurpfile live "$work/live.json" \
        'include "llm-gpu"; plan($desired[0]; $live[0])' "$work/nodes.json")
fi
echo "$namespace/$name: $action"
if [[ "$action" == create ]]; then
    oc get pods -A -o json > "$work/pods.json"
    query -e '(.items | type) == "array"' "$work/pods.json" > /dev/null
    query -e --slurpfile cfg "$work/config.json" --slurpfile pods "$work/pods.json" \
        'include "llm-gpu"; availability($cfg[0]; $pods[0])' "$work/nodes.json" > "$work/available.json"
    if ! query -e --slurpfile cfg "$work/config.json" \
        '.slots >= ($cfg[0].replicas | tonumber)' "$work/available.json" > /dev/null; then
        cat "$work/available.json" >&2
        fail 'Selected GPU resources are occupied. Stop the listed workloads yourself; no workload was deleted.'
    fi
fi
# Match serve_model.yml: wait for the webhook unless the existing model is Ready.
if ! query -e 'any(.status.conditions[]?; .type == "Ready" and .status == "True")' \
    "$work/live.json" > /dev/null; then
    webhook_ready=false
    for ((attempt=0; attempt<=24; attempt++)); do
        if endpoint=$(oc get endpoints llmisvc-webhook-server-service -n redhat-ods-applications \
            -o jsonpath='{.subsets[0].addresses[0].ip}') && [[ -n "$endpoint" ]]; then
            webhook_ready=true; break
        fi
        if ((attempt < 24)); then sleep 10; fi
    done
    [[ "$webhook_ready" == true ]] || fail 'LLMInferenceService webhook is not ready'
fi
if [[ "$action" == recreate ]]; then
    oc delete "$lis_resource" "$name" -n "$namespace" --ignore-not-found
    released=false
    for ((attempt=0; attempt<=60; attempt++)); do
        # Read errors and remaining Pods both retry, then fail closed after 300 s.
        if oc get pods -n "$namespace" -l "app.kubernetes.io/name=$name" -o json > "$work/remaining.json" &&
           query -e '(.items | type) == "array" and (.items | length) == 0' \
               "$work/remaining.json" > /dev/null; then
            released=true; break
        fi
        if ((attempt < 60)); then sleep 5; fi
    done
    [[ "$released" == true ]] || fail "GPU release wait failed for $namespace/$name; no recreation was applied. Resolve remaining Pods/read errors and rerun."
fi
oc apply -f "$manifest"
oc wait --for=condition=Ready "$lis_resource/$name" -n "$namespace" --timeout=1200s
