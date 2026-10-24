#!/usr/bin/env python3
"""playbooks/llm_edit_args.yml が実際にパッチする args の検証。

過去の不具合:
  - 渡された vllm_args をそのまま oc patch していた。KServe/llm-d の起動
    スクリプトは `eval "exec vllm serve ... $@"` で引数を評価するため、
      --speculative-config={"method":"mtp","num_speculative_tokens":1}
    が bash のブレース展開で 2 つの引数に分割され、MTP が壊れていた。
    通常デプロイのテンプレートは値をシングルクォートで保護しているのに、
    この playbook だけ素通しで、経路によって結果が変わっていた。
  - --served-model-name を自前で付与していた（KServe が付けるので重複）。

oc をスタブして playbook を実行し、patch に渡された JSON を検証する。

実行:
    python3 -m unittest tests.test_llm_edit_args
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
PLAYBOOK = ANSIBLE_DIR / "playbooks" / "llm_edit_args.yml"
TEMPLATE = (ANSIBLE_DIR / "roles" / "llm_serving" / "templates"
            / "llminferenceservice.yml.j2")

SPEC_ARG = '--speculative-config={"method":"mtp","num_speculative_tokens":1}'

# oc のスタブ。patch に渡された引数を PATCH_LOG へ書き出す。
OC_STUB = r"""#!/usr/bin/env bash
case "$1" in
  whoami) echo "kube:admin"; exit 0 ;;
  get)    echo '["--max-model-len=4096"]'; exit 0 ;;
  patch)
      # WEBHOOK_FAILS 回だけ webhook エラーを返してリトライを検証する
      if [ -n "$WEBHOOK_FAILS" ]; then
        n=0; [ -f "$FAIL_COUNT" ] && n=$(cat "$FAIL_COUNT")
        if [ "$n" -lt "$WEBHOOK_FAILS" ]; then
          echo $((n + 1)) > "$FAIL_COUNT"
          echo 'Error from server (InternalError): Internal error occurred: failed calling webhook "llminferenceservice.kserve-webhook-server.v1alpha2.defaulter": context deadline exceeded' >&2
          exit 1
        fi
      fi
      for a in "$@"; do
        case "$a" in
          '[{"op":'*|'[{"op": '*) printf '%s' "$a" > "$PATCH_LOG" ;;
        esac
      done
      echo "llminferenceservice.serving.kserve.io/x patched"
      exit 0 ;;
  wait)   exit 0 ;;
