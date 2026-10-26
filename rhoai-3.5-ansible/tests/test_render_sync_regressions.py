#!/usr/bin/env python3
"""
Ansible role 側だけ直して render-manifests/render.py を忘れる事故を機械検出する。

tests/test_render_parity.py は llminferenceservice / maas-policies の
テンプレートを対象にしているが、「タスク内 definition: で作るリソース」と
「手順書のシェル片」は対象外になっている。実際、その穴から 2 回連続で
同期漏れを出した (2026-10-26):

  1. guardrails の修正 (#15) を role のテンプレートにだけ入れ、render.py には
     古い定義 (extra_body ネスト + 死んだ define user) が残った。
     manifest 経路では #15 がそのまま再現する状態だった
  2. limitador のスクレイプ経路を role に足したが、手順書の適用ループが
     `0[1-4]*.yaml` + `06*.yaml` の決め打ちで、新規の
     07-limitador-platform-scrape.yaml が永久に apply されなかった。
     openshift-rhcl への namespace ラベル付与も手順書に無かった

どちらも「目視で同期したつもり」で通り抜けている。ここでは可能な限り
**両側を実際に突き合わせる**形にして、片側だけ変えたら落ちるようにする。
期待値をテスト内に直書きすると、両側を同時に間違えたときに検出できず、
また正当な変更のたびにテストを書き換えることになるため避けている。

クラスタ不要。inventory/sample だけで完結する。

実行:
    python3 -m unittest tests.test_render_sync_regressions
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ANSIBLE_DIR = Path(__file__).resolve().parent.parent
RENDER_PY = ANSIBLE_DIR / "render-manifests" / "render.py"
OBS_FILES = ANSIBLE_DIR / "roles" / "observability" / "files"
GUARDRAILS_TPL = (ANSIBLE_DIR / "roles" / "guardrails" / "templates"
                  / "nemo-guardrails-config.yml.j2")

#: 手順書に出てくる、レンダリング結果のディレクトリ名
OBS_MANIFEST_DIR = "manifests/13-observability"

#: ClusterPolicy が既に在るときだけ適用を見送る特例ファイル。
#: 手順書でも個別に扱われるので、一括適用ループの対象外で正しい。
CONDITIONAL_OBS_FILES = {"05-gpu-utilization.yaml"}


def _walk(node):
    """ネストした dict/list を再帰的に辿る。"""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def find_k8s_definitions(task_file: Path, kind: str):
    """role のタスク YAML から definition: の中の指定 kind を拾う。

    Jinja 式はそのまま文字列として残る。値まで比較したい場面では
    Jinja を含まないキーだけを見ること。
    """
    doc = yaml.safe_load(task_file.read_text(encoding="utf-8"))
    return [n for n in _walk(doc)
            if isinstance(n, dict) and n.get("kind") == kind and "apiVersion" in n]


def _inventory_with_all_roles(tmpdir: Path) -> Path:
    """inventory/sample を複製し、install_* を全て有効にしたものを返す。

    sample は guardrails を無効にしているため、そのまま使うと guardrails の
    manifest が生成されず肝心の比較が skip される（= 事故を見逃す）。
    CI では sample だけが git 管理されているので、他の inventory に依存せず
    ここで複製して上書きする。
    """
    src = ANSIBLE_DIR / "inventory" / "sample"
    dst = tmpdir / "inv"
    shutil.copytree(src, dst)
    comp = dst / "group_vars" / "all" / "components.yml"
    if not comp.exists():  # sample は .sample 拡張子で置かれていることがある
        sample = dst / "group_vars" / "all" / "components.yml.sample"
        if sample.exists():
            shutil.copy(sample, comp)
    text = comp.read_text(encoding="utf-8")
    text = re.sub(r"^(install_guardrails:)\s*false\s*$", r"\1 true",
                  text, flags=re.MULTILINE)
    comp.write_text(text, encoding="utf-8")
    return dst


def render_guide_and_manifests(tmpdir: Path):
    """render.py を実行し、(guide, manifest_root) を返す."""
    out = tmpdir / "out"
    inv = _inventory_with_all_roles(tmpdir)
    proc = subprocess.run(
        [sys.executable, str(RENDER_PY), "-c", str(inv), "-o", str(out)],
        capture_output=True, text=True, cwd=str(ANSIBLE_DIR),
    )
    if proc.returncode != 0:
        raise AssertionError(f"render.py failed: {proc.stdout[-800:]}\n{proc.stderr[-800:]}")
    return (out / "manual-deploy-guide.md").read_text(encoding="utf-8"), out / "manifests"


def render_guardrails_from_role(model_name: str, enable_thinking: bool = False) -> dict:
    """Ansible 側の guardrails テンプレートを Jinja2 で描画して ConfigMap を返す。"""
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader(str(GUARDRAILS_TPL.parent)),
                      keep_trailing_newline=True)
    rendered = env.get_template(GUARDRAILS_TPL.name).render(
        llm_model_name=model_name,
        guardrails_enable_thinking=enable_thinking,
    )
    return yaml.safe_load(rendered)


def guardrails_cm_from_render_py(manifest_root: Path) -> dict:
    path = manifest_root / "12-guardrails" / "guardrails.yml"
    if not path.exists():
        raise unittest.SkipTest("guardrails manifest が生成されていない "
                                "(inventory で install_guardrails: false)")
    docs = [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]
    return next(d for d in docs if d["kind"] == "ConfigMap")


def normalize_colang(text: str):
    """Colang をコメント・空行・インデント差を無視して比較できる形にする。"""
    return [ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


class RenderSyncBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="render-sync-")
        cls.guide, cls.manifests = render_guide_and_manifests(Path(cls._tmp))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)


class TestGuardrailsParity(RenderSyncBase):
    """#15 の再発防止。role のテンプレートと render.py の出力を突き合わせる。"""

    def _both_sides(self):
        cm_render = guardrails_cm_from_render_py(self.manifests)
        model = yaml.safe_load(cm_render["data"]["config.yaml"])["models"][0]["model"]
        cm_role = render_guardrails_from_role(model)
        return cm_role, cm_render

    def test_config_yaml_matches(self):
        """models / rails / prompts が完全一致すること。"""
        cm_role, cm_render = self._both_sides()
        a = yaml.safe_load(cm_role["data"]["config.yaml"])
        b = yaml.safe_load(cm_render["data"]["config.yaml"])
        for key in sorted(set(a) | set(b)):
            self.assertEqual(
                a.get(key), b.get(key),
                f"guardrails config.yaml の '{key}' が role と render.py で食い違っている。"
                f"\n  role     : {str(a.get(key))[:400]}"
                f"\n  render.py: {str(b.get(key))[:400]}")

    def test_rails_co_matches(self):
        cm_role, cm_render = self._both_sides()
        self.assertEqual(normalize_colang(cm_role["data"]["rails.co"]),
                         normalize_colang(cm_render["data"]["rails.co"]),
                         "rails.co が role と render.py で食い違っている")

    def test_chat_template_kwargs_is_not_nested_under_extra_body(self):
        """#15 原因 2。

        nemoguardrails 0.23+ は models[].parameters の未知キーをそのまま HTTP
        ボディへ転送する。extra_body: でネストすると vLLM には extra_body という
        未知フィールドが届いて黙って無視され、思考が切れないまま 200 OK が返る。
        """
        for label, cm in zip(("role", "render.py"), self._both_sides()):
            params = yaml.safe_load(cm["data"]["config.yaml"])["models"][0]["parameters"]
            self.assertNotIn(
                "extra_body", params,
                f"{label}: chat_template_kwargs を extra_body でネストしてはいけない "
                f"(vLLM に届かず #15 が再発する)")
            self.assertIn(
                "chat_template_kwargs", params,
                f"{label}: parameters 直下に chat_template_kwargs が必要")

    def test_no_user_canonical_form_in_colang(self):
        """#15 原因 1。

        `define user ...` を 1 つでも書くと nemoguardrails は passthrough を
        やめて dialog rail モードに入り、rail に掛からない通常の質問が
        完了しなくなる (actions/llm/generation.py generate_user_intent の
        `if self.config.user_messages:` 分岐)。
        有害プロンプトのブロックは input rail が担当するので不要。
        """
        for label, cm in zip(("role", "render.py"), self._both_sides()):
            offending = [ln for ln in normalize_colang(cm["data"]["rails.co"])
                         if ln.startswith("define user ")]
            self.assertEqual(
                [], offending,
                f"{label}: rails.co に user canonical form がある。"
                f" dialog rail が起動し #15 が再発する: {offending}")


