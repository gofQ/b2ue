# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Abdulkerim Tabakoğlu
"""Measure how much a processed mesh differs from the original.

All distances are reported as a percentage of the original's bounding-box
diagonal, so numbers mean the same for a 10 cm prop and a 2 m character.

  shape_mean / shape_p95 / shape_max   surface distance (both directions)
  normal_deg                           mean angle between surface normals
  texture                              mean base-colour difference (0-100 %)
"""

import random

import bpy
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree
from mathutils.geometry import barycentric_transform


class Snapshot:
    """Triangles, UVs and base colour of a mesh, kept for comparison later."""

    def __init__(self, obj, image=None):
        me = obj.data
        me.calc_loop_triangles()
        mw = obj.matrix_world
        co = np.empty(len(me.vertices) * 3)
        me.vertices.foreach_get("co", co)
        co = (co.reshape(-1, 3) @ np.array(mw.to_3x3()).T + np.array(mw.translation)).astype(np.float32)
        tri = np.empty(len(me.loop_triangles) * 3, dtype=np.int64)
        me.loop_triangles.foreach_get("vertices", tri)
        self.tris = tri.reshape(-1, 3).astype(np.int32)
        self.co = co
        self.uv = None
        if me.uv_layers.active is not None:
            loops = np.empty(len(me.loop_triangles) * 3, dtype=np.int64)
            me.loop_triangles.foreach_get("loops", loops)
            uv = np.empty(len(me.loops) * 2)
            me.uv_layers.active.data.foreach_get("uv", uv)
            self.uv = uv.reshape(-1, 2).astype(np.float32)[loops.reshape(-1, 3)]
        self.pixels = _pixels(image) if image is not None else None  # shared, small copy
        a, b, c = (co[self.tris[:, i]] for i in range(3))
        cross = np.cross(b - a, c - a)
        self.area = np.linalg.norm(cross, axis=1) / 2
        self.normals = cross / np.maximum(np.linalg.norm(cross, axis=1), 1e-20)[:, None]
        # numpy arrays go straight to C; Python lists of 2M Vectors cost ~700 MB
        self.bvh = BVHTree.FromPolygons(co, self.tris)
        self.diagonal = float(np.linalg.norm(co.max(0) - co.min(0))) or 1.0

    def sample(self, n, rng):
        """n random surface points: (points, triangle index, barycentric uv)."""
        p = self.area / self.area.sum()
        idx = rng.choice(len(self.tris), size=n, p=p)
        u, v = rng.random(n), rng.random(n)
        flip = u + v > 1
        u[flip], v[flip] = 1 - u[flip], 1 - v[flip]
        a, b, c = (self.co[self.tris[idx, i]] for i in range(3))
        pts = a + (b - a) * u[:, None] + (c - a) * v[:, None]
        return pts, idx, u, v

    def colour_at(self, tri_index, point):
        if self.uv is None or self.pixels is None:
            return None
        t = self.tris[tri_index]
        uvs = self.uv[tri_index]
        uv = barycentric_transform(Vector(point), *(Vector(self.co[i]) for i in t),
                                   *(Vector((*x, 0)) for x in uvs))
        h, w, _ = self.pixels.shape
        return self.pixels[int((uv.y % 1) * (h - 1)), int((uv.x % 1) * (w - 1))]


_PIXEL_CACHE = {}


def _pixels(image, size=1024):
    """Base colour as a small (at most size x size) array, cached per image.
    A 4K float copy costs 200 MB; colour comparison does not need it."""
    key = (image.name, size)
    if key in _PIXEL_CACHE:
        return _PIXEL_CACHE[key]
    w, h = image.size
    if not w:
        return None
    px = np.empty(w * h * 4, dtype=np.float32)
    image.pixels.foreach_get(px)
    px = px.reshape(h, w, 4)
    step = max(1, max(w, h) // size)
    small = np.ascontiguousarray(px[::step, ::step, :3])
    del px
    _PIXEL_CACHE.clear()  # one image at a time is enough
    _PIXEL_CACHE[key] = small
    return small


def base_colour_image(obj):
    for slot in obj.material_slots:
        mat = slot.material
        if not (mat and mat.use_nodes):
            continue
        for node in mat.node_tree.nodes:
            if node.type == "TEX_IMAGE" and node.image is not None:
                for link in node.outputs["Color"].links:
                    if link.to_socket.name == "Base Color":
                        return node.image
    return None


def compare(before, after, samples=6000, seed=1):
    """Compare two Snapshots. Returns a dict of percentages / degrees."""
    rng = np.random.default_rng(seed)
    dists, angles, colours = [], [], []
    for src, dst, measure_rest in ((after, before, True), (before, after, False)):
        pts, idx, _, _ = src.sample(samples, rng)
        for k, p in enumerate(pts):
            loc, normal, face, d = dst.bvh.find_nearest(Vector(p))
            if loc is None:
                continue
            dists.append(d)
            if measure_rest:
                cos = abs(float(np.dot(src.normals[idx[k]], dst.normals[face])))
                angles.append(np.degrees(np.arccos(min(cos, 1.0))))
                c_new = src.colour_at(idx[k], p)
                c_old = dst.colour_at(face, loc)
                if c_new is not None and c_old is not None:
                    colours.append(float(np.abs(c_new - c_old).mean()))
    d = np.array(dists) / before.diagonal * 100
    return {
        "shape_mean": float(d.mean()),
        "shape_p95": float(np.percentile(d, 95)),
        "shape_max": float(d.max()),
        "normal_deg": float(np.mean(angles)) if angles else None,
        "texture": float(np.mean(colours) * 100) if colours else None,
    }


def describe(q):
    text = f"shape off by {q['shape_mean']:.3f}% avg, {q['shape_p95']:.2f}% p95"
    if q.get("texture") is not None:
        text += f", colour {q['texture']:.2f}%"
    return text