esac
exit 0
"""


def _have_ansible():
    return shutil.which("ansible-playbook") is not None


@unittest.skipUnless(PLAYBOOK.exists(), "llm_edit_args.yml がありません")
@unittest.skipUnless(_have_ansible(), "ansible-playbook がありません")
class TestLlmEditArgs(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        inv = self.tmp / "inv"
        (inv / "group_vars" / "all").mkdir(parents=True)
        (inv / "hosts.yml").write_text(
            "all:\n  hosts:\n    localhost:\n"
            "      ansible_connection: local\n"
            '      ansible_python_interpreter: "{{ ansible_playbook_python }}"\n'
        )
        (inv / "group_vars" / "all" / "components.yml").write_text(
            "models:\n"
            "  demo-model:\n"
            "    hf_repo: org/demo\n"
            "    state: present\n"
        )
        self.inv = inv

        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        oc = bin_dir / "oc"
        oc.write_text(OC_STUB)
        oc.chmod(0o755)
        self.bin_dir = bin_dir
        self.patch_log = self.tmp / "patch.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, args_list):
        env = {
            **os.environ,
            "PATH": f"{self.bin_dir}:{os.environ['PATH']}",
            "PATCH_LOG": str(self.patch_log),
            "ANSIBLE_LOCALHOST_WARNING": "False",
            "ANSIBLE_INVENTORY_UNPARSED_WARNING": "False",
        }
        r = subprocess.run(
            ["ansible-playbook", str(PLAYBOOK), "-i", str(self.inv),
             "-e", "model=demo-model",
             "-e", "edit_args_wait=0",
             "-e", json.dumps({"vllm_args": args_list})],
            cwd=str(ANSIBLE_DIR), capture_output=True, text=True, env=env,
        )
        self.assertEqual(r.returncode, 0,
                         f"playbook が失敗しました:\n{r.stdout[-3000:]}")
        self.assertTrue(self.patch_log.exists(), "oc patch が呼ばれていません")
        payload = json.loads(self.patch_log.read_text())
        return payload[0]["value"]

    def test_speculative_config_value_is_quoted(self):
        """JSON 値の引数は eval を通っても壊れないよう保護されること"""
        applied = self._run([SPEC_ARG, "--max-model-len=8192"])
        spec = [a for a in applied if a.startswith("--speculative-config")]
        self.assertEqual(len(spec), 1, f"想定外の args: {applied}")
        self.assertEqual(
            spec[0],
            '--speculative-config=\'{"method":"mtp","num_speculative_tokens":1}\'',
            "値がシングルクォートで保護されていません。"
            "KServe の eval でブレース展開され 2 引数に分割されます",
        )

    def test_quoted_value_survives_shell_eval(self):
        """実際に bash の eval を通して 1 引数のままか確かめる"""
        applied = self._run([SPEC_ARG])
        quoted = applied[-1]
        # KServe/llm-d の起動スクリプトと同じく eval を 1 回通す。
        # 引数自体がシングルクォートを含むので、bash -c に埋め込まず
        # スクリプトファイルから実行する。
        # KServe は argv で受け取った値を eval の中で展開する
        # (eval "exec vllm serve ... $@")。引数をスクリプト本文に埋め込むと
        # 展開のタイミングが変わってしまうので、argv 経由で渡す。
        script = self.tmp / "evaltest.sh"
        script.write_text('eval "set -- $1"\nprintf \'%s\\n\' "$@"\n')
        r = subprocess.run(["bash", str(script), quoted],
                           capture_output=True, text=True)
        out = [ln for ln in r.stdout.splitlines() if ln]
        self.assertEqual(
            out, [SPEC_ARG],
            "eval 後に引数が分割・変形しています: " + repr(out),
        )

    def test_plain_args_are_not_quoted(self):
        """ブレース展開の対象にならない引数はそのまま渡すこと"""
        applied = self._run(["--max-model-len=8192", "--enable-prefix-caching"])
        self.assertIn("--max-model-len=8192", applied)
        self.assertIn("--enable-prefix-caching", applied)

    def test_served_model_name_is_not_added(self):
        """--served-model-name は KServe が付けるので足さないこと"""
        applied = self._run(["--max-model-len=8192"])
        self.assertFalse(
            [a for a in applied if a.startswith("--served-model-name")],
            f"--served-model-name が重複指定されています: {applied}",
        )

    def test_matches_what_a_normal_deploy_would_apply(self):
        """通常デプロイのテンプレートが出す args と一致すること

        編集経路とデプロイ経路で結果が変わると、oc edit 感覚で直した設定が
        次の site.yml で別物に戻る。
        """
        import re

        import yaml
        from jinja2 import Environment, FileSystemLoader

        args_list = [SPEC_ARG, "--max-model-len=8192", "--enable-prefix-caching"]
        applied = self._run(args_list)

        env = Environment(loader=FileSystemLoader(str(TEMPLATE.parent)))
        env.filters["to_json"] = json.dumps
        env.filters["default"] = lambda val, d="": val if val else d
        env.filters["combine"] = lambda a, b: {**a, **b}
        env.tests["search"] = (
            lambda val, pattern: re.search(pattern, str(val)) is not None
        )
        doc = yaml.safe_load(env.get_template(TEMPLATE.name).render(
            _model_name="demo-model",
            _model_cfg={
                "namespace": "llm-serving",
                "vllm_image": "vllm/vllm-openai:v0.28.0",
                "vllm_args": args_list,
                "vllm_env": {},
                "custom_chat_template": "",
                "chat_template_configmap": "",
                "shm_size": "",
            },
        ))
        self.assertEqual(
            applied, doc["spec"]["template"]["containers"][0]["args"],
            "編集経路とデプロイ経路で args が食い違っています",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestQuotingIsIdempotent(unittest.TestCase):
    """既にクォート済みの引数を再適用しても二重にならないこと

    scripts/vllm-args.sh は稼働中の LLMInferenceService から args を読んで
    $EDITOR に渡し、保存すると llm_edit_args.yml に戻す。読んだ時点で値は
    既にシングルクォート済みなので、素朴に再クォートすると
      --speculative-config=''{"method":"mtp",...}''
    となり、eval 後にまた 2 引数へ分割されて MTP が壊れる。
    """

    def _render(self, args_list):
        import json
        import re

        import yaml
        from jinja2 import Environment, FileSystemLoader

        env = Environment(loader=FileSystemLoader(str(TEMPLATE.parent)))
        env.filters["to_json"] = json.dumps
        env.filters["default"] = lambda val, d="": val if val else d
        env.filters["combine"] = lambda a, b: {**a, **b}
        env.tests["search"] = (
            lambda val, pattern: re.search(pattern, str(val)) is not None
        )
        doc = yaml.safe_load(env.get_template(TEMPLATE.name).render(
            _model_name="demo-model",
            _model_cfg={
                "namespace": "llm-serving",
                "vllm_image": "vllm/vllm-openai:v0.28.0",
                "vllm_args": args_list,
                "vllm_env": {},
                "custom_chat_template": "",
                "chat_template_configmap": "",
                "shm_size": "",
            },
        ))
        return doc["spec"]["template"]["containers"][0]["args"]

    def test_template_quoting_is_idempotent(self):
        once = self._render([SPEC_ARG])
        twice = self._render(once)
        self.assertEqual(once, twice,
                         "再適用で二重クォートになっています: " + repr(twice))

    def test_double_quoted_value_would_split(self):
        """二重クォートが実際に壊れることを示し、テストの意味を担保する"""
        broken = SPEC_ARG.split("=", 1)[0] + "=''" + SPEC_ARG.split("=", 1)[1] + "''"
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / "e.sh"
            script.write_text('eval "set -- $1"\nprintf \'%s\\n\' "$@"\n')
            r = subprocess.run(["bash", str(script), broken],
                               capture_output=True, text=True)
        out = [ln for ln in r.stdout.splitlines() if ln]
        self.assertGreater(len(out), 1,
                           "二重クォートが分割されない前提が崩れています")

    def test_round_trip_through_cluster_args_survives_eval(self):
        """クラスタから読んだ args を再適用しても eval 後に原形を保つこと"""
        once = self._render([SPEC_ARG])
        twice = self._render(once)
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / "e.sh"
            script.write_text('eval "set -- $1"\nprintf \'%s\\n\' "$@"\n')
            r = subprocess.run(["bash", str(script), twice[-1]],
                               capture_output=True, text=True)
        out = [ln for ln in r.stdout.splitlines() if ln]
        self.assertEqual(out, [SPEC_ARG],
                         "再適用後に分割・変形しています: " + repr(out))


class TestWebhookRetry(TestLlmEditArgs):
    """KServe の admission webhook が一時的に応答しない場合の挙動

    ノード再起動直後などに
      failed calling webhook "...defaulter": context deadline exceeded
    で patch が失敗する（実機で発生）。一時的なものなのでリトライし、
    それでも駄目なら原因が分かるメッセージを出す。
    """

    def _run_with(self, args_list, env_extra, expect_ok=True):
        env = {
            **os.environ,
            "PATH": f"{self.bin_dir}:{os.environ['PATH']}",
            "PATCH_LOG": str(self.patch_log),
            "FAIL_COUNT": str(self.tmp / "failcount"),
            "ANSIBLE_LOCALHOST_WARNING": "False",
            "ANSIBLE_INVENTORY_UNPARSED_WARNING": "False",
            **env_extra,
        }
        r = subprocess.run(
            ["ansible-playbook", str(PLAYBOOK), "-i", str(self.inv),
             "-e", "model=demo-model",
             "-e", "edit_args_wait=0",
             "-e", "edit_args_webhook_retries=3",
             "-e", json.dumps({"vllm_args": args_list})],
            cwd=str(ANSIBLE_DIR), capture_output=True, text=True, env=env,
        )
        if expect_ok:
            self.assertEqual(r.returncode, 0,
                             f"リトライで復帰していません:\n{r.stdout[-3000:]}")
        return r

    def test_transient_webhook_error_is_retried(self):
        """数回 webhook エラーでも、復帰すれば成功すること"""
        self._run_with(["--max-model-len=8192"], {"WEBHOOK_FAILS": "2"})
        self.assertTrue(self.patch_log.exists(),
                        "リトライ後も patch が適用されていません")

    def test_persistent_webhook_error_fails_with_guidance(self):
        """回復しない場合は原因と確認手順を示して失敗すること"""
        r = self._run_with(["--max-model-len=8192"], {"WEBHOOK_FAILS": "99"},
                           expect_ok=False)
        self.assertNotEqual(r.returncode, 0, "失敗すべきところで成功しています")
        out = r.stdout
        self.assertIn("admission webhook", out,
                      "webhook が原因であることを案内していません")
        self.assertIn("llmisvc-webhook-server-service", out,
                      "確認コマンドを案内していません")
        self.assertFalse(self.patch_log.exists(),
                         "失敗したのに patch が適用されています")
