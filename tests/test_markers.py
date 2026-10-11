# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Abdulkerim Tabakoğlu
"""Rig markers: placed in the user's scene, followed through normalisation."""

import math
import sys
from pathlib import Path

import bpy
import pytest
from mathutils import Matrix, Vector

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import b2ue  # noqa: E402
from b2ue import humanoid  # noqa: E402
from b2ue.options import build  # noqa: E402
from b2ue.pipeline import process_file  # noqa: E402
from humanoid_fixture import make_humanoid  # noqa: E402
from test_pipeline import empty_scene, reimport, save  # noqa: E402


@pytest.fixture
def addon_on():
    empty_scene()
    b2ue.register()
    yield
    b2ue.unregister()


def _character(pose="A"):
    obj = make_humanoid(pose=pose, subdivide=1)
    obj.location = (2, 1, 0.5)              # somewhere else in the scene,
    obj.rotation_euler.z = math.radians(180)  # facing the other way
    bpy.context.view_layer.update()
    for o in bpy.context.scene.objects:
        o.select_set(o is obj)
    bpy.context.view_layer.objects.active = obj
    return obj


def _bones(fbx):
    rig = next(o for o in reimport(fbx) if o.type == "ARMATURE")
    return {b.name: rig.matrix_world @ b.head_local for b in rig.data.bones}


def test_place_and_clear_markers(addon_on):
    _character()
    assert bpy.ops.b2ue.place_markers() == {"FINISHED"}
    names = sorted(o.name for o in bpy.context.scene.objects if o.name.startswith("b2ue_marker_"))
    assert len(names) == len(humanoid.MARKERS)
    # markers sit on the character in its own (moved, turned) place
    elbow = bpy.context.scene.objects["b2ue_marker_elbow_l"].location
    assert 1.5 < elbow.x < 2.6 and elbow.z > 0.9
    assert bpy.ops.b2ue.clear_markers() == {"FINISHED"}
    assert not [o for o in bpy.context.scene.objects if o.name.startswith("b2ue_marker_")]


def test_unmoved_markers_give_the_automatic_rig(addon_on, tmp_path):
    _character()
    src = save(tmp_path, "auto.blend")
    auto = _bones(process_file(src, tmp_path / "a", build("character", target_tris=0))["exported"][0]["path"])

    bpy.ops.wm.open_mainfile(filepath=str(src))
    bpy.ops.b2ue.place_markers()
    src2 = save(tmp_path, "marked.blend")
    marked = _bones(process_file(src2, tmp_path / "m", build("character", target_tris=0))["exported"][0]["path"])
    for name in ("lowerarm_l", "calf_r", "neck_01", "upperarm_r"):
        assert (auto[name] - marked[name]).length < 0.01, name


def test_moved_marker_moves_the_joint(addon_on, tmp_path):
    _character()
    bpy.ops.b2ue.place_markers()
    marker = bpy.context.scene.objects["b2ue_marker_elbow_l"]
    before = marker.location.copy()
    marker.location.z += 0.05                 # 5 cm up in a ~1 m tall scene
    src = save(tmp_path, "moved.blend")
    result = process_file(src, tmp_path / "out", build("character", target_tris=0))
    assert result["error"] is None
    assert any("rig markers" in n for n in result["notes"])
    bones = _bones(result["exported"][0]["path"])
    bpy.ops.wm.open_mainfile(filepath=str(src))   # same scene, marker moved back
    bpy.context.scene.objects["b2ue_marker_elbow_l"].location = before
    plain = _bones(process_file(save(tmp_path, "plain.blend"), tmp_path / "p",
                                build("character", target_tris=0))["exported"][0]["path"])
    dz = bones["lowerarm_l"].z - plain["lowerarm_l"].z
    assert dz == pytest.approx(0.05 * 1.8 / 1.0, rel=0.25)   # scaled to 1.8 m with the body


def test_markers_rescue_a_pose_detection_rejects(addon_on, tmp_path):
    obj = _character()
    for v in obj.data.vertices:                 # squeeze the arms against the body
        if abs(v.co.x) > 0.13:
            v.co.x = math.copysign(0.13 + (abs(v.co.x) - 0.13) * 0.1, v.co.x)
    plain = process_file(save(tmp_path, "down.blend"), tmp_path / "a", build("character", target_tris=0))
    assert [e["kind"] for e in plain["exported"]] == ["static"]   # no rig without markers

    bpy.ops.wm.open_mainfile(filepath=str(tmp_path / "down.blend"))
    bpy.ops.b2ue.place_markers()               # falls back to average proportions
    result = process_file(save(tmp_path, "down_marked.blend"), tmp_path / "b",
                          build("character", target_tris=0))
    assert [e["kind"] for e in result["exported"]] == ["skeletal"]


