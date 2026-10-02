#!/usr/bin/env python3
"""
pack-playbook.sh で作った tarball を展開先と比較し、差分を確認してから適用する。

- tarball を一時ディレクトリに展開
- 既存ディレクトリとの差分を表示（コード系のみ、環境固有ファイルは除外）
- ユーザー確認後に適用（rsync or shutil）
"""

import argparse
import difflib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

EXCLUDE_DIRS = {
    "vendor",
    ".venv",
    ".local",
    "collections",
    ".kube",
    ".credentials",
    ".tracecraft",
    "__pycache__",
}

EXCLUDE_FILES = {
    ".generated-passwords.yml",
    ".maas-policies-generated.yaml",
    "vault.yml",
    "uv.lock",
    ".DS_Store",
}

EXCLUDE_SUFFIXES = {".pyc"}
EXCLUDE_PREFIXES = {"._"}


def should_skip(rel_path: str) -> bool:
    parts = Path(rel_path).parts
    for part in parts[:-1]:
        if part in EXCLUDE_DIRS:
            return True
    name = parts[-1] if parts else ""
    if name in EXCLUDE_FILES:
        return True
    if any(name.startswith(p) for p in EXCLUDE_PREFIXES):
        return True
    if any(name.endswith(s) for s in EXCLUDE_SUFFIXES):
        return True
    return False


def collect_files(base_dir: Path) -> dict[str, str]:
    """ディレクトリ配下のファイルを {相対パス: 内容} で収集する（テキストのみ）"""
    result = {}
    for root, dirs, files in os.walk(base_dir):
        # 除外ディレクトリを walk から除去
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith("._")]
        for f in files:
            full = Path(root) / f
            rel = str(full.relative_to(base_dir))
            if should_skip(rel):
                continue
            try:
                result[rel] = full.read_text(encoding="utf-8")
            except (UnicodeDecodeError, PermissionError):
                pass  # バイナリファイルはスキップ
    return result


def show_diff(project_name: str, existing_files: dict, new_files: dict) -> bool:
    all_keys = sorted(set(existing_files) | set(new_files))
    has_diff = False

    added = []
    removed = []
    modified = []

    for key in all_keys:
        if key not in existing_files:
            added.append(key)
        elif key not in new_files:
            removed.append(key)
        elif existing_files[key] != new_files[key]:
            modified.append(key)

    if not added and not removed and not modified:
        return False

    # サマリ
    print("--- 変更サマリ ---")
    for f in added:
        print(f"  追加: {project_name}/{f}")
    for f in removed:
        print(f"  削除: {project_name}/{f}")
    for f in modified:
        print(f"  変更: {project_name}/{f}")

    print()
    print("--- 差分詳細 ---")

    for f in added:
        lines = new_files[f].splitlines(keepends=True)
        print(f"--- /dev/null")
        print(f"+++ b/{project_name}/{f}")
        print(f"@@ -0,0 +1,{len(lines)} @@")
        for line in lines:
            print(f"+{line}", end="" if line.endswith("\n") else "\n")

    for f in removed:
        lines = existing_files[f].splitlines(keepends=True)
        print(f"--- a/{project_name}/{f}")
        print(f"+++ /dev/null")
        print(f"@@ -1,{len(lines)} +0,0 @@")
        for line in lines:
            print(f"-{line}", end="" if line.endswith("\n") else "\n")

    for f in modified:
        old_lines = existing_files[f].splitlines(keepends=True)
        new_lines = new_files[f].splitlines(keepends=True)
        diff = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{project_name}/{f}",
            tofile=f"b/{project_name}/{f}",
        )
        sys.stdout.writelines(diff)

    return True


def apply_changes(src_dir: Path, dst_dir: Path, new_files: dict, existing_files: dict):
    all_keys = sorted(set(existing_files) | set(new_files))
    applied = 0

    for key in all_keys:
        dst_path = dst_dir / key
        src_path = src_dir / key

        if key not in existing_files:
            # 追加
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dst_path)
            applied += 1
        elif key not in new_files:
            # tarball に含まれないファイルは削除しない（環境固有の可能性）
            pass
        elif existing_files[key] != new_files[key]:
            # 変更
            shutil.copy2(src_path, dst_path)
            applied += 1

    print(f"\n=== 適用完了: {applied} ファイル更新 ===")
    print(f"  展開先: {dst_dir}")


