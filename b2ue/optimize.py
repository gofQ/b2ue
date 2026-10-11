# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Abdulkerim Tabakoğlu
"""Decimate one mesh with measurement: a fixed triangle budget, or the lowest
count that stays within a shape-error limit, optionally followed by a normal
map bake from the original. Every result carries before/after numbers."""

from . import bake, cleanup, quality

# Bake only when the decimated surface turns away from the original by more
# than this on average; below it a baked map changes nothing visible.
BAKE_BELOW_DEG = 4.0

# Shape error limits, in % of the bounding-box diagonal: (p95, max).
# Chosen from measurements on AI-generated props (see README, "Quality").
QUALITY_LEVELS = {
    "high": (0.01, 0.05),
    "balanced": (0.02, 0.10),
    "light": (0.05, 0.30),
}


def _within(q, level):
    p95, worst = QUALITY_LEVELS[level]
    return q["shape_p95"] <= p95 and q["shape_max"] <= worst


def optimize_mesh(obj, target_tris=0, quality_level="", protect_group=None, detail=3.0,
                  drop_shape_keys=False, bake_size=0, measure=True, min_tris=1000,
                  texture_limit=0):
    """Returns a dict: before, after, quality (or None), baked (bool), note."""
    before = cleanup.triangle_count(obj.data)
    out = {"before": before, "after": before, "quality": None, "baked": False, "note": ""}
    cap = target_tris or before
    if before <= cap and not quality_level:
        return None

    if obj.data.shape_keys is not None and not drop_shape_keys:
        raise cleanup.HasShapeKeys(obj)

    image = quality.base_colour_image(obj)
    reference = quality.Snapshot(obj, image) if (measure or quality_level) else None
    high = bake.keep_high_poly(obj) if bake_size else None
    try:
        if before > cap:
            cleanup.decimate(obj, cap, protect_group, detail, drop_shape_keys)
        q = quality.compare(reference, quality.Snapshot(obj, image)) if reference else None

        if quality_level:
            if q is not None and not _within(q, quality_level):
                out["note"] = (f"even {cleanup.triangle_count(obj.data):,} tris exceed the "
                               f"'{quality_level}' limit; kept that budget")
            # step down while the result stays inside the limit
            while q is not None and _within(q, quality_level):
                current = cleanup.triangle_count(obj.data)
                nxt = int(current * 0.7)
                if nxt < min_tris:
                    break
                saved = obj.data.copy()
                cleanup.decimate(obj, nxt, protect_group, detail, drop_shape_keys)
                trial = quality.compare(reference, quality.Snapshot(obj, image))
                if _within(trial, quality_level):
                    q = trial
                    bpy_remove_mesh(saved)
                else:
                    rejected = obj.data
                    obj.data = saved
                    bpy_remove_mesh(rejected)
                    break

        out["after"] = cleanup.triangle_count(obj.data)
        out["quality"] = q
        # free the measurement data (BVH of the original) before baking:
        # the bake needs a lot of memory of its own
        reference = None
        import gc
        gc.collect()
        if high is not None and q is not None and q["normal_deg"] < BAKE_BELOW_DEG:
            # the surface still points the same way as the original: a baked
            # map would add nothing (measured on a 2M -> 72k Tripo mask: 0.68%
            # vs 0.69% render difference) but costs time and ~2 GB of memory
            out["note"] = (out["note"] + "; " if out["note"] else "") + (
                f"normal bake not needed (surface within {q['normal_deg']:.1f}°)")
        elif high is not None:
            h, high = high, None  # bake_normals deletes it
            try:
                baked, notes = bake.bake_normals(obj, h, size=bake_size,
                                                 name=f"{obj.name}_baked_normal",
                                                 texture_limit=texture_limit)
                out["baked"] = bool(baked)
                out["baked_materials"] = baked
                if notes:
                    out["note"] = "; ".join(([out["note"]] if out["note"] else []) + notes)
            except Exception as exc:  # a failed bake must not lose the decimated mesh
                out["note"] = (out["note"] + "; " if out["note"] else "") + f"normal bake skipped: {exc}"
    finally:
        if high is not None:
            import bpy
            bpy.data.objects.remove(high, do_unlink=True)
    return out


def bpy_remove_mesh(mesh):
    import bpy
    if mesh.users == 0:
        bpy.data.meshes.remove(mesh)
