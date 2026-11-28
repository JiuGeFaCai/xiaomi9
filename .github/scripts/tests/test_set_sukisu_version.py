"""Evaluate upstream Make rules and compile the resulting version definitions."""

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "set_sukisu_version.py"
SPEC = importlib.util.spec_from_file_location("sukisu_version", SCRIPT)
sukisu_version = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sukisu_version)

UPSTREAM = r'''MDIR := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
REPO_NAME := SukiSU-Nova
REPO_BRANCH := main
VERSION_BASE := 30000
VERSION_OFFSET := 4001
GIT_BIN := git
CURL_BIN := curl
KSU_SRC := $(MDIR)
git_short_sha = $(shell cd $(KSU_SRC) && $(GIT_BIN) rev-parse --short=8 HEAD)
git_branch = $(shell cd $(KSU_SRC) && $(GIT_BIN) rev-parse --abbrev-ref HEAD)
git_commit_count = $(shell cd $(KSU_SRC) && $(GIT_BIN) rev-list --count $(REPO_BRANCH))
GITHUB_COMMITS := $(shell $(CURL_BIN) -sI "https://version.invalid/commits")
LOCAL_COUNT := $(if $(GITHUB_COMMITS),$(GITHUB_COMMITS),$(git_commit_count))
KSU_VERSION := $(if $(LOCAL_COUNT),$(shell expr $(VERSION_BASE) + $(LOCAL_COUNT) - $(VERSION_OFFSET)),13000)
KSU_VERSION_FALLBACK := 13000
VERSION_TAG := 3.0.0
KSU_VERSION_FULL := $(if $(call git_short_sha),v$(VERSION_TAG)-$(call git_short_sha)@$(call git_branch),unknown)
KSU_VERSION_API := 30000
KSU_PACKAGE_NAME ?= com.sukisu.nova
ccflags-y += -DKSU_VERSION=$(KSU_VERSION)
ccflags-y += -DKSU_VERSION_FULL=\"$(KSU_VERSION_FULL)\"
ccflags-y += -DKSU_VERSION_API=$(KSU_VERSION_API)
'''


