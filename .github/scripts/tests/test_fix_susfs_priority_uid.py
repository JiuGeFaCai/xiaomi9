"""Check old SUSFS, upstream fixes, and compiler-visible priority UID calls."""

import contextlib
import importlib.util
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "fix_susfs_priority_uid.py"
SPEC = importlib.util.spec_from_file_location("priority_uid", SCRIPT)
priority = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(priority)

OBSERVER = """void observe(void)
{
    susfs_parse_priority_app_strings();
}
"""
DISPATCH = """int dispatch(unsigned int cmd, void **arg)
{
    switch (cmd) {
        case CMD_EXISTING:
            return 7;
        case CMD_SUSFS_SET_PRIORITY_UID_FILTER:
            susfs_set_priority_uid_filter(arg);
            return 0;
        default:
            return -22;
    }
}
"""


class PriorityUidTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.observer = self.root / "drivers/kernelsu/manager/pkg_observer.c"
        self.dispatch = self.root / "drivers/kernelsu/supercall/dispatch.c"
        self.header = self.root / "include/linux/susfs.h"
        self.definitions = self.root / "include/linux/susfs_def.h"
        self.implementation = self.root / "fs/susfs.c"
        for path in (self.observer, self.dispatch, self.header, self.definitions, self.implementation):
            path.parent.mkdir(parents=True, exist_ok=True)
        self.observer.write_text(OBSERVER)
        self.dispatch.write_text(DISPATCH)
        self.header.write_text("#define SUSFS_VERSION \"v2.3.0\"\n")
        self.definitions.write_text("#define CMD_EXISTING 1\n")
        self.implementation.write_text("/* old SUSFS */\n")

    def fix(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return priority.fix(self.root)

    def add_complete_api(self):
        self.definitions.write_text(self.definitions.read_text() +
                                    f"#define {priority.COMMAND} 0x60030\n")
        self.header.write_text(self.header.read_text() +
                               f"void {priority.PARSE}(void);\n"
                               f"void {priority.SET}(void **arg);\n")
        self.implementation.write_text(
            f"void {priority.PARSE}(void) {{}}\n"
            f"void {priority.SET}(void **arg) {{}}\n",
        )

    def test_old_susfs_is_guarded_and_a_second_run_is_unchanged(self):
        self.assertEqual(set(self.fix()), {self.observer, self.dispatch})
        guarded = (self.observer.read_bytes(), self.dispatch.read_bytes())
        self.assertIn("case CMD_EXISTING:\n            return 7;", self.dispatch.read_text())
        self.assertEqual(self.fix(), [])
        self.assertEqual(guarded, (self.observer.read_bytes(), self.dispatch.read_bytes()))

    def test_new_susfs_is_untouched_even_with_the_same_version_string(self):
        self.add_complete_api()
        self.assertEqual(self.fix(), [])
        self.assertEqual(self.observer.read_text(), OBSERVER)
        self.assertEqual(self.dispatch.read_text(), DISPATCH)

    def test_upstream_guards_are_not_duplicated(self):
        self.observer.write_text(OBSERVER.replace(
            "    susfs_parse_priority_app_strings();\n",
            "#ifdef CMD_SUSFS_SET_PRIORITY_UID_FILTER\n"
            "    susfs_parse_priority_app_strings();\n#endif\n",
        ))
        self.dispatch.write_text("#if defined(CMD_SUSFS_SET_PRIORITY_UID_FILTER)\n" +
                                 DISPATCH + "#endif\n")
        originals = (self.observer.read_bytes(), self.dispatch.read_bytes())
        self.assertEqual(self.fix(), [])
        self.assertEqual(originals, (self.observer.read_bytes(), self.dispatch.read_bytes()))

    def test_upstream_removal_and_renamed_layout_are_skipped(self):
        self.observer.write_text("void observe(void) {}\n")
        self.dispatch.unlink()
        self.assertEqual(self.fix(), [])

    def test_comments_do_not_pretend_an_api_is_available(self):
        self.definitions.write_text("/* #define CMD_SUSFS_SET_PRIORITY_UID_FILTER 1 */\n")
        self.header.write_text("/* void susfs_parse_priority_app_strings(void); */\n")
        self.implementation.write_text("// void susfs_parse_priority_app_strings(void) {}\n")
        self.assertEqual(len(self.fix()), 2)

    def test_command_without_implementations_fails_before_writing(self):
        self.add_complete_api()
        self.implementation.write_text("/* missing definitions */\n")
        with self.assertRaisesRegex(SystemExit, "incomplete"):
            self.fix()
        self.assertEqual(self.observer.read_text(), OBSERVER)
        self.assertEqual(self.dispatch.read_text(), DISPATCH)

    def test_unknown_call_layout_fails_without_partial_edits(self):
        changed_dispatch = DISPATCH.replace("(arg);", "(different_arg);")
        self.dispatch.write_text(changed_dispatch)
        with self.assertRaisesRegex(SystemExit, "no files written"):
            self.fix()
        self.assertEqual(self.observer.read_text(), OBSERVER)
        self.assertEqual(self.dispatch.read_text(), changed_dispatch)

    def test_an_or_condition_does_not_count_as_an_upstream_guard(self):
        self.observer.write_text("#if defined(CONFIG_KSU_SUSFS) || "
                                 "defined(CMD_SUSFS_SET_PRIORITY_UID_FILTER)\n" +
                                 OBSERVER + "#endif\n")
        self.assertEqual(len(self.fix()), 2)
        self.assertIn(priority.GUARD, self.observer.read_text())

    def test_function_name_comments_do_not_trigger_edits(self):
        self.observer.write_text("// susfs_parse_priority_app_strings();\n")
        self.dispatch.write_text("/* CMD_SUSFS_SET_PRIORITY_UID_FILTER */\n")
        self.assertEqual(self.fix(), [])

    def test_clang_reproduces_the_failure_and_keeps_new_api_active(self):
        compiler = shutil.which("clang")
        if not compiler:
            self.skipTest("clang is not installed")
        source = ("#include \"include/linux/susfs_def.h\"\n"
                  "#include \"include/linux/susfs.h\"\n"
                  "#include \"drivers/kernelsu/manager/pkg_observer.c\"\n"
                  "#include \"drivers/kernelsu/supercall/dispatch.c\"\n")
        unit = self.root / "unity.c"
        unit.write_text(source)
        flags = [compiler, "-std=gnu11", "-Werror=implicit-function-declaration",
                 "-fsyntax-only", "-DCONFIG_KSU_SUSFS", str(unit)]
        before = subprocess.run(flags, capture_output=True, text=True)
        self.assertNotEqual(before.returncode, 0)
        for name in (priority.PARSE, priority.SET, priority.COMMAND):
            self.assertIn(name, before.stderr)
        self.fix()
        subprocess.run(flags, check=True, capture_output=True, text=True)
        subprocess.run([flag for flag in flags if flag != "-DCONFIG_KSU_SUSFS"],
                       check=True, capture_output=True, text=True)
        self.add_complete_api()
        subprocess.run(flags, check=True, capture_output=True, text=True)
        expanded = subprocess.check_output(
            [compiler, "-E", "-P", "-DCONFIG_KSU_SUSFS", str(unit)], text=True,
        )
        self.assertIn("susfs_parse_priority_app_strings();", expanded)
        self.assertIn("susfs_set_priority_uid_filter(arg);", expanded)


if __name__ == "__main__":
    unittest.main()
