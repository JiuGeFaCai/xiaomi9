"""Check SUSFS rollup patch placement, C11 compilation, and lock/VMA progress."""

import contextlib
import difflib
import importlib.util
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "fix_susfs_smaps_rollup_patch.py"
SPEC = importlib.util.spec_from_file_location("rollup_patch", SCRIPT)
rollup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rollup)

SOURCE = """static int show_smaps_rollup(struct vm_area_struct *vma)
{
	struct mem_size_stats mss = {0};
	unsigned long last_vma_end;

	smap_gather_stats(vma, &mss, 0);
	last_vma_end = vma->vm_end;
	if (contended) {
		unlocks++;
		locks++;
		vma = &next_vma;
		if (vma->vm_start >= last_vma_end) {
			smap_gather_stats(vma, &mss, 0);
			last_vma_end = vma->vm_end;
		} else if (vma->vm_end > last_vma_end) {
			smap_gather_stats(vma, &mss, last_vma_end);
			last_vma_end = vma->vm_end;
		}
	}
	reported_end = last_vma_end;
	return mss.total;
}
"""

PATCH = """diff --git a/fs/proc/task_mmu.c b/fs/proc/task_mmu.c
--- a/fs/proc/task_mmu.c
+++ b/fs/proc/task_mmu.c
@@ -1618,6 +1667,11 @@ static int show_smaps_rollup(struct seq_file *m, void *v)
 			goto out_unlock;
 		}
 
+#ifdef CONFIG_KSU_SUSFS_SUS_MAP
+		if (vma->vm_file && SUSFS_IS_INODE_SUS_MAP(file_inode(vma->vm_file)))
+			goto bypass_orig_flow;
+#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MAP
+
 		if (vma->vm_start < last_vma_end) {
 			/*
 			 * After retaking the lock, already reported VMA grew
@@ -1630,6 +1684,9 @@ static int show_smaps_rollup(struct seq_file *m, void *v)
 			/* Found next unreported VMA, start from its beginning */
 			smap_gather_stats(priv, vma, &mss);
 		}
+#ifdef CONFIG_KSU_SUSFS_SUS_MAP
+bypass_orig_flow:
+#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MAP
 		last_vma_end = vma->vm_end;
 
 		/*
"""

PREAMBLE = """#include <stdlib.h>
struct file { int hidden, mapping; };
struct vm_area_struct { struct file *vm_file; unsigned long vm_start, vm_end; };
struct mem_size_stats { int total; };
static struct vm_area_struct next_vma;
static int contended, locks, unlocks;
static int umounted_app;
static unsigned long reported_end;
#define file_inode(file) (file)
#define SUSFS_IS_INODE_SUS_MAP(inode) \\
    inode && inode->mapping && inode->hidden && umounted_app
static void smap_gather_stats(struct vm_area_struct *vma,
                              struct mem_size_stats *mss, unsigned long start)
{
    mss->total += vma->vm_end - (start ? start : vma->vm_start);
}
"""

MAIN = """int main(void)
{
    for (int hidden = 0; hidden < 2; hidden++)
    for (int mapped = 0; mapped < 2; mapped++)
    for (int app = 0; app < 2; app++)
    for (int backed = 0; backed < 2; backed++) {
        struct file file = {hidden, mapped};
        struct vm_area_struct first = {backed ? &file : NULL, 0, 10};
        umounted_app = app;
        for (int scenario = 0; scenario < 3; scenario++) {
            locks = unlocks = 0;
            contended = scenario != 0;
            next_vma = (struct vm_area_struct){&file, scenario == 2 ? 0 : 10, 20};
            int result = show_smaps_rollup(&first);
#ifdef CONFIG_KSU_SUSFS_SUS_MAP
            int first_hidden = backed && mapped && hidden && app;
            int next_hidden = mapped && hidden && app;
            int expected = (first_hidden ? 0 : 10) + (contended && !next_hidden ? 10 : 0);
#else
            int expected = contended ? 20 : 10;
#endif
            if (result != expected || reported_end != (contended ? 20UL : 10UL) ||
                locks != contended || unlocks != contended)
                abort();
        }
    }
    return 0;
}
"""

SMAP_SOURCE = """static int show_smap(void *v)
{
\tstruct vm_area_struct *vma = v;
\tstruct mem_size_stats mss = {};

\tif (!vma_pages(vma))
\t\tgoto show_pad;

\tsmap_gather_stats(vma, &mss, 0);
\treturn mss.total;
show_pad:
\treturn 0;
}

"""

OPEN_SOURCE = """
static int smaps_rollup_open(void)
{
\tint ret;
\tstruct mem_size_stats mss = {};

\tret = mss.total;
\treturn ret;
}

"""

