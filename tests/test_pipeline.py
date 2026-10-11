# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Abdulkerim Tabakoğlu
"""End-to-end tests. Run with the bpy module installed:  python -m pytest tests"""

import sys
from pathlib import Path

import bpy
import pytest
from mathutils import Euler, Vector

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from b2ue.cli import main  # noqa: E402
from b2ue.options import Options, build  # noqa: E402
from b2ue.pipeline import process_file  # noqa: E402


# ---------- fixture builders ----------

def empty_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    return bpy.context.scene


def link(obj):
    bpy.context.scene.collection.objects.link(obj)
    return obj


def world_dims(objects):
    pts = [o.matrix_world @ v.co for o in objects if o.type == "MESH" for v in o.data.vertices]
    lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    return tuple(hi - lo)


def messy_box(name="Crate"):
    """A 1 m box made of six separate quads (duplicate verts at every corner),
    with inward-facing normals on one face, a loose vertex, and no UVs."""
    s = 0.5
    c = [(-s, -s, -s), (s, -s, -s), (s, s, -s), (-s, s, -s),
         (-s, -s, s), (s, -s, s), (s, s, s), (-s, s, s)]
    quads = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4),
             (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    quads[0] = tuple(reversed(quads[0]))  # flip one face
    verts, faces = [], []
    for q in quads:  # every face gets its own copy of its corners
        faces.append(tuple(range(len(verts), len(verts) + 4)))
        verts.extend(c[i] for i in q)
    verts.append((5, 5, 5))  # loose vertex
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    obj = link(bpy.data.objects.new(name, me))
    obj.location = (3, -2, 1)
    obj.rotation_euler = Euler((0, 0, 0.785398))
    obj.scale = (2, 2, 2)
    bpy.context.view_layer.update()  # refresh matrix_world
    return obj


def rigged_mesh(two_roots=False):
    arm_data = bpy.data.armatures.new("Rig")
    arm = link(bpy.data.objects.new("Rig", arm_data))
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="EDIT")
    root = arm_data.edit_bones.new("root")
    root.head, root.tail = (0, 0, 0), (0, 0, 1)
    spine = arm_data.edit_bones.new("spine")
    spine.head, spine.tail = (0, 0, 1), (0, 0, 2)
    spine.parent = root
    if two_roots:
        extra = arm_data.edit_bones.new("prop_root")
        extra.head, extra.tail = (1, 0, 0), (1, 0, 1)
    bpy.ops.object.mode_set(mode="OBJECT")

    me = bpy.data.meshes.new("Body")
    me.from_pydata([(-.3, -.3, 0), (.3, -.3, 0), (.3, .3, 2), (-.3, .3, 2)], [], [(0, 1, 2, 3)])
    me.uv_layers.new()
    body = link(bpy.data.objects.new("Body", me))
    body.parent = arm
    mod = body.modifiers.new("Armature", "ARMATURE")
    mod.object = arm
    body.vertex_groups.new(name="root").add([0, 1], 1.0, "REPLACE")
    body.vertex_groups.new(name="spine").add([2, 3], 1.0, "REPLACE")
    mat = bpy.data.materials.new("M_Body")
    body.data.materials.append(mat)
    return arm, body


def save(tmp_path, name):
    path = tmp_path / name
    bpy.ops.wm.save_as_mainfile(filepath=str(path))
    return path


def reimport(fbx):
    empty_scene()
    bpy.ops.import_scene.fbx(filepath=str(fbx))
    return list(bpy.context.scene.objects)


# ---------- tests ----------

