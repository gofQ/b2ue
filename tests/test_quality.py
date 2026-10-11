# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Abdulkerim Tabakoğlu
"""Measured decimation, quality levels, normal baking and rig quality."""

import sys
import time
from pathlib import Path

import bpy
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import b2ue  # noqa: E402
from b2ue import addon, cleanup, humanoid, quality, rigcheck  # noqa: E402
from b2ue.options import Options, build  # noqa: E402
from b2ue.pipeline import process_file  # noqa: E402
from humanoid_fixture import make_humanoid  # noqa: E402
from test_pipeline import empty_scene, reimport, save  # noqa: E402


def detailed_prop(tmp_path, name="statue.blend"):
    """A dense, bumpy, UV-mapped, textured sphere: easy to measure."""
    empty_scene()
    bpy.ops.mesh.primitive_uv_sphere_add(segments=256, ring_count=128)
    obj = bpy.context.active_object
    obj.name = "tripo_node_x"
    tex = bpy.data.textures.new("bumps", "VORONOI")
    disp = obj.modifiers.new("d", "DISPLACE")
    disp.texture, disp.strength = tex, 0.05
    with bpy.context.temp_override(object=obj, active_object=obj):
        bpy.ops.object.modifier_apply(modifier="d")
    img = bpy.data.images.new("base", 64, 64)
    img.pixels = [v for i in range(64 * 64) for v in ((i % 64) / 64, (i // 64) / 64, 0.5, 1)]
    img.pack()
    mat = bpy.data.materials.new("M")
    mat.use_nodes = True
    node = mat.node_tree.nodes.new("ShaderNodeTexImage")
    node.image = img
    mat.node_tree.links.new(node.outputs["Color"],
                            mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"])
    obj.data.materials.append(mat)
    return save(tmp_path, name)


def test_measurement_is_zero_for_identical_and_grows_with_reduction(tmp_path):
    detailed_prop(tmp_path)
    obj = bpy.context.scene.objects["tripo_node_x"]
    img = quality.base_colour_image(obj)
    ref = quality.Snapshot(obj, img)
    same = quality.compare(ref, quality.Snapshot(obj, img))
    assert same["shape_max"] < 1e-3 and same["texture"] < 0.5  # float precision only
    errors = []
    for budget in (20000, 5000, 1000):
        cleanup.decimate(obj, budget)
        errors.append(quality.compare(ref, quality.Snapshot(obj, img))["shape_mean"])
    assert errors[0] < errors[1] < errors[2]


def test_quality_level_picks_fewer_tris_within_the_limit(tmp_path):
    src = detailed_prop(tmp_path)
    result = process_file(src, tmp_path / "out", Options(quality="light", target_tris=40000))
    assert result["error"] is None
    [q] = result["quality"]
    assert q["after"] < 40000                       # went below the upper limit
    p95, worst = __import__("b2ue.optimize", fromlist=["x"]).QUALITY_LEVELS["light"]
    assert q["quality"]["shape_p95"] <= p95 and q["quality"]["shape_max"] <= worst


def test_bake_is_skipped_when_normals_already_match(tmp_path):
    src = detailed_prop(tmp_path)
    result = process_file(src, tmp_path / "out",
                          Options(target_tris=20000, bake_normals=True, texture_size=256))
    assert result["error"] is None
    assert not result["quality"][0]["baked"]
    assert "not needed" in result["quality"][0]["note"]


def test_baked_normal_map_is_exported_and_used(monkeypatch, tmp_path):
    monkeypatch.setattr("b2ue.optimize.BAKE_BELOW_DEG", 0.0)   # always bake
    src = detailed_prop(tmp_path)
    result = process_file(src, tmp_path / "out",
                          Options(target_tris=2000, bake_normals=True, texture_size=256))
    assert result["error"] is None
    assert result["quality"][0]["baked"]
    [item] = result["exported"]
    normal = [t for t in item["textures"] if t["role"] == "Normal"]
    assert normal and normal[0]["size"] == [256, 256]
    reimport(item["path"])
    used = [n.image for m in bpy.data.materials if m.use_nodes for n in m.node_tree.nodes
            if n.type == "TEX_IMAGE" and any(l.to_node.type == "NORMAL_MAP"
                                             for l in n.outputs["Color"].links)]
    assert used and Path(used[0].filepath).name == Path(normal[0]["path"]).name


def _two_material_prop(tmp_path, own_normal=0):
    """The detailed sphere with a second material on its lower half. Each
    material's UVs fill the whole 0-1 square, like a character's skin and
    clothes: they overlap each other but not themselves."""
    detailed_prop(tmp_path)
    obj = bpy.context.scene.objects["tripo_node_x"]
    second = bpy.data.materials.new("Cloth")
    second.use_nodes = True
    if own_normal:
        img = bpy.data.images.new("cloth_normal", own_normal, own_normal)
        img.generated_color = (0.5, 0.5, 1.0, 1.0)
        img.pack()
        tree = second.node_tree
        tex = tree.nodes.new("ShaderNodeTexImage")
        tex.image = img
        nmap = tree.nodes.new("ShaderNodeNormalMap")
        tree.links.new(tex.outputs["Color"], nmap.inputs["Color"])
        tree.links.new(nmap.outputs["Normal"], tree.nodes["Principled BSDF"].inputs["Normal"])
    obj.data.materials.append(second)
    me = obj.data
    uv = me.uv_layers.active.data
    for poly in me.polygons:
        lower = poly.center.z < 0
        poly.material_index = 1 if lower else 0
        for li in poly.loop_indices:
            u, v = uv[li].uv
            uv[li].uv = (u, min(max(v * 2 if lower else v * 2 - 1, 0.0), 1.0))
    return save(tmp_path, "two.blend")


def test_uv_overlap_measure():
    from b2ue import bake
    import numpy as np
    square = np.array([[[0, 0], [1, 0], [1, 1]], [[0, 0], [1, 1], [0, 1]]], dtype=float)
    assert bake.uv_overlap(square) == pytest.approx(1.0, abs=0.06)
    assert bake.uv_overlap(np.vstack([square, square])) == pytest.approx(2.0, abs=0.12)


def test_every_material_gets_its_own_baked_map(monkeypatch, tmp_path):
    """Two materials sharing the UV square used to be baked into one image,
    painting one material's detail over the other (dark smears on characters)."""
    monkeypatch.setattr("b2ue.optimize.BAKE_BELOW_DEG", 0.0)
    src = _two_material_prop(tmp_path)
    result = process_file(src, tmp_path / "out",
                          Options(target_tris=2000, bake_normals=True, texture_size=256))
    assert result["error"] is None
    q = result["quality"][0]
    assert sorted(q["baked_materials"]) == ["Cloth", "M"]
    [item] = result["exported"]
    normals = [t for t in item["textures"] if t["role"] == "Normal"]
    assert len(normals) == 2
    reimport(item["path"])
    images = {}
    for m in bpy.data.materials:
        if not m.use_nodes:
            continue
        for n in m.node_tree.nodes:
            if n.type == "TEX_IMAGE" and any(l.to_node.type == "NORMAL_MAP"
                                             for l in n.outputs["Color"].links):
                images[m.name] = n.image.name
    assert len(set(images.values())) == 2


def test_sharper_own_normal_map_is_kept(monkeypatch, tmp_path):
    monkeypatch.setattr("b2ue.optimize.BAKE_BELOW_DEG", 0.0)
    src = _two_material_prop(tmp_path, own_normal=512)
    result = process_file(src, tmp_path / "out",
                          Options(target_tris=2000, bake_normals=True, bake_size=256))
    q = result["quality"][0]
    assert q["baked_materials"] == ["M"]
    assert "Cloth: kept its sharper 512px normal map" in q["note"]


def test_own_map_shrunk_by_texture_limit_is_baked(monkeypatch, tmp_path):
    """A 512 px map exported at 256 px is no sharper than a 256 px bake, and
    the bake adds the detail lost by decimation: bake it."""
    monkeypatch.setattr("b2ue.optimize.BAKE_BELOW_DEG", 0.0)
    src = _two_material_prop(tmp_path, own_normal=512)
    result = process_file(src, tmp_path / "out",
                          Options(target_tris=2000, bake_normals=True, texture_size=256,
                                  bake_size=2048))
    q = result["quality"][0]
    assert sorted(q["baked_materials"]) == ["Cloth", "M"]


def test_mirrored_uvs_are_not_baked(monkeypatch, tmp_path):
    monkeypatch.setattr("b2ue.optimize.BAKE_BELOW_DEG", 0.0)
    detailed_prop(tmp_path)
    obj = bpy.context.scene.objects["tripo_node_x"]
    uv = obj.data.uv_layers.active.data
    for li in range(len(uv)):            # fold the right half onto the left
        u, v = uv[li].uv
        uv[li].uv = (u * 2 if u < 0.5 else (1 - u) * 2, v)
    src = save(tmp_path, "mirror.blend")
    result = process_file(src, tmp_path / "out",
                          Options(target_tris=2000, bake_normals=True, texture_size=256))
    q = result["quality"][0]
    assert not q["baked"] and "UVs overlap" in q["note"]


def test_proxy_weights_rig_multi_part_characters(tmp_path):
    """Separate pieces (here every limb is its own shell) made Blender's heat
    weights fail; the proxy method must weight everything and deform cleanly."""
    results = {}
    for method in ("heat", "proxy"):
        empty_scene()
        obj = make_humanoid(pose="A", subdivide=1)
        # an extra floating piece, like eyelashes or a hair card
        bpy.ops.mesh.primitive_plane_add(size=0.05, location=(0, -0.09, 0.95))
        card = bpy.context.active_object
        obj = humanoid.join_meshes([obj, card])
        humanoid.scale_to_height([obj], 1.8)
        humanoid.normalize(obj)
        lm = humanoid.find_landmarks(obj)
        arm, _, filled = humanoid.build_rig(obj, lm, method=method)
        results[method] = (filled, rigcheck.pose_stretch(obj, arm))
    filled, stretch = results["proxy"]
    assert filled == 0
    assert stretch["tears"] <= results["heat"][1]["tears"]
    assert stretch["max"] < 10


def test_addon_asset_name_field(monkeypatch, tmp_path):
    monkeypatch.setattr(addon, "blender_command", lambda: [sys.executable, str(addon.CLI)])
    src = detailed_prop(tmp_path)
    empty_scene()
    b2ue.register()
    try:
        s = bpy.context.scene.b2ue
        s.name = "Circus Bird"
        s.preset = "background"
        job = addon.start_job(s, [src], tmp_path / "out", ["--name", s.name])
        end = time.time() + 300
        while job.poll():
            assert time.time() < end
            time.sleep(0.05)
        assert job.status == "done", job.lines + job.log
        assert [Path(p).name for p in job.exported] == ["SM_Circus_Bird.fbx"]
        assert job.file_steps["decimate"][0] == "ok"
        assert job.plan[:2] == ["load", "cleanup"] and job.plan[-1] == "export"
    finally:
        b2ue.unregister()


def test_every_preset_is_valid():
    from b2ue.options import PRESET_HELP, PRESETS
    assert set(PRESETS) == set(PRESET_HELP)
    for name in PRESETS:
        opts = build(name)
        assert opts.target_tris > 0 or opts.quality
        if opts.rig:
            assert opts.human and opts.height
    assert build("creature").human is False
    assert build("auto_balanced").quality == "balanced"


def test_best_weights_pick_the_cleaner_deformation(tmp_path):
    """The default tries proxy and Blender heat weights and keeps whichever
    stretches the mesh less in the test pose; other vertex groups survive."""
    empty_scene()
    obj = make_humanoid(pose="A", subdivide=1)
    humanoid.scale_to_height([obj], 1.8)
    humanoid.normalize(obj)
    lm = humanoid.find_landmarks(obj)
    humanoid.mark_face(obj, lm)
    arm, _, _ = humanoid.build_rig(obj, lm)
    test = arm["b2ue_weight_test"]
    chosen = arm["b2ue_weights"]
    best = min(("proxy", test["proxy_tears_pct"]), ("heat", test["heat_tears_pct"]), key=lambda t: t[1])
    assert chosen == best[0] or test["proxy_tears_pct"] == test["heat_tears_pct"]
    assert humanoid.FACE_GROUP in obj.vertex_groups
    stretch = rigcheck.pose_stretch(obj, arm)
    assert abs(stretch["tears"] * 100 - test[f"{chosen}_tears_pct"]) < 1e-6
    deform = {b.name for b in arm.data.bones if b.use_deform}
    idx = {g.index for g in obj.vertex_groups if g.name in deform}
    assert all(sum(g.weight for g in v.groups if g.group in idx) > 0.99 for v in obj.data.vertices)


def _corner_spread(me):
    """Median angle (deg) between each corner normal and its vertex's mean."""
    import numpy as np
    cn = np.empty(len(me.loops) * 3); me.corner_normals.foreach_get("vector", cn); cn = cn.reshape(-1, 3)
    lv = np.empty(len(me.loops), dtype=np.int64); me.loops.foreach_get("vertex_index", lv)
    sums = np.zeros((len(me.vertices), 3)); np.add.at(sums, lv, cn)
    mean = sums / np.maximum(np.linalg.norm(sums, axis=1), 1e-12)[:, None]
    return float(np.median(np.degrees(np.arccos(np.clip((cn * mean[lv]).sum(1), -1, 1)))))


def test_decimated_custom_normals_stay_smooth(tmp_path):
    """Decimation left each corner with the normal of a different original
    vertex, so the surface looked faceted (median 12.9 deg on a character)."""
    import numpy as np
    detailed_prop(tmp_path)
    obj = bpy.context.scene.objects["tripo_node_x"]
    me = obj.data
    co = np.empty(len(me.vertices) * 3); me.vertices.foreach_get("co", co); co = co.reshape(-1, 3)
    smooth = co / np.linalg.norm(co, axis=1)[:, None]          # smooth sphere normals
    rng = np.random.default_rng(0)                                # plus AI-style noise
    noisy = smooth + rng.normal(0, 0.08, smooth.shape)
    me.normals_split_custom_set_from_vertices(noisy / np.linalg.norm(noisy, axis=1)[:, None])
    cleanup.decimate(obj, 2000)
    assert obj.data.has_custom_normals
    assert _corner_spread(obj.data) < 1.0


def test_matte_raises_roughness_but_keeps_its_detail(tmp_path):
    """--matte: glossy AI materials (roughness ~0.1) come out at least
    matte_min rough, and the variation in the map survives."""
    import numpy as np
    detailed_prop(tmp_path)
    obj = bpy.context.scene.objects["tripo_node_x"]
    mat = obj.data.materials[0]
    rough = bpy.data.images.new("rough", 64, 64)
    rough.colorspace_settings.name = "Non-Color"            # before pixels: it resets them
    vals = np.tile(np.linspace(0.05, 0.3, 64), 64)          # glossy, with a gradient
    rough.pixels = np.repeat(vals, 4).astype(np.float32)
    rough.pack()
    tree = mat.node_tree
    tex = tree.nodes.new("ShaderNodeTexImage")
    tex.image = rough
    tree.links.new(tex.outputs["Color"], tree.nodes["Principled BSDF"].inputs["Roughness"])
    second = bpy.data.materials.new("Glossy")              # roughness typed in, no map
    second.use_nodes = True
    second.node_tree.nodes["Principled BSDF"].inputs["Roughness"].default_value = 0.1
    obj.data.materials.append(second)
    src = save(tmp_path, "glossy.blend")

    result = process_file(src, tmp_path / "out", Options(matte=True, matte_min=0.6))
    assert result["error"] is None
    assert any(n.startswith("matte: T_glossy_Roughness") for n in result["notes"])
    [item] = result["exported"]
    [rmap] = [t for t in item["textures"] if t["role"] == "Roughness"]
    img = bpy.data.images.load(rmap["path"])
    img.colorspace_settings.name = "Non-Color"
    px = np.array(img.pixels[:]).reshape(-1, 4)[:, 0]
    assert px.min() >= 0.6 - 0.01
    assert px.max() - px.min() > 0.08                      # the gradient is still there
    assert abs(px.max() - (0.6 + 0.4 * 0.3)) < 0.02
    reimport(item["path"])
    glossy = bpy.data.materials.get("Glossy")
    assert glossy is None or glossy.node_tree.nodes["Principled BSDF"].inputs["Roughness"].default_value >= 0.6


def test_without_matte_roughness_is_untouched(tmp_path):
    import numpy as np
    detailed_prop(tmp_path)
    obj = bpy.context.scene.objects["tripo_node_x"]
    tree = obj.data.materials[0].node_tree
    rough = bpy.data.images.new("rough", 16, 16)
    rough.colorspace_settings.name = "Non-Color"
    rough.pixels = [0.1] * (16 * 16 * 4)
    rough.pack()
    tex = tree.nodes.new("ShaderNodeTexImage")
    tex.image = rough
    tree.links.new(tex.outputs["Color"], tree.nodes["Principled BSDF"].inputs["Roughness"])
    src = save(tmp_path, "plain.blend")
    [item] = process_file(src, tmp_path / "out", Options())["exported"]
    [rmap] = [t for t in item["textures"] if t["role"] == "Roughness"]
    img = bpy.data.images.load(rmap["path"])
    img.colorspace_settings.name = "Non-Color"
    px = np.array(img.pixels[:]).reshape(-1, 4)[:, 0]
    assert 0.05 < px.min() and px.max() < 0.15
