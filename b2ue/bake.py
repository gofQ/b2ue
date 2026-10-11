# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Abdulkerim Tabakoğlu
"""Bake the detail lost by decimation back into normal maps.

A copy of the mesh is kept before decimation. After decimation, Cycles casts
rays from the low-poly surface to the high-poly copy and records the
high-poly surface direction (including its own normal map) in a new
tangent-space normal map. The low-poly mesh then shades almost like the
original.

Every material gets its own image: characters usually have several
materials (skin, eyes, clothes) whose UVs all fill the same 0-1 square, so a
single shared image would paint one material's detail over another's.

A material is left alone (it keeps its own normal map) when:
  - its UVs overlap themselves (mirrored or stacked islands): a bake would mix
    the overlapping parts;
  - it already has a normal map sharper than the bake size: baking would make
    it blurrier;
  - the bake missed the original surface or came out facing backwards on too
    many pixels (checked after baking).
"""

import bpy
import numpy as np

UV_OVERLAP_LIMIT = 1.15   # total UV area / covered area; 1.0 = no overlap
BAD_PIXEL_LIMIT = 0.03    # share of covered pixels that may be missed / inverted


def keep_high_poly(obj):
    """Copy of obj (with its own mesh data) to bake from later."""
    high = obj.copy()
    high.data = obj.data.copy()
    high.name = f"{obj.name}_b2ue_high"
    bpy.context.scene.collection.objects.link(high)
    high.modifiers.clear()
    return high


def _normal_map_node(mat):
    for node in mat.node_tree.nodes:
        if node.type == "NORMAL_MAP":
            return node
    return None


def _normal_map_size(mat):
    """Largest side of the image feeding mat's Normal Map node (0 if none)."""
    if not (mat and mat.use_nodes):
        return 0
    nmap = _normal_map_node(mat)
    if nmap is None:
        return 0
    for link in nmap.inputs["Color"].links:
        node = link.from_node
        if node.type == "TEX_IMAGE" and node.image is not None:
            return max(node.image.size)
    return 0


def _uv_triangles(mesh):
    """(uv triangles (n,3,2), material index per triangle) or (None, None)."""
    if mesh.uv_layers.active is None:
        return None, None
    mesh.calc_loop_triangles()
    n = len(mesh.loop_triangles)
    loops = np.empty(n * 3, dtype=np.int64)
    mesh.loop_triangles.foreach_get("loops", loops)
    mat = np.empty(n, dtype=np.int64)
    mesh.loop_triangles.foreach_get("material_index", mat)
    uv = np.empty(len(mesh.loops) * 2, dtype=np.float64)
    mesh.uv_layers.active.data.foreach_get("uv", uv)
    return uv.reshape(-1, 2)[loops].reshape(n, 3, 2), mat


def _uv_samples(tris, res, per_pixel=4, seed=0):
    """Random points spread over the UV triangles (about per_pixel per covered
    pixel), as integer pixel coordinates. Returns (px, total area in pixels)."""
    a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
    e1, e2 = b - a, c - a
    area = np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]) / 2 * res * res
    total = float(area.sum())
    if total <= 0:
        return np.zeros((0, 2), dtype=np.int64), 0.0
    count = int(min(max(total * per_pixel, 1000), 4_000_000))
    rng = np.random.default_rng(seed)
    idx = rng.choice(len(tris), size=count, p=area / total)
    u, v = rng.random(count), rng.random(count)
    flip = u + v > 1
    u[flip], v[flip] = 1 - u[flip], 1 - v[flip]
    p = a[idx] + (b[idx] - a[idx]) * u[:, None] + (c[idx] - a[idx]) * v[:, None]
    px = np.clip((np.mod(p, 1.0) * res).astype(np.int64), 0, res - 1)
    return px, total


def uv_overlap(tris, res=256):
    """Total UV area / area actually covered. 1.0 means no overlap, 2.0 means
    everything is covered twice (e.g. mirrored halves)."""
    px, total = _uv_samples(tris, res)
    if total <= 0:
        return 1.0
    covered = len(np.unique(px[:, 1] * res + px[:, 0]))
    # with ~4 samples per covered pixel, ~2% of truly covered pixels get none
    return total / max(covered / 0.98, 1)


