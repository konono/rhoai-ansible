#!/usr/bin/env python3
"""
models 変数リファクタリングの統合テスト

テスト対象:
1. テンプレートレンダリング (maas-policies, llminferenceservice, download-job)
2. 変数解決ロジック (model_defaults マージ、state フィルタリング)
3. preflight バリデーションロジック
4. Ansible 構文チェック
5. エッジケース (全 absent, 単一モデル, purge フラグ)
"""

import fcntl
import os
import re
import subprocess
import sys
import unittest

# Fix non-blocking IO for aw container
for fd in [sys.stdout, sys.stderr]:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)

from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader

ANSIBLE_DIR = Path(__file__).resolve().parent.parent
KEYCLOAK_TEMPLATES = ANSIBLE_DIR / "roles" / "keycloak" / "templates"
LLM_TEMPLATES = ANSIBLE_DIR / "roles" / "llm_serving" / "templates"
LLM_DEFAULTS = ANSIBLE_DIR / "roles" / "llm_serving" / "defaults" / "main.yml"
COMPONENTS_MYENV = (
    ANSIBLE_DIR / "inventory" / "myenv" / "group_vars" / "all" / "components.yml"
)
COMPONENTS_SAMPLE = (
    ANSIBLE_DIR
    / "inventory"
    / "sample"
    / "group_vars"
    / "all"
    / "components.yml.sample"
)

MODEL_DEFAULTS = {
    "namespace": "llm-serving",
    "state": "present",
    "vllm_image": "vllm/vllm-openai:v0.28.0",
    "tool_call_parser": "qwen3_coder",
    "reasoning_parser": "",
    "chat_template_configmap": "qwen3-chat-template",
    "gpu_count": 1,
    "vllm_extra_args": ["--max-model-len=4096", "--enable-auto-tool-choice"],
    "purge": False,
}


def make_jinja_env(template_dir):
    env = Environment(loader=FileSystemLoader(str(template_dir)))
    env.filters["combine"] = lambda a, b: {**a, **b}
    env.filters["default"] = lambda val, d="": val if val else d
    return env


class TestDefaultsFile(unittest.TestCase):
    """roles/llm_serving/defaults/main.yml の検証"""

    def test_defaults_file_exists(self):
        self.assertTrue(LLM_DEFAULTS.exists())

    def test_defaults_has_model_defaults(self):
        data = yaml.safe_load(LLM_DEFAULTS.read_text())
        self.assertIn("model_defaults", data)
        md = data["model_defaults"]
        for key in [
            "namespace",
            "state",
            "vllm_image",
            "tool_call_parser",
            "reasoning_parser",
            "chat_template_configmap",
            "gpu_count",
            "vllm_extra_args",
            "purge",
        ]:
            self.assertIn(key, md, f"model_defaults missing key: {key}")

    def test_defaults_values(self):
        data = yaml.safe_load(LLM_DEFAULTS.read_text())
        md = data["model_defaults"]
        self.assertEqual(md["namespace"], "llm-serving")
        self.assertEqual(md["state"], "present")
        self.assertEqual(md["gpu_count"], 1)
        self.assertFalse(md["purge"])


