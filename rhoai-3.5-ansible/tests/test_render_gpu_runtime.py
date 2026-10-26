"""Execute the shipped bash/jq update path and compare decisions to Ansible."""
import copy
import json
import os
import re
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml
from tests.test_mig_playbook import render
from tests.test_mixed_gpu import ROOT, TASKS, MIG, LARGE, node, pod
from tests.test_render_parity import materialize_inventory
from tests import test_render_parity as parity


MOCK_OC = r'''
import json, os, sys
from pathlib import Path
p = Path(os.environ['CASE_DIR']); args = sys.argv[1:]
with (p/'calls').open('a') as f: f.write(json.dumps(args)+'\n')
mode = os.environ.get('MOCK_MODE', '')
def emit(name): print((p/(name+'.json')).read_text())
if args[0] == 'create':
 assert '--dry-run=client' in args and '--validate=false' in args
 if mode == 'manifest-error': sys.exit(1)
 if mode == 'manifest-empty': sys.exit(0)
 if mode == 'manifest-duplicate':
  docs = json.loads((p/'manifest.json').read_text())['items']
  print(json.dumps(docs[0])); print(json.dumps(docs[0])); sys.exit(0)
 if mode == 'list-output': emit('manifest')
 else:
  for obj in json.loads((p/'manifest.json').read_text())['items']: print(json.dumps(obj))
elif args[:2] == ['get', 'llminferenceservices.v1alpha2.serving.kserve.io']:
 if mode == 'get-error': sys.exit(1)
 if (p/'live.json').exists(): emit('live')
elif args[:2] == ['get', 'nodes']:
 if mode == 'nodes-error': sys.exit(1)
 emit('nodes')
elif args[:2] == ['get', 'pods']:
 if mode == 'pods-error': sys.exit(1)
 if (p/'deleted').exists():
  if mode == 'release-error': sys.exit(1)
  if mode == 'release-malformed': print('{}')
  elif mode == 'release-busy': emit('pods')
  else: print('{"items":[]}')
 else: emit('pods')
elif args[:2] == ['get', 'endpoints']:
 if mode == 'webhook-error': sys.exit(1)
 print('10.0.0.1')
elif args[0] == 'delete':
 assert args[1] == 'llminferenceservices.v1alpha2.serving.kserve.io'
 if mode == 'delete-notfound' and '--ignore-not-found' not in args: sys.exit(1)
 if mode == 'delete-error': sys.exit(1)
 (p/'deleted').touch()
elif args[0] == 'apply':
 if mode == 'apply-error': sys.exit(1)
elif args[0] == 'wait':
 assert any(x.startswith('llminferenceservices.v1alpha2.serving.kserve.io/') for x in args)
 if mode == 'ready-error': sys.exit(1)
else: sys.exit('unexpected command: '+repr(args))
'''