class TestClusterPolicyParity(RenderSyncBase):
    """DCGM の ServiceMonitor が片側だけ落ちないようにする。"""

    def _role_dcgm(self):
        defs = find_k8s_definitions(
            ANSIBLE_DIR / "roles" / "gpu_operator" / "tasks" / "main.yml", "ClusterPolicy")
        self.assertTrue(defs, "gpu_operator role に ClusterPolicy の定義が見つからない")
        return defs[0]["spec"]["dcgmExporter"]

    def _render_dcgm(self):
        path = self.manifests / "03-deps-gpu" / "02-clusterpolicy.yml"
        doc = next(d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d)
        return doc["spec"]["dcgmExporter"]

    def test_dcgm_exporter_matches(self):
        self.assertEqual(
            self._role_dcgm(), self._render_dcgm(),
            "ClusterPolicy の dcgmExporter が role と render.py で食い違っている")

    def test_dcgm_service_monitor_enabled(self):
        """これが無いと DCGM_FI_DEV_* が収集されず GPU 系が全パネル 0 になる。

        observability role の 05-gpu-utilization.yaml にも同じ設定があるが、
        そちらは ClusterPolicy 未作成時しか適用されず、site.yml では
        gpu_operator が先に CR を作るため常に skip される。
        有効なのは gpu_operator role 側だけ。
        """
        for label, dcgm in (("role", self._role_dcgm()), ("render.py", self._render_dcgm())):
            self.assertIs(
                dcgm.get("serviceMonitor", {}).get("enabled"), True,
                f"{label}: dcgmExporter.serviceMonitor.enabled が true でない")

    def test_optional_mig_strategy_matches_role(self):
        """MIG の指定と未指定を、role の実式と renderer の出力で比較する。"""
        import importlib.util
        from ansible.parsing.dataloader import DataLoader
        from ansible.template import Templar
        try:
            from ansible.template import trust_as_template
        except ImportError:
            def trust_as_template(value):
                return value

        spec = importlib.util.spec_from_file_location("render_mig_test", RENDER_PY)
        renderer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(renderer)
        definition = find_k8s_definitions(
            ANSIBLE_DIR / "roles/gpu_operator/tasks/main.yml", "ClusterPolicy")[0]
        expression = definition["spec"]["mig"]
        for strategy in (None, "single", "mixed"):
            with self.subTest(strategy=strategy), tempfile.TemporaryDirectory() as tmp:
                cfg = renderer.load_config(ANSIBLE_DIR / "inventory/sample/group_vars/all")
                variables = {"omit": "__omitted__"}
                if strategy is not None:
                    cfg["gpu_mig_strategy"] = strategy
                    variables["gpu_mig_strategy"] = strategy
                role_value = Templar(loader=DataLoader(), variables=variables).template(
                    trust_as_template(expression))
                out = Path(tmp)
                renderer.ManifestRenderer(cfg, out, ANSIBLE_DIR / "inventory/sample").render_gpu_operator()
                rendered = yaml.safe_load(
                    (out / "manifests/03-deps-gpu/02-clusterpolicy.yml").read_text())
                if strategy is None:
                    self.assertEqual("__omitted__", role_value)
                    self.assertNotIn("mig", rendered["spec"])
                else:
                    self.assertEqual({"strategy": strategy}, role_value)
                    self.assertEqual(role_value, rendered["spec"]["mig"])


