#!/usr/bin/env bash
set -euo pipefail
#
# oc edit と同じ感覚で vLLM 引数だけを編集する。
#
#   bash scripts/vllm-args.sh <モデルキー> [-i inventory/<env>]
#
# 動作:
#   1. 稼働中の LLMInferenceService から args を取得する
#      （クラスタに無ければ inventory の vllm_args を使う）
#   2. role が自動付与する --served-model-name / --chat-template を除いた行を
#      $EDITOR で開く
#   3. 保存して閉じると playbooks/llm_edit_args.yml で適用する
#   4. inventory に貼るスニペットが表示される
#
# 編集をやめる場合はファイルを空にするか、変更せずに閉じる。
#
# 環境変数:
#   EDITOR          使用するエディタ (既定: vi)
#   EDIT_ARGS_WAIT  Ready 待ちの秒数。0 で待たない (既定: 600)
#

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ANSIBLE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

usage() {
    echo "Usage: bash scripts/vllm-args.sh <model-key> [-i inventory/<env>]" >&2
    echo "  例: bash scripts/vllm-args.sh qwen3-8-27b -i inventory/<env>" >&2
    exit 1
}

[ $# -ge 1 ] || usage
MODEL="$1"; shift

INVENTORY=""
while [ $# -gt 0 ]; do
    case "$1" in
        -i|--inventory) INVENTORY="${2:-}"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "不明な引数: $1" >&2; usage ;;
    esac
done

if [ -z "$INVENTORY" ]; then
    # inventory が 1 つだけならそれを使う（sample は除く）
    mapfile -t _invs < <(find "${ANSIBLE_DIR}/inventory" -maxdepth 1 -mindepth 1 -type d \
                           ! -name sample -printf '%p\n' 2>/dev/null | sort)
    if [ "${#_invs[@]}" -eq 1 ]; then
        INVENTORY="${_invs[0]}"
        echo "inventory: ${INVENTORY}"
    else
        echo "-i で inventory を指定してください。候補:" >&2
        printf '  %s\n' "${_invs[@]}" >&2
        exit 1
    fi
fi

cd "$ANSIBLE_DIR"

NAMESPACE="$(oc get llminferenceservice "$MODEL" -A \
    -o jsonpath='{.items[0].metadata.namespace}' 2>/dev/null || true)"
[ -n "$NAMESPACE" ] || NAMESPACE="llm-serving"

TMP="$(mktemp -t vllm-args-XXXXXX.txt)"
trap 'rm -f "$TMP" "$TMP.orig"' EXIT

{
    echo "# ${MODEL} の vLLM 引数 — 1 行に 1 引数"
    echo "#"
    echo "# --served-model-name と --chat-template は role が自動付与するため"
    echo "# ここには書かない。'#' で始まる行は無視される。"
    echo "#"
    echo "# 保存して閉じると、稼働中の LLMInferenceService に適用される。"
    echo "# 中身を空にすると中止する。"
    echo "#"
} > "$TMP"

# 現在の args を取得する。クラスタに無ければ inventory から拾う。
CURRENT="$(oc get llminferenceservice "$MODEL" -n "$NAMESPACE" \
    -o jsonpath='{range .spec.template.containers[0].args[*]}{@}{"\n"}{end}' 2>/dev/null || true)"

if [ -n "$CURRENT" ]; then
    echo "# 取得元: クラスタ (${NAMESPACE}/${MODEL})" >> "$TMP"
    printf '%s\n' "$CURRENT" \
      | grep -v -e '^--served-model-name=' -e '^--chat-template=' >> "$TMP"
else
    echo "# 取得元: inventory (クラスタ上に LLMInferenceService が無いため)" >> "$TMP"
    uv run ansible-playbook playbooks/llm_edit_args.yml -i "$INVENTORY" \
        -e "model=${MODEL}" 2>/dev/null \
      | sed -n '/-- inventory の vllm_args --/,/^$/p' \
      | sed '1d' | sed 's/^ *//' | grep -v '^$' >> "$TMP" || true
fi

cp "$TMP" "$TMP.orig"
"${EDITOR:-vi}" "$TMP"

if cmp -s "$TMP" "$TMP.orig"; then
    echo "変更がないため中止しました。"
    exit 0
fi

# コメントと空行を落として JSON 配列にする
ARGS_JSON="$(grep -v -e '^[[:space:]]*#' -e '^[[:space:]]*$' "$TMP" \
    | sed 's/[[:space:]]*$//' \
    | python3 -c 'import sys,json; print(json.dumps([l.rstrip("\n") for l in sys.stdin]))')"

if [ "$ARGS_JSON" = "[]" ]; then
    echo "引数が空のため中止しました。"
    exit 0
fi

echo "--- 適用する引数 ---"
python3 -c 'import sys,json;[print(" ",a) for a in json.loads(sys.argv[1])]' "$ARGS_JSON"
echo

uv run ansible-playbook playbooks/llm_edit_args.yml -i "$INVENTORY" \
    -e "model=${MODEL}" \
    -e "edit_args_wait=${EDIT_ARGS_WAIT:-600}" \
    -e "{\"vllm_args\": ${ARGS_JSON}}"
