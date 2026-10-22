#!/usr/bin/env python3
"""
role のタスク内に直書きされた definition: と render.py の生成物を比較する。

背景:
  render.py は Ansible role のロジックを Python で再実装したもので、両者が
  ずれると「Ansible で入れた環境」と「手順書で入れた環境」が別物になる。
  テンプレート (.j2) 化されているリソースは tests/test_render_parity.py が
  比較しているが、role のリソース定義の大半はタスク内にインラインで書かれており
  そちらは未検証だった。

方針（role 側は変更しない）:
  1. roles/*/tasks/*.yml をパースして definition: ブロックを取り出す
  2. 取り出した定義を YAML に書き出し、本物の Ansible でテンプレート評価する
     （jinja2 を自前フィルタで模倣するとスタブと本物がずれて検出漏れになるため）
  3. render.py の生成物と (kind, namespace, name) で突き合わせる

対象外:
  - register した実行時ファクトに依存する定義（_chat_template, _noobaa_ca_b64,
    _tenant_ns など）。クラスタ実行時にしか値が決まらないため再現できない。
  - loop の item に依存する定義。
  - uninstall 系タスク。
  いずれもレンダリングに失敗した時点でスキップし、SKIPPED_REASONS に記録する。

実行:
    python3 -m unittest tests.test_inline_definition_parity
"""

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Fix non-blocking IO for aw container
for fd in [sys.stdout, sys.stderr]:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)

import yaml

ANSIBLE_DIR = Path(__file__).resolve().parent.parent
ROLES_DIR = ANSIBLE_DIR / "roles"
RENDER_PY = ANSIBLE_DIR / "render-manifests" / "render.py"
INVENTORY_DIR = ANSIBLE_DIR / "inventory"

def _inventories():
    """inventory/ 配下を自動検出する。

    環境名を直書きすると公開リポジトリに顧客名が残るため、実在するものを拾う。
    CI では sample だけが git 管理されている。
    """
    if not INVENTORY_DIR.is_dir():
        return []
    return sorted(p.name for p in INVENTORY_DIR.iterdir()
                  if p.is_dir() and (p / "group_vars" / "all").is_dir())


INVENTORIES = _inventories()

# 共有タスク。呼び出し側の vars: と組み合わせて展開する。
INSTALL_OPERATOR = ROLES_DIR / "common" / "tasks" / "install_operator.yml"

# 比較対象の最低件数。これを下回ったら抽出が壊れているとみなす。
MIN_COMPARED = 15


def ansible_available():
    if shutil.which("ansible-playbook"):
        return True
    try:
        import ansible  # noqa: F401
        return True
    except ImportError:
        return False


def ansible_cmd():
    if shutil.which("ansible-playbook"):
        return ["ansible-playbook"]
    return [sys.executable, "-m", "ansible", "playbook"]


def materialize_inventory(src_name, dst_dir):
    """`.sample` 拡張子を外した inventory を作る（Ansible は .yml しか読まない）"""
    src = INVENTORY_DIR / src_name
    if not src.is_dir():
        return None
    dst = Path(dst_dir) / src_name
    for p in src.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(src)
        name = rel.name[: -len(".sample")] if rel.name.endswith(".sample") else rel.name
        target = dst / rel.parent / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(p, target)
    return dst


def _iter_tasks(obj):
    """block/rescue/always を含めてタスクを再帰的に辿る"""
    if isinstance(obj, list):
        for e in obj:
            yield from _iter_tasks(e)
    elif isinstance(obj, dict):
        yield obj
        for key in ("block", "rescue", "always"):
            if key in obj:
                yield from _iter_tasks(obj[key])


# 通常デプロイでは走らない role。render.py も扱わないため対象外。
# keycloak_admin_recovery は spec の部分パッチ（instances: 0 等）を
# definition: として書いており、完全なリソース定義ではない。
EXCLUDED_ROLES = {"keycloak_admin_recovery"}


def _task_files():
    for p in sorted(ROLES_DIR.rglob("tasks/*.yml")):
        if "uninstall" in p.name or p.name.startswith("_"):
            continue
        if p.relative_to(ROLES_DIR).parts[0] in EXCLUDED_ROLES:
            continue
        if p == INSTALL_OPERATOR:
            continue
        yield p


