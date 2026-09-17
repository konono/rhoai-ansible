#!/usr/bin/env bash
set -euo pipefail
#
# オンライン環境で実行: Python wheels と Ansible Galaxy collections をローカルにダウンロード
#
# Usage:
#   bash scripts/download-deps.sh            # 全部ダウンロード
#   bash scripts/download-deps.sh uv         # uv バイナリのみ
#   bash scripts/download-deps.sh python     # Python スタンドアロンビルドのみ
#   bash scripts/download-deps.sh wheels     # Python wheels のみ
#   bash scripts/download-deps.sh collections # Ansible Galaxy collections のみ
#   bash scripts/download-deps.sh python wheels  # 複数指定も可
#
# ダウンロード先:
#   ansible/vendor/uv/          — uv バイナリ
#   ansible/vendor/python/      — Python スタンドアロンビルド
#   ansible/vendor/wheels/      — Python wheels
#   ansible/vendor/collections/ — Ansible Galaxy collections
#

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ANSIBLE_DIR="${SCRIPT_DIR}/.."
VENDOR_DIR="${ANSIBLE_DIR}/vendor"
WHEELS_DIR="${VENDOR_DIR}/wheels"
COLLECTIONS_DIR="${VENDOR_DIR}/collections"

# ターゲット Python バージョン (wheels / standalone build 共通)
TARGET_PY_MAJOR_MINOR="3.12"
TARGET_PY_VERSION_TAG="312"

# --- 引数パース: 何をダウンロードするか ---
DO_UV=false
DO_PYTHON=false
DO_WHEELS=false
DO_COLLECTIONS=false

if [ $# -eq 0 ]; then
    DO_UV=true; DO_PYTHON=true; DO_WHEELS=true; DO_COLLECTIONS=true
else
    for arg in "$@"; do
        case "$arg" in
            uv)          DO_UV=true ;;
            python)      DO_PYTHON=true ;;
            wheels)      DO_WHEELS=true ;;
            collections) DO_COLLECTIONS=true ;;
            all)         DO_UV=true; DO_PYTHON=true; DO_WHEELS=true; DO_COLLECTIONS=true ;;
            -h|--help)
                echo "Usage: $(basename "$0") [uv] [python] [wheels] [collections] [all]"
                echo "  引数なし / all: 全部ダウンロード"
                echo "  uv:          uv バイナリ"
                echo "  python:      Python ${TARGET_PY_MAJOR_MINOR} スタンドアロンビルド"
                echo "  wheels:      Python wheels"
                echo "  collections: Ansible Galaxy collections"
                exit 0
                ;;
            *)
                echo "ERROR: unknown argument '$arg' (uv / python / wheels / collections / all)"
                exit 1
                ;;
        esac
    done
fi

# ============================
# uv バイナリ
# ============================
if [ "$DO_UV" = true ]; then
    echo "=== uv バイナリのダウンロード ==="
    UV_DIR="${VENDOR_DIR}/uv"
    mkdir -p "$UV_DIR"

    for pair in \
      "macOS ARM64:uv-aarch64-apple-darwin.tar.gz" \
      "macOS x86_64:uv-x86_64-apple-darwin.tar.gz" \
      "Linux aarch64:uv-aarch64-unknown-linux-gnu.tar.gz" \
      "Linux x86_64:uv-x86_64-unknown-linux-gnu.tar.gz"; do
        _label="${pair%%:*}"
        _file="${pair#*:}"
        echo "--- uv for ${_label} ---"
        curl -fsSL "https://github.com/astral-sh/uv/releases/latest/download/${_file}" \
          -o "$UV_DIR/${_file}" 2>&1 || echo "WARN: uv ${_label} download failed"
    done
    echo ""
fi