class TestObservabilityGuideCoverage(RenderSyncBase):
    """「静的ファイルを足したのに手順書が apply しない」を防ぐ。"""

    def test_every_static_file_is_applied_by_the_guide(self):
        """roles/observability/files/*.yaml が手順書で必ず適用されること。

        過去、適用ループが `0[1-4]*.yaml` + `06*.yaml` の決め打ちだったため
        07-limitador-platform-scrape.yaml が漏れた。

        期待集合は **role の元ファイル** から作る。レンダリング結果から作ると、
        renderer がファイルを出力しなくなった変異を検出できない
        (出力が減れば期待も減るので常に通ってしまう)。
        """
        expected = sorted(p.name for p in OBS_FILES.glob("*.yaml")
                          if p.name != "kustomization.yaml")
        self.assertTrue(expected, "roles/observability/files に静的ファイルが無い")

        rendered = sorted(p.name for p in (self.manifests / "13-observability").glob("*.yaml"))
        missing = [n for n in expected if n not in rendered]
        self.assertEqual(
            [], missing,
            f"role に在る静的ファイルが render.py の出力に無い: {missing}")

        # 手順書が一括適用に使っている glob を取り出す
        globs = re.findall(rf"for f in (?:{re.escape(OBS_MANIFEST_DIR)}/\S+\s*)+;", self.guide)
        self.assertTrue(globs, "手順書に observability の一括適用ループが見つからない")
        # `for f in .../0*.yaml;` の末尾 `;` を取り除いてから glob として扱う
        patterns = [p.rstrip(";")
                    for p in re.findall(rf"{re.escape(OBS_MANIFEST_DIR)}/(\S+)", globs[0])]

        import fnmatch
        for name in expected:
            if name in CONDITIONAL_OBS_FILES:
                self.assertIn(name, self.guide,
                              f"{name} は条件付き適用だが手順書に一切登場しない")
                continue
            covered = any(fnmatch.fnmatch(name, pat) for pat in patterns)
            self.assertTrue(
                covered,
                f"{name} が手順書の適用ループ {patterns} に含まれていない。"
                f" レンダリングされるだけで誰も apply しない状態になる")

    def test_conditional_file_is_excluded_from_bulk_loop(self):
        """05 は ClusterPolicy 未作成時のみ。一括ループに入っていては困る。"""
        self.assertRegex(
            self.guide, r"05-gpu-utilization\.yaml\) continue",
            "05-gpu-utilization.yaml が一括適用ループから除外されていない")


class TestObservabilitySideEffectsInGuide(RenderSyncBase):
    """manifest では表現できず、手順書のコマンドとして持つしかないもの。"""

    def test_openshift_rhcl_is_labeled_for_platform_monitoring(self):
        """limitador を platform Prometheus に拾わせるための namespace ラベル。

        PodMonitor だけ置いてもこのラベルが無いと収集されず、
        Rate Limit 系パネルが恒久的に空になる。
        UWM は openshift-* namespace を対象外にする仕様なので代替できない。
        """
        self.assertIn("oc label namespace openshift-rhcl", self.guide)
        self.assertIn("openshift.io/cluster-monitoring=true", self.guide)
        # role 側にも同じ意図のタスクがあること
        obs_tasks = (ANSIBLE_DIR / "roles" / "observability" / "tasks" / "main.yml").read_text()
        self.assertIn("openshift.io/cluster-monitoring", obs_tasks,
                      "role 側に namespace ラベル付与のタスクが無い")

    def test_duplicate_gateway_podmonitor_is_removed(self):
        """odh-model-controller 製の PodMonitor と二重スクレイプすると istio 系が 2 倍になる。"""
        self.assertIn("oc delete podmonitor maas-gateway-istio-metrics", self.guide)
        obs_tasks = (ANSIBLE_DIR / "roles" / "observability" / "tasks" / "main.yml").read_text()
        self.assertIn("maas-gateway-istio-metrics", obs_tasks,
                      "role 側に重複 PodMonitor の削除タスクが無い")

    def test_user_workload_monitoring_is_enabled(self):
        """user-workload-monitoring-config だけでは UWM は起動しない。

        enableUserWorkload を立てないと llm-serving (ユーザー namespace) の
        PodMonitor が誰にも見られず vllm:* が丸ごと欠落する。
        """
        path = self.manifests / "13-observability" / "00-enable-uwm.yml"
        self.assertTrue(path.exists(), "cluster-monitoring-config の manifest が無い")
        doc = next(d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d)
        self.assertIs(yaml.safe_load(doc["data"]["config.yaml"]).get("enableUserWorkload"), True)

        obs_tasks = (ANSIBLE_DIR / "roles" / "observability" / "tasks" / "main.yml").read_text()
        self.assertIn("enableUserWorkload", obs_tasks,
                      "role 側に UWM 有効化のタスクが無い")


