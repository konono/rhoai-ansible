#!/usr/bin/env python3
"""
scripts/setup-opencode.sh の API キー発行まわりの回帰テスト。

過去の不具合:
  - .vllm-token があれば発行先ユーザーを確認せず再利用していた。
    OPENCODE_API_USER=admin1 で実行したあと testuser1 で再実行しても、
    管理者のキーが設定に残ってしまう。
  - X-MaaS-Group が常に ["maas-admins"] だった。一般ユーザー向けのキーにも
    管理者グループが紐づいていた。

oc をスタブして、クラスタ無しでスクリプトの振る舞いを検証する。

実行:
    python3 -m unittest tests.test_setup_opencode
"""

import fcntl
import json
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
SCRIPT = ANSIBLE_DIR / "scripts" / "setup-opencode.sh"

# oc のスタブ。呼ばれた引数を CALL_LOG に追記し、
# maas-api への POST には固定の API キーを返す。
OC_STUB = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$CALL_LOG"
case "$1 $2" in
  "whoami "*) echo "kube:admin"; exit 0 ;;
esac
case "$*" in
  *"gateway maas-default-gateway"*) echo "maas.apps.example.com"; exit 0 ;;
  *"get pods"*"maas-api"*)          echo "maas-api-stub";          exit 0 ;;
  *"get llminferenceservice"*)
      cat <<'EOF'
{"items":[{"metadata":{"name":"demo-model","namespace":"llm-serving"},
 "status":{"conditions":[{"type":"Ready","status":"True"}]},
 "spec":{"template":{"containers":[{"args":["--max-model-len=4096"]}]}}}]}
EOF
      exit 0 ;;
  *"api-keys"*) echo '{"id":"id-1","key":"sk-oai-STUBKEY"}'; exit 0 ;;
esac
exit 0
"""


@unittest.skipUnless(SCRIPT.exists(), "setup-opencode.sh がありません")
class TestSetupOpencodeApiKey(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.workdir = self.tmp / "pb"
        (self.workdir / "scripts").mkdir(parents=True)
        (self.workdir / "inventory" / "myenv" / "group_vars" / "all").mkdir(parents=True)
        shutil.copyfile(SCRIPT, self.workdir / "scripts" / "setup-opencode.sh")

        # グループ解決に使う inventory
        (self.workdir / "inventory" / "myenv" / "group_vars" / "all"
         / "components.yml").write_text(
            "keycloak_users:\n"
            "  - username: admin1\n"
            "    groups: [maas-admins]\n"
            "  - username: testuser1\n"
            "    groups: [maas-demo-model-users]\n"
        )

        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        oc = bin_dir / "oc"
        oc.write_text(OC_STUB)
        oc.chmod(0o755)
        self.bin_dir = bin_dir
        self.call_log = self.tmp / "calls.log"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, user):
        env = {
            **os.environ,
            "PATH": f"{self.bin_dir}:{os.environ['PATH']}",
            "CALL_LOG": str(self.call_log),
            "OPENCODE_API_USER": user,
            "OPENCODE_INVENTORY": str(self.workdir / "inventory" / "myenv"),
        }
        return subprocess.run(
            ["bash", "scripts/setup-opencode.sh", "setup"],
            cwd=str(self.workdir), capture_output=True, env=env,
        )

    def _issued_groups(self):
        """スタブに渡された X-MaaS-Group を新しい順に返す"""
        if not self.call_log.exists():
            return []
        out = []
        for line in self.call_log.read_text().splitlines():
            if "api-keys" in line and "X-MaaS-Group:" in line:
                out.append(line.split("X-MaaS-Group:", 1)[1].split("-H")[0].strip())
        return out

    def test_group_comes_from_inventory_not_hardcoded_admin(self):
        r = self._run("testuser1")
        self.assertEqual(r.returncode, 0, r.stderr.decode()[-2000:])
        groups = self._issued_groups()
        self.assertTrue(groups, "API キー発行が呼ばれていません")
        self.assertIn("maas-demo-model-users", groups[-1],
                      "一般ユーザーのキーに inventory のグループが使われていません")
        self.assertNotIn("maas-admins", groups[-1],
                         "一般ユーザーのキーに管理者グループが紐づいています")

    def test_key_is_reissued_when_user_changes(self):
        r1 = self._run("admin1")
        self.assertEqual(r1.returncode, 0, r1.stderr.decode()[-2000:])
        meta = json.loads((self.workdir / ".vllm-token.meta").read_text())
        self.assertEqual(meta["user"], "admin1")
        self.assertEqual(meta["groups"], ["maas-admins"])

        before = len(self._issued_groups())
        r2 = self._run("testuser1")
        self.assertEqual(r2.returncode, 0, r2.stderr.decode()[-2000:])
        after = len(self._issued_groups())

        self.assertGreater(
            after, before,
            "ユーザーを切り替えたのに API キーが再発行されていません",
        )
        meta = json.loads((self.workdir / ".vllm-token.meta").read_text())
        self.assertEqual(meta["user"], "testuser1")
        self.assertEqual(meta["groups"], ["maas-demo-model-users"])

    def test_key_is_reused_for_same_user(self):
        self._run("admin1")
        before = len(self._issued_groups())
        self._run("admin1")
        self.assertEqual(
            len(self._issued_groups()), before,
            "同じユーザーなのに API キーが再発行されています",
        )

    def test_token_files_are_not_world_readable(self):
        self._run("admin1")
        for f in (".vllm-token", ".vllm-token.meta"):
            p = self.workdir / f
            self.assertTrue(p.exists(), f"{f} が作られていません")
            self.assertEqual(oct(p.stat().st_mode & 0o777), "0o600", f)


if __name__ == "__main__":
    unittest.main(verbosity=2)