# ============================
# Python スタンドアロンビルド
# ============================
if [ "$DO_PYTHON" = true ]; then
    echo "=== Python ${TARGET_PY_MAJOR_MINOR} スタンドアロンビルドのダウンロード ==="
    PYTHON_DIR="${VENDOR_DIR}/python"
    mkdir -p "$PYTHON_DIR"

    echo "--- Fetching latest python-build-standalone release tag ---"
    PBS_TAG=""

    # 方法1: gh CLI (GITHUB_TOKEN で認証済み、レートリミットに強い)
    if command -v gh &>/dev/null; then
        PBS_TAG=$(gh api repos/astral-sh/python-build-standalone/releases/latest --jq '.tag_name' 2>/dev/null) || true
    fi

    # 方法2: releases/latest のリダイレクト先 URL からタグを抽出
    if [ -z "${PBS_TAG:-}" ]; then
        _redirect_url=$(curl -fsSL -o /dev/null -w '%{url_effective}' \
          "https://github.com/astral-sh/python-build-standalone/releases/latest" 2>/dev/null) || true
        if [ -n "${_redirect_url:-}" ]; then
            PBS_TAG=$(echo "$_redirect_url" | grep -o '[0-9]\{8\}$') || true
        fi
    fi

    # 方法3: curl + API (認証なし)
    if [ -z "${PBS_TAG:-}" ]; then
        PBS_TAG=$(curl -fsSL "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest" \
          | grep -o '"tag_name":"[^"]*"' | head -1 | cut -d'"' -f4) || true
    fi

    # 方法4: フォールバックタグ
    if [ -z "${PBS_TAG:-}" ]; then
        PBS_TAG="20260901"
        echo "WARN: Could not detect latest PBS tag, using fallback: ${PBS_TAG}"
    fi

    echo "Using PBS tag: ${PBS_TAG}"

    # リリースから対象 Python バージョンのパッチ番号を取得
    _release_json=""
    if command -v gh &>/dev/null; then
        _release_json=$(gh api "repos/astral-sh/python-build-standalone/releases/tags/${PBS_TAG}" 2>/dev/null) || true
    fi
    if [ -z "${_release_json:-}" ]; then
        _release_json=$(curl -fsSL "https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/${PBS_TAG}" 2>/dev/null) || true
    fi
    PYTHON_PATCH=$(echo "$_release_json" \
      | grep -o "cpython-${TARGET_PY_MAJOR_MINOR}\.[0-9]*+${PBS_TAG}" | head -1 \
      | sed "s/cpython-//;s/+.*//") || true

    if [ -z "${PYTHON_PATCH:-}" ]; then
        echo "WARN: No Python ${TARGET_PY_MAJOR_MINOR}.x found in PBS release ${PBS_TAG}"
    else
        echo "Found Python version: ${PYTHON_PATCH}"
        PBS_BASE="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}"

        for target in \
          "aarch64-apple-darwin" \
          "x86_64-apple-darwin" \
          "aarch64-unknown-linux-gnu" \
          "x86_64-unknown-linux-gnu"; do
            FNAME="cpython-${PYTHON_PATCH}+${PBS_TAG}-${target}-install_only.tar.gz"
            echo "--- Python ${PYTHON_PATCH} for ${target} ---"
            curl -fsSL "${PBS_BASE}/${FNAME}" -o "${PYTHON_DIR}/${FNAME}" 2>&1 || \
              echo "WARN: download failed for ${target}"
        done
    fi
    echo ""
fi