class TestComponentsYml(unittest.TestCase):
    """components.yml の検証"""

    @unittest.skipUnless(COMPONENTS_MYENV.exists(), "inventory/myenv not available")
    def test_myenv_has_models(self):
        data = yaml.safe_load(COMPONENTS_MYENV.read_text())
        self.assertIn("models", data)
        self.assertIsInstance(data["models"], dict)
        self.assertGreater(len(data["models"]), 0)

    @unittest.skipUnless(COMPONENTS_MYENV.exists(), "inventory/myenv not available")
    def test_myenv_no_old_variables(self):
        content = COMPONENTS_MYENV.read_text()
        old_vars = [
            "llm_model_name",
            "llm_hf_repo",
            "llm_vllm_image",
            "llm_tool_call_parser",
            "llm_reasoning_parser",
            "llm_chat_template_configmap",
            "llm_gpu_count",
            "llm_vllm_extra_args",
            "keycloak_models",
        ]
        for var in old_vars:
            self.assertNotIn(
                f"{var}:", content, f"Old variable {var} still in components.yml"
            )

    @unittest.skipUnless(COMPONENTS_MYENV.exists(), "inventory/myenv not available")
    def test_myenv_models_have_hf_repo(self):
        data = yaml.safe_load(COMPONENTS_MYENV.read_text())
        for name, cfg in data["models"].items():
            self.assertIn("hf_repo", cfg, f"models.{name} missing hf_repo")

    @unittest.skipUnless(COMPONENTS_MYENV.exists(), "inventory/myenv not available")
    def test_myenv_models_rfc1123(self):
        data = yaml.safe_load(COMPONENTS_MYENV.read_text())
        pattern = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
        for name in data["models"]:
            self.assertTrue(
                pattern.match(name), f"models key '{name}' not RFC 1123 compliant"
            )

    def test_sample_has_models(self):
        data = yaml.safe_load(COMPONENTS_SAMPLE.read_text())
        self.assertIn("models", data)

    def test_sample_no_keycloak_models(self):
        content = COMPONENTS_SAMPLE.read_text()
        self.assertNotIn("keycloak_models:", content)

    @unittest.skipUnless(COMPONENTS_MYENV.exists(), "inventory/myenv not available")
    def test_keycloak_groups_reference_models_keys(self):
        """keycloak_groups[].models の値が models の dict キーに含まれるか検証"""
        data = yaml.safe_load(COMPONENTS_MYENV.read_text())
        model_names = set(data["models"].keys())
        for group_name, group_cfg in data.get("keycloak_groups", {}).items():
            for model_ref in group_cfg.get("models", []):
                if model_ref != "*":
                    self.assertIn(
                        model_ref,
                        model_names,
                        f"keycloak_groups.{group_name}.models references '{model_ref}' "
                        f"but it's not in models dict (available: {model_names})",
                    )