class TestUwmMergeShell(RenderSyncBase):
    """生成された UWM 有効化シェルを模擬 oc で実行し、既存設定を壊さないか見る。

    テキスト処理で済ませていた旧実装は、末尾改行の無い既存値に対して
    `retention: 15denableUserWorkload: true` という壊れた YAML を書き戻し、
    しかも終了コード 0 で成功に見えていた (レビュー指摘)。
    """

    #: 壊しやすい既存設定のバリエーション。
    #: 下 3 つはレビューで「行追記では壊れる」と実証された形。
    CASES = {
        "no_trailing_newline": 'prometheus:\n  retention: 15d',
        "flow_style": '{prometheus: {retention: 15d}}\n',
        "doc_start_flow_style": '---\n{prometheusK8s: {retention: 15d}}\n',
        "doc_end_marker": 'prometheusK8s:\n  retention: 15d\n...\n',
        "value_on_next_line": 'enableUserWorkload:\n  false\nprometheusK8s:\n  retention: 15d\n',
        "already_true": 'enableUserWorkload: true\nprometheus:\n  retention: 15d\n',
        "already_false": 'enableUserWorkload: false\nprometheus:\n  retention: 15d\n',
    }

    def _extract_snippet(self) -> str:
        """手順書から UWM 有効化のブロックだけ取り出す。"""
        start = self.guide.index("CMO_ERR=$(mktemp)")
        end = self.guide.index("oc apply -f manifests/13-observability/02-uwm.yml", start)
        return self.guide[start:end]

    def _run_with_mock_oc(self, existing: str, with_pyyaml: bool,
                          fail_get: bool = False, fail_set: bool = False,
                          silent_empty_merge: bool = False,
                          not_found: bool = False, fail_apply: bool = False):
        """模擬 oc を PATH に置いて生成シェルを実行し、書き戻された内容を返す。

        PyYAML の有無で実装が分岐するので、両方を実行できるよう python3 も
        差し替える。bastion に PyYAML が無いことは実際にあり
        (本開発機の system python3 がまさにそうだった)、フォールバック側が
        壊れていても気づけない。
        """
        work = Path(tempfile.mkdtemp(prefix="uwm-"))
        try:
            (work / "current").write_text(existing, encoding="utf-8")
            written = work / "written"
            mock = work / "bin"
            mock.mkdir()

            # 既存 CM を ConfigMap JSON として返す模擬 oc。
            # 実装は -o json で 1 回だけ取得する形なので、それに合わせる。
            (work / "cm.json").write_text(
                json.dumps({"apiVersion": "v1", "kind": "ConfigMap",
                            "metadata": {"name": "cluster-monitoring-config"},
                            "data": {"config.yaml": existing}}), encoding="utf-8")
            oc = mock / "oc"
            oc.write_text(
                "#!/usr/bin/env bash\n"
                f'WORK="{work}"\n'
                f'FAIL_GET={"1" if fail_get else "0"}\n'
                f'FAIL_SET={"1" if fail_set else "0"}\n'
                f'NOT_FOUND={"1" if not_found else "0"}\n'
                f'FAIL_APPLY={"1" if fail_apply else "0"}\n'
                'if [ "$1" = "get" ] && [ "$2" = "cm" ]; then\n'
                '  if [ "$FAIL_GET" = "1" ]; then\n'
                '    echo "Error from server: etcdserver: request timed out" >&2; exit 1\n'
                '  fi\n'
                '  if [ "$NOT_FOUND" = "1" ]; then\n'
                '    echo \'Error from server (NotFound): configmaps "x" not found\' >&2; exit 1\n'
                '  fi\n'
                '  cat "$WORK/cm.json"; exit 0\n'
                'fi\n'
                'if [ "$1" = "apply" ]; then\n'
                '  if [ "$FAIL_APPLY" = "1" ]; then\n'
                '    echo "Error from server: apply denied" >&2; exit 1\n'
                '  fi\n'
                '  touch "$WORK/applied"; exit 0\n'
                'fi\n'
                'if [ "$1" = "set" ] && [ "$2" = "data" ]; then\n'
                '  if [ "$FAIL_SET" = "1" ]; then\n'
                '    echo "Error from server: admission webhook denied the request" >&2; exit 1\n'
                '  fi\n'
                '  for a in "$@"; do\n'
                '    case "$a" in --from-file=config.yaml=*)\n'
                '      src="${a#--from-file=config.yaml=}"\n'
                '      [ -f "$src" ] || { echo "mock oc: missing $src" >&2; exit 1; }\n'
                '      cp "$src" "$WORK/written" ;;\n'
                '    esac\n'
                '  done\n'
                '  exit 0\n'
                'fi\n'
                'exit 0\n', encoding="utf-8")
            oc.chmod(0o755)

            py = mock / "python3"
            if silent_empty_merge:
                # import yaml は通すが、マージ本体は何も書かずに成功して返る。
                # 書き戻し直前の非空チェックが効いているかを見るため。
                py.write_text(
                    "#!/usr/bin/env bash\n"
                    'if [ "$1" = "-c" ]; then exit 0; fi\n'
                    'cat >/dev/null 2>&1 || true\n'
                    'exit 0\n', encoding="utf-8")
            elif with_pyyaml:
                # PyYAML を持つインタプリタ (このテストを動かしている処理系) へ委譲
                py.write_text(f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n',
                              encoding="utf-8")
            else:
                # import yaml だけ失敗させ、他はそのまま通す
                py.write_text(
                    "#!/usr/bin/env bash\n"
                    'if [ "$1" = "-c" ] && [ "$2" = "import yaml" ]; then exit 1; fi\n'
                    f'exec "{sys.executable}" "$@"\n', encoding="utf-8")
            py.chmod(0o755)

            env = dict(os.environ, PATH=f"{mock}:{os.environ['PATH']}")
            proc = subprocess.run(["bash", "-c", self._extract_snippet()],
                                  capture_output=True, text=True, env=env)
            out = written.read_text(encoding="utf-8") if written.exists() else None
            self._applied = (work / "applied").exists()
            return proc, out
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_existing_config_is_preserved_and_flag_is_set(self):
        """PyYAML がある正常系。どの既存形でも他キーを保って true になること。"""
        for name, existing in self.CASES.items():
            with self.subTest(case=name):
                proc, written = self._run_with_mock_oc(existing, with_pyyaml=True)
                self.assertEqual(0, proc.returncode,
                                 f"{name}: 生成シェルが失敗した\n{proc.stderr[-500:]}")
                self.assertIsNotNone(written, f"{name}: 書き戻しが発生していない")

                parsed = yaml.safe_load(written)
                self.assertIsInstance(
                    parsed, dict,
                    f"{name}: 書き戻した内容が YAML として読めない\n---\n{written}\n---")
                self.assertIs(parsed.get("enableUserWorkload"), True,
                              f"{name}: enableUserWorkload が true になっていない")

                original = yaml.safe_load(existing) or {}
                for key, value in original.items():
                    if key == "enableUserWorkload":
                        continue
                    self.assertEqual(
                        value, parsed.get(key),
                        f"{name}: 既存の '{key}' が失われた / 変化した")

    def test_no_trailing_newline_does_not_concatenate(self):
        """旧実装はここで 'retention: 15denableUserWorkload: true' を作っていた。"""
        _, written = self._run_with_mock_oc(
            self.CASES["no_trailing_newline"], with_pyyaml=True)
        self.assertIsNotNone(written)
        self.assertNotIn("15denableUserWorkload", written,
                         "末尾改行の無い既存値と連結して壊れた YAML になっている")

    def test_aborts_when_pyyaml_is_missing(self):
        """PyYAML が無ければ、劣化した方法に落ちず中止すること。

        テキスト処理のフォールバックは '---' 始まりの flow style、文書末尾の
        '...'、値が次行にある形などを静かに壊す (レビューで実証された)。
        安全に扱えないなら書き戻さないのが正しい。
        """
        for name in self.CASES:
            with self.subTest(case=name):
                proc, written = self._run_with_mock_oc(
                    self.CASES[name], with_pyyaml=False)
                self.assertNotEqual(0, proc.returncode,
                                    f"{name}: PyYAML 無しなのに成功扱いになっている")
                self.assertIsNone(written,
                                  f"{name}: 中止すべき場面で書き戻しが発生している")
                self.assertIn("PyYAML", proc.stderr,
                              f"{name}: 中止理由が利用者に伝わらない")

    def test_get_failure_never_writes_back(self):
        """取得に失敗したのを『空の設定』と取り違えて既存を消さないこと。

        以前の実装は、存在確認だけ成功して本体の取得が失敗すると、空入力を
        正常な空設定として扱い enableUserWorkload: true だけを書き戻して
        終了コード 0 になっていた (レビュー指摘)。
        """
        proc, written = self._run_with_mock_oc(
            self.CASES["already_false"], with_pyyaml=True, fail_get=True)
        self.assertNotEqual(0, proc.returncode,
                            "取得に失敗したのに成功扱いになっている")
        self.assertIsNone(written,
                          "取得に失敗したのに書き戻しが発生している (既存設定の消失)")

    def test_set_data_failure_is_not_masked(self):
        """更新の失敗が、後続の後始末で 0 に化けないこと。"""
        proc, _ = self._run_with_mock_oc(
            self.CASES["already_false"], with_pyyaml=True, fail_set=True)
        self.assertNotEqual(0, proc.returncode,
                            "oc set data の失敗が握りつぶされている")

    def test_silent_empty_merge_never_writes_back(self):
        """マージが静かに空を返しても、空の設定で上書きしないこと。"""
        proc, written = self._run_with_mock_oc(
            self.CASES["already_false"], with_pyyaml=True, silent_empty_merge=True)
        self.assertNotEqual(0, proc.returncode,
                            "空のマージ結果が成功扱いになっている")
        self.assertIsNone(written, "空のマージ結果を書き戻している")

    def test_notfound_creates_the_configmap(self):
        """ConfigMap が無い場合は manifest から新規作成する経路を通ること。"""
        proc, written = self._run_with_mock_oc(
            self.CASES["already_false"], with_pyyaml=True, not_found=True)
        self.assertEqual(0, proc.returncode, f"NotFound で失敗している\n{proc.stderr}")
        self.assertIsNone(written, "新規作成の経路なのに set data が走っている")
        self.assertTrue(self._applied, "oc apply による新規作成が行われていない")

    def test_create_failure_is_not_masked(self):
        """新規作成が失敗したら成功扱いにしないこと。"""
        proc, _ = self._run_with_mock_oc(
            self.CASES["already_false"], with_pyyaml=True,
            not_found=True, fail_apply=True)
        self.assertNotEqual(0, proc.returncode,
                            "oc apply の失敗が握りつぶされている")

    def test_no_fixed_tmp_paths(self):
        """固定 /tmp パスは前回の残骸を掴む。実機でまさにそれが起きた。"""
        snippet = self._extract_snippet()
        for bad in ("/tmp/cmo-new.yaml", "/tmp/cmo-current.yaml"):
            self.assertNotIn(bad, snippet, f"固定パス {bad} を使っている")
        self.assertIn("mktemp", snippet, "一時ファイルが mktemp になっていない")


