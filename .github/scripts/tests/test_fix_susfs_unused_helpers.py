"""Reproduce SUSFS credential warnings with the optional callers disabled."""

import contextlib
import importlib.util
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "fix_susfs_unused_helpers.py"
SPEC = importlib.util.spec_from_file_location("susfs_unused_helpers", SCRIPT)
helpers = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(SCRIPT.parent))
try:
    SPEC.loader.exec_module(helpers)
finally:
    sys.path.pop(0)

PREAMBLE = """#define __maybe_unused __attribute__((__unused__))
/* linux/compiler_types.h marks ordinary kernel inline functions unused. */
#define inline inline __attribute__((__gnu_inline__, __unused__))
struct cred { int sid; };
extern struct cred *prepare_creds(void);
extern void commit_creds(struct cred *cred);
"""
DEFINITIONS = """static struct cred *susfs_switch_to_ksu_domain(void) {
    return prepare_creds();
}

static void susfs_restore_creds(struct cred *orig_cred) {
    if (orig_cred)
        commit_creds(orig_cred);
}
"""
CALLERS = """
#ifdef CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER
void susfs_parse_priority_app_strings(void) {
    struct cred *orig_cred = susfs_switch_to_ksu_domain();
    susfs_restore_creds(orig_cred);
}
#endif
"""
SOURCE = PREAMBLE + DEFINITIONS + CALLERS


class SusfsUnusedHelpersTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "fs/susfs.c"
        self.source.parent.mkdir()
        self.source.write_text(SOURCE)

    def fix(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return helpers.fix(self.root)

    def compile(self, enabled=False):
        compiler = shutil.which("clang")
        if not compiler:
            self.skipTest("clang is required")
        command = [compiler, "-std=gnu11", "-Wall", "-Wextra", "-Werror",
                   "-Wunused-function", "-fsyntax-only", str(self.source)]
        if enabled:
            command.append("-DCONFIG_KSU_SUSFS_PRIORITY_UID_FILTER")
        return subprocess.run(command, capture_output=True, text=True)

    def test_clang_reproduces_failure_then_accepts_both_configurations(self):
        self.assertEqual(self.compile(enabled=True).returncode, 0)
        before = self.compile()
        self.assertNotEqual(before.returncode, 0)
        for name in helpers.HELPERS:
            self.assertIn(f"unused function '{name}'", before.stderr)
        self.assertEqual(self.fix(), [self.source])
        for enabled in (False, True):
            result = self.compile(enabled)
            self.assertEqual(result.returncode, 0, result.stderr)
        fixed = self.source.read_text()
        self.assertEqual(fixed.replace("static __maybe_unused ", "static "), SOURCE)
        self.assertEqual(self.fix(), [])
        self.assertEqual(self.source.read_text(), fixed)

    def test_unrelated_unused_function_is_still_an_error(self):
        self.source.write_text(SOURCE + "\nstatic void unrelated_helper(void) {}\n")
        self.fix()
        result = self.compile()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unused function 'unrelated_helper'", result.stderr)
        for name in helpers.HELPERS:
            self.assertNotIn(f"unused function '{name}'", result.stderr)

    def test_upstream_annotations_inline_and_guards_are_untouched(self):
        variants = [
            DEFINITIONS.replace("static ", "static __maybe_unused "),
            DEFINITIONS.replace("static ", "static __attribute__((unused)) "),
            DEFINITIONS.replace("static ", "static inline "),
            DEFINITIONS.replace("*susfs_switch", "* __maybe_unused susfs_switch")
                       .replace("void susfs_restore", "void __maybe_unused susfs_restore"),
            "#ifdef CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER\n" + DEFINITIONS + "#endif\n",
            "#if defined(CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER)\n" + DEFINITIONS + "#endif\n",
        ]
        for definitions in variants:
            with self.subTest(definitions=definitions):
                source = PREAMBLE + definitions + CALLERS
                self.source.write_text(source)
                self.assertEqual(self.fix(), [])
                self.assertEqual(self.source.read_text(), source)
                for enabled in (False, True):
                    result = self.compile(enabled)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_partial_upstream_fix_is_completed_independently(self):
        source = SOURCE.replace("static struct cred", "static __maybe_unused struct cred")
        self.source.write_text(source)
        self.assertEqual(self.fix(), [self.source])
        self.assertEqual(self.source.read_text(), SOURCE.replace("static ", "static __maybe_unused "))
        result = self.compile()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_removed_or_changed_helpers_are_untouched(self):
        self.source.unlink()
        self.assertEqual(self.fix(), [])
        for source in ("/* upstream removed unused helpers */\n",
                       SOURCE.replace("susfs_switch_to_ksu_domain", "new_switch")
                             .replace("susfs_restore_creds", "new_restore"),
                       DEFINITIONS.replace("static ", "")):
            with self.subTest(source=source):
                self.source.write_text(source)
                self.assertEqual(self.fix(), [])
                self.assertEqual(self.source.read_text(), source)

    def test_comments_strings_and_declarations_are_not_definitions(self):
        source = ('/*\n' + DEFINITIONS + '*/\n' +
                  'const char *example = "static void susfs_restore_creds(void) { }";\n' +
                  'static struct cred *susfs_switch_to_ksu_domain(void);\n' +
                  'static void susfs_restore_creds(struct cred *);\n')
        self.source.write_text(source)
        self.assertEqual(self.fix(), [])
        self.assertEqual(self.source.read_text(), source)

    def test_multiline_definitions_and_comments_preserve_function_bodies(self):
        source = SOURCE.replace("static struct cred *", "static /* helper */\nstruct cred *\n")
        source = source.replace("static void susfs_restore_creds", "static\nvoid\nsusfs_restore_creds")
        self.source.write_text(source)
        self.assertEqual(self.fix(), [self.source])
        self.assertEqual(self.source.read_text().replace("static __maybe_unused", "static"), source)
        result = self.compile()
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
