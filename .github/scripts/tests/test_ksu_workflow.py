import contextlib
import importlib.util
import io
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location("ksu_workflow", Path(__file__).parents[1] / "ksu_workflow.py")
ksu = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ksu)


class KernelSUWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="test-ksu-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.envfile = self.root / "github-env"
        self.envfile.touch()
        environment = {k: v for k, v in os.environ.items() if not k.startswith("KSU")}
        environment["GITHUB_ENV"] = str(self.envfile)
        self.environment = mock.patch.dict(os.environ, environment, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def emitted(self):
        return dict(line.split("=", 1) for line in self.envfile.read_text().splitlines())

    def resolve(self, variant, *, susfs="true", kpm="false", ref="", override=""):
        os.environ.update(
            KSU_VARIANT_INPUT=variant, KSU_SUSFS_INPUT=susfs, KSU_KPM_INPUT=kpm,
            KSU_REF_INPUT=ref, KSU_VERSION_OVERRIDE_INPUT=override,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            ksu.resolve()
        os.environ.update(self.emitted())

    def git(self, source, *args):
        return subprocess.check_output(["git", "-C", str(source), *args], text=True, stderr=subprocess.DEVNULL).strip()

    def commit(self, source):
        self.git(source, "add", ".")
        self.git(source, "-c", "user.name=Workflow Test", "-c", "user.email=test@example.invalid",
                 "commit", "-q", "-m", "fixture")
        return self.git(source, "rev-parse", "HEAD")

    def repository(self, name="source", *, builtin=False, susfs=True):
        source = self.root / name
        (source / "kernel").mkdir(parents=True)
        self.git(source, "init", "-q", "-b", "main")
        config = 'config KSU\n    bool "KernelSU"\n    default y\n'
        if susfs:
            config += 'config KSU_SUSFS\n    bool "SUSFS"\n'
        if builtin:
            config += 'config KPM\n    bool "KPM"\n'
        (source / "kernel/Kconfig").write_text(config)
        (source / "kernel/Kbuild").write_text("KSU_VERSION := 12345\nKSU_VERSION_FULL := preserve\n")
        self.commit(source)
        self.git(source, "branch", "dev-susfs")
        return source

    def patch_directory(self, text):
        directory = self.root / "susfs"
        path = directory / "kernel_patches/KernelSU/10_enable_susfs_for_ksu.patch"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return directory

    def test_default_refs_and_none_alias(self):
        cases = {
            "SukiSU": ("main", "SukiSU"), "BakaSU": ("main", "BakaSU"),
            "ksunext": ("dev-susfs", "KSUNext"), "ksu": ("main", "KSU"),
            "none": ("", "NoKSU"), "无": ("", "NoKSU"),
        }
        for variant, (ref, label) in cases.items():
            with self.subTest(variant=variant):
                self.resolve(variant)
                values = self.emitted()
                self.assertEqual(values["KSU_REF"], ref)
                self.assertEqual(values["KSUVARIANT"], label)
                self.assertEqual(values["KSU_SUSFS_ENABLED"], "false" if label == "NoKSU" else "true")

    def test_invalid_options_are_rejected(self):
        cases = [
            {"variant": "unknown"}, {"variant": "none", "kpm": "builtin"},
            {"variant": "ksu", "ref": "--cleanup"}, {"variant": "ksu", "ref": "bad ref"},
            {"variant": "ksu", "override": "40x"}, {"variant": "ksu", "kpm": "true"},
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                self.resolve(**case)

    def test_none_performs_no_source_setup_and_keeps_matrix_suffix(self):
        self.resolve("none", ref="ignored", override="ignored", kpm="kpn")
        os.environ["KSU_ARTIFACT_SUFFIX"] = "oneplus_13"
        with mock.patch.object(ksu.subprocess, "run") as command:
            ksu.setup(self.root / "missing")
            ksu.finalize(self.root / "missing")
            command.assert_not_called()
        self.assertEqual(self.emitted()["KSUVER"], "0_oneplus_13")

    def test_config_modes_remove_stale_root_options_and_preserve_other_features(self):
        config = self.root / "gki_defconfig"
        initial = (
            "CONFIG_ZRAM=y\nCONFIG_REKERNEL=y\nCONFIG_KSU=y\nCONFIG_KSU_DEBUG=y\n"
            "CONFIG_KSU_SUSFS=y\nCONFIG_KSU_SUSFS_SUS_PATH=y\nCONFIG_KSU_SUSFS_EXTRA=y\nCONFIG_KPM=y\n"
        )
        for variant in ("SukiSU", "BakaSU", "ksunext", "ksu", "none"):
            for susfs in ("true", "false"):
                for mode in ("false", "kpn"):
                    with self.subTest(variant=variant, susfs=susfs, mode=mode):
                        self.resolve(variant, susfs=susfs, kpm=mode)
                        config.write_text(initial)
                        ksu.configure(config)
                        result = config.read_text()
                        self.assertIn("CONFIG_ZRAM=y\nCONFIG_REKERNEL=y", result)
                        self.assertNotIn("CONFIG_KPM=y", result)
                        self.assertEqual("CONFIG_KSU=y" in result, variant != "none")
                        self.assertEqual("CONFIG_KSU_SUSFS=y" in result, variant != "none" and susfs == "true")
                        self.assertEqual("CONFIG_KSU_SUSFS_EXTRA=y" in result, variant != "none" and susfs == "true")
                        self.assertEqual("CONFIG_KSU_DEBUG=y" in result, variant != "none")
                        ksu.configure(config)
                        self.assertEqual(config.read_text(), result)
        self.resolve("SukiSU", kpm="builtin")
        ksu.configure(config)
        self.assertIn("CONFIG_KPM=y", config.read_text())

    def test_setup_uses_each_upstream_directory_and_verifies_requested_ref(self):
        fixture = self.repository()
        run = subprocess.run
        expected = {
            "SukiSU": ("SukiSU-Ultra/SukiSU-Ultra/main", "KernelSU"),
            "BakaSU": ("Baka-SU/BakaSU/main", "KernelSU"),
            "ksunext": ("pershoot/KernelSU-Next/dev-susfs", "KernelSU-Next"),
            "ksu": ("tiann/KernelSU/main", "KernelSU"),
        }
        for variant, (url_path, directory) in expected.items():
            with self.subTest(variant=variant):
                workspace = self.root / variant
                workspace.mkdir()
                self.resolve(variant)
                def installer(command, **kwargs):
                    if command[0] == "curl":
                        self.assertEqual(command[-1], f"https://raw.githubusercontent.com/{url_path}/kernel/setup.sh")
                        script = (f"git clone -q {shlex.quote(str(fixture))} {directory}\n"
                                  f'git -C {directory} checkout -q "$1" || true\n')
                        return subprocess.CompletedProcess(command, 0, stdout=script.encode())
                    return run(command, **kwargs)
                with mock.patch.object(ksu.subprocess, "run", side_effect=installer):
                    ksu.setup(workspace)
                self.assertEqual(self.emitted()["KSU_SOURCE_DIR"], str(workspace / directory))
                self.resolve(variant, ref="missing-ref")
                # Upstream can report success despite a failed checkout; we must reject it.
                empty = self.root / (variant + "-missing")
                empty.mkdir()
                with mock.patch.object(ksu.subprocess, "run", side_effect=installer):
                    with self.assertRaises(subprocess.CalledProcessError):
                        ksu.setup(empty)

    def test_builtin_requires_real_kconfig_support(self):
        source = self.repository(susfs=False)
        self.resolve("BakaSU", kpm="builtin")
        workspace = self.root / "workspace"
        workspace.mkdir()
        run = subprocess.run
        def installer(command, **kwargs):
            if command[0] == "curl":
                script = f"git clone -q {shlex.quote(str(source))} KernelSU\n"
                return subprocess.CompletedProcess(command, 0, stdout=script.encode())
            return run(command, **kwargs)
        with mock.patch.object(ksu.subprocess, "run", side_effect=installer):
            with self.assertRaisesRegex(ValueError, "没有内置 KPM"):
                ksu.setup(workspace)
        self.assertNotIn("KSU_SOURCE_DIR", self.emitted())

    def test_next_version_uses_base_branch_not_susfs_commit_count(self):
        source = self.repository()
        self.git(source, "checkout", "-q", "-b", "dev")
        (source / "base").write_text("base")
        base = self.commit(source)
        self.git(source, "update-ref", "refs/remotes/origin/dev", base)
        self.git(source, "checkout", "-q", "-B", "dev-susfs")
        for i in range(2):
            (source / "susfs-change").write_text(str(i))
            self.commit(source)
        self.assertEqual(ksu.version_metadata(source, "ksunext")[0], 30002)
        self.assertEqual(ksu.version_metadata(source, "ksu")[0], 30004)
        self.assertEqual(ksu.version_metadata(source, "BakaSU")[0], 30704)
        self.assertEqual(ksu.version_metadata(source, "SukiSU")[0], 37186)

    def test_version_override_handles_eval_and_gitless_fallback(self):
        source = self.repository()
        kbuild = source / "kernel/Kbuild"
        kbuild.write_text("""KSU_GIT_VERSION := 7
KSU_VERSION_API := 19
KSU_VERSION_FULL := keep
ifdef KSU_GIT_VERSION_VALID
$(eval KSU_VERSION=$(shell expr 30000 + $(KSU_GIT_VERSION)))
ccflags-y += -DKSU_VERSION=$(KSU_VERSION)
else
KSU_VERSION_FALLBACK := 1
ccflags-y += -DKSU_VERSION=$(KSU_VERSION_FALLBACK)
endif
""")
        ksu.override_version(source, 40819)
        target = "show:\n\t@echo '$(ccflags-y)|$(KSU_VERSION_API)|$(KSU_VERSION_FULL)'\n"
        for extra in ([], ["KSU_GIT_VERSION_VALID=1"]):
            output = subprocess.check_output(
                ["make", "-s", "-f", str(kbuild), "-f", "-", *extra, "show"], input=target, text=True,
            ).strip()
            self.assertEqual(output, "-DKSU_VERSION=40819|19|keep")

    def test_failed_patch_preflight_does_not_partially_modify_source(self):
        source = self.repository(susfs=False)
        self.resolve("ksu", ref="main")
        directory = self.patch_directory("""--- a/kernel/Kconfig
+++ b/kernel/Kconfig
@@ -1,3 +1,5 @@
 config KSU
     bool "KernelSU"
     default y
+config KSU_SUSFS
+    bool "SUSFS"
--- a/kernel/Kbuild
+++ b/kernel/Kbuild
@@ -1 +1 @@
-missing context
+replacement
""")
        original = (source / "kernel/Kconfig").read_bytes()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(ValueError):
            ksu.apply_susfs(source, directory)
        self.assertEqual((source / "kernel/Kconfig").read_bytes(), original)
        self.assertEqual(list(source.rglob("*.rej")), [])

    def test_automatic_ref_can_select_fully_checked_compatible_commit(self):
        source = self.repository(susfs=False)
        compatible = self.git(source, "rev-parse", "HEAD")
        config = source / "kernel/Kconfig"
        config.write_text(config.read_text().replace('bool "KernelSU"', 'bool "Changed"'))
        self.commit(source)
        self.resolve("ksu", override="40819")
        os.environ["KSU_SOURCE_DIR"] = str(source)
        directory = self.patch_directory("""--- a/kernel/Kconfig
+++ b/kernel/Kconfig
@@ -1,3 +1,5 @@
 config KSU
     bool "KernelSU"
     default y
+config KSU_SUSFS
+    bool "SUSFS"
""")
        with mock.patch.object(ksu, "SUSFS_COMPAT_REVISIONS", (compatible,)):
            ksu.finalize(directory)
        self.assertEqual(self.git(source, "rev-parse", "HEAD"), compatible)
        self.assertTrue(ksu.has_config(source, "KSU_SUSFS"))
        self.assertEqual(self.emitted()["KSUVER"], "40819")
        self.assertIn("KSU_VERSION := 40819", (source / "kernel/Kbuild").read_text())

    def test_explicit_ref_does_not_switch_to_a_compatible_older_commit(self):
        source = self.repository(susfs=False)
        compatible = self.git(source, "rev-parse", "HEAD")
        config = source / "kernel/Kconfig"
        config.write_text(config.read_text().replace('bool "KernelSU"', 'bool "Changed"'))
        current = self.commit(source)
        original = config.read_bytes()
        self.resolve("ksu", ref="main")
        os.environ["KSU_SOURCE_DIR"] = str(source)
        directory = self.patch_directory("""--- a/kernel/Kconfig
+++ b/kernel/Kconfig
@@ -1,3 +1,5 @@
 config KSU
     bool "KernelSU"
     default y
+config KSU_SUSFS
+    bool "SUSFS"
""")
        with mock.patch.object(ksu, "SUSFS_COMPAT_REVISIONS", (compatible,)):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(ValueError):
                ksu.finalize(directory)
        self.assertEqual(self.git(source, "rev-parse", "HEAD"), current)
        self.assertEqual(config.read_bytes(), original)
        self.assertNotIn("KSUVER", self.emitted())


if __name__ == "__main__":
    unittest.main()
