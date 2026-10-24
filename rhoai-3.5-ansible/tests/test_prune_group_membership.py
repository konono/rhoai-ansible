#!/usr/bin/env python3
"""
グループ所属の宣言的な解除（prune）の条件を検証する。

過去の不具合:
  `groups` キーを書いていないユーザーが `| default([])` で「所属ゼロ」と
  みなされ、書き忘れただけで管理対象グループの所属が全部消えていた。
  所属を外す意図がある場合は `groups: []` と明示させる。

実行:
    python3 -m unittest tests.test_prune_group_membership
"""

import fcntl
import os
import sys
import unittest
from pathlib import Path

for fd in [sys.stdout, sys.stderr]:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)

import yaml

ANSIBLE_DIR = Path(__file__).resolve().parent.parent
TASKS = ANSIBLE_DIR / "roles" / "keycloak" / "tasks" / "manage_maas_access.yml"


class TestPruneCondition(unittest.TestCase):

    def setUp(self):
        tasks = yaml.safe_load(TASKS.read_text())
        self.task = next(
            t for t in tasks
            if t.get("name", "").startswith("Remove users from managed groups")
        )
        self.when = [str(c) for c in self.task["when"]]

    def test_requires_groups_to_be_defined(self):
        """groups 未定義のユーザーは解除対象にしない"""
        self.assertTrue(
            any("groups is defined" in c for c in self.when),
            "groups が未定義のユーザーを除外する条件がありません。"
            "default([]) だけだと書き忘れで全解除されます",
        )

    def test_does_not_default_groups_to_empty_in_membership_check(self):
        """所属判定で groups を default([]) しない（上の条件で担保済みのため）"""
        membership = [c for c in self.when if "not in" in c]
        self.assertTrue(membership, "所属判定の条件が見つかりません")
        for c in membership:
            self.assertNotIn(
                "default([])", c,
                "groups is defined で担保しているので default([]) は不要。"
                "残っていると意図が曖昧になります",
            )

    def test_only_managed_groups_are_pruned(self):
        """keycloak_groups に定義済みのグループだけを解除対象にする"""
        self.assertTrue(
            any("keycloak_groups" in c for c in self.when),
            "管理対象グループに限定する条件がありません",
        )

    def test_prune_can_be_disabled(self):
        self.assertTrue(
            any("keycloak_prune_group_membership" in c for c in self.when),
            "解除を無効化するフラグの条件がありません",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
