"""Release updater tests using local archives only."""

import hashlib
import importlib.util
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "self-update.py"
SPEC = importlib.util.spec_from_file_location("self_update", SCRIPT)
UPDATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(UPDATE)


def archive_with(files):
    payload = dict(files)
    payload[UPDATE.MANIFEST] = "\n".join(sorted(files)) + "\n"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in payload.items():
            data = text.encode()
            info = tarfile.TarInfo(f"{UPDATE.PROJECT}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class SelfUpdateTests(unittest.TestCase):
    def test_archive_accepts_parent_directories_for_sample_inventory(self):
        payload = {
            "VERSION": "0.2.0\n",
            "inventory/sample/hosts.yml.sample": "all: {}\n",
        }
        manifest = "\n".join(sorted(payload)) + "\n"
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for directory in (UPDATE.PROJECT, f"{UPDATE.PROJECT}/inventory",
                              f"{UPDATE.PROJECT}/inventory/sample"):
                info = tarfile.TarInfo(directory)
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            for name, text in {**payload, UPDATE.MANIFEST: manifest}.items():
                data = text.encode()
                info = tarfile.TarInfo(f"{UPDATE.PROJECT}/{name}")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        with tempfile.TemporaryDirectory() as temp:
            files = UPDATE.extract_archive(buf.getvalue(), Path(temp))
            self.assertIn("inventory/sample/hosts.yml.sample", files)

    def test_cli_previews_then_applies_verified_archive(self):
        data = archive_with({"VERSION": "0.2.0\n", "site.yml": "---\n"})
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "installed"
            root.mkdir()
            (root / "VERSION").write_text("0.1.0\n")
            archive = base / UPDATE.ASSET
            archive.write_bytes(data)
            checksums = base / "checksums.txt"
            checksums.write_text(f"{hashlib.sha256(data).hexdigest()}  {UPDATE.ASSET}\n")
            args = ["--archive", str(archive), "--checksums", str(checksums)]
            self.assertEqual(UPDATE.main(args, root=root), 0)
            self.assertFalse((root / "site.yml").exists())
            self.assertEqual(UPDATE.main(args + ["--apply"], root=root), 0)
            self.assertEqual((root / "site.yml").read_text(), "---\n")
            self.assertEqual((root / "VERSION").read_text(), "0.2.0\n")

    def test_checksum_rejects_modified_archive(self):
        data = archive_with({"VERSION": "0.2.0\n"})
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            UPDATE.verify_checksum(data + b"x", f"{hashlib.sha256(data).hexdigest()}  {UPDATE.ASSET}")

    def test_rejects_environment_inventory(self):
        data = archive_with({"VERSION": "0.2.0\n", "inventory/myenv/vault.yml": "secret"})
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "Environment inventory"):
                UPDATE.extract_archive(data, Path(temp))

    def test_rejects_symlinked_install_path(self):
        data = archive_with({"VERSION": "0.2.0\n", "scripts/new.sh": "new\n"})
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "installed"
            root.mkdir()
            (root / "VERSION").write_text("0.1.0\n")
            outside = Path(temp) / "outside"
            outside.mkdir()
            (root / "scripts").symlink_to(outside, target_is_directory=True)
            staged = Path(temp) / "staged"
            staged.mkdir()
            files = UPDATE.extract_archive(data, staged)
            with self.assertRaisesRegex(ValueError, "symlink"):
                UPDATE.changes(root, staged, files)
            self.assertEqual(list(outside.iterdir()), [])

    def test_apply_preserves_runtime_and_removes_only_old_managed_files(self):
        data = archive_with({"VERSION": "0.2.0\n", "scripts/new.sh": "new\n"})
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "installed"
            root.mkdir()
            (root / "VERSION").write_text("0.1.0\n")
            (root / "old.txt").write_text("old\n")
            (root / UPDATE.MANIFEST).write_text("VERSION\nold.txt\n")
            secret = root / "inventory" / "myenv" / "vault.yml"
            secret.parent.mkdir(parents=True)
            secret.write_text("secret\n")
            runtime = root / ".venv" / "marker"
            runtime.parent.mkdir()
            runtime.write_text("runtime\n")
            staged = Path(temp) / "staged"
            staged.mkdir()
            files = UPDATE.extract_archive(data, staged)
            replace, remove = UPDATE.changes(root, staged, files)
            self.assertIn("old.txt", remove)
            UPDATE.apply(root, staged, replace, remove)
            self.assertEqual((root / "VERSION").read_text(), "0.2.0\n")
            self.assertFalse((root / "old.txt").exists())
            self.assertEqual(secret.read_text(), "secret\n")
            self.assertEqual(runtime.read_text(), "runtime\n")


if __name__ == "__main__":
    unittest.main()
