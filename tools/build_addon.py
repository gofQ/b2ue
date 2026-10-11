# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Abdulkerim Tabakoğlu
"""Build the installable add-on zip: dist/b2ue-<version>.zip

    python tools/build_addon.py

Same result as Blender's own builder, which you can also use:

    blender --command extension build --source-dir b2ue --output-dir dist
"""

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


if __name__ == "__main__":
    print(build(*sys.argv[1:]))
