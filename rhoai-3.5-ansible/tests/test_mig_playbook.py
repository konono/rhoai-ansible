"""Evaluate the actual Ansible MIG safety expressions without a cluster."""
import copy
import unittest
from pathlib import Path
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar
try:
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(value):
        return value

ROOT = Path(__file__).resolve().parents[1]


def render(expression, variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(expression))


class MigWorkloadGuard(unittest.TestCase):
    def setUp(self):
        tasks = yaml.safe_load((ROOT / 'playbooks/tasks/mig_check_workloads.yml').read_text())
        self.expression = tasks[1]['ansible.builtin.set_fact']['_mig_busy']
        self.base = {'metadata': {'name': 'model', 'namespace': 'test'},
                     'spec': {'nodeName': 'worker', 'containers': [{'resources': {'requests': {'nvidia.com/gpu': '1'}}}]},
                     'status': {'phase': 'Running'}}

    def evaluate(self, pods, change=False, targets=None):
        return render(self.expression, {'_mig_pods': {'resources': pods}, '_mig_strategy_change': change,
                       '_mig_targets': [{'metadata': {'name': name}} for name in (['worker'] if targets is None else targets)]})

    def test_empty_result_is_list_not_whitespace_string(self):
        self.assertEqual(self.evaluate([]), [])

    def test_active_gpu_blocks_change(self):
        self.assertEqual(self.evaluate([self.base]), ['test/model'])

    def test_converged_node_allows_inference(self):
        self.assertEqual(self.evaluate([self.base], targets=[]), [])

    def test_global_strategy_change_checks_other_nodes(self):
        self.assertEqual(self.evaluate([self.base], change=True, targets=[]), ['test/model'])

    def test_completed_pods_do_not_block(self):
        for phase in ['Succeeded', 'Failed']:
            p = copy.deepcopy(self.base); p['status']['phase'] = phase
            self.assertEqual(self.evaluate([p]), [])

    def test_pending_assigned_pod_blocks(self):
        p = copy.deepcopy(self.base); p['status']['phase'] = 'Pending'
        self.assertEqual(self.evaluate([p]), ['test/model'])

    def test_mixed_resource_init_container_blocks(self):
        p = copy.deepcopy(self.base)
        p['spec']['containers'] = [{'name': 'cpu'}]
        p['spec']['initContainers'] = [{'resources': {'limits': {'nvidia.com/mig-1g.24gb': 1}}}]
        self.assertEqual(self.evaluate([p]), ['test/model'])

    def test_cpu_or_zero_gpu_does_not_block(self):
        for resources in [{}, {'requests': {'cpu': '1'}}, {'limits': {'nvidia.com/gpu': '0'}}]:
            p = copy.deepcopy(self.base); p['spec']['containers'] = [{'resources': resources}]
            self.assertEqual(self.evaluate([p]), [])

    def test_unrelated_node_does_not_block_profile_change(self):
        self.assertEqual(self.evaluate([self.base], targets=['another']), [])


class MigProfileCompletion(unittest.TestCase):
    def setUp(self):
        play = yaml.safe_load((ROOT / 'playbooks/configure_mig.yml').read_text())[0]
        block = next(t['block'] for t in play['tasks'] if t['name'] == 'Apply MIG configuration')
        task = next(t for t in block if t['name'].startswith('Set MIG profile and invalidate'))
        self.labels = task['kubernetes.core.k8s']['definition']['metadata']['labels']
        self.assertEqual(task['kubernetes.core.k8s']['merge_type'], 'merge')

    def test_profile_change_invalidates_previous_success(self):
        result = render(self.labels, {'mig_profile': 'all-disabled', 'item': {
            'metadata': {'labels': {'nvidia.com/mig.config': 'all-1g.24gb', 'nvidia.com/mig.config.state': 'success'}}}})
        self.assertEqual(result, {'nvidia.com/mig.config': 'all-disabled', 'nvidia.com/mig.config.state': None})

    def test_same_profile_preserves_controller_status(self):
        result = render(self.labels, {'mig_profile': 'all-1g.24gb', 'item': {
            'metadata': {'labels': {'nvidia.com/mig.config': 'all-1g.24gb'}}}})
        self.assertNotIn('nvidia.com/mig.config.state', result)

    def test_first_configuration_has_no_stale_success(self):
        result = render(self.labels, {'mig_profile': 'all-1g.24gb', 'item': {'metadata': {'labels': {}}}})
        self.assertIsNone(result['nvidia.com/mig.config.state'])

    def test_disable_import_uses_whole_gpu_capacity(self):
        play = yaml.safe_load((ROOT / 'playbooks/disable_mig.yml').read_text())[-1]
        self.assertEqual(play['vars']['mig_profile'], 'all-disabled')
        self.assertEqual(render(play['vars']['mig_expected_resources'], {'mig_full_gpu_resources': {'nvidia.com/gpu': 2}}), {'nvidia.com/gpu': 2})
        self.assertNotIn('gpu_mig_strategy', play['vars'])


if __name__ == '__main__':
    unittest.main()
