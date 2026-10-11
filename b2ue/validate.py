# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Abdulkerim Tabakoğlu
"""Checks for problems that commonly break or degrade an Unreal import."""

from dataclasses import asdict, dataclass

from .textures import image_loads
from .cleanup import (EPS, deforming_armature, has_armature_modifier,
                      is_identity_rot_scale, triangle_count)

ERROR, WARNING, INFO = "error", "warning", "info"


@dataclass
class Issue:
    level: str
    object: str
    code: str
    message: str

    def to_dict(self):
        return asdict(self)


def validate_mesh(obj, max_tris):
    issues = []
    me = obj.data
    add = lambda level, code, msg: issues.append(Issue(level, obj.name, code, msg))

    scale = obj.matrix_basis.to_scale()
    if any(s < 0 for s in scale):
        add(ERROR, "NEGATIVE_SCALE", "Negative scale flips normals in Unreal; apply it.")
    elif not is_identity_rot_scale(obj):
        add(WARNING, "UNAPPLIED_TRANSFORM",
            "Rotation/scale is not applied (skinned or shared mesh, left as is).")

    if not me.polygons:
        add(ERROR, "NO_FACES", "Mesh has no faces.")
        return issues

    if not me.uv_layers:
        add(ERROR, "NO_UV", "No UV map; textures and lightmaps will not work.")

    ngons = sum(1 for p in me.polygons if len(p.vertices) > 4)
    if ngons:
        add(WARNING, "NGONS",
            f"{ngons} n-gon(s); Unreal triangulates them and shading may break.")

    tris = triangle_count(me)
    if max_tris and tris > max_tris:
        add(WARNING, "TRI_BUDGET", f"{tris} triangles, budget is {max_tris}.")

    for slot in obj.material_slots:
        mat = slot.material
        if mat is None or not mat.use_nodes:
            continue
        for node in mat.node_tree.nodes:
            if node.type == "TEX_IMAGE" and node.image is not None \
                    and not image_loads(node.image):
                add(WARNING, "MISSING_TEXTURE",
                    f"Texture '{node.image.name}' in material '{mat.name}' cannot be loaded.")

    if not obj.material_slots:
        add(WARNING, "NO_MATERIAL", "No material slot; Unreal will use a default material.")
    elif any(slot.material is None for slot in obj.material_slots):
        add(WARNING, "EMPTY_MATERIAL_SLOT", "One or more material slots are empty.")

    if has_armature_modifier(obj):
        if not obj.vertex_groups:
            add(ERROR, "SKINNED_NO_WEIGHTS",
                "Has an Armature modifier but no vertex groups.")
        arm = deforming_armature(obj)
        if arm is not None and obj.parent is not arm:
            add(WARNING, "SKINNED_NOT_PARENTED",
                f"Deformed by '{arm.name}' but not parented to it.")
    return issues


def validate_armature(obj):
    issues = []
    add = lambda level, code, msg: issues.append(Issue(level, obj.name, code, msg))
    bones = obj.data.bones

    if not all(abs(s - 1.0) < EPS for s in obj.matrix_basis.to_scale()):
        add(WARNING, "ARMATURE_SCALE",
            "Armature object scale is not 1; apply it before rigging/export.")

    roots = [b.name for b in bones if b.parent is None]
    if len(roots) > 1:
        add(ERROR, "MULTIPLE_ROOT_BONES",
            f"Unreal needs a single root bone, found {len(roots)}: {', '.join(roots[:5])}")
    if not bones:
        add(ERROR, "NO_BONES", "Armature has no bones.")
    else:
        add(INFO, "BONE_COUNT", f"{len(bones)} bone(s).")
    return issues


def validate_scene(scene, max_tris=0):
    issues = []
    for obj in scene.objects:
        if obj.type == "MESH":
            issues.extend(validate_mesh(obj, max_tris))
        elif obj.type == "ARMATURE":
            issues.extend(validate_armature(obj))
    return issues
