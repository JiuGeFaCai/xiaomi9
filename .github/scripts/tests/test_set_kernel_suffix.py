"""Execute patched setlocalversion scripts with modern and legacy output forms."""

import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "set_kernel_suffix.py"
SPEC = importlib.util.spec_from_file_location("kernel_suffix", SCRIPT)
kernel_suffix = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(kernel_suffix)

MODERN = '''#!/bin/sh
set -e
scm_version() { printf '%s' '-upstream-dirty'; }
if [ "${1-}" = "--no-local" ]; then
    echo "${KERNELVERSION}$(scm_version --no-dirty)"
    exit 0
fi
file_localversion=-file
config_localversion=-4k
LOCALVERSION=-local
scm_version=$(scm_version)
echo "${KERNELVERSION}${file_localversion}${config_localversion}${LOCALVERSION}${scm_version}"
'''
LEGACY = '''#!/bin/sh
set -e
collect_files() {
    local res=-file
    echo "$res"
}
if [ "${1-}" = "--save-scmversion" ]; then
    printf '%s\\n' '-upstream' > .scmversion
    exit 0
fi
res="$(collect_files)"
res="$res-4k"
res="$res-dirty"
echo "$res"
'''


class KernelSuffixTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "scripts/setlocalversion"
        self.path.parent.mkdir()

    def patch(self, original, suffix="-android17-1-o-g0123456789abcd", build_number=""):
        self.path.write_text(original)
        self.path.chmod(0o755)
        kernel_suffix.set_suffix(self.root, suffix, build_number)

    def run_script(self, *args, **env):
        return subprocess.run(
            ["sh", str(self.path), *args], cwd=self.root,
            env={**os.environ, "KERNELVERSION": "6.18.21", **env},
            text=True, capture_output=True, check=True,
        ).stdout

    def test_modern_output_is_one_full_release_line(self):
        suffix = "-android17-1-o-g0123456789abcd"
        self.patch(MODERN, suffix)
        self.assertEqual(self.run_script(), "6.18.21" + suffix + "\n")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o755)
        # The helper's echo and the --no-local path must not be patched.
        self.assertEqual(self.path.read_text().splitlines()[:-1], MODERN.splitlines()[:-1])
        self.assertEqual(self.run_script("--no-local"), "6.18.21-upstream-dirty\n")

    def test_legacy_output_is_only_the_suffix(self):
        self.patch(LEGACY, "-android14-custom")
        self.assertEqual(self.run_script(), "-android14-custom\n")
        self.assertEqual(self.path.read_text().splitlines()[:-1], LEGACY.splitlines()[:-1])
        self.assertEqual(self.run_script("--save-scmversion"), "")
        self.assertEqual((self.root / ".scmversion").read_text(), "-upstream\n")

    def test_selected_build_number_survives_custom_output_and_is_not_duplicated(self):
        for source, prefix in ((MODERN, "6.18.21"), (LEGACY, "")):
            for suffix in ("-android17-test", "-android17-test-ab016372235"):
                with self.subTest(prefix=prefix, suffix=suffix):
                    self.patch(source, suffix, "016372235")
                    self.assertEqual(self.run_script(BUILD_NUMBER=""), prefix + "-android17-test-ab016372235\n")

    def test_number_only_keeps_upstream_output_and_early_exits(self):
        legacy = LEGACY.replace('res="$res-dirty"', 'res="$res-dirty${BUILD_NUMBER:+-ab$BUILD_NUMBER}"')
        for source, expected in ((MODERN, "6.18.21-file-4k-local-upstream-dirty"), (legacy, "-file-4k-dirty")):
            with self.subTest(expected=expected):
                self.patch(source, None, "16372235")
                for number in ("", "16372235"):
                    self.assertEqual(self.run_script(BUILD_NUMBER=number), expected + "-ab16372235\n")
                if source == MODERN:
                    self.assertEqual(self.run_script("--no-local"), "6.18.21-upstream-dirty\n")
                else:
                    self.assertEqual(self.run_script("--save-scmversion"), "")
                    self.assertEqual((self.root / ".scmversion").read_text(), "-upstream\n")

    def test_invalid_build_number_does_not_modify_source(self):
        self.path.write_text(MODERN)
        for number in ("", "x123", "123\n"):
            with self.subTest(number=number), self.assertRaises(ValueError):
                kernel_suffix.set_suffix(self.root, None, number)
            self.assertEqual(self.path.read_text(), MODERN)

    def test_shell_characters_are_literal_and_cannot_execute_commands(self):
        suffix = "-android17-'\"\\&|$(touch injected)`touch injected`"
        for source, prefix in ((MODERN, "6.18.21"), (LEGACY, "")):
            with self.subTest(prefix=prefix):
                self.patch(source, suffix)
                self.assertEqual(self.run_script(), prefix + suffix + "\n")
                self.assertFalse((self.root / "injected").exists())

    def test_unknown_output_does_not_modify_source(self):
        original = MODERN + "# different upstream layout\n"
        self.path.write_text(original)
        with self.assertRaisesRegex(ValueError, "unrecognized"):
            kernel_suffix.set_suffix(self.root, "-android17-test")
        self.assertEqual(self.path.read_text(), original)

    def test_invalid_upstream_shell_does_not_modify_source(self):
        original = MODERN.replace("set -e", 'broken="')
        self.path.write_text(original)
        with self.assertRaisesRegex(ValueError, "syntax check failed"):
            kernel_suffix.set_suffix(self.root, "-android17-test")
        self.assertEqual(self.path.read_text(), original)

    def test_invalid_suffix_does_not_modify_source(self):
        self.path.write_text(MODERN)
        for suffix in ("android17", "-android17\nextra", "-android17\rextra", "-android17\0"):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                kernel_suffix.set_suffix(self.root, suffix)
            self.assertEqual(self.path.read_text(), MODERN)


if __name__ == "__main__":
    unittest.main()