# ============================
# Python wheels
# ============================
if [ "$DO_WHEELS" = true ]; then
    echo "=== Python wheels のダウンロード ==="
    mkdir -p "$WHEELS_DIR"

    DEPS="ansible-core ansible>=10.0 kubernetes jmespath"

    download_wheels_pip() {
        local label="$1"
        local platform="$2"

        echo "--- ${label} wheels ---"
        pip download \
            --dest "$WHEELS_DIR" \
            --platform "$platform" \
            --python-version "$TARGET_PY_VERSION_TAG" \
            --only-binary=:all: \
            $DEPS 2>&1 || true
        pip download \
            --dest "$WHEELS_DIR" \
            --python-version "$TARGET_PY_VERSION_TAG" \
            --no-binary=:all: \
            --no-deps \
            $DEPS 2>&1 || true
        pip download \
            --dest "$WHEELS_DIR" \
            $DEPS 2>&1 || true
    }

    download_wheels_pip "macOS ARM64" "macosx_11_0_arm64"
    download_wheels_pip "macOS x86_64" "macosx_11_0_x86_64"
    download_wheels_pip "Linux aarch64" "manylinux2014_aarch64"
    download_wheels_pip "Linux x86_64" "manylinux2014_x86_64"
    echo ""
fi

# ============================
# Ansible Galaxy collections
# ============================
if [ "$DO_COLLECTIONS" = true ]; then
    echo "=== Ansible Galaxy collections のダウンロード ==="
    mkdir -p "$COLLECTIONS_DIR"

    # ansible-galaxy コマンドの検出
    if command -v ansible-galaxy &>/dev/null; then
        GALAXY_CMD="ansible-galaxy"
    elif pip show ansible-core &>/dev/null; then
        GALAXY_CMD="python3 -m ansible"
    else
        echo "ansible-galaxy not found, installing ansible-core temporarily..."
        _venv=$(mktemp -d)
        python3 -m venv "$_venv/venv"
        "$_venv/venv/bin/pip" install --quiet ansible-core 2>&1
        GALAXY_CMD="$_venv/venv/bin/ansible-galaxy"
    fi

    $GALAXY_CMD collection download \
        -r "${ANSIBLE_DIR}/requirements.yml" \
        -p "$COLLECTIONS_DIR" 2>&1 || {
        echo "collection download failed, falling back to install + cache..."
        _tmpdir=$(mktemp -d)
        $GALAXY_CMD collection install \
            -r "${ANSIBLE_DIR}/requirements.yml" \
            -p "$_tmpdir" \
            --force 2>&1
        for ns_dir in "$_tmpdir/ansible_collections"/*/*; do
            [ -d "$ns_dir" ] || continue
            _ns=$(basename "$(dirname "$ns_dir")")
            _name=$(basename "$ns_dir")
            _ver=$(grep -o '"version": *"[^"]*"' "$ns_dir/MANIFEST.json" 2>/dev/null | head -1 | grep -o '[0-9][0-9.]*' || echo "0.0.0")
            echo "Packaging: ${_ns}.${_name} ${_ver}"
            (cd "$(dirname "$ns_dir")/.." && tar czf "$COLLECTIONS_DIR/${_ns}-${_name}-${_ver}.tar.gz" "${_ns}/${_name}")
        done
        rm -rf "$_tmpdir"
    }

    [ -n "${_venv:-}" ] && rm -rf "$_venv"
    echo ""
fi

# ============================
# サマリ
# ============================
echo "=== ダウンロード完了 ==="
[ "$DO_UV" = true ] && echo "uv:          $(ls "${VENDOR_DIR}/uv"/*.tar.gz 2>/dev/null | wc -l) files"
[ "$DO_PYTHON" = true ] && echo "python:      $(ls "${VENDOR_DIR}/python"/*.tar.gz 2>/dev/null | wc -l) files"
[ "$DO_WHEELS" = true ] && echo "wheels:      $(ls "$WHEELS_DIR"/*.whl 2>/dev/null | wc -l) files"
[ "$DO_COLLECTIONS" = true ] && echo "collections: $(ls "$COLLECTIONS_DIR"/*.tar.gz 2>/dev/null | wc -l) files"
echo ""
echo "ディスコネクト環境に以下をコピーしてください:"
echo "  ansible/vendor/"
echo "  ansible/pyproject.toml"
echo ""
echo "インストール:"
echo "  cd ansible && bash scripts/setup-env.sh"