class RenderGpuRuntime(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        inv = materialize_inventory('sample', cls.root)
        comp = inv/'group_vars/all/components.yml'
        cfg = yaml.safe_load(comp.read_text())
        cfg['models'] = {'small': {'hf_repo': 'Qwen/Qwen3-0.6B', 'gpu_resource': MIG,
                                   'node_selector': {'pool': 'mig'}}}
        comp.write_text(yaml.safe_dump(cfg))
        out = cls.root/'out'
        parity.TestRenderParity()._render_with_render_py_dir(inv, out)
        cls.runtime = out/'manifests/09-llm-serving'
        cls.guide = (out/'manual-deploy-guide.md').read_text()
        cls.manifest = cls.runtime/'model-small-deploy.yml'
        cls.docs = list(yaml.safe_load_all(cls.manifest.read_text()))
        cls.desired = next(d for d in cls.docs if d['kind'] == 'LLMInferenceService')
        cls.cfg = json.loads((cls.runtime/'gpu-models.json').read_text())[0]
        cls.serve = yaml.safe_load((TASKS/'serve_model.yml').read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def jq(self, expression, value, **variables):
        cmd = ['jq', '-c', '-L', str(self.runtime)]
        for name, val in variables.items(): cmd += ['--argjson', name, json.dumps(val)]
        p = subprocess.run(cmd + ['include "llm-gpu"; '+expression], input=json.dumps(value),
                           text=True, capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def run_script(self, live=None, nodes=None, pods=None, mode='', operation='deploy'):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            for name, value in {'manifest': {'kind': 'List', 'items': self.docs},
                                'nodes': {'items': nodes if nodes is not None else [node()]},
                                'pods': {'items': pods or []}}.items():
                (path/(name+'.json')).write_text(json.dumps(value))
            if live is not None: (path/'live.json').write_text(json.dumps(live))
            oc = path/'oc'; oc.write_text('#!'+sys.executable+'\n'+MOCK_OC); oc.chmod(0o755)
            sleep = path/'sleep'; sleep.write_text('#!/bin/sh\nexit 0\n'); sleep.chmod(0o755)
            cmd = ['bash', str(self.runtime/'llm-deploy.sh'), operation]
            if operation == 'deploy': cmd.append(str(self.manifest))
            result = None
            error = None
            try:
                result = subprocess.run(cmd, env={**os.environ, 'PATH': str(path)+':'+os.environ['PATH'],
                                        'CASE_DIR': tmp, 'MOCK_MODE': mode}, text=True,
                                        capture_output=True, timeout=30)
            except Exception as exc:
                error = exc
                raise
            finally:
                calls = [json.loads(line) for line in (path/'calls').read_text().splitlines()] if (path/'calls').exists() else []
                if os.environ.get('GPU_RUNTIME_TEST_LOG_DIR'):
                    def output(field):
                        value = getattr(result if result is not None else error, field, None)
                        return value.decode(errors='replace') if isinstance(value, bytes) else value
                    logdir = Path(os.environ['GPU_RUNTIME_TEST_LOG_DIR'])
                    logdir.mkdir(parents=True, exist_ok=True)
                    (logdir/(path.name+'.json')).write_text(json.dumps({
                        'test': self.id(), 'mode': mode, 'operation': operation,
                        'returncode': output('returncode'), 'stdout': output('stdout'),
                        'stderr': output('stderr'), 'error': repr(error) if error else None, 'calls': calls,
                        'nodes': nodes, 'pods': pods, 'live': live}, indent=2))
            return result, calls

    def test_diagnostics_survive_timeout_and_failure_before_first_oc(self):
        for timeout in (True, False):
            with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'GPU_RUNTIME_TEST_LOG_DIR': tmp}):
                if timeout:
                    error = subprocess.TimeoutExpired('bash', 30, output=b'partial', stderr=b'timed out')
                    with patch('subprocess.run', side_effect=error), self.assertRaises(subprocess.TimeoutExpired):
                        self.run_script()
                else:
                    with patch('subprocess.run', return_value=subprocess.CompletedProcess('bash', 127, '', 'missing jq')):
                        result, calls = self.run_script()
                    self.assertEqual(result.returncode, 127)
                    self.assertEqual(calls, [])
                logs = list(Path(tmp).glob('*.json'))
                self.assertEqual(len(logs), 1)
                record = json.loads(logs[0].read_text())
                self.assertEqual(record['calls'], [])
                self.assertEqual(record['stderr'], 'timed out' if timeout else 'missing jq')
                if timeout:
                    self.assertIn('TimeoutExpired', record['error'])
                    self.assertEqual(record['stdout'], 'partial')

    def test_guide_proxy_failure_and_no_downloads_are_tolerated(self):
        section = self.guide.split(': LLM Serving\n', 1)[1].split('\n## Step ', 1)[0]
        block = re.findall(r'```bash\n(.*?)```', section, flags=re.S)[0]
        for proxy_missing in (True, False):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                runtime = root/'manifests/09-llm-serving'
                shutil.copytree(self.runtime, runtime)
                (runtime/'gpu-models.json').write_text('[]')
                for download in runtime.glob('model-*-download.yml'): download.unlink()
                oc = root/'oc'
                oc.write_text('#!/bin/sh\n'
                              'printf "%s\\n" "$*" >> "$GUIDE_CALLS"\n'
                              'case "$1 $2" in\n'
                              ' "get nodes") echo \'{"items":[]}\';;\n'
                              ' "get proxy") '+('exit 1' if proxy_missing else 'echo http://proxy')+';;\n'
                              ' "set env") exit 9;;\n'
                              'esac\n')
                oc.chmod(0o755)
                result = subprocess.run(['bash', '-c', block], cwd=root, text=True, capture_output=True,
                    env={**os.environ, 'PATH': tmp+':'+os.environ['PATH'], 'GUIDE_CALLS': str(root/'calls')})
                self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
                calls = (root/'calls').read_text()
                self.assertNotIn('set env', calls)
                self.assertIn('create configmap small-chat-template', calls)
                if proxy_missing: self.assertIn('No proxy configured', result.stdout)

    def changed(self):
        live = copy.deepcopy(self.desired)
        live['spec']['template']['containers'][0]['image'] = 'old-image'
        live['status'] = {'conditions': [{'type': 'Ready', 'status': 'True'}]}
        return live

    def test_plan_matches_actual_ansible_expressions(self):
        compare = next(t for t in self.serve if '_lis_spec_changed' in t.get('ansible.builtin.set_fact', {}))
        recreate = next(t for t in self.serve if '_needs_recreate' in t.get('ansible.builtin.set_fact', {}))
        fixtures = [copy.deepcopy(self.desired), self.changed()]
        for field, value in [('args', ['different']), ('env', []), ('resources', {}),
                             ('resources', {'requests': {LARGE: '1'}}),
                             ('volumeMounts', [{'name': 'extra'}]), ('startupProbe', {})]:
            live = copy.deepcopy(self.desired)
            live['spec']['template']['containers'][0][field] = value
            fixtures.append(live)
        for field, value in [('nodeSelector', {}), ('volumes', [{'name': 'extra'}])]:
            live = copy.deepcopy(self.desired); live['spec']['template'][field] = value
            fixtures.append(live)
        live = copy.deepcopy(self.desired); live['spec']['replicas'] = 2; fixtures.append(live)
        live = copy.deepcopy(self.desired); live['spec']['model']['uri'] = 'pvc://other'; fixtures.append(live)
        for live in fixtures:
            for count in (1, 4):
                variables = {'_existing_lis': {'resources': [live]}, '_desired_lis': self.desired,
                             '_model_cfg': self.cfg, '_rollout_gpu_total': count,
                             '_cur': live['spec']['template']['containers'][0],
                             '_new': self.desired['spec']['template']['containers'][0]}
                changed = render(compare['ansible.builtin.set_fact']['_lis_spec_changed'], variables)
                variables['_lis_spec_changed'] = changed
                variables['_current_gpu_keys'] = render(recreate['vars']['_current_gpu_keys'], variables)
                needs = render(recreate['ansible.builtin.set_fact']['_needs_recreate'], variables)
                expected = 'unchanged' if not changed else 'recreate' if needs else 'update'
                self.assertEqual(self.jq('plan($desired; $live)', {'items': [node(resources={MIG: str(count)})]},
                                         desired=self.desired, live=live), expected)

    def test_reservations_match_ansible_including_init_and_pending(self):
        expr = yaml.safe_load((TASKS/'check_gpu_available.yml').read_text())[2]['ansible.builtin.set_fact']['_gpu_availability']
        fixtures = [[], [pod()], [pod(phase='Pending')], [pod(phase='Succeeded')],
                    [pod(node_name='')], [pod(LARGE)],
                    [pod(init=[{'resources': {'limits': {MIG: '3'}}}])],
                    [pod(init=[{'restartPolicy': 'Always', 'resources': {'requests': {MIG: '2'}}},
                               {'resources': {'requests': {MIG: '1'}}}])]]
        for pods in fixtures:
            for nodes in ([node()], [node('other', labels={'pool': 'other'})],
                          [node('a', {MIG: '1'}), node('b', {MIG: '1'})]):
                variables = {'_model_cfg': self.cfg, '_available_nodes': {'resources': nodes},
                             '_available_pods': {'resources': pods}}
                self.assertEqual(self.jq('availability($cfg; $pods)', {'items': nodes}, cfg=self.cfg,
                                         pods={'items': pods}), render(expr, variables))

    def test_new_model_checks_free_slots_and_never_deletes_other_pods(self):
        for pods, success in [([], True), ([pod(count=4)], False), ([pod(LARGE, 4)], True),
                              ([pod(count=4, phase='Pending')], False)]:
            p, calls = self.run_script(pods=pods)
            self.assertEqual(p.returncode == 0, success, p.stdout+p.stderr)
            self.assertFalse(any(c[0] == 'delete' for c in calls))
            self.assertEqual(any(c[0] == 'apply' for c in calls), success)
            if not success: self.assertIn('test/model', p.stderr)

    def test_manifest_list_output_is_also_supported(self):
        p, _ = self.run_script(mode='list-output')
        self.assertEqual(p.returncode, 0, p.stdout+p.stderr)

    def test_unchanged_model_is_not_deleted_or_queried_for_capacity(self):
        p, calls = self.run_script(live=self.desired, mode='nodes-error')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(any(c[0] == 'delete' or c[:2] == ['get', 'nodes'] for c in calls))

    def test_rollout_or_recreation_and_order(self):
        for count in (1, 4):
            p, calls = self.run_script(live=self.changed(), nodes=[node(resources={MIG: str(count)})])
            self.assertEqual(p.returncode, 0, p.stderr)
            deletes = [i for i, c in enumerate(calls) if c[0] == 'delete']
            self.assertEqual(len(deletes), 1 if count == 1 else 0)
            if deletes:
                delete = deletes[0]; apply = next(i for i,c in enumerate(calls) if c[0] == 'apply')
                self.assertEqual(calls[delete][1:3], ['llminferenceservices.v1alpha2.serving.kserve.io', 'small'])
                self.assertTrue(any(c[:2] == ['get', 'pods'] for c in calls[delete+1:apply]))

    def test_disappeared_lis_delete_is_idempotent(self):
        p, calls = self.run_script(live=self.changed(), nodes=[node(resources={MIG: '1'})],
                                   mode='delete-notfound')
        self.assertEqual(p.returncode, 0, p.stdout+p.stderr)
        self.assertTrue(any(c[0] == 'apply' for c in calls))
        self.assertIn('--ignore-not-found', next(c for c in calls if c[0] == 'delete'))

    def test_invalid_allocatable_has_context_and_never_mutates(self):
        for operation in ('preflight', 'deploy'):
            p, calls = self.run_script(live=self.changed(), nodes=[node(resources={MIG: '4k'})],
                                       operation=operation)
            self.assertNotEqual(p.returncode, 0, p.stdout)
            for message in ('Invalid GPU allocatable', 'node=mig', 'resource='+MIG, '4k'):
                self.assertIn(message, p.stderr)
            self.assertFalse(any(c[0] in ('delete', 'apply') for c in calls))

    def test_guide_blocks_fail_without_exiting_parent_shell(self):
        section = self.guide.split(': LLM Serving\n', 1)[1].split('\n## Step ', 1)[0]
        blocks = re.findall(r'```bash\n(.*?)```', section, flags=re.S)
        self.assertGreaterEqual(len(blocks), 3)
        with tempfile.TemporaryDirectory() as tmp:
            oc = Path(tmp)/'oc'
            oc.write_text('#!/bin/sh\nexit 1\n'); oc.chmod(0o755)
            for block in blocks:
                p = subprocess.run(['bash', '-c', block+'\nrc=$?; echo PARENT_ALIVE:$rc'],
                                   cwd=self.runtime.parent.parent, text=True, capture_output=True,
                                   env={**os.environ, 'PATH': tmp+':'+os.environ['PATH']}, timeout=10)
                self.assertEqual(p.returncode, 0, p.stdout+p.stderr)
                self.assertIn('PARENT_ALIVE:1', p.stdout, p.stdout+p.stderr)

    def test_errors_never_reach_apply(self):
        for mode in ('get-error', 'nodes-error', 'delete-error', 'release-error', 'release-malformed', 'release-busy'):
            p, calls = self.run_script(live=self.changed(), nodes=[node(resources={MIG: '1'})],
                                       pods=[pod()], mode=mode)
            self.assertNotEqual(p.returncode, 0, mode)
            self.assertFalse(any(c[0] == 'apply' for c in calls), mode)
        for mode in ('pods-error', 'webhook-error'):
            p, calls = self.run_script(mode=mode)
            self.assertNotEqual(p.returncode, 0, mode)
            self.assertFalse(any(c[0] in ('delete', 'apply') for c in calls), mode)
        for mode in ('manifest-error', 'manifest-empty', 'manifest-duplicate'):
            p, calls = self.run_script(mode=mode)
            self.assertNotEqual(p.returncode, 0, mode)
            self.assertFalse(any(c[0] in ('delete', 'apply') for c in calls), mode)

    def test_zero_or_wrong_resource_stops_before_deletion(self):
        for nodes in ([], [node(resources={LARGE: '40'})], [node(labels={'pool': 'elsewhere'})]):
            p, calls = self.run_script(live=self.changed(), nodes=nodes)
            self.assertNotEqual(p.returncode, 0, p.stdout)
            self.assertFalse(any(c[0] in ('delete', 'apply') for c in calls))

    def test_apply_and_ready_failures_are_not_masked(self):
        for mode in ('apply-error', 'ready-error'):
            p, calls = self.run_script(live=self.desired, mode=mode)
            self.assertNotEqual(p.returncode, 0, mode)

    def test_preflight_resource_totals_and_selector_fit(self):
        for nodes, success in [([node()], True), ([], False), ([node(resources={LARGE: '40'})], False),
                              ([node(labels={'pool': 'elsewhere'})], False)]:
            p, calls = self.run_script(nodes=nodes, operation='preflight')
            self.assertEqual(p.returncode == 0, success, p.stderr)
            self.assertTrue(all(c[:2] == ['get', 'nodes'] for c in calls))
        p, _ = self.run_script(operation='preflight', mode='nodes-error')
        self.assertNotEqual(p.returncode, 0)

    def test_preflight_matches_ansible_aggregate_and_per_model_checks(self):
        tasks = yaml.safe_load((TASKS/'check_gpu_capacity.yml').read_text())
        model_sets = [[self.cfg], [self.cfg, self.cfg | {'name': 'second', 'replicas': 4}],
                      [self.cfg, self.cfg | {'name': 'large', 'gpu_resource': LARGE}],
                      [self.cfg | {'gpu_count': 3, 'replicas': 2}],
                      [self.cfg | {'node_selector': {'pool': 'missing'}}]]
        node_sets = [[node()], [node(), node('large', {LARGE: '1'})],
                     [node('a', {MIG: '2'}), node('b', {MIG: '2'})],
                     [dict(node(), spec={'unschedulable': True})]]
        for models in model_sets:
            for nodes in node_sets:
                variables = {'_gpu_models': models, '_capacity_nodes': {'resources': nodes}}
                facts = tasks[2]['ansible.builtin.set_fact']
                demands = render(facts['_gpu_demands'], variables)
                variables['_gpu_capacity'] = render(facts['_gpu_capacity'], variables)
                checks = [render('{{ '+tasks[3]['ansible.builtin.assert']['that']+' }}',
                                 variables | {'item': {'key': key, 'value': count}})
                          for key, count in demands.items()]
                for cfg in models:
                    values = variables | {'item': cfg}
                    values['_selected_slots'] = render(tasks[4]['vars']['_selected_slots'], values)
                    checks.append(render('{{ '+tasks[4]['ansible.builtin.assert']['that'][0]+' }}', values))
                actual = self.jq('try preflight($models) catch false', {'items': nodes}, models=models)
                self.assertEqual(actual, all(checks), (models, nodes))


if __name__ == '__main__':
    unittest.main()
