#!/usr/bin/env bash
set -euo pipefail
#
# Playbook のソースファイルだけを tarball に固める（vendor / .venv / secrets を除外）
#
# Usage:
#   bash pack-playbook.sh rhoai-3.5-ansible
#   bash pack-playbook.sh rhoai-3.5-ansible -o /tmp/pb.tar.gz
#
# disconnected 先での展開:
#   bash unpack-playbook.sh rhoai-3.5-ansible-*.tar.gz
#

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${SCRIPT_DIR}"

OUTPUT=""
PROJECT_NAME=""

while [ $# -gt 0 ]; do
    case "$1" in
        -o|--output) OUTPUT="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: $(basename "$0") PROJECT_NAME [-o OUTPUT_PATH]"
            echo ""
            echo "  PROJECT_NAME  ansible ディレクトリ名 (例: rhoai-3.5-ansible)"
            echo "  -o PATH       出力先ファイルパス"
            echo ""
            echo "例:"
            echo "  $(basename "$0") rhoai-3.5-ansible"
            echo "  $(basename "$0") rhoai-3.5-ansible -o /tmp/pb.tar.gz"
            exit 0
            ;;
        -*) echo "ERROR: unknown option '$1'"; exit 1 ;;
        *)
            if [ -z "$PROJECT_NAME" ]; then
                PROJECT_NAME="$1"
            else
                echo "ERROR: unexpected argument '$1'"
                exit 1
            fi
            shift
            ;;
    esac
done

if [ -z "$PROJECT_NAME" ]; then
    # 自動検出: *-ansible ディレクトリを探す
    candidates=()
    for d in "$PROJECT_DIR"/*-ansible; do
        [ -d "$d" ] && candidates+=("$(basename "$d")")
    done
    if [ ${#candidates[@]} -eq 1 ]; then
        PROJECT_NAME="${candidates[0]}"
        echo "自動検出: $PROJECT_NAME"
    elif [ ${#candidates[@]} -gt 1 ]; then
        echo "ERROR: 複数の ansible ディレクトリが見つかりました。PROJECT_NAME を指定してください:"
        printf "  %s\n" "${candidates[@]}"
        exit 1
    else
        echo "ERROR: ansible ディレクトリが見つかりません。PROJECT_NAME を指定してください。"
        exit 1
    fi
fi

if [ ! -d "$PROJECT_DIR/$PROJECT_NAME" ]; then
    echo "ERROR: ディレクトリが存在しません: $PROJECT_DIR/$PROJECT_NAME"
    exit 1
fi

if [ -z "$OUTPUT" ]; then
    OUTPUT="${PROJECT_DIR}/${PROJECT_NAME}-$(date +%Y%m%d-%H%M%S).tar.gz"
fi

cd "$PROJECT_DIR"

# macOS の tar が ._AppleDouble ファイルを含めないようにする
export COPYFILE_DISABLE=1

tar czf "$OUTPUT" \
    --exclude='._*' \
    --exclude='.DS_Store' \
    --exclude='vendor' \
    --exclude='.venv' \
    --exclude='.local' \
    --exclude='collections' \
    --exclude='.kube' \
    --exclude='.credentials' \
    --exclude='.tracecraft' \
    --exclude='.generated-passwords.yml' \
    --exclude='.maas-policies-generated.yaml' \
    --exclude='*/vault.yml' \
    --exclude='uv.lock' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    "$PROJECT_NAME/"

SIZE=$(du -h "$OUTPUT" | cut -f1)
echo "=== Pack 完了 ==="
echo "  プロジェクト: $PROJECT_NAME"
echo "  出力: $OUTPUT"
echo "  サイズ: $SIZE"
echo ""
echo "含まれるもの:"
echo "  - playbooks, roles, tasks, templates"
echo "  - inventory (vault.yml 除外)"
echo "  - scripts (setup-env.sh, download-deps.sh)"
echo "  - pyproject.toml, requirements.yml, ansible.cfg"
echo ""
echo "除外されるもの:"
echo "  - vendor/ (.venv, wheels, collections, uv, python)"
echo "  - .credentials/, .kube/, vault.yml (シークレット)"
echo ""
echo "disconnected 先で展開:"
echo "  python3 unpack-playbook.py $(basename "$OUTPUT")"
echo ""
echo "差分確認のみ:"
echo "  python3 unpack-playbook.py --diff-only $(basename "$OUTPUT")"