def test_static_cleanup_and_export(tmp_path):
    empty_scene()
    box = messy_box()
    corners = [box.matrix_world @ v.co for v in box.data.vertices][:-1]  # minus loose vert
    expected = tuple(max(getattr(p, a) for p in corners) - min(getattr(p, a) for p in corners)
                     for a in "xyz")
    src = save(tmp_path, "crate.blend")

    result = process_file(src, tmp_path / "out")

    c = result["cleanup"]
    assert c["transforms_applied"] == 1
    assert c["merged_verts"] == 24 - 8
    assert c["loose_verts_removed"] == 1
    codes = {i["code"] for i in result["issues"]}
    assert "NO_UV" in codes and "NO_MATERIAL" in codes

    [item] = result["exported"]
    assert item["kind"] == "static" and item["path"].endswith("SM_crate.fbx")
    assert item["triangles"] == 12

    objs = reimport(item["path"])
    mesh = next(o for o in objs if o.type == "MESH")
    assert world_dims([mesh]) == pytest.approx(expected, abs=1e-3)
    # default pivot is the bottom centre: it stands on the floor at the origin
    pts = [mesh.matrix_world @ v.co for v in mesh.data.vertices]
    assert min(p.z for p in pts) == pytest.approx(0, abs=1e-4)
    assert (min(p.x for p in pts) + max(p.x for p in pts)) / 2 == pytest.approx(0, abs=1e-4)
    assert len(mesh.data.vertices) == 8

    # every face points away from the box centre after cleanup
    centre = sum((v.co for v in mesh.data.vertices), Vector()) / 8
    for poly in mesh.data.polygons:
        assert poly.normal.dot(poly.center - centre) > 0


def test_skeletal_export_keeps_bones_and_size(tmp_path):
    empty_scene()
    arm, body = rigged_mesh()
    expected = world_dims([body])
    src = save(tmp_path, "hero.blend")

    result = process_file(src, tmp_path / "out")
    assert not [i for i in result["issues"] if i["level"] == "error"]
    [item] = result["exported"]
    assert item["kind"] == "skeletal" and item["path"].endswith("SK_hero.fbx")

    objs = reimport(item["path"])
    rig = next(o for o in objs if o.type == "ARMATURE")
    assert sorted(b.name for b in rig.data.bones) == ["root", "spine"]  # no leaf bones
    mesh = next(o for o in objs if o.type == "MESH")
    assert world_dims([mesh]) == pytest.approx(expected, abs=1e-3)
    assert {g.name for g in mesh.vertex_groups} == {"root", "spine"}


def test_multiple_root_bones_is_an_error_and_strict_skips(tmp_path):
    empty_scene()
    rigged_mesh(two_roots=True)
    src = save(tmp_path, "bad_rig.blend")

    result = process_file(src, tmp_path / "out", Options(strict=True))
    assert "MULTIPLE_ROOT_BONES" in {i["code"] for i in result["issues"]}
    assert result["skipped"] == ["Rig"] and not result["exported"]


def test_glb_from_ai_tool_is_flattened(tmp_path):
    empty_scene()
    holder = link(bpy.data.objects.new("world", None))
    holder.rotation_euler = Euler((1.5708, 0, 0))
    box = messy_box("Automaton")
    box.data.uv_layers.new()
    box.parent = holder
    glb = tmp_path / "automaton.glb"
    bpy.ops.export_scene.gltf(filepath=str(glb), export_format="GLB")

    result = process_file(glb, tmp_path / "out")
    assert result["cleanup"]["unparented"] >= 1
    assert result["cleanup"]["empties_removed"] >= 1
    assert [e["kind"] for e in result["exported"]] == ["static"]
    assert result["exported"][0]["path"].endswith("SM_automaton.fbx")


def test_cli_batch_and_report(tmp_path):
    empty_scene()
    messy_box()
    save(tmp_path, "a.blend")
    empty_scene()
    rigged_mesh()
    save(tmp_path, "b.blend")

    report = tmp_path / "report.json"
    code = main(["export", str(tmp_path), "--out", str(tmp_path / "out"),
                 "--report", str(report), "--max-tris", "10"])
    assert code == 0
    assert report.exists()
    out = sorted(p.name for p in (tmp_path / "out").iterdir())
    assert out == ["SK_b.fbx", "SM_a.fbx"]


