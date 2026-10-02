"""Check MaaS verification when one configured Subscription has failed."""

from pathlib import Path
import unittest

import yaml
from jinja2 import Environment


VERIFY = Path(__file__).resolve().parent.parent / "playbooks" / "verify.yml"


class TestMaaSVerification(unittest.TestCase):
    def test_api_key_creation_requires_explicit_opt_in(self):
        tasks = yaml.safe_load(VERIFY.read_text())[0]["tasks"]
        key_task = next(t for t in tasks if t["name"] == "Verify: MaaS API Key issuable")
        self.assertIn("-X POST", key_task["ansible.builtin.command"])
        self.assertIn(
            "verify_maas_api_key_issuance | default(false) | bool",
            key_task["when"],
        )

    def test_each_configured_subscription_must_be_active(self):
        tasks = yaml.safe_load(VERIFY.read_text())[0]["tasks"]
        task = next(
            t for t in tasks
            if t["name"] == "Verify: Each configured MaaS Subscription Active"
        )
        env = Environment()
        env.filters["difference"] = lambda left, right: list(set(left) - set(right))
        env.filters["intersect"] = lambda left, right: list(set(left) & set(right))

        def fails(phase, refs):
            self.assertEqual(task["kubernetes.core.k8s_info"]["name"], "{{ item.key }}-subscription")
            resource = {
                "status": {"phase": phase},
                "spec": {"modelRefs": [{"name": name} for name in refs]},
            }
            rendered = env.from_string("{{ " + task["failed_when"] + " }}").render(
                _subscriptions={"resources": [resource]},
                _expected_models=["qwen3-06b"],
            )
            return rendered == "True"

        self.assertFalse(fails("Active", ["qwen3-06b"]))
        self.assertTrue(fails("Failed", ["qwen3-06b"]))
        self.assertTrue(fails("Active", []))


if __name__ == "__main__":
    unittest.main()
