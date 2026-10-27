[日本語](README_ja.md)

# RHOAI 3.5 Ansible Installer

For an existing manually deployed environment, start in [migration/](migration/README.md).
It contains discovery, inventory generation, migration planning and apply playbooks,
plus a customer-site AI skill. Existing CSI/storage infrastructure stays externally managed.

## Playbook updates and releases

Preview the latest GitHub Release, then apply the verified source update:

```bash
python3 scripts/self-update.py
python3 scripts/self-update.py --apply
```

For disconnected hosts, transfer the Release assets `rhoai-3.5-ansible.tar.gz` and `checksums.txt`, then use `--archive PATH --checksums PATH` (and `--apply` after previewing). The updater preserves environment inventories, secrets, `.venv`, and vendor files. See [release-process.md](docs/release-process.md) for publishing steps.

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

Proxy settings are automatically read from the Proxy CR (`.status`). The `cluster_proxy` variable has been deprecated.

> **Proxy note**: The playbook does not automatically inject proxy env vars into any Deployment. When a Proxy CR is detected, diagnostic commands and manual configuration instructions are displayed as debug messages. Apply manually after confirming connectivity issues. See `docs/troubleshooting.md` sections 2.7 and 2.8 for details.

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

**Key naming convention**: lowercase the model portion of `hf_repo` and replace `.` with `-` (e.g. `Qwen/Qwen3.8-27B-FP8` → `qwen3-8-27b-fp8`). Keys become Kubernetes resource names, so only lowercase alphanumerics and `-` are allowed. The key is used verbatim as the PVC directory name, as vLLM's `--served-model-name`, and in the Keycloak group name `maas-<key>-users` — so **never rename a key once chosen** (renaming misses the PVC cache and forces a re-download). See [docs/operations.md](docs/operations.md) for details.

```yaml
models:
  qwen3-06b:
    hf_repo: Qwen/Qwen3-0.6B       # (required) HuggingFace repository name
    state: present                   # present=deploy, absent=remove
    # vLLM arguments are a raw list — the role only auto-adds
    # --chat-template (only when custom_chat_template is set), so new vLLM flags
    # can be adopted without changing the role.
    vllm_args:
      - "--enable-prefix-caching"
      - "--gpu-memory-utilization=0.90"
      - "--max-model-len=4096"
      - "--tool-call-parser=qwen3_coder"
      - "--reasoning-parser=qwen3"
      - "--enable-auto-tool-choice"
      - '--speculative-config={"method":"ngram","num_speculative_tokens":3,"prompt_lookup_min":2,"prompt_lookup_max":4}'
    # The following have defaults and can be omitted
    # vllm_image: vllm/vllm-openai:v0.28.0
    # vllm_env: {}                   # extra container env vars
    # custom_chat_template: ""      # empty = use the model's bundled chat template
    #                                # a name loads inventory/<env>/chat_templates/<name>.jinja
    # chat_template_configmap: ""    # override the generated ConfigMap name
    # replicas: 1
    # gpu_count: 1
    # resources: {requests: {cpu: "500m", memory: 4Gi}, limits: {cpu: "2", memory: 8Gi}}
    # shm_size: ""                   # e.g. 16Gi when using tensor parallelism
    # purge: false                   # true to also delete model data on PVC
```

| Field | Description | Default |
|---|---|---|
| `hf_repo` | HuggingFace repository name (required) | — |
| `state` | `present` to deploy, `absent` to remove | `present` |
| `namespace` | Target namespace for deployment | `llm-serving` |
| `vllm_image` | vLLM container image | `vllm/vllm-openai:v0.28.0` |
| `vllm_args` | vLLM arguments, passed through verbatim | see `model_defaults` |
| `vllm_env` | Extra container environment variables (map) | `{}` |
| `custom_chat_template` | Chat template to inject, loaded from `inventory/<env>/chat_templates/<name>.jinja`. Empty keeps the model's bundled template and omits `--chat-template` | `""` |
| `chat_template_configmap` | Override the generated ConfigMap name | `<model>-chat-template` |
| `replicas` | LLMInferenceService replicas | `1` |
| `gpu_count` | GPU devices / MIG slices requested per Pod | `1` |
| `gpu_resource` | Kubernetes GPU resource name | `nvidia.com/gpu` |
| `node_selector` | Inference Pod node labels (mapping) | `{}` |
| `resources` | CPU/memory requests and limits (merged recursively) | `500m`/`4Gi` → `2`/`8Gi` |
| `shm_size` | `/dev/shm` emptyDir size; empty = not mounted | `""` |
| `startup_probe_failure_threshold` / `startup_probe_period_seconds` | Startup wait budget | `120` / `10` |
| `purge` | Whether to delete PVC cache when `state: absent` | `false` |

> **vLLM flags are not modeled as named keys.** Everything in `vllm_args` is passed to the container verbatim, so a vLLM upgrade that adds or renames flags needs an inventory edit only — no role change.

> **The dict key becomes the model name.** It is used as K8s resource names, so it must be RFC 1123 compliant (lowercase alphanumeric, hyphens, and dots only).

#### Behavior by State

| State | Behavior |
|---|---|
| `present` | Download model (skip if cached) → Create LLMInferenceService + MaaSModelRef → Include in AuthPolicy/Subscription |
| `absent` | Delete LLMInferenceService + MaaSModelRef (release GPU) → Exclude from AuthPolicy/Subscription. PVC cache is retained |
| `absent` + `purge: true` | In addition to above, also delete model data on PVC |

