#!/usr/bin/env bash
set -euo pipefail
#
# Disconnected 環境で実行: ローカルの wheels から Python 環境を構築
#
# すべてのファイルは ansible/ ディレクトリ内に閉じます（ホストに影響しません）:
#   ansible/.venv/        — Python 仮想環境
#   ansible/collections/  — Ansible Galaxy collections
#
# Usage:
#   cd ansible
#   bash scripts/setup-env.sh
#
# 実行後:
#   uv run ansible-playbook site.yml -i inventory/myenv
#   # または
#   source .venv/bin/activate && ansible-playbook site.yml -i inventory/myenv
#
# 前提:
#   - Python 3.11+ がインストール済み
#   - uv がインストール済み（なければ pip フォールバック）
#   - vendor/wheels/ にダウンロード済みの wheels が配置済み
#

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ANSIBLE_DIR="${SCRIPT_DIR}/.."
VENDOR_DIR="${ANSIBLE_DIR}/vendor"
WHEELS_DIR="${VENDOR_DIR}/wheels"
COLLECTIONS_DIR="${VENDOR_DIR}/collections"
VENV_DIR="${ANSIBLE_DIR}/.venv"

# --- uv の確認・インストール ---
if ! command -v uv &>/dev/null; then
    UV_DIR="${VENDOR_DIR}/uv"
    if [ -d "$UV_DIR" ]; then
        echo "uv が見つかりません。vendor/uv/ からインストールします..."
        # プラットフォーム検出
        ARCH=$(uname -m)
        OS=$(uname -s | tr '[:upper:]' '[:lower:]')
        case "${OS}-${ARCH}" in
            darwin-arm64)  UV_TARBALL="uv-aarch64-apple-darwin.tar.gz" ;;
            darwin-x86_64) UV_TARBALL="uv-x86_64-apple-darwin.tar.gz" ;;
            linux-aarch64) UV_TARBALL="uv-aarch64-unknown-linux-gnu.tar.gz" ;;
            linux-x86_64)  UV_TARBALL="uv-x86_64-unknown-linux-gnu.tar.gz" ;;
            *) echo "WARN: uv binary not available for ${OS}-${ARCH}" ;;
        esac
        if [ -n "${UV_TARBALL:-}" ] && [ -f "$UV_DIR/$UV_TARBALL" ]; then
            mkdir -p "${ANSIBLE_DIR}/.local/bin"
            tar xzf "$UV_DIR/$UV_TARBALL" -C "${ANSIBLE_DIR}/.local/bin" --strip-components=1 2>/dev/null || \
            tar xzf "$UV_DIR/$UV_TARBALL" -C "${ANSIBLE_DIR}/.local/bin" 2>/dev/null
            chmod +x "${ANSIBLE_DIR}/.local/bin/uv" 2>/dev/null
            export PATH="${ANSIBLE_DIR}/.local/bin:$PATH"
            echo "uv をインストールしました: ${ANSIBLE_DIR}/.local/bin/uv"
        fi
    fi
fi

# --- Python の確認・インストール ---
# wheels は特定の cpython バージョン向けにビルドされているため、
# venv の Python もそのメジャー.マイナーに合わせる必要がある
TARGET_PY="3.12"
PYTHON_DIR="${VENDOR_DIR}/python"

# まず vendor/python にスタンドアロンビルドがあればそれを優先する
_use_vendor_python=false
if [ -d "$PYTHON_DIR" ]; then
    ARCH=$(uname -m)
    OS=$(uname -s | tr '[:upper:]' '[:lower:]')
    case "${OS}-${ARCH}" in
        darwin-arm64)      TARGET="aarch64-apple-darwin" ;;
        darwin-x86_64)     TARGET="x86_64-apple-darwin" ;;
        linux-aarch64)     TARGET="aarch64-unknown-linux-gnu" ;;
        linux-x86_64)      TARGET="x86_64-unknown-linux-gnu" ;;
        *) TARGET="" ;;
    esac
    if [ -n "$TARGET" ]; then
        PY_TARBALL=$(ls "$PYTHON_DIR"/cpython-${TARGET_PY}.*-${TARGET}-install_only.tar.gz 2>/dev/null | head -1)
        if [ -n "${PY_TARBALL:-}" ]; then
            _use_vendor_python=true
        fi
    fi
fi

# システム Python が TARGET_PY と一致するか確認
_sys_py_ok=false
if python3 --version 2>/dev/null | grep -qE "^Python ${TARGET_PY}\."; then
    _sys_py_ok=true
fi

