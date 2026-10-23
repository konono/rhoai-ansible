#!/usr/bin/env python3
"""Read-only B1 diagnosis for a vLLM model in OpenShift.

Usage: python3 scripts/diagnose-mtp.py [model-key] [--namespace llm-serving]
Only matching Pod specs, the last 500 log lines, and localhost metrics are read.
No raw logs, environment variables, metric labels, or secrets are printed.
"""

import argparse
import json
import re
import subprocess
import sys


SPEC_METRIC = re.compile(r"^(?:vllm:)?(spec_decode_[a-zA-Z0-9_]+)(?:\{[^\n]*\})?\s+([-+0-9.eE]+)(?:\s|$)")
NAME = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
STARTUP = re.compile(r"speculative|spec_decode|\bmtp\b|draft", re.I)
ERROR = re.compile(r"error|exception|invalid|unsupported|unrecognized|failed", re.I)


def oc(*args):
    try:
        return subprocess.run(["oc", *args], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return subprocess.CompletedProcess([], 127, "", "oc command not found")


def speculative_args(args):
    """Return only the relevant argv entries, without printing unrelated flags."""
    found = []
    for index, arg in enumerate(args):
        if arg == "--speculative-config":
            found.append(arg)
            if index + 1 < len(args):
                found.append(args[index + 1])
        elif arg.startswith("--speculative-config="):
            found.append(arg)
    return found


def check_args(container):
    args = container.get("args") or []
    found = speculative_args(args)
    print("  ① Pod args: speculative-config 関連の argv 要素数=%d。" % len(found))
    if not found:
        print("     判定: Pod に speculative-config がありません。レシピ/CR/Pod の反映を確認してください。")
        return
    configs = []
    for index, arg in enumerate(args):
        if arg.startswith("--speculative-config="):
            configs.append(arg.split("=", 1)[1])
        elif arg == "--speculative-config" and index + 1 < len(args):
            configs.append(args[index + 1])
    if len(configs) != 1:
        print("     判定: config の個数が想定外です。argv の組み立てを確認してください。")
        return
    try:
        config = json.loads(configs[0])
    except json.JSONDecodeError:
        print("     判定: config が単一の JSON 引数として解釈できません。分割/引用符を確認してください。")
        return
    if not isinstance(config, dict):
        print("     判定: config は JSON オブジェクトではありません。")
        return
    method = config.get("method")
    method = method if method in ("mtp", "qwen3_5_mtp", "ngram") else "(other)"
    tokens = config.get("num_speculative_tokens")
    tokens = tokens if isinstance(tokens, int) and not isinstance(tokens, bool) else "(other)"
    print("     判定: JSON 引数あり (method=%r, num_speculative_tokens=%r)。受理と実行は②③と照合。" %
          (method, tokens))


def check_logs(namespace, pod):
    result = oc("logs", "-n", namespace, pod, "-c", "main", "--tail=500")
    if result.returncode:
        print("  ② 起動ログ: 取得失敗 (oc exit=%d)。受理は未判定。" % result.returncode)
        return
    matches = [line for line in result.stdout.splitlines() if STARTUP.search(line)]
    errors = sum(bool(ERROR.search(line)) for line in matches)
    signals = sorted({word for word in ("unsupported", "unrecognized", "invalid", "failed", "exception", "error")
                      if any(re.search(r"\b" + word + r"\b", line, re.I) for line in matches)})
    print("  ② 起動ログ: 関連行=%d, エラーらしい行=%d (末尾 500 行)。" % (len(matches), errors))
    if signals:
        print("     エラー語: %s (ログ本文は表示しません)。" % ", ".join(signals))
    if errors:
        print("     判定: 起動時拒否の可能性があります。権限のある環境で該当ログを確認してください。")
    else:
        print("     判定: 明確な拒否は検出されません。関連行の有無だけでは受理を確定できません。")


def metric_values(body):
    values = {}
    for line in body.splitlines():
        match = SPEC_METRIC.match(line)
        if match:
            try:
                values[match.group(1)] = values.get(match.group(1), 0) + float(match.group(2))
            except ValueError:
                continue
    return values


def check_metrics(namespace, pod):
    for scheme in ("https", "http"):
        result = oc("exec", "-n", namespace, pod, "-c", "main", "--",
                    "curl", "-kfsS", "--max-time", "5", "%s://localhost:8000/metrics" % scheme)
        if result.returncode:
            print("  ③ %s metrics: 取得失敗 (oc/curl exit=%d)。" % (scheme.upper(), result.returncode))
            continue
        values = metric_values(result.stdout)
        print("  ③ %s metrics: 到達。spec_decode 系 %d 種類。" % (scheme.upper(), len(values)))
        for name, value in sorted(values.items()):
            print("     %s = %g" % (name, value))
        active = any(value > 0 for name, value in values.items()
                     if "draft_tokens" in name or "accepted_tokens" in name)
        if active:
            print("     判定: draft/accepted token カウンタが正です。推測デコードの実行を確認。")
        elif values:
            print("     判定: 指標はありますが、実行は未確認です。リクエスト後に再計測してください。")
        else:
            print("     判定: spec_decode 指標がありません。無効とは断定せず、バージョン/起動状態を確認してください。")
        return
    print("     判定: HTTP/HTTPS とも取得できず、MTP の稼働状態は未判定です。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", nargs="?", default="qwen3-8-27b-fp8")
    parser.add_argument("--namespace", default="llm-serving")
    args = parser.parse_args()
    if not NAME.fullmatch(args.model) or not NAME.fullmatch(args.namespace):
        parser.error("model/namespace must be Kubernetes DNS labels")
    result = oc("get", "pods", "-n", args.namespace,
                "-l", "app.kubernetes.io/name=" + args.model, "-o", "json")
    if result.returncode:
        print("Pod 一覧を取得できません (oc exit=%d)。クラスタ接続・権限を確認してください。" % result.returncode,
              file=sys.stderr)
        return 2
    try:
        pods = json.loads(result.stdout)["items"]
    except (ValueError, KeyError, TypeError):
        print("Pod 一覧の JSON を解析できません。", file=sys.stderr)
        return 2
    if not pods:
        print("対象 Pod は 0 件です。selector、namespace、LLMInferenceService の状態を確認してください。")
        return 2
    print("対象: %s/%s (%d Pod)" % (args.namespace, args.model, len(pods)))
    for pod in sorted(pods, key=lambda item: item.get("metadata", {}).get("name", "")):
        name = pod.get("metadata", {}).get("name", "")
        phase = pod.get("status", {}).get("phase", "Unknown")
        print("\nPod %s (phase=%s)" % (name, phase))
        container = next((item for item in pod.get("spec", {}).get("containers", [])
                          if item.get("name") == "main"), None)
        if not name or container is None:
            print("  main container が見つからず未判定です。")
            continue
        check_args(container)
        check_logs(args.namespace, name)
        if phase == "Running":
            check_metrics(args.namespace, name)
        else:
            print("  ③ Pod は Running ではないため metrics は未判定です。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
