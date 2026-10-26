#!/usr/bin/env bash
# Runtime counterpart of roles/llm_serving/tasks/deploy_model.yml.
set -euo pipefail
umask 077
[[ $# == 1 ]] || { echo 'Usage: llm-download.sh MODEL-download.yml' >&2; exit 1; }
manifest=$1
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
# Generated download manifests contain one Job. If adding documents, normalize
# JSON streams/Lists and require exactly one Job as llm-deploy.sh does for LIS.
oc create --dry-run=client --validate=false -f "$manifest" -o json > "$work/desired.json"
jq -e '.kind == "Job"' "$work/desired.json" > /dev/null
job=$(jq -er '.metadata.name | select(startswith("download-"))' "$work/desired.json")
namespace=$(jq -er '.metadata.namespace | select(type == "string" and length > 0)' "$work/desired.json")
model=${job#download-}
oc get llminferenceservices.v1alpha2.serving.kserve.io "$model" -n "$namespace" \
    --ignore-not-found -o json > "$work/live.json"
if [[ -s "$work/live.json" ]]; then
    ready=$(jq -er '[.status.conditions[]? | select(.type == "Ready" and .status == "True")] | length' "$work/live.json")
    if ((ready > 0)); then
        echo "$namespace/$model: Ready model; download skipped"
        exit 0
    fi
fi
oc get job "$job" -n "$namespace" --ignore-not-found -o json > "$work/job.json"
if [[ -s "$work/job.json" ]]; then
    active=$(jq -er '.status.active // 0' "$work/job.json")
    succeeded=$(jq -er '.status.succeeded // 0' "$work/job.json")
    if ((active > 0)); then
        echo "$namespace/$job: waiting for running download"
        oc wait --for=condition=complete "job/$job" -n "$namespace" --timeout=3600s
        exit 0
    fi
    if ((succeeded > 0)); then
        echo "$namespace/$job: download already completed"
        exit 0
    fi
fi
# A cache check is needed only when the role would consider a new download.
# Redirect stdin: the enclosing generated guide may itself use a here-document.
overrides=$(jq -cn --arg path "/models/$model/config.json" \
    '{spec:{containers:[{name:"check",image:"registry.access.redhat.com/ubi9/ubi-minimal:latest",
      command:["test","-f",$path],volumeMounts:[{name:"cache",mountPath:"/models"}]}],
      volumes:[{name:"cache",persistentVolumeClaim:{claimName:"hf-model-cache"}}]}}')
if oc run "pvc-check-$model" --rm -i --restart=Never -n "$namespace" \
    --image=registry.access.redhat.com/ubi9/ubi-minimal:latest \
    --overrides="$overrides" </dev/null; then
    echo "$namespace/$model: model data already on PVC; download skipped"
    exit 0
fi
# Like the role, a failed cache probe does not establish a cache hit.
if [[ -s "$work/job.json" ]]; then
    # Unlike the role's asynchronous delete, wait before reusing the Job name.
    oc delete job "$job" -n "$namespace" --ignore-not-found --wait=true
fi
oc apply -f "$manifest"
oc wait --for=condition=complete "job/$job" -n "$namespace" --timeout=3600s
