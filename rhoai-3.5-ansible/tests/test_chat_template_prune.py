#!/usr/bin/env python3
"""chat template ConfigMap の prune 条件の検証。

モデルを absent にしたり custom_chat_template を外すと、作成済みの ConfigMap が
クラスタに残り続けていた（実機で確認）。そこで prune を入れたが、削除条件を
誤ると「稼働中のモデルが参照している ConfigMap」まで消してしまう。

守るべき条件:
  - 参照されていない <モデル名>-chat-template だけを削除する
  - custom_chat_template なしで chat_template_configmap だけ指定する使い方
    （利用者が自分で作った ConfigMap をマウントする）は削除しない
  - chat_template_configmap で名前を上書きしている場合も削除しない

Ansible 側 (roles/llm_serving/tasks/main.yml) と手動手順 (render.py) の
両方を同じケースで確認する。

実行:
    python3 -m unittest tests.test_chat_template_prune
"""

import fcntl
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

# Fix non-blocking IO for aw container
for fd in [sys.stdout, sys.stderr]:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)

ANSIBLE_DIR = Path(__file__).resolve().parent.parent
RENDER = ANSIBLE_DIR / "render-manifests" / "render.py"
TASKS = ANSIBLE_DIR / "roles" / "llm_serving" / "tasks" / "main.yml"
SAMPLE_INV = ANSIBLE_DIR / "inventory" / "sample"


def _render_guide(models, extra=None):
    """models を差し替えて render.py を回し、手順書を返す"""
    tmp = Path(tempfile.mkdtemp())
    try:
        inv = tmp / "inv"
        shutil.copytree(SAMPLE_INV, inv)
        gv = inv / "group_vars" / "all"
        comp = gv / "components.yml.sample"
        cfg = yaml.safe_load(comp.read_text())
        cfg["models"] = models
        cfg.update(extra or {})
        comp.write_text(yaml.safe_dump(cfg, allow_unicode=True))
        out = tmp / "out"
        r = subprocess.run(
            [sys.executable, str(RENDER), "-c", str(inv), "-o", str(out)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise AssertionError(r.stdout + r.stderr)
        return (out / "manual-deploy-guide.md").read_text()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _deleted_cms(guide):
    return {
        ln.split()[3] for ln in guide.splitlines()
        if ln.startswith("oc delete configmap") and "chat-template" in ln
    }


@unittest.skipUnless(RENDER.exists(), "render.py がありません")
class TestRenderGuidePrune(unittest.TestCase):

    def test_absent_model_configmap_is_deleted(self):
        """absent にしたモデルの ConfigMap は削除対象になる"""
        guide = _render_guide({
            "qwen3-06b": {"hf_repo": "Qwen/Qwen3-0.6B", "state": "absent"},
        })
        self.assertIn("qwen3-06b-chat-template", _deleted_cms(guide))

    def test_referenced_configmap_is_not_deleted(self):
        """custom_chat_template を使う present モデルの ConfigMap は残す"""
        guide = _render_guide({
            "qwen3-06b": {"hf_repo": "Qwen/Qwen3-0.6B", "state": "present"},
        })
        self.assertNotIn("qwen3-06b-chat-template", _deleted_cms(guide))

    def test_user_provided_configmap_is_not_deleted(self):
        """custom_chat_template なしで chat_template_configmap だけ指定した場合

        利用者が自分で作った ConfigMap をマウントする使い方。実体はこちらが
        作っていないので、消してはいけない。
        """
        guide = _render_guide({
            "demo": {
                "hf_repo": "org/demo",
                "state": "present",
                "custom_chat_template": "",
                "chat_template_configmap": "demo-chat-template",
            },
        })
        self.assertNotIn(
            "demo-chat-template", _deleted_cms(guide),
            "利用者が用意した ConfigMap を削除対象にしています",
        )

    def test_overridden_name_is_not_deleted(self):
        """chat_template_configmap で名前を上書きした場合も残す"""
        guide = _render_guide({
            "demo": {
                "hf_repo": "org/demo",
                "state": "present",
                "custom_chat_template": "qwen3",
                "chat_template_configmap": "demo-chat-template",
            },
        })
        self.assertNotIn("demo-chat-template", _deleted_cms(guide))


@unittest.skipUnless(TASKS.exists(), "llm_serving の tasks がありません")
class TestAnsiblePruneExpression(unittest.TestCase):
    """Ansible 側の prune 条件が参照中の名前を除外していること

    式そのものを Jinja で評価して確かめる。
    """

    EXPR = (
        "{{ (models | dict2items | map(attribute='key')"
        " | map('regex_replace', '$', '-chat-template') | list)"
        " | difference(refs) | difference(specs) }}"
    )

    def _eval(self, models, refs, specs):
        from jinja2 import Environment

        env = Environment()
        env.filters["dict2items"] = lambda d: [
            {"key": k, "value": v} for k, v in d.items()
        ]
        env.filters["difference"] = lambda a, b: [x for x in a if x not in b]
        import re
        env.filters["regex_replace"] = (
            lambda v, p, r: re.sub(p, r, str(v))
        )
        return env.from_string(self.EXPR).render(
            models=models, refs=refs, specs=specs
        )

    def test_task_file_subtracts_referenced_names(self):
        """tasks/main.yml が参照中の名前を引いていること"""
        s = TASKS.read_text()
        self.assertIn("difference(_chat_template_refs | default([]))", s,
                      "prune が参照中の ConfigMap 名を除外していません")

    def test_referenced_name_survives(self):
        out = self._eval(
            {"demo": {}},
            refs=["demo-chat-template"],
            specs=[],
        )
        self.assertNotIn("demo-chat-template", out)

    def test_unreferenced_name_is_pruned(self):
        out = self._eval({"demo": {}}, refs=[], specs=[])
        self.assertIn("demo-chat-template", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