def test_decimation_hits_target_and_names_after_file(tmp_path):
    empty_scene()
    bpy.ops.mesh.primitive_uv_sphere_add(segments=128, ring_count=64)  # ~16k tris
    sphere = bpy.context.active_object
    sphere.name = "tripo_node_0123abcd"
    src = save(tmp_path, "Circus Automaton.blend")

    result = process_file(src, tmp_path / "out", Options(target_tris=2000))
    [item] = result["exported"]
    assert item["path"].endswith("SM_Circus_Automaton.fbx")
    assert 1500 <= item["triangles"] <= 2100
    assert "TRI_BUDGET" not in {i["code"] for i in result["issues"]}


def test_decimation_keeps_skin_weights(tmp_path):
    empty_scene()
    arm, body = rigged_mesh()
    sub = body.modifiers.new("sub", "SUBSURF")
    sub.levels = 4
    with bpy.context.temp_override(object=body, active_object=body):
        bpy.ops.object.modifier_apply(modifier="sub")
    src = save(tmp_path, "hero.blend")

    result = process_file(src, tmp_path / "out", Options(target_tris=100))
    objs = reimport(result["exported"][0]["path"])
    mesh = next(o for o in objs if o.type == "MESH")
    assert {g.name for g in mesh.vertex_groups} == {"root", "spine"}
    assert len(mesh.data.polygons) <= 110


def textured_glb(tmp_path):
    """A GLB like the ones Tripo/Meshy produce: textures packed inside."""
    empty_scene()
    bpy.ops.mesh.primitive_cube_add()
    cube = bpy.context.active_object
    cube.name = "tripo_node_abc"
    mat = bpy.data.materials.new("tripo_material")
    mat.use_nodes = True
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    bsdf = nodes["Principled BSDF"]

    def image_node(name, color):
        img = bpy.data.images.new(name, 16, 16)
        img.pixels = list(color) * (16 * 16)
        img.filepath_raw = str(tmp_path / f"{name}.png")
        img.file_format = "PNG"
        img.save()
        node = nodes.new("ShaderNodeTexImage")
        node.image = img
        return node

    links.new(image_node("base", (1, 0, 0, 1)).outputs["Color"], bsdf.inputs["Base Color"])
    nmap = nodes.new("ShaderNodeNormalMap")
    links.new(image_node("normal", (0.5, 0.5, 1, 1)).outputs["Color"], nmap.inputs["Color"])
    links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])
    cube.data.materials.append(mat)
    glb = tmp_path / "Fox Mask.glb"
    bpy.ops.export_scene.gltf(filepath=str(glb), export_format="GLB")
    for name in ("base.png", "normal.png"):
        (tmp_path / name).unlink()  # only the GLB's packed copies remain
    return glb


def test_packed_textures_are_written_and_found_by_fbx(tmp_path):
    glb = textured_glb(tmp_path)
    out = tmp_path / "out"
    result = process_file(glb, out)

    [item] = result["exported"]
    roles = {t["role"]: Path(t["path"]).name for t in item["textures"]}
    assert roles == {"BaseColor": "T_Fox_Mask_BaseColor.png", "Normal": "T_Fox_Mask_Normal.png"}
    assert not item["missing_textures"]
    assert all((out / name).is_file() for name in roles.values())

    # Re-import the FBX somewhere else: its materials must find the textures.
    reimport(item["path"])
    used = [n.image for m in bpy.data.materials if m.use_nodes
            for n in m.node_tree.nodes if n.type == "TEX_IMAGE"]
    assert used and all(img.size[0] > 0 for img in used)
    assert {Path(img.filepath).name for img in used} == set(roles.values())


def test_embedded_textures(tmp_path):
    glb = textured_glb(tmp_path)
    result = process_file(glb, tmp_path / "out", Options(embed_textures=True))
    [item] = result["exported"]
    assert Path(item["path"]).stat().st_size > 1000
    reimport(item["path"])
    used = [n.image for m in bpy.data.materials if m.use_nodes
            for n in m.node_tree.nodes if n.type == "TEX_IMAGE"]
    assert used and all(img.size[0] > 0 for img in used)