class TestMaasPoliciesTemplate(unittest.TestCase):
    """maas-policies.yml.j2 テンプレートの検証"""

    def setUp(self):
        self.env = make_jinja_env(KEYCLOAK_TEMPLATES)
        self.template = self.env.get_template("maas-policies.yml.j2")
        self.keycloak_groups = {
            "maas-admins": {
                "models": ["*"],
                "priority": 20,
                "quota_tokens_24h": 5000000,
            },
            "maas-qwen3-06b-users": {
                "models": ["qwen3-06b"],
                "priority": 10,
                "quota_tokens_24h": 1000000,
            },
            "maas-qwen3-4b-users": {
                "models": ["qwen3-4b"],
                "priority": 10,
                "quota_tokens_24h": 1000000,
            },
        }

    def _render(self, models):
        rendered = self.template.render(
            models=models,
            model_defaults=MODEL_DEFAULTS,
            keycloak_groups=self.keycloak_groups,
        )
        return [d for d in yaml.safe_load_all(rendered) if d]

    def test_mixed_state_filters_absent(self):
        """absent モデルは modelRefs に含まれない"""
        models = {
            "qwen3-06b": {"hf_repo": "Qwen/Qwen3-0.6B", "state": "absent"},
            "qwen3-4b": {"hf_repo": "Qwen/Qwen3-4B", "state": "present"},
        }
        docs = self._render(models)
        all_refs = []
        for doc in docs:
            refs = doc.get("spec", {}).get("modelRefs", [])
            all_refs.extend([r["name"] for r in refs])
        self.assertNotIn("qwen3-06b", all_refs)
        self.assertIn("qwen3-4b", all_refs)

    def test_absent_only_group_skipped(self):
        """全モデルが absent のグループの AuthPolicy/Subscription はスキップ"""
        models = {
            "qwen3-06b": {"hf_repo": "Qwen/Qwen3-0.6B", "state": "absent"},
            "qwen3-4b": {"hf_repo": "Qwen/Qwen3-4B", "state": "present"},
        }
        docs = self._render(models)
        names = [d["metadata"]["name"] for d in docs]
        self.assertNotIn("maas-qwen3-06b-users-auth-policy", names)
        self.assertNotIn("maas-qwen3-06b-users-subscription", names)

    def test_all_present(self):
        """全モデル present の場合、全グループのポリシーが生成される"""
        models = {
            "qwen3-06b": {"hf_repo": "Qwen/Qwen3-0.6B", "state": "present"},
            "qwen3-4b": {"hf_repo": "Qwen/Qwen3-4B", "state": "present"},
        }
        docs = self._render(models)
        names = [d["metadata"]["name"] for d in docs]
        self.assertEqual(len(docs), 6)
        self.assertIn("admin-auth-policy", names)
        self.assertIn("maas-qwen3-06b-users-auth-policy", names)
        self.assertIn("maas-qwen3-4b-users-auth-policy", names)
        self.assertIn("maas-admins-subscription", names)

    def test_all_absent(self):
        """全モデル absent の場合、admin-auth-policy のみ（modelRefs 空）"""
        models = {
            "qwen3-06b": {"hf_repo": "Qwen/Qwen3-0.6B", "state": "absent"},
            "qwen3-4b": {"hf_repo": "Qwen/Qwen3-4B", "state": "absent"},
        }
        docs = self._render(models)
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["metadata"]["name"], "admin-auth-policy")

    def test_wildcard_expands_to_present_only(self):
        """'*' は present モデルのみに展開される"""
        models = {
            "qwen3-06b": {"hf_repo": "Qwen/Qwen3-0.6B", "state": "absent"},
            "qwen3-4b": {"hf_repo": "Qwen/Qwen3-4B", "state": "present"},
        }
        docs = self._render(models)
        admin_sub = [
            d for d in docs if d["metadata"]["name"] == "maas-admins-subscription"
        ]
        self.assertEqual(len(admin_sub), 1)
        refs = admin_sub[0]["spec"]["modelRefs"]
        ref_names = [r["name"] for r in refs]
        self.assertEqual(ref_names, ["qwen3-4b"])

    def test_namespace_resolution(self):
        """namespace は model_defaults からマージされる"""
        models = {
            "qwen3-4b": {"hf_repo": "Qwen/Qwen3-4B", "state": "present"},
        }
        docs = self._render(models)
        admin = [d for d in docs if d["metadata"]["name"] == "admin-auth-policy"][0]
        ref = admin["spec"]["modelRefs"][0]
        self.assertEqual(ref["namespace"], "llm-serving")

    def test_custom_namespace(self):
        """モデル固有の namespace が反映される"""
        models = {
            "qwen3-4b": {
                "hf_repo": "Qwen/Qwen3-4B",
                "state": "present",
                "namespace": "custom-ns",
            },
        }
        docs = self._render(models)
        admin = [d for d in docs if d["metadata"]["name"] == "admin-auth-policy"][0]
        ref = admin["spec"]["modelRefs"][0]
        self.assertEqual(ref["namespace"], "custom-ns")

    def test_valid_yaml(self):
        """レンダリング結果が有効な YAML"""
        models = {
            "qwen3-06b": {"hf_repo": "Qwen/Qwen3-0.6B", "state": "present"},
            "qwen3-4b": {"hf_repo": "Qwen/Qwen3-4B", "state": "present"},
        }
        rendered = self.template.render(
            models=models,
            model_defaults=MODEL_DEFAULTS,
            keycloak_groups=self.keycloak_groups,
        )
        docs = list(yaml.safe_load_all(rendered))
        for doc in docs:
            if doc:
                self.assertIn("apiVersion", doc)
                self.assertIn("kind", doc)
                self.assertIn("metadata", doc)

    def test_quota_values(self):
        """トークンクォータ値が正しく設定される"""
        models = {
            "qwen3-4b": {"hf_repo": "Qwen/Qwen3-4B", "state": "present"},
        }
        docs = self._render(models)
        admin_sub = [
            d for d in docs if d["metadata"]["name"] == "maas-admins-subscription"
        ][0]
        limit = admin_sub["spec"]["modelRefs"][0]["tokenRateLimits"][0]["limit"]
        self.assertEqual(limit, 5000000)


