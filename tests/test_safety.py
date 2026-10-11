# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Abdulkerim Tabakoğlu
"""Output safety and unsaved texture edits."""

import io
import sys
import time
from pathlib import Path

import bpy
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import b2ue  # noqa: E402
from b2ue import addon, cli, export, textures  # noqa: E402
from b2ue.console import ConsoleReporter  # noqa: E402
from b2ue.options import Options  # noqa: E402
from b2ue.output import STAGING_PREFIX, Batch  # noqa: E402
from b2ue.pipeline import process_file  # noqa: E402
from test_pipeline import empty_scene, messy_box, save  # noqa: E402


def crate_glb(path, size=1.0):
    empty_scene()
    box = messy_box("Crate")
    box.scale = (size, size, size)
    box.data.uv_layers.new()
    path.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.export_scene.gltf(filepath=str(path), export_format="GLB")
    return path


def run_cli(*argv):
    buf = io.StringIO()
    code = cli.main([str(a) for a in argv], reporter=ConsoleReporter(stream=buf))
    return code, buf.getvalue()


def no_staging_left(folder):
    return not list(Path(folder).glob(STAGING_PREFIX + "*"))


# ---------------------------------------------------------------- names and overwrite

def test_same_file_name_in_one_batch_is_refused(tmp_path):
    a = crate_glb(tmp_path / "shop" / "crate.glb")
    b = crate_glb(tmp_path / "attic" / "crate.glb", size=3)
    out = tmp_path / "out"
    code, text = run_cli("export", a, b, "--out", out)
    assert code == 1
    assert sorted(p.name for p in out.iterdir()) == ["SM_crate.fbx"]
    assert "same file name" in text
    assert no_staging_left(out)


def test_existing_output_is_not_overwritten_without_permission(tmp_path):
    glb = crate_glb(tmp_path / "crate.glb")
    out = tmp_path / "out"
    out.mkdir()
    (out / "SM_crate.fbx").write_bytes(b"my older export")

    result = process_file(glb, out, Options())
    assert "already exists" in result["error"]
    assert (out / "SM_crate.fbx").read_bytes() == b"my older export"
    assert no_staging_left(out)

    result = process_file(glb, out, Options(overwrite=True))
    assert result["error"] is None
    assert (out / "SM_crate.fbx").read_bytes() != b"my older export"


def test_source_file_is_never_overwritten(tmp_path):
    out = tmp_path / "out"
    first = process_file(crate_glb(tmp_path / "crate.glb"), out, Options())
    source = Path(first["exported"][0]["path"])        # out/SM_crate.fbx
    before = source.read_bytes()

    # re-exporting that FBX into the same folder would write SM_crate.fbx again
    result = process_file(source, out, Options(overwrite=True))
    assert "overwrite a source file" in result["error"]
    assert source.read_bytes() == before


def test_two_sources_cannot_claim_the_same_output(tmp_path):
    out = tmp_path / "out"
    a = crate_glb(tmp_path / "a.glb")
    b = crate_glb(tmp_path / "b.glb")
    batch = Batch.for_files([a, b], overwrite=True)
    assert process_file(a, out, Options(name="Crate"), batch=batch)["error"] is None
    result = process_file(b, out, Options(name="Crate"), batch=batch)
    assert "already written in this run" in result["error"]


def test_clashing_object_names_get_stable_unique_files(tmp_path):
    empty_scene()
    for name, x in (("Door.A", 0), ("Door A", 3)):
        box = messy_box(name)
        box.location.x = x
        box.data.uv_layers.new()
    src = save(tmp_path, "doors.blend")

    names = []
    for run in ("run1", "run2"):
        result = process_file(src, tmp_path / run, Options())
        assert result["error"] is None
        names.append(sorted(Path(e["path"]).name for e in result["exported"]))
    assert len(set(names[0])) == 2            # no file overwrote the other
    assert names[0] == names[1]               # same input, same names
    assert all(n.startswith("SM_Door_A_") for n in names[0])


def test_failed_export_leaves_no_partial_files(tmp_path, monkeypatch):
    glb = crate_glb(tmp_path / "crate.glb")
    out = tmp_path / "out"
    out.mkdir()
    (out / "SM_crate.fbx").write_bytes(b"good old file")

    def broken_export(asset, folder, **kwargs):
        (Path(folder) / "SM_crate.fbx").write_bytes(b"half written")
        (Path(folder) / "T_crate_BaseColor.png").write_bytes(b"half written")
        raise RuntimeError("disk full")

    monkeypatch.setattr(export, "export_asset", broken_export)
    result = process_file(glb, out, Options(overwrite=True))
    assert "disk full" in result["error"]
    assert sorted(p.name for p in out.iterdir()) == ["SM_crate.fbx"]
    assert (out / "SM_crate.fbx").read_bytes() == b"good old file"
    assert no_staging_left(out)


# ---------------------------------------------------------------- textures

