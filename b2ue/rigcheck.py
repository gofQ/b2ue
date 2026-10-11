# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Abdulkerim Tabakoğlu
"""Measure how well an auto-rig deforms: pose the skeleton and look at how
much the mesh edges stretch. Good weights stretch edges a little and evenly;
bad weights (a vertex stuck to the wrong bone) produce long spikes and tears.
"""

import math

import bpy
import numpy as np
from mathutils import Euler, Matrix

# A test pose for mannequin bone names: arms down, elbows and knees bent,
# head and spine turned. Angles in degrees, applied around each bone's own axes.
TEST_POSE = {
    "upperarm_l": (0, 0, -40), "upperarm_r": (0, 0, 40),
    "lowerarm_l": (0, 0, -50), "lowerarm_r": (0, 0, 50),
    "thigh_l": (-45, 0, 0), "calf_l": (70, 0, 0),
    "spine_02": (0, 20, 0), "head": (15, 25, 0),
}


def _edge_lengths(mesh, coords):
    edges = np.empty(len(mesh.edges) * 2, dtype=np.int64)
    mesh.edges.foreach_get("vertices", edges)
    edges = edges.reshape(-1, 2)
    return np.linalg.norm(coords[edges[:, 0]] - coords[edges[:, 1]], axis=1)


def _evaluated_coords(obj):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    ev = obj.evaluated_get(depsgraph)
    me = ev.to_mesh()
    co = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get("co", co)
    ev.to_mesh_clear()
    return co.reshape(-1, 3)


def pose_stretch(mesh_obj, armature, pose=TEST_POSE):
    """Pose the armature, measure edge stretch, then restore the rest pose.

    Returns a dict: median / p99 / max stretch (1.0 = unchanged length) and the
    share of edges stretched more than 2x ("tears").
    """
    rest = _evaluated_coords(mesh_obj)
    rest_len = _edge_lengths(mesh_obj.data, rest)
    saved = {}
    for name, angles in pose.items():
        bone = armature.pose.bones.get(name)
        if bone is None:
            continue
        saved[name] = (bone.rotation_mode, bone.rotation_euler.copy())
        bone.rotation_mode = "XYZ"
        bone.rotation_euler = Euler([math.radians(a) for a in angles])
    bpy.context.view_layer.update()
    posed = _evaluated_coords(mesh_obj)
    for name, (mode, rot) in saved.items():
        bone = armature.pose.bones[name]
        bone.rotation_euler = rot
        bone.rotation_mode = mode
    bpy.context.view_layer.update()

    posed_len = _edge_lengths(mesh_obj.data, posed)
    ok = rest_len > 1e-9
    ratio = np.maximum(posed_len[ok] / rest_len[ok], rest_len[ok] / np.maximum(posed_len[ok], 1e-12))
    return {
        "median": float(np.median(ratio)),
        "p99": float(np.percentile(ratio, 99)),
        "max": float(ratio.max()),
        "tears": float((ratio > 2.0).mean()),
    }
