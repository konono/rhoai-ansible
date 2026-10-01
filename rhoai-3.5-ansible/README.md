[日本語](README_ja.md)

# RHOAI 3.5 Ansible Installer

## 1. Overview

An Ansible Playbook for deploying OpenShift AI (RHOAI) 3.5 to an OpenShift environment. It supports both Single Node OpenShift (SNO) and multi-node clusters.

The following components are installed in stages:

- **Infrastructure**: LVM Operator (local storage), MetalLB (LoadBalancer)
- **Dependency Operators**: cert-manager, ServiceMesh, NFD, GPU Operator, RHCL (Authorino), Kueue, JobSet, LeaderWorkerSet
- **Storage**: ODF (NooBaa object storage)
- **Platform**: OpenShift AI (RHOAI), Keycloak (IdP)
- **Integration**: Keycloak → OpenShift OAuth, Keycloak → MaaS policies, AITenant OIDC
- **Workloads**: LLM Serving (vLLM), MaaS resources, MLflow, OGX, NeMo Guardrails, Observability

---

## 2. Prerequisites

### Hardware Requirements

| Resource | Minimum | Notes |
|---|---|---|
| GPU | NVIDIA GPU × 1 | Managed by GPU Operator. Required for vLLM inference |
| Storage | 1 or more free block devices | Used by LVM Operator as VolumeGroup. 200GB+ recommended |
| Memory | 64GB or more | On SNO all components run on a single node. Can be distributed on multi-node |
| CPU | 16 vCPU or more | |

### Software Requirements

| Software | Version | Purpose |
|---|---|---|
| OpenShift | 4.22 | Base cluster |
| Python | 3.12.x | Ansible execution environment. Wheels are built for cp312, so **only 3.12.x is supported** |
| `oc` | 4.22+ | OpenShift CLI (must be in PATH) |
| pip | latest | Python package management (uv is auto-installed, no prior setup needed) |

> **Note on Python version**: Even if the host does not have Python 3.12, `download-deps.sh` downloads a standalone Python 3.12 build, and `setup-env.sh` installs it automatically. It does not affect the host's Python installation.

### Python Packages

| Package | Version | Purpose |
|---|---|---|
| ansible-core | >= 2.17, < 3.0 | Ansible core |
| ansible | >= 10.0 | Ansible collections |
| kubernetes | >= 29.0 | For `kubernetes.core` modules |
| jmespath | >= 1.0 | For `json_query` filter |

> These are defined in `pyproject.toml`. No manual pip install is required.

### Ansible Galaxy Collections

| Collection | Version | Purpose |
|---|---|---|
| kubernetes.core | >= 5.0.0 | K8s resource management |
| community.general | >= 9.0.0 | General-purpose filters and modules |

### Cluster Requirements

- Logged in with `cluster-admin` privileges (`oc login`)
- Access to OperatorHub (redhat-operators) (pre-mirror for disconnected environments)
- DNS resolution for `*.apps.<cluster_domain>`

---

## 3. Setup

### Quick Start (All Environments)

```bash
# 1. Download dependencies in an online environment (first time only)
cd ansible
bash scripts/download-deps.sh

# 2. Environment setup (same command for both online and disconnected)
bash scripts/setup-env.sh

# 3. Run playbook
uv run ansible-playbook site.yml -i inventory/myenv
```

That's it. The following sections provide detailed explanations.

### Running Playbooks

This project uses **`uv run` as the standard execution method**. `source .venv/bin/activate` is not needed.

```bash
# Prefix all ansible commands with uv run
uv run ansible-playbook site.yml -i inventory/myenv
uv run ansible-playbook playbooks/verify.yml -i inventory/myenv
uv run ansible-galaxy collection list
uv run ansible --version
```

> **Why `uv run`**: `uv run` executes commands within the virtual environment without activating `.venv`. This prevents the "accidentally ran with host Python" issue caused by forgetting to activate. The `[tool.uv] find-links` setting in `pyproject.toml` ensures local wheels are referenced automatically even offline.

### Step 1: Download Dependencies (Run in Online Environment)

```bash
cd ansible
bash scripts/download-deps.sh
```

The following are downloaded:

| Destination | Contents | Approx. Size |
|---|---|---|
| `vendor/uv/` | uv binary (4 platforms) | ~70MB |
| `vendor/python/` | Python 3.12 standalone build (4 platforms) | ~200MB |
| `vendor/wheels/` | Python wheels (macOS ARM64/x86_64, Linux aarch64/x86_64) | ~160MB |
| `vendor/collections/` | Ansible Galaxy collections | ~3MB |

You can also re-download specific components:

```bash
bash scripts/download-deps.sh python       # Python only
bash scripts/download-deps.sh wheels       # wheels only
bash scripts/download-deps.sh uv python    # multiple targets
bash scripts/download-deps.sh --help       # help
```

> **GitHub API Rate Limiting**: Downloading Python standalone builds uses the GitHub API. If `gh` CLI is available, it uses authenticated API calls to avoid rate limiting. Running `gh auth login` beforehand is recommended.

### Step 2: Transfer to Disconnected Environment

Copying the entire `ansible/` directory is the most reliable method. The minimum required files are:

```
ansible/
├── vendor/              ← Downloaded dependencies (required)
│   ├── uv/              ← uv binary
│   ├── python/          ← Python 3.12 standalone build
│   ├── wheels/          ← Python wheels
│   └── collections/     ← Ansible Galaxy collections
├── pyproject.toml       ← Dependency definitions + uv find-links config (required)
├── scripts/             ← Setup scripts (required)
├── ansible.cfg          ← Ansible configuration (required)
├── requirements.yml     ← Galaxy collections definition (required)
├── site.yml             ← Main playbook
├── roles/               ← Roles
├── inventory/           ← Inventory
└── ...
```

### Step 3: Environment Setup (Run in Disconnected Environment)

```bash
cd ansible
bash scripts/setup-env.sh
```

`setup-env.sh` automatically performs the following:

