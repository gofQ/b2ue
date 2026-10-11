# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Abdulkerim Tabakoğlu
"""Mesh and hierarchy cleanup that makes assets safe to send to Unreal."""

import bmesh
import numpy as np
import bpy
from mathutils import Matrix

EPS = 1e-6


def has_armature_modifier(obj):
    return any(m.type == "ARMATURE" for m in obj.modifiers)


def deforming_armature(obj):
    """The armature object that deforms this mesh, if any."""
    for m in obj.modifiers:
        if m.type == "ARMATURE" and m.object is not None:
            return m.object
    if obj.parent is not None and obj.parent.type == "ARMATURE":
        return obj.parent
    return None


def is_identity_rot_scale(obj):
    _, rot, scale = obj.matrix_basis.decompose()
    rot_ok = abs(abs(rot.w) - 1.0) < EPS
    scale_ok = all(abs(s - 1.0) < EPS for s in scale)
    return rot_ok and scale_ok


def flatten_hierarchy(scene):
    """Unparent static meshes from empties and other helpers, keeping their
    world transform, then delete empties left with nothing under them.

    Imported GLB/glTF files (Tripo, Meshy and similar) usually wrap meshes in
    empties that carry the axis conversion. Unreal does not need them.
    Meshes that belong to an armature are left alone.
    """
    moved = 0
    for obj in list(scene.objects):
        if obj.type != "MESH" or obj.parent is None:
            continue
        if deforming_armature(obj) is not None:
            continue
        if obj.parent.type == "MESH":
            continue
        world = obj.matrix_world.copy()
        obj.parent = None
        obj.matrix_world = world
        moved += 1

    removed = 0
    changed = True
    while changed:
        changed = False
        for obj in list(scene.objects):
            if obj.type == "EMPTY" and not obj.children \
                    and not obj.name.startswith("b2ue_marker_"):  # rig markers stay
                bpy.data.objects.remove(obj, do_unlink=True)
                removed += 1
                changed = True
    bpy.context.view_layer.update()
    return {"unparented": moved, "empties_removed": removed}


def apply_rotation_scale(obj):
    """Bake rotation and scale into the mesh data (location is kept).

    Skipped for skinned meshes, meshes with children and shared mesh data,
    where baking would move other things. Those are reported by validation.
    """
    if obj.type != "MESH" or is_identity_rot_scale(obj):
        return False
    if has_armature_modifier(obj) or obj.children or obj.data.users > 1:
        return False
    loc, rot, scale = obj.matrix_basis.decompose()
    obj.data.transform(rot.to_matrix().to_4x4() @ Matrix.Diagonal(scale.to_4d()), shape_keys=True)
    obj.matrix_basis = Matrix.Translation(loc)
    obj.data.update()
    # matrix_world is only recomputed on a scene update; without this, code
    # that reads it next would apply the rotation/scale a second time
    bpy.context.view_layer.update()
    return True


NORMAL_ATTR = "b2ue_normal"


def save_normals(obj):
    """Keep the mesh's custom (split) normals in a plain corner attribute.

    AI tools often ship noisy geometry with smooth custom normals that make it
    look clean. bmesh edits and decimation lose or garble those normals; a
    float corner attribute survives both (decimation interpolates it), so the
    normals can be put back afterwards with restore_normals().
    Returns True if there was something to keep.
    """
    me = obj.data
    if not me.has_custom_normals:
        return False
    n = np.empty(len(me.loops) * 3, dtype=np.float32)
    me.corner_normals.foreach_get("vector", n)
    if NORMAL_ATTR in me.attributes:
        me.attributes.remove(me.attributes[NORMAL_ATTR])
    me.attributes.new(NORMAL_ATTR, "FLOAT_VECTOR", "CORNER").data.foreach_set("vector", n)
    return True


