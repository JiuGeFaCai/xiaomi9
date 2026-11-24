"""Check SUSFS rollup patch placement, C11 compilation, and lock/VMA progress."""

import contextlib
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


if __name__ == "__main__":
    unittest.main()
