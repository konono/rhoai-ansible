"""The normal Keycloak token path must not mutate a working installation."""

from pathlib import Path
import unittest

import yaml
from jinja2 import Environment


TASK_FILE = (
    Path(__file__).resolve().parent.parent
    / "roles" / "keycloak" / "tasks" / "_get_kc_token.yml"
)


class TestKeycloakTokenSafety(unittest.TestCase):
    def test_rejected_secret_fails_without_cluster_mutation(self):
        source = TASK_FILE.read_text()
        tasks = yaml.safe_load(source)
        commands = [t.get("ansible.builtin.command", "") for t in tasks]
        self.assertFalse(any("oc delete" in command or "oc patch" in command for command in commands))
        self.assertFalse(any("kubernetes.core.k8s" in task for task in tasks))

        request = next(t for t in tasks if t["name"] == "Obtain Keycloak admin token")
        self.assertEqual(request["ansible.builtin.uri"]["status_code"], [200, 401])
        self.assertTrue(request["no_log"])

        refusal = next(t for t in tasks if t["name"].startswith("Fail if Keycloak"))
        predicate = Environment().from_string("{{ " + refusal["when"] + " }}")
        self.assertEqual(predicate.render(_kc_token_response={"status": 401}), "True")
        self.assertEqual(predicate.render(_kc_token_response={"status": 200}), "False")


if __name__ == "__main__":
    unittest.main()
