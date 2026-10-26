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

    #: 壊しやすい既存設定のバリエーション
    CASES = {
        "no_trailing_newline": 'prometheus:\n  retention: 15d',
        "flow_style": '{prometheus: {retention: 15d}}\n',
        "already_true": 'enableUserWorkload: true\nprometheus:\n  retention: 15d\n',
        "already_false": 'enableUserWorkload: false\nprometheus:\n  retention: 15d\n',
    }

    def _extract_snippet(self) -> str:
        """手順書から UWM 有効化のブロックだけ取り出す。"""
        start = self.guide.index("if oc get cm cluster-monitoring-config")
        end = self.guide.index("oc apply -f manifests/13-observability/02-uwm.yml", start)
        return self.guide[start:end]

    def _run_with_mock_oc(self, existing: str, with_pyyaml: bool):
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

            oc = mock / "oc"
            oc.write_text(
                "#!/usr/bin/env bash\n"
                "# 模擬 oc。jsonpath は解釈せず、保存しておいた既存値をそのまま返す。\n"
                f'WORK="{work}"\n'
                'if [ "$1" = "get" ] && [ "$2" = "cm" ]; then\n'
                '  for a in "$@"; do case "$a" in -o) HAS_O=1;; jsonpath=*) HAS_JP=1;; esac; done\n'
                '  if [ -n "${HAS_JP:-}" ]; then cat "$WORK/current"; fi\n'
                '  exit 0\n'
                'fi\n'
                'if [ "$1" = "set" ] && [ "$2" = "data" ]; then\n'
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
            if with_pyyaml:
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
            return proc, out
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_existing_config_is_preserved_and_flag_is_set(self):
        for with_yaml in (True, False):
            for name, existing in self.CASES.items():
                if name == "flow_style" and not with_yaml:
                    continue  # フォールバックは flow style を扱えない (別テストで検証)
                with self.subTest(case=name, pyyaml=with_yaml):
                    proc, written = self._run_with_mock_oc(existing, with_yaml)
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
        for with_yaml in (True, False):
            with self.subTest(pyyaml=with_yaml):
                _, written = self._run_with_mock_oc(
                    self.CASES["no_trailing_newline"], with_yaml)
                self.assertIsNotNone(written)
                self.assertNotIn("15denableUserWorkload", written,
                                 "末尾改行の無い既存値と連結して壊れた YAML になっている")

    def test_fallback_refuses_flow_style_instead_of_corrupting(self):
        """PyYAML が無く flow style のときは、壊すより中止すること。"""
        proc, written = self._run_with_mock_oc(self.CASES["flow_style"], with_pyyaml=False)
        self.assertNotEqual(0, proc.returncode,
                            "flow style を扱えないのに成功扱いになっている")
        self.assertIsNone(written, "中止すべき場面で書き戻しが発生している")

    def test_merge_failure_never_writes_back(self):
        """マージ段階で失敗したら oc set data に進まないこと。

        実機で、PyYAML が無く python が落ちたのに処理が続行し、
        前回の残骸 /tmp/cmo-new.yaml を書き戻そうとした (admission webhook が
        拒否して事なきを得た)。固定パスをやめ、失敗時は必ず止める。
        """
        snippet = self._extract_snippet()
        self.assertNotIn("/tmp/cmo-new.yaml", snippet,
                         "固定の /tmp パスを使っている。前回の残骸を掴む危険がある")
        self.assertNotIn("/tmp/cmo-current.yaml", snippet,
                         "固定の /tmp パスを使っている。前回の残骸を掴む危険がある")
        self.assertIn("mktemp", snippet, "一時ファイルが mktemp になっていない")
        self.assertIn('-s "$CMO_NEW"', snippet,
                      "書き戻し前にマージ結果の非空チェックが無い")


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

    def test_role_delete_is_guarded_by_when_conditions(self):
        """when: が「Pending」かつ「volumeName 空」かつ「参照 Pod 無し」であること。"""
        task = self._delete_task()
        conds = task.get("when")
        self.assertIsInstance(conds, list, "削除タスクに when: のリストが無い")
        joined = " ".join(str(c) for c in conds)
        self.assertIn("Pending", joined, "when: に Pending 判定が無い")
        self.assertIn("volumeName", joined,
                      "when: に volumeName 空の判定が無い "
                      "(Pending だけでは『データが無い』証明にならない)")
        self.assertIn("claimName", joined,
                      "when: に参照 Pod の検査が無い")

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

        oc delete は precondition を送れないため API を直接叩く形になっている。
        """
        self.assertIn("preconditions", self.guide,
                      "手順書の削除が precondition 無しで実行されている")
        for token in ('\\"uid\\"', '\\"resourceVersion\\"'):
            self.assertIn(token, self.guide, f"手順書の precondition に {token} が無い")
        self.assertIn("volumeName", self.guide,
                      "手順書のガードに volumeName 空の判定が無い")
        self.assertIn("INUSE", self.guide,
                      "手順書のガードに参照 Pod の検査が無い")
        self.assertNotIn("oc delete pvc ogx-postgres-data", self.guide,
                         "precondition を送れない素の oc delete が残っている")


if __name__ == "__main__":
    unittest.main(verbosity=2)
