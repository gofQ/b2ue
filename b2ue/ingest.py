# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Abdulkerim Tabakoğlu
"""Load a source file into a fresh Blender scene."""

from pathlib import Path

import bpy

SUPPORTED = {".blend", ".glb", ".gltf", ".obj", ".fbx"}


def load(path):
    """Open a .blend, or import a model file into an empty scene."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in SUPPORTED:
        raise ValueError(f"Unsupported file type: {path.name}")

    if ext == ".blend":
        bpy.ops.wm.open_mainfile(filepath=str(path))
        return

    bpy.ops.wm.read_factory_settings(use_empty=True)
    if ext in {".glb", ".gltf"}:
        bpy.ops.import_scene.gltf(filepath=str(path))
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=str(path))
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=str(path))


def use_image_files(mapping_path):
    """Point images at files written by the add-on. Used for images that had
    unsaved edits in the user's Blender session, which a saved .blend copy
    does not contain."""
    import json
    mapping = json.loads(Path(mapping_path).read_text(encoding="utf-8"))
    for name, file in mapping.items():
        image = bpy.data.images.get(name)
        if image is None:
            continue
        if image.packed_file is not None:
            image.unpack(method="REMOVE")
        image.source = "FILE"
        image.filepath = file
        image.filepath_raw = file
        image.reload()
    return len(mapping)


def keep_only(scene, names):
    """Delete every object except the named ones, their parents, their
    children, and the armatures that deform them. Returns how many were removed."""
    keep = set()
    by_name = {o.name: o for o in scene.objects}
    todo = [by_name[n] for n in names if n in by_name]
    while todo:
        obj = todo.pop()
        if obj.name in keep:
            continue
        keep.add(obj.name)
        related = list(obj.children)
        if obj.parent is not None:
            related.append(obj.parent)
        related += [m.object for m in getattr(obj, "modifiers", [])
                    if m.type == "ARMATURE" and m.object is not None]
        todo += related
    removed = 0
    for obj in list(scene.objects):
        if obj.name not in keep and not obj.name.startswith("b2ue_marker_"):
            bpy.data.objects.remove(obj, do_unlink=True)
            removed += 1
    return removed


def collect_inputs(target, recursive=False):
    """Return the supported files for a file or directory argument."""
    target = Path(target)
    if target.is_file():
        return [target]
    pattern = "**/*" if recursive else "*"
    return sorted(p for p in target.glob(pattern) if p.suffix.lower() in SUPPORTED)
