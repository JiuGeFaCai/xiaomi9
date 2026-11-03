"""Exercise cache replacement preparation without contacting GitHub."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "prepare_ccache_refresh.sh"
FAKE_CLI = """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['MOCK_LOG'], 'a') as log:
    log.write(json.dumps([name, args]) + '\\n')
if name == 'ccache':
    sys.exit(int(os.environ.get('CLEANUP_EXIT', '0')))
method = args[args.index('--method') + 1]
if method == 'GET':
    print(os.environ['CACHE_PAGES'])
    sys.exit(int(os.environ.get('LIST_EXIT', '0')))
if method == 'DELETE':
    sys.exit(int(os.environ.get('DELETE_EXIT', '0')))
sys.exit(99)
"""


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq required")
class CacheRefreshTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = self.root / "ccache-data"
        self.cache.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for command in ("gh", "ccache"):
            executable = self.bin / command
            executable.write_text(FAKE_CLI)
            executable.chmod(0o755)
        self.log = self.root / "commands.jsonl"
        self.env = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "GITHUB_REPOSITORY": "example/kernel",
            "CACHE_KEY": "ccache-v2-Linux-oneplus_13",
            "CACHE_REF": "refs/heads/cache/update",
            "CCACHE_DIR": str(self.cache),
            "CCACHE_MAXSIZE": "800MB",
            "MOCK_LOG": str(self.log),
            "CACHE_PAGES": '{"actions_caches": []}',
            "CLEANUP_EXIT": "0",
            "LIST_EXIT": "0",
            "DELETE_EXIT": "0",
        }

    def entry(self, cache_id, **overrides):
        return {
            "id": cache_id,
            "key": self.env["CACHE_KEY"],
            "ref": self.env["CACHE_REF"],
            **overrides,
        }

    def pages(self, *pages):
        self.env["CACHE_PAGES"] = "\n".join(
            json.dumps({"actions_caches": page}) for page in pages
        )

    def run_script(self):
        return subprocess.run(
            ["bash", str(SCRIPT)], env=self.env,
            text=True, capture_output=True, timeout=10,
        )

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def deletions(self):
        return [args[-1] for name, args in self.calls()
                if name == "gh" and "DELETE" in args]

    def test_only_exact_key_on_current_ref_is_deleted(self):
        self.pages([
            self.entry(1),
            self.entry(2, key=self.env["CACHE_KEY"] + "_b"),
            self.entry(3, ref="refs/heads/main"),
        ])
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.deletions(), ["repos/example/kernel/actions/caches/1"])
        self.assertEqual(self.calls()[0], ["ccache", ["--cleanup"]])
        query = self.calls()[1][1]
        self.assertIn("key=" + self.env["CACHE_KEY"], query)
        self.assertIn("ref=" + self.env["CACHE_REF"], query)
        self.assertIn("--paginate", query)

    def test_first_run_and_fallback_from_other_branch_need_no_deletion(self):
        for entries in ([], [self.entry(1, ref="refs/heads/main")]):
            with self.subTest(entries=entries):
                self.pages(entries)
                result = self.run_script()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.deletions(), [])

    def test_all_pages_are_consumed_and_matching_versions_are_removed(self):
        self.pages([self.entry(1, version="old")], [self.entry(2, version="new")])
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.deletions(), [
            "repos/example/kernel/actions/caches/1",
            "repos/example/kernel/actions/caches/2",
        ])

    def test_failed_listing_cannot_delete_even_with_partial_response(self):
        self.pages([self.entry(1)])
        self.env["LIST_EXIT"] = "1"
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.deletions(), [])

    def test_invalid_api_response_preserves_old_cache(self):
        self.env["CACHE_PAGES"] = "invalid json"
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.deletions(), [])

    def test_failed_local_cleanup_preserves_remote_cache(self):
        self.env["CLEANUP_EXIT"] = "1"
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [["ccache", ["--cleanup"]]])

    def test_missing_local_cache_preserves_remote_cache(self):
        self.cache.rmdir()
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_empty_key_cannot_trigger_a_broad_deletion(self):
        self.env["CACHE_KEY"] = ""
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_deletion_failure_stops_refresh(self):
        self.pages([self.entry(1), self.entry(2)])
        self.env["DELETE_EXIT"] = "1"
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.deletions(), ["repos/example/kernel/actions/caches/1"])


if __name__ == "__main__":
    unittest.main()