class TestLLMInferenceServiceTemplate(unittest.TestCase):
    """llminferenceservice.yml.j2 テンプレートの検証"""

    def setUp(self):
        self.env = make_jinja_env(LLM_TEMPLATES)
        self.template = self.env.get_template("llminferenceservice.yml.j2")

    def _render(self, model_name, model_cfg):
        cfg = {**MODEL_DEFAULTS, **model_cfg}
        rendered = self.template.render(_model_name=model_name, _model_cfg=cfg)
        return yaml.safe_load(rendered)

    def test_model_name_in_metadata(self):
        doc = self._render("qwen3-4b", {"hf_repo": "Qwen/Qwen3-4B"})
        self.assertEqual(doc["metadata"]["name"], "qwen3-4b")

    def test_model_uri(self):
        doc = self._render("qwen3-4b", {"hf_repo": "Qwen/Qwen3-4B"})
        self.assertEqual(doc["spec"]["model"]["uri"], "pvc://hf-model-cache/qwen3-4b")

    def test_served_model_name(self):
        doc = self._render("qwen3-4b", {"hf_repo": "Qwen/Qwen3-4B"})
        args = doc["spec"]["template"]["containers"][0]["args"]
        self.assertIn("--served-model-name=qwen3-4b", args)

    def test_tool_call_parser(self):
        doc = self._render(
            "qwen3-4b",
            {"hf_repo": "Qwen/Qwen3-4B", "tool_call_parser": "hermes"},
        )
        args = doc["spec"]["template"]["containers"][0]["args"]
        self.assertIn("--tool-call-parser=hermes", args)

    def test_reasoning_parser_omitted_when_empty(self):
        doc = self._render(
            "qwen3-4b",
            {"hf_repo": "Qwen/Qwen3-4B", "reasoning_parser": ""},
        )
        args = doc["spec"]["template"]["containers"][0]["args"]
        reasoning_args = [a for a in args if "--reasoning-parser" in a]
        self.assertEqual(len(reasoning_args), 0)

    def test_reasoning_parser_included_when_set(self):
        doc = self._render(
            "qwen3-06b",
            {"hf_repo": "Qwen/Qwen3-0.6B", "reasoning_parser": "qwen3"},
        )
        args = doc["spec"]["template"]["containers"][0]["args"]
        self.assertIn("--reasoning-parser=qwen3", args)

    def test_extra_args(self):
        doc = self._render(
            "qwen3-4b",
            {
                "hf_repo": "Qwen/Qwen3-4B",
                "vllm_extra_args": ["--max-model-len=8192", "--custom-flag"],
            },
        )
        args = doc["spec"]["template"]["containers"][0]["args"]
        self.assertIn("--max-model-len=8192", args)
        self.assertIn("--custom-flag", args)

    def test_vllm_image(self):
        doc = self._render(
            "qwen3-4b",
            {"hf_repo": "Qwen/Qwen3-4B", "vllm_image": "custom/vllm:v1.0"},
        )
        image = doc["spec"]["template"]["containers"][0]["image"]
        self.assertEqual(image, "custom/vllm:v1.0")

    def test_chat_template_configmap(self):
        doc = self._render(
            "qwen3-4b",
            {
                "hf_repo": "Qwen/Qwen3-4B",
                "chat_template_configmap": "my-template",
            },
        )
        vol = doc["spec"]["template"]["volumes"][0]
        self.assertEqual(vol["configMap"]["name"], "my-template")

    def test_namespace(self):
        doc = self._render(
            "qwen3-4b",
            {"hf_repo": "Qwen/Qwen3-4B", "namespace": "custom-ns"},
        )
        self.assertEqual(doc["metadata"]["namespace"], "custom-ns")


class TestDownloadJobTemplate(unittest.TestCase):
    """download-job.yml.j2 テンプレートの検証"""

    def setUp(self):
        self.env = make_jinja_env(LLM_TEMPLATES)
        self.template = self.env.get_template("download-job.yml.j2")

    def test_job_name(self):
        cfg = {**MODEL_DEFAULTS, "hf_repo": "Qwen/Qwen3-4B"}
        rendered = self.template.render(_model_name="qwen3-4b", _model_cfg=cfg)
        doc = yaml.safe_load(rendered)
        self.assertEqual(doc["metadata"]["name"], "download-qwen3-4b")

    def test_hf_repo_in_command(self):
        cfg = {**MODEL_DEFAULTS, "hf_repo": "Qwen/Qwen3-4B"}
        rendered = self.template.render(_model_name="qwen3-4b", _model_cfg=cfg)
        doc = yaml.safe_load(rendered)
        cmd = doc["spec"]["template"]["spec"]["containers"][0]["command"][2]
        self.assertIn("Qwen/Qwen3-4B", cmd)
        self.assertIn("/models/qwen3-4b", cmd)

    def test_namespace(self):
        cfg = {**MODEL_DEFAULTS, "hf_repo": "Qwen/Qwen3-4B", "namespace": "custom-ns"}
        rendered = self.template.render(_model_name="qwen3-4b", _model_cfg=cfg)
        doc = yaml.safe_load(rendered)
        self.assertEqual(doc["metadata"]["namespace"], "custom-ns")