def collect_definitions():
    """(出所, definition dict, 追加 vars) のリストを返す"""
    found = []

    # --- 各 role のタスク内 definition: ---
    for path in _task_files():
        try:
            doc = yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            continue
        for task in _iter_tasks(doc):
            if not isinstance(task, dict):
                continue
            mod = task.get("kubernetes.core.k8s")
            if not isinstance(mod, dict):
                continue
            definition = mod.get("definition")
            if not isinstance(definition, dict):
                continue
            if mod.get("state") == "absent":
                continue
            if "loop" in task or "with_items" in task:
                continue  # item 依存は再現できない
            src = f"{path.relative_to(ROLES_DIR)}: {task.get('name', '(no name)')}"
            found.append((src, definition, task.get("vars") or {}))

    # --- common/tasks/install_operator.yml を呼び出し側の vars で展開 ---
    if INSTALL_OPERATOR.exists():
        op_defs = []
        for task in _iter_tasks(yaml.safe_load(INSTALL_OPERATOR.read_text())):
            if isinstance(task, dict) and isinstance(
                task.get("kubernetes.core.k8s"), dict
            ):
                d = task["kubernetes.core.k8s"].get("definition")
                if isinstance(d, dict):
                    op_defs.append((task.get("name", "?"), d))

        for path in _task_files():
            try:
                doc = yaml.safe_load(path.read_text())
            except yaml.YAMLError:
                continue
            for task in _iter_tasks(doc):
                if not isinstance(task, dict):
                    continue
                inc = task.get("ansible.builtin.include_tasks")
                if not isinstance(inc, str) or "install_operator.yml" not in inc:
                    continue
                call_vars = task.get("vars") or {}
                if "operator_name" not in call_vars:
                    continue
                for tname, d in op_defs:
                    src = (f"common/tasks/install_operator.yml: {tname} "
                           f"<- {path.relative_to(ROLES_DIR)} "
                           f"({call_vars.get('operator_name')})")
                    found.append((src, d, call_vars))

    return found


def _role_defaults():
    """全 role の defaults/main.yml をマージして返す。

    role defaults は play レベルでは見えないため明示的に渡す必要がある。
    ただし inventory より優先してはいけないので、呼び出し側で
    inventory が定義しているキーを除いてから使う。
    """
    out = {}
    for p in sorted(ROLES_DIR.glob("*/defaults/main.yml")):
        try:
            d = yaml.safe_load(p.read_text()) or {}
        except yaml.YAMLError:
            continue
        for k, v in d.items():
            out.setdefault(k, v)
    return out


def _inventory_keys(invdir):
    keys = set()
    for p in (Path(invdir) / "group_vars" / "all").glob("*.yml"):
        try:
            d = yaml.safe_load(p.read_text()) or {}
        except (yaml.YAMLError, UnicodeDecodeError):
            # vault 暗号化や非 UTF-8 のファイルは無視する
            continue
        if not isinstance(d, dict):
            continue
        keys |= set(d)
    return keys


def render_with_ansible(items, invdir, workdir):
    """定義を本物の Ansible でテンプレート評価する。

    定義ごとに 1 タスクを生成する（loop + 動的 vars は不安定なため）。
    変数未定義などで失敗したものはスキップし、理由を返す。
    """
    out_dir = Path(workdir) / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    # inventory が持つキーは上書きしない（inventory を優先させる）
    base_vars = {k: v for k, v in _role_defaults().items()
                 if k not in _inventory_keys(invdir)}

    # 定義は JSON のまま Ansible に渡し、include_vars + to_json で解決させる。
    # YAML テキストに書き出すと "{{ {'k': v} }}" のクォートが壊れ、
    # リストを返す式が文字列化されてしまうため（k8s モジュールと同じ経路にする）。
    defs_file = Path(workdir) / "defs.json"
    defs_file.write_text(json.dumps({"items": [d for _s, d, _v in items]}))

    tasks = [{
        "name": "load definitions",
        "ansible.builtin.include_vars": {"file": str(defs_file), "name": "defs"},
    }]
    for i, (_src, _definition, extra_vars) in enumerate(items):
        tasks.append({
            "name": f"render {i:03d}",
            "ansible.builtin.copy": {
                "content": "{{ defs['items'][%d] | to_json }}" % i,
                "dest": f"{out_dir}/{i:03d}.json",
                "mode": "0644",
            },
            "vars": {**base_vars, **extra_vars},
            "ignore_errors": True,
        })

    pb = Path(workdir) / "render.yml"
    pb.write_text(yaml.safe_dump([{
        "hosts": "localhost",
        "connection": "local",
        "gather_facts": False,
        "tasks": tasks,
    }], default_flow_style=False, allow_unicode=True, width=10**6))

    env = {**os.environ,
           "ANSIBLE_COLLECTIONS_PATH": os.environ.get(
               "ANSIBLE_COLLECTIONS_PATH",
               str(Path.home() / ".ansible" / "collections")),
           "ANSIBLE_DISPLAY_SKIPPED_HOSTS": "false",
           "ANSIBLE_DEPRECATION_WARNINGS": "false"}
    subprocess.run(
        ansible_cmd() + [str(pb), "-i", str(invdir)],
        capture_output=True, cwd=str(ANSIBLE_DIR), env=env,
    )

    rendered, skipped = {}, []
    for i, (src, _definition, _extra) in enumerate(items):
        f = out_dir / f"{i:03d}.json"
        if not f.exists():
            skipped.append((src, "テンプレート評価に失敗（実行時ファクト依存）"))
            continue
        try:
            doc = json.loads(f.read_text())
        except json.JSONDecodeError as e:
            skipped.append((src, f"JSON パース失敗: {e}"))
            continue
        if not isinstance(doc, dict) or "kind" not in doc:
            skipped.append((src, "kind が解決できない"))
            continue
        meta = doc.get("metadata") or {}
        name = meta.get("name")
        if not name or "{{" in str(name):
            skipped.append((src, "name が解決できない"))
            continue
        rendered[(doc["kind"], meta.get("namespace", ""), name)] = (doc, src)
    return rendered, skipped