def check_baked(image, tris, res=256):
    """Share of the material's pixels where the bake missed (no direction) or
    points into the surface. Both show up as dark smears when rendered."""
    w, h = image.size
    px = np.empty(w * h * 4, dtype=np.float32)
    image.pixels.foreach_get(px)
    px = px.reshape(h, w, 4)
    step = max(1, max(w, h) // res)
    small = px[::step, ::step, :3]
    del px
    sh, sw = small.shape[:2]
    pts, _ = _uv_samples(tris, max(sh, sw), per_pixel=1)
    if not len(pts):
        return 0.0
    pts = np.minimum(pts, [sw - 1, sh - 1])
    vals = small[pts[:, 1], pts[:, 0]]
    vec = vals * 2 - 1
    length = np.linalg.norm(vec, axis=1)
    bad = (length < 0.5) | (vec[:, 2] < 0.05)
    return float(bad.mean())


def bake_normals(low, high, size=2048, name="baked_normal", texture_limit=0):
    """Bake high -> low, one image per material, wire each image into its
    material and delete high (always, even when baking fails).

    Returns (baked material names, notes about materials left alone)."""
    try:
        return _bake_normals(low, high, size, name, texture_limit)
    finally:
        bpy.data.objects.remove(high, do_unlink=True)


def _bake_normals(low, high, size, name, texture_limit=0):
    scene = bpy.context.scene
    tris, mat_index = _uv_triangles(low.data)
    if tris is None:
        raise ValueError(f"{low.name} has no UV map")

    targets, notes = [], []
    for i, slot in enumerate(low.material_slots):
        mat = slot.material
        mine = tris[mat_index == i]
        if mat is None or not len(mine):
            continue
        overlap = uv_overlap(mine)
        if overlap > UV_OVERLAP_LIMIT:
            notes.append(f"{mat.name}: UVs overlap ({overlap:.2f}x), kept its own normal map")
            continue
        own = _normal_map_size(mat)
        # compare with what will actually be exported: the texture step
        # shrinks the material's own map to texture_limit anyway
        exported = min(own, texture_limit) if texture_limit else own
        if exported > size:
            notes.append(f"{mat.name}: kept its sharper {exported}px normal map")
            continue
        mat.use_nodes = True
        image = bpy.data.images.new(f"{name}_{mat.name}", size, size, alpha=False,
                                    float_buffer=False)
        image.colorspace_settings.name = "Non-Color"
        image.generated_color = (0.5, 0.5, 1.0, 1.0)
        node = mat.node_tree.nodes.new("ShaderNodeTexImage")
        node.image = image
        node.interpolation = "Linear"
        targets.append((mat, node, image, mine))

    # Cycles bakes into the *active* image node of every material, so the
    # materials we leave alone must not have one pointing at a real texture.
    previous_active = {}
    for slot in low.material_slots:
        mat = slot.material
        if mat is None or not mat.use_nodes:
            continue
        previous_active[mat.name] = mat.node_tree.nodes.active
    dummy = None
    if targets and len(targets) < len([s for s in low.material_slots if s.material]):
        dummy = bpy.data.images.new(f"{name}_unused", 8, 8, alpha=False)
    for slot in low.material_slots:
        mat = slot.material
        if mat is None:
            continue
        mat.use_nodes = True
        mine = next((t for t in targets if t[0] == mat), None)
        if mine:
            mat.node_tree.nodes.active = mine[1]
        elif dummy is not None:
            sink = mat.node_tree.nodes.new("ShaderNodeTexImage")
            sink.name = "b2ue_bake_sink"
            sink.image = dummy
            mat.node_tree.nodes.active = sink

    if not targets:
        return [], notes

    diagonal = max(low.dimensions) or 1.0
    previous_engine = scene.render.engine
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 1
    bake = scene.render.bake
    bake.use_selected_to_active = True
    bake.use_cage = False
    bake.cage_extrusion = diagonal * 0.01
    bake.max_ray_distance = diagonal * 0.05
    bake.normal_space = "TANGENT"
    bake.margin = 8

    for other in scene.objects:
        other.select_set(False)
    high.select_set(True)
    low.select_set(True)
    bpy.context.view_layer.objects.active = low
    try:
        with bpy.context.temp_override(object=low, active_object=low,
                                       selected_objects=[high, low],
                                       selected_editable_objects=[high, low]):
            bpy.ops.object.bake(type="NORMAL")
    finally:
        scene.render.engine = previous_engine
        for slot in low.material_slots:
            mat = slot.material
            if mat is None or not mat.use_nodes:
                continue
            sink = mat.node_tree.nodes.get("b2ue_bake_sink")
            if sink is not None:
                mat.node_tree.nodes.remove(sink)
            prev = previous_active.get(mat.name)
            if prev is not None and prev.name in mat.node_tree.nodes:
                mat.node_tree.nodes.active = prev
        if dummy is not None:
            bpy.data.images.remove(dummy)

    baked = []
    for mat, node, image, mine in targets:
        tree = mat.node_tree
        bad = check_baked(image, mine)
        if bad > BAD_PIXEL_LIMIT:
            notes.append(f"{mat.name}: bake came out noisy ({bad:.0%} of pixels missed the "
                         f"surface or face inwards), kept the original normal map")
            tree.nodes.remove(node)
            bpy.data.images.remove(image)
            continue
        nmap = _normal_map_node(mat)
        bsdf = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if nmap is None and bsdf is not None:
            nmap = tree.nodes.new("ShaderNodeNormalMap")
            tree.links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])
        if nmap is None:
            tree.nodes.remove(node)
            bpy.data.images.remove(image)
            continue
        for link in list(nmap.inputs["Color"].links):
            old = link.from_node
            tree.links.remove(link)
            if old.type == "TEX_IMAGE" and not old.outputs["Color"].links:
                tree.nodes.remove(old)
        nmap.space = "TANGENT"
        tree.links.new(node.outputs["Color"], nmap.inputs["Color"])
        image.pack()
        baked.append(mat.name)
    return baked, notes