def main():
    parser = argparse.ArgumentParser(
        description="tarball またはディレクトリと既存ディレクトリの差分を表示し、確認後に適用する"
    )
    parser.add_argument("source", help="tarball (.tar.gz) または展開済みディレクトリ")
    parser.add_argument("-d", "--dest", default=os.getcwd(),
                        help="比較先の親ディレクトリ (デフォルト: カレントディレクトリ)")
    parser.add_argument("--diff-only", action="store_true", help="差分表示のみ")
    parser.add_argument("--force", action="store_true", help="確認なしで適用")
    args = parser.parse_args()

    source = Path(args.source)
    dest_dir = Path(args.dest)

    if source.is_dir():
        # ディレクトリ同士の比較
        new_dir, project_name, tmp_ctx = _resolve_dir_source(source)
    elif source.is_file():
        new_dir, project_name, tmp_ctx = _resolve_tarball_source(source, dest_dir)
    else:
        print(f"ERROR: ファイル/ディレクトリが見つかりません: {source}", file=sys.stderr)
        sys.exit(1)

    print(f"プロジェクト: {project_name}")

    existing_dir = dest_dir / project_name
    if not existing_dir.is_dir():
        if source.is_file():
            print(f"既存ディレクトリが見つかりません: {existing_dir}")
            print("初回展開として tarball をそのまま展開します。")
            with tarfile.open(source, "r:gz") as tf:
                members = [m for m in tf.getmembers()
                           if not any(p.startswith("._") for p in Path(m.name).parts)
                           and ".DS_Store" not in m.name]
                tf.extractall(dest_dir, members=members)
            print("展開完了。")
        else:
            print(f"ERROR: 比較先が見つかりません: {existing_dir}", file=sys.stderr)
            sys.exit(1)
        _cleanup_tmp(tmp_ctx)
        return

    # new_dir と existing_dir が同じパスなら比較にならない
    if new_dir.resolve() == existing_dir.resolve():
        print("ERROR: ソースと比較先が同じディレクトリです。-d で比較先を指定してください。",
              file=sys.stderr)
        _cleanup_tmp(tmp_ctx)
        sys.exit(1)

    print(f"\n==========================================")
    print(f"  差分比較: {existing_dir}")
    print(f"       vs:  {source}")
    print(f"==========================================\n")

    existing_files = collect_files(existing_dir)
    new_files = collect_files(new_dir)

    has_diff = show_diff(project_name, existing_files, new_files)

    if not has_diff:
        print("差分なし — ディレクトリは同一です。")
        _cleanup_tmp(tmp_ctx)
        return

    print()

    if args.diff_only:
        _cleanup_tmp(tmp_ctx)
        return

    if not args.force:
        print("==========================================")
        try:
            answer = input("この差分を適用しますか？ [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n適用をキャンセルしました。")
            _cleanup_tmp(tmp_ctx)
            return
        if answer not in ("y", "yes"):
            print("適用をキャンセルしました。")
            _cleanup_tmp(tmp_ctx)
            return

    apply_changes(new_dir, existing_dir, new_files, existing_files)
    _cleanup_tmp(tmp_ctx)


def _resolve_dir_source(source: Path):
    """ディレクトリをソースとして使う場合の解決"""
    project_name = source.name
    return source, project_name, None


def _resolve_tarball_source(source: Path, dest_dir: Path):
    """tarball をソースとして使う場合: 一時展開して返す"""
    tmpdir = tempfile.mkdtemp()
    with tarfile.open(source, "r:gz") as tf:
        first = tf.getnames()[0]
        project_name = first.split("/")[0]
        members = [m for m in tf.getmembers()
                   if not any(p.startswith("._") for p in Path(m.name).parts)
                   and ".DS_Store" not in m.name]
        tf.extractall(tmpdir, members=members)
    return Path(tmpdir) / project_name, project_name, tmpdir


def _cleanup_tmp(tmp_ctx):
    if tmp_ctx and os.path.isdir(tmp_ctx):
        shutil.rmtree(tmp_ctx, ignore_errors=True)


if __name__ == "__main__":
    main()