def _pixels(path):
    img = bpy.data.images.load(str(path), check_existing=False)
    px = np.empty(img.size[0] * img.size[1] * 4, dtype=np.float32)
    img.pixels.foreach_get(px)
    bpy.data.images.remove(img)
    return px.reshape(-1, 4)[0]


def _image(tmp_path, name, colour, mode):
    """mode: disk, packed or generated."""
    img = bpy.data.images.new(name, 8, 8)
    img.pixels = list(colour) * 64
    if mode == "generated":
        return img
    img.filepath_raw = str(tmp_path / f"{name}_src.png")
    img.file_format = "PNG"
    img.save()
    img.reload()
    if mode == "packed":
        img.pack()
    return img


@pytest.mark.parametrize("mode", ["disk", "packed"])
def test_unsaved_edits_win_over_old_bytes(tmp_path, mode):
    empty_scene()
    img = _image(tmp_path, "paint", (0, 0, 0, 1), mode)
    img.pixels = [1, 0, 0, 1] * 64                      # painted, not saved
    assert img.is_dirty
    out = tmp_path / "out"
    out.mkdir()
    path = textures._write_image(img, out, "T_x_BaseColor")
    assert _pixels(path)[0] == pytest.approx(1.0)        # the edit is exported
    assert img.is_dirty                                  # the user's image is untouched
    if mode == "disk":
        assert _pixels(tmp_path / "paint_src.png")[0] == pytest.approx(0.0)  # source untouched


@pytest.mark.parametrize("mode", ["disk", "packed"])
def test_untouched_images_are_copied_byte_for_byte(tmp_path, mode):
    empty_scene()
    img = _image(tmp_path, "clean", (0.2, 0.4, 0.6, 1), mode)
    original = (img.packed_file.data if mode == "packed"
                else (tmp_path / "clean_src.png").read_bytes())
    out = tmp_path / "out"
    out.mkdir()
    path = textures._write_image(img, out, "T_x_BaseColor")
    assert path.read_bytes() == original


def test_generated_image_is_written(tmp_path):
    empty_scene()
    img = _image(tmp_path, "gen", (0, 1, 0, 1), "generated")
    out = tmp_path / "out"
    out.mkdir()
    path = textures._write_image(img, out, "T_x_Mask")
    assert path.suffix == ".png" and _pixels(path)[1] == pytest.approx(1.0)


def test_colour_space_is_kept_for_edited_data_maps(tmp_path):
    empty_scene()
    img = _image(tmp_path, "rough", (0.5, 0.5, 0.5, 1), "disk")
    img.colorspace_settings.name = "Non-Color"
    img.pixels = [0.25, 0.25, 0.25, 1] * 64
    out = tmp_path / "out"
    out.mkdir()
    path = textures._write_image(img, out, "T_x_Roughness")
    assert _pixels(path)[0] == pytest.approx(0.25, abs=0.01)  # values, not re-encoded colours


# ---------------------------------------------------------------- add-on

@pytest.fixture
def registered(monkeypatch):
    monkeypatch.setattr(addon, "blender_command", lambda: [sys.executable, str(addon.CLI)])
    empty_scene()
    b2ue.register()
    yield
    b2ue.unregister()


def _wait(job, timeout=300):
    end = time.time() + timeout
    while job.poll():
        assert time.time() < end
        time.sleep(0.05)
    return job


