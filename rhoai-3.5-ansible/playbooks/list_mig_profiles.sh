#!/usr/bin/env bash
# Read-only GPU Operator profile discovery. Requires bash, oc and jq.
# Keep ConfigMap precedence aligned with tasks/mig_preflight_node.yml.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: ./playbooks/list_mig_profiles.sh [--node NAME | --selector SELECTOR]
                                      [--namespace NAMESPACE] [--policy NAME]

GPU Operator の MIG プロファイルと分割数を、元の YAML のまま表示します。
既定の対象: nvidia.com/mig.capable=true の全ノード
既定の namespace: nvidia-gpu-operator / ClusterPolicy: gpu-cluster-policy

取得順序（configure_mig.yml と同じ）:
  カスタム ConfigMap が指定されていれば、その ConfigMap のみ。
  未指定なら <node-name>-mig-config、無ければ default-mig-parted-config。

例:
  ./playbooks/list_mig_profiles.sh
  ./playbooks/list_mig_profiles.sh --selector rhoai-validation=mig
  ./playbooks/list_mig_profiles.sh --node <GPU-worker-name>

KUBECONFIG または oc の現在の context を使用します。取得のみで、設定変更はしません。
共有・カスタム ConfigMap は設定上の候補であり、全項目の実 GPU 適用を保証しません。
EOF
}
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
value_required() {
    [[ $# -ge 2 && -n "$2" && "$2" != -* ]] || die "$1 には値が必要です"
}
node=''
selector='nvidia.com/mig.capable=true'
selector_explicit=false
namespace='nvidia-gpu-operator'
policy='gpu-cluster-policy'
while [[ $# -gt 0 ]]; do
    case "$1" in
        --node) value_required "$@"; node=$2; shift 2 ;;
        --selector|-l) value_required "$@"; selector=$2; selector_explicit=true; shift 2 ;;
        --namespace|-n) value_required "$@"; namespace=$2; shift 2 ;;
        --policy) value_required "$@"; policy=$2; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) die "不明な引数: $1（--help で使い方を表示）" ;;
    esac
done
[[ -z "$node" || "$selector_explicit" == false ]] || die '--node と --selector は同時に指定できません'
for tool in oc jq; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool が必要です"
done

policy_json=$(oc get clusterpolicy "$policy" -o json)
custom_config=$(jq -r '.spec.migManager.config.name // empty' <<< "$policy_json")
strategy=$(jq -r '.spec.mig.strategy // "none"' <<< "$policy_json")
if [[ -n "$node" ]]; then
    nodes_json=$(oc get node "$node" -o json | jq '{items: [.] }')
else
    nodes_json=$(oc get nodes --selector="$selector" -o json)
fi
jq -e '.items | type == "array" and length > 0' <<< "$nodes_json" >/dev/null || die '対象ノードがありません'

printf 'ClusterPolicy: %s / strategy: %s\n' "$policy" "$strategy"
shown=0
while IFS= read -r node_json; do
    name=$(jq -r '.metadata.name' <<< "$node_json")
    capable=$(jq -r '.metadata.labels["nvidia.com/mig.capable"] // "unknown"' <<< "$node_json")
    printf '\nNode: %s\n' "$name"
    jq -r '"GPU: " + (.metadata.labels["nvidia.com/gpu.product"] // "unknown"),
           "Current profile: " + (.metadata.labels["nvidia.com/mig.config"] // "unset") +
           " / state: " + (.metadata.labels["nvidia.com/mig.config.state"] // "unset")' <<< "$node_json"
    if [[ "$capable" != true ]]; then
        printf 'MIG 対応を確認できません (mig.capable=%s)。スキップします。\n' "$capable"
        continue
    fi
    if [[ -n "$custom_config" ]]; then
        candidates=("$custom_config")
        source_kind='custom'
    else
        candidates=("${name}-mig-config" 'default-mig-parted-config')
        source_kind='node-generated'
    fi
    config_json=''
    config_name=''
    for candidate in "${candidates[@]}"; do
        # Only NotFound permits fallback. Forbidden/network/API failures stop here.
        config_json=$(oc get configmap "$candidate" -n "$namespace" --ignore-not-found -o json)
        if [[ -n "$config_json" ]]; then
            config_name=$candidate
            break
        fi
    done
    [[ -n "$config_name" ]] || die "$name の MIG ConfigMap がありません（MIG Manager の状態を確認してください）"
    if [[ -z "$custom_config" && "$config_name" == default-mig-parted-config ]]; then
        source_kind='shared-default'
    fi
    printf 'ConfigMap: %s/%s (%s)\n' "$namespace" "$config_name" "$source_kind"
    if [[ "$source_kind" != node-generated ]]; then
        printf '注意: 共有・カスタム設定の一覧です。各項目がこの GPU で利用可能とは限りません。\n'
    fi
    printf 'Profiles / partition counts (config.yaml):\n'
    # YAML is intentionally not parsed with grep/sed. This also preserves mixed
    # layouts, per-device selectors and custom/flow-style configurations exactly.
    jq -er '.data["config.yaml"] | select(type == "string" and length > 0)' <<< "$config_json" \
        || die "$config_name に空でない config.yaml がありません"
    shown=$((shown + 1))
done <<< "$(jq -c '.items[]' <<< "$nodes_json")"
[[ "$shown" -gt 0 ]] || die 'MIG 対応を確認できるノードがありませんでした'
