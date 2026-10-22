#!/usr/bin/env python3
"""Update the playbook source from a verified GitHub Release or local archive."""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

REPO = "konono/rhoai-ansible"
PROJECT = "rhoai-3.5-ansible"
ASSET = f"{PROJECT}.tar.gz"
MANIFEST = ".release-files.txt"
ROOT = Path(__file__).resolve().parent.parent
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def fail(message):
    raise ValueError(message)


def version_tuple(value):
    match = VERSION_RE.fullmatch(value.strip())
    if not match:
        fail(f"Invalid version: {value!r}")
    return tuple(map(int, match.groups()))


def download(url):
    request = urllib.request.Request(url, headers={"User-Agent": "rhoai-self-update"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def latest_release():
    data = json.loads(download(f"https://api.github.com/repos/{REPO}/releases/latest"))
    assets = {item["name"]: item["browser_download_url"] for item in data["assets"]}
    for name in (ASSET, "checksums.txt"):
        if name not in assets:
            fail(f"Release {data['tag_name']} is missing {name}")
    return data["tag_name"], assets


def verify_checksum(archive, checksum_text):
    matches = []
    for line in checksum_text.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2 and parts[1].lstrip("*") == ASSET:
            matches.append(parts[0])
    if len(matches) != 1 or not re.fullmatch(r"[0-9a-fA-F]{64}", matches[0]):
        fail(f"checksums.txt must contain exactly one SHA-256 entry for {ASSET}")
    actual = hashlib.sha256(archive).hexdigest()
    if actual.lower() != matches[0].lower():
        fail(f"SHA-256 mismatch for {ASSET}")


def managed_path(name):
    """Only release-owned source files may be installed or removed."""
    path = PurePosixPath(name)
    parts = path.parts
    if not parts or path.is_absolute() or any(part in ("", ".", "..") for part in parts):
        fail(f"Unsafe archive path: {name}")
    if parts[0] in {".venv", ".local", ".kube", ".credentials", "vendor", "collections", ".git", "__pycache__"}:
        fail(f"Runtime path in release: {name}")
    if parts[0] == "inventory" and (len(parts) < 2 or parts[1] != "sample"):
        fail(f"Environment inventory in release: {name}")
    if path.name in {"vault.yml", "uv.lock", ".generated-passwords.yml", ".vllm-token", "opencode.json"}:
        fail(f"Secret or runtime file in release: {name}")
    return Path(*parts)


def extract_archive(archive, destination):
    files = set()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar:
            path = PurePosixPath(member.name)
            if not path.parts or path.parts[0] != PROJECT:
                fail(f"Unexpected archive member: {member.name}")
            if len(path.parts) == 1:
                if not member.isdir():
                    fail("Project root must be a directory")
                continue
            if member.isdir():
                continue
            rel = managed_path(str(PurePosixPath(*path.parts[1:])))
            if not member.isfile():
                fail(f"Links and special files are not allowed: {member.name}")
            if str(rel) in files:
                fail(f"Duplicate archive member: {member.name}")
            files.add(str(rel))
            target = destination / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as source, target.open("wb") as out:
                shutil.copyfileobj(source, out)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
    if "VERSION" not in files or MANIFEST not in files:
        fail("Release is missing VERSION or file manifest")
    declared = set((destination / MANIFEST).read_text().splitlines())
    if declared != files - {MANIFEST}:
        fail("Release file manifest does not match archive contents")
    return files


def previous_files(root):
    manifest = root / MANIFEST
    if manifest.is_symlink():
        fail("Installed release manifest must not be a symlink")
    if not manifest.exists():
        return set()
    return {str(managed_path(name)) for name in manifest.read_text().splitlines() if name}


def check_target(root, name):
    path = root
    for part in Path(name).parts:
        path = path / part
        if path.is_symlink():
            fail(f"Installed path is a symlink: {name}")


def changes(root, staged, new_files):
    old_files = previous_files(root)
    for name in old_files | new_files:
        check_target(root, name)
    replace = sorted(name for name in new_files if not (root / name).is_file() or
                     (root / name).read_bytes() != (staged / name).read_bytes())
    remove = sorted(name for name in old_files - new_files if (root / name).is_file())
    return replace, remove


def apply(root, staged, replace, remove):
    for name in replace + remove:
        check_target(root, name)
    # Back up every touched file before changing the installation.
    with tempfile.TemporaryDirectory(prefix="rhoai-update-backup-") as backup_dir:
        backup = Path(backup_dir)
        existing = [name for name in replace + remove if (root / name).is_file()]
        for name in existing:
            target = backup / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / name, target)
        touched = []
        try:
            for name in replace:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temp:
                    temp_path = Path(temp.name)
                    with (staged / name).open("rb") as source:
                        shutil.copyfileobj(source, temp)
                temp_path.chmod((staged / name).stat().st_mode & 0o777)
                os.replace(temp_path, target)
                touched.append(name)
            for name in remove:
                (root / name).unlink()
                touched.append(name)
        except OSError:
            for name in reversed(touched):
                target = root / name
                if name in existing:
                    shutil.copy2(backup / name, target)
                else:
                    target.unlink(missing_ok=True)
            raise


def main(argv=None, root=ROOT):
    parser = argparse.ArgumentParser(description="Preview or apply a verified playbook update")
    parser.add_argument("--archive", type=Path, help="Local release archive for disconnected use")
    parser.add_argument("--checksums", type=Path, help="checksums.txt for --archive")
    parser.add_argument("--apply", action="store_true", help="Apply after showing changes")
    args = parser.parse_args(argv)
    if bool(args.archive) != bool(args.checksums):
        parser.error("--archive and --checksums must be supplied together")

    current = (root / "VERSION").read_text().strip()
    if args.archive:
        archive = args.archive.read_bytes()
        checksums = args.checksums.read_text()
    else:
        tag, assets = latest_release()
        archive = download(assets[ASSET])
        checksums = download(assets["checksums.txt"]).decode()
    verify_checksum(archive, checksums)

    with tempfile.TemporaryDirectory(prefix="rhoai-update-") as temp_dir:
        staged = Path(temp_dir)
        new_files = extract_archive(archive, staged)
        target = (staged / "VERSION").read_text().strip()
        if not args.archive and version_tuple(target) != version_tuple(tag):
            fail(f"Release tag {tag} does not match archive VERSION {target}")
        if version_tuple(target) <= version_tuple(current):
            print(f"Already up to date: {current} (release {target})")
            return 0
        replace, remove = changes(root, staged, new_files)
        print(f"Update {current} -> {target}: {len(replace)} add/change, {len(remove)} remove")
        for name in replace:
            print(f"  + {name}")
        for name in remove:
            print(f"  - {name}")
        if not args.apply:
            print("Preview only. Add --apply to install this release.")
            return 0
        if (root / ".git").exists() or (root.parent / ".git").exists():
            fail("This is a Git checkout; use git to update it instead of --apply")
        apply(root, staged, replace, remove)
        print(f"Updated to {target}. Keep your inventory and runtime files; run setup-env.sh if dependencies changed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, tarfile.TarError, urllib.error.URLError) as error:
        print(f"Update failed: {error}", file=sys.stderr)
        sys.exit(1)