def test_addon_exports_unsaved_texture_paint(registered, tmp_path):
    box = messy_box("Statue")
    box.data.uv_layers.new()
    img = _image(tmp_path, "statue_paint", (0, 0, 0, 1), "disk")
    mat = bpy.data.materials.new("M_Statue")
    mat.use_nodes = True
    node = mat.node_tree.nodes.new("ShaderNodeTexImage")
    node.image = img
    mat.node_tree.links.new(node.outputs["Color"],
                            mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"])
    box.data.materials.append(mat)
    img.pixels = [0, 0, 1, 1] * 64                         # painted in this session
    for o in bpy.context.scene.objects:
        o.select_set(o is box)
    bpy.context.view_layer.objects.active = box

    s = bpy.context.scene.b2ue
    s.out_dir = str(tmp_path / "out")
    s.texture_size = "0"
    assert bpy.ops.b2ue.export() == {"FINISHED"}
    job = _wait(addon.JOB)
    assert job.status == "done", job.lines + job.log
    [tex] = [t for r in job.results for e in r["exported"] for t in e["textures"]]
    assert _pixels(tex["path"])[2] == pytest.approx(1.0)   # the blue paint reached the export
    assert img.is_dirty                                    # and is still unsaved in the session
    assert Path(img.filepath).name == "statue_paint_src.png"


def test_addon_refuses_to_overwrite_and_says_why(registered, tmp_path):
    glb = crate_glb(tmp_path / "crate.glb")
    out = tmp_path / "out"
    out.mkdir()
    (out / "SM_crate.fbx").write_bytes(b"keep me")
    s = bpy.context.scene.b2ue
    job = _wait(addon.start_job(s, [glb], out))
    assert job.status == "failed"
    assert (out / "SM_crate.fbx").read_bytes() == b"keep me"
    assert any("already exists" in line for line in job.lines + [r["error"] or "" for r in job.results])
    assert no_staging_left(out)


# ---------------------------------------------------------------- shape keys

def _keyed_humanoid(tmp_path, name="body.blend"):
    from humanoid_fixture import make_humanoid
    empty_scene()
    obj = make_humanoid(pose="T")
    obj.shape_key_add(name="Basis")
    smile = obj.shape_key_add(name="Smile")
    for i, v in enumerate(obj.data.vertices):
        if v.co.z > 0.88:
            smile.data[i].co.y -= 0.01
    obj.rotation_euler.z = 3.14159          # unapplied rotation, baked by cleanup
    return save(tmp_path, name)


def _shape_keys(fbx):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.fbx(filepath=str(fbx))
    mesh = next(o for o in bpy.context.scene.objects if o.type == "MESH")
    return mesh.data.shape_keys.key_blocks if mesh.data.shape_keys else None


def test_meshes_with_shape_keys_are_kept_not_failed(tmp_path):
    """Regression: a character with morphs made the whole export fail
    ('Modifier cannot be applied to a mesh with shape keys')."""
    from b2ue.options import build
    src = _keyed_humanoid(tmp_path)
    result = process_file(src, tmp_path / "out", build("character", target_tris=6000))
    assert result["error"] is None
    assert any("shape keys" in n for n in result["notes"])
    [item] = result["exported"]
    assert item["kind"] == "skeletal" and item["triangles"] > 6000   # kept, not decimated
    keys = _shape_keys(item["path"])
    assert [k.name for k in keys] == ["Basis", "Smile"]
    # the morph moved with the mesh when it was scaled to 1.8 m and turned around
    offset = max((k.co - b.co).length for k, b in zip(keys["Smile"].data, keys[0].data))
    assert offset == pytest.approx(0.01 * 1.8, rel=0.05)


def test_drop_shape_keys_allows_decimation(tmp_path):
    from b2ue.options import build
    src = _keyed_humanoid(tmp_path)
    result = process_file(src, tmp_path / "out",
                          build("character", target_tris=6000, drop_shape_keys=True))
    assert result["error"] is None
    [item] = result["exported"]
    assert item["triangles"] <= 6000 * 1.03
    assert _shape_keys(item["path"]) is None


def test_transforms_carry_shape_keys(tmp_path):
    from b2ue import cleanup
    empty_scene()
    bpy.ops.mesh.primitive_cube_add()
    cube = bpy.context.active_object
    cube.shape_key_add(name="Basis")
    up = cube.shape_key_add(name="Up")
    up.data[0].co.z += 1
    cube.scale = (2, 2, 2)
    bpy.context.view_layer.update()
    assert cleanup.apply_rotation_scale(cube)
    keys = cube.data.shape_keys.key_blocks
    assert (keys["Up"].data[0].co - keys["Basis"].data[0].co).z == pytest.approx(2.0)


# ---------------------------------------------------------------- add-on status

def test_failed_job_explains_the_problem_in_full(registered, tmp_path):
    broken = tmp_path / "broken.glb"
    broken.write_bytes(b"not a model")
    job = _wait(addon.start_job(bpy.context.scene.b2ue, [broken], tmp_path / "out"))
    assert job.status == "failed"
    assert job.problem.startswith("load step:")
    assert not job.problem.lower().startswith("load step: error:")


def test_status_panel_draws_in_every_state(registered, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    glb = crate_glb(tmp_path / "crate.glb")
    job = addon.start_job(bpy.context.scene.b2ue, [glb], tmp_path / "out")
    context = SimpleNamespace(region=SimpleNamespace(width=300), scene=bpy.context.scene)
    for state in ("running", "done", "failed", "cancelled"):
        job.status = state
        job.problem = "decimate step: Modifier cannot be applied to a mesh with shape keys"
        for panel in (addon.B2UE_PT_main, addon.B2UE_PT_status):
            panel.draw(SimpleNamespace(layout=MagicMock()), context)
    job.status = "running"
    _wait(job)
    labels = []
    layout = MagicMock()
    layout.column.return_value.label.side_effect = lambda text, **kw: labels.append(text)
    addon._wrapped(layout, context, "x" * 120)
    assert len(labels) >= 3 and all(len(t) <= 40 for t in labels)


def test_elapsed_time_stops_when_the_job_ends(registered, tmp_path):
    """Regression: the panel kept counting after a failed job ('Failed in 328s')."""
    broken = tmp_path / "broken.glb"
    broken.write_bytes(b"not a model")
    job = _wait(addon.start_job(bpy.context.scene.b2ue, [broken], tmp_path / "out"))
    first = job.elapsed
    time.sleep(0.3)
    assert job.elapsed == first
