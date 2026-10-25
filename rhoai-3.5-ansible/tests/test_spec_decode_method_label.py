#!/usr/bin/env python3
"""投機デコードの方式ラベル抽出の検証。

llm_verify_inference.yml は結果に投機デコードの方式を表示する。方式は
モデルごとに違う (Qwen3.5-9B は mtp、Qwen3-0.6B は ngram)。以前は常に
「MTP」と決め打ちで表示しており、ngram のモデルでも MTP と出ていた。

過去の不具合:
  - --speculative-config を持たないモデルだと regex_search が None を返し、
    そのまま first に渡して 'NoneType' object is not iterable で落ちていた。
    投機デコードを使わないモデルがあると検証 playbook 全体が失敗する。

式を Python で再実装すると Ansible の regex_search とズレるため、
verify_inference.yml から式を取り出して **実際の ansible-playbook** で
評価する。

実行:
    python3 -m unittest tests.test_spec_decode_method_label
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

import yaml

# Fix non-blocking IO for aw container
for fd in [sys.stdout, sys.stderr]:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)

ANSIBLE_DIR = Path(__file__).resolve().parent.parent
TASKS = ANSIBLE_DIR / "roles" / "llm_serving" / "tasks" / "verify_inference.yml"
RECIPES = ANSIBLE_DIR / "roles" / "llm_serving" / "vars" / "model_recipes.yml"

SPEC_MTP = '--speculative-config={"method":"mtp","num_speculative_tokens":1}'
SPEC_MTP_QUOTED = (
    '--speculative-config=\'{"method":"mtp","num_speculative_tokens":1}\''
)
SPEC_NGRAM_SPACED = (
    '--speculative-config={"method": "ngram", "num_speculative_tokens": 3}'
)


def _extract_expression():
    """verify_inference.yml から _vi_method の式を取り出す"""
    for task in yaml.safe_load(TASKS.read_text()):
        expr = (task.get("vars") or {}).get("_vi_method")
        if expr:
            return expr
    raise AssertionError("_vi_method の定義が見つかりません")


def _evaluate(cases):
    """{名前: vllm_args} を実際の Ansible で評価して {名前: 方式} を返す"""
    expr = _extract_expression()
    tmp = Path(tempfile.mkdtemp())
    try:
        pb = tmp / "eval.yml"
        pb.write_text(yaml.safe_dump([{
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "tasks": [{
                "name": "evaluate",
                "ansible.builtin.debug": {
                    "msg": "RESULT {{ item.key }} = {{ _vi_method }}"
                },
                "vars": {"_model_cfg": {"vllm_args": "{{ item.value }}"},
                         "_vi_method": expr},
                "loop": "{{ cases | dict2items }}",
                "loop_control": {"label": "{{ item.key }}"},
            }],
        }], default_flow_style=False, allow_unicode=True))
        r = subprocess.run(
            ["ansible-playbook", str(pb), "-i", "localhost,",
             "-e", json.dumps({"cases": cases})],
            capture_output=True, text=True,
            env={**os.environ, "ANSIBLE_LOCALHOST_WARNING": "False"},
        )
        if r.returncode != 0:
            raise AssertionError(
                "式の評価に失敗しました（端ケースで落ちていませんか）:\n"
                + r.stdout[-3000:]
            )
        out = {}
        for ln in r.stdout.splitlines():
            if "RESULT " in ln:
                body = ln.split("RESULT ", 1)[1].rstrip('"').strip()
                k, v = body.split(" = ", 1)
                out[k] = v.strip()
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _have_ansible():
    return shutil.which("ansible-playbook") is not None


@unittest.skipUnless(TASKS.exists(), "verify_inference.yml がありません")
@unittest.skipUnless(_have_ansible(), "ansible-playbook がありません")
class TestSpecDecodeMethodLabel(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.got = _evaluate({
            "mtp": [SPEC_MTP],
            "mtp_quoted": [SPEC_MTP_QUOTED],
            "ngram_spaced": [SPEC_NGRAM_SPACED],
            "no_spec_config": ["--max-model-len=4096"],
            "empty_args": [],
            "spec_without_method": [
                '--speculative-config={"num_speculative_tokens":1}'
            ],
        })

    def test_mtp(self):
        self.assertEqual(self.got["mtp"], "mtp")

    def test_mtp_already_quoted(self):
        """クラスタから読んだ値（クォート済み）でも拾えること"""
        self.assertEqual(self.got["mtp_quoted"], "mtp")

    def test_ngram_with_spaces(self):
        self.assertEqual(self.got["ngram_spaced"], "ngram")

    def test_no_speculative_config_does_not_raise(self):
        """投機デコードを使わないモデルで落ちないこと

        regex_search が None を返す経路。以前はここで
        'NoneType' object is not iterable になり、検証 playbook 全体が
        失敗していた。評価自体が失敗すると setUpClass で例外になる。
        """
        self.assertEqual(self.got["no_spec_config"], "未設定")

    def test_empty_args_does_not_raise(self):
        self.assertEqual(self.got["empty_args"], "未設定")

    def test_speculative_config_without_method(self):
        self.assertEqual(self.got["spec_without_method"], "未設定")


@unittest.skipUnless(RECIPES.exists(), "model_recipes.yml がありません")
@unittest.skipUnless(_have_ansible(), "ansible-playbook がありません")
class TestRecipesAreCovered(unittest.TestCase):
    """登録済みレシピすべてで方式を解決でき、落ちないこと"""

    def test_every_recipe_resolves(self):
        recipes = yaml.safe_load(RECIPES.read_text())["model_recipes"]
        got = _evaluate({
            name.replace("/", "_"): cfg.get("vllm_args", [])
            for name, cfg in recipes.items()
        })
        for name in recipes:
            key = name.replace("/", "_")
            with self.subTest(recipe=name):
                self.assertTrue(got.get(key), f"{name} で方式が空になりました")


if __name__ == "__main__":
    unittest.main(verbosity=2)
