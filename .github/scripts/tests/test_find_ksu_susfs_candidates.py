"""Regression tests for discovery from real, local SUSFS Git histories."""

import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "find_ksu_susfs_candidates.py"
SPEC = importlib.util.spec_from_file_location("ksu_candidates", SCRIPT)
candidates = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(candidates)


class CandidateDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git("init", "--quiet")
        self.git("config", "user.name", "SUSFS test")
        self.git("config", "user.email", "test@example.com")
        self.patch = self.repo / candidates.PATCH_PATH
        self.patch.parent.mkdir(parents=True)
        self.revision = 0

    def git(self, *args):
        return subprocess.check_output(
            ["git", *args], cwd=self.repo, text=True, stderr=subprocess.PIPE,
        ).strip()

    def commit_patch(self, message):
        self.revision += 1
        self.patch.write_text(f"patch revision {self.revision}\n")
        self.git("add", "--", candidates.PATCH_PATH)
        self.git("commit", "--quiet", "-m", message)
        return self.git("rev-parse", "HEAD")

    def discover(self, repo=None):
        with contextlib.redirect_stderr(io.StringIO()):
            return candidates.find_candidates(repo or self.repo)

    def test_new_baselines_precede_known_fallbacks_and_are_deduplicated(self):
        old, new = "a" * 40, "b" * 40
        self.commit_patch(f"Sync https://github.com/tiann/KernelSU/commit/{old}")
        self.commit_patch(f"Sync https://github.com/tiann/KernelSU/commit/{new}")
        self.commit_patch(f"Fix https://github.com/tiann/KernelSU/commit/{new}")
        self.assertEqual(
            self.discover(), [new, old, *candidates.FALLBACK_REVISIONS],
        )

    def test_pinned_susfs_head_does_not_use_newer_branch_history(self):
        old, new = "a" * 40, "b" * 40
        pinned = self.commit_patch(f"Sync https://github.com/tiann/KernelSU/commit/{old}")
        self.commit_patch(f"Sync https://github.com/tiann/KernelSU/commit/{new}")
        self.git("checkout", "--quiet", "--detach", pinned)
        self.assertEqual(self.discover(), [old, *candidates.FALLBACK_REVISIONS])

    def test_unrelated_susfs_commits_do_not_supply_candidates(self):
        baseline, unrelated = "a" * 40, "b" * 40
        self.commit_patch(f"Sync https://github.com/tiann/KernelSU/commit/{baseline}")
        (self.repo / "README.md").write_text("Unrelated change\n")
        self.git("add", "README.md")
        self.git("commit", "--quiet", "-m",
                 f"Example https://github.com/tiann/KernelSU/commit/{unrelated}")
        self.assertEqual(self.discover(), [baseline, *candidates.FALLBACK_REVISIONS])

    def test_only_full_official_commit_links_are_accepted(self):
        official = "a" * 40
        self.commit_patch("\n".join([
            f"https://github.com/tiann/KernelSU/commit/{official.upper()}",
            f"https://github.com/SukiSU-Ultra/SukiSU-Ultra/commit/{'b' * 40}",
            f"https://github.com/pershoot/KernelSU-Next/commit/{'c' * 40}",
            f"https://github.com/Baka-SU/BakaSU/commit/{'d' * 40}",
            "https://github.com/tiann/KernelSU/commit/abcdef0",
            f"https://github.com/tiann/KernelSU/commit/{'e' * 41}",
            f"https://github.com/tiann/KernelSU/commit/{'f' * 40}g",
            f"https://github.com/tiann/KernelSU.evil/commit/{'1' * 40}",
        ]))
        self.assertEqual(self.discover(), [official, *candidates.FALLBACK_REVISIONS])

    def test_known_baseline_in_history_is_not_duplicated(self):
        baseline = candidates.FALLBACK_REVISIONS[1]
        self.commit_patch(f"Sync https://github.com/tiann/KernelSU/commit/{baseline}")
        self.assertEqual(self.discover(), [
            baseline, candidates.FALLBACK_REVISIONS[0], candidates.FALLBACK_REVISIONS[2],
        ])

    def test_patch_updates_without_links_still_use_earlier_sync(self):
        baseline = "a" * 40
        self.commit_patch(f"Sync https://github.com/tiann/KernelSU/commit/{baseline}")
        self.commit_patch("Fix patch context without changing the KSU baseline")
        self.assertEqual(self.discover(), [baseline, *candidates.FALLBACK_REVISIONS])

    def test_missing_git_history_uses_known_fallbacks(self):
        self.assertEqual(self.discover(), list(candidates.FALLBACK_REVISIONS))
        self.assertEqual(
            self.discover(self.repo / "missing"), list(candidates.FALLBACK_REVISIONS),
        )

    def test_cli_stdout_contains_only_commit_ids(self):
        baseline = "a" * 40
        self.commit_patch(f"Sync https://github.com/tiann/KernelSU/commit/{baseline}")
        result = subprocess.run(
            ["python3", str(SCRIPT), str(self.repo)],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(
            result.stdout.splitlines(), [baseline, *candidates.FALLBACK_REVISIONS],
        )
        self.assertIn(baseline, result.stderr)


if __name__ == "__main__":
    unittest.main()
