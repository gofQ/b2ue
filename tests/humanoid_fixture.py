# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Abdulkerim Tabakoğlu
"""Build test humanoids the way AI tools deliver them: one dense, unrigged,
UV-mapped mesh, about 1 unit tall, facing -Y, in T-pose or A-pose."""

import math

import bmesh
import bpy


def _segment(bm, a, b, radius):
    """Add a closed tube from a to b (a cylinder with rounded caps)."""
    import mathutils
    a, b = mathutils.Vector(a), mathutils.Vector(b)
    length = (b - a).length
    res = bmesh.ops.create_cone(bm, cap_ends=True, segments=24, radius1=radius,
                                radius2=radius * 0.9, depth=length)
    rot = (b - a).to_track_quat("Z", "Y").to_matrix().to_4x4()
    mat = mathutils.Matrix.Translation((a + b) / 2) @ rot
    bmesh.ops.transform(bm, matrix=mat, verts=res["verts"])


def _sphere(bm, centre, radius, scale=(1, 1, 1)):
    import mathutils
    res = bmesh.ops.create_uvsphere(bm, u_segments=32, v_segments=16, radius=radius)
    mat = mathutils.Matrix.Translation(centre) @ mathutils.Matrix.Diagonal((*scale, 1))
    bmesh.ops.transform(bm, matrix=mat, verts=res["verts"])


def make_humanoid(name="tripo_node_human", pose="T", subdivide=2):
    """A ~1 unit tall humanoid. pose: "T" (arms horizontal) or "A" (arms 45 deg down)."""
    bm = bmesh.new()
    # legs (left = +X when facing -Y)
    for side in (1, -1):
        x = 0.07 * side
        _segment(bm, (x, 0, 0.47), (x, 0, 0.26), 0.05)       # thigh
        _segment(bm, (x, 0, 0.26), (x, 0, 0.04), 0.04)       # calf
        _segment(bm, (x, -0.02, 0.03), (x, -0.09, 0.02), 0.03)  # foot, toes forward (-Y)
    # torso, neck, head with a nose so "front" exists
    _segment(bm, (0, 0, 0.45), (0, 0, 0.78), 0.11)
    _segment(bm, (0, 0, 0.78), (0, 0, 0.86), 0.04)
    _sphere(bm, (0, 0, 0.92), 0.07, (0.9, 1.0, 1.1))
    _sphere(bm, (0, -0.07, 0.915), 0.015)                    # nose
    # arms
    angle = 0.0 if pose == "T" else math.radians(45)
    for side in (1, -1):
        sh = (0.13 * side, 0, 0.76)
        def along(d):
            return (sh[0] + side * d * math.cos(angle), 0, sh[2] - d * math.sin(angle))
        _segment(bm, sh, along(0.15), 0.035)                 # upper arm
        _segment(bm, along(0.15), along(0.29), 0.03)         # forearm
        _segment(bm, along(0.29), along(0.36), 0.025)        # hand
    if subdivide:
        bmesh.ops.subdivide_edges(bm, edges=bm.edges[:], cuts=subdivide, use_grid_fill=True)
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    obj = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    with bpy.context.temp_override(object=obj, active_object=obj, selected_objects=[obj],
                                   selected_editable_objects=[obj]):
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.uv.smart_project()
        bpy.ops.object.mode_set(mode="OBJECT")
    mat = bpy.data.materials.new("skin")
    me.materials.append(mat)
    return obj
