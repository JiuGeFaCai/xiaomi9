# KernelSU workflow helpers

`ksu_workflow.py` is shared by `1.yml` through `4.yml`. The existing input name
`ksu_variant` is retained so callers do not need to switch to `ksu_type`.

| Option | Source | Default ref | Source directory |
| --- | --- | --- | --- |
| `SukiSU` | `SukiSU-Ultra/SukiSU-Ultra` | `main` | `KernelSU` |
| `BakaSU` | `Baka-SU/BakaSU` | `main` | `KernelSU` |
| `ksunext` | `pershoot/KernelSU-Next` | `dev-susfs` | `KernelSU-Next` |
| `ksu` | `tiann/KernelSU` | `main`, with the SUSFS compatibility handling below | `KernelSU` |
| `none` | No KSU integration | Ignored | None |

The new sources follow the reference project's
[ksu_type implementation](https://github.com/JiuGeFaCai/oppo_oplus_realme_sm8750/blob/main/.github/workflows/fastbuild_6.6.118.yml).
The existing SukiSU option continues to use its own source.

## Inputs and execution

- Leave `sukisu_version` empty to select the default ref. An explicit branch,
  tag, or commit is checked after the upstream installer runs; a failed checkout
  cannot silently build a different revision.
- `none` disables KSU and SUSFS, including stale KSU options in the defconfig.
  ZRAM preparation remains independent. The old `无` value is accepted by the
  helper for compatibility.
- `kpm_enable=builtin` requires an actual `config KPM` declaration in the selected
  source. Unsupported combinations fail before compilation. `kpn` remains an
  independent option, including for `none`.
- `ksu_version_override` handles both `KSU_VERSION := ...` and
  `$(eval KSU_VERSION=...)`. Next's version calculation follows its Kbuild's
  merge-base with the base branch. Official KSU and Next also receive matching
  fallback version values for build environments that cannot access `.git`.
- Workflow 3 retains the device suffix in `KSUVER`, including in `none` mode,
  so matrix jobs do not upload artifacts with identical names.

## Official KSU with SUSFS

Official KSU needs the selected SUSFS repository's
`kernel_patches/KernelSU/10_enable_susfs_for_ksu.patch`. Every hunk is checked with
`--fuzz=0` before applying the patch. A failed preflight does not partially patch
the source or allow the build to continue.

When `sukisu_version` is empty and the current `main` fails that preflight, the
helper checks these official revisions, newest first, in temporary directories:

- `0ff54fab4e741d958daf3048d7711644c40eca79`: matches the GKI SUSFS patch base.
- `87db8315db2c9f5286e47b963ea02b46a88a7e56`: matches the OKI SUSFS patch base.

Only a revision that passes the complete preflight is selected, and its SHA is
printed and exported as `KSU_REF`. A dirty source tree or an explicitly supplied
ref prevents this automatic switch. If no candidate matches, the workflow fails
with an explanation. Updating these candidates requires checking the actual
patches again; do not replace the failure with `|| true`.

## Local checks

Run the helper's tests without downloading kernels:

```sh
python3 -B -m unittest discover -s .github/scripts/tests -v
```

These tests cover branch selection, source directory differences, version
calculation and overrides, unsupported combinations, config cleanup, and patch
preflight behavior. They do not replace compiling the target device's kernel.
