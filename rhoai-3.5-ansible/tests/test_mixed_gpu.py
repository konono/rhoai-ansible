"""Exercise real Ansible capacity expressions and both manifest generators."""
import subprocess
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
import yaml
from tests.test_mig_playbook import render
from tests.test_render_parity import materialize_inventory, ansible_cmd
from tests import test_render_parity as parity

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / 'roles/llm_serving/tasks'
MIG = 'nvidia.com/mig-1g.24gb'
LARGE = 'nvidia.com/mig-2g.48gb'


def node(name='mig', resources=None, labels=None):
    return {'metadata': {'name': name, 'labels': labels or {'pool': 'mig'}},
            'spec': {}, 'status': {'allocatable': resources or {MIG: '4'}}}


def pod(resource=MIG, count=1, phase='Running', init=None, node_name='mig'):
    return {'metadata': {'name': 'model', 'namespace': 'test'},
            'spec': {'nodeName': node_name,
                     'containers': [{'resources': {'requests': {resource: str(count)}}}],
                     'initContainers': init or []}, 'status': {'phase': phase}}


class TestMixedCapacity(unittest.TestCase):
    def setUp(self):
        self.tasks = yaml.safe_load((TASKS / 'check_gpu_capacity.yml').read_text())
        self.available = yaml.safe_load((TASKS / 'check_gpu_available.yml').read_text())[2]['ansible.builtin.set_fact']['_gpu_availability']
        self.cfg = {'name': 'small', 'gpu_resource': MIG, 'gpu_count': 1, 'replicas': 1,
                    'node_selector': {'pool': 'mig'}}

    def available_slots(self, pods, nodes=None, cfg=None):
        return render(self.available, {'_model_cfg': cfg or self.cfg,
                      '_available_nodes': {'resources': nodes or [node()]},
                      '_available_pods': {'resources': pods}})

    def test_resource_demands_do_not_mix_and_include_replicas(self):
        expr = self.tasks[2]['ansible.builtin.set_fact']['_gpu_demands']
        models = [self.cfg | {'replicas': 3}, self.cfg | {'gpu_resource': LARGE, 'gpu_count': 2},
                  {'gpu_count': 1}, self.cfg | {'replicas': 2}]
        self.assertEqual(render(expr, {'_gpu_models': models}), {MIG: 5, LARGE: 2, 'nvidia.com/gpu': 1})

    def test_capacity_is_resource_specific_and_excludes_cordoned_nodes(self):
        expr = self.tasks[2]['ansible.builtin.set_fact']['_gpu_capacity']
        off = node('off'); off['spec']['unschedulable'] = True
        result = render(expr, {'_capacity_nodes': {'resources': [node(), node('l4', {'nvidia.com/gpu': '1'}), off]}})
        self.assertEqual(result, {MIG: 4, 'nvidia.com/gpu': 1})
        condition = self.tasks[3]['ansible.builtin.assert']['that']
        self.assertFalse(render('{{ ' + condition + ' }}', {'item': {'key': LARGE, 'value': 1}, '_gpu_capacity': result}))

    def test_selector_and_per_pod_fit(self):
        expr = self.tasks[4]['vars']['_selected_slots']
        for cfg, expected in [(self.cfg, 4), (self.cfg | {'node_selector': {'pool': 'missing'}}, 0),
                              (self.cfg | {'gpu_count': 3}, 1), (self.cfg | {'gpu_resource': LARGE}, 0)]:
            with self.subTest(cfg=cfg):
                self.assertEqual(render(expr, {'item': cfg, '_capacity_nodes': {'resources': [node(), node('other', {MIG: '40'}, {'pool': 'other'})]}}), expected)

    def test_other_resource_does_not_consume_mig(self):
        self.assertEqual(self.available_slots([pod('nvidia.com/gpu'), pod(LARGE)]), {'slots': 4, 'pods': []})

    def test_running_and_bound_pending_reserve_resources(self):
        result = self.available_slots([pod(), pod(phase='Pending')])
        self.assertEqual(result['slots'], 2)
        self.assertEqual(result['pods'], ['test/model', 'test/model'])

    def test_completed_unbound_and_other_nodes_do_not_reserve(self):
        self.assertEqual(self.available_slots([pod(phase='Succeeded'), pod(phase='Failed'), pod(node_name=''), pod(node_name='other')])['slots'], 4)

    def test_init_peak_and_limits_default(self):
        p = pod(init=[{'resources': {'limits': {MIG: '3'}}}])
        self.assertEqual(self.available_slots([p])['slots'], 1)

    def test_restartable_init_adds_to_app(self):
        p = pod(init=[{'restartPolicy': 'Always', 'resources': {'requests': {MIG: '2'}}}])
        self.assertEqual(self.available_slots([p])['slots'], 1)

    def test_no_cross_node_fragmentation_or_selector_leak(self):
        cfg = self.cfg | {'gpu_count': 2}
        self.assertEqual(self.available_slots([], [node('a', {MIG: '1'}), node('b', {MIG: '1'})], cfg)['slots'], 0)
        self.assertEqual(self.available_slots([], [node('other', {MIG: '40'}, {'pool': 'other'})])['slots'], 0)

    def test_validation_rejects_invalid_configuration(self):
        conditions = self.tasks[0]['ansible.builtin.assert']['that']
        for bad in [{'gpu_resource': 'nvidia.com/gpu;bad'}, {'gpu_count': 0}, {'gpu_count': 1.5}, {'replicas': -1}, {'node_selector': {'pool': True}}]:
            with self.subTest(bad=bad):
                results = [render('{{ ' + c + ' }}', {'item': self.cfg | bad}) for c in conditions]
                self.assertIn(False, results)
        self.assertTrue(all(render('{{ ' + c + ' }}', {'item': self.cfg}) for c in conditions))

    def test_rollout_counts_selected_resource_and_compares_selector(self):
        tasks = yaml.safe_load((TASKS / 'serve_model.yml').read_text())
        expr = next(t['ansible.builtin.set_fact']['_rollout_gpu_total'] for t in tasks if '_rollout_gpu_total' in t.get('ansible.builtin.set_fact', {}))
        self.assertEqual(render(expr, {'_model_cfg': self.cfg, '_rollout_nodes': {'resources': [node(), node('l4', {'nvidia.com/gpu': '100'})]}}), 4)
        compare = next(t for t in tasks if '_lis_spec_changed' in t.get('ansible.builtin.set_fact', {}))
        desired = {'spec': {'template': {'nodeSelector': {'pool': 'mig'}}}}
        current = {'spec': {'template': {'nodeSelector': {'pool': 'other'}}}}
        self.assertTrue(render(compare['ansible.builtin.set_fact']['_lis_spec_changed'], {'_cur': {}, '_new': {}, '_desired_lis': desired, '_existing_lis': {'resources': [current]}}))

    def test_resource_or_selector_change_recreates_without_stale_keys(self):
        tasks = yaml.safe_load((TASKS / 'serve_model.yml').read_text())
        task = next(t for t in tasks if '_needs_recreate' in t.get('ansible.builtin.set_fact', {}))
        def lis(resource, selector):
            return {'spec': {'template': {'nodeSelector': selector,
                    'containers': [{'name': 'main', 'resources': {'requests': {resource: '1'}}}]}}}
        for current_resource, current_selector, changed, expected in [
            ('nvidia.com/gpu', {'pool': 'mig'}, True, True),
            (MIG, {'pool': 'old'}, True, True),
            (MIG, {'pool': 'mig'}, True, False),
            (MIG, {'pool': 'mig'}, False, False),
        ]:
            with self.subTest(resource=current_resource, selector=current_selector, changed=changed):
                variables = {'_existing_lis': {'resources': [lis(current_resource, current_selector)]},
                             '_desired_lis': lis(MIG, {'pool': 'mig'}), '_model_cfg': self.cfg,
                             '_lis_spec_changed': changed, '_rollout_gpu_total': 10}
                variables['_current_gpu_keys'] = render(task['vars']['_current_gpu_keys'], variables)
                self.assertEqual(render(task['ansible.builtin.set_fact']['_needs_recreate'], variables), expected)
        self.assertFalse(render(task['ansible.builtin.set_fact']['_needs_recreate'], {'_existing_lis': {'resources': []}}))


