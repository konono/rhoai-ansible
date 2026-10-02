"""Check the inventory opt-in and the recovery role's production wiring."""

from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parent.parent


class TestKeycloakAdminRecovery(unittest.TestCase):
    def test_site_imports_role_before_keycloak_with_inventory_flag(self):
        tasks = yaml.safe_load((ROOT / "site.yml").read_text())[0]["tasks"]
        recovery = next(t for t in tasks if t.get("name") == "Recover stale Keycloak admin credentials")
        keycloak = next(t for t in tasks if t.get("name") == "Keycloak")
        self.assertLess(tasks.index(recovery), tasks.index(keycloak))
        self.assertEqual(recovery["ansible.builtin.import_role"]["name"], "keycloak_admin_recovery")
        self.assertIn("keycloak_admin_recovery | default(false) | bool", recovery["when"])

    def test_sample_inventory_enables_recovery(self):
        sample = yaml.safe_load(
            (ROOT / "inventory/sample/group_vars/all/components.yml.sample").read_text()
        )
        self.assertIs(sample["keycloak_admin_recovery"], True)

    def test_recovery_only_enters_existing_ready_instance(self):
        tasks = yaml.safe_load(
            (ROOT / "roles/keycloak_admin_recovery/tasks/main.yml").read_text()
        )
        entry = next(t for t in tasks if t.get("ansible.builtin.include_tasks") == "existing.yml")
        self.assertIn("Ready", entry["when"])
        self.assertIn("resources | length == 1", entry["when"])

    def test_valid_credentials_cannot_enter_mutation_path(self):
        tasks = yaml.safe_load(
            (ROOT / "roles/keycloak_admin_recovery/tasks/existing.yml").read_text()
        )
        recovery = next(t for t in tasks if t.get("ansible.builtin.include_tasks") == "recover.yml")
        self.assertEqual(recovery["when"], "_kc_initial_token.status == 401")
        self.assertNotIn("keycloak_recovery_backup_confirmed", str(tasks))

    def test_recovery_keeps_db_and_pvc(self):
        source = (ROOT / "roles/keycloak_admin_recovery/tasks/recover.yml").read_text()
        self.assertNotIn("PersistentVolumeClaim", source)
        self.assertNotIn("keycloak-db-credentials", source)
        self.assertNotIn("delete pvc", source.lower())


if __name__ == "__main__":
    unittest.main()
