"""Exercise the production uninstall Job selection without a live cluster."""
import copy
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
TASKS = yaml.safe_load((ROOT / 'roles/llm_serving/tasks/uninstall.yml').read_text())[0]['block']
DELETE = next(t for t in TASKS if t.get('name') ==
              'Delete model download Jobs before deleting their cache PVC')


class UninstallDownloadJobs(unittest.TestCase):
    def test_job_removal_finishes_before_cache_deletion(self):
        pvc = next(t for t in TASKS if t.get('name') == 'Delete model-cache PVC')
        self.assertLess(TASKS.index(DELETE), TASKS.index(pvc))
        self.assertIn(repr(pvc['kubernetes.core.k8s']['name']), str(DELETE['when']))
        module = DELETE['kubernetes.core.k8s']
        self.assertEqual(module['kind'], 'Job')
        self.assertEqual(module['state'], 'absent')
        self.assertTrue(module['wait'])
        self.assertEqual(module['delete_options']['propagationPolicy'], 'Foreground')
        self.assertNotIn('failed_when', DELETE)
        self.assertGreater(module['wait_timeout'], 0)

    @unittest.skipUnless(shutil.which('ansible-playbook'), 'ansible-playbook unavailable')
    def test_real_when_selects_only_download_jobs_using_the_cache(self):
        def job(name, volume='hf-model-cache', status=None):
            return {'metadata': {'name': name}, 'status': status or {},
                    'spec': {'template': {'spec': {'volumes': [
                        {'name': 'cache', 'persistentVolumeClaim': {'claimName': volume}}]}}}}

        fixtures = [job('download-completed', status={'succeeded': 1}),
                    job('download-failed', status={'failed': 3}),
                    job('download-active', status={'active': 1}),
                    job('download-other-pvc', volume='unrelated'),
                    job('other-workload'),
                    {'metadata': {'name': 'download-no-volumes'}, 'spec': {'template': {'spec': {}}}},
                    {'metadata': {'name': 'download-null-volumes'},
                     'spec': {'template': {'spec': {'volumes': None}}}}]
        task = copy.deepcopy(DELETE)
        del task['kubernetes.core.k8s']
        task['ansible.builtin.set_fact'] = {'removed': '{{ removed + [item.metadata.name] }}'}
        play = [{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                 'vars': {'removed': [], '_download_jobs': {'resources': fixtures}},
                 'tasks': [task, {'ansible.builtin.assert': {'that':
                     "removed == ['download-completed', 'download-failed', 'download-active']"}}]}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'verify.yml'
            path.write_text(yaml.safe_dump(play, sort_keys=False))
            result = subprocess.run(['ansible-playbook', '-i', 'localhost,', str(path)],
                                    text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