> **Processing order**: When running site.yml, `absent` models are processed first (to release GPUs). Then `present` models are deployed.
>
> **GPU capacity check**: `present` models are checked per resource name (`gpu_count × replicas`), and each model must fit its selected nodes. Before creating a model, bound active Pods (including Pending and init containers) are counted. Insufficient capacity stops deployment without scaling down other models. This is a GPU check, not a complete scheduler: CPU, memory, taints, storage topology and simultaneous scheduling remain Kubernetes constraints.

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
| `install_guardrails` | NeMo Guardrails | `false` | Disabled by default due to a known issue ([#15](https://github.com/konono/rhoai-ansible/issues/15)). See docs/daily-operations.md before enabling |
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
**Variables used**: `storage_class`, `maas_db_password`, `operator_channels.rhoai` (Proxy read from Proxy CR status)

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

GroupSync verifies Keycloak TLS using `openshift-config-managed/default-ingress-cert` (`ca-bundle.crt`) directly. Its Subscription sets `WATCH_NAMESPACE=group-sync-operator,openshift-config-managed` so the controller cache can read the managed bundle and follow certificate rotation. No CA copy or insecure TLS override is used. After user/group changes, allow up to five minutes for synchronization before running `verify.yml`. Verification checks current-generation reconciliation and actual OpenShift Group membership; old error conditions can remain after successful recovery.
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

# After a full deployment, verify.yml runs automatically with no exclusions.
# exit 0 means all components passed verification. After a partial run with tags,
# restore the required roles first and then run verification separately:
uv run ansible-playbook playbooks/verify.yml -i inventory/myenv

# API key issuance verification creates and revokes a test key, so it runs only when explicitly enabled:
uv run ansible-playbook playbooks/verify.yml -i inventory/myenv -e verify_maas_api_key_issuance=true

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
| MaaS users (admin1, testuser1, etc.) | `.credentials/<inventory>/<username>.password` | For MaaS Dashboard / API Key issuance |
| MaaS API Key | `.vllm-token` | Bearer token for LLM inference requests |

Commands to check credentials:

```bash
# Keycloak admin
oc get secret keycloak-initial-admin -n keycloak \
  -o jsonpath='username: {.data.username} / password: {.data.password}' | \
  xargs -I{} sh -c 'echo {} | sed "s/username: //" | cut -d/ -f1 | base64 -d; echo -n " / "; echo {} | sed "s/.*password: //" | base64 -d; echo'

# MaaS user passwords
cat .credentials/<inventory>/admin1.password
cat .credentials/<inventory>/testuser1.password

# MaaS API Key
cat .vllm-token
```

#### Credential File Layout

```
rhoai-3.5-ansible/
├── .credentials/<inventory>/  ← Keycloak user passwords (gitignored)
│                              Scoped per inventory so the same username in
│                              different environments does not share a password
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
| (Proxy) | Proxy CR status | Auto-detected. Manual application after diagnosis |
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
│   ├── llm_download.yml         # Download model data only (download_only)
│   ├── llm_edit_args.yml        # Patch vLLM args in place (oc edit equivalent)
│   ├── llm_show_manifest.yml    # Preview the rendered manifest without a cluster
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
│   ├── daily-operations.md       # Day-to-day request handling (users, quota, models)
│   ├── offline-environment.md    # Building / migrating the offline execution environment
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

Removes enabled components in reverse installation order. The RHOAI role uses Red Hat's ConfigMap and label uninstall procedure; the other roles remove their dependent resources before their Operators.

### 12.2 Uninstalling Specific Components

```bash
# RHOAI + workloads only
uv run ansible-playbook playbooks/uninstall.yml -i inventory/myenv --tags platform,workload

# Specific roles only (can run even if disabled in inventory)
uv run ansible-playbook playbooks/uninstall.yml -i inventory/myenv --tags gpu -e '{"uninstall_gpu_operator": true}'
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
  --tags rhoai -e '{"uninstall_rhoai": true}'
```

### 12.5 Retrying on Errors

If a failure occurs mid-process, a retry command is shown in the error message:

```
TASK [Fail if PVCs using lvms-vg1 still exist] ********************************
fatal: [localhost]: FAILED! =>
  msg: |-
    LVM Operator uninstall failed.
    Retry: ansible-playbook playbooks/uninstall.yml -i inventory/<env> --tags lvm -e '{"uninstall_lvm": true}'
```

Replace `inventory/<env>` with your actual inventory path and run the command.

### 12.6 Uninstall Behavior

The roles use these removal steps:

| Role | Uninstall Method |
|---|---|
| **RHOAI** | Official ConfigMap+label trigger method. Deletes install-created resources, then triggers Operator cleanup via `delete-self-managed-odh`. If namespaces remain, recovers only CRs already being deleted, with no pods and only known finalizers; confirms namespace deletion |
| **ODF** | Waits for all OBCs to be removed, enables NooBaa `spec.cleanupPolicy.allowNoobaaDeletion`, then deletes NooBaa and its Operator |
| **ServiceMesh** | Istio CR → IstioCNI CR → namespace → Operator in order |
| **GPU Operator** | Delete ClusterPolicy CR → wait for deletion → delete Operator + namespace |
| **cert-manager** | Bulk delete Certificate/Issuer/ClusterIssuer CRs → wait for deletion → delete Operator + namespace |
| **Kueue** | Bulk delete Kueue CRs → auto-handle stuck finalizers → delete Operator + namespace |
| **LVM** | PVC pre-check (stop if PVCs using lvms-vg1 exist) → delete LVMCluster → delete Operator + namespace |
| **Others** | Common pattern: delete CR → wait for deletion → delete Operator + namespace |

> **Notes**:
> - Back up persistent disks used by PVCs before uninstalling. This is a prerequisite in Red Hat's RHOAI uninstall procedure; the playbook cannot verify that a backup exists.
> - LLM uninstall deletes `download-*` Jobs that reference `hf-model-cache`, waits for their dependent Pods to disappear, then deletes the cache PVC. Job lookup or deletion failures stop this sequence so a completed Job cannot survive deletion of its cache.
> - Full RHOAI uninstall also deletes the `llm-serving` namespace. Namespace-local VolumeSnapshots are not sufficient protection with a `Delete` policy; retain the snapshot contents and cloud snapshot handles outside the deleted namespace when using CSI snapshots as backups.
> - Remove ODF-backed PVCs and OBCs before removing NooBaa. The ODF role waits for all OBCs to disappear and stops if any remain.
> - The playbook sets the NooBaa deletion policy after OBC removal, following [Red Hat's procedure](https://access.redhat.com/solutions/5948631).
> - LVM Operator does not automatically delete LVM resources (VG/LV) on nodes. Handle manually if needed
> - Deleting cert-manager resources also deletes associated TLS Secrets

---

## 13. Related Documentation

| Document | Contents |
|---|---|
| [docs/operations.md](docs/operations.md) | Operations guide — model addition procedures, user/group management, MaaS access control, API Key issuance, quota configuration, deletion procedures |
| [docs/daily-operations.md](docs/daily-operations.md) | Day-to-day operations — handling user requests: add/disable users, password reset, quota changes, model add/swap/remove, API key issuance, Keycloak admin recovery, where secrets live |
| [docs/offline-environment.md](docs/offline-environment.md) | Offline execution environment — asset layout, building from scratch, migrating from a running deployment, venv path rewriting, distributing updates with pack/unpack |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Troubleshooting — past incident examples, pre-deployment checklist, monitoring methods, failure recovery flows, resource health check commands, disconnected environment considerations |

## 追加 GPU worker の MIG 設定

`playbooks/configure_mig.yml` は GPU Operator 導入済みのノードを対象に、
MIG Manager の設定を確認し、MIG profile と allocatable resource 数が収束するまで待ちます。
L4 は MIG 非対応です。今回の RTX PRO 6000 Blackwell Server Edition 96 GB では
`all-1g.24gb` / `single` により `nvidia.com/gpu: 4` を確認しました。

```bash
export KUBECONFIG=/path/to/cluster/kubeconfig
# まず読み取りのみで対象・対応 profile・workload を確認
uv run ansible-playbook playbooks/configure_mig.yml -i inventory/aws-sno-ipi \
  -e @playbooks/examples/mig-rtx-pro-6000.yml
# 設定を適用し、既定 20 分まで収束を待つ
uv run ansible-playbook playbooks/configure_mig.yml -i inventory/aws-sno-ipi \
  -e @playbooks/examples/mig-rtx-pro-6000.yml -e mig_apply=true
```

対象 selector、profile、期待 resource 数は明示します。サンプルの selector は今回の
worker に付けた `rhoai-validation=mig` です。他の GPU には、その GPU が対応する
profile を指定してください。GPU Operator のノード別 / カスタム / 既定 ConfigMap を
読み、profile の存在を検査します。`single` では同形状の MIG 領域を通常の
`nvidia.com/gpu` として要求でき、`mixed` では対応する `nvidia.com/mig-*` resource を使います。

分割変更が必要な対象に GPU Pod があれば中止します。ClusterPolicy の strategy を
変える場合はクラスタ全体で GPU workload を停止してから実行してください。
自動 eviction、drain、再起動は行いません。変更対象を cordon して再検査し、成功後だけ
元の schedulable 状態に戻します。失敗時は cordon を維持するため、MIG Manager logs、
`nvidia.com/mig.config.state`、GPU resource 数を確認してから復旧してください。
ハードウェアが reboot を要求する場合もこの playbook は再起動を実行しません。
既に収束していれば推論稼働中でも再実行でき、cordon や GPU 再設定は行いません。

Machine API の worker は成功後に所有 MachineSet の node label にも profile を保存し、
replacement worker に引き継ぎます。Machine API を使わないノードでは
`mig_persist_machineset=false` を指定します。`gpu_mig_strategy` は inventory にも保存し、
後続の `site.yml` / manifest レンダリングと同じ値にしてください。
MIG 設定はハードウェアと稼働 Pod の照合を必要とする独立した運用 playbook で、
通常の `site.yml` 実行から自動的に GPU を再分割することはありません。

参考: [NVIDIA MIG 対応 GPU](https://docs.nvidia.com/datacenter/tesla/mig-user-guide/supported-gpus.html)、
[GPU Operator の MIG 設定](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/gpu-operator-mig.html)。

### 設定は role ではなく inventory に置く

`mig_node_selector`・`mig_profile`・`mig_expected_resources` は、環境の GPU と
運用方針に依存するため **inventory の変数**として管理します。
role の `vars/main.yml` に固定すると環境ごとの変更が難しくなります。
role に置くなら共通処理や汎用的な既定値であり、対象ノードや分割形状の選択は inventory が担当します。
現状の共通処理は `playbooks/tasks/mig_*.yml` にあります。

一つの構成を管理する場合の配置例:

```text
inventory/myenv/
├── hosts.yml                         # 実行ホストは localhost
└── group_vars/all/
    ├── cluster.yml                   # クラスタ共通の gpu_mig_strategy
    ├── components.yml
    ├── vault.yml
    └── mig.yml                       # 対象 selector、profile、期待リソース数
```

`cluster.yml`:

```yaml
gpu_mig_strategy: single
```

`mig.yml`（RTX PRO 6000 Blackwell Server Edition 96 GB × 1 枚の例）:

```yaml
mig_node_selector: mig-group=rtx6000-24gb
mig_profile: all-1g.24gb
mig_expected_resources:
  nvidia.com/gpu: 4
mig_full_gpu_resources:
  nvidia.com/gpu: 1
mig_apply: false
```

`gpu_mig_strategy` は `cluster.yml` に置いてください。通常の `site.yml` に加え、
manifest renderer もこのファイルを読みます。独立した MIG 操作用の `mig.yml` は
Ansible が読み込みますが、renderer は読みません。renderer はノードの分割を自動適用しません。
環境別 inventory は Git 管理外の場合があるため、別途バックアップや構成管理の対象にしてください。

### ノード名・hostname label・GPU label を確認する

以下のコマンドは `rhoai-3.5-ansible/` で実行します。

```bash
export KUBECONFIG=/path/to/cluster/kubeconfig
oc get nodes -o wide
oc get nodes -L kubernetes.io/hostname,nvidia.com/gpu.product,nvidia.com/mig.capable,mig-group
# あるノードの全 label
oc get node <node-name> --show-labels
# 現在の分割・状態・割り当て可能リソース
oc get node <node-name> -o json | jq '{
  name: .metadata.name,
  hostname: .metadata.labels["kubernetes.io/hostname"],
  gpu: .metadata.labels["nvidia.com/gpu.product"],
  profile: .metadata.labels["nvidia.com/mig.config"],
  state: .metadata.labels["nvidia.com/mig.config.state"],
  allocatable: .status.allocatable
}'
```

Node の `metadata.name` と `kubernetes.io/hostname` は同じとは限らないため、
selector には確認した **label の値**を使います。自動生成される `nvidia.com/*` label は
GPU Operator が管理します。運用グループには別の label（ここでは `mig-group`）を使います。

### 特定の 1 ノードに適用する

inventory の profile・期待数を使い、対象 selector だけ上書きできます。

```bash
# <hostname-label-value> は上の確認コマンドで取得した値
oc get nodes -l 'kubernetes.io/hostname=<hostname-label-value>'
./playbooks/list_mig_profiles.sh --node <node-name>

# 読み取りのみの事前検査
uv run ansible-playbook playbooks/configure_mig.yml -i inventory/myenv \
  -e 'mig_node_selector=kubernetes.io/hostname=<hostname-label-value>'
# 確認後に適用
uv run ansible-playbook playbooks/configure_mig.yml -i inventory/myenv \
  -e 'mig_node_selector=kubernetes.io/hostname=<hostname-label-value>' -e mig_apply=true
```

複数ノードを持つ MachineSet の一部だけを選び、`mig_persist_machineset=true` のまま
実行すると停止します。MachineSet の template label は全 replacement worker に影響するためです。
一時的に 1 ノードだけ変えるなら `-e mig_persist_machineset=false` を明示します。
恒久的に異なる構成を使う worker は、MachineSet 自体も分けて管理してください。

### inventory と group_vars で構成別に管理する

**Ansible の group と Kubernetes のノードグループは別物です。**
この playbook は `hosts: localhost` で実行し、`mig_node_selector` で Kubernetes ノードを選びます。
GPU ノード名を Ansible inventory に追加しただけでは対象になりません。
`--limit <GPU-node-name>` でも Kubernetes ノードは選択できません。

複数の構成を管理する場合は、クラスタ共通 inventory に **構成別の補助 inventory を一つ重ねる**
方式にできます。Ansible group は localhost に適用する設定を選び、実ノードは label で対応づけます。

```bash
# Kubernetes 側のグループ。<node-a>, <node-b> は実際のノード名に置換
oc label node <node-a> mig-group=rtx6000-24gb --overwrite
oc label node <node-b> mig-group=rtx6000-48gb --overwrite
# 適用前に所属ノードを確認
oc get nodes -l mig-group=rtx6000-24gb
oc get nodes -l mig-group=rtx6000-48gb
```

```text
inventory/
├── myenv/                            # クラスタ共通。既存の inventory
│   ├── hosts.yml
│   └── group_vars/all/cluster.yml     # gpu_mig_strategy: single
├── mig-rtx24/                        # 24 GB × 4 用の補助 inventory
│   ├── hosts.yml
│   └── group_vars/mig_rtx24.yml
└── mig-rtx48/                        # 48 GB × 2 用の補助 inventory
    ├── hosts.yml
    └── group_vars/mig_rtx48.yml
```

`inventory/mig-rtx24/hosts.yml`:

```yaml
all:
  children:
    mig_rtx24:
      hosts:
        localhost:
          ansible_connection: local
          ansible_python_interpreter: "{{ ansible_playbook_python }}"
```

`inventory/mig-rtx24/group_vars/mig_rtx24.yml`:

```yaml
mig_node_selector: mig-group=rtx6000-24gb
mig_profile: all-1g.24gb
mig_expected_resources:
  nvidia.com/gpu: 4
mig_full_gpu_resources:
  nvidia.com/gpu: 1
mig_persist_machineset: true
mig_apply: false
```

48 GB 用は `hosts.yml` の group 名を `mig_rtx48` にし、
`inventory/mig-rtx48/group_vars/mig_rtx48.yml` を次のようにします。

```yaml
mig_node_selector: mig-group=rtx6000-48gb
mig_profile: all-2g.48gb
mig_expected_resources:
  nvidia.com/gpu: 2
mig_full_gpu_resources:
  nvidia.com/gpu: 1
mig_persist_machineset: true
mig_apply: false
```

```bash
# Ansible 側の所属を確認（秘密変数を表示する --vars は不要）
uv run ansible-inventory -i inventory/myenv -i inventory/mig-rtx24 --graph
# 構成別に事前検査
uv run ansible-playbook playbooks/configure_mig.yml \
  -i inventory/myenv -i inventory/mig-rtx24
uv run ansible-playbook playbooks/configure_mig.yml \
  -i inventory/myenv -i inventory/mig-rtx48
# 対象・停止条件を確認してから、必要なグループだけ適用
uv run ansible-playbook playbooks/configure_mig.yml \
  -i inventory/myenv -i inventory/mig-rtx24 -e mig_apply=true
# 同じグループを全 GPU に戻す場合
uv run ansible-playbook playbooks/disable_mig.yml \
  -i inventory/myenv -i inventory/mig-rtx24 -e mig_apply=true
```

子 group の設定は `group_vars/all` より優先されます。ただし `host_vars/localhost` や
`-e` に同じ変数がある場合はさらに優先されるため、重複定義を整理してください。
**24 GB 用と 48 GB 用の補助 inventory を同じ実行に両方渡さないでください。**
localhost に両 group の変数がマージされ、片方が上書きされます。
`--limit` は変数のマージを解除しないため、この回避策にはなりません。
変数の読み込み規則は [Ansible inventory の公式説明](https://docs.ansible.com/projects/ansible/latest/inventory_guide/intro_inventory.html) を参照してください。

この補助 inventory は MIG 操作専用です。通常の `site.yml` と renderer はクラスタ共通の
`inventory/myenv` を使います。replacement worker の `mig-group` label は、worker 作成用
inventory の `worker_node_labels` にも保存してください。MIG playbook が MachineSet に保存するのは
`nvidia.com/mig.config` であり、任意のグループ label を自動保存するわけではありません。

### `single` と `mixed` の違い・設定方法

正しい値は **`mixed`** です（`mix` ではありません）。strategy は GPU の分割形状そのものではなく、
分割済み GPU を Kubernetes のどのリソース名で公開するかを決めます。

| 項目 | `single` | `mixed` |
|---|---|---|
| MIG を使うノード内の構成 | 同一形状に揃える。複数 GPU がある場合も揃える | 異なる形状や、全 GPU と MIG の混在に対応 |
| MIG 領域のリソース名 | `nvidia.com/gpu` | `nvidia.com/mig-<形状>` |
| 24 GB × 4 の例 | `nvidia.com/gpu: 4` | `nvidia.com/mig-1g.24gb: 4` |
| Pod が 1 領域を要求する例 | `nvidia.com/gpu: 1` | `nvidia.com/mig-1g.24gb: 1` |
| MIG 無効の GPU | `nvidia.com/gpu` | `nvidia.com/gpu` |
| この repo の LLM 配信テンプレート | 既定値で利用可能 | モデルの `gpu_resource` で MIG リソース名を指定 |

`single` でも、別ノードで 24 GB と 48 GB の profile を使い分けられます。
ただし同じ `nvidia.com/gpu: 1` なので、Pod 側にも `nodeSelector` / node affinity を設定して
必要な容量のノードを選びます。**MIG 設定の `mig_node_selector` は推論 Pod の配置を設定しません。**
別ノードの L4 のように MIG を使わない GPU とも同じリソース名になる点に注意してください。

`mixed` の設定例（対応 profile と resource 名は実機で確認すること）:

```yaml
# クラスタ共通 inventory の group_vars/all/cluster.yml
gpu_mig_strategy: mixed
```

```yaml
# 対象 group の変数
mig_node_selector: mig-group=rtx6000-24gb
mig_profile: all-1g.24gb
mig_expected_resources:
  nvidia.com/mig-1g.24gb: 4
mig_full_gpu_resources:
  nvidia.com/gpu: 1
```

同じ `configure_mig.yml` で適用します。ClusterPolicy の strategy は **クラスタ共通**なので、
ノードごとに `single` と `mixed` を切り替える運用にはしません。strategy を変更する場合は
この playbook がクラスタ全体の GPU workload を検査し、残っていれば停止します。
変更後はすべての MIG group の期待リソース名と Pod の GPU 要求を揃えてください。
分割 profile の変更だけなら、通常はその対象ノードの workload 停止で済みます。

モデルの設定例（`inventory/<env>/group_vars/all/components.yml`）:

```yaml
models:
  qwen3-06b:
    hf_repo: Qwen/Qwen3-0.6B
    state: present
    gpu_resource: nvidia.com/mig-1g.24gb
    gpu_count: 1
    replicas: 2
    node_selector:
      mig-group: rtx6000-24gb
```

この設定は Ansible と `render.py` の両方で、LLMInferenceService の
`spec.template.nodeSelector` とコンテナの requests / limits に反映されます。
上の例は **各 Pod が 24 GB の区画を 1 個、2 replicas で合計 2 個**要求します。
別モデルに `nvidia.com/mig-2g.48gb` を指定することもできますが、対応する区画を
事前に用意してください。`gpu_resource` は区画を作成する設定ではありません。
省略時は従来どおり `nvidia.com/gpu` です。GPU 要求は `resources` に直接追加せず、
`gpu_resource` / `gpu_count` で指定してください。

容量確認と更新時の GPU 数判定も、このリソース名と `node_selector` を使います。
物理 GPU、24 GB 区画、48 GB 区画は別々に数えます。既存モデルの `gpu_resource` や
`node_selector` を変更した場合は古いキーを残さないため対象 LLMInferenceService を再作成し、
そのモデルの推論は一時停止します。モデルを新規作成する際に
容量が使用中なら、対象 Pod を表示して停止します。別モデルを自動縮退しません。
再作成時は旧 Pod の消滅を待ち、待機期限超過や取得エラーなら再作成へ進まず停止します。
既存モデルの再作成前には他 workload の予約を含む空き容量確認を行わないため、
配置先変更前に対象ノードの空きを確認してください。複数モデルが同じ selector に
集中する場合の合計要求も、事前のモデル別容量確認だけでは検出できません。
共有 PVC は従来どおり RWO のため、異なるノードへ配置する場合は PVC の制約にも注意してください。
生成手順書にも同じ GPU 判定を行う `llm-deploy.sh` / `llm-gpu.jq` を同梱します
（実行環境は Bash・`oc`・`jq`）。全モデルの資源別合計・選択ノード容量を事前確認し、
新規モデルでは bound Pending を含む Pod と init/sidecar の予約量を差し引きます。
既存モデルでは image・args・env・resources・mount/probe・volumes・replicas・model URI・
nodeSelector の変更を比較し、GPU 種別・配置先変更またはローリング更新の容量不足なら
対象モデルだけを再作成します。変更なしでは削除しません。
ダウンロードは同梱の `llm-download.sh` が Ansible と同じ順序で判断します。
Ready なモデルと完了済み Job はスキップし、実行中の Job は完了を待ちます。
新たなダウンロードが必要な場合は PVC のキャッシュを確認し、キャッシュがなければ
失敗済み Job を削除して再作成します。既存のモデルキャッシュ PVC は変更せず、
不在時だけ `01-model-cache-pvc.yml` から作成します。
旧 Pod の終了待ちは最大 300 秒で、取得エラー・期限超過なら適用へ進まず失敗します。
原因解消後に同じ手順を再実行してください。適用失敗・Ready 待機失敗も成功扱いにしません。
手順書の LLM Serving の Bash ブロックは専用の子 Bash 内で実行されるため、
エラーで手順は停止しても対話シェルは終了しません。ブロックの開始・終端行も含めて実行してください。
`oc --dry-run=client` もクラスター設定と CRD のリソース情報を使うため、
LLMInferenceService / MaaSModelRef の CRD が登録済みのクラスターへの接続が必要です。
上記の再作成前の空き容量・selector 競合・RWO の制約は生成手順書にも共通です。
GPU の MIG 有効化・無効化は専用 Ansible playbook で行い、生成手順書は構成済み GPU を使います。
異なるサイズを同じ GPU に混在させる例は
[`playbooks/examples/mig-mixed-rtx-pro-6000.yml`](playbooks/examples/mig-mixed-rtx-pro-6000.yml)
を参照してください。RTX PRO 6000 の `all-balanced` は 24 GB × 2 と 48 GB × 1 です。
別 GPU では `list_mig_profiles.sh` で実機の profile を確認してください。

配信後は、Ansible で実推論・Pod の GPU 要求・実際に見える MIG UUID・ノードラベル・
Prometheus の推論成功カウンターと DCGM メモリ使用量を確認できます。
この検証用 playbook は MIG 設定やモデルの削除を行いません。

```bash
# models.yml には検証対象のモデルを定義する（通常 GPU モデルとの混在も可）
uv run ansible-playbook playbooks/verify_mig_models.yml \
  -i inventory/<env> -e @models.yml \
  -e mig_verification_output=/tmp/mixed-result.json
```

推論確認は選択された全モデルに対して行い、その後の MIG UUID・監視・同時推論の
検証は設定を合成した結果が `nvidia.com/mig-*` になるモデルに限定します。
MIG 対象が 0 件なら成功扱いにせず停止します。

既定ではモデルに直接 3 回ずつ推論し、その後各 replica に 3 リクエストずつ同時送信します。
監視の前提は UWM / DCGM の収集、
`thanos-querier` Route、および監視データを読める ServiceAccount です。
ServiceAccount は既定で `openshift-cluster-observability-operator/perses-sa`、
`mig_metrics_namespace` / `mig_metrics_serviceaccount` で変更できます。
MaaS 経由の追加検証は、アクセス権を設定したうえで既存の
`llm_verify_inference.yml -e inference_via=maas -e maas_api_user=<user>` を利用します。

2026-10-26 に RTX PRO 6000 Blackwell の `mixed` / `all-balanced` で実機検証しました。
Qwen3-0.6B を 24 GB 区画、Qwen3-4B を 48 GB 区画へ Ansible role で配置し、
直接推論 6 件、同時推論 6 件、MaaS の body-based / path-based 各 6 件が成功しました。
各 Pod の MIG UUID が異なること、Prometheus に両サイズの DCGM メトリクスがあること、
MIG 設定とモデル配置の再実行が `changed=0` であることも確認しました。
GPU Pod が残っている間の MIG 無効化は、対象 2 Pod を表示して変更せず停止しました。
検証後は Ansible で MIG 無効・元の `single` strategy へ戻し、既存の Qwen3.5-9B を復元して
推論応答（`Tokyo`）まで確認しました。検証用の MaaS ポリシーと一時 API キーは削除・失効済みです。

#### 複数モデルを MaaS Gateway から利用する

Gateway は共通で、`GET /v1/models` にアクセス可能なモデルが一覧表示されます。
上の実機検証では次の 2 ID が `ready: true` で返り、同じ Subscription に所属していました。

| モデル | body-based の `model` | path-based の URL パス |
|---|---|---|
| 0.6B | `publishers/llm-serving-mig/models/qwen3-06b-mig` | `/llm-serving-mig/qwen3-06b-mig/v1/chat/completions` |
| 4B | `publishers/llm-serving-mig/models/qwen3-4b-mig` | `/llm-serving-mig/qwen3-4b-mig/v1/chat/completions` |

body-based は共通の `/v1/chat/completions` へ完全修飾 ID を送り、
path-based は上の個別パスへ短いモデル名（例 `qwen3-06b-mig`）を送ります。
利用者ごとの一覧は次の playbook で取得できます。一時 API キーは終了時に失効させます。

```bash
uv run ansible-playbook playbooks/maas_list_models.yml \
  -i inventory/<env> -e maas_api_user=<inventoryに定義したユーザー>
```

##### body-based の `model` ID を確認する

**`GET /v1/models` が返す `data[].id` を、そのままリクエストの `model` に使います。**
次の playbook は inventory の `keycloak_users` から指定ユーザーのグループを解決し、
その利用者の一時 API キーで Gateway の `/v1/models` を実際に問い合わせます。
名前を組み立てて表示しているだけではありません。

```bash
cd rhoai-3.5-ansible

# <env> と <user> は利用する inventory と keycloak_users の username に置換
uv run ansible-playbook playbooks/maas_list_models.yml \
  -i inventory/<env> -e maas_api_user=<user> \
  -e maas_model_list_output=/tmp/maas-models.json

# body-based リクエストに指定できる ID を表示
jq -r '.data[].id' /tmp/maas-models.json

# Ready 状態と所属 Subscription も併せて確認
jq '.data[] | {id, ready, subscriptions}' /tmp/maas-models.json
```

今回の検証時の出力例:

```text
publishers/llm-serving-mig/models/qwen3-06b-mig
publishers/llm-serving-mig/models/qwen3-4b-mig
```

例えば 1 行目を使う場合、共通の `/v1/chat/completions` に次の JSON を送ります。

```json
{
  "model": "publishers/llm-serving-mig/models/qwen3-06b-mig",
  "messages": [{"role": "user", "content": "こんにちは"}],
  "max_tokens": 64,
  "chat_template_kwargs": {"enable_thinking": false}
}
```

ID の形式は `publishers/<namespace>/models/<LLMInferenceService の metadata.name>` です。
Kubernetes 上の名前は次のコマンドで確認できます。

```bash
oc get llminferenceservice -A
```

ただし、この一覧は Kubernetes 上の配置情報です。利用者の MaaS アクセス権とは異なります。
モデルが配置済みでも `/v1/models` に出ない場合は、問い合わせたユーザーのグループ、
MaaSAuthPolicy、MaaSSubscription、MaaSModelRef の状態を確認してください。
`ready: true` のモデルを選び、**最終的には実際の API 一覧の `id` を使ってください**。
保存される JSON に API キーは含まれず、検証用キーは playbook 終了時に失効します。

Subscription はモデル単位ではなく `keycloak_groups` のグループ単位で生成します。
`llm_add_model.yml` は `install_keycloak: true` の場合、モデル配置の後に
`manage_maas_access` も実行し、`<group>-subscription` の `modelRefs` を更新します。
通常グループでは `keycloak_groups.<group>.models` に新しいモデル名を追加してください。
`models: ["*"]` の管理者グループの Subscription は、再適用時に全 `present` モデルを含みます。
LLM role だけの実行ではアクセス設定を更新しません。

仕様の詳細は [NVIDIA GPU Operator](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/gpu-operator-mig.html) と
[device plugin の MIG_STRATEGY](https://github.com/NVIDIA/k8s-device-plugin#configuration-option-details) を参照してください。

### MIG 変数の一覧

| 変数 | 内容・単位 | 既定 / 指定方法 |
|---|---|---|
| `gpu_mig_strategy` | クラスタ共通の GPU 公開方式。`single` または `mixed` | MIG 設定時は必須。`cluster.yml` に保存 |
| `mig_node_selector` | 対象 Kubernetes ノードの label selector | 必須。例 `mig-group=rtx6000-24gb` |
| `mig_profile` | GPU Operator ConfigMap の `mig-configs` 配下のキー | 必須。例 `all-1g.24gb` |
| `mig_expected_resources` | 適用後に確認するリソース名と個数。**各ノードの個数**でありグループ全体の合計ではない | 必須。割り当て可能な総数（allocatable）を指定 |
| `mig_full_gpu_resources` | 無効化後の **各ノードの物理 GPU 数** | `disable_mig.yml` で必須。例 `nvidia.com/gpu: 1` |
| `mig_apply` | `true` で変更を実行。`false` で事前検査のみ | `false`。`--check` は `true` 指定時も変更しない |
| `mig_persist_machineset` | 成功後の profile を所有 MachineSet に保存 | `true`。同じ MachineSet の既存ノードをすべて選ぶ。非 Machine API ノードは `false` |
| `mig_ready_timeout` | MIG state・resource 数・Node Ready を待つ秒数 | `1200`。ハードウェア検査には別途最大 30 回・10 秒間隔の再試行がある |
| `mig_operator_namespace` | GPU Operator / MIG ConfigMap の namespace | `nvidia-gpu-operator` |
| `mig_cluster_policy` | 操作する ClusterPolicy 名 | `gpu-cluster-policy` |

`mig_expected_resources` は検査条件であり、GPU を増やす設定ではありません。
実際の分割を決めるのは `mig_profile` です。物理 GPU が 2 枚で各 4 分割なら、
ノードの期待数は 8、無効化後の物理 GPU 数は 2 になります。
異なる物理 GPU 数や profile のノードは同じグループにせず、selector を分けてください。
また、この検査は指定したリソースの値を照合するもので、未指定リソースの不存在までは検査しません。

### MIG を無効化して元の GPU 全体へ戻す

```bash
# 事前検査（今回の inventory は物理 GPU 数 = 1 を設定済み）
uv run ansible-playbook playbooks/disable_mig.yml -i inventory/aws-sno-ipi
# 対象 worker の GPU モデルを停止し、GPU を要求する Pod が無くなってから実行
uv run ansible-playbook playbooks/disable_mig.yml -i inventory/aws-sno-ipi -e mig_apply=true
# 再び 4 分割する場合
uv run ansible-playbook playbooks/configure_mig.yml -i inventory/aws-sno-ipi -e mig_apply=true
```

`disable_mig.yml` は `all-disabled` を適用し、物理 GPU 数
（`mig_full_gpu_resources`。今回 `nvidia.com/gpu: 1`）と、NVIDIA MIG Parted の
読み取り専用 `assert` で MIG 無効を確認します。対象 node と MachineSet の profile を
同時に管理するため、replacement worker も全 GPU の状態になります。
他ノードの MIG を壊さないよう **ClusterPolicy の strategy は現在値を維持**します。
`single` / `mixed` のままでも、MIG 無効の GPU は全体を `nvidia.com/gpu` で割り当てられます。

推論 Pod を自動停止する処理はありません。Deployment を直接 scale しても
LLMInferenceService controller が復元する場合があるため、モデルの管理元で停止してください。
設定変更で停止したモデルを自動的に復元することもありません。全 GPU に戻した後は
モデルの VRAM 要件と GPU 数を見直してから配信を再開してください。
GPU worker や PVC 自体は削除しません。

分割 profile を変更するときは、前回の success label を新しい profile と同一 patch 内で
消し、古い成功状態を完了と取り違えないようにしています。適用後はラベル・resource 数に
加えて NVIDIA MIG Parted で実際の構成を検証します。`WITH_REBOOT` が有効な MIG Manager は
事前検査で拒否するため、この playbook 経由で意図せず自動 reboot を起こすことはありません。

MIG 解除時に必要な GPU workload 数は **対象ノードで 0 Pod** です。
4 分割に 4 Pod が動いている場合、3 Pod だけ止めて残り 1 Pod をそのまま全 GPU に
引き継ぐことはできません。4 Pod ともいったん停止し、解除後に必要なモデルを再起動します。
playbook は変更前に対象ノード、停止が必要な namespace/Pod 名、現在数、必要数 0 を表示し、
条件を満たさなければ停止します。対象外ノードの workload は、strategy を変更しない限り
この停止条件には含めません。

2026-10-26 実機確認: MIG worker 上の 3 モデルを管理元の LLMInferenceService から停止後、
`disable_mig.yml -e mig_apply=true` を実行し、`ok=48 changed=4 failed=0`。
MIG Parted の実構成検査、`nvidia-smi` の `Disabled`、allocatable `nvidia.com/gpu: 1`、
MachineSet の `all-disabled`、cordon 解除を確認しました。キャッシュ PVC は保持し、
別ノードの L4 / qwen3-5-9b の推論も継続成功しています。

### GPU ごとの MIG プロファイル一覧

Bash ツール `playbooks/list_mig_profiles.sh` で、GPU Operator が公開する profile と
分割数を確認できます。依存は `oc` と `jq` のみです。

```bash
# MIG 対応ラベルを持つ全ノード
./playbooks/list_mig_profiles.sh
# 対象を絞る
./playbooks/list_mig_profiles.sh --selector rhoai-validation=mig
./playbooks/list_mig_profiles.sh --node <GPU-worker-name>
```

ノード名・GPU 名・現在の profile/state・取得元 ConfigMap と、その `config.yaml` を
表示します。例えば `all-1g.24gb` の `1g.24gb: 4` は 24 GB の領域 4 個、
`all-2g.48gb` の `2g.48gb: 2` は 48 GB の領域 2 個です。
カスタム ConfigMap 指定を優先し、未指定ならノード別、次に共有の既定 ConfigMap を使います。
共有・カスタム設定は、この GPU に適用できない項目を含む場合があるため取得元を確認してください。
YAML をテキスト処理で解釈せず原文表示するため、混在分割や device ごとの設定も確認できます。
設定変更や Pod 内でのコマンド実行は行いません。権限・接続エラー時は停止します。