def restore_normals(obj, weld=False):
    """Apply the normals kept by save_normals() as custom normals again.
    weld: after decimation, make each vertex's corners agree again."""
    me = obj.data
    attr = me.attributes.get(NORMAL_ATTR)
    if attr is None:
        return False
    n = np.empty(len(me.loops) * 3, dtype=np.float32)
    attr.data.foreach_get("vector", n)
    me.attributes.remove(attr)
    n = n.reshape(-1, 3)
    # faces flipped by the cleanup must get their normals flipped too
    face_n = np.empty(len(me.polygons) * 3, dtype=np.float32)
    me.polygons.foreach_get("normal", face_n)
    totals = np.empty(len(me.polygons), dtype=np.int64)
    me.polygons.foreach_get("loop_total", totals)
    per_loop = np.repeat(face_n.reshape(-1, 3), totals, axis=0)
    flip = np.einsum("ij,ij->i", n, per_loop) < 0
    n[flip] *= -1
    length = np.linalg.norm(n, axis=1)
    bad = length < 1e-6
    n[bad] = per_loop[bad]
    n[~bad] /= length[~bad, None]
    if weld:
        n = _weld_corner_normals(me, n)
    me.normals_split_custom_set(n)  # numpy array: no huge Python list
    return True


# Corners of one vertex within this angle of their average are averaged;
# the rest keep their own normal. Chosen by rendering a character face and a
# deeply carved prop at 0, 20, 35 and 60 degrees: 20 removes the faceted
# patches on skin, while the carving's render difference stays within 0.1% of
# not averaging at all (60 made it 1.2% worse, with oily-looking streaks).
WELD_ANGLE = 20.0


def _weld_corner_normals(me, n):
    """Average the corner normals of each vertex.

    Decimation collapses many vertices into one, and each face around the
    survivor keeps the corner normal of a different original vertex. Corners
    that should agree then differ by 10-30 degrees and the surface looks
    faceted (measured: median 12.9 deg on a 50k character). Corners of a vertex
    that are within WELD_ANGLE of their average are set to that average; the
    rest keep their own normal, so creases and hard edges stay sharp."""
    lv = np.empty(len(me.loops), dtype=np.int64)
    me.loops.foreach_get("vertex_index", lv)
    sums = np.zeros((len(me.vertices), 3))
    np.add.at(sums, lv, n)
    mean = sums / np.maximum(np.linalg.norm(sums, axis=1), 1e-12)[:, None]
    per_corner = mean[lv]
    close = np.einsum("ij,ij->i", n, per_corner) > np.cos(np.radians(WELD_ANGLE))
    out = n.copy()
    out[close] = per_corner[close]
    return out.astype(np.float32)


def clean_mesh(obj, merge_distance=1e-5, triangulate=False):
    """Merge duplicate vertices, drop loose geometry, fix normal direction."""
    me = obj.data
    had_normals = save_normals(obj)
    bm = bmesh.new()
    bm.from_mesh(me)
    verts_before = len(bm.verts)

    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=merge_distance)
    merged = verts_before - len(bm.verts)

    loose_edges = [e for e in bm.edges if not e.link_faces]
    if loose_edges:
        bmesh.ops.delete(bm, geom=loose_edges, context="EDGES")
    loose_verts = [v for v in bm.verts if not v.link_edges]
    if loose_verts:
        bmesh.ops.delete(bm, geom=loose_verts, context="VERTS")

    if bm.faces:
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    if triangulate and bm.faces:
        bmesh.ops.triangulate(bm, faces=bm.faces)

    bm.to_mesh(me)
    me.update()
    bm.free()
    if had_normals:
        restore_normals(obj)
    return {
        "merged_verts": merged,
        "loose_edges_removed": len(loose_edges),
        "loose_verts_removed": len(loose_verts),
    }


def triangle_count(mesh):
    return sum(len(p.vertices) - 2 for p in mesh.polygons)


def _apply_decimate(obj, ratio, mask_group=None):
    """Apply a collapse Decimate modifier as the first modifier.

    With mask_group, only vertices in that group (weight 1) are collapsed and
    the rest of the mesh is left exactly as it is.
    """
    had_normals = save_normals(obj)
    mod = obj.modifiers.new("b2ue_decimate", "DECIMATE")
    mod.decimate_type = "COLLAPSE"
    mod.ratio = min(max(ratio, 0.0001), 1.0)
    mod.use_collapse_triangulate = True
    if mask_group:
        mod.vertex_group = mask_group
    with bpy.context.temp_override(object=obj, active_object=obj,
                                   selected_objects=[obj], selected_editable_objects=[obj]):
        bpy.ops.object.modifier_move_to_index(modifier=mod.name, index=0)
        bpy.ops.object.modifier_apply(modifier=mod.name)
    if had_normals:
        restore_normals(obj, weld=True)