class TestPreflightLogic(unittest.TestCase):
    """preflight バリデーションロジックの検証"""

    @staticmethod
    def _first_present_model(models):
        """preflight の set_fact ロジックを再現"""
        present = [k for k, v in models.items() if v.get("state") == "present"]
        no_state = [k for k, v in models.items() if "state" not in v]
        candidates = present + no_state
        return candidates[0] if candidates else ""

    def test_first_present_model(self):
        models = {
            "qwen3-06b": {"hf_repo": "Qwen/Qwen3-0.6B", "state": "absent"},
            "qwen3-4b": {"hf_repo": "Qwen/Qwen3-4B", "state": "present"},
        }
        self.assertEqual(self._first_present_model(models), "qwen3-4b")

    def test_all_absent_returns_empty(self):
        models = {
            "qwen3-06b": {"hf_repo": "Qwen/Qwen3-0.6B", "state": "absent"},
        }
        self.assertEqual(self._first_present_model(models), "")

    def test_no_state_defaults_to_present(self):
        models = {
            "qwen3-06b": {"hf_repo": "Qwen/Qwen3-0.6B"},
        }
        self.assertEqual(self._first_present_model(models), "qwen3-06b")

    def test_rfc1123_valid(self):
        pattern = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
        for name in ["qwen3-06b", "deepseek-r1", "llama3.1-8b", "a"]:
            self.assertTrue(pattern.match(name), f"{name} should be valid")

    def test_rfc1123_invalid(self):
        pattern = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
        for name in ["Qwen3-0.6B", "qwen3_5", "-bad", "bad-", "", "Bad"]:
            self.assertFalse(pattern.match(name), f"{name} should be invalid")


class TestModelDefaultsMerge(unittest.TestCase):
    """model_defaults と models エントリのマージ動作の検証"""

    def test_override_specific_field(self):
        model_entry = {"hf_repo": "Qwen/Qwen3-4B", "tool_call_parser": "hermes"}
        merged = {**MODEL_DEFAULTS, **model_entry}
        self.assertEqual(merged["tool_call_parser"], "hermes")
        self.assertEqual(merged["vllm_image"], "vllm/vllm-openai:v0.28.0")

    def test_defaults_preserved(self):
        model_entry = {"hf_repo": "Qwen/Qwen3-4B", "state": "present"}
        merged = {**MODEL_DEFAULTS, **model_entry}
        self.assertEqual(merged["gpu_count"], 1)
        self.assertEqual(merged["namespace"], "llm-serving")
        self.assertFalse(merged["purge"])

    def test_all_fields_overridden(self):
        model_entry = {
            "hf_repo": "org/model",
            "state": "present",
            "namespace": "custom",
            "vllm_image": "custom:v1",
            "tool_call_parser": "hermes",
            "reasoning_parser": "deep",
            "chat_template_configmap": "custom-tpl",
            "gpu_count": 4,
            "vllm_extra_args": ["--flag"],
            "purge": True,
        }
        merged = {**MODEL_DEFAULTS, **model_entry}
        for k, v in model_entry.items():
            self.assertEqual(merged[k], v, f"Field {k} not properly overridden")


class TestNoOldVariableReferences(unittest.TestCase):
    """リファクタリング対象ファイルに旧変数が残っていないことを確認"""

    OLD_VARS = [
        "llm_model_name",
        "llm_hf_repo",
        "llm_vllm_image",
        "llm_tool_call_parser",
        "llm_reasoning_parser",
        "llm_chat_template_configmap",
        "llm_gpu_count",
        "llm_vllm_extra_args",
        "keycloak_models",
    ]

    def _check_file_no_old_vars(self, filepath, allowed=None):
        allowed = allowed or []
        content = filepath.read_text()
        for var in self.OLD_VARS:
            if var in allowed:
                continue
            # Check for variable usage (not just mentions in comments/strings)
            lines = content.split("\n")
            for i, line in enumerate(lines, 1):
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                if f"{var}" in line:
                    self.fail(
                        f"Old variable '{var}' found in {filepath.name}:{i}: {line.strip()}"
                    )

    def test_deploy_model_no_old_vars(self):
        self._check_file_no_old_vars(
            ANSIBLE_DIR / "roles" / "llm_serving" / "tasks" / "deploy_model.yml"
        )

    def test_main_yml_no_old_vars(self):
        self._check_file_no_old_vars(
            ANSIBLE_DIR / "roles" / "llm_serving" / "tasks" / "main.yml"
        )

    def test_llminferenceservice_no_old_vars(self):
        self._check_file_no_old_vars(
            ANSIBLE_DIR
            / "roles"
            / "llm_serving"
            / "templates"
            / "llminferenceservice.yml.j2"
        )

    def test_download_job_no_old_vars(self):
        self._check_file_no_old_vars(
            ANSIBLE_DIR
            / "roles"
            / "llm_serving"
            / "templates"
            / "download-job.yml.j2"
        )

    def test_maas_resources_no_old_vars(self):
        self._check_file_no_old_vars(
            ANSIBLE_DIR / "roles" / "maas_resources" / "tasks" / "main.yml"
        )

    def test_maas_policies_template_no_old_vars(self):
        self._check_file_no_old_vars(
            ANSIBLE_DIR / "roles" / "keycloak" / "templates" / "maas-policies.yml.j2"
        )

    def test_preflight_allowed_backward_compat(self):
        """preflight は llm_model_name を後方互換のために使う — それ以外は禁止"""
        filepath = ANSIBLE_DIR / "roles" / "preflight" / "tasks" / "main.yml"
        content = filepath.read_text()
        for var in self.OLD_VARS:
            if var == "llm_model_name":
                continue
            for i, line in enumerate(content.split("\n"), 1):
                if line.lstrip().startswith("#"):
                    continue
                if var in line:
                    self.fail(
                        f"Old variable '{var}' in preflight/main.yml:{i}: {line.strip()}"
                    )


