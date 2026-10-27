import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
spec = importlib.util.spec_from_file_location('migration_analysis', ROOT / 'filter_plugins/migration.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def fixture():
    return dict(schema_version=1, cluster_uid='cluster-a', objects=[dict(
        id='Subscription/cert-manager-operator/openshift-cert-manager-operator',
        apiVersion='operators.coreos.com/v1alpha1', kind='Subscription',
        resource='subscriptions.operators.coreos.com', name='openshift-cert-manager-operator',
        namespace='cert-manager-operator', uid='sub-a',
        spec=dict(name='openshift-cert-manager-operator', channel='stable-v1',
                  source='customer-catalog', sourceNamespace='openshift-marketplace',
                  installPlanApproval='Manual'), observed={'installedCSV': 'cert.v1'})],
        pvcs=[dict(apiVersion='v1', kind='PersistentVolumeClaim',
                   metadata=dict(name='db', namespace='tenant', uid='pvc-a'),
                   spec=dict(storageClassName='customer-csi', accessModes=['ReadWriteOnce'],
                             volumeName='customer-pv', resources={'requests': {'storage': '20Gi'}}))],
        storage_classes=[dict(apiVersion='storage.k8s.io/v1', kind='StorageClass',
                              metadata=dict(name='customer-csi', uid='sc-a'),
                              provisioner='customer.example/csi', reclaimPolicy='Retain')])


def config():
    return dict(migration_mode='standardize', migration_external_storage=True,
                migration_runtime_reviewed=True, migration_backup_reference='customer-ticket-1',
                storage_class='customer-csi', models={})


class AnalysisTests(unittest.TestCase):
    def assess(self, cfg=None, docs=None, snap=None):
        return m.assess(snap or fixture(), cfg or config(), docs or [], 'code', 'inventory')

    def codes(self, report):
        return {x['code'] for x in report['blockers']}

    def test_draft_retains_channels_but_never_adopts_storage(self):
        snap = fixture()
        snap['objects'].append({**snap['objects'][0], 'name': 'odf-operator',
                                'spec': {'name': 'odf-operator', 'channel': 'stable-4.22'}})
        mapping = yaml.safe_load((ROOT / 'roles/migration/vars/main.yml').read_text())['migration_operator_mapping']
        out = m.infer(snap, {'install_odf': True, 'install_lvm': True, 'install_rhoai': True},
                      mapping, '/repo', '/bundle', '/kubeconfig')
        self.assertFalse(out['install_odf'])
        self.assertFalse(out['install_lvm'])
        self.assertFalse(out['install_rhoai'])
        self.assertTrue(out['install_cert_manager'])
        self.assertEqual(out['operator_channels']['openshift_cert_manager_operator'], 'stable-v1')
        self.assertEqual(out['storage_class'], 'customer-csi')
        self.assertEqual(out['keycloak_users'], [])

    def test_multiple_storage_classes_require_explicit_choice(self):
        snap = fixture()
        snap['pvcs'].append({**snap['pvcs'][0], 'spec': {'storageClassName': 'other'}})
        out = m.infer(snap, {}, {}, '/repo', '/bundle', '/kube')
        self.assertEqual(out['storage_class'], '')
        self.assertIn('CSI_SELECTION', self.codes(self.assess(out, snap=snap)))

    def test_storage_operators_and_s3_provisioning_block_apply(self):
        for flag, code in [('install_lvm', 'STORAGE_OWNERSHIP'), ('install_odf', 'STORAGE_OWNERSHIP'),
                           ('install_ogx', 'S3_ADAPTER_REQUIRED'), ('install_mlflow', 'S3_ADAPTER_REQUIRED')]:
            with self.subTest(flag=flag):
                self.assertIn(code, self.codes(self.assess({**config(), flag: True})))

    def test_storage_manifests_cannot_hide_behind_disabled_flags(self):
        for kind in ['StorageClass', 'PersistentVolume', 'BackingStore', 'ObjectBucketClaim', 'NooBaa']:
            report = self.assess(docs=[dict(kind=kind, metadata={'name': 'external'}, spec={})])
            self.assertIn('STORAGE_MUTATION', self.codes(report))

    def test_missing_credentials_and_identity_are_not_invented(self):
        codes = self.codes(self.assess({**config(), 'install_keycloak': True}))
        self.assertTrue({'CREDENTIAL_HANDOFF', 'MISSING_CREDENTIAL', 'IDENTITY_HANDOFF'} <= codes)

    def test_no_secret_values_in_reports(self):
        cfg = {**config(), 'maas_db_password': 'DO-NOT-PRINT'}
        secret = dict(kind='Secret', metadata={'name': 'db', 'namespace': 'tenant'},
                      data={'password': 'DO-NOT-PRINT'})
        self.assertNotIn('DO-NOT-PRINT', json.dumps(self.assess(cfg, [secret])))

    def test_keycloak_client_and_service_credentials_are_required_even_after_confirmation(self):
        report = self.assess({**config(), 'install_keycloak': True,
            'migration_credentials_verified': True, 'migration_identity_verified': True,
            'keycloak_db_password': 'inherited'})
        self.assertEqual({b['resource'] for b in report['blockers'] if b['code'] == 'MISSING_CREDENTIAL'},
                         {'oidc_client_secret', 'maas_oidc_client_secret', 'group_sync_password'})

    def test_missing_api_is_explicitly_reviewed_and_discovery_drift_invalidates_plan(self):
        snap = fixture(); snap['api_discovery'] = [dict(api_version='serving.kserve.io/v1alpha2',
                                                       kind='LLMInferenceService', api_found=False)]
        self.assertIn('API_NOT_DISCOVERED', self.codes(self.assess(snap=snap)))
        cfg = {**config(), 'migration_absent_apis': ['serving.kserve.io/v1alpha2/LLMInferenceService']}
        approved = self.assess(cfg, snap=snap)
        self.assertNotIn('API_NOT_DISCOVERED', self.codes(approved))
        changed = copy.deepcopy(snap); changed['api_discovery'][0]['api_found'] = True
        with self.assertRaises(ValueError):
            m.validate_plan(approved, changed, 'code', 'inventory', approved['plan_digest'])

    def test_missing_namespace_and_hidden_storage_subscription_are_blocked(self):
        docs = [dict(kind='Deployment', metadata={'name': 'app'}, spec={}),
                dict(kind='Subscription', metadata={'name': 'storage', 'namespace': 'storage'},
                     spec={'name': 'odf-operator'})]
        self.assertTrue({'NAMESPACE_REQUIRED', 'STORAGE_MUTATION'} <= self.codes(self.assess(docs=docs)))

    def test_incomplete_discovery_entry_is_a_diagnostic_not_key_error(self):
        snap = fixture(); snap['api_discovery'] = [{'api_found': True}]
        docs = [dict(kind='ConfigMap', metadata={'name': 'config', 'namespace': 'app'}, data={})]
        self.assertIn('DISCOVERY_FORMAT', self.codes(self.assess(docs=docs, snap=snap)))

    def test_pvc_class_and_size_changes_are_blocked(self):
        doc = copy.deepcopy(fixture()['pvcs'][0]); doc['spec']['storageClassName'] = 'different'
        doc['spec']['resources']['requests']['storage'] = '10Gi'
        self.assertTrue({'PVC_IMMUTABLE', 'PVC_SIZE_REVIEW'} <= self.codes(self.assess(docs=[doc])))

    def test_target_fields_do_not_report_every_server_default_as_removal(self):
        self.assertEqual(m.changed_paths({'a': 1, 'serverDefault': 2}, {'a': 1}), [])
        self.assertEqual(m.changed_paths({'a': [1, 2]}, {'a': [1]}), ['spec.a'])

    def test_new_claim_and_database_need_explicit_mapping(self):
        docs = [dict(kind='PersistentVolumeClaim', metadata=dict(name='new', namespace='tenant'), spec={}),
                dict(kind='StatefulSet', metadata=dict(name='db', namespace='tenant'), spec={})]
        self.assertTrue({'PVC_MAPPING_REQUIRED', 'STATEFUL_IDENTITY'} <= self.codes(self.assess(docs=docs)))
        cfg = {**config(), 'migration_new_pvcs': ['PersistentVolumeClaim/tenant/new'],
               'migration_new_statefulsets': ['StatefulSet/tenant/db']}
        self.assertEqual(self.codes(self.assess(cfg, docs)), set())

    def test_collection_and_deployment_cannot_target_different_clusters(self):
        cfg = {**config(), 'migration_kubeconfig': '/customer', 'k8s_kubeconfig': '/other'}
        self.assertIn('KUBECONFIG_MISMATCH', self.codes(self.assess(cfg)))

    def test_empty_model_repository_and_purge_block(self):
        cfg = {**config(), 'models': {'model': {'hf_repo': '', 'purge': True}}}
        self.assertTrue({'MODEL_SOURCE_UNKNOWN', 'MODEL_PURGE'} <= self.codes(self.assess(cfg)))

    def test_different_cluster_code_inventory_and_spec_cannot_reuse_approval(self):
        report = self.assess()
        self.assertTrue(m.validate_plan(report, fixture(), 'code', 'inventory', report['plan_digest']))
        for key, value in [('cluster_uid', 'cluster-b'), ('objects', [])]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                m.validate_plan(report, {**fixture(), key: value}, 'code', 'inventory', report['plan_digest'])
        for code, inv in [('changed', 'inventory'), ('code', 'changed')]:
            with self.assertRaises(ValueError):
                m.validate_plan(report, fixture(), code, inv, report['plan_digest'])

    def test_status_only_changes_do_not_invalidate_the_snapshot(self):
        snap = fixture(); changed = copy.deepcopy(snap)
        changed['pvcs'][0]['metadata']['resourceVersion'] = '999'
        changed['pvcs'][0]['status'] = {'phase': 'Bound'}
        self.assertEqual(m.snapshot_digest(snap), m.snapshot_digest(changed))

    def test_tampered_report_and_failed_render_cannot_authorize_apply(self):
        report = self.assess(); report['changes'].append({'action': 'tamper'})
        with self.assertRaises(ValueError):
            m.validate_plan(report, fixture(), 'code', 'inventory', report['plan_digest'])
        failed = m.assess(fixture(), config(), [], 'code', 'inventory', False)
        self.assertIn('RENDER_FAILED', self.codes(failed))
        with self.assertRaises(ValueError):
            m.validate_plan(failed, fixture(), 'code', 'inventory', failed['plan_digest'])

    def test_unknown_schema_and_old_rhoai_are_not_accepted(self):
        with self.assertRaises(ValueError):
            m.assess({**fixture(), 'schema_version': 2}, config(), [], '', '')
        snap = fixture(); snap['objects'][0]['spec'].update(name='rhods-operator', channel='stable-2.25')
        self.assertIn('RHOAI_VERSION_PATH', self.codes(self.assess({**config(), 'install_rhoai': True}, snap=snap)))


class OfflinePlaybookTests(unittest.TestCase):
    def run_playbook(self, name, variables, extra=None):
        # No real kubeconfig or oc is available to these runs.
        env = {**os.environ, 'KUBECONFIG': '/nonexistent/migration-test-kubeconfig',
               'ANSIBLE_CONFIG': str(ROOT / 'ansible.cfg')}
        return subprocess.run([str(Path(sys.executable).parent / 'ansible-playbook'), name,
                               '-e', json.dumps(variables), *(extra or [])], cwd=ROOT,
                              env=env, text=True, capture_output=True, timeout=90)

    def run_tasks(self, tasks, variables):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'test.yml'
            path.write_text(yaml.safe_dump([dict(hosts='localhost', gather_facts=False,
                                                 connection='local', tasks=tasks)]))
            return self.run_playbook(str(path), variables)

    def test_pack_includes_toolkit_but_excludes_customer_output(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            script = temp / 'pack-playbook.sh'
            script.write_bytes((REPO.parent / 'pack-playbook.sh').read_bytes())
            toolkit = temp / 'sample-ansible/migration'
            (toolkit / 'output/customer').mkdir(parents=True)
            (toolkit / 'output/customer/baseline.yml').write_text('private-fixture')
            (toolkit / 'collect.yml').write_text('public-tool')
            archive = temp / 'source.tar.gz'
            result = subprocess.run(['bash', str(script), 'sample-ansible', '-o', str(archive)],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            with tarfile.open(archive) as packed:
                names = packed.getnames()
            self.assertIn('sample-ansible/migration/collect.yml', names)
            self.assertFalse(any('/migration/output' in name for name in names))

    def test_direct_site_entry_stops_before_any_oc_call(self):
        # Production guard, followed by a sentinel, with no cluster commands involved.
        first = yaml.safe_load((REPO / 'roles/preflight/tasks/main.yml').read_text())[0]
        tasks = [first,
                 {'name': 'MUTATION_SENTINEL', 'ansible.builtin.fail': {'msg': 'guard failed'}}]
        for flags in [dict(migration_mode='standardize'), dict(migration_external_storage=True)]:
            result = self.run_tasks(tasks, dict(ansible_root=str(REPO), **flags))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Require the reviewed migration entrypoint', result.stdout)
            self.assertNotIn('MUTATION_SENTINEL', result.stdout)

    def test_migration_neither_adopts_lvm_nor_loads_or_writes_password_cache(self):
        production = yaml.safe_load((REPO / 'roles/preflight/tasks/main.yml').read_text())
        names = {'Load previously generated passwords', 'Generate maas_db_password if empty',
                 'Generate ogx_db_password if empty', 'Generate keycloak_db_password if empty',
                 'Check if passwords file needs update', 'Save generated passwords to local file',
                 'Auto-enable install_lvm when storage_class uses lvms'}
        selected = [t for t in production if t.get('name') in names]
        self.assertEqual(len(selected), len(names), 'Production task names changed; update the extraction')
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp) / '.generated-passwords.yml'
            original = 'maas_db_password: stale-cache\nkeycloak_db_password: stale-cache\n'
            cache.write_text(original)
            result = self.run_tasks(selected + [{'ansible.builtin.assert': {'that': [
                'not install_lvm', "maas_db_password == 'customer-existing'",
                "keycloak_db_password == ''", "ogx_db_password == ''"]}}], dict(
                ansible_root=temp, migration_mode='standardize', migration_external_storage=True,
                storage_class='lvms-existing', install_lvm=False, maas_db_password='customer-existing',
                keycloak_db_password='', ogx_db_password=''))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(cache.read_text(), original)

    def test_offline_inventory_plan_and_refusal_to_apply_blocked_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp); snapshot = temp / 'source.yml'; bundle = temp / 'bundle'
            snapshot.write_text(yaml.safe_dump(fixture()))
            result = self.run_playbook('generate.yml', dict(migration_snapshot_file=str(snapshot),
                migration_output_dir=str(bundle), migration_kubeconfig='/nonexistent/kubeconfig'))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            cfg = yaml.safe_load((bundle / 'inventory/group_vars/all/components.yml').read_text())
            self.assertTrue(cfg['install_cert_manager'])
            self.assertFalse(cfg['install_odf'])
            self.assertEqual((bundle / 'baseline.yml').stat().st_mode & 0o777, 0o600)
            result = self.run_playbook('plan.yml', {'migration_bundle': str(bundle)})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            plan = yaml.safe_load((bundle / 'plan.yml').read_text())
            self.assertTrue(plan['blockers'])
            self.assertNotIn('RENDER_FAILED', {b['code'] for b in plan['blockers']})
            self.assertTrue(any(c['resource'].startswith('Subscription/') for c in plan['changes']))
            result = self.run_playbook('apply.yml', dict(migration_bundle=str(bundle),
                migration_approved_digest=plan['plan_digest']))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Unresolved migration blockers', result.stdout + result.stderr)
            self.assertNotIn('Refresh live identity', result.stdout)
            # Collection/generation never silently overwrites an evidence bundle.
            result = self.run_playbook('generate.yml', dict(migration_snapshot_file=str(snapshot),
                migration_output_dir=str(bundle), migration_kubeconfig='/nonexistent/kubeconfig'))
            self.assertNotEqual(result.returncode, 0)

    def test_real_apply_gate_accepts_matching_state_and_rejects_fresh_drift(self):
        tasks = yaml.safe_load((ROOT / 'roles/migration/tasks/apply.yml').read_text())
        reads = [t for t in tasks if t.get('ansible.builtin.include_tasks') == 'read.yml']
        self.assertEqual(len(reads), 1)
        # Replace only API collection with a synthetic response; drive the production gate.
        reads[0].pop('ansible.builtin.include_tasks')
        reads[0]['ansible.builtin.set_fact'] = {'_migration_snapshot': '{{ synthetic_live }}'}
        tasks.append({'name': 'AUTHORIZED_SENTINEL', 'ansible.builtin.assert': {
            'that': '_migration_authorized | bool'}})
        with tempfile.TemporaryDirectory() as temp:
            bundle = Path(temp)
            variables_dir = bundle / 'inventory/group_vars/all'
            variables_dir.mkdir(parents=True)
            (variables_dir / 'components.yml').write_text(yaml.safe_dump(config()))
            (bundle / 'baseline.yml').write_text(yaml.safe_dump(fixture()))
            report = m.assess(fixture(), config(), [], m.source_digest(REPO), m.inventory_digest(bundle))
            (bundle / 'plan.yml').write_text(yaml.safe_dump(report))
            variables = dict(ansible_root=str(REPO), migration_bundle=str(bundle),
                             migration_approved_digest=report['plan_digest'], synthetic_live=fixture())
            result = self.run_tasks(tasks, variables)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('AUTHORIZED_SENTINEL', result.stdout)
            variables['synthetic_live'] = {**fixture(), 'cluster_uid': 'other-cluster'}
            result = self.run_tasks(tasks, variables)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('AUTHORIZED_SENTINEL', result.stdout)


if __name__ == '__main__':
    unittest.main()
