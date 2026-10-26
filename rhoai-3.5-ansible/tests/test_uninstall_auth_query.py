"""Run the production authentication query with an argv-checking oc stub."""
import copy
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
TASKS = yaml.safe_load((ROOT / 'roles/integration_keycloak_oauth/tasks/uninstall.yml').read_text())[0]['block']
QUERY = next(t for t in TASKS if t.get('register') == '_auth_co')


class UninstallAuthQuery(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ansible-playbook'), 'ansible-playbook unavailable')
    def test_jsonpath_reaches_oc_as_one_intact_argument(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            oc = path / 'oc'
            oc.write_text('''#!/bin/sh
[ "$#" -eq 5 ] || exit 7
[ "$5" = 'jsonpath={range .status.conditions[*]}{.type}={.status}{" "}{end}' ] || exit 8
printf 'Available=True Degraded=False Progressing=False '
''')
            oc.chmod(0o700)
            query = copy.deepcopy(QUERY)
            query.update(retries=0, delay=0)
            play = [{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                     'environment': {'PATH': tmp + os.pathsep + os.environ['PATH']},
                     'tasks': [query, {'ansible.builtin.assert': {'that': [
                         '_auth_co.rc == 0', "'Available=True' in _auth_co.stdout",
                         "'Degraded=False' in _auth_co.stdout"]}}]}]
            file = path / 'test.yml'
            file.write_text(yaml.safe_dump(play, sort_keys=False))
            result = subprocess.run(['ansible-playbook', '-i', 'localhost,', str(file)],
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
