#!/usr/bin/env python3
"""
Ansible テンプレートと render-manifests/render.py の出力一致を検証する。

CLAUDE.md の方針どおり render.py は Ansible role のロジックを Python で再実装した
ものなので、両者がずれると「Ansible で入れた環境」と「手順書で入れた環境」で
別物ができあがる。実際に過去、以下の乖離が起きている:

  - chat_template_configmap が空のとき、Ansible は volumeMounts/volumes の
    キー自体を省略するのに render.py は [] を出していた
  - model_defaults.vllm_args 廃止後、render.py 側だけ旧既定値を持っていた

そのため「目視で同期したつもり」ではなく、実際に両方をレンダリングして
LLMInferenceService と MaaSModelRef を構造比較する。

Ansible 側は jinja2 を直接呼ぶのではなく、playbooks/llm_show_manifest.yml を
実際の ansible-playbook で実行して得る。jinja2 を自前フィルタで模倣すると
スタブと本物がずれたときに検出できないため（過去に combine の
recursive 有無で実際にずれた）。ansible-core が無い環境ではスキップする。

比較対象の範囲:
  Ansible 側は 10 個の Jinja テンプレートと 69 箇所のタスク内 definition: で
  リソースを作っている。後者は when: 条件や register した実行時ファクト
  （cluster_domain、Proxy status、OBC 認証情報など）に依存するため、
  クラスタ無しには再現できず比較できない。
  ここで比較するのは inventory の変数だけで決まるテンプレート:
    - llminferenceservice.yml.j2
    - maas-policies.yml.j2
  パスワードやクラスタ由来の値を含むテンプレート
  (postgres / keycloak-instance / realm-import / datasciencecluster) は、
  両者で値が一致しないため対象外。

実行:
    python3 -m unittest tests.test_render_parity
"""

import fcntl
import json
import os
import subprocess
import sys
import shutil
import tempfile
import unittest
from pathlib import Path

# Fix non-blocking IO for aw container
for fd in [sys.stdout, sys.stderr]:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)

import yaml
from jinja2 import Environment, FileSystemLoader

ANSIBLE_DIR = Path(__file__).resolve().parent.parent
LLM_TEMPLATES = ANSIBLE_DIR / "roles" / "llm_serving" / "templates"
LLM_DEFAULTS = ANSIBLE_DIR / "roles" / "llm_serving" / "defaults" / "main.yml"
MODEL_RECIPES = ANSIBLE_DIR / "roles" / "llm_serving" / "vars" / "model_recipes.yml"
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




def combine_recursive(a, b):
    """Ansible の combine(recursive=True) 相当"""
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = combine_recursive(out[k], v)
        else:
            out[k] = v
    return out


def strip_meta(d):
    """レシピの _source / _notes / _verified_on は設定値ではないので除く"""
    return {k: v for k, v in d.items() if not k.startswith("_")}


def make_jinja_env():
    env = Environment(loader=FileSystemLoader(str(LLM_TEMPLATES)))
    env.filters["combine"] = combine_recursive
    env.filters["to_json"] = json.dumps
    env.filters["default"] = lambda val, d="": val if val else d
    return env


def load_components(inv_name):
    base = INVENTORY_DIR / inv_name / "group_vars" / "all"
    for candidate in ("components.yml", "components.yml.sample"):
        p = base / candidate
        if p.exists():
            return yaml.safe_load(p.read_text())
    return None


def resolve_model_cfg(defaults, recipes, entry):
    """roles/llm_serving/tasks/main.yml と同じ解決順で設定をマージする"""
    cfg = combine_recursive(defaults, strip_meta(recipes.get("_fallback", {})))
    cfg = combine_recursive(cfg, strip_meta(recipes.get(entry.get("hf_repo", ""), {})))
    return combine_recursive(cfg, entry)


def ansible_available():
    return shutil.which("ansible-playbook") is not None or _ansible_module()


def _ansible_module():
    try:
        import ansible  # noqa: F401
        return True
    except ImportError:
        return False


def ansible_cmd():
    """ansible-playbook の実行方法を返す（entry point が無ければ -m ansible）"""
    if shutil.which("ansible-playbook"):
        return ["ansible-playbook"]
    return [sys.executable, "-m", "ansible", "playbook"]


def materialize_inventory(src_name, dst_dir):
    """`.sample` 拡張子を外した inventory を作る。

    Ansible の group_vars は `.yml` しか読まないため、inventory/sample を
    そのまま -i に渡しても変数が空になる。テストでは実体化して使う。
    戻り値は inventory ディレクトリのパス。対象が無ければ None。
    """
    src = INVENTORY_DIR / src_name
    if not src.is_dir():
        return None
    dst = Path(dst_dir) / src_name
    (dst / "group_vars" / "all").mkdir(parents=True, exist_ok=True)
    for p in src.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(src)
        name = rel.name[: -len(".sample")] if rel.name.endswith(".sample") else rel.name
        target = dst / rel.parent / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            # バイナリや非 UTF-8 のファイルが混ざっていても落ちないようにする
            shutil.copyfile(p, target)
    return dst