if [ "$_use_vendor_python" = true ]; then
    echo "vendor/python/ から Python ${TARGET_PY} をインストールします..."
    PY_INSTALL="${ANSIBLE_DIR}/.local/python"
    mkdir -p "$PY_INSTALL"
    tar xzf "$PY_TARBALL" -C "$PY_INSTALL" --strip-components=1
    export PATH="${PY_INSTALL}/bin:$PATH"
    echo "Python をインストールしました: $(python3 --version) at ${PY_INSTALL}/bin/python3"
elif [ "$_sys_py_ok" = true ]; then
    echo "System Python $(python3 --version 2>&1) を使用します"
elif python3 --version 2>/dev/null | grep -qE '3\.(1[1-9]|[2-9][0-9])'; then
    _actual=$(python3 --version 2>&1)
    echo "WARN: wheels は Python ${TARGET_PY} 向けです。システムの ${_actual} ではバイナリ wheel のインストールに失敗する可能性があります。"
    echo "      vendor/python/ にスタンドアロンビルドを配置するか、Python ${TARGET_PY} をインストールしてください。"
else
    echo "Python 3.11+ が見つかりません。"
    echo "ERROR: Python 3.11+ が必要です。vendor/python/ にスタンドアロンビルドを配置するか、Python をインストールしてください。"
    exit 1
fi

# --- wheels の確認 ---
if [ ! -d "$WHEELS_DIR" ] || [ -z "$(ls "$WHEELS_DIR"/*.whl 2>/dev/null)" ]; then
    echo "ERROR: $WHEELS_DIR に wheels が見つかりません。"
    echo "オンライン環境で download-deps.sh を実行してください。"
    exit 1
fi

# 使用する Python インタプリタのパスを確定
TARGET_PYTHON="$(command -v python3)"
echo "Using Python: ${TARGET_PYTHON} ($(python3 --version 2>&1))"
echo "=== Python 仮想環境の構築 (${VENV_DIR}) ==="

# --- uv で venv 作成 + インストール ---
if command -v uv &>/dev/null; then
    cd "$ANSIBLE_DIR"
    # --python で vendor/system の 3.12 を明示し、uv が別バージョンを掴むのを防ぐ
    UV_NO_SYNC=0 uv sync --python "$TARGET_PYTHON" --no-install-project --offline 2>&1 || {
        echo "uv sync failed, trying manual venv + install..."
        uv venv --python "$TARGET_PYTHON" "$VENV_DIR" 2>/dev/null || true
        uv pip install \
            --python "$VENV_DIR/bin/python" \
            --no-index \
            --find-links "$WHEELS_DIR" \
            ansible-core ansible kubernetes jmespath
    }
elif command -v pip &>/dev/null; then
    echo "uv が見つかりません。pip でフォールバックします..."
    # venv をプロジェクト内に作成（ホストに影響しない）
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --no-index --find-links "$WHEELS_DIR" \
        ansible-core ansible kubernetes jmespath
else
    echo "ERROR: uv も pip も見つかりません。"
    echo "uv のインストール: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

echo ""

# --- Ansible Galaxy collections ---
# ansible.cfg の collections_paths = collections に合わせてインストール
INSTALL_PATH="${ANSIBLE_DIR}/collections"
if [ -d "$COLLECTIONS_DIR" ] && [ -n "$(ls "$COLLECTIONS_DIR"/*.tar.gz 2>/dev/null)" ]; then
    echo "=== Ansible Galaxy collections のインストール (${INSTALL_PATH}) ==="
    mkdir -p "$INSTALL_PATH"

    # .venv 内の ansible-galaxy を使用
    ANSIBLE_GALAXY="${VENV_DIR}/bin/ansible-galaxy"
    if [ ! -x "$ANSIBLE_GALAXY" ]; then
        # uv run 経由で探す
        if command -v uv &>/dev/null; then
            ANSIBLE_GALAXY="uv run ansible-galaxy"
        else
            ANSIBLE_GALAXY="ansible-galaxy"
        fi
    fi

    for tarball in "$COLLECTIONS_DIR"/*.tar.gz; do
        echo "  Installing: $(basename "$tarball")"
        $ANSIBLE_GALAXY collection install "$tarball" --force -p "$INSTALL_PATH" 2>/dev/null || true
    done
fi

echo ""
echo "=== セットアップ完了 ==="
echo ""
echo "インストール先（すべて ansible/ 内に閉じています）:"
echo "  Python 環境:  ${VENV_DIR}"
echo "  Collections:  ${INSTALL_PATH}"
echo ""
echo "使い方:"
echo "  cd ansible"
echo "  uv run ansible-playbook site.yml -i inventory/myenv"
echo ""
echo "確認:"
echo "  uv run ansible-playbook --version"