def test_arms_hanging_down_are_not_an_a_pose():
    empty_scene()
    obj = make_humanoid(pose="A")
    for v in obj.data.vertices:                 # rotate the arms to ~70 deg below horizontal
        if abs(v.co.x) > 0.14 and v.co.z > 0.45:
            side = math.copysign(1, v.co.x)
            dx, dz = abs(v.co.x) - 0.13, v.co.z - 0.76
            r = math.hypot(dx, dz)
            a = math.radians(70)
            v.co.x = side * (0.13 + r * math.cos(a) * 0.9)
            v.co.z = 0.76 - r * math.sin(a)
    humanoid.scale_to_height([obj], 1.8)
    humanoid.normalize(obj)
    with pytest.raises(humanoid.NotHumanoid):
        humanoid.find_landmarks(obj)


def test_mirror_moves_the_twin_marker(addon_on):
    from b2ue import overlay
    _character()
    bpy.ops.b2ue.place_markers()
    found = overlay.markers_in(bpy.context.scene)
    left, right = found["elbow_l"], found["elbow_r"]
    plane = overlay.mirror_plane(bpy.context.scene)
    assert (overlay.mirrored(left.location, plane) - right.location).length < 1e-4  # symmetric
    left.location.z += 0.07
    bpy.context.view_layer.update()
    overlay.mirror_from(left, bpy.context.scene)
    assert (overlay.mirrored(left.location, plane) - right.location).length < 1e-4
    neck = found["neck"]
    neck.location += plane[1] * 0.05          # pushed off the middle line
    bpy.context.view_layer.update()
    overlay.mirror_from(neck, bpy.context.scene)
    assert abs((neck.location - plane[0]).dot(plane[1])) < 1e-4


def test_marker_depth_does_not_matter(addon_on, tmp_path):
    """Markers are placed in the front view; how far in front of or behind
    the body they end up must not change the rig."""
    _character()
    bpy.ops.b2ue.place_markers()
    src = save(tmp_path, "flat.blend")
    flat = _bones(process_file(src, tmp_path / "f", build("character", target_tris=0))["exported"][0]["path"])
    bpy.ops.wm.open_mainfile(filepath=str(src))
    for obj in bpy.context.scene.objects:
        if obj.name.startswith("b2ue_marker_"):
            obj.location.y += 0.3              # far off the body (character faces +Y here)
    deep = _bones(process_file(save(tmp_path, "deep.blend"), tmp_path / "d",
                               build("character", target_tris=0))["exported"][0]["path"])
    for name in ("lowerarm_l", "hand_r", "calf_l", "neck_01"):
        assert (flat[name] - deep[name]).length < 0.005, name


def test_hand_bone_ends_at_the_fingertips(tmp_path):
    empty_scene()
    obj = make_humanoid(pose="A", subdivide=1)
    humanoid.normalize(obj)
    lm = humanoid.find_landmarks(obj)
    co = humanoid._coords(obj)
    for s, sign in (("l", 1), ("r", -1)):
        wrist, tail = lm.joints[f"hand_{s}"]
        reach = (co[:, 0] * sign).max()
        assert abs(tail.x * sign - reach) < 0.02 * lm.height
    # moving the wrist marker keeps the hand inside the fingertips
    markers = humanoid.marker_points(lm)
    markers["wrist_l"] = markers["wrist_l"] + Vector((-0.02, 0, 0.02))
    humanoid.apply_markers(lm, markers, co)
    assert lm.joints["hand_l"][1].x <= (co[:, 0]).max() + 0.01 * lm.height


def test_arm_joints_lie_inside_the_arm(tmp_path):
    """Shoulder, elbow and wrist must sit on the arm's centre line, not on
    the chest or beside the arm (the fixture's arm axis is known)."""
    for pose, angle in (("A", 45), ("T", 0)):
        empty_scene()
        obj = make_humanoid(pose=pose, subdivide=1)
        humanoid.normalize(obj)
        lm = humanoid.find_landmarks(obj)
        z0 = float(humanoid._coords(obj)[:, 2].min())  # normalize moved the feet to 0
        for s, sign in (("l", 1), ("r", -1)):
            start = Vector((0.13 * sign, 0, 0.76 + z0 - z0))
            axis = Vector((sign * math.cos(math.radians(angle)), 0, -math.sin(math.radians(angle))))
            for bone in (f"upperarm_{s}", f"lowerarm_{s}", f"hand_{s}"):
                p = lm.joints[bone][0]
                rel = p - start
                off = (rel - axis * rel.dot(axis)).length
                assert off < 0.03, (pose, bone, tuple(p), off)


def test_mirror_follows_live_edits(addon_on):
    """Moving the active marker in the viewport updates its twin by itself."""
    from b2ue import overlay
    _character()
    bpy.ops.b2ue.place_markers()
    found = overlay.markers_in(bpy.context.scene)
    left, right = found["wrist_l"], found["wrist_r"]
    bpy.context.view_layer.objects.active = left
    left.location.z += 0.1
    bpy.context.view_layer.update()            # fires the depsgraph handler
    plane = overlay.mirror_plane(bpy.context.scene)
    assert (overlay.mirrored(left.location, plane) - right.location).length < 1e-4
    bpy.context.scene.b2ue.mirror_markers = False
    left.location.z += 0.1
    bpy.context.view_layer.update()
    assert (overlay.mirrored(left.location, plane) - right.location).length > 0.05


