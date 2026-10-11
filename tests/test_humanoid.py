# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Abdulkerim Tabakoğlu
"""Humanoid mode: detection, face protection and auto-rigging."""

import io
import math
import sys
from pathlib import Path

import bpy
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from b2ue import cli, humanoid  # noqa: E402
from b2ue.cleanup import decimate, triangle_count  # noqa: E402
from b2ue.console import ConsoleReporter  # noqa: E402
from b2ue.options import Options, build  # noqa: E402
from b2ue.pipeline import process_file  # noqa: E402
from humanoid_fixture import make_humanoid  # noqa: E402
from test_pipeline import empty_scene, reimport  # noqa: E402

UE_NAMES = {name for name, _, _ in humanoid.BONES}


def humanoid_glb(tmp_path, pose="T", turn=0, name="Witch.glb", arms_down=False):
    empty_scene()
    obj = make_humanoid(pose=pose)
    if arms_down:  # squeeze the arms against the body: not a T/A-pose
        for v in obj.data.vertices:
            if abs(v.co.x) > 0.13:
                v.co.x = math.copysign(0.13 + (abs(v.co.x) - 0.13) * 0.1, v.co.x)
    obj.rotation_euler.z = math.radians(turn)
    path = tmp_path / name
    bpy.ops.export_scene.gltf(filepath=str(path), export_format="GLB")
    return path


@pytest.mark.parametrize("pose, turn", [("T", 0), ("A", 180), ("T", 90)])
def test_character_preset_rigs_and_exports(tmp_path, pose, turn):
    glb = humanoid_glb(tmp_path, pose, turn)
    result = process_file(glb, tmp_path / "out", build("character", target_tris=6000))

    assert result["error"] is None
    [item] = result["exported"]
    assert item["kind"] == "skeletal" and item["path"].endswith("SK_Witch.fbx")
    assert item["triangles"] <= 6000 * 1.03

    objs = reimport(item["path"])
    rig = next(o for o in objs if o.type == "ARMATURE")
    mesh = next(o for o in objs if o.type == "MESH")
    assert {b.name for b in rig.data.bones} == UE_NAMES
    roots = [b for b in rig.data.bones if b.parent is None]
    assert [b.name for b in roots] == ["root"]

    pts = [mesh.matrix_world @ v.co for v in mesh.data.vertices]
    assert max(p.z for p in pts) - min(p.z for p in pts) == pytest.approx(1.8, abs=0.01)
    assert min(p.z for p in pts) == pytest.approx(0, abs=0.01)
    # the character faces -Y: toes are in front of the heels
    feet = [p for p in pts if p.z < 0.05]
    assert sum(p.y for p in feet) / len(feet) < 0

    # every vertex is skinned, and the hands follow the hand bones
    deform = {b.name for b in rig.data.bones if b.use_deform}
    names = {g.index: g.name for g in mesh.vertex_groups}
    for v in mesh.data.vertices:
        assert sum(g.weight for g in v.groups if names[g.group] in deform) > 0.99
    tip = max(mesh.data.vertices, key=lambda v: (mesh.matrix_world @ v.co).x)
    best = max(tip.groups, key=lambda g: g.weight)
    assert names[best.group] in {"hand_l", "lowerarm_l"}


def test_face_keeps_more_detail(tmp_path):
    def densities(keep_face):
        empty_scene()
        obj = make_humanoid(pose="T", subdivide=0)
        remesh = obj.modifiers.new("r", "REMESH")  # even density, like AI meshes
        remesh.mode, remesh.voxel_size = "VOXEL", 0.005
        with bpy.context.temp_override(object=obj, active_object=obj):
            bpy.ops.object.modifier_apply(modifier="r")
        humanoid.scale_to_height([obj], 1.8)
        humanoid.normalize(obj)
        lm = humanoid.find_landmarks(obj)
        humanoid.mark_face(obj, lm)
        decimate(obj, 4000, humanoid.FACE_GROUP if keep_face else None, detail=4)
        from b2ue.cleanup import _region_stats
        _, t_in, t_out, a_in, a_out = _region_stats(obj, humanoid.FACE_GROUP)
        return (t_in / a_in) / (t_out / a_out), triangle_count(obj.data)

    plain, plain_total = densities(False)
    kept, kept_total = densities(True)
    assert kept_total <= 4000 * 1.03
    # the voxel-remeshed body starts with even density, so the face should end
    # up about `detail` times denser, and clearly denser than without protection
    assert kept > 3.5
    assert kept > plain * 1.8


def test_arms_down_is_reported_and_exported_static(tmp_path):
    glb = humanoid_glb(tmp_path, arms_down=True)
    result = process_file(glb, tmp_path / "out", build("character", target_tris=6000))
    assert result["error"] is None
    assert any("T-pose or A-pose" in n for n in result["notes"])
    assert [e["kind"] for e in result["exported"]] == ["static"]


def test_existing_skeleton_is_left_alone(tmp_path):
    glb = humanoid_glb(tmp_path)
    first = process_file(glb, tmp_path / "out1", build("character", target_tris=6000))
    fbx = Path(first["exported"][0]["path"])
    second = process_file(fbx, tmp_path / "out2", build("character", target_tris=6000))
    assert second["error"] is None
    assert [e["kind"] for e in second["exported"]] == ["skeletal"]


def test_console_output_is_readable(tmp_path):
    glb = humanoid_glb(tmp_path)
    buf = io.StringIO()
    code = cli.main([str(glb), "--preset", "character", "--target-tris", "6000"],
                    reporter=ConsoleReporter(stream=buf))
    text = buf.getvalue()
    assert code == 0
    for word in ("[1/1] Witch.glb", "load", "humanoid", "decimate", "rig", "export",
                 "SK_Witch.fbx", "Summary", "Done in"):
        assert word in text
    assert "FBX export starting" not in text  # Blender's own chatter is hidden
    assert (tmp_path / "export" / "SK_Witch.fbx").is_file()  # default output folder


def test_wizard_builds_the_command(monkeypatch, tmp_path):
    glb = humanoid_glb(tmp_path)
    answers = iter([str(glb), "2", "y", "n", "1.65", "", "1024", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    argv = cli.wizard()
    assert argv[:4] == ["export", str(glb), "--preset", "character"]
    assert "--no-keep-face" in argv and "--no-rig" not in argv
    assert argv[argv.index("--height") + 1] == "1.65"
    assert argv[argv.index("--texture-size") + 1] == "1024"
    opts_args = cli.build_parser().parse_args(argv)
    assert opts_args.keep_face is False and opts_args.height == 1.65
