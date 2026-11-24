"""Exercise CI snapshot preparation with real Git refs and pinned commits."""

import contextlib
import importlib.util
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET


SCRIPT = Path(__file__).resolve().parents[1] / "prepare_ci_manifest.py"
SPEC = importlib.util.spec_from_file_location("ci_manifest", SCRIPT)
ci_manifest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci_manifest)


@unittest.skipUnless(shutil.which("git"), "git is required")
class CiManifestTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote_root = self.root / "remotes"
        self.repo = self.remote_root / "kernel/common"
        self.repo.parent.mkdir(parents=True)
        self.git("init", "--quiet", "--initial-branch=main", str(self.repo))
        self.git("-C", str(self.repo), "config", "user.name", "CI test")
        self.git("-C", str(self.repo), "config", "user.email", "ci-test@example.invalid")
        (self.repo / "snapshot.txt").write_text("original build\n")
        self.git("-C", str(self.repo), "add", "snapshot.txt")
        self.git("-C", str(self.repo), "commit", "--quiet", "-m", "CI snapshot")
        self.revision = self.git("-C", str(self.repo), "rev-parse", "HEAD").strip()
        (self.repo / "snapshot.txt").write_text("newer upstream build\n")
        self.git("-C", str(self.repo), "commit", "--quiet", "-am", "Newer upstream")
        self.tip = self.git("-C", str(self.repo), "rev-parse", "HEAD").strip()
        self.source = self.root / "manifest.xml"
        self.output = self.root / "prepared.xml"

    def git(self, *args, check=True):
        result = subprocess.run(["git", *args], text=True, capture_output=True)
        if check and result.returncode:
            self.fail(result.stderr)
        return result.stdout

    def write_manifest(self, attributes="", default_attributes="", extra=""):
        self.source.write_text(
            '<manifest><remote name="aosp" fetch="' + self.remote_root.as_uri() + '/" />'
            '<default remote="aosp" revision="main-kernel-2025" ' + default_attributes + ' />'
            '<project name="kernel/common" path="common" revision="' + self.revision + '" '
            + attributes + '><linkfile src="snapshot.txt" dest="build-snapshot.txt" /></project>'
            + extra + '</manifest>'
        )

    def prepare(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            ci_manifest.prepare_manifest(self.source, self.output, **kwargs)
        original = ET.parse(self.source).getroot()
        prepared = ET.parse(self.output).getroot()
        self.assertEqual(
            [p.get("revision") for p in original.iter("project")],
            [p.get("revision") for p in prepared.iter("project")],
        )
        return prepared, prepared.find("project")

    def fetch_like_repo(self, project):
        """Use the same pinned-commit + upstream fetch spec as shallow repo sync."""
        checkout = self.root / "checkout"
        self.git("init", "--quiet", str(checkout))
        command = ["-C", str(checkout), "fetch", "--quiet", "--depth=1", "--no-tags",
                   self.repo.as_uri(), project.get("revision")]
        if project.get("upstream"):
            command.append(project.get("upstream"))
        self.git(*command)
        self.git("-C", str(checkout), "checkout", "--quiet", "--detach", project.get("revision"))
        self.assertEqual(self.git("-C", str(checkout), "rev-parse", "HEAD").strip(), self.revision)
        self.assertEqual((checkout / "snapshot.txt").read_text(), "original build\n")

    def test_moved_branch_fetches_original_build_not_newer_tip(self):
        branch = "android16-6.12-2025-06"
        self.git("-C", str(self.repo), "branch", "deprecated/" + branch)
        self.write_manifest(f'upstream="{branch}" dest-branch="{branch}"')
        _, project = self.prepare()
        self.assertEqual(project.get("upstream"), "deprecated/" + branch)
        self.assertEqual(project.get("dest-branch"), "deprecated/" + branch)
        self.assertNotEqual(project.get("revision"), self.tip)
        self.fetch_like_repo(project)

    def test_valid_branch_wins_when_both_names_exist(self):
        branch = "android16-6.12"
        for name in (branch, "deprecated/" + branch):
            self.git("-C", str(self.repo), "branch", name)
        self.write_manifest(f'upstream="{branch}" dest-branch="{branch}"')
        _, project = self.prepare()
        self.assertEqual(project.get("upstream"), branch)
        self.assertEqual(project.get("dest-branch"), branch)
        self.fetch_like_repo(project)

    def test_missing_upstream_drops_hint_and_fetches_commit(self):
        self.write_manifest('upstream="removed-release" dest-branch="removed-release"')
        _, project = self.prepare()
        self.assertIsNone(project.get("upstream"))
        self.assertIsNone(project.get("dest-branch"))
        self.fetch_like_repo(project)

    def test_default_branch_is_not_inherited_as_repo_upstream(self):
        self.write_manifest()
        root, project = self.prepare()
        self.assertIsNone(root.find("default").get("revision"))
        self.assertIsNone(project.get("upstream"))
        self.fetch_like_repo(project)

    def test_inherited_hints_resolve_per_repository(self):
        self.git("-C", str(self.repo), "branch", "deprecated/old-release")
        self.write_manifest(
            default_attributes='upstream="old-release" dest-branch="old-release"',
            extra=f'<project name="kernel/common-copy" revision="{self.revision}" />',
        )
        shutil.copytree(self.repo, self.remote_root / "kernel/common-copy")
        self.git("-C", str(self.remote_root / "kernel/common-copy"), "branch", "old-release")
        root, _ = self.prepare()
        first, second = root.findall("project")
        self.assertEqual(first.get("upstream"), "deprecated/old-release")
        self.assertEqual(second.get("upstream"), "old-release")
        self.assertIsNone(root.find("default").get("upstream"))
        self.assertIsNone(root.find("default").get("dest-branch"))

    def test_full_ref_and_existing_deprecated_prefix_are_idempotent(self):
        ref = "refs/heads/deprecated/android13-5.10-2023-01"
        self.git("-C", str(self.repo), "branch", ref.removeprefix("refs/heads/"))
        self.write_manifest(f'upstream="{ref}" dest-branch="{ref}"')
        _, project = self.prepare()
        self.assertEqual(project.get("upstream"), ref)
        first_output = self.output.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            ci_manifest.prepare_manifest(self.output, self.output)
        self.assertEqual(self.output.read_bytes(), first_output)
        self.fetch_like_repo(project)

    def test_full_ref_gains_deprecated_prefix(self):
        branch = "android14-6.1-2024-06"
        self.git("-C", str(self.repo), "branch", "deprecated/" + branch)
        self.write_manifest(f'upstream="refs/heads/{branch}"')
        _, project = self.prepare()
        self.assertEqual(project.get("upstream"), "refs/heads/deprecated/" + branch)

    def test_valid_tag_ref_stays_a_tag(self):
        tag = "android16-6.12-2025-06_r47"
        self.git("-C", str(self.repo), "tag", tag, self.revision)
        self.write_manifest(f'upstream="refs/tags/{tag}"')
        _, project = self.prepare()
        self.assertEqual(project.get("upstream"), "refs/tags/" + tag)
        self.fetch_like_repo(project)

    def test_review_remote_is_changed_for_fetch_only(self):
        self.write_manifest()
        tree = ET.parse(self.source)
        remote = tree.getroot().find("remote")
        remote.set("fetch", "https://android-review.googlesource.com/")
        remote.set("review", "https://android-review.googlesource.com/")
        tree.write(self.source)
        root, project = self.prepare()
        self.assertEqual(root.find("remote").get("fetch"), "https://android.googlesource.com/")
        self.assertEqual(root.find("remote").get("review"), "https://android-review.googlesource.com/")
        self.assertEqual(project.find("linkfile").get("dest"), "build-snapshot.txt")

    def test_transport_failure_preserves_commits_without_bad_hints(self):
        self.write_manifest('upstream="old-release" dest-branch="old-release"')
        def offline(*_):
            raise RuntimeError("offline")

        with contextlib.redirect_stderr(io.StringIO()):
            _, project = self.prepare(lookup=offline)
        self.assertIsNone(project.get("upstream"))
        self.fetch_like_repo(project)

    def test_unlocked_or_invalid_manifest_never_overwrites_output(self):
        self.write_manifest()
        source = self.source.read_text()
        self.output.write_text("existing output")
        for text in (
            source.replace(self.revision, "android16-6.12"),
            source.replace('name="kernel/common"', 'name="kernel/common" remote="missing"'),
            source.replace("</manifest>", '<include name="other.xml" /></manifest>'),
            "<html>404</html>",
            "<manifest>",
        ):
            with self.subTest(manifest=text):
                self.source.write_text(text)
                with self.assertRaises((ValueError, ET.ParseError)):
                    ci_manifest.prepare_manifest(self.source, self.output)
                self.assertEqual(self.output.read_text(), "existing output")

    def test_ref_lookup_retries_transient_failure(self):
        replies = [
            subprocess.CompletedProcess([], 128, "", "TLS connection lost"),
            subprocess.CompletedProcess([], 0, self.revision + "\trefs/heads/main\n", ""),
        ]
        with patch.object(ci_manifest.subprocess, "run", side_effect=replies) as run:
            refs = ci_manifest.fetch_refs(self.repo.as_uri(), ["refs/heads/main"])
        self.assertEqual(refs, {"refs/heads/main"})
        self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
