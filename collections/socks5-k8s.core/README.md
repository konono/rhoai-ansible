# kubernetes.core over a SOCKS5 proxy — control node bundle

Run `kubernetes.core` against a cluster that is only reachable through a SOCKS5
proxy, e.g. an air-gapped cluster behind an SSH bastion.

**No patched collection is needed.** The stock `kubernetes.core` from Ansible
Galaxy already passes the `proxy` value straight through to the Python
kubernetes client, and that client gained SOCKS support in **v37.0.0**. All this
bundle does is pin the Python side correctly.

Verified with: Python 3.12.15, ansible-core 2.21.5, kubernetes 37.0.1,
urllib3 2.8.0, PySocks 1.7.1, kubernetes.core 6.6.0 (unmodified Galaxy artifact).

## What is in here

| file | purpose |
|---|---|
| `pyproject.toml` | Python dependencies. `kubernetes>=37.0.0` and `urllib3[socks]` are the two that matter. |
| `uv.lock` | Resolved versions, so the target host gets the same set. |
| `.python-version` | Pins Python 3.12 for uv. Delete it if you want uv to pick 3.13. |
| `requirements.yml` | The stock `kubernetes.core` 6.6.0 from Galaxy. |
| `ansible.cfg` | Puts the collection in `./collections`, so the bundle does not depend on `~/.ansible/collections`. |
| `verify-socks.yml` | Smoke test: checks PySocks, checks the kubernetes version, then queries the cluster through the proxy. |

## Setup on the target control node

The target needs network access to PyPI and Ansible Galaxy, plus `uv`.

```console
# 1. copy this directory over, then from inside it:
$ uv sync

# 2. install the stock collection next to the bundle
$ uv run ansible-galaxy collection install -r requirements.yml -p ./collections
```

Sanity check:

```console
$ uv run python -c "import kubernetes, socks; print(kubernetes.__version__)"
37.0.1
```

If that prints a version below 37, or raises `ModuleNotFoundError: No module
named 'socks'`, the proxy will not work — see Troubleshooting.

## Usage

Open the tunnel (this is what provides the SOCKS5 proxy):

```console
$ ssh -D 1080 -N bastion.example.com &
```

Verify end to end before running anything real:

```console
$ uv run ansible-playbook verify-socks.yml \
    -e k8s_host=https://api.cluster.internal:6443 \
    -e k8s_kubeconfig=~/.kube/config
```

Expected:

```
TASK [Show what came back] *****************************************************
ok: [localhost] => {
    "msg": "Reached https://api.cluster.internal:6443 through socks5h://127.0.0.1:1080 - server v1.29.0"
}
```

Then in your own playbooks, just set `proxy` on any `kubernetes.core` task:

```yaml
- name: List pods in the air-gapped cluster
  kubernetes.core.k8s_info:
    kind: Pod
    namespace: default
    kubeconfig: ~/.kube/config
    proxy: socks5h://127.0.0.1:1080
```

Or without touching every task:

```console
$ export K8S_AUTH_PROXY=socks5h://127.0.0.1:1080
```

### Why `socks5h` and not `socks5`

With `socks5` the client resolves the hostname **locally**; with `socks5h` it
hands the name to the proxy and the proxy resolves it. If the API server
hostname only exists in the remote network's DNS — the usual case behind a
bastion — you need `socks5h`. Use `socks5` only when the name already resolves
on the control node.

### Hosts that should bypass the proxy

`no_proxy` works for SOCKS exactly as it does for HTTP proxies:

```yaml
    proxy: socks5h://127.0.0.1:1080
    no_proxy: "localhost,127.0.0.1,.internal.example.com"
```

## Troubleshooting

| symptom | cause | fix |
|---|---|---|
| `Proxy URL had unsupported scheme socks5h, should use http:// or https://` | `kubernetes` is older than 37.0.0 | `uv sync` in this directory; check with `uv run python -c "import kubernetes; print(kubernetes.__version__)"` |
| `No module named 'socks'` | PySocks missing | `uv sync` — `urllib3[socks]` in `pyproject.toml` pulls it in |
| Connection hangs or times out | the SSH tunnel is not up | re-run `ssh -D 1080 -N bastion.example.com` |
| `Name or service not known` for the API host | using `socks5` instead of `socks5h` | switch the scheme to `socks5h` |
| `AttributeError: module 'dataclasses' has no attribute '_is_type'` | Python 3.15 picked up | keep `.python-version`, or re-run `uv sync` |

## Note on limitations

`k8s_exec` and `k8s_cp` talk to the cluster over websockets
(`kubernetes.stream` / websocket-client), which does not go through the same
connection pool. Those two modules are **not** covered by the `proxy` setting
and will still try to connect directly.
