"""Local analysis for Ansible migration playbooks. No API calls or deployment code."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def identity(obj):
    meta = obj.get("metadata", obj)
    return f"{obj['kind']}/{meta.get('namespace') or '_cluster'}/{meta['name']}"


def source_digest(root):
    root = Path(root).resolve()
    files = [root / "site.yml", root / "ansible.cfg", root / "migration/ansible.cfg"]
    for directory in ("roles", "playbooks", "render-manifests", "migration/roles", "migration/filter_plugins"):
        files += [p for p in (root / directory).rglob("*") if p.is_file()
                  and "__pycache__" not in p.parts and p.suffix != ".pyc"]
    files += list((root / "migration").glob("*.yml"))
    return digest([(str(p.relative_to(root)), hashlib.sha256(p.read_bytes()).hexdigest())
                   for p in sorted(set(files))])


def inventory_digest(bundle):
    base = Path(bundle) / "inventory"
    return digest([(str(p.relative_to(base)), hashlib.sha256(p.read_bytes()).hexdigest())
                   for p in sorted(base.rglob("*")) if p.is_file()])


def snapshot_digest(snapshot):
    # Status / resourceVersion naturally change; identity and desired state do not.
    def state(obj):
        meta = obj.get("metadata", obj)
        return {"id": identity(obj), "uid": meta.get("uid"), "spec": obj.get("spec", {}),
                "observed": obj.get("observed", {})}
    return digest({"cluster_uid": snapshot["cluster_uid"],
                   "api_discovery": snapshot.get('api_discovery', []),
                   "objects": sorted([state(o) for o in snapshot["objects"]], key=lambda o: o['id']),
                   "pvcs": sorted([state(o) for o in snapshot.get("pvcs", [])], key=lambda o: o['id']),
                   "storage_classes": sorted([{k: o.get(k) for k in
                        ("provisioner", "parameters", "reclaimPolicy", "volumeBindingMode", "allowVolumeExpansion")}
                        | {"name": o["metadata"]["name"], "uid": o["metadata"].get("uid")}
                        for o in snapshot.get("storage_classes", [])], key=lambda o: o['name'])})


def infer(snapshot, defaults, mapping, root, bundle, kubeconfig):
    """Generate a draft, never invent passwords, HF repositories or tenant identities."""
    cfg = copy.deepcopy(defaults)
    for k in list(cfg):
        if k.startswith(("install_", "integrate_")):
            cfg[k] = False
    cfg.update(migration_mode="standardize", migration_external_storage=True,
               migration_credentials_verified=False, migration_identity_verified=False,
               migration_runtime_reviewed=False, migration_backup_reference='',
               migration_baseline_file=str(Path(bundle) / "baseline.yml"),
               ansible_root=str(Path(root).resolve()), k8s_kubeconfig=kubeconfig,
               migration_kubeconfig=kubeconfig, models={}, keycloak_users=[], keycloak_groups={},
               keycloak_admin_recovery=False, keycloak_prune_group_membership=False)
    cfg["operator_channels"] = dict(cfg.get("operator_channels", {}))
    subscriptions = [o for o in snapshot['objects'] if o['kind'] == 'Subscription']
    catalogs = {'redhat': set(), 'community': set(), 'certified': set()}
    for o in subscriptions:
        spec = o['spec']; package = spec.get('name')
        if package in mapping and sum(x['spec'].get('name') == package for x in subscriptions) == 1:
            cfg['operator_channels'][mapping[package]['channel_key']] = spec.get('channel', '')
            cfg[mapping[package]['flag']] = True
            category = 'community' if package == 'group-sync-operator' else ('certified' if package == 'gpu-operator-certified' else 'redhat')
            if spec.get('source'):
                catalogs[category].add(spec['source'])
    cfg['catalog_sources'] = dict(cfg.get('catalog_sources', {}))
    for category, sources in catalogs.items():
        if len(sources) == 1:
            cfg['catalog_sources'][category] = next(iter(sources))
    # Storage platform ownership is never inferred from its presence.
    cfg.update(install_lvm=False, install_odf=False, install_metallb=False)
    classes = {o['spec'].get('storageClassName') for o in snapshot.get('pvcs', [])}
    classes.discard(None); classes.discard('')
    cfg['storage_class'] = next(iter(classes)) if len(classes) == 1 else ''
    keycloaks = [o for o in snapshot['objects'] if o['kind'] == 'Keycloak']
    if len(keycloaks) == 1:
        cfg['keycloak_namespace'] = keycloaks[0]['namespace']
    for kind, flag in [('MLflow', 'install_mlflow'), ('OGXServer', 'install_ogx'),
                       ('NemoGuardrails', 'install_guardrails'), ('LLMInferenceService', 'install_llm_serving')]:
        cfg[flag] = any(o['kind'] == kind for o in snapshot['objects'])
    namespaces = {o['namespace'] for o in snapshot['objects'] if o['kind'] == 'LLMInferenceService'}
    if len(namespaces) == 1:
        cfg['llm_namespace'] = next(iter(namespaces))
    for o in snapshot['objects']:
        if o['kind'] != 'LLMInferenceService':
            continue
        spec = o['spec']; pod = spec.get('template', {})
        main = next((c for c in pod.get('containers', []) if c.get('name') == 'main'), {})
        resources = copy.deepcopy(main.get('resources', {}))
        gpu = [(k, v) for k, v in resources.get('limits', {}).items() if k.startswith('nvidia.com/')]
        for section in resources.values():
            for k in list(section):
                if k.startswith('nvidia.com/'):
                    section.pop(k)
        # Duplicate names cannot be represented in the models dict: assess() blocks them.
        cfg['models'][o['name']] = dict(
            hf_repo='', state='present', purge=False, namespace=o['namespace'],
            vllm_image=main.get('image', ''), vllm_args=main.get('args', []),
            resources=resources, replicas=spec.get('replicas', 1),
            gpu_resource=gpu[0][0] if len(gpu) == 1 else '',
            gpu_count=gpu[0][1] if len(gpu) == 1 else 0,
            node_selector=pod.get('nodeSelector', {}),
            custom_chat_template='', chat_template_configmap='')
    return cfg


def changed_paths(live, desired, path="spec"):
    """Target-field comparison, NOT an emulation of SSA/strategic merge."""
    if isinstance(desired, dict) and isinstance(live, dict):
        result = []
        for k, v in desired.items():
            result += changed_paths(live.get(k), v, f"{path}.{k}")
        return result
    return [] if live == desired else [path]


def assess(snapshot, cfg, manifests, code_hash, config_hash, rendered=True):
    if snapshot.get('schema_version') != 1 or not snapshot.get('cluster_uid'):
        raise ValueError('Unsupported or incomplete migration snapshot')
    blockers, warnings, changes = [], [], []

    def issue(bucket, code, resource, detail):
        bucket.append(dict(code=code, resource=resource, detail=detail))

    objects = snapshot['objects']
    if not cfg.get('migration_runtime_reviewed'):
        issue(blockers, 'ROLE_PATH_REVIEW', '', 'Review non-manifest API/prune/recreate paths for this customer and record migration_runtime_reviewed=true only after completing that review.')
    if not cfg.get('migration_backup_reference'):
        issue(blockers, 'BACKUP_REFERENCE', '', 'Record the customer-approved database/data recovery reference; this tool does not create or restore storage backups.')
    if not rendered:
        issue(blockers, 'RENDER_FAILED', '', 'Target rendering failed; inspect the private analysis render.log. Partial manifests cannot authorize apply.')
    live = {identity(o): o for o in objects + snapshot.get('pvcs', [])}
    for flag in ('install_lvm', 'install_odf', 'install_metallb'):
        if cfg.get(flag, False):
            issue(blockers, 'STORAGE_OWNERSHIP', flag, 'External infrastructure must remain disabled.')
    if not cfg.get('migration_external_storage') or cfg.get('migration_mode') != 'standardize':
        issue(blockers, 'PROFILE', '', 'Keep migration_mode=standardize and migration_external_storage=true.')
    if cfg.get('storage_class') not in {o['metadata']['name'] for o in snapshot.get('storage_classes', [])}:
        issue(blockers, 'CSI_SELECTION', '', 'Choose an existing StorageClass; do not infer a default when classes differ.')
    # These roles currently create/manage NooBaa resources. No blanket escape hatch.
    for flag in ('install_mlflow', 'install_ogx'):
        if cfg.get(flag):
            issue(blockers, 'S3_ADAPTER_REQUIRED', flag,
                  'This role creates OBC/BackingStore resources. Adapt it and the renderer to an existing S3 service before enabling it; CSI alone is not S3.')
    if (cfg.get('install_rhoai') or cfg.get('install_keycloak')) and not cfg.get('migration_credentials_verified'):
        issue(blockers, 'CREDENTIAL_HANDOFF', '', 'Verify existing DB/client credentials and supply vault values; random replacement breaks existing data.')
    for flag, password in [('install_rhoai', 'maas_db_password'), ('install_keycloak', 'keycloak_db_password'),
                           ('install_keycloak', 'oidc_client_secret'), ('install_keycloak', 'maas_oidc_client_secret'),
                           ('install_keycloak', 'group_sync_password')]:
        if cfg.get(flag) and not cfg.get(password):
            issue(blockers, 'MISSING_CREDENTIAL', password, 'Existing credential required; do not let preflight generate a replacement.')
    if cfg.get('install_keycloak') and not cfg.get('migration_identity_verified'):
        issue(blockers, 'IDENTITY_HANDOFF', '', 'Map realm, clients, users, groups and OAuth before running Keycloak/integration roles.')
    if cfg.get('keycloak_admin_recovery'):
        issue(blockers, 'ADMIN_RECOVERY', '', 'Automatic admin recovery is not allowed in a migration.')
    if cfg.get('migration_kubeconfig') != cfg.get('k8s_kubeconfig'):
        issue(blockers, 'KUBECONFIG_MISMATCH', '', 'Discovery and standard roles must use the same explicit kubeconfig.')
    for api in snapshot.get('api_discovery', []):
        if not isinstance(api, dict) or not all(isinstance(api.get(k), str) and api[k]
                                               for k in ('api_version', 'kind')):
            issue(blockers, 'DISCOVERY_FORMAT', '', 'Recollect API discovery with named kinds and versions.')
            continue
        api_id = api['api_version'] + '/' + api['kind']
        if not api.get('api_found'):
            confirmed = api_id in cfg.get('migration_absent_apis', [])
            issue(warnings if confirmed else blockers, 'API_NOT_DISCOVERED', api_id,
                  'Requested API was not found. Confirm actual absence versus another served version; only confirmed absence may be listed in migration_absent_apis.')
    models = cfg.get('models', {})
    names = [o['name'] for o in objects if o['kind'] == 'LLMInferenceService']
    if len(names) != len(set(names)):
        issue(blockers, 'DUPLICATE_MODEL_NAME', '', 'Multiple namespaces use the same model name; a single models dictionary cannot represent them.')
    for name, model in models.items():
        if model.get('purge'):
            issue(blockers, 'MODEL_PURGE', name, 'Cache deletion is outside the storage-preserving contract.')
        if model.get('state', 'present') == 'present' and not model.get('hf_repo'):
            issue(blockers, 'MODEL_SOURCE_UNKNOWN', name, 'pvc:// does not identify a HuggingFace repository. Map it explicitly.')
        if model.get('state') == 'absent':
            issue(warnings, 'MODEL_REMOVAL', name, 'LIS, model reference and download Job will be removed; serving stops.')
    for o in objects:
        if o['kind'] == 'Subscription' and o['spec'].get('name') == 'rhods-operator':
            if cfg.get('install_rhoai') and not o['spec'].get('channel', '').startswith('stable-3.5'):
                issue(blockers, 'RHOAI_VERSION_PATH', identity(o), 'Validate the supported RHOAI upgrade path before applying this 3.5 playbook; channel changes are not a migration plan.')
    for doc in manifests:
        if not isinstance(doc, dict) or not doc.get('kind'):
            continue
        kind = doc['kind']; key = identity(doc); old = live.get(key)
        if re.search(r'<[A-Z][A-Z0-9_]*>', json.dumps(doc)):
            issue(warnings, 'RUNTIME_PLACEHOLDER', key, 'Renderer contains runtime placeholders (domain, certificate or API-derived data). This static diff is not the final applied value; review the corresponding standard role.')
        namespaced = {'PersistentVolumeClaim', 'StatefulSet', 'Deployment', 'Subscription',
                      'Keycloak', 'GroupSync', 'LLMInferenceService', 'MaaSModelRef', 'MaaSAuthPolicy',
                      'MaaSSubscription', 'Secret', 'ConfigMap', 'Service', 'Route', 'Job',
                      'ServiceAccount', 'Role', 'RoleBinding', 'MLflow', 'OGXServer', 'NemoGuardrails',
                      'OperatorGroup', 'KeycloakRealmImport', 'Kuadrant', 'MaasTenantConfig',
                      'NetworkPolicy', 'HardwareProfile', 'Gateway'}
        if kind in namespaced and not doc.get('metadata', {}).get('namespace'):
            issue(blockers, 'NAMESPACE_REQUIRED', key, 'Namespaced target lacks an explicit namespace; identity comparison cannot be trusted.')
        collected_kinds = {o['kind'] for o in objects} | {'PersistentVolumeClaim', 'StorageClass'} | {
            a['kind'] for a in snapshot.get('api_discovery', []) if isinstance(a, dict) and a.get('api_found') and a.get('kind')}
        if kind not in collected_kinds:
            issue(warnings, 'UNOBSERVED_KIND', key, 'This kind was not collected; existing objects and overwritten fields require independent review.')
        storage_subscription = kind == 'Subscription' and doc.get('spec', {}).get('name') in (
            'odf-operator', 'ocs-operator', 'lvms-operator', 'local-storage-operator', 'odf-csi-addons-operator')
        if kind in ('StorageClass', 'PersistentVolume', 'StorageCluster', 'CephCluster', 'NooBaa',
                    'BackingStore', 'BucketClass', 'LVMCluster', 'ObjectBucketClaim', 'CephObjectStore',
                    'CephFilesystem', 'CephBlockPool', 'StorageSystem', 'VolumeSnapshotClass',
                    'VolumeSnapshotContent', 'CSIDriver', 'VolumeSnapshot') or storage_subscription:
            issue(blockers, 'STORAGE_MUTATION', key, 'The target still contains storage platform/bucket provisioning. Use existing services.')
            continue
        if kind == 'Secret':
            issue(warnings, 'SECRET_WRITE', key, 'Role may create/update credentials; values are deliberately excluded from this report.')
            continue
        if kind == 'PersistentVolumeClaim' and old:
            fields = ('storageClassName', 'accessModes', 'volumeMode', 'dataSource', 'dataSourceRef')
            for field in fields:
                if field in doc['spec'] and doc['spec'][field] != old['spec'].get(field):
                    issue(blockers, 'PVC_IMMUTABLE', key, f'Existing PVC differs at spec.{field}; no replacement is allowed.')
            wanted = doc['spec'].get('resources', {}).get('requests', {}).get('storage')
            actual = old['spec'].get('resources', {}).get('requests', {}).get('storage')
            if wanted and wanted != actual:
                issue(blockers, 'PVC_SIZE_REVIEW', key, 'Requested size differs (including potentially equivalent units); retain existing PVC, do not resize automatically.')
        if kind == 'PersistentVolumeClaim' and not old:
            if key not in cfg.get('migration_new_pvcs', []):
                issue(blockers, 'PVC_MAPPING_REQUIRED', key, 'Map the existing data PVC or explicitly list this intentional new CSI claim in migration_new_pvcs. A new empty volume is not data migration.')
        if kind == 'StatefulSet':
            if old and doc.get('spec', {}).get('volumeClaimTemplates') != old.get('spec', {}).get('volumeClaimTemplates'):
                issue(blockers, 'STATEFUL_CLAIMS', key, 'Claim templates differ; review immutable storage fields and preserve existing volume identity.')
            if not old and key not in cfg.get('migration_new_statefulsets', []):
                issue(blockers, 'STATEFUL_IDENTITY', key, 'Map the existing database StatefulSet/PVC before starting a new standard-named instance; explicitly list intentional new instances in migration_new_statefulsets.')
        if old:
            paths = changed_paths(old.get('spec', {}), doc.get('spec', {}))
            if paths:
                changes.append(dict(resource=key, action='potential-update', paths=paths))
                if kind == 'LLMInferenceService':
                    issue(warnings, 'MODEL_RECREATE', key, 'GPU capacity/node placement/spec changes can delete and recreate the serving resource; plan downtime.')
                if kind == 'Subscription':
                    issue(warnings, 'OPERATOR_CHANGE', key, 'Channel/catalog/approval/config differences can trigger an operator update and reconciliation.')
        else:
            changes.append(dict(resource=key, action='create-or-unobserved', paths=[]))
            same_kind = [o for o in objects if o['kind'] == kind]
            if same_kind and kind in ('DataScienceCluster', 'DSCInitialization', 'ClusterPolicy', 'Keycloak'):
                issue(blockers, 'IDENTITY_MISMATCH', key, 'Existing resource uses another name/namespace; do not create a competing singleton.')
    if any(o['kind'] == 'LLMInferenceService' for o in objects) and cfg.get('install_llm_serving'):
        for o in objects:
            if o['kind'] == 'LLMInferenceService':
                expected = f"pvc://hf-model-cache/{o['name']}"
                if o['spec'].get('model', {}).get('uri') != expected:
                    issue(blockers, 'MODEL_STORAGE_MAPPING', identity(o), 'Standard role uses hf-model-cache/<model-name>; preserve existing model data with an explicit adapter before continuing.')
    issue(warnings, 'NON_MANIFEST_ACTIONS', '', 'The standard roles also execute API calls, credential generation, pruning and conditional deletes. Review role paths; manifest diff is not a complete execution simulation.')
    report = dict(schema_version=1, cluster_uid=snapshot['cluster_uid'],
                  baseline_digest=snapshot_digest(snapshot), code_digest=code_hash,
                  inventory_digest=config_hash, blockers=blockers, warnings=warnings, changes=changes,
                  coverage='Target-field static comparison plus explicit risk rules; not a server-side dry-run or proof of safe apply.')
    report['plan_digest'] = digest(report)
    return report


def validate_plan(report, snapshot, code_hash, config_hash, approved):
    unsigned = {k: v for k, v in report.items() if k != 'plan_digest'}
    if digest(unsigned) != report.get('plan_digest') or approved != report.get('plan_digest'):
        raise ValueError('Plan checksum/approval mismatch; review and approve the exact regenerated plan.')
    if report.get('blockers'):
        raise ValueError('Unresolved migration blockers; apply is disabled.')
    if report['baseline_digest'] != snapshot_digest(snapshot):
        raise ValueError('Cluster identity or declarative state changed; collect and plan again.')
    if report['code_digest'] != code_hash or report['inventory_digest'] != config_hash:
        raise ValueError('Implementation or inventory changed after planning; regenerate the plan.')
    return True


class FilterModule:
    def filters(self):
        return {'migration_infer': infer, 'migration_assess': assess,
                'migration_source_digest': source_digest, 'migration_inventory_digest': inventory_digest,
                'migration_snapshot_digest': snapshot_digest, 'migration_validate_plan': validate_plan}