class TestAnsibleSyntax(unittest.TestCase):
    """Ansible 構文チェック"""

    def _syntax_check(self, playbook):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                f"""
import fcntl, os, sys
for fd in [sys.stdout, sys.stderr]:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
os.execvp('{sys.executable}', ['{sys.executable}', '-m', 'ansible', 'playbook',
    '--syntax-check', '{playbook}', '-i', 'inventory/sample/hosts.yml'])
""",
            ],
            capture_output=True,
            text=True,
            cwd=str(ANSIBLE_DIR),
        )
        return result

    def test_site_yml(self):
        r = self._syntax_check("site.yml")
        self.assertEqual(r.returncode, 0, f"Syntax error in site.yml:\n{r.stderr}")

    def test_llm_add_model(self):
        r = self._syntax_check("playbooks/llm_add_model.yml")
        self.assertEqual(
            r.returncode, 0,
            f"Syntax error in llm_add_model.yml:\n{r.stderr}",
        )

    def test_verify(self):
        r = self._syntax_check("playbooks/verify.yml")
        self.assertEqual(
            r.returncode, 0, f"Syntax error in verify.yml:\n{r.stderr}"
        )

    def test_uninstall(self):
        r = self._syntax_check("playbooks/uninstall.yml")
        self.assertEqual(
            r.returncode, 0, f"Syntax error in uninstall.yml:\n{r.stderr}"
        )


class TestRemoveModelTaskFile(unittest.TestCase):
    """remove_model.yml タスクファイルの検証"""

    def test_file_exists(self):
        path = ANSIBLE_DIR / "roles" / "llm_serving" / "tasks" / "remove_model.yml"
        self.assertTrue(path.exists())

    def test_has_required_tasks(self):
        path = ANSIBLE_DIR / "roles" / "llm_serving" / "tasks" / "remove_model.yml"
        content = path.read_text()
        self.assertIn("Delete LLMInferenceService", content)
        self.assertIn("Delete MaaSModelRef", content)
        self.assertIn("Purge model data from PVC", content)

    def test_uses_model_vars(self):
        path = ANSIBLE_DIR / "roles" / "llm_serving" / "tasks" / "remove_model.yml"
        content = path.read_text()
        self.assertIn("_model_name", content)
        self.assertIn("_model_cfg", content)

    def test_purge_conditional(self):
        path = ANSIBLE_DIR / "roles" / "llm_serving" / "tasks" / "remove_model.yml"
        content = path.read_text()
        self.assertIn("_model_cfg.purge", content)


class TestMainYmlModelLoop(unittest.TestCase):
    """llm_serving/tasks/main.yml のモデルループ構造の検証"""

    def setUp(self):
        self.content = (
            ANSIBLE_DIR / "roles" / "llm_serving" / "tasks" / "main.yml"
        ).read_text()

    def test_absent_before_present(self):
        """absent モデルが present より先に処理される"""
        absent_pos = self.content.index("Process absent models")
        present_pos = self.content.index("Deploy present models")
        self.assertLess(absent_pos, present_pos)

    def test_loops_over_models(self):
        self.assertIn("models | dict2items", self.content)

    def test_includes_remove_model(self):
        self.assertIn("remove_model.yml", self.content)

    def test_includes_deploy_model(self):
        self.assertIn("deploy_model.yml", self.content)

    def test_passes_model_vars(self):
        self.assertIn("_model_name:", self.content)
        self.assertIn("_model_cfg:", self.content)
        self.assertIn("model_defaults | combine", self.content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