class TestRenderParity(unittest.TestCase):
    """Ansible テンプレートと render.py の生成物が一致すること"""

    @classmethod
    def setUpClass(cls):
        cls.defaults = yaml.safe_load(LLM_DEFAULTS.read_text())["model_defaults"]
        cls.recipes = yaml.safe_load(MODEL_RECIPES.read_text())["model_recipes"]
        cls.template = make_jinja_env().get_template("llminferenceservice.yml.j2")

    def _render_with_render_py_dir(self, invdir, outdir):
        r = subprocess.run(
            [sys.executable, str(RENDER_PY), "-c", str(invdir), "-o", str(outdir)],
            capture_output=True, cwd=str(ANSIBLE_DIR),
        )
        self.assertEqual(r.returncode, 0,
                         f"render.py が失敗しました:\n{r.stderr.decode()}")

    def _render_with_render_py(self, inv_name, outdir):
        r = subprocess.run(
            [sys.executable, str(RENDER_PY),
             "-c", str(INVENTORY_DIR / inv_name), "-o", str(outdir)],
            capture_output=True, cwd=str(ANSIBLE_DIR),
        )
        self.assertEqual(
            r.returncode, 0,
            f"render.py が inventory/{inv_name} で失敗しました:\n{r.stderr.decode()}",
        )

    @staticmethod
    def _extract(path, kind):
        if not path.exists():
            return None
        for doc in yaml.safe_load_all(path.read_text()):
            if doc and doc.get("kind") == kind:
                return doc
        return None

    @unittest.skipUnless(ansible_available(), "ansible-core が無い環境ではスキップ")
    def test_llminferenceservice_matches_real_ansible(self):
        """本物の ansible-playbook の出力と render.py の出力を比較する"""
        for inv in INVENTORIES:
            with tempfile.TemporaryDirectory() as tmp:
                invdir = materialize_inventory(inv, tmp)
                if invdir is None:
                    continue
                components = yaml.safe_load(
                    (invdir / "group_vars" / "all" / "components.yml").read_text()
                )
                models = components.get("models") or {}

                # --- Ansible 側 ---
                ans_dir = Path(tmp) / "ansible-out"
                r = subprocess.run(
                    ansible_cmd() + [
                        str(ANSIBLE_DIR / "playbooks" / "llm_show_manifest.yml"),
                        "-i", str(invdir),
                        "-e", f"dest={ans_dir}",
                        "-e", "all_models=true",
                    ],
                    capture_output=True, cwd=str(ANSIBLE_DIR),
                    env={**os.environ,
                         "ANSIBLE_COLLECTIONS_PATH":
                             os.environ.get("ANSIBLE_COLLECTIONS_PATH",
                                            str(Path.home() / ".ansible" / "collections"))},
                )
                self.assertEqual(
                    r.returncode, 0,
                    f"llm_show_manifest.yml が inventory/{inv} で失敗:\n"
                    f"{r.stdout.decode()[-3000:]}",
                )

                # --- render.py 側 ---
                rnd_dir = Path(tmp) / "render-out"
                self._render_with_render_py_dir(invdir, rnd_dir)

                checked = 0
                for name, entry in models.items():
                    if entry.get("state", "present") != "present":
                        continue
                    checked += 1
                    ans_file = ans_dir / f"{name}-llminferenceservice.yml"
                    with self.subTest(inventory=inv, model=name):
                        self.assertTrue(
                            ans_file.exists(),
                            f"Ansible が {name} の manifest を出力していません",
                        )
                        expected = yaml.safe_load(ans_file.read_text())
                        actual = self._extract(
                            rnd_dir / "manifests" / "09-llm-serving"
                            / f"model-{name}-deploy.yml",
                            "LLMInferenceService",
                        )
                        self.assertIsNotNone(
                            actual,
                            f"render.py が {name} の LLMInferenceService を生成していません",
                        )
                        self.assertEqual(
                            expected, actual,
                            "Ansible と render.py の出力が一致しません。\n"
                            "差分:\n" + "\n".join(_diff(expected, actual)),
                        )
                with self.subTest(inventory=inv):
                    self.assertGreater(checked, 0,
                                       f"inventory/{inv} に present なモデルがありません")

    def test_maasmodelref_matches(self):
        """MaaSModelRef は role 側がタスク内で直接定義しているので値だけ照合する"""
        for inv in INVENTORIES:
            components = load_components(inv)
            if components is None:
                continue
            with tempfile.TemporaryDirectory() as tmp:
                outdir = Path(tmp) / "out"
                self._render_with_render_py(inv, outdir)

                for name, entry in (components.get("models") or {}).items():
                    if entry.get("state", "present") != "present":
                        continue
                    cfg = resolve_model_cfg(self.defaults, self.recipes, entry)
                    ref = self._extract(
                        outdir / "manifests" / "09-llm-serving"
                        / f"model-{name}-deploy.yml",
                        "MaaSModelRef",
                    )
                    with self.subTest(inventory=inv, model=name):
                        self.assertIsNotNone(ref)
                        self.assertEqual(ref["metadata"]["name"], name)
                        self.assertEqual(ref["metadata"]["namespace"], cfg["namespace"])
                        self.assertEqual(ref["spec"]["modelRef"]["name"], name)
                        self.assertEqual(
                            ref["spec"]["modelRef"]["kind"], "LLMInferenceService"
                        )
                        self.assertEqual(ref["spec"]["tenantRef"], "models-as-a-service")

    @unittest.skipUnless(ansible_available(), "ansible-core が無い環境ではスキップ")
    def test_maas_policies_matches_real_ansible(self):
        """MaaSAuthPolicy / MaaSSubscription / RBAC の生成物を比較する"""
        render_pb = """---
- hosts: localhost
  connection: local
  gather_facts: false
  tasks:
    - ansible.builtin.include_vars:
        file: "{{ role_defaults }}"
        name: _rd
    - ansible.builtin.set_fact:
        model_defaults: "{{ _rd.model_defaults }}"
    - ansible.builtin.copy:
        content: "{{ lookup('template', tpl) }}"
        dest: "{{ dest }}"
        mode: "0644"
"""
        for inv in INVENTORIES:
            with tempfile.TemporaryDirectory() as tmp:
                invdir = materialize_inventory(inv, tmp)
                if invdir is None:
                    continue

                pb = Path(tmp) / "render-policies.yml"
                pb.write_text(render_pb)
                out = Path(tmp) / "maas-policies.yml"
                r = subprocess.run(
                    ansible_cmd() + [
                        str(pb), "-i", str(invdir),
                        "-e", f"tpl={ANSIBLE_DIR}/roles/keycloak/templates/maas-policies.yml.j2",
                        "-e", f"role_defaults={LLM_DEFAULTS}",
                        "-e", f"dest={out}",
                    ],
                    capture_output=True, cwd=str(ANSIBLE_DIR),
                    env={**os.environ,
                         "ANSIBLE_COLLECTIONS_PATH":
                             os.environ.get("ANSIBLE_COLLECTIONS_PATH",
                                            str(Path.home() / ".ansible" / "collections"))},
                )
                self.assertEqual(
                    r.returncode, 0,
                    f"maas-policies のレンダリングに失敗 (inventory/{inv}):\n"
                    f"{r.stdout.decode()[-3000:]}",
                )

                rnd_dir = Path(tmp) / "render-out"
                self._render_with_render_py_dir(invdir, rnd_dir)
                rnd_file = rnd_dir / "manifests" / "07-integration" / "maas-policies.yml"

                expected = _by_key(yaml.safe_load_all(out.read_text()))
                actual = _by_key(yaml.safe_load_all(rnd_file.read_text()))

                # MaasTenantConfig は Ansible 側ではテンプレートではなく
                # manage_maas_access.yml のタスクで作っている。
                # render.py は同じ manifest にまとめているので、
                # 出所の違いを吸収してタスク定義と突き合わせる。
                tenant_key = ("MaasTenantConfig", "redhat-ods-applications",
                              "default-tenant")
                if tenant_key in actual:
                    with self.subTest(inventory=inv, resource=tenant_key):
                        self.assertEqual(
                            _tenant_config_from_task(), actual.pop(tenant_key),
                            "MaasTenantConfig が manage_maas_access.yml の定義と"
                            "一致しません",
                        )

                with self.subTest(inventory=inv):
                    self.assertEqual(
                        sorted(expected), sorted(actual),
                        "生成されるリソースの種類/名前が一致しません\n"
                        f"Ansible のみ: {sorted(set(expected) - set(actual))}\n"
                        f"render.py のみ: {sorted(set(actual) - set(expected))}",
                    )
                for key in sorted(expected):
                    with self.subTest(inventory=inv, resource=key):
                        self.assertEqual(
                            expected[key], actual[key],
                            "差分:\n" + "\n".join(_diff(expected[key], actual[key])),
                        )

    @unittest.skipUnless(ansible_available(), "ansible-core が無い環境ではスキップ")
    def test_inventory_model_defaults_override_is_honored(self):
        """inventory の model_defaults による上書きが両方に効くこと

        Ansible は inventory の group_vars が role defaults に優先する。
        render.py が role defaults 相当を固定で持っていると、この上書きが
        無視されて確認用 manifest と実際の配信設定がずれる。
        """
        base = INVENTORIES[0] if INVENTORIES else None
        self.assertIsNotNone(base, "inventory がありません")

        with tempfile.TemporaryDirectory() as tmp:
            invdir = materialize_inventory(base, tmp)
            comp = invdir / "group_vars" / "all" / "components.yml"
            data = yaml.safe_load(comp.read_text())

            # 上書きを注入する
            data["model_defaults"] = {
                "namespace": "custom-serving",
                "resources": {"limits": {"memory": "64Gi"}},
            }
            # レシピが resources を持たないモデルを対象にする
            # （レシピ側の値が勝つのは正しい挙動のため）
            target = None
            for name, entry in (data.get("models") or {}).items():
                if entry.get("state", "present") != "present":
                    continue
                recipe = self.recipes.get(entry.get("hf_repo", ""), {})
                if "resources" not in recipe:
                    target = name
                    break
            if target is None:
                # 全モデルがレシピで resources を持つ場合は素のモデルを足す
                target = "parity-probe"
                data.setdefault("models", {})[target] = {
                    "hf_repo": "org/parity-probe", "state": "present"}
            comp.write_text(yaml.safe_dump(data, allow_unicode=True))

            ans_dir = Path(tmp) / "ansible-out"
            r = subprocess.run(
                ansible_cmd() + [
                    str(ANSIBLE_DIR / "playbooks" / "llm_show_manifest.yml"),
                    "-i", str(invdir), "-e", f"dest={ans_dir}", "-e", "all_models=true",
                ],
                capture_output=True, cwd=str(ANSIBLE_DIR),
                env={**os.environ,
                     "ANSIBLE_COLLECTIONS_PATH": os.environ.get(
                         "ANSIBLE_COLLECTIONS_PATH",
                         str(Path.home() / ".ansible" / "collections"))},
            )
            self.assertEqual(r.returncode, 0, r.stdout.decode()[-2000:])

            rnd_dir = Path(tmp) / "render-out"
            self._render_with_render_py_dir(invdir, rnd_dir)

            ans = yaml.safe_load(
                (ans_dir / f"{target}-llminferenceservice.yml").read_text())
            rnd = self._extract(
                rnd_dir / "manifests" / "09-llm-serving"
                / f"model-{target}-deploy.yml", "LLMInferenceService")

            # 上書きが Ansible 側に効いていること
            self.assertEqual(ans["metadata"]["namespace"], "custom-serving",
                             "Ansible が inventory の model_defaults を無視しています")
            self.assertEqual(
                ans["spec"]["template"]["containers"][0]["resources"]["limits"]["memory"],
                "64Gi")
            # render.py も同じであること
            self.assertIsNotNone(rnd, "render.py が manifest を生成していません")
            self.assertEqual(ans, rnd,
                             "差分:\n" + "\n".join(_diff(ans, rnd)))

    def test_render_py_defaults_have_no_vllm_args(self):
        """vLLM 引数は model_recipes 側で持つ方針。render.py が独自に持たないこと"""
        src = RENDER_PY.read_text()
        self.assertIn("_load_model_recipes", src,
                      "render.py が model_recipes.yml を読んでいません")
        self.assertIn("model_recipes.yml", src)


def _tenant_config_from_task():
    """manage_maas_access.yml の "Ensure MaasTenantConfig exists" の定義を取り出す"""
    path = ANSIBLE_DIR / "roles" / "keycloak" / "tasks" / "manage_maas_access.yml"
    for task in yaml.safe_load(path.read_text()):
        if task.get("name") == "Ensure MaasTenantConfig exists":
            return task["kubernetes.core.k8s"]["definition"]
    raise AssertionError("Ensure MaasTenantConfig exists タスクが見つかりません")


def _by_key(docs):
    """マルチドキュメント YAML を (kind, namespace, name) をキーにした dict にする"""
    out = {}
    for d in docs:
        if not d:
            continue
        m = d.get("metadata", {})
        out[(d.get("kind"), m.get("namespace", ""), m.get("name"))] = d
    return out


def _diff(expected, actual):
    import difflib
    a = json.dumps(expected, sort_keys=True, indent=1, ensure_ascii=False).splitlines()
    b = json.dumps(actual, sort_keys=True, indent=1, ensure_ascii=False).splitlines()
    return list(difflib.unified_diff(a, b, "ansible", "render.py", lineterm="", n=2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