class TestOgxOrphanPvc(RenderSyncBase):
    """誰からも参照されない PVC を両経路とも作らないこと。"""

    def test_standalone_pvc_is_not_emitted(self):
        """StatefulSet は volumeClaimTemplates を使うので data-ogx-postgres-0 が実体。

        単体の ogx-postgres-data は参照されず、WaitForFirstConsumer の
        StorageClass では永久に Pending で残る。
        """
        path = self.manifests / "11-ogx" / "01-postgres.yml"
        docs = [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]
        orphans = [d for d in docs
                   if d["kind"] == "PersistentVolumeClaim"
                   and d["metadata"]["name"] == "ogx-postgres-data"]
        self.assertEqual([], orphans,
                         "render.py が参照されない PVC ogx-postgres-data を出している")

        role_defs = find_k8s_definitions(
            ANSIBLE_DIR / "roles" / "ogx" / "tasks" / "main.yml", "PersistentVolumeClaim")
        role_orphans = [d for d in role_defs
                        if d.get("metadata", {}).get("name") == "ogx-postgres-data"]
        self.assertEqual([], role_orphans,
                         "role が参照されない PVC ogx-postgres-data を作っている")

    def _delete_task(self):
        """role の PVC 削除タスクを YAML 構造として取り出す。

        生テキストの `assertIn` で済ませてはいけない。ガードを削っても
        コメント中の文字列で成立してしまい、テストが素通りする
        (実際にレビューでその穴を指摘された)。
        """
        doc = yaml.safe_load(
            (ANSIBLE_DIR / "roles" / "ogx" / "tasks" / "main.yml").read_text(encoding="utf-8"))
        tasks = [n for n in _walk(doc)
                 if isinstance(n, dict)
                 and isinstance(n.get("kubernetes.core.k8s"), dict)
                 and n["kubernetes.core.k8s"].get("state") == "absent"
                 and n["kubernetes.core.k8s"].get("name") == "ogx-postgres-data"]
        self.assertEqual(1, len(tasks),
                         "role に ogx-postgres-data を削除するタスクがちょうど 1 つ必要")
        return tasks[0]

    @staticmethod
    def _eval_when(conds, pvc, pods):
        """when: の式を実際の Templar で評価する。

        文字列の有無だけ見ると、条件を反転させても通ってしまう
        (`volumeName == ''` を `!= ''` にしても素通りした、とレビューで指摘)。
        実際に評価して真偽を確かめる。
        """
        from ansible.module_utils.parsing.convert_bool import boolean
        from ansible.parsing.dataloader import DataLoader
        from ansible.template import Templar
        try:  # ansible-core 2.19+ はテンプレートに trust が要る
            from ansible.template import trust_as_template
        except ImportError:  # 古い ansible-core
            def trust_as_template(x):
                return x
        tmpl = Templar(loader=DataLoader(), variables={
            "_ogx_pvc": {"resources": ([pvc] if pvc else [])},
            "_ogx_pods": {"resources": pods},
        })
        for c in conds:
            # Templar は False を文字列 "False" で返すことがある。
            # bool("False") は True なので、必ず boolean() を通す。
            rendered = tmpl.template(trust_as_template("{{ %s }}" % c))
            # 置換されずに素通りしたら、式が評価されていない証拠なので弾く
            assert "{{" not in str(rendered), f"not templated: {rendered!r}"
            if not boolean(rendered, strict=False):
                return False
        return True

    @staticmethod
    def _pvc(phase="Pending", volume_name=""):
        return {"metadata": {"uid": "u-1", "resourceVersion": "100"},
                "spec": {"volumeName": volume_name}, "status": {"phase": phase}}

    @staticmethod
    def _pod_using(claim):
        return {"spec": {"volumes": [{"persistentVolumeClaim": {"claimName": claim}}]}}

    def test_role_when_conditions_evaluate_correctly(self):
        """削除してよい場合だけ真になることを、実際に評価して確かめる。"""
        conds = self._delete_task()["when"]
        cases = [
            ("削除してよい: Pending / 未割当 / 参照なし", self._pvc(), [], True),
            ("PVC が存在しない", None, [], False),
            ("Bound", self._pvc(phase="Bound"), [], False),
            ("Pending だが PV 割当済み", self._pvc(volume_name="pv-1"), [], False),
            ("参照している Pod がある", self._pvc(),
             [self._pod_using("ogx-postgres-data")], False),
            ("別 PVC を参照する Pod は無関係", self._pvc(),
             [self._pod_using("other-pvc")], True),
            ("volumes が無い Pod", self._pvc(), [{"spec": {}}], True),
            ("volumes が null の Pod", self._pvc(), [{"spec": {"volumes": None}}], True),
        ]
        for label, pvc, pods, expected in cases:
            with self.subTest(case=label):
                self.assertEqual(expected, self._eval_when(conds, pvc, pods),
                                 f"{label}: when: の評価結果が期待と違う")

    def test_role_delete_sends_uid_and_resource_version_preconditions(self):
        """確認から削除までの間に Bound へ変わったら API 側で弾かせること。"""
        task = self._delete_task()
        pre = (task["kubernetes.core.k8s"].get("delete_options") or {}).get("preconditions")
        self.assertIsInstance(
            pre, dict,
            "delete_options.preconditions が無い。読み取りと削除の間に状態が"
            " 変わっても気づけず、Bound になった PVC を消しうる (TOCTOU)")
        for key in ("uid", "resourceVersion"):
            self.assertIn(key, pre, f"preconditions に {key} が無い")
            self.assertIn("_ogx_pvc", str(pre[key]),
                          f"preconditions.{key} が、直前に読み取った値を参照していない")

    def test_guide_delete_uses_preconditions_and_full_guard(self):
        """手順書側も同じ安全性を持つこと。

        oc delete は precondition を送れないが `oc delete --raw=URI -f -` なら
        body を送れる (実機で 409 時 exit 1 を確認)。
        """
        self.assertIn("preconditions", self.guide,
                      "手順書の削除が precondition 無しで実行されている")
        self.assertIn("oc delete --raw=", self.guide,
                      "body を送れない削除方法になっている")
        self.assertIn("volumeName", self.guide,
                      "手順書のガードに volumeName 空の判定が無い")
        self.assertIn("INUSE", self.guide,
                      "手順書のガードに参照 Pod の検査が無い")
        self.assertNotIn("oc delete pvc ogx-postgres-data", self.guide,
                         "precondition を送れない素の oc delete が残っている")

    def _pvc_snippet(self) -> str:
        start = self.guide.index("PVC_ERR=$(mktemp)")
        end = self.guide.index("rm -f \"$PVC_ERR\"; trap - EXIT", start) + 40
        return self.guide[start:end]

    def _run_pvc_snippet(self, *, get_rc=0, get_err="", pvc=None,
                         delete_rc=0, delete_err=""):
        """模擬 oc で PVC 掃除部分を実行する。"""
        work = Path(tempfile.mkdtemp(prefix="pvcdel-"))
        try:
            pvc = pvc or {"metadata": {"uid": "u-1", "resourceVersion": "100"},
                          "spec": {"volumeName": ""}, "status": {"phase": "Pending"}}
            (work / "pvc.json").write_text(json.dumps(pvc), encoding="utf-8")
            (work / "pods.json").write_text(json.dumps({"items": []}), encoding="utf-8")
            mock = work / "bin"; mock.mkdir()
            oc = mock / "oc"
            oc.write_text(
                "#!/usr/bin/env bash\n"
                f'WORK="{work}"\n'
                f'GET_RC={get_rc}; DELETE_RC={delete_rc}\n'
                'if [ "$1" = "get" ] && [ "$2" = "pvc" ]; then\n'
                f'  if [ "$GET_RC" != "0" ]; then echo {get_err!r} >&2; exit "$GET_RC"; fi\n'
                '  cat "$WORK/pvc.json"; exit 0\n'
                'fi\n'
                'if [ "$1" = "get" ] && [ "$2" = "pods" ]; then cat "$WORK/pods.json"; exit 0; fi\n'
                'if [ "$1" = "delete" ]; then\n'
                '  # body を捨てずに保存する。捨てると preconditions の配線ミス\n'
                '  # (uid/rv を取り違える等) をテストが素通りしてしまう。\n'
                '  cat > "$WORK/delete-body"\n'
                '  for a in "$@"; do case "$a" in --raw=*) printf "%s" "${a#--raw=}" '
                '> "$WORK/delete-uri" ;; esac; done\n'
                f'  if [ "$DELETE_RC" != "0" ]; then echo {delete_err!r} >&2; exit "$DELETE_RC"; fi\n'
                '  touch "$WORK/deleted"; exit 0\n'
                'fi\n'
                'exit 0\n', encoding="utf-8")
            oc.chmod(0o755)
            env = dict(os.environ, PATH=f"{mock}:{os.environ['PATH']}")
            proc = subprocess.run(["bash", "-c", self._pvc_snippet()],
                                  capture_output=True, text=True, env=env)
            body_f, uri_f = work / "delete-body", work / "delete-uri"
            sent = {
                "body": body_f.read_text(encoding="utf-8") if body_f.exists() else None,
                "uri": uri_f.read_text(encoding="utf-8") if uri_f.exists() else None,
            }
            return proc, (work / "deleted").exists(), sent
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_guide_pvc_get_failure_stops(self):
        """取得エラーを『存在しない』と取り違えないこと。"""
        proc, deleted, _sent = self._run_pvc_snippet(
            get_rc=1, get_err="Error from server: etcdserver: request timed out")
        self.assertNotEqual(0, proc.returncode, "取得失敗が成功扱いになっている")
        self.assertFalse(deleted)

    def test_guide_pvc_notfound_is_a_clean_noop(self):
        proc, deleted, _sent = self._run_pvc_snippet(
            get_rc=1, get_err='Error from server (NotFound): pvc "x" not found')
        self.assertEqual(0, proc.returncode, f"NotFound は無害な no-op のはず\n{proc.stderr}")
        self.assertFalse(deleted)

    def test_guide_pvc_conflict_is_tolerated_without_deleting(self):
        """409 は『確認後に変化した』。消さずに続行するのが正しい。"""
        proc, deleted, _sent = self._run_pvc_snippet(
            delete_rc=1,
            delete_err="Error from server (Conflict): Operation cannot be fulfilled")
        self.assertEqual(0, proc.returncode, f"409 で異常終了している\n{proc.stderr}")
        self.assertFalse(deleted, "409 なのに削除されている")

    def test_guide_pvc_unexpected_delete_failure_stops(self):
        """500 などの予期しない失敗は握りつぶさないこと。"""
        proc, _deleted, _sent = self._run_pvc_snippet(
            delete_rc=1, delete_err="Error from server (InternalError): 500")
        self.assertNotEqual(0, proc.returncode,
                            "予期しない削除失敗が成功扱いになっている")

    def test_guide_pvc_happy_path_deletes(self):
        proc, deleted, _sent = self._run_pvc_snippet()
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertTrue(deleted, "削除してよい条件なのに削除されていない")

    def test_guide_pvc_delete_sends_exact_uid_and_resource_version(self):
        """DELETE の body と URI を実際に捕捉して照合する。

        body を捨てる模擬では、uid と resourceVersion を取り違えて配線しても
        テストが素通りしてしまう (r の指摘)。
        """
        pvc = {"metadata": {"uid": "uid-abc-123", "resourceVersion": "987654"},
               "spec": {"volumeName": ""}, "status": {"phase": "Pending"}}
        proc, deleted, sent = self._run_pvc_snippet(pvc=pvc)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertTrue(deleted)

        self.assertIsNotNone(sent["body"], "DELETE の body が送られていない")
        body = json.loads(sent["body"])
        self.assertEqual({"uid": "uid-abc-123", "resourceVersion": "987654"},
                         body.get("preconditions"),
                         "preconditions に読み取った値がそのまま入っていない")
        self.assertEqual(
            "/api/v1/namespaces/redhat-ods-applications"
            "/persistentvolumeclaims/ogx-postgres-data",
            sent["uri"], "DELETE の対象 URI が違う")

    def test_guide_pvc_bound_is_not_deleted(self):
        proc, deleted, _sent = self._run_pvc_snippet(
            pvc={"metadata": {"uid": "u-1", "resourceVersion": "100"},
                 "spec": {"volumeName": "pv-1"}, "status": {"phase": "Bound"}})
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertFalse(deleted, "Bound な PVC を削除している")