class TestMixedVerificationTargets(unittest.TestCase):
    def test_recreation_wait_stops_on_remaining_pods_or_read_failure(self):
        source = yaml.safe_load((TASKS / 'serve_model.yml').read_text())
        wait = next(t for t in source if t.get('register') == '_recreate_pods')
        for command, success in [('printf old-pod', False), ('false', False), ('true', True)]:
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp:
                task = dict(wait, retries=1, delay=0)
                task['ansible.builtin.command'] = command
                play = [{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                         'vars': {'_needs_recreate': True, '_model_name': 'test'},
                         'tasks': [task, {'ansible.builtin.debug': {'msg': 'RECREATION_REACHED'}}]}]
                path = Path(tmp) / 'wait.yml'
                path.write_text(yaml.safe_dump(play))
                result = subprocess.run(ansible_cmd() + ['-i', 'localhost,', str(path)],
                                        cwd=ROOT, text=True, capture_output=True, timeout=60)
                self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
                self.assertEqual('RECREATION_REACHED' in result.stdout, success, result.stdout)

    def run_selection(self, targets, expected, defaults=None, recipes=None):
        # Execute production selection and its actual include loop with Ansible.
        # Replace only the cluster-reading include body with a local recorder.
        source = yaml.safe_load((ROOT / 'playbooks/verify_mig_models.yml').read_text())[1]['tasks']
        tasks = source[:4]
        include = next(t for t in source if t.get('ansible.builtin.include_tasks') == 'tasks/verify_mig_model.yml').copy()
        with tempfile.TemporaryDirectory() as tmp:
            record = Path(tmp) / 'record.yml'
            record.write_text(yaml.safe_dump([{'ansible.builtin.set_fact': {
                '_visited': '{{ _visited + [_mixed_model.key] }}'}}]))
            include['ansible.builtin.include_tasks'] = str(record)
            play = [{'hosts': 'localhost', 'gather_facts': False, 'vars': {
                '_targets': targets, '_visited': [],
                '_rd': {'model_defaults': defaults or {'gpu_resource': 'nvidia.com/gpu'}},
                'model_recipes': recipes or {'_fallback': {}},
                '_expected': expected,
            }, 'tasks': tasks + [include, {'ansible.builtin.assert': {
                'that': '_visited == _expected'}}]}]
            path = Path(tmp) / 'check.yml'
            path.write_text(yaml.safe_dump(play))
            return subprocess.run(ansible_cmd() + ['-i', 'localhost,', str(path)],
                                  cwd=ROOT, text=True, capture_output=True, timeout=60)

    def test_mixed_inventory_skips_full_gpu_in_mig_checks(self):
        result = self.run_selection([
            {'key': 'full', 'value': {}},
            {'key': 'small', 'value': {'gpu_resource': MIG}},
            {'key': 'large', 'value': {'gpu_resource': LARGE}},
        ], ['small', 'large'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_resolved_defaults_recipes_and_overrides_select_targets(self):
        result = self.run_selection([
            {'key': 'default-mig', 'value': {}},
            {'key': 'recipe-full', 'value': {'hf_repo': 'full'}},
            {'key': 'override-mig', 'value': {'hf_repo': 'full', 'gpu_resource': LARGE}},
        ], ['default-mig', 'override-mig'], defaults={'gpu_resource': MIG},
            recipes={'_fallback': {}, 'full': {'gpu_resource': 'nvidia.com/gpu'}})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_full_gpu_only_fails_instead_of_empty_pass(self):
        result = self.run_selection([{'key': 'full', 'value': {}}], [])
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('No selected model uses', result.stdout)


class TestMixedManifest(unittest.TestCase):
    def test_real_ansible_and_renderer_mixed_full_and_single(self):
        with tempfile.TemporaryDirectory() as tmp:
            inv = materialize_inventory('sample', tmp)
            comp = inv / 'group_vars/all/components.yml'
            cfg = yaml.safe_load(comp.read_text())
            cfg['models'] = {
                'small': {'hf_repo': 'Qwen/Qwen3-0.6B', 'gpu_resource': MIG, 'node_selector': {'pool': 'mig'}, 'replicas': 2},
                'large': {'hf_repo': 'Qwen/Qwen3-4B', 'gpu_resource': LARGE, 'node_selector': {'pool': 'large'}},
                'full': {'hf_repo': 'Qwen/Qwen3-0.6B'},
            }
            comp.write_text(yaml.safe_dump(cfg))
            ans = Path(tmp) / 'ans'; out = Path(tmp) / 'out'
            result = subprocess.run(ansible_cmd() + [str(ROOT / 'playbooks/llm_show_manifest.yml'), '-i', str(inv), '-e', f'dest={ans}'], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            helper = parity.TestRenderParity()
            helper._render_with_render_py_dir(inv, out)
            for name, resource in [('small', MIG), ('large', LARGE), ('full', 'nvidia.com/gpu')]:
                actual = yaml.safe_load((ans / f'{name}-llminferenceservice.yml').read_text())
                rendered = helper._extract(out / f'manifests/09-llm-serving/model-{name}-deploy.yml', 'LLMInferenceService')
                self.assertEqual(actual, rendered)
                resources = actual['spec']['template']['containers'][0]['resources']
                for kind in ['requests', 'limits']:
                    self.assertEqual({k: v for k, v in resources[kind].items() if k.startswith('nvidia.com/')}, {resource: '1'})
                if name != 'full':
                    self.assertEqual(actual['spec']['template']['nodeSelector'], cfg['models'][name]['node_selector'])
                else:
                    self.assertNotIn('nodeSelector', actual['spec']['template'])
            self.assertEqual(yaml.safe_load((ans / 'small-llminferenceservice.yml').read_text())['spec']['replicas'], 2)
            # Execute the actual generated capacity shell, including HTTP/read failure.
            guide = (out / 'manual-deploy-guide.md').read_text()
            snippet = re.search(r"gpu_nodes=\$\(oc get nodes -o json -l pool=mig\).*?exit 1; }\n", guide, re.S).group()
            bindir = Path(tmp) / 'bin'; bindir.mkdir()
            mock = bindir / 'oc'
            mock.write_text('#!/bin/sh\n[ "$MOCK_FAIL" = 1 ] && exit 1\ncat "$GPU_NODES_FILE"\n')
            mock.chmod(0o755)
            nodesfile = Path(tmp) / 'nodes.json'
            for resources, failure, expected in [({MIG: '4'}, '0', 0), ({'nvidia.com/gpu': '40'}, '0', 1), ({MIG: '1'}, '0', 1), ({MIG: '4'}, '1', 1)]:
                nodesfile.write_text(json.dumps({'items': [node(resources=resources)]}))
                result = subprocess.run(['bash', '-c', snippet], capture_output=True, text=True,
                    env={**os.environ, 'PATH': str(bindir) + ':' + os.environ['PATH'],
                         'GPU_NODES_FILE': str(nodesfile), 'MOCK_FAIL': failure})
                self.assertEqual(result.returncode, expected, result.stderr)



if __name__ == '__main__':
    unittest.main()
