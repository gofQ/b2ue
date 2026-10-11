# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Abdulkerim Tabakoğlu
"""The Blender add-on: registration, settings, background jobs, operators."""

import sys
import time
import tomllib
from pathlib import Path

import bpy
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import b2ue  # noqa: E402
from b2ue import addon  # noqa: E402
from humanoid_fixture import make_humanoid  # noqa: E402
from test_pipeline import empty_scene, messy_box, reimport, textured_glb  # noqa: E402


@pytest.fixture
def registered(monkeypatch):
    """Register the add-on and run jobs with this Python instead of a Blender binary."""
    monkeypatch.setattr(addon, "blender_command", lambda: [sys.executable, str(addon.CLI)])
    empty_scene()
    b2ue.register()
    yield
    b2ue.unregister()


def wait(job, timeout=300):
    end = time.time() + timeout
    while job.poll():
        assert time.time() < end, "job did not finish"
        time.sleep(0.05)
    return job


def test_manifest_is_valid():
    manifest = tomllib.loads((ROOT / "b2ue" / "blender_manifest.toml").read_text(encoding="utf-8"))
    for key in ("schema_version", "id", "version", "name", "tagline", "maintainer",
                "type", "blender_version_min", "license"):
        assert manifest[key], key
    assert manifest["id"] == "b2ue"
    assert manifest["type"] == "add-on"
    assert manifest["version"] == b2ue.__version__
    assert manifest["license"] == ["SPDX:GPL-3.0-or-later"]
    assert len(manifest["tagline"]) <= 64 and not manifest["tagline"].endswith(".")


def test_register_and_presets(registered):
    s = bpy.context.scene.b2ue
    s.preset = "character"
    assert s.human and s.rig and s.keep_face
    assert s.target_tris == 50_000 and s.height == pytest.approx(1.8) and s.texture_size == "2048"
    s.preset = "mobile"
    assert not s.human and s.target_tris == 8_000 and s.texture_size == "1024"
    assert "--no-human" in addon._option_args(s)


def test_export_selected_objects_in_background(registered, tmp_path):
    empty_scene()
    b2ue.unregister()
    b2ue.register()  # settings live on the scene, register again after reset
    box = messy_box("Crate")
    box.data.uv_layers.new()
    other = messy_box("Barrel")
    other.location = (10, 0, 0)
    for o in bpy.context.scene.objects:
        o.select_set(False)
    box.select_set(True)
    bpy.context.view_layer.objects.active = box

    s = bpy.context.scene.b2ue
    s.preset = "prop"
    s.out_dir = str(tmp_path / "out")
    assert bpy.ops.b2ue.export() == {"FINISHED"}
    job = wait(addon.JOB)

    assert job.status == "done", job.lines + job.log
    assert [Path(p).name for p in job.exported] == ["SM_Crate.fbx"]  # only the selection
    assert job.progress == 1.0
    assert any("export" in line for line in job.lines)
    assert box.location.x == pytest.approx(3)  # the open scene was not touched
    assert not Path(job.cleanup_dir).exists()  # the temporary copy is gone

    assert bpy.ops.b2ue.preview() == {"FINISHED"}
    preview = bpy.data.collections["b2ue preview"]
    assert any(o.type == "MESH" for o in preview.objects)


def test_process_files_with_humanoid_settings(registered, tmp_path):
    empty_scene()
    make_humanoid(pose="A")
    glb = tmp_path / "Witch.glb"
    bpy.ops.export_scene.gltf(filepath=str(glb), export_format="GLB")
    empty_scene()
    b2ue.unregister()
    b2ue.register()

    s = bpy.context.scene.b2ue
    s.preset = "character"
    s.target_tris = 6000
    result = bpy.ops.b2ue.process_files("EXEC_DEFAULT", directory=str(tmp_path),
                                        files=[{"name": glb.name}])
    assert result == {"FINISHED"}
    job = wait(addon.JOB)
    assert job.status == "done", job.lines + job.log
    [fbx] = job.exported
    assert Path(fbx) == tmp_path / "export" / "SK_Witch.fbx"  # default: next to the file
    assert any(line.strip().startswith("rig:") for line in job.lines)
    rig = next(o for o in reimport(fbx) if o.type == "ARMATURE")
    assert "upperarm_l" in rig.data.bones


def test_textures_and_failures_are_reported(registered, tmp_path):
    glb = textured_glb(tmp_path)  # resets the scene: read its settings afterwards
    s = bpy.context.scene.b2ue
    broken = tmp_path / "broken.glb"
    broken.write_bytes(b"not a model")
    s.out_dir = str(tmp_path / "out")
    addon.start_job(s, [glb, broken], tmp_path / "out")
    job = wait(addon.JOB)
    assert job.status == "failed"  # one of the two files failed
    assert len(job.exported) == 1
    assert any("FAIL" in l or "x load" in l for l in job.lines)


def test_cancel(registered, tmp_path):
    empty_scene()
    obj = make_humanoid(subdivide=3)
    glb = tmp_path / "Big.glb"
    bpy.ops.export_scene.gltf(filepath=str(glb), export_format="GLB")
    empty_scene()
    b2ue.unregister()
    b2ue.register()
    job = addon.start_job(bpy.context.scene.b2ue, [glb], tmp_path / "out")
    job.cancel()
    wait(job)
    assert job.status == "cancelled"
    assert not job.exported


def test_build_script_makes_an_installable_zip(tmp_path):
    import zipfile
    sys.path.insert(0, str(ROOT / "tools"))
    from build_addon import build
    target = build(tmp_path)
    assert target.name == f"b2ue-{b2ue.__version__}.zip"
    names = zipfile.ZipFile(target).namelist()
    assert "blender_manifest.toml" in names and "__init__.py" in names and "addon.py" in names
    assert not any("__pycache__" in n or n.endswith(".pyc") for n in names)


def test_zip_passes_blenders_extension_validator(tmp_path):
    """The same checks extensions.blender.org runs on upload (field lengths and so on)."""
    sys.path.insert(0, str(ROOT / "tools"))
    from build_addon import build, validate
    ok, message = validate(build(tmp_path))
    assert ok, message