class TestGroupSyncCA(RenderSyncBase):
    def test_role_and_renderer_reference_the_same_verified_ca(self):
        task_file = ANSIBLE_DIR / "roles/keycloak/tasks/main.yml"
        role = find_k8s_definitions(task_file, "GroupSync")[0]
        rendered = next(d for d in yaml.safe_load_all(
            (self.manifests / "06-keycloak/07-group-sync.yml").read_text())
            if d and d.get("kind") == "GroupSync")
        actual = rendered['spec']['providers'][0]['keycloak']
        expected = role['spec']['providers'][0]['keycloak']
        self.assertEqual(expected['ca'], actual['ca'])
        self.assertFalse(actual.get('insecure', False))
        self.assertFalse(expected.get('insecure', False))
        self.assertEqual(actual['ca'], {
            'kind': 'ConfigMap', 'name': 'default-ingress-cert',
            'namespace': 'openshift-config-managed', 'key': 'ca-bundle.crt'})

    def test_cross_namespace_ca_is_in_the_operator_cache_in_both_paths(self):
        from ansible.parsing.dataloader import DataLoader
        from ansible.template import Templar
        try:
            from ansible.template import trust_as_template
        except ImportError:
            def trust_as_template(value):
                return value
        tasks = yaml.safe_load((ANSIBLE_DIR / 'roles/keycloak/tasks/main.yml').read_text())
        install = next(t for t in _walk(tasks)
                       if t.get('name') == 'Install Group Sync Operator')
        definition = find_k8s_definitions(
            ANSIBLE_DIR / 'roles/common/tasks/install_operator.yml', 'Subscription')[0]
        config = Templar(loader=DataLoader(), variables=install['vars']).template(
            trust_as_template(definition['spec']['config']))
        subscription = next(d for d in yaml.safe_load_all(
            (self.manifests / '06-keycloak/06-group-sync-operator.yml').read_text())
            if d and d.get('kind') == 'Subscription')
        self.assertEqual(config, subscription['spec']['config'])
        watch = next(e['value'] for e in config['env'] if e['name'] == 'WATCH_NAMESPACE')
        self.assertEqual(set(watch.split(',')),
                         {'group-sync-operator', 'openshift-config-managed'})


    def test_verify_rejects_missing_stale_or_failed_group_sync(self):
        from ansible.parsing.dataloader import DataLoader
        from ansible.template import Templar
        try:
            from ansible.template import trust_as_template
        except ImportError:
            def trust_as_template(value):
                return value
        tasks = yaml.safe_load((ANSIBLE_DIR / 'playbooks/verify.yml').read_text())
        task = next(t for t in _walk(tasks) if t.get('name') ==
                    'Verify: Group Sync reconciled the current configuration')
        success = {'type': 'ReconcileSuccess', 'status': 'True',
                   'observedGeneration': 3, 'lastTransitionTime': '2026-10-26T13:22:08Z'}
        old_error = {'type': 'ReconcileError', 'status': 'True',
                     'observedGeneration': 2, 'lastTransitionTime': '2026-10-26T13:16:26Z'}
        cases = [
            ([], True),
            ([{'metadata': {'generation': 3}}], True),
            ([{'metadata': {'generation': 3}, 'status': {'conditions': [old_error]}}], True),
        ]
        for conditions, generation, fails in [
            ([success], 3, False), ([old_error, success], 3, False),
            ([success], 4, True),
            ([{k: v for k, v in success.items() if k != 'observedGeneration'}], 3, True),
            ([success, {**old_error, 'lastTransitionTime': '2026-10-26T13:23:00Z'}], 3, True),
        ]:
            cases.append(([{'metadata': {'generation': generation}, 'status': {
                'conditions': conditions, 'lastSyncSuccessTime': '2026-10-26T13:22:08Z'}}], fails))
        for resources, fails in cases:
            with self.subTest(resources=resources):
                variables = {**{k: trust_as_template(v) for k, v in task['vars'].items()},
                             '_group_sync': {'resources': resources}}
                result = Templar(loader=DataLoader(), variables=variables).template(
                    trust_as_template('{{ ' + task['failed_when'] + ' }}'))
                self.assertEqual(str(result).lower(), str(fails).lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
