#!/usr/bin/env python3
"""
inventory/sample に環境固有の情報が混入していないことを検証する。

sample は公開されるデフォルト設定であり、実環境の識別子を含んではならない。
過去に実環境の構成を sample へ反映した際、実ユーザー名・メールアドレス・
MetalLB の IP レンジなどを持ち込む危険があった。

実行:
    python3 -m unittest tests.test_sample_sanitized
"""

import fcntl
import os
import re
import sys
import unittest
from pathlib import Path

# Fix non-blocking IO for aw container
for fd in [sys.stdout, sys.stderr]:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)

import yaml

ANSIBLE_DIR = Path(__file__).resolve().parent.parent
SAMPLE_DIR = ANSIBLE_DIR / "inventory" / "sample"

# メールアドレスとして許可するドメイン（RFC 2606 の予約ドメイン）
ALLOWED_EMAIL_DOMAINS = ("example.com", "example.org", "example.net")

# 実環境を示す値のパターン。ドキュメント URL は除外する。
FORBIDDEN_PATTERNS = [
    # 組織固有ドメイン（docs.redhat.com などの参照 URL は別途除外）
    (r"@[A-Za-z0-9.-]*\.(?:go\.jp|co\.jp|or\.jp)\b", "組織固有のメールドメイン"),
    (r"\b[A-Za-z0-9._%+-]+@(?!example\.(?:com|org|net))[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
     "example.com 以外のメールアドレス"),
    # 内部ネットワーク / クラスタ識別子
    (r"\b100\.64\.\d{1,3}\.\d{1,3}\b", "CGNAT レンジの実 IP"),
    (r"\.lab\.internal\b", "内部クラスタドメイン"),
    (r"\bapi\.[a-z0-9-]+\.[a-z0-9.-]+:6443\b", "実クラスタの API エンドポイント"),
    # 資格情報
    (r"\bsk-oai-[A-Za-z0-9]", "MaaS API キー"),
    (r"\bhf_[A-Za-z0-9]{20,}", "HuggingFace トークン"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "秘密鍵"),
]

# 参照 URL など、検査から除外してよい行
ALLOW_LINE_PATTERNS = [
    re.compile(r"https?://(?:docs|access|www)\.redhat\.com"),
    re.compile(r"https?://huggingface\.co"),
    re.compile(r"https?://recipes\.vllm\.ai"),
]


def sample_files():
    return sorted(p for p in SAMPLE_DIR.rglob("*") if p.is_file())


class TestSampleSanitized(unittest.TestCase):
    """inventory/sample に実環境の情報が含まれないこと"""

    def test_sample_dir_exists(self):
        self.assertTrue(SAMPLE_DIR.is_dir(), f"{SAMPLE_DIR} がありません")
        self.assertGreater(len(sample_files()), 0)

    def test_no_environment_specific_values(self):
        for path in sample_files():
            try:
                text = path.read_text()
            except UnicodeDecodeError:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if any(p.search(line) for p in ALLOW_LINE_PATTERNS):
                    continue
                for pattern, label in FORBIDDEN_PATTERNS:
                    m = re.search(pattern, line)
                    if m:
                        rel = path.relative_to(ANSIBLE_DIR)
                        self.fail(
                            f"{rel}:{lineno} に{label}が含まれています: "
                            f"{m.group(0)!r}\n  {line.strip()}"
                        )

    def test_user_emails_use_example_domain(self):
        components = SAMPLE_DIR / "group_vars" / "all" / "components.yml.sample"
        data = yaml.safe_load(components.read_text())
        users = data.get("keycloak_users") or []
        self.assertGreater(len(users), 0, "keycloak_users が空です")
        for u in users:
            with self.subTest(user=u.get("username")):
                self.assertTrue(
                    u["email"].endswith(ALLOWED_EMAIL_DOMAINS),
                    f"{u['email']} は予約ドメインではありません",
                )

    def test_group_names_follow_convention(self):
        """グループ名は maas-<モデルキー>-users（maas-admins を除く）"""
        components = SAMPLE_DIR / "group_vars" / "all" / "components.yml.sample"
        data = yaml.safe_load(components.read_text())
        models = set(data.get("models") or {})
        for name, cfg in (data.get("keycloak_groups") or {}).items():
            if name == "maas-admins":
                continue
            with self.subTest(group=name):
                self.assertRegex(name, r"^maas-[a-z0-9.-]+-users$")
                for m in cfg.get("models", []):
                    self.assertIn(
                        m, models,
                        f"グループ {name} が未定義のモデル {m} を参照しています",
                    )

    def test_model_keys_are_rfc1123(self):
        components = SAMPLE_DIR / "group_vars" / "all" / "components.yml.sample"
        data = yaml.safe_load(components.read_text())
        pattern = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
        for key in (data.get("models") or {}):
            with self.subTest(model=key):
                self.assertRegex(key, pattern)


if __name__ == "__main__":
    unittest.main(verbosity=2)
