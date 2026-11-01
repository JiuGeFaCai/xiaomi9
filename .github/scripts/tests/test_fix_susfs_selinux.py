"""Reproduce missing SUSFS SELinux symbols and verify optional hooks at link time."""

import contextlib
import importlib.util
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "fix_susfs_selinux.py"
SPEC = importlib.util.spec_from_file_location("susfs_selinux", SCRIPT)
selinux = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(SCRIPT.parent))
try:
    SPEC.loader.exec_module(selinux)
finally:
    sys.path.pop(0)

TYPE_HOOK, RULE_HOOK = selinux.HOOKS
SEPOLICY = """typedef unsigned int u32;
typedef unsigned short u16;
struct type_datum { u32 value; };
struct class_datum { u16 value; };
struct avtab_node { struct { union { u32 data; } u; } datum; };

#ifdef CONFIG_KSU_SUSFS
extern int susfs_add_sus_avd_allowed_by_type(u32 s_type, u32 t_type, u16 type, u32 orig_avd_allowed);
extern int susfs_add_sus_type(const char *secctx, u32 type_value);
#endif // #ifdef CONFIG_KSU_SUSFS

static void add_rule_raw(struct type_datum *src, struct type_datum *tgt,
                         struct class_datum *cls, struct avtab_node *node)
{
#ifdef CONFIG_KSU_SUSFS
        susfs_add_sus_avd_allowed_by_type(src->value, tgt->value, cls->value, node->datum.u.data);
#endif // #ifdef CONFIG_KSU_SUSFS
    node->datum.u.data |= 4U;
}

static int add_type(const char *type_name, struct type_datum *type)
{
#ifdef CONFIG_KSU_SUSFS
    susfs_add_sus_type(type_name, type->value);
#endif // #ifdef CONFIG_KSU_SUSFS
    type->value += 2U;
    return 1;
}

void policy_entry(void)
{
    struct type_datum src = {1}, tgt = {2};
    struct class_datum cls = {3};
    struct avtab_node node = {0};
    add_rule_raw(&src, &tgt, &cls, &node);
    add_type("ksu", &src);
}
"""
IMPLEMENTATIONS = {
    TYPE_HOOK: """int susfs_add_sus_type(const char *secctx, u32 type_value)
{
    return type_value;
}
""",
    RULE_HOOK: """int susfs_add_sus_avd_allowed_by_type(u32 s_type, u32 t_type,
                                      u16 type, u32 orig_avd_allowed)
{
    return orig_avd_allowed;
}
""",
}


class SusfsSelinuxTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sepolicy = self.root / "drivers/kernelsu/selinux/sepolicy.c"
        self.implementation = self.root / "fs/susfs.c"
        self.header = self.root / "include/linux/susfs.h"
        for path in (self.sepolicy, self.implementation, self.header):
            path.parent.mkdir(parents=True, exist_ok=True)
        self.sepolicy.write_text(SEPOLICY)
        self.implementation.write_text("/* old SUSFS has no SELinux recording hooks */\n")
        self.header.write_text('#define SUSFS_VERSION "v2.3.0"\n')

    def fix(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return selinux.fix(self.root)

    def provide(self, *names):
        self.implementation.write_text("typedef unsigned int u32;\n"
                                       "typedef unsigned short u16;\n" +
                                       "".join(IMPLEMENTATIONS[name] for name in names))

    def test_old_susfs_disables_hooks_and_preserves_policy_changes(self):
        self.assertEqual(self.fix(), [self.sepolicy])
        text = self.sepolicy.read_text()
        self.assertIn("node->datum.u.data |= 4U;", text)
        self.assertIn("type->value += 2U;", text)
        for capability in selinux.HOOKS.values():
            self.assertIn(f"#undef {capability}", text)
            self.assertNotIn(f"#define {capability} 1", text)
        self.assertEqual(self.fix(), [])
        self.assertEqual(text, self.sepolicy.read_text())

    def test_new_susfs_is_untouched_with_the_same_version_string(self):
        self.provide(*selinux.HOOKS)
        self.assertEqual(self.fix(), [])
        self.assertEqual(self.sepolicy.read_text(), SEPOLICY)

    def test_each_hook_is_checked_independently(self):
        self.provide(TYPE_HOOK)
        self.fix()
        text = self.sepolicy.read_text()
        self.assertIn(f"#define {selinux.HOOKS[TYPE_HOOK]} 1", text)
        self.assertNotIn(f"#define {selinux.HOOKS[RULE_HOOK]} 1", text)

    def test_declarations_comments_and_static_functions_are_not_linkable_providers(self):
        self.header.write_text(SEPOLICY)
        self.implementation.write_text(
            f"extern int {TYPE_HOOK}(const char *, u32);\n" +
            "/* " + IMPLEMENTATIONS[RULE_HOOK] + " */\n" +
            "static " + IMPLEMENTATIONS[TYPE_HOOK],
        )
        self.fix()
        for capability in selinux.HOOKS.values():
            self.assertNotIn(f"#define {capability} 1", self.sepolicy.read_text())

    def test_unknown_usage_fails_without_writing(self):
        text = SEPOLICY.replace(f"    {TYPE_HOOK}(type_name, type->value);",
                                f"    int rc = {TYPE_HOOK}(type_name, type->value);")
        self.sepolicy.write_text(text)
        with self.assertRaisesRegex(SystemExit, "no files written"):
            self.fix()
        self.assertEqual(self.sepolicy.read_text(), text)

    def test_missing_or_comment_only_policy_is_skipped(self):
        self.sepolicy.write_text(f"// {TYPE_HOOK}()\n/* {RULE_HOOK}() */\n")
        self.assertEqual(self.fix(), [])
        self.sepolicy.unlink()
        self.assertEqual(self.fix(), [])

    def test_malformed_generated_block_fails_without_writing(self):
        text = selinux.BEGIN + "\n" + SEPOLICY
        self.sepolicy.write_text(text)
        with self.assertRaisesRegex(SystemExit, "malformed.*no files written"):
            self.fix()
        self.assertEqual(self.sepolicy.read_text(), text)

    def test_symlinked_kernel_source_is_patched(self):
        kernel = self.root / "KernelSU-Next/kernel"
        (kernel / "selinux").mkdir(parents=True)
        real_policy = kernel / "selinux/sepolicy.c"
        self.sepolicy.rename(real_policy)
        self.sepolicy.parent.rmdir()
        self.sepolicy.parent.parent.rmdir()
        self.sepolicy.parent.parent.symlink_to(kernel, target_is_directory=True)
        self.assertEqual(self.fix(), [self.sepolicy])
        self.assertIn(selinux.BEGIN, real_policy.read_text())

    def test_lld_reproduces_failure_then_links_old_and_new_susfs(self):
        compiler = shutil.which("clang")
        linker = shutil.which("ld.lld")
        nm = shutil.which("llvm-nm") or shutil.which("nm")
        if not all((compiler, linker, nm)):
            self.skipTest("clang, ld.lld and nm are required")
        policy_object = self.root / "sepolicy.o"
        susfs_object = self.root / "susfs.o"
        image = self.root / "vmlinux"
        flags = [compiler, "-std=gnu11", "-ffreestanding", "-fno-stack-protector",
                 "-fno-pic", "-fno-pie", "-c"]

        def link(configured=True):
            configured_flags = flags + (["-DCONFIG_KSU_SUSFS"] if configured else [])
            subprocess.run(configured_flags + [str(self.sepolicy), "-o", str(policy_object)],
                           check=True, capture_output=True, text=True)
            subprocess.run(flags + [str(self.implementation), "-o", str(susfs_object)],
                           check=True, capture_output=True, text=True)
            return subprocess.run([linker, "-e", "policy_entry", "-o", str(image),
                                   str(policy_object), str(susfs_object)],
                                  capture_output=True, text=True)

        before = link()
        self.assertNotEqual(before.returncode, 0)
        for name in selinux.HOOKS:
            self.assertIn("undefined symbol: " + name, before.stderr)
        self.fix()
        self.assertEqual(link().returncode, 0)
        undefined = subprocess.check_output([nm, "--undefined-only", str(policy_object)], text=True)
        for name in selinux.HOOKS:
            self.assertNotIn(name, undefined)
        self.assertEqual(link(configured=False).returncode, 0)

        self.provide(TYPE_HOOK)
        self.fix()
        self.assertEqual(link().returncode, 0)
        undefined = subprocess.check_output([nm, "--undefined-only", str(policy_object)], text=True)
        self.assertIn(TYPE_HOOK, undefined)
        self.assertNotIn(RULE_HOOK, undefined)

        self.provide(*selinux.HOOKS)
        self.fix()
        self.assertEqual(link().returncode, 0)
        undefined = subprocess.check_output([nm, "--undefined-only", str(policy_object)], text=True)
        for name in selinux.HOOKS:
            self.assertIn(name, undefined)
        self.assertEqual(self.fix(), [])
        self.implementation.write_text("/* switched back to older SUSFS */\n")
        self.fix()
        self.assertEqual(link().returncode, 0)


if __name__ == "__main__":
    unittest.main()
