# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Abdulkerim Tabakoğlu
"""Build the installable add-on zip: dist/b2ue-<version>.zip

    python tools/build_addon.py

The zip is checked with Blender's own extension validator (the same rules
as extensions.blender.org) when the bpy module is available.

Same result as Blender's own builder, which you can also use:

    blender --command extension build --source-dir b2ue --output-dir dist
"""

import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "b2ue"
SKIP_DIRS = {"__pycache__", ".git"}


def build(out_dir=ROOT / "dist"):
    manifest = tomllib.loads((SOURCE / "blender_manifest.toml").read_text(encoding="utf-8"))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{manifest['id']}-{manifest['version']}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(SOURCE.rglob("*")):
            if path.is_dir() or SKIP_DIRS & set(path.parts) or path.suffix == ".pyc":
                continue
            zf.write(path, path.relative_to(SOURCE).as_posix())
    return target


def validator():
    """Path of Blender's extension validator (blender_ext.py), or None without bpy."""
    try:
        import bpy
    except ImportError:
        return None
    path = Path(bpy.utils.system_resource("SCRIPTS")) / "addons_core" / "bl_pkg" / "cli" / "blender_ext.py"
    return path if path.exists() else None


def validate(target):
    """Run Blender's validator on the zip. Returns (ok, message); ok is None when it cannot run."""
    tool = validator()
    if tool is None:
        return None, "bpy not found, skipped Blender's extension validator"
    run = subprocess.run([sys.executable, str(tool), "validate", str(target)],
                         capture_output=True, text=True)
    message = (run.stdout + run.stderr).strip()
    return run.returncode == 0 and "ERROR" not in message, message


if __name__ == "__main__":
    target = build(*sys.argv[1:])
    print(target)
    ok, message = validate(target)
    if ok is False:
        print(message)
        sys.exit(1)
    print(message if ok is None else "valid for extensions.blender.org")