@unittest.skipUnless(shutil.which("make") and shutil.which("curl"), "make and curl are required")
class SukiSUVersionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "KernelSU"
        self.kernel = self.repo / "kernel"
        self.kernel.mkdir(parents=True)
        self.path = self.kernel / "Kbuild"
        self.path.write_text(UPSTREAM)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.curl_log = self.root / "curl-args"
        (self.bin / "curl").write_text(
            '#!/bin/sh\nprintf "%s\\n" "$@" >> "$CURL_ARGS_LOG"\n'
            '[ "${UPSTREAM_OFFLINE:-0}" = 1 ] || printf "%s" "${UPSTREAM_COUNT:-5001}"\n'
        )
        (self.bin / "curl").chmod(0o755)
        (self.bin / "git").write_text(
            '#!/bin/sh\ncase "$1" in\n'
            '  rev-list) printf "%s\\n" "${LOCAL_COUNT:-5001}" ;;\n'
            '  rev-parse) case "$2" in\n'
            '    --short=8) printf "1234abcd\\n" ;;\n'
            '    --abbrev-ref) printf "HEAD\\n" ;;\n'
            '    *) exit 1 ;;\n'
            '  esac ;;\n'
            '  *) exit 1 ;;\n'
            'esac\n'
        )
        (self.bin / "git").chmod(0o755)
        environment = {
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "CURL_ARGS_LOG": str(self.curl_log),
            "UPSTREAM_COUNT": "5001", "LOCAL_COUNT": "5001", "UPSTREAM_OFFLINE": "0",
        }
        self.env = patch.dict(os.environ, environment)
        self.env.start()
        self.addCleanup(self.env.stop)

    def configure(self, source=UPSTREAM, filename="Kbuild"):
        self.path.unlink(missing_ok=True)
        self.path = self.kernel / filename
        self.path.write_text(source)

    def build_version(self):
        compiler = shutil.which("cc")
        if not compiler:
            self.skipTest("a C compiler is required")
        (self.kernel / "version.c").write_text(
            '#include <stdio.h>\nint main(void) { printf("%d\\n%s\\n", KSU_VERSION, KSU_VERSION_FULL); }\n'
        )
        makefile = f"include kernel/{self.path.name}\n"
        makefile += (
            ".PHONY: version_test\nversion_test:\n"
            "\t$(CC) $(ccflags-y) $(CFLAGS_core/init.o) kernel/version.c -o version-test\n"
        )
        subprocess.run(
            ["make", "--no-print-directory", "-s", "-f", "-", "version_test", f"CC={compiler}"],
            cwd=self.repo, input=makefile, text=True, capture_output=True, check=True,
        )
        output = subprocess.run(
            [str(self.repo / "version-test")], text=True, capture_output=True, check=True,
        ).stdout.splitlines()
        return int(output[0]), output[1]

    def test_nova_builtin_and_main_use_their_own_rules(self):
        cases = (
            (UPSTREAM, "Makefile", 31000),
            (UPSTREAM.replace("VERSION_OFFSET := 4001", "VERSION_OFFSET := 996")
             .replace("- $(VERSION_OFFSET)", "+ $(VERSION_OFFSET)")
             .replace("REPO_NAME := SukiSU-Nova", "REPO_NAME := SukiSU"), "Kbuild", 35997),
        )
        for source, filename, expected in cases:
            with self.subTest(filename=filename):
                self.configure(source, filename)
                metadata = sukisu_version.set_version(self.repo, "builtin")
                self.assertEqual(metadata["name"], "SukiSU_Nova")
                self.assertEqual(metadata["version"], expected)
                self.assertEqual(metadata["full"], "v3.0.0-1234abcd@builtin")
                self.assertEqual(self.build_version(), (expected, metadata["full"]))
        self.assertIn("--connect-timeout\n5\n--max-time\n10\n", self.curl_log.read_text())

    def test_split_kbuild_pins_versions_in_per_object_flags(self):
        kbuild = (
            "MDIR := $(realpath $(dir $(abspath $(lastword $(MAKEFILE_LIST)))))\n"
            "KSU_KBUILD_DIR := $(MDIR)/kbuild-config\n"
            "include $(KSU_KBUILD_DIR)/version.mk\n"
            "include $(KSU_KBUILD_DIR)/manager.mk\n"
        )
        self.configure(kbuild)
        fragments = self.kernel / "kbuild-config"
        fragments.mkdir()
        (fragments / "manager.mk").write_text("KSU_PACKAGE_NAME := com.sukisu.nova\n")
        (fragments / "version.mk").write_text(
            UPSTREAM.split("\n", 1)[1]
            .replace("REPO_NAME := SukiSU-Nova", "REPO_NAME := SukiSU")
            .replace("VERSION_OFFSET := 4001", "VERSION_OFFSET := 996")
            .replace("- $(VERSION_OFFSET)", "+ $(VERSION_OFFSET)")
            .replace("KSU_PACKAGE_NAME ?= com.sukisu.nova\n", "")
            .replace("ccflags-y", "CFLAGS_core/init.o")
        )

        for override, suffix, version in (("", "", 35997), ("0035000", "调试 #1", 35000)):
            with self.subTest(override=override, suffix=suffix):
                metadata = sukisu_version.set_version(self.repo, "builtin", override, suffix)
                full = "v3.0.0-1234abcd@builtin" + (f"-{suffix}" if suffix else "")
                self.assertEqual(metadata, {"name": "SukiSU_Nova", "version": version, "full": full})
                self.assertEqual(self.build_version(), (version, full))
        self.assertEqual(self.path.read_text(), kbuild)

    def test_old_ultra_keeps_its_version_formula_and_package(self):
        source = (UPSTREAM.replace("VERSION_BASE := 30000", "VERSION_BASE := 40000")
                  .replace("VERSION_OFFSET := 4001", "VERSION_OFFSET := 2815")
                  .replace("REPO_NAME := SukiSU-Nova", "REPO_NAME := SukiSU-Ultra")
                  .replace("3.0.0", "4.2.0").replace("com.sukisu.nova", "com.sukisu.ultra"))
        self.configure(source)
        metadata = sukisu_version.set_version(self.repo, "fd2a851", suffix="custom")
        self.assertEqual(metadata["name"], "SukiSU_Ultra")
        self.assertEqual(metadata["version"], 42186)
        self.assertEqual(metadata["full"], "v4.2.0-1234abcd@fd2a851-custom")
        self.assertIn("KSU_PACKAGE_NAME ?= com.sukisu.ultra", self.path.read_text())
        self.assertIn("KSU_VERSION_API := 30000", self.path.read_text())
        self.assertEqual(self.build_version(), (42186, metadata["full"]))

    def test_manual_override_and_repeated_runs_match_the_binary(self):
        for override, suffix in (("0035000", "first"), ("0", ""), ("35001", "second")):
            with self.subTest(override=override):
                metadata = sukisu_version.set_version(self.repo, "v3.0.0", override, suffix)
                self.assertEqual(metadata["version"], int(override))
                expected = "v3.0.0-1234abcd@v3.0.0" + ("-" + suffix if suffix else "")
                self.assertEqual(metadata["full"], expected)
                self.assertEqual(self.build_version(), (int(override), expected))
                self.assertIn(f"KSU_VERSION_FALLBACK := {int(override)}", self.path.read_text())

    def test_upstream_count_is_preferred_and_version_is_pinned(self):
        with patch.dict(os.environ, {"UPSTREAM_COUNT": "6001", "LOCAL_COUNT": "5001"}):
            metadata = sukisu_version.set_version(self.repo, "builtin")
        self.assertEqual(metadata["version"], 32000)
        with patch.dict(os.environ, {"UPSTREAM_COUNT": "7001"}):
            self.assertEqual(self.build_version(), (32000, metadata["full"]))

    def test_unavailable_api_uses_upstream_local_fallback(self):
        with patch.dict(os.environ, {"UPSTREAM_OFFLINE": "1"}):
            metadata = sukisu_version.set_version(self.repo, "builtin")
        self.assertEqual(metadata["version"], 31000)

    def test_suffix_is_literal_through_make_shell_and_c(self):
        suffix = '中文 \'"\\#&|$(touch injected)`touch injected` with spaces '
        ref = "feature/test"
        metadata = sukisu_version.set_version(self.repo, ref, suffix=suffix)
        self.assertEqual(metadata["full"], "v3.0.0-1234abcd@" + ref + "-" + suffix)
        self.assertEqual(self.build_version(), (31000, metadata["full"]))
        self.assertFalse((self.repo / "injected").exists())

    def test_kbuild_takes_precedence_over_an_external_makefile(self):
        external = self.kernel / "Makefile"
        wrapper = "all:\n\t$(MAKE) -C /missing/kernel M=$(CURDIR)\n"
        external.write_text(wrapper)
        sukisu_version.set_version(self.repo, "builtin")
        self.assertEqual(external.read_text(), wrapper)

    def test_multiline_definition_is_replaced_as_one_make_assignment(self):
        source = UPSTREAM.replace("KSU_VERSION := $(if $(LOCAL_COUNT),", "KSU_VERSION := \\\n    $(if $(LOCAL_COUNT),")
        self.configure(source)
        metadata = sukisu_version.set_version(self.repo, "builtin")
        self.assertEqual(self.build_version(), (31000, metadata["full"]))

    def test_invalid_inputs_or_metadata_do_not_modify_sources(self):
        for arguments in (
            {"ref": "builtin", "suffix": "bad\nline"},
            {"ref": "bad\rref"},
            {"ref": "builtin", "version": "v35000"},
            {"ref": ""},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                sukisu_version.set_version(self.repo, **arguments)
            self.assertEqual(self.path.read_text(), UPSTREAM)
        source = UPSTREAM.replace("KSU_VERSION := $(if $(LOCAL_COUNT),", "KSU_VERSION := $(if $(MISSING),")
        source = source.replace(")),13000)", ")),invalid)")
        self.configure(source)
        with self.assertRaises(ValueError):
            sukisu_version.set_version(self.repo, "builtin")
        self.assertEqual(self.path.read_text(), source)

    def test_cli_returns_metadata_for_workflow_without_extra_make_output(self):
        result = subprocess.run(
            ["python3", str(SCRIPT), str(self.repo), "--ref", "builtin", "--suffix", "source"],
            text=True, capture_output=True, check=True,
        )
        self.assertEqual(result.stdout, "SukiSU_Nova\n31000\nv3.0.0-1234abcd@builtin-source\n")


if __name__ == "__main__":
    unittest.main()