1. **uv installation** — Extracts the platform-appropriate uv binary from `vendor/uv/` to `.local/bin/`
2. **Python 3.12 installation** — Extracts the standalone build from `vendor/python/` to `.local/python/` (only if host lacks Python 3.12)
3. **Virtual environment creation** — Creates `.venv/` and offline-installs dependencies from `vendor/wheels/`
4. **Galaxy collections installation** — Installs from `vendor/collections/` to `collections/`

> **No host impact**: All files are contained within the `ansible/` directory. No impact on host Python, pip, or global packages.
>
> | Output | Contents |
> |---|---|
> | `.venv/` | Python virtual environment |
> | `.local/bin/` | uv binary (only if host lacks uv) |
> | `.local/python/` | Python 3.12 (only if host lacks 3.12) |
> | `collections/` | Ansible Galaxy collections |

### Verifying Setup

```bash
uv run ansible-playbook --version
```

Success looks like Python 3.12.x and ansible-core 2.21.x:

```
ansible-playbook [core 2.21.4]
  ...
  python version = 3.12.14 (...)
```

### Troubleshooting

| Symptom | Cause | Solution |
|---|---|---|
| `uv: command not found` | uv not installed | Re-run `bash scripts/setup-env.sh` (auto-installs from vendor/uv/) |
| `cp314` wheel not found error | Host Python 3.14 is being used | Check if standalone build exists in `vendor/python/`. If not, re-download with `bash scripts/download-deps.sh python` |
| `uv sync` resolution error | `requires-python` in pyproject.toml doesn't match host Python version | Verify `requires-python = ">=3.12,<3.13"` |
| Galaxy collection not found | `vendor/collections/` is empty | Re-download with `bash scripts/download-deps.sh collections` |

---

## 4. Inventory Preparation

### Creating an Inventory

```bash
cp -r inventory/sample inventory/myenv
```

### Files to Edit

| File | Contents | Impact |
|---|---|---|
| `group_vars/all/cluster.yml` | Cluster-wide settings (kubeconfig, StorageClass, Proxy, Operator channels, DB passwords) | Referenced by multiple roles. Changes have wide impact |
| `group_vars/all/components.yml` | Solution-specific settings (LVM devices, model names, Keycloak users, enable/disable flags) | Used only within individual roles |
| `group_vars/all/vault.yml` | Secrets such as HuggingFace tokens | Required for LLM downloads |
| `hosts.yml` | Ansible host definition | Usually no changes needed (fixed to localhost) |

> **Note**: The default inventory in `ansible.cfg` is `inventory/sample`. Always specify `-i inventory/myenv`. Forgetting this will deploy with sample settings.

### Minimum Required Edits