PAGEMAP_SOURCE = """static ssize_t pagemap_read(struct file *file, char __user *buf,
\t\tsize_t count, loff_t *ppos)
{
\twhile (count && (start_vaddr < end_vaddr)) {
\t\tint len;
\t\tunsigned long end;

\t\tpm.pos = 0;
\t\tend = (start_vaddr + PAGEMAP_WALK_SIZE) & PAGEMAP_WALK_MASK;

\t\tret = mmap_read_lock_killable(mm);
\t\tif (ret)
\t\t\tgoto out_free;
\t\tret = walk_page_range(mm, start_vaddr, end, &pagemap_ops, &pm);
\t\tmmap_read_unlock(mm);
\t\tstart_vaddr = end;

\t}
}
"""

# Real neighboring SUSFS hunks retain their old line numbers. Rebasing only
# the rollup hunks placed these out of order and allowed -F3 to hit open().
SMAP_HUNK = """@@ -1549,6 +1591,13 @@ static int show_smap(struct seq_file *m, void *v)
 \tstruct vm_area_struct *vma = v;
 \tstruct mem_size_stats mss = {};

+#ifdef CONFIG_KSU_SUSFS_SUS_MAP
+\tif (vma->vm_file) {
+\t\tif (SUSFS_IS_INODE_SUS_MAP(file_inode(vma->vm_file)))
+\t\t\treturn 0;
+\t}
+#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MAP
+
 \tif (!vma_data_pages(vma))
 \t\tgoto show_pad;

""".replace("\n\n", "\n \n")

PAGEMAP_HUNKS = """@@ -2449,6 +2506,9 @@ static ssize_t pagemap_read(struct file *file, char __user *buf,
 \twhile (count && (start_vaddr < end_vaddr)) {
 \t\tint len;
 \t\tunsigned long end;
+#ifdef CONFIG_KSU_SUSFS_SUS_MAP
+\t\tstruct vm_area_struct *vma;
+#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MAP

 \t\tpm.pos = 0;
 \t\tend = (start_vaddr + PAGEMAP_WALK_SIZE) & PAGEMAP_WALK_MASK;
@@ -2458,7 +2518,15 @@ static ssize_t pagemap_read(struct file *file, char __user *buf,
 \t\tret = mmap_read_lock_killable(mm);
 \t\tif (ret)
 \t\t\tgoto out_free;
+#ifdef CONFIG_KSU_SUSFS_SUS_MAP
+\t\tvma = vma_lookup(mm, start_vaddr);
+\t\tif (vma && vma->vm_file && SUSFS_IS_INODE_SUS_MAP(file_inode(vma->vm_file)))
+\t\t\tgoto bypass_orig_flow;
+#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MAP
 \t\tret = walk_page_range(mm, start_vaddr, end, &pagemap_ops, &pm);
+#ifdef CONFIG_KSU_SUSFS_SUS_MAP
+bypass_orig_flow:
+#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MAP
 \t\tmmap_read_unlock(mm);
 \t\tstart_vaddr = end;

""".replace("\n\n", "\n \n")

OTHER_PATCH = """diff --git a/other.c b/other.c
--- a/other.c
+++ b/other.c
@@ -1 +1 @@
-old
+new
"""


class RollupPatchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "fs/proc/task_mmu.c"
        self.source.parent.mkdir(parents=True)
        self.source.write_text(SOURCE)
        self.patch = self.root / "susfs.patch"
        self.patch.write_text(PATCH)

    def adapt(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return rollup.adapt_patch(self.root, self.patch)

    def prepare(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            rollup.prepare_and_apply(self.root, self.patch)
        return output.getvalue()

    def upstream_patch(self, before, after):
        return rollup.SECTION + "".join(difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True),
            fromfile="a/fs/proc/task_mmu.c", tofile="b/fs/proc/task_mmu.c",
        ))

    def test_exact_patch_application_and_c11_behavior_with_susfs_on_and_off(self):
        compiler = shutil.which("clang")
        if not compiler or not shutil.which("patch"):
            self.skipTest("clang and patch are required")
        self.assertTrue(self.adapt())
        self.assertEqual(self.source.read_text(), SOURCE)
        adapted = self.patch.read_bytes()
        self.assertFalse(self.adapt())
        self.assertEqual(self.patch.read_bytes(), adapted)
        result = subprocess.run(["patch", "--batch", "-p1", "-F", "0", "-i", str(self.patch)],
                                cwd=self.root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        fixed = self.source.read_text()
        self.assertNotIn("bypass_orig_flow", fixed)
        # The former guard must reproduce the reported Clang error with the
        # actual unparenthesized SUSFS macro shape before the fixed one passes.
        self.source.write_text((PREAMBLE + fixed + MAIN).replace(
            "!(SUSFS_IS_INODE_SUS_MAP(file_inode(vma->vm_file)))",
            "!SUSFS_IS_INODE_SUS_MAP(file_inode(vma->vm_file))",
        ))
        result = subprocess.run(
            [compiler, "-std=gnu11", "-Werror", "-Wlogical-op-parentheses", "-fsyntax-only",
             "-DCONFIG_KSU_SUSFS_SUS_MAP", str(self.source)],
            text=True, capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("-Wlogical-op-parentheses", result.stderr)
        # Also cover upstream versions that already parenthesize the macro.
        for parenthesized in (False, True):
            preamble = PREAMBLE
            if parenthesized:
                preamble = preamble.replace(
                    "inode && inode->mapping && inode->hidden && umounted_app",
                    "(inode && inode->mapping && inode->hidden && umounted_app)",
                )
            self.source.write_text(preamble + fixed + MAIN)
            for enabled in (False, True):
                command = [compiler, "-std=gnu11", "-Werror", "-Wc23-extensions",
                           "-Wlogical-op-parentheses", str(self.source),
                           "-o", str(self.root / "check")]
                if enabled:
                    command.append("-DCONFIG_KSU_SUSFS_SUS_MAP")
                result = subprocess.run(command, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                result = subprocess.run([str(self.root / "check")], capture_output=True)
                self.assertEqual(result.returncode, 0)

    def test_legacy_walker_is_untouched(self):
        self.source.write_text(SOURCE.replace("smap_gather_stats(vma, &mss, 0)",
                                              "smap_gather_stats(priv, vma, &mss)")
                                      .replace("smap_gather_stats(vma, &mss, last_vma_end)",
                                               "smap_gather_stats(priv, vma, &mss)"))
        self.assertFalse(self.adapt())
        self.assertEqual(self.patch.read_text(), PATCH)

    def test_unknown_patch_or_walker_fails_before_writing(self):
        for source, patch in ((SOURCE.replace("smap_gather_stats(vma, &mss, last_vma_end);", ""), PATCH),
                              (SOURCE, PATCH.replace("goto bypass_orig_flow;", "goto new_flow;"))):
            with self.subTest(source=source, patch=patch):
                self.source.write_text(source)
                self.patch.write_text(patch)
                with self.assertRaises(SystemExit):
                    self.adapt()
                self.assertEqual(self.source.read_text(), source)
                self.assertEqual(self.patch.read_text(), patch)

    def test_neighboring_hunks_rebased_and_applied_with_no_misplaced_vma(self):
        if not shutil.which("clang") or not shutil.which("patch"):
            self.skipTest("clang and patch are required")
        original = SMAP_SOURCE + SOURCE + OPEN_SOURCE + PAGEMAP_SOURCE
        self.source.write_text(original)
        header, hunks = PATCH.split("@@ ", 1)
        upstream = header + SMAP_HUNK + "@@ " + hunks + PAGEMAP_HUNKS
        self.patch.write_text(OTHER_PATCH + upstream)
        self.prepare()
        fixed = self.source.read_text()
        self.assertEqual(self.patch.read_text(), OTHER_PATCH)
        self.assertIn(OPEN_SOURCE, fixed)
        self.assertIn("vma = vma_lookup(mm, start_vaddr);", fixed)
        self.assertIn("struct vm_area_struct *vma;", fixed)
        self.assertFalse(list(self.root.rglob("*.rej")))
        with contextlib.redirect_stdout(io.StringIO()):
            rollup.verify_hooks(self.root)
        # Compile all adjacent smaps functions together, including open(),
        # where the failed workflow had inserted an undeclared VMA reference.
        functions = fixed[:fixed.index("static ssize_t pagemap_read")]
        preamble = PREAMBLE + "#define vma_pages(vma) ((vma)->vm_end - (vma)->vm_start)\n"
        main = MAIN.replace("    return 0;\n}", """    if (smaps_rollup_open()) abort();
    struct file f = {1, 1};
    struct vm_area_struct vma = {&f, 0, 10};
    umounted_app = 1;
#ifdef CONFIG_KSU_SUSFS_SUS_MAP
    if (show_smap(&vma)) abort();
#else
    if (show_smap(&vma) != 10) abort();
#endif
    f.hidden = 0;
    if (show_smap(&vma) != 10) abort();
    return 0;
}""")
        check = self.root / "check.c"
        check.write_text(preamble + functions + main)
        for enabled in (False, True):
            cmd = ["clang", "-std=gnu11", "-Werror", "-Wlogical-op-parentheses", str(check),
                   "-o", str(self.root / "check")]
            if enabled:
                cmd.append("-DCONFIG_KSU_SUSFS_SUS_MAP")
            result = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(subprocess.run([str(self.root / "check")]).returncode, 0)

    def test_updated_author_patch_is_used_verbatim_and_not_applied_twice(self):
        updated = SOURCE.replace(
            "\tsmap_gather_stats(vma, &mss, 0);",
            "#if defined(CONFIG_KSU_SUSFS_SUS_MAP)\n"
            "\tif (!vma->vm_file || !(SUSFS_IS_INODE_SUS_MAP(file_inode(vma->vm_file))))\n"
            "#endif\n\t\tsmap_gather_stats(vma, &mss, 0);",
        ).replace(
            "\t\t\tsmap_gather_stats(vma, &mss, last_vma_end);",
            "#if defined(CONFIG_KSU_SUSFS_SUS_MAP)\n"
            "\t\t\tif (!vma->vm_file || !(SUSFS_IS_INODE_SUS_MAP(file_inode(vma->vm_file))))\n"
            "#endif\n\t\t\t\tsmap_gather_stats(vma, &mss, last_vma_end);",
        )
        author_patch = self.upstream_patch(SOURCE, updated)
        self.patch.write_text(author_patch + OTHER_PATCH)
        self.assertIn("without adaptation", self.prepare())
        self.assertEqual(self.source.read_text(), updated)
        self.assertEqual(self.patch.read_text(), OTHER_PATCH)
        self.patch.write_text(author_patch + OTHER_PATCH)
        self.assertIn("skipping duplicate", self.prepare())
        self.assertEqual(self.source.read_text(), updated)
        self.assertEqual(self.patch.read_text(), OTHER_PATCH)

    def test_updated_author_can_filter_in_helper_instead_of_call_sites(self):
        helper = """static void smap_gather_stats(struct vm_area_struct *vma,
\t\tstruct mem_size_stats *mss, unsigned long start)
{
\tmss->total += vma->vm_end - start;
}

""".replace("\n\n", "\n \n")
        original = helper + SOURCE
        updated = original.replace(
            "\tmss->total +=",
            "#ifdef CONFIG_KSU_SUSFS_SUS_MAP\n"
            "\tif (vma->vm_file && SUSFS_IS_INODE_SUS_MAP(file_inode(vma->vm_file)))\n"
            "\t\treturn;\n#endif\n\tmss->total +=",
        )
        self.source.write_text(original)
        self.patch.write_text(self.upstream_patch(original, updated))
        self.assertIn("without adaptation", self.prepare())
        self.assertEqual(self.source.read_text(), updated)

    def test_unrecognized_author_update_stops_before_modifications(self):
        for updated in (
            SOURCE.replace("reported_end = last_vma_end;", "reported_end = 0;"),
            SOURCE.replace("smap_gather_stats(vma, &mss, 0);", "unsafe_gather_stats(vma, &mss, 0);"),
            SOURCE.replace("smap_gather_stats", "new_stats_walker"),
        ):
            with self.subTest(updated=updated):
                self.source.write_text(SOURCE)
                patch = self.upstream_patch(SOURCE, updated)
                self.patch.write_text(patch)
                with self.assertRaises(SystemExit):
                    self.prepare()
                self.assertEqual(self.source.read_text(), SOURCE)
                self.assertEqual(self.patch.read_text(), patch)

    def test_incompatible_patch_cannot_duplicate_existing_hooks(self):
        self.assertTrue(self.adapt())
        fixed = rollup.preview_section(SOURCE, self.patch.read_text())
        self.source.write_text(fixed)
        self.patch.write_text(PATCH)
        with self.assertRaisesRegex(SystemExit, "duplicate"):
            self.prepare()
        self.assertEqual(self.source.read_text(), fixed)
        self.assertEqual(self.patch.read_text(), PATCH)

    def test_failed_exact_preflight_does_not_modify_source_or_remaining_patch(self):
        self.assertTrue(self.adapt())
        rebased = self.patch.read_text()
        changed = SOURCE.replace("reported_end = last_vma_end;", "reported_end = 999;")
        self.source.write_text(changed)
        with self.assertRaises(SystemExit):
            rollup.apply_rebased_patch(self.root, self.patch)
        self.assertEqual(self.source.read_text(), changed)
        self.assertEqual(self.patch.read_text(), rebased)

    def test_rejects_and_vma_leak_are_detected_before_compilation(self):
        self.source.write_text(SOURCE + OPEN_SOURCE.replace("\tint ret;", "\tif (vma->vm_file) return 0;\n\tint ret;"))
        with self.assertRaisesRegex(SystemExit, "leaked"):
            rollup.verify_hooks(self.root)
        self.source.with_suffix(".c.rej").write_text("rejected SUSFS hunk")
        with self.assertRaisesRegex(SystemExit, "rejected hunks"):
            rollup.verify_hooks(self.root)


if __name__ == "__main__":
    unittest.main()