def render_py_resources(invdir, outdir):
    r = subprocess.run(
        [sys.executable, str(RENDER_PY), "-c", str(invdir), "-o", str(outdir)],
        capture_output=True, cwd=str(ANSIBLE_DIR),
    )
    assert r.returncode == 0, r.stderr.decode()
    out = {}
    for f in (Path(outdir) / "manifests").rglob("*.yml"):
        for doc in yaml.safe_load_all(f.read_text()):
            if not isinstance(doc, dict) or "kind" not in doc:
                continue
            meta = doc.get("metadata") or {}
            if not meta.get("name"):
                continue
            out[(doc["kind"], meta.get("namespace", ""), meta["name"])] = doc
    return out


def mask_secret_values(doc):
    """Secret の値は Ansible と render.py で別々に生成されるためマスクする。

    パスワードは両者が独立に生成する（Ansible は .generated-passwords.yml、
    render.py は自前生成）ので値の一致は原理的にあり得ない。
    一方でキー構成の変更（キー名の追加・削除・リネーム）は検知したいので、
    値だけ伏せてキーは比較する。
    """
    if doc.get("kind") != "Secret":
        return doc
    out = json.loads(json.dumps(doc))
    for field in ("data", "stringData"):
        if isinstance(out.get(field), dict):
            out[field] = {k: "<MASKED>" for k in out[field]}
    return out


def _diff(expected, actual):
    import difflib
    a = json.dumps(expected, sort_keys=True, indent=1, ensure_ascii=False).splitlines()
    b = json.dumps(actual, sort_keys=True, indent=1, ensure_ascii=False).splitlines()
    return list(difflib.unified_diff(a, b, "ansible", "render.py", lineterm="", n=2))


@unittest.skipUnless(ansible_available(), "ansible-core が無い環境ではスキップ")
class TestInlineDefinitionParity(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.items = collect_definitions()
        cls.results = {}
        for inv in INVENTORIES:
            tmp = tempfile.mkdtemp()
            cls.results[inv] = {"tmp": tmp}
            invdir = materialize_inventory(inv, tmp)
            if invdir is None:
                cls.results[inv] = None
                continue
            rendered, skipped = render_with_ansible(cls.items, invdir, tmp)
            cls.results[inv] = {
                "tmp": tmp,
                "ansible": rendered,
                "skipped": skipped,
                "render_py": render_py_resources(invdir, Path(tmp) / "render-out"),
            }

    @classmethod
    def tearDownClass(cls):
        for r in cls.results.values():
            if r:
                shutil.rmtree(r["tmp"], ignore_errors=True)

    def test_definitions_were_extracted(self):
        """抽出が壊れて no-op になっていないこと"""
        self.assertGreaterEqual(
            len(self.items), 50,
            f"definition: の抽出数が少なすぎます ({len(self.items)})。"
            "role のタスク構造が変わった可能性があります",
        )

    def test_inline_definitions_match_render_py(self):
        """両方に存在するリソースは内容が一致すること"""
        total_compared = 0
        for inv in INVENTORIES:
            res = self.results.get(inv)
            if res is None:
                continue
            common = set(res["ansible"]) & set(res["render_py"])
            total_compared += len(common)
            for key in sorted(common):
                ans_doc, src = res["ansible"][key]
                rnd_doc = res["render_py"][key]
                ans_doc = mask_secret_values(ans_doc)
                rnd_doc = mask_secret_values(rnd_doc)
                with self.subTest(inventory=inv, resource=key, source=src):
                    self.assertEqual(
                        ans_doc, rnd_doc,
                        f"{src}\n差分:\n" + "\n".join(_diff(ans_doc, rnd_doc)),
                    )
        self.assertGreaterEqual(
            total_compared, MIN_COMPARED,
            f"比較できたリソースが {total_compared} 件しかありません。"
            "抽出かレンダリングが壊れている可能性があります",
        )

    def test_report_coverage(self):
        """カバレッジを出力する（失敗はさせない。状況把握用）"""
        for inv in INVENTORIES:
            res = self.results.get(inv)
            if res is None:
                continue
            a, r = set(res["ansible"]), set(res["render_py"])
            print(f"\n[{inv}] 抽出 {len(self.items)} / "
                  f"Ansible 側で解決 {len(a)} / render.py {len(r)} / "
                  f"突き合わせ {len(a & r)} / スキップ {len(res['skipped'])}")
            for src, why in res["skipped"]:
                print(f"    skip: {src}  ({why})")
            only_a = sorted(a - r)
            if only_a:
                print(f"    Ansible のみ ({len(only_a)}): "
                      + ", ".join(f"{k[0]}/{k[2]}" for k in only_a[:8]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