def _region_stats(obj, group_name):
    """Triangles and surface area inside / outside a vertex group."""
    group = obj.vertex_groups[group_name].index
    inside = set()
    for v in obj.data.vertices:
        for g in v.groups:
            if g.group == group and g.weight >= 0.5:
                inside.add(v.index)
    tris_in = tris_out = 0
    area_in = area_out = 0.0
    for poly in obj.data.polygons:
        tris = len(poly.vertices) - 2
        if sum(1 for i in poly.vertices if i in inside) * 2 > len(poly.vertices):
            tris_in += tris
            area_in += poly.area
        else:
            tris_out += tris
            area_out += poly.area
    return inside, tris_in, tris_out, area_in, area_out


class HasShapeKeys(Exception):
    """Blender cannot decimate a mesh that has shape keys (morph targets):
    the collapsed topology no longer matches the stored key positions."""

    def __init__(self, obj):
        self.count = len(obj.data.shape_keys.key_blocks)
        super().__init__(f"{obj.name} has {self.count} shape keys")


def decimate(obj, target_tris, protect_group=None, detail=3.0, drop_shape_keys=False):
    """Reduce a mesh to about target_tris triangles with collapse decimation
    applied before any other modifier (so skin weights are kept).

    protect_group: a vertex group (e.g. the face) that keeps `detail` times
    more of its triangles than the rest of the mesh. Blender's Decimate vertex
    group is all-or-nothing, so the budget is split here and the two regions
    are decimated one after the other.

    Returns (before, after) triangle counts, or None if nothing was done.
    Meshes with shared data are skipped because the modifier cannot be applied.
    """
    if obj.type != "MESH" or not target_tris or obj.data.users > 1:
        return None
    before = triangle_count(obj.data)
    if before <= target_tris:
        return None
    if obj.data.shape_keys is not None:
        if not drop_shape_keys:
            raise HasShapeKeys(obj)
        obj.shape_key_clear()

    if not protect_group or protect_group not in obj.vertex_groups or detail <= 1.0:
        _apply_decimate(obj, target_tris / before)
        return before, triangle_count(obj.data)

    inside, tris_in, tris_out, _, _ = _region_stats(obj, protect_group)
    if not inside or not tris_out:
        _apply_decimate(obj, target_tris / before)
        return before, triangle_count(obj.data)

    # Keep fraction r of the rest and min(1, detail * r) of the protected region.
    r_rest = target_tris / (tris_out + detail * tris_in)
    if detail * r_rest >= 1.0:  # the whole face fits: keep it untouched
        r_rest = max(target_tris - tris_in, 0.05 * target_tris) / tris_out
    want_in = min(tris_in, round(detail * r_rest * tris_in))

    # pass 1: everything except the protected region
    rest = obj.vertex_groups.new(name="b2ue_rest")
    rest.add([v.index for v in obj.data.vertices if v.index not in inside], 1.0, "REPLACE")
    _apply_decimate(obj, (r_rest * tris_out + tris_in) / before, mask_group=rest.name)
    obj.vertex_groups.remove(obj.vertex_groups["b2ue_rest"])

    # pass 2: the protected region down to its share
    now = triangle_count(obj.data)
    if want_in < tris_in and now > target_tris * 1.02:
        _apply_decimate(obj, (now - tris_in + want_in) / now, mask_group=protect_group)
    return before, triangle_count(obj.data)


def cleanup_scene(scene, merge_distance=1e-5, triangulate=False):
    stats = {"transforms_applied": 0, "merged_verts": 0,
             "loose_edges_removed": 0, "loose_verts_removed": 0}
    stats.update(flatten_hierarchy(scene))
    for obj in scene.objects:
        if obj.type != "MESH":
            continue
        if apply_rotation_scale(obj):
            stats["transforms_applied"] += 1
        for key, value in clean_mesh(obj, merge_distance, triangulate).items():
            stats[key] += value
    return stats
