# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Abdulkerim Tabakoğlu
"""b2ue: a small, scriptable Blender -> Unreal Engine 5 asset pipeline.

Load a .blend or a raw AI-generated model (GLB/glTF/OBJ/FBX), clean it up,
check it against common Unreal import problems, and export one
Unreal-ready FBX per asset.

Use it from the command line (cli.py) or as a Blender add-on (addon.py).
"""

__version__ = "0.4.6"


# Blender add-on entry points. The UI lives in addon.py and is only imported
# when Blender loads the add-on, so the command line tool stays lightweight.

def register():
    from . import addon
    addon.register()


def unregister():
    from . import addon
    addon.unregister()