def test_texture_downscale(tmp_path):
    glb = textured_glb(tmp_path)
    result = process_file(glb, tmp_path / "out", Options(texture_size=8))
    [item] = result["exported"]
    assert all(t["size"] == [8, 8] for t in item["textures"])
    img = bpy.data.images.load(item["textures"][0]["path"])
    assert tuple(img.size) == (8, 8)


@pytest.mark.parametrize("origin, expected_z", [("center", -0.5), ("bottom", 0.0)])
def test_origin_modes(tmp_path, origin, expected_z):
    empty_scene()
    messy_box()
    src = save(tmp_path, "crate.blend")
    result = process_file(src, tmp_path / "out", Options(origin=origin))
    objs = reimport(result["exported"][0]["path"])
    mesh = next(o for o in objs if o.type == "MESH")
    zs = [(mesh.matrix_world @ v.co).z for v in mesh.data.vertices]
    assert min(zs) == pytest.approx(expected_z * (max(zs) - min(zs)), abs=1e-3)


def test_height_scales_props(tmp_path):
    empty_scene()
    messy_box()
    src = save(tmp_path, "crate.blend")
    result = process_file(src, tmp_path / "out", Options(height=0.5))
    objs = reimport(result["exported"][0]["path"])
    mesh = next(o for o in objs if o.type == "MESH")
    assert world_dims([mesh])[2] == pytest.approx(0.5, abs=1e-3)


def test_presets_and_overrides():
    opts = build("character", target_tris=12000, rig=False)
    assert opts.human and opts.keep_face and not opts.rig
    assert opts.target_tris == 12000 and opts.height == 1.8
    assert build(None, rig=True).human  # --rig implies --human
    with pytest.raises(ValueError):
        build(None, origin="sideways")


class WindowsConsoleLike:
    """Behaves like Python's console stream on Windows: it always writes to
    whatever fd 1 points at, and fails when that is not a terminal (the real
    one calls WriteConsoleW, which raises 'The handle is invalid')."""

    encoding = "utf-8"
    errors = "strict"

    def __init__(self, sink):
        self.sink = sink

    def write(self, text):
        import os
        import stat
        if stat.S_ISREG(os.fstat(1).st_mode):
            raise OSError(9, "The handle is invalid", None, 6, None)
        self.sink.append(text)
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return True

    def fileno(self):
        return 1


def test_quiet_mode_survives_a_windows_console(tmp_path, monkeypatch):
    """Regression: on Windows the FBX exporter's print() crashed the export
    while Blender's output was redirected to the log file."""
    import io
    from b2ue.console import ConsoleReporter

    glb = textured_glb(tmp_path)
    sink = []
    console = WindowsConsoleLike(sink)
    monkeypatch.setattr(sys, "stdout", console)
    monkeypatch.setattr(sys, "stderr", console)
    rep = ConsoleReporter(stream=io.StringIO())
    result = process_file(glb, tmp_path / "out", Options(), rep)
    assert result["error"] is None, result["error"]
    assert result["exported"]


def test_rotation_is_applied_once(tmp_path):
    """Regression: matrix_world was stale after cleanup baked the rotation, so
    the next step applied it a second time (180 deg became 0)."""
    from b2ue import cleanup
    empty_scene()
    box = messy_box()
    box.scale = (1, 1, 1)
    box.data.vertices[0].co.x -= 0.4          # make it asymmetric
    bpy.context.view_layer.update()
    before = [box.matrix_world @ v.co for v in box.data.vertices]
    cleanup.apply_rotation_scale(box)
    after = [box.matrix_world @ v.co for v in box.data.vertices]
    for a, b in zip(before, after):
        assert (a - b).length < 1e-5
