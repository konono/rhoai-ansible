"""Read-only MTP diagnosis at its oc command boundary."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "diagnose-mtp.py"
spec = importlib.util.spec_from_file_location("diagnose_mtp", SCRIPT)
diagnose = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnose)


def result(stdout="", rc=0):
    return subprocess.CompletedProcess([], rc, stdout, "")


def pod(name, args, phase="Running"):
    return {"metadata": {"name": name}, "status": {"phase": phase},
            "spec": {"containers": [{"name": "main", "args": args}]}}


class TestDiagnoseMtp(unittest.TestCase):
    def run_main(self, responses):
        calls = []

        def fake_oc(*args):
            calls.append(args)
            return responses.pop(0)

        output = io.StringIO()
        with patch.object(diagnose, "oc", side_effect=fake_oc), \
                patch.object(sys, "argv", [str(SCRIPT)]), \
                contextlib.redirect_stdout(output):
            code = diagnose.main()
        return code, output.getvalue(), calls

    def test_multiple_pods_and_https_metrics(self):
        pods = [pod("pod-b", ['--speculative-config={"method":"mtp","num_speculative_tokens":3}']),
                pod("pod-a", ["--max-model-len=4096"], "Pending")]
        responses = [result(json.dumps({"items": pods})), result(""),
                     result("speculative config initialized\n"),
                     result('vllm:spec_decode_num_draft_tokens_total{model_name="private"} 4\n'
                            'vllm:spec_decode_num_accepted_tokens_total 2\n')]
        code, output, calls = self.run_main(responses)
        self.assertEqual(code, 0)
        self.assertLess(output.index("Pod pod-a"), output.index("Pod pod-b"))
        self.assertIn("Pod に speculative-config がありません", output)
        self.assertIn("推測デコードの実行を確認", output)
        self.assertNotIn("private", output)
        self.assertFalse(any("exec" in call for call in calls[:2]))
        self.assertEqual(calls[-1][0], "exec")
        self.assertIn("https://localhost:8000/metrics", calls[-1])

    def test_http_fallback_failure_is_not_mtp_disabled(self):
        pods = [pod("pod-a", ["--speculative-config", '{"method":"mtp"}'])]
        responses = [result(json.dumps({"items": pods})), result("unsupported mtp method\n"),
                     result(rc=7), result(rc=7)]
        code, output, calls = self.run_main(responses)
        self.assertEqual(code, 0)
        self.assertIn("起動時拒否の可能性", output)
        self.assertIn("HTTP/HTTPS とも取得できず", output)
        self.assertIn("http://localhost:8000/metrics", calls[-1])
        self.assertNotIn("unsupported mtp method", output)

    def test_no_pods(self):
        code, output, calls = self.run_main([result('{"items":[]}')])
        self.assertEqual(code, 2)
        self.assertIn("0 件", output)
        self.assertEqual(len(calls), 1)

    def test_split_json_argument_is_identified(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            diagnose.check_args({"args": ["--speculative-config={\"method\":", '"mtp"}']})
        self.assertIn("分割/引用符", output.getvalue())
        self.assertNotIn("mtp", output.getvalue())


if __name__ == "__main__":
    unittest.main()