1. **vault.yml** — Set the HuggingFace token (required for LLM downloads):
   ```bash
   vi inventory/myenv/group_vars/all/vault.yml
   ```
   ```yaml
   hf_token: "hf_xxxxxxxxxxxxxxxxxxxxx"
   ```
   > Create a "Read" permission token at the HuggingFace [Access Tokens](https://huggingface.co/settings/tokens) page.

2. **cluster.yml** — Set the kubeconfig path (environment-dependent):
   ```bash
   vi inventory/myenv/group_vars/all/cluster.yml
   ```
   No edit needed if the `KUBECONFIG` environment variable is already set.

3. **components.yml** — Adjust LVM device paths and model settings:
   ```bash
   vi inventory/myenv/group_vars/all/components.yml
   ```
   Set `lvm_device_paths` to match your environment's actual devices.

---

## 5. Parameter Reference

### 5.1 cluster.yml — Cluster-Wide Settings

These parameters are referenced by multiple roles. Changes have wide impact.

#### Path Resolution

| Variable | Description | Default | Customer Change |
|---|---|---|---|
| `ansible_root` | Absolute path to the Ansible project root (`ansible/`) | Auto-calculated from `playbook_dir` | **No change needed** — Resolves correctly whether run from `site.yml` or `playbooks/*.yml` |

> `ansible_root` is the base path for referencing files from roles and templates. Since `playbook_dir` varies by entry point, use `ansible_root` instead of `playbook_dir` directly.

#### Kubeconfig

| Variable | Description | Default | Customer Change |
|---|---|---|---|
| `k8s_kubeconfig` | Path to kubeconfig file | Not set (falls back to `KUBECONFIG` env var → `~/.kube/config`) | **Environment-dependent** — Not needed if `KUBECONFIG` is set after `oc login`. Set only if you want to specify explicitly |

#### Storage

| Variable | Description | Default | Customer Change |
|---|---|---|---|
| `storage_class` | StorageClass name used for all PVCs | `lvms-vg1` | **As needed** — Change to `gp3-csi` etc. if not using LVM (e.g., direct EBS). Specifying `lvms-*` automatically enables `install_lvm` |

#### Proxy

| Variable | Description | Default | Customer Change |
|---|---|---|---|
| `cluster_proxy.http_proxy` | HTTP proxy URL | `""` (empty = skip) | **Proxy environments only** |
| `cluster_proxy.https_proxy` | HTTPS proxy URL | `""` | **Proxy environments only** |
| `cluster_proxy.no_proxy` | Proxy exclusion list | `"localhost,127.0.0.1,.cluster.local,.svc"` | **Proxy environments only** — Be careful not to include `.apps.<cluster_domain>` as it affects MaaS Gateway communication |

> **Proxy note**: When set, the `rhoai` role injects these as environment variables into `kube-auth-proxy`, `rhods-dashboard`, and `maas-ui`. Skipped when empty.

#### Operator Channels

| Variable | Description | Default | Customer Change |
|---|---|---|---|
| `operator_channels.lvm` | LVM Operator channel | `stable-4.22` | Match to OpenShift version |
| `operator_channels.metallb` | MetalLB channel | `stable` | Usually no change needed |
| `operator_channels.cert_manager` | cert-manager channel | `stable-v1` | Usually no change needed |
| `operator_channels.servicemesh` | ServiceMesh channel | `stable` | Usually no change needed |
| `operator_channels.nfd` | NFD channel | `stable` | Usually no change needed |
| `operator_channels.gpu` | GPU Operator channel | `stable` | Usually no change needed |
| `operator_channels.rhcl` | RHCL (Kuadrant/Authorino) channel | `stable` | Usually no change needed |
| `operator_channels.kueue` | Kueue channel | `stable-v1.4` | Usually no change needed |
| `operator_channels.jobset` | JobSet channel | `stable-v1.0` | Usually no change needed |
| `operator_channels.leaderworkerset` | LeaderWorkerSet channel | `stable-v1.0` | Usually no change needed |
| `operator_channels.coo` | Cluster Observability Operator channel | `stable` | Usually no change needed |
| `operator_channels.opentelemetry` | OpenTelemetry channel | `stable` | Usually no change needed |
| `operator_channels.odf` | ODF channel | `stable-4.22` | Match to OpenShift version |
| `operator_channels.rhoai` | RHOAI channel | `stable-3.5` | Match to RHOAI version |
| `operator_channels.keycloak` | Keycloak (RHBK) channel | `stable-v26` | Usually no change needed |
| `operator_channels.group_sync` | Group Sync Operator channel | `alpha` | Usually no change needed |

> **Operator channel note**: When changing the OpenShift major/minor version, update the `lvm` and `odf` channels accordingly.

#### DB Passwords

| Variable | Description | Default | Customer Change |
|---|---|---|---|
| `maas_db_password` | MaaS PostgreSQL password | `""` (empty = auto-generated) | **Usually not needed** — Left empty, a password is auto-generated on first deploy and saved to `.generated-passwords.yml` |
| `ogx_db_password` | OGX PostgreSQL password | `""` | Same as above |
| `keycloak_db_password` | Keycloak PostgreSQL password | `""` | Same as above |

> **Auto-generated passwords**: On first deploy, the `preflight` role generates 20-character random passwords and saves them to `ansible/.generated-passwords.yml`. On subsequent runs, passwords are loaded from this file and not regenerated.

### 5.2 components.yml — Solution-Specific Settings

These parameters are used only within their respective roles.

#### LVM Settings

| Variable | Description | Default | Customer Change |
|---|---|---|---|
| `lvm_device_paths` | Block device paths for LVMS VolumeGroup (list) | `["/dev/disk/by-path/pci-0000:34:00.0-nvme-1"]` | **Required** — Devices vary by environment |

Three path formats are available:

| Format | Example | Stability | Recommended |
|---|---|---|---|
| Device name | `/dev/nvme1n1` | Low (numbers may change on reboot) | △ |
| PCI path | `/dev/disk/by-path/pci-0000:34:00.0-nvme-1` | High (PCI slot is fixed) | **◎ Recommended** |
| Device ID | `/dev/disk/by-id/nvme-Amazon_EC2_...` | High (instance-specific) | ○ |

**Device identification commands**:

```bash
# Device list (identify purpose by size and model)
oc debug node/<node-name> -- chroot /host lsblk -d -o NAME,SIZE,TYPE,MODEL

# Device name to PCI path mapping
oc debug node/<node-name> -- chroot /host bash -c \
  'for d in $(lsblk -dn -o NAME | grep nvme); do \
    BP=$(find /dev/disk/by-path -lname "*/$d" -printf "%f" 2>/dev/null); \
    printf "%-12s %-8s %-40s %s\n" "/dev/$d" \
      "$(lsblk -dn -o SIZE /dev/$d)" \
      "$(lsblk -dn -o MODEL /dev/$d)" \
      "${BP:-(none)}"; \
  done'
```

Example output:

```
/dev/nvme0n1  300G    Amazon Elastic Block Store               pci-0000:00:04.0-nvme-1
/dev/nvme1n1 419.1G   Amazon EC2 NVMe Instance Storage         pci-0000:34:00.0-nvme-1
/dev/nvme2n1  200G    Amazon Elastic Block Store               pci-0000:23:00.0-nvme-1
```

> **EC2 Instance Storage note**: Device numbers may change on instance reboot (e.g., nvme14n1 → nvme1n1). **Always use the PCI path format.** LVMS resolves symlinks before using devices, so by-path paths work correctly.

#### MetalLB Settings

| Variable | Description | Default | Customer Change |
|---|---|---|---|
| `metallb_ip_range` | IP address range for MetalLB | `""` (empty = auto-calculated from node IP) | **Usually not needed** — When empty, uses the same address as the node's InternalIP. Set only if a custom range is needed (e.g., `"192.168.1.100-192.168.1.110"`) |

#### LLM Model Settings

Model management is centralized in the `models` variable. Default values are defined in `roles/llm_serving/defaults/main.yml` under `model_defaults`. Only fields you want to override need to be specified in `models`.

```yaml
models:
  qwen3-06b:
    hf_repo: Qwen/Qwen3-0.6B       # (required) HuggingFace repository name
    state: present                   # present=deploy, absent=remove
    # The following have defaults and can be omitted
    # vllm_image: vllm/vllm-openai:v0.28.0
    # tool_call_parser: qwen3_coder
    # reasoning_parser: ""
    # chat_template_configmap: qwen3-chat-template
    # gpu_count: 1
    # vllm_extra_args: ["--max-model-len=4096", "--enable-auto-tool-choice"]
    # purge: false                   # true to also delete model data on PVC
```

| Field | Description | Default |
|---|---|---|
| `hf_repo` | HuggingFace repository name (required) | — |
| `state` | `present` to deploy, `absent` to remove | `present` |
| `namespace` | Target namespace for deployment | `llm-serving` |
| `vllm_image` | vLLM container image | `vllm/vllm-openai:v0.28.0` |
| `tool_call_parser` | `--tool-call-parser` | `qwen3_coder` |
| `reasoning_parser` | `--reasoning-parser` (omitted if empty) | `""` |
| `chat_template_configmap` | Chat template ConfigMap name | `qwen3-chat-template` |
| `gpu_count` | Number of GPUs requested | `1` |
| `vllm_extra_args` | Additional vLLM arguments (list) | `["--max-model-len=4096", "--enable-auto-tool-choice"]` |
| `purge` | Whether to delete PVC cache when `state: absent` | `false` |

> **The dict key becomes the model name.** It is used as K8s resource names, so it must be RFC 1123 compliant (lowercase alphanumeric, hyphens, and dots only).

#### Behavior by State

| State | Behavior |
|---|---|
| `present` | Download model (skip if cached) → Create LLMInferenceService + MaaSModelRef → Include in AuthPolicy/Subscription |
| `absent` | Delete LLMInferenceService + MaaSModelRef (release GPU) → Exclude from AuthPolicy/Subscription. PVC cache is retained |
| `absent` + `purge: true` | In addition to above, also delete model data on PVC |

> **Processing order**: When running site.yml, `absent` models are processed first (to release GPUs). Then `present` models are deployed.
>
> **GPU capacity check**: If the total `gpu_count` of `present` models exceeds the cluster's GPU count, deployment stops with an error before proceeding.

#### Keycloak Settings

| Variable | Description | Default | Customer Change |
|---|---|---|---|
| `keycloak_namespace` | Namespace for Keycloak deployment | `keycloak` | Usually no change needed |
| `keycloak_realm` | Keycloak realm name | `maas` | Usually no change needed |
| `keycloak_users` | List of users to create | admin1 + testuser1 | **As needed** — Adjust usernames, emails, and group assignments for your environment |
| `keycloak_groups` | MaaS access control group definitions | maas-admins + maas-qwen3-06b-users | **Required when changing models** — Group names follow `maas-<model-name>-users` format |

> **Naming convention**: Group names are used as K8s resource names and must be RFC 1123 compliant. The `keycloak_groups` dict keys, `keycloak_users[].groups` values, and `keycloak_groups[].models` values must be consistent.

> **Access control**: MaaSAuthPolicy (access permission) and MaaSSubscription (quota) are auto-generated from each group in `keycloak_groups`. `keycloak_groups[].models` references the dict keys in the `models` variable. Only `state: present` models are included in policies. `priority` is the request contention priority (higher = more priority), and `quota_tokens_24h` is the 24-hour token limit. See [docs/operations.md](docs/operations.md#access-control-and-quotas) for details.

#### Role Enable/Disable Flags

| Variable | Target Role | Default | Customer Change |
|---|---|---|---|
| `install_lvm` | LVM Operator | `true` | Set to `false` if `storage_class` is not `lvms-*` |
| `install_metallb` | MetalLB | `true` | Set to `false` for bare metal or when LoadBalancer is not needed |
| `install_cert_manager` | cert-manager | `true` | Usually no change needed (RHOAI dependency) |
| `install_servicemesh` | ServiceMesh | `true` | Usually no change needed (RHOAI dependency) |
| `install_nfd` | Node Feature Discovery | `true` | Usually no change needed (RHOAI/GPU dependency) |
| `install_gpu_operator` | GPU Operator | `true` | Set to `false` for environments without GPU |
| `install_rhcl` | RHCL (Kuadrant/Authorino) | `true` | Usually no change needed (RHOAI dependency) |
| `install_kueue` | Kueue | `true` | Usually no change needed (RHOAI dependency) |
| `install_jobset` | JobSet | `true` | Usually no change needed (RHOAI dependency) |
| `install_leaderworkerset` | LeaderWorkerSet | `true` | Usually no change needed (RHOAI dependency) |
| `install_odf` | ODF (NooBaa) | `true` | Set to `false` if not using MLflow / OGX |
| `install_rhoai` | OpenShift AI | `true` | Usually no change needed (core component) |
| `install_keycloak` | Keycloak | `true` | Set to `false` if auth is not needed (note: MaaS policies will also be disabled) |
| `install_llm_serving` | LLM Serving | `true` | Set to `false` if LLM inference is not needed |
| `install_maas_resources` | MaaS resources | `true` | Usually no change needed (llm_serving dependency) |
| `install_mlflow` | MLflow | `true` | Set to `false` if experiment tracking is not needed |
| `install_ogx` | OGX Server | `true` | Set to `false` if OGX is not needed |
| `install_guardrails` | NeMo Guardrails | `true` | Set to `false` if Guardrails is not needed |
| `install_observability` | Observability | `true` | Set to `false` if monitoring is not needed |

> **Automatic dependency resolution**: Enabling a parent role automatically enables its dependencies. For example, setting `install_rhoai: true` automatically enables `cert_manager`, `servicemesh`, `rhcl`, `kueue`, `jobset`, `leaderworkerset`, `nfd`, and `gpu_operator`. Additionally, `install_lvm` is automatically enabled when `storage_class: lvms-*`.

#### Integration Enable/Disable Flags

| Variable | Target | Default | Customer Change |
|---|---|---|---|
| `integrate_keycloak_oauth` | Keycloak → OpenShift OAuth IdP registration | `true` | Set to `false` if not using Keycloak for OpenShift login |
| `integrate_keycloak_maas` | Keycloak → MaaS AuthPolicy/Subscription generation | `true` | Usually no change needed |
| `integrate_rhoai_oidc` | AITenant OIDC configuration | `true` | Usually no change needed |

### 5.3 vault.yml — Secrets

| Variable | Description | Customer Change |
|---|---|---|
| `hf_token` | HuggingFace access token | **Required** — Obtain from https://huggingface.co/settings/tokens. Needed for model downloads |

---

## 6. Deployment Flow

### 6.1 Role List and Execution Order

| # | Phase | Role Name | Tags | Flag | Deployed Resources | Dependencies |
|---|---|---|---|---|---|---|
| 1 | Preflight | `preflight` | `always` | — | oc login check, password generation, variable validation, dependency resolution | — |
| 2 | Infrastructure | `lvm` | `infra, lvm` | `install_lvm` | LVM Operator, LVMCluster, default StorageClass setup | — |
| 3 | Infrastructure | `metallb` | `infra, metallb` | `install_metallb` | MetalLB Operator, IPAddressPool, L2Advertisement | — |
| 4 | Dependencies | `cert_manager` | `deps, cert_manager` | `install_cert_manager` | cert-manager Operator | — |
| 5 | Dependencies | `servicemesh` | `deps, servicemesh` | `install_servicemesh` | ServiceMesh Operator | — |
| 6 | Dependencies | `nfd` | `deps, nfd` | `install_nfd` | NFD Operator, NodeFeatureDiscovery CR | — |
| 7 | Dependencies | `gpu_operator` | `deps, gpu` | `install_gpu_operator` | GPU Operator, ClusterPolicy | — |
| 8 | Dependencies | `rhcl` | `deps, rhcl` | `install_rhcl` | RHCL Operator, Kuadrant CR | — |
| 9 | Dependencies | `kueue` | `deps, kueue` | `install_kueue` | Kueue Operator | — |
| 10 | Dependencies | `jobset` | `deps, jobset` | `install_jobset` | JobSet Operator, JobSetOperator CR | — |
| 11 | Dependencies | `leaderworkerset` | `deps, leaderworkerset` | `install_leaderworkerset` | LeaderWorkerSet Operator | — |
| 12 | Storage | `odf` | `storage, odf` | `install_odf` | ODF Operator, NooBaa CR | — |
| 13 | Platform | `rhoai` | `platform, rhoai` | `install_rhoai` | RHOAI Operator, DataScienceCluster, MaaS PostgreSQL, MaaS Gateway, HardwareProfile, Authorino TLS, NetworkPolicy | cert_manager, servicemesh, rhcl, kueue, jobset, leaderworkerset, nfd, gpu_operator |
| 14 | Platform | `keycloak` | `platform, keycloak` | `install_keycloak` | RHBK Operator, PostgreSQL, Keycloak CR, Realm Import, Group Sync Operator | lvm |
| 15 | Integration | `integration_keycloak_oauth` | `integration, keycloak_oauth` | `integrate_keycloak_oauth` | OpenShift OAuth IdP registration, keycloak-ca ConfigMap, OIDC client secret | keycloak |
| 16 | Integration | `integration_keycloak_maas` | `integration, keycloak_maas` | `integrate_keycloak_maas` | Keycloak user/group sync, MaaS AuthPolicy, MaaS Subscription | keycloak |
| 17 | Integration | `integration_rhoai_oidc` | `integration, rhoai_oidc` | `integrate_rhoai_oidc` | AITenant OIDC patch, maas-oidc-client-secret | keycloak, rhoai |
| 18 | Workload | `llm_serving` | `workload, llm` | `install_llm_serving` | llm-serving Namespace, hf-token Secret, PVC, ClusterStorageContainer, ChatTemplate ConfigMap, Download Job, LLMInferenceService (Ready wait up to 20 min) | rhoai |
| 19 | Workload | `maas_resources` | `workload, maas` | `install_maas_resources` | Dashboard RBAC | rhoai |
| 20 | Workload | `mlflow` | `workload, mlflow` | `install_mlflow` | mlflow-workspace Namespace, OBC, MLflow CR | rhoai, odf |
| 21 | Workload | `ogx` | `workload, ogx` | `install_ogx` | OGX PostgreSQL, OGXServer CR, vLLM connection Secret | rhoai, odf |
| 22 | Workload | `guardrails` | `workload, guardrails` | `install_guardrails` | NeMo Guardrails ConfigMap, NemoGuardrails CR | llm_serving |
| 23 | Workload | `observability` | `workload, observability` | `install_observability` | COO Operator, OpenTelemetry Operator, Perses, PrometheusRule, PodMonitor, ScrapeConfig, Dashboard | rhoai |
| 24 | Post-deploy | — | `post_deploy, opencode` | — | OpenCode configuration (API Key issuance + opencode.json generation) | — |

### 6.2 Role Scope Details

#### `preflight` (always)

| Process | Details |
|---|---|
| oc login check | Verify connection with `oc whoami` |
| cluster_domain retrieval | Get `.spec.domain` from `ingresses.config.openshift.io/cluster` |
| Password generation | Auto-generate if `maas_db_password`, `ogx_db_password`, `keycloak_db_password` are empty |
| Password persistence | Save to `.generated-passwords.yml` (loaded on re-runs) |
| LVM auto-enable | Auto-set `install_lvm` to true if `storage_class` is `lvms-*` |
| models validation | Verify `models` variable is defined, each key is RFC 1123 compliant, and each model has `hf_repo` defined |
| RFC 1123 validation | Verify `models` keys and `keycloak_groups` keys are valid K8s names |
| Required variable validation | `storage_class`, `lvm_device_paths`. LLM Serving only: `hf_token`, `llm_storage_initializer_image` |
| Dependency resolution | Auto-enable dependency roles when parent role is enabled |

#### `lvm`

| Created Resource | Namespace |
|---|---|
| Namespace `openshift-storage` | — |
| Subscription `lvms-operator` | openshift-lvm-storage |
| LVMCluster `lvmcluster` | openshift-lvm-storage |
| Set StorageClass `lvms-vg1` as default | — |

**Wait**: LVMCluster `status.state == Ready`
**Variables used**: `lvm_device_paths`, `operator_channels.lvm`

#### `metallb`

| Created Resource | Namespace |
|---|---|
| Subscription `metallb-operator` | metallb-system |
| MetalLB CR | metallb-system |
| IPAddressPool `default-pool` | metallb-system |
| L2Advertisement `default-l2` | metallb-system |

**Wait**: MetalLB speaker Pod is Running
**Variables used**: `metallb_ip_range`, `operator_channels.metallb`

#### `rhoai`

| Created Resource | Namespace |
|---|---|
| Subscription `rhods-operator` | redhat-ods-operator |
| PVC `maas-postgres-data` | redhat-ods-applications |
| Deployment `maas-postgres` | redhat-ods-applications |
| Secret `maas-db-config` | redhat-ods-applications, redhat-ai-gateway-infra |
| DataScienceCluster `default-dsc` | — |
| HardwareProfile `nvidia-gpu-1` | redhat-ods-applications |
| Gateway `maas-default-gateway` | openshift-ingress |
| Namespace `llm-serving` | — |
| NetworkPolicy `maas-authorino-allow-rhcl` | redhat-ai-gateway-infra |
| Gateway ConfigMap `maas-gateway-options` | openshift-ingress |

**Wait**: Gateway `Programmed`, AITenant exists, rhods-dashboard Deployment Ready
**Variables used**: `storage_class`, `maas_db_password`, `operator_channels.rhoai`, `cluster_proxy.*`

#### `keycloak`

| Created Resource | Namespace |
|---|---|
| Namespace `keycloak` | — |
| Subscription `rhbk-operator` | keycloak |
| StatefulSet `postgres` (PostgreSQL) | keycloak |
| Keycloak CR | keycloak |
| KeycloakRealmImport `maas-realm` | keycloak |
| Route `keycloak` (reencrypt) | keycloak |
| Subscription `group-sync-operator` | group-sync-operator |
| GroupSync CR | group-sync-operator |

**Wait**: Keycloak Ready, realm import complete
**Variables used**: `keycloak_namespace`, `keycloak_realm`, `keycloak_db_password`, `keycloak_users`, `keycloak_groups`, `operator_channels.keycloak`, `operator_channels.group_sync`

#### `llm_serving`

| Created Resource | Namespace |
|---|---|
| Namespace `llm-serving` | — |
| Secret `hf-token` | llm-serving |
| PVC `hf-model-cache` (200Gi) | llm-serving |
| ClusterStorageContainer `default` | — (Cluster-scoped) |
| ConfigMap `<chat_template_configmap>` | llm-serving |
| Job `download-<model_name>` (per model) | llm-serving |
| LLMInferenceService `<model_name>` (per model) | llm-serving |
| MaaSModelRef `<model_name>` (per model) | llm-serving |

**Processing order**: Delete `state: absent` models first (release GPU) → Deploy `state: present` models
**Wait**: Download Job complete, LLMInferenceService Ready (up to 20 min per model)
**Variables used**: `models`, `model_defaults`, `llm_storage_initializer_image`, `hf_token`, `storage_class`

---

## 7. Running the Deployment

```bash
# Full deployment
uv run ansible-playbook site.yml -i inventory/myenv

# Specific phase only
uv run ansible-playbook site.yml -i inventory/myenv --tags platform
uv run ansible-playbook site.yml -i inventory/myenv --tags workload

# Skip components
uv run ansible-playbook site.yml -i inventory/myenv -e install_ogx=false -e install_guardrails=false

# Available tags
# always, infra, lvm, metallb, deps, cert_manager, servicemesh, nfd, gpu,
# rhcl, kueue, jobset, leaderworkerset, storage, odf, platform, rhoai,
# keycloak, integration, keycloak_oauth, keycloak_maas, rhoai_oidc,
# workload, llm, maas, mlflow, ogx, guardrails, observability, post_deploy, opencode
```

### Common Operator Installation Pattern

All Operators are installed using `roles/common/tasks/install_operator.yml` with the following steps:

1. Create Namespace
2. Create OperatorGroup (only if not exists)
3. Create Subscription
4. Wait for InstalledCSV to appear (up to 10 min)
5. Wait for CSV `Succeeded` (up to 10 min)

---

## 8. Verification

```bash
uv run ansible-playbook playbooks/verify.yml -i inventory/myenv
```

### Check Items

| Category | Check |
|---|---|
| Infrastructure | LVM Operator CSV Succeeded, LVMCluster Ready, LVMCluster VolumeGroupsReady, MetalLB speaker Running |
| Dependencies | 8 Operators (cert-manager, ServiceMesh, NFD, GPU, RHCL, Kueue, JobSet, LeaderWorkerSet) CSV Succeeded |
| Storage | NooBaa Ready |
| Platform | RHOAI Operator CSV, DataScienceCluster Ready, MaaS Gateway Programmed, RHOAI Dashboard Ready, Keycloak Ready |
| Integration | Keycloak user/group assignment, OAuth IdP registration, AITenant exists |
| Workloads | LLMInferenceService Ready, MaaS API healthy, MaaSModelRef exists, MaaS Subscription Active, **API Key issuance test**, MLflow Available, OGX Ready, Guardrails Ready |
| Health | Route HostAlreadyClaimed detection, abnormal Pod detection in managed namespaces |
| Namespaces | Existence check for all managed namespaces |

An environment summary is displayed on verify completion (Console URL, Dashboard URL, Keycloak admin info, MaaS endpoint, user passwords, etc.).

### Post-Deployment Access Information

#### Service URLs

After deployment, each service is accessible at `https://<service>.<cluster_domain>`.

| Service | URL Format | Purpose |
|---|---|---|
| OpenShift Console | `https://console-openshift-console.apps.<cluster_domain>` | Cluster management |
| RHOAI Dashboard | `https://rhods-dashboard-redhat-ods-applications.apps.<cluster_domain>` | AI platform management |
| Keycloak Admin Console | `https://keycloak-keycloak.apps.<cluster_domain>` | IdP management |
| MaaS Gateway | `https://maas.apps.<cluster_domain>` | LLM inference API |
| MLflow | `https://rh-ai.apps.<cluster_domain>/mlflow` | Experiment tracking |

Commands to check URLs:

```bash
# List all Routes
oc get route --all-namespaces -o custom-columns='SERVICE:.metadata.name,URL:.spec.host'

# Individual checks
oc get route console -n openshift-console -o jsonpath='https://{.spec.host}'
oc get route rhods-dashboard -n redhat-ods-applications -o jsonpath='https://{.spec.host}'
oc get route keycloak -n keycloak -o jsonpath='https://{.spec.host}'
oc get route maas-gateway -n openshift-ingress -o jsonpath='https://{.spec.host}'
```

#### Account Information

| Account | Location | Description |
|---|---|---|
| OpenShift cluster-admin | Specified during `oc login` | Cluster administrator. Check with `oc whoami` |
| Keycloak admin | Secret `keycloak-initial-admin` (namespace: keycloak) | Login for Keycloak admin console |
| MaaS users (admin1, testuser1, etc.) | `.credentials/<username>.password` | For MaaS Dashboard / API Key issuance |
| MaaS API Key | `.vllm-token` | Bearer token for LLM inference requests |

Commands to check credentials:

```bash
# Keycloak admin
oc get secret keycloak-initial-admin -n keycloak \
  -o jsonpath='username: {.data.username} / password: {.data.password}' | \
  xargs -I{} sh -c 'echo {} | sed "s/username: //" | cut -d/ -f1 | base64 -d; echo -n " / "; echo {} | sed "s/.*password: //" | base64 -d; echo'

# MaaS user passwords
cat .credentials/admin1.password
cat .credentials/testuser1.password

# MaaS API Key
cat .vllm-token
```

#### Credential File Layout

```
rhoai-3.5-ansible/
├── .credentials/              ← Keycloak user passwords (gitignored)
│   ├── admin1.password
│   └── testuser1.password
├── .vllm-token                ← MaaS API Key (gitignored)
├── .generated-passwords.yml   ← DB passwords (gitignored)
```

> **Note**: These files are gitignored. If you cleanup and redeploy, the API Key becomes invalid, but if `.credentials/` password files remain, users are recreated with the same passwords.

---

## 9. Settings That Must Be Changed for Customer Environments

The following parameters **must be reviewed and changed** for each environment.

### Required Changes

| Parameter | File | Description |
|---|---|---|
| `lvm_device_paths` | components.yml | Device paths for LVM. Identify using the device check commands above |
| `hf_token` | vault.yml | HuggingFace token. Required for model downloads |

### Change as Needed

| Parameter | File | Condition |
|---|---|---|
| `k8s_kubeconfig` | cluster.yml | If `KUBECONFIG` environment variable is not set |
| `storage_class` | cluster.yml | If using storage other than LVM (e.g., `gp3-csi`) |
| `metallb_ip_range` | components.yml | If auto-calculation is unsuitable |
| `cluster_proxy.*` | cluster.yml | If in a proxy environment |
| `operator_channels.lvm` | cluster.yml | If OpenShift version is not 4.22 |
| `operator_channels.odf` | cluster.yml | Same as above |
| `install_gpu_operator` | components.yml | For environments without GPU (set to `false`) |
| `keycloak_users` | components.yml | Change to actual environment users |
| `models` | components.yml | When using different models (define hf_repo, state, etc.) |

### No Changes Needed (Automatic)

| Parameter | Reason |
|---|---|
| `maas_db_password` / `ogx_db_password` / `keycloak_db_password` | Auto-generated when left empty |
| Dependency Operator flags | Auto-enabled by `install_rhoai: true` |
| `install_lvm` | Auto-enabled by `storage_class: lvms-*` |

---

## 10. Deploying in Disconnected Environments

### Overview

In disconnected (no internet) environments, the following preparation is required:

1. **Python packages**: Pre-download to `vendor/wheels/`
2. **Ansible Galaxy collections**: Pre-download to `vendor/collections/`
3. **Operator catalogs**: Mirror OperatorHub for OpenShift
4. **Container images**: Place images like vLLM in a mirror registry
5. **HuggingFace models**: Pre-download model files

### Python / Ansible Offline Installation

```bash
# Run in online environment
cd ansible
bash scripts/download-deps.sh

# Run in disconnected environment
cd ansible
bash scripts/setup-env.sh
# Use uv run for commands from here (no activate needed)
uv run ansible-playbook site.yml -i inventory/myenv
```

### Pre-downloading HuggingFace Models

In disconnected environments, models cannot be downloaded from HuggingFace, so they must be pre-downloaded and copied to the PVC.

```bash
# In online environment
pip install huggingface_hub
huggingface-cli download Qwen/Qwen3-0.6B --local-dir ./qwen3-06b

# Copy model to PVC in disconnected environment
oc rsync ./qwen3-06b/ <pod-name>:/models/qwen3-06b/ -n llm-serving
```

### Operator Catalog Mirroring

Use `oc-mirror` to mirror the required Operators. See "6.1 Role List and Execution Order" for the list of required Operators.

### Container Images

The following images are needed in the mirror registry:

- `vllm/vllm-openai:v0.28.0` (LLM Serving)
- `quay.io/modh/kserve-storage-initializer:rhoai-2.22` (KServe)
- `registry.access.redhat.com/ubi9/python-311:latest` (Model download Job)
- `registry.access.redhat.com/ubi9/ubi-minimal:latest` (PVC check)

---

## 11. Directory Structure

```
ansible/
├── site.yml                      # Main playbook
├── ansible.cfg                   # Ansible configuration (collections_paths, default inventory, etc.)
├── pyproject.toml                # Python dependency definitions (uv compatible, find-links = vendor/wheels)
├── requirements.yml              # Ansible Galaxy collections
├── inventory/
│   ├── sample/                   # Template (copy to use)
│   │   └── group_vars/all/
│   │       ├── cluster.yml.sample
│   │       ├── components.yml.sample
│   │       └── vault.yml.sample
│   └── myenv/                    # Environment-specific settings (gitignored)
├── playbooks/
│   ├── uninstall.yml             # Uninstall (reverse order of site.yml)
│   ├── verify.yml                # Deployment verification + environment summary
│   ├── manage_maas_access.yml    # MaaS access management (users + policies)
│   ├── llm_add_model.yml         # Add model (deploy + MaaS + Keycloak)
│   └── maas_create_apikey.yml    # API Key issuance
├── scripts/
│   ├── cleanup-all.sh            # Delete all resources
│   ├── setup-opencode.sh         # OpenCode config generation (API Key + opencode.json)
│   ├── download-deps.sh          # Download dependencies for disconnected use
│   └── setup-env.sh              # Disconnected environment setup
├── tasks/
│   ├── resolve_dependencies.yml  # Automatic dependency resolution
│   └── _resolve_one.yml
├── roles/
│   ├── preflight/                # Pre-checks + fact gathering
│   │   └── tasks/
│   │       ├── main.yml          # Full preflight
│   │       └── light.yml         # Lightweight preflight (for operational playbooks)
│   ├── common/                   # Shared Operator install/wait tasks
│   │   └── tasks/
│   │       ├── install_operator.yml
│   │       ├── uninstall_operator.yml
│   │       ├── create_if_absent.yml
│   │       ├── wait_for_crd.yml
│   │       ├── wait_for_field.yml
│   │       └── wait_for_resource.yml
│   ├── lvm/                      # LVM Operator
│   ├── metallb/                  # MetalLB
│   ├── cert_manager/             # cert-manager
│   ├── servicemesh/              # ServiceMesh 3
│   ├── nfd/                      # Node Feature Discovery
│   ├── gpu_operator/             # GPU Operator
│   ├── rhcl/                     # RHCL (Kuadrant/Authorino)
│   ├── kueue/                    # Kueue
│   ├── jobset/                   # JobSet
│   ├── leaderworkerset/          # LeaderWorkerSet
│   ├── odf/                      # ODF (NooBaa)
│   ├── rhoai/                    # OpenShift AI
│   ├── keycloak/                 # Keycloak + Group Sync
│   ├── integration_keycloak_oauth/   # Keycloak → OpenShift OAuth
│   ├── integration_keycloak_maas/    # Keycloak → MaaS policies
│   ├── integration_rhoai_oidc/       # AITenant OIDC
│   ├── llm_serving/              # LLM model deployment
│   ├── maas_resources/           # MaaS Dashboard RBAC
│   ├── mlflow/                   # MLflow
│   ├── ogx/                      # OGX Server
│   ├── guardrails/               # NeMo Guardrails
│   └── observability/            # Monitoring / Dashboards
├── docs/
│   ├── operations.md             # Operations guide (add models, user management, etc.)
│   └── troubleshooting.md        # Troubleshooting
├── vendor/                       # Disconnected dependencies (gitignored)
│   ├── wheels/                   # Python wheels
│   └── collections/              # Ansible Galaxy collections
└── .venv/                        # Python virtual environment (gitignored)
```

---

## 12. Uninstall

### 12.1 Full Uninstall

```bash
uv run ansible-playbook playbooks/uninstall.yml -i inventory/myenv
```

Removes all components in reverse installation order. Each Operator is safely removed following official uninstall procedures: CR → Operator → Namespace.

### 12.2 Uninstalling Specific Components

```bash
# RHOAI + workloads only
uv run ansible-playbook playbooks/uninstall.yml -i inventory/myenv --tags platform,workload

# Specific roles only (can run even if disabled in inventory)
uv run ansible-playbook playbooks/uninstall.yml -i inventory/myenv --tags deps,gpu -e uninstall_gpu_operator=true
```

### 12.3 Available Tags

| Tag | Target |
|---|---|
| `infra, lvm` | LVM Operator |
| `infra, metallb` | MetalLB |
| `deps, cert_manager` | cert-manager |
| `deps, servicemesh` | ServiceMesh |
| `deps, nfd` | Node Feature Discovery |
| `deps, gpu` | GPU Operator |
| `deps, rhcl` | RHCL (Kuadrant/Authorino) |
| `deps, kueue` | Kueue |
| `deps, jobset` | JobSet |
| `deps, leaderworkerset` | LeaderWorkerSet |
| `storage, odf` | ODF (NooBaa) |
| `platform, rhoai` | OpenShift AI |
| `platform, keycloak` | Keycloak |
| `integration, keycloak_oauth` | Keycloak → OpenShift OAuth |
| `integration, keycloak_maas` | Keycloak → MaaS Policies |
| `integration, rhoai_oidc` | AITenant OIDC |
| `workload, llm` | LLM Serving |
| `workload, maas` | MaaS Resources |
| `workload, mlflow` | MLflow |
| `workload, ogx` | OGX Server |
| `workload, guardrails` | NeMo Guardrails |
| `workload, observability` | Observability |

### 12.4 Uninstall Variables

Individual control is available via `uninstall_<role>` variables. When not specified, the value of `install_<role>` is used as fallback.

```bash
# Uninstall only RHOAI (ignores other install_* even if true)
uv run ansible-playbook playbooks/uninstall.yml -i inventory/myenv \
  --tags platform,rhoai -e uninstall_rhoai=true
```

### 12.5 Retrying on Errors

If a failure occurs mid-process, a retry command is shown in the error message:

```
TASK [Fail if PVCs using lvms-vg1 still exist] ********************************
fatal: [localhost]: FAILED! =>
  msg: |-
    LVM Operator uninstall failed.
    Retry: ansible-playbook playbooks/uninstall.yml -i inventory/<env> --tags infra,lvm -e uninstall_lvm=true
```

Replace `inventory/<env>` with your actual inventory path and run the command.

### 12.6 Uninstall Behavior

Each Operator is removed following official documentation procedures:

| Role | Uninstall Method |
|---|---|
| **RHOAI** | Official ConfigMap+label trigger method. First deletes all resources created during install (MaaS PostgreSQL, Gateway, HardwareProfile, etc.), then triggers Operator auto-cleanup via `delete-self-managed-odh` ConfigMap. Waits for namespace deletion and runs verification |
| **ServiceMesh** | Istio CR → IstioCNI CR → namespace → Operator in order |
| **GPU Operator** | Delete ClusterPolicy CR → wait for deletion → delete Operator + namespace |
| **cert-manager** | Bulk delete Certificate/Issuer/ClusterIssuer CRs → wait for deletion → delete Operator + namespace |
| **Kueue** | Bulk delete Kueue CRs → auto-handle stuck finalizers → delete Operator + namespace |
| **LVM** | PVC pre-check (stop if PVCs using lvms-vg1 exist) → delete LVMCluster → delete Operator + namespace |
| **Others** | Common pattern: delete CR → wait for deletion → delete Operator + namespace |

> **Notes**:
> - Backing up persistent disks used by PVCs is recommended before uninstalling
> - LVM Operator does not automatically delete LVM resources (VG/LV) on nodes. Handle manually if needed
> - Deleting cert-manager resources also deletes associated TLS Secrets

---

## 13. Related Documentation

| Document | Contents |
|---|---|
| [docs/operations.md](docs/operations.md) | Operations guide — model addition procedures, user/group management, MaaS access control, API Key issuance, quota configuration, deletion procedures |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Troubleshooting — past incident examples, pre-deployment checklist, monitoring methods, failure recovery flows, resource health check commands, disconnected environment considerations |
