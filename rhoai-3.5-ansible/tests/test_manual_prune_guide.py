#!/usr/bin/env python3
"""manual-deploy-guide.md の「不要になった MaaS Policy の削除」手順の検証。

`oc apply` は manifest から消えたリソースを削除しないため、モデルを absent に
するとクラスタに古い MaaSAuthPolicy / MaaSSubscription が残り、存在しない
モデルを参照したまま Failed になる（実機で発生）。Ansible 側は
manage_maas_access で後始末するので、手順書側にも同じ処理が要る。

過去の不具合:
  - 残す名前の一覧を手順書の中で `python3 -c "import yaml ..."` して作って
    いた。素の python3 に PyYAML が無い環境では一覧が空になり、管理対象の
    ポリシーを「全件削除」すると判定してしまった。
    → 名前はレンダリング時に確定しているので手順書に直接埋め込む。
  - `oc get` が失敗しても削除ループへ進んでいた。
    → 取得に失敗したら削除せず中断する。

生成された手順書からシェルブロックを取り出し、oc をスタブして実行する。

実行:
    python3 -m unittest tests.test_manual_prune_guide
"""

import fcntl
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Fix non-blocking IO for aw container
for fd in [sys.stdout, sys.stderr]:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)

ANSIBLE_DIR = Path(__file__).resolve().parent.parent
RENDER = ANSIBLE_DIR / "render-manifests" / "render.py"
SAMPLE_INV = ANSIBLE_DIR / "inventory" / "sample"
HEADING = "### 不要になった MaaS Policy の削除"


@unittest.skipUnless(RENDER.exists(), "render.py がありません")
class TestManualPruneGuide(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        out = cls.tmp / "out"
        subprocess.run(
            [sys.executable, str(RENDER), "-c", str(SAMPLE_INV), "-o", str(out)],
            check=True, capture_output=True,
        )
        guide = (out / "manual-deploy-guide.md").read_text()
        if HEADING not in guide:
            raise AssertionError("prune 手順が手順書にありません")
        block = guide[guide.index(HEADING):]
        cls.script = block.split("```bash", 1)[1].split("```", 1)[0]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _run(self, oc_stub):
        """oc をスタブして prune スクリプトを実行する"""
        work = Path(tempfile.mkdtemp(dir=self.tmp))
        bin_dir = work / "bin"
        bin_dir.mkdir()
        oc = bin_dir / "oc"
        oc.write_text(oc_stub)
        oc.chmod(0o755)
        script = work / "prune.sh"
        script.write_text(self.script)
        return subprocess.run(
            ["bash", str(script)], cwd=str(work), capture_output=True, text=True,
            env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        )

    def test_script_is_valid_shell(self):
        r = subprocess.run(["bash", "-n"], input=self.script,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_keep_list_is_embedded_not_parsed_at_runtime(self):
        """残す名前は埋め込み済みで、実行時に YAML を読まないこと

        素の python3 に PyYAML が無い環境で一覧が空になり、全件削除に
        化けた不具合の再発防止。
        """
        self.assertIn("KEEP='", self.script)
        self.assertNotIn("import yaml", self.script)
        self.assertNotIn("python3", self.script)

    def test_nothing_is_deleted_when_listing_fails(self):
        """oc get が失敗したら 1 件も削除しないこと"""
        r = self._run("#!/bin/sh\nexit 1\n")
        self.assertNotIn("deleting", r.stdout,
                         "一覧取得に失敗したのに削除しています")
        self.assertIn("削除せず中断", r.stderr)

    def test_only_stale_policies_are_deleted(self):
        """生成物に無いものだけを削除し、あるものは残すこと"""
        stub = '''#!/bin/sh
case "$1 $2" in
  "get maasauthpolicy")
      echo "admin-auth-policy"; echo "stale-auth-policy"; exit 0 ;;
  "get maassubscription")
      echo "maas-admins-subscription"; echo "stale-subscription"; exit 0 ;;
  "delete "*) echo "DELETED $2"; exit 0 ;;
esac
exit 0
'''
        r = self._run(stub)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("DELETED maasauthpolicy/stale-auth-policy", r.stdout)
        self.assertIn("DELETED maassubscription/stale-subscription", r.stdout)
        # 生成物に含まれるものは消さない
        self.assertNotIn("admin-auth-policy", r.stdout.replace(
            "stale-auth-policy", ""))
        self.assertNotIn("DELETED maassubscription/maas-admins-subscription",
                         r.stdout)

    def test_only_managed_resources_are_listed(self):
        """管理外のリソースを巻き込まないよう label で絞っていること"""
        self.assertIn(
            "app.kubernetes.io/managed-by=ansible-maas-access", self.script
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