def test_mirror_survives_opening_a_file(addon_on, tmp_path):
    """Blender drops app handlers on File > Open unless they are persistent;
    the mirror then silently stopped working."""
    from b2ue import overlay
    _character()
    bpy.ops.b2ue.place_markers()
    src = save(tmp_path, "markers.blend")
    bpy.ops.wm.open_mainfile(filepath=str(src))
    assert overlay._on_depsgraph in bpy.app.handlers.depsgraph_update_post
    found = overlay.markers_in(bpy.context.scene)
    right, left = found["shoulder_r"], found["shoulder_l"]     # move the RIGHT one
    bpy.context.view_layer.objects.active = right
    right.location.z += 0.06
    right.location += overlay.mirror_plane(bpy.context.scene)[1] * 0.03
    bpy.context.view_layer.update()
    plane = overlay.mirror_plane(bpy.context.scene)
    assert (overlay.mirrored(right.location, plane) - left.location).length < 1e-4


def test_mirror_works_for_markers_without_a_stored_plane(addon_on):
    """Markers placed by an older version have no stored symmetry plane."""
    from b2ue import overlay
    _character()
    bpy.ops.b2ue.place_markers()
    stored = overlay.mirror_plane(bpy.context.scene)
    del bpy.data.collections[overlay.MARKER_COLLECTION][overlay.MIRROR_KEY]
    guessed = overlay.mirror_plane(bpy.context.scene)
    assert abs(guessed[1].dot(stored[1])) > 0.999
    assert abs((guessed[0] - stored[0]).dot(stored[1])) < 1e-3


def test_turning_mirror_on_makes_markers_symmetric(addon_on):
    from b2ue import overlay
    _character()
    bpy.ops.b2ue.place_markers()
    s = bpy.context.scene.b2ue
    s.mirror_markers = False
    found = overlay.markers_in(bpy.context.scene)
    found["elbow_r"].location.z += 0.1
    found["knee_l"].location.z -= 0.05
    bpy.context.view_layer.update()
    bpy.context.view_layer.objects.active = found["elbow_r"]
    s.mirror_markers = True                      # the right side leads
    plane = overlay.mirror_plane(bpy.context.scene)
    for key in ("elbow", "knee", "wrist", "shoulder", "ankle"):
        l, r = found[f"{key}_l"].location, found[f"{key}_r"].location
        assert (overlay.mirrored(r, plane) - l).length < 1e-4, key


def test_preview_root_bone_is_short(tmp_path):
    """Blender's FBX importer stretches the root bone to the pelvis (about 1 m,
    lying backwards); the preview shortens it for display."""
    from b2ue import addon
    empty_scene()
    make_humanoid(pose="A", subdivide=1)
    src = save(tmp_path, "h.blend")
    fbx = process_file(src, tmp_path / "o", build("character", target_tris=0))["exported"][0]["path"]
    rig = next(o for o in reimport(fbx) if o.type == "ARMATURE")
    assert rig.data.bones["root"].length > 0.5            # what the importer does
    addon._shorten_root(bpy.context, rig)
    assert rig.data.bones["root"].length < 0.3
    assert rig.data.bones["root"].head_local.length < 1e-4  # still at the origin


def test_t_pose_shoulder_is_not_inside_the_torso(tmp_path):
    """A sleeve that widens gradually towards the shoulder (jackets, padded
    shoulders) never triggers the 'arm joins the torso' checks; the walk went
    on into the chest and put the shoulder joint next to the neck."""
    import bmesh
    from humanoid_fixture import _segment
    empty_scene()
    obj = make_humanoid(pose="T", subdivide=1)
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    for side in (1, -1):                       # a cone-shaped sleeve
        res = bmesh.ops.create_cone(bm, cap_ends=True, segments=24, radius1=0.07,
                                    radius2=0.035, depth=0.2)
        bmesh.ops.rotate(bm, verts=res["verts"], cent=(0, 0, 0),
                         matrix=Matrix.Rotation(math.radians(90 * side), 3, "Y"))
        bmesh.ops.translate(bm, verts=res["verts"], vec=(side * 0.2, 0, 0.76))
    bm.to_mesh(obj.data)
    bm.free()
    humanoid.normalize(obj)
    lm = humanoid.find_landmarks(obj)
    torso = 0.11                                # the fixture's torso radius
    for s in ("l", "r"):
        assert abs(lm.joints[f"upperarm_{s}"][0].x) >= torso * 0.95, lm.joints[f"upperarm_{s}"][0]
