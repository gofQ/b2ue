# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Abdulkerim Tabakoğlu
"""Group the scene into assets and export one Unreal-ready FBX per asset."""

from dataclasses import dataclass, field
from pathlib import Path

import bpy
from mathutils import Vector

from .cleanup import deforming_armature, triangle_count
from .textures import export_textures

PREFIXES = {"static": "SM_", "skeletal": "SK_"}

# Settings that import into Unreal at the right size, facing and bind pose.
UNREAL_FBX_SETTINGS = dict(
    apply_unit_scale=True,
    apply_scale_options="FBX_SCALE_ALL",
    axis_forward="-Z",
    axis_up="Y",
    use_mesh_modifiers=True,
    mesh_smooth_type="FACE",
    use_tspace=True,
    add_leaf_bones=False,
    primary_bone_axis="Y",
    secondary_bone_axis="X",
    armature_nodetype="NULL",
)


def safe_name(text):
    """Keep letters, digits and underscores, which Unreal asset names accept."""
    cleaned = "".join(c if c.isalnum() else "_" for c in text)
    return "_".join(part for part in cleaned.split("_") if part) or "Asset"


@dataclass
class Asset:
    kind: str  # "static" or "skeletal"
    root: object
    objects: list = field(default_factory=list)
    label: str = ""  # overrides the file name, e.g. the source file's name

    @property
    def name(self):
        return self.root.name

    @property
    def file_name(self):
        prefix = PREFIXES[self.kind]
        base = safe_name(self.label or self.name)
        for p in PREFIXES.values():
            if base.startswith(p):
                base = base[len(p):]
        return f"{prefix}{base}.fbx"

    @property
    def triangles(self):
        return sum(triangle_count(o.data) for o in self.objects if o.type == "MESH")


def make_names_unique(assets):
    """Different object names can clean up to the same file name ("Door.A" and
    "Door A" both become "Door_A"). Give every clashing asset a short suffix
    taken from its original name, so the result is the same on every run."""
    import hashlib
    groups = {}
    for asset in assets:
        groups.setdefault(asset.file_name.lower(), []).append(asset)
    for clashing in groups.values():
        if len(clashing) < 2:
            continue
        for asset in clashing:
            original = asset.label or asset.name
            digest = hashlib.sha1(original.encode("utf-8")).hexdigest()[:6]
            asset.label = f"{safe_name(original)}_{digest}"


def _mesh_descendants(obj):
    out = []
    for child in obj.children:
        if child.type == "MESH":
            out.append(child)
            out.extend(_mesh_descendants(child))
    return out


def collect_assets(scene):
    """Armature + the meshes it deforms -> one skeletal asset.
    Every other top-level mesh (with its mesh children) -> one static asset."""
    assets, used = [], set()

    for arm in (o for o in scene.objects if o.type == "ARMATURE"):
        meshes = [o for o in scene.objects
                  if o.type == "MESH" and deforming_armature(o) is arm]
        assets.append(Asset("skeletal", arm, [arm] + meshes))
        used.update(o.name for o in meshes)

    for obj in scene.objects:
        if obj.type != "MESH" or obj.name in used:
            continue
        if obj.parent is not None and obj.parent.type == "MESH":
            continue
        group = [obj] + [m for m in _mesh_descendants(obj) if m.name not in used]
        assets.append(Asset("static", obj, group))
        used.update(o.name for o in group)
    return assets


def _pivot(asset, origin):
    """World-space point that should end up at the origin in Unreal."""
    if origin == "object":
        return asset.root.matrix_world.translation.copy()
    pts = [o.matrix_world @ Vector(c) for o in asset.objects if o.type == "MESH"
           for c in o.bound_box]
    lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    centre = (lo + hi) / 2
    if origin == "bottom":
        centre.z = lo.z
    return centre


def export_asset(asset, out_dir, origin="bottom", bake_animation=True,
                 embed_textures=False, texture_size=0, matte=0.0):
    """Export a single asset.

    origin (static meshes only) picks the pivot Unreal will use:
      bottom - centre of the bottom of the bounding box (stands on the floor)
      center - centre of the bounding box
      object - the object's own origin
      keep   - leave the asset where it is in the scene
    Skeletal assets keep their armature where it is.

    Textures are written next to the FBX as T_<Name>_<Role> files, or embedded
    in the FBX with embed_textures=True. Returns (fbx path, textures, missing).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / asset.file_name
    base_name = Path(asset.file_name).stem[len(PREFIXES[asset.kind]):]
    textures, missing = export_textures(asset.objects, out_dir, base_name, texture_size, matte)

    view_layer = bpy.context.view_layer
    view_layer.update()
    for obj in bpy.context.scene.objects:
        obj.select_set(False)
    for obj in asset.objects:
        obj.hide_set(False)
        obj.select_set(True)
    view_layer.objects.active = asset.root

    original = asset.root.matrix_world.copy()
    if asset.kind == "static" and origin != "keep":
        moved = original.copy()
        moved.translation = original.translation - _pivot(asset, origin)
        asset.root.matrix_world = moved
    try:
        bpy.ops.export_scene.fbx(
            filepath=str(path),
            use_selection=True,
            object_types={"MESH", "ARMATURE"},
            bake_anim=bake_animation and asset.kind == "skeletal",
            path_mode="COPY" if embed_textures else "RELATIVE",
            embed_textures=embed_textures,
            **UNREAL_FBX_SETTINGS,
        )
    finally:
        asset.root.matrix_world = original
    return path, textures, missing
