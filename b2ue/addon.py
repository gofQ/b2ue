# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Abdulkerim Tabakoğlu
"""Blender add-on: a "b2ue" tab in the 3D Viewport sidebar (N panel).

The work runs in a separate background Blender process on a saved copy of
the scene (or on the chosen files), so the open file is never modified and
Blender stays responsive while a model is decimated or rigged.
"""

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import fields
from pathlib import Path

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty,
                       IntProperty, PointerProperty, StringProperty)

from mathutils import Matrix, Vector

from . import overlay
from .options import ORIGINS, PRESET_HELP, PRESETS, Options
from .overlay import MARKER_COLLECTION
from .output import STAGING_PREFIX
from .textures import save_pixels

CLI = Path(__file__).with_name("cli.py")
PREFIX = "B2UE "
FILE_TYPES = "*.glb;*.gltf;*.fbx;*.obj;*.blend"

# ---------------------------------------------------------------- job

class Job:
    """One background run of the pipeline and what it has reported so far."""

    def __init__(self, command, out_dir, plan, cleanup_dir=None):
        self.command = command
        self.out_dir = out_dir
        self.plan = list(plan)                 # step names expected for each file
        self.expected_steps = max(len(self.plan), 1)
        self.file_steps = {}                   # step -> (status, detail) for the current file
        self.cleanup_dir = cleanup_dir
        self.status = "running"         # running | done | failed | cancelled
        self.problem = ""               # first failed step, in full
        self.warnings = 0
        self.files_total = 1
        self.file_index = 0
        self.file_name = ""
        self.step = "starting Blender"
        self.steps_done = 0
        self.lines = []                 # what the panel shows
        self.results = []
        self.log = []                   # raw output that is not progress
        self.log_path = None
        self.started = time.time()
        self.finished = None
        self._queue = queue.Queue()
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
            creationflags=flags)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            self._queue.put(line.rstrip("\n"))
        self._queue.put(None)

    @property
    def progress(self):
        total = self.expected_steps * self.files_total
        return min(self.steps_done / total, 1.0) if total else 0.0

    @property
    def elapsed(self):
        """Seconds the job ran; stops counting when it ends."""
        return (self.finished or time.time()) - self.started

    def poll(self):
        """Handle new output. Returns True while the job is still running."""
        while True:
            try:
                line = self._queue.get_nowait()
            except queue.Empty:
                return self.status == "running"
            if line is None:
                self._finish()
                return False
            if line.startswith(PREFIX):
                try:
                    self._event(json.loads(line[len(PREFIX):]))
                    continue
                except ValueError:
                    pass
            self.log.append(line)

    def _event(self, ev):
        kind = ev.get("event")
        if kind == "file":
            self.file_index, self.files_total = ev["index"], ev["total"]
            self.file_name = ev["name"]
            self.steps_done = (self.file_index - 1) * self.expected_steps
            self.file_steps = {}
            self.lines.append(f"[{self.file_index}/{self.files_total}] {self.file_name}")
        elif kind == "begin":
            self.step = ev["step"]
            if ev["step"] not in self.plan:
                self.plan.append(ev["step"])
            self.file_steps[ev["step"]] = ("running", "")
        elif kind == "step":
            self.steps_done += 1
            self.file_steps[ev["step"]] = (ev["status"], ev["detail"])
            mark = {"ok": "", "warn": "! ", "skip": "- ", "fail": "x "}.get(ev["status"], "")
            self.lines.append(f"  {mark}{ev['step']}: {ev['detail']}")
            self.lines += [f"      {extra}" for extra in ev.get("lines", [])]
            if ev["status"] == "fail" and not self.problem:
                where = f"{self.file_name}, " if self.files_total > 1 else ""
                self.problem = f"{where}{ev['step']} step: {_clean_error(ev['detail'])}"
            elif ev["status"] == "warn":
                self.warnings += 1
        elif kind == "log":
            self.log_path = ev.get("path")
            self.lines += [f"      {line}" for line in ev.get("lines", [])[-4:]]
        elif kind == "done":
            self.results = ev.get("results", [])
            self.log_path = ev.get("log")
        elif kind == "message":
            self.lines.append(ev.get("text", ""))

    def _finish(self):
        code = self.proc.wait()
        self.finished = time.time()
        if self.status == "cancelled":
            pass
        elif code == 0 and self.results and not any(r.get("error") for r in self.results):
            self.status = "done"
        else:
            self.status = "failed"
            if not self.results and self.log:
                self.lines += ["Blender stopped unexpectedly:"] + [f"  {l}" for l in self.log[-6:]]
        self.steps_done = self.expected_steps * self.files_total if self.status == "done" else self.steps_done
        # a killed process can leave its staging folder behind; it never
        # contains finished results, so it is safe to remove
        import shutil
        for stale in Path(self.out_dir).glob(f"{STAGING_PREFIX}{self.proc.pid}-*"):
            shutil.rmtree(stale, ignore_errors=True)
        if self.cleanup_dir:
            import shutil
            shutil.rmtree(self.cleanup_dir, ignore_errors=True)

    def cancel(self):
        if self.status == "running":
            self.status = "cancelled"
            self.lines.append("Cancelled.")
            self.proc.terminate()

    @property
    def exported(self):
        return [e["path"] for r in self.results for e in r.get("exported", [])]


JOB = None  # the current or last job


def _clean_error(text):
    """Blender prefixes operator errors with 'Error: '; keep the message."""
    text = (text or "").strip()
    while text.lower().startswith("error:"):
        text = text[6:].strip()
    return text


def _wrapped(layout, context, text, icon="NONE", indent=0):
    """Labels cut long text off; split it over several lines instead."""
    import textwrap
    width = max(int(context.region.width / 7.5) - indent, 20) if context.region else 60
    lines = textwrap.wrap(text, width) or [""]
    col = layout.column(align=True)
    col.scale_y = 0.8
    for i, line in enumerate(lines):
        col.label(text=line, icon=icon if i == 0 else "BLANK1" if icon != "NONE" else "NONE")


def _redraw():
    wm = getattr(bpy.context, "window_manager", None)
    for window in (wm.windows if wm else []):
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _tick():
    if JOB is None:
        return None
    running = JOB.poll()
    _redraw()
    return 0.2 if running else None


# ---------------------------------------------------------------- settings

def _apply_preset(self, _context):
    if self.preset == "CUSTOM":
        return
    values = {f.name: f.default for f in fields(Options)}
    values.update(PRESETS[self.preset])
    self.human = values["human"]
    self.rig = values["rig"] or not values["human"]
    self.keep_face = values["keep_face"] or not values["human"]
    self.target_tris = values["target_tris"]
    self.height = values["height"]
    self.texture_size = str(values["texture_size"])
    self.quality = values["quality"] or "OFF"
    self.bake_normals = values["bake_normals"]


def _mirror_toggled(context):
    """Turning Mirror on makes the markers symmetric right away, copying
    the side of the selected marker (left when none is selected)."""
    scene = context.scene
    if not scene.b2ue.mirror_markers:
        return
    active = context.view_layer.objects.active if context.view_layer else None
    key = overlay.marker_key(active)
    overlay.symmetrize(scene, overlay.side_of(key) if key and overlay.side_of(key) != "c" else "l")


class B2UE_Settings(bpy.types.PropertyGroup):
    name: StringProperty(
        name="Asset name",
        description="Name of the exported files (SM_<name>.fbx / SK_<name>.fbx). Empty: the "
                    "active object's name, or the file name for Process Files")
    preset: EnumProperty(
        name="Preset", default="prop", update=_apply_preset,
        items=[(k, k.title(), PRESET_HELP[k]) for k in PRESETS]
        + [("CUSTOM", "Custom", "Keep the values below")])
    source: EnumProperty(
        name="Export", default="SELECTED",
        items=[("SELECTED", "Selected", "Only the selected objects"),
               ("SCENE", "Scene", "Every object in the scene")])
    human: BoolProperty(name="Humanoid (T/A-pose)",
                        description="One upright human character standing in a T-pose or "
                                    "A-pose. Required for the auto-rig, which only rigs humans")
    rig: BoolProperty(name="Auto-rig", default=True,
                      description="Add a skeleton with Unreal mannequin bone names")
    keep_face: BoolProperty(name="Keep face detail", default=True,
                            description="Leave more triangles on the face when decimating")
    face_detail: FloatProperty(name="Face detail", default=3.0, min=1.0, max=10.0,
                               description="How many times more of its triangles the face keeps")
    target_tris: IntProperty(name="Triangles", default=20000, min=0, step=1000,
                             description="Decimate meshes above this count (0 = keep all)")
    height: FloatProperty(name="Height", default=0.0, min=0.0, unit="LENGTH",
                          description="Scale to this height (0 = keep the size)")
    texture_size: EnumProperty(
        name="Textures", default="2048",
        items=[("0", "Keep", "Keep the original size")]
        + [(str(n), f"{n} px", f"Downscale textures larger than {n} px")
           for n in (512, 1024, 2048, 4096)])
    origin: EnumProperty(name="Pivot", default="bottom",
                         items=[(o, o.title(), "") for o in ORIGINS])
    embed_textures: BoolProperty(name="Embed textures in FBX")
    matte: BoolProperty(
        name="Matte (less shine)",
        description="AI models often look like glossy plastic in strong light. Raises the "
                    "roughness maps so nothing is shinier than Matte amount; scratches and "
                    "worn edges in the map stay")
    matte_min: FloatProperty(
        name="Matte amount", default=0.6, min=0.0, max=1.0,
        description="Lowest roughness after Matte (0 = mirror, 1 = completely matte)")
    quality: EnumProperty(
        name="Quality", default="OFF",
        description="Let b2ue pick the triangle count: the lowest count whose shape error stays "
                    "within the level. Triangles is then the upper limit",
        items=[("OFF", "Fixed count", "Use the Triangles value"),
               ("high", "Auto: high", "Shape error under 0.01% of the size"),
               ("balanced", "Auto: balanced", "Shape error under 0.02% of the size"),
               ("light", "Auto: light", "Shape error under 0.05% of the size")])
    bake_normals: BoolProperty(
        name="Bake lost detail", default=True,
        description="Bake the original high-poly detail into the normal map after decimating. "
                    "Takes about 10 seconds per mesh")
    show_details: BoolProperty(name="Details", description="Show every step's full report")
    mirror_markers: BoolProperty(
        name="Mirror", default=True, update=lambda self, context: _mirror_toggled(context),
        description="Moving a left rig marker moves the right one too (and the other way round); "
                    "neck and pelvis stay on the middle line")
    drop_shape_keys: BoolProperty(
        name="Decimate meshes with shape keys",
        description="Blender cannot decimate a mesh that has shape keys (morphs). Off: such "
                    "meshes keep their shape keys and full detail. On: their shape keys are "
                    "removed so they can be decimated")
    overwrite: BoolProperty(
        name="Overwrite existing",
        description="Replace output files that already exist. Without this, the export stops "
                    "instead of replacing files. Source files are never replaced")
    out_dir: StringProperty(
        name="Output", subtype="DIR_PATH", default="",
        description="Where the FBX and texture files go. Empty: an 'export' folder next to "
                    "the .blend file (or in your home folder if it is not saved)")


def _out_dir(settings, near=None):
    if settings.out_dir:
        return Path(bpy.path.abspath(settings.out_dir))
    if near is not None:
        return Path(near).parent / "export"
    if bpy.data.filepath:
        return Path(bpy.data.filepath).parent / "export"
    return Path.home() / "b2ue_export"


def _option_args(s):
    args = ["--human" if s.human else "--no-human"]
    if s.human:
        args += ["--rig" if s.rig else "--no-rig",
                 "--keep-face" if s.keep_face else "--no-keep-face",
                 "--face-detail", f"{s.face_detail:g}"]
    if s.quality != "OFF":
        args += ["--quality", s.quality]
    args.append("--bake-normals" if s.bake_normals else "--no-bake-normals")
    args += ["--target-tris", str(s.target_tris), "--height", f"{s.height:g}",
             "--texture-size", s.texture_size, "--origin", s.origin]
    if s.embed_textures:
        args.append("--embed-textures")
    if s.matte:
        args += ["--matte", "--matte-min", f"{s.matte_min:g}"]
    if s.overwrite:
        args.append("--overwrite")
    if s.drop_shape_keys:
        args.append("--drop-shape-keys")
    return args


def write_unsaved_images(folder):
    """Save images with unsaved edits (e.g. Texture Paint) to folder.

    A .blend copy only holds the images' file paths, so without this the
    background process would load the old pictures from disk. The user's
    images are not changed. Returns {image name: file}.
    """
    mapping = {}
    for i, image in enumerate(sorted(bpy.data.images, key=lambda im: im.name)):
        if image.type != "IMAGE" or not image.is_dirty or not image.users:
            continue
        Path(folder).mkdir(parents=True, exist_ok=True)
        mapping[image.name] = str(save_pixels(image, Path(folder) / f"{i:03d}_{bpy.path.clean_name(image.name)}"))
    return mapping


STEP_LABELS = {"load": "Load", "cleanup": "Clean up", "humanoid": "Find body",
               "scale": "Scale", "decimate": "Reduce triangles", "rig": "Auto-rig",
               "validate": "Check", "export": "Export", "check": "Check name"}


def _expected_steps(s):
    plan = ["load", "cleanup"]
    if s.human:
        plan.append("humanoid")
    elif s.height:
        plan.append("scale")
    if s.target_tris or s.quality != "OFF":
        plan.append("decimate")
    if s.human and s.rig:
        plan.append("rig")
    return plan + ["validate", "export"]


def blender_command():
    """How to start the background process. Tests replace this."""
    return [bpy.app.binary_path, "-b", "-q", "--factory-startup",
            "--python-exit-code", "1", "-P", str(CLI), "--"]


def start_job(settings, inputs, out_dir, extra=(), cleanup_dir=None):
    global JOB
    command = (blender_command() + ["export", *map(str, inputs), "--out", str(out_dir),
                                    "--json-progress"] + _option_args(settings) + list(extra))
    JOB = Job(command, out_dir, _expected_steps(settings), cleanup_dir)
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=0.2)
    return JOB


def _busy(op):
    if JOB is not None and JOB.status == "running":
        op.report({"WARNING"}, "b2ue is already working. Wait for it or press Cancel.")
        return True
    return False


# ---------------------------------------------------------------- operators

class B2UE_OT_export(bpy.types.Operator):
    """Clean up, decimate, rig and export the selected objects (or the scene) for Unreal.
    Runs in the background on a copy, your scene is not changed"""
    bl_idname = "b2ue.export"
    bl_label = "Export to Unreal"

    def execute(self, context):
        if _busy(self):
            return {"CANCELLED"}
        s = context.scene.b2ue
        extra = []
        if s.source == "SELECTED":
            names = [o.name for o in context.selected_objects if o.type in {"MESH", "ARMATURE"}]
            if not names:
                self.report({"WARNING"}, "Select at least one mesh, or switch Export to Scene.")
                return {"CANCELLED"}
            extra += ["--objects", ",".join(names)]
            active = context.active_object
            name = (active.name if active and active.name in names else names[0])
        else:
            name = Path(bpy.data.filepath).stem if bpy.data.filepath else context.scene.name
        name = s.name.strip() or name
        extra += ["--name", name]

        tmp = Path(tempfile.mkdtemp(prefix="b2ue_"))
        images = write_unsaved_images(tmp / "images")
        if images:
            mapping = tmp / "images.json"
            mapping.write_text(json.dumps(images), encoding="utf-8")
            extra += ["--image-files", str(mapping)]
        copy = tmp / f"{bpy.path.clean_name(name)}.blend"
        bpy.ops.wm.save_as_mainfile(filepath=str(copy), copy=True, check_existing=False,
                                    relative_remap=True)
        start_job(s, [copy], _out_dir(s), extra, cleanup_dir=tmp)
        self.report({"INFO"}, f"b2ue started: {name}")
        return {"FINISHED"}


class B2UE_OT_process_files(bpy.types.Operator):
    """Pick model files (GLB, FBX, OBJ, .blend) and export them for Unreal without
    opening them. Good for files downloaded from Tripo, Meshy and similar tools"""
    bl_idname = "b2ue.process_files"
    bl_label = "Process Files"

    filter_glob: StringProperty(default=FILE_TYPES, options={"HIDDEN"})
    files: CollectionProperty(type=bpy.types.OperatorFileListElement, options={"HIDDEN", "SKIP_SAVE"})
    directory: StringProperty(subtype="DIR_PATH", options={"HIDDEN", "SKIP_SAVE"})

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        if _busy(self):
            return {"CANCELLED"}
        paths = [Path(self.directory) / f.name for f in self.files if f.name]
        paths = [p for p in paths if p.is_file()]
        if not paths:
            self.report({"WARNING"}, "No files selected.")
            return {"CANCELLED"}
        s = context.scene.b2ue
        extra = ["--name", s.name.strip()] if s.name.strip() and len(paths) == 1 else []
        start_job(s, paths, _out_dir(s, near=paths[0]), extra)
        self.report({"INFO"}, f"b2ue started: {len(paths)} file(s)")
        return {"FINISHED"}




def _marker_objects(scene):
    from .humanoid import MARKER_PREFIX
    return [o for o in scene.objects if o.name.startswith(MARKER_PREFIX)]


class B2UE_OT_place_markers(bpy.types.Operator):
    """Put a marker on each joint the auto-rig uses (neck, pelvis, shoulders,
    elbows, wrists, knees, ankles), where b2ue detects them. Move any marker
    that is off; the export then uses the markers instead of its own guess.
    For one humanoid in T-pose or A-pose"""
    bl_idname = "b2ue.place_markers"
    bl_label = "Place Rig Markers"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        from . import humanoid
        meshes = [o for o in context.selected_objects if o.type == "MESH"] or \
                 [o for o in context.scene.objects if o.type == "MESH"
                  and not o.name.startswith(humanoid.MARKER_PREFIX)]
        if not meshes:
            self.report({"WARNING"}, "Select the character's meshes first.")
            return {"CANCELLED"}
        # work on a temporary copy so the character is not touched
        copies = []
        for obj in meshes:
            c = obj.copy()
            c.data = obj.data.copy()
            c.modifiers.clear()
            if c.data.shape_keys is not None:
                c.shape_key_clear()
            context.scene.collection.objects.link(c)
            c.matrix_world = obj.matrix_world.copy()
            copies.append(c)
        temp = humanoid.join_meshes(copies)
        note = ""
        try:
            humanoid.normalize(temp)
            try:
                lm = humanoid.find_landmarks(temp)
            except humanoid.NotHumanoid as exc:
                lm = humanoid.default_landmarks(float(humanoid._coords(temp)[:, 2].max()))
                note = f" Detection failed ({exc}); markers use average proportions, move them."
            to_world = humanoid.normalize_matrix(temp).inverted()
            points = {k: to_world @ v for k, v in humanoid.marker_points(lm).items()}
            H = lm.height
        finally:
            mesh = temp.data
            bpy.data.objects.remove(temp, do_unlink=True)
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)

        for old in _marker_objects(context.scene):
            bpy.data.objects.remove(old, do_unlink=True)
        coll = bpy.data.collections.get(MARKER_COLLECTION) or bpy.data.collections.new(MARKER_COLLECTION)
        if coll.name not in context.scene.collection.children:
            context.scene.collection.children.link(coll)
        rot = to_world.to_3x3().normalized()
        overlay.set_mirror_plane(coll, to_world @ Vector((0, 0, 0)), rot @ Vector((1, 0, 0)))
        coll["b2ue_front"] = [v for row in rot for v in row]
        coll["b2ue_centre"] = list(to_world @ Vector((0, 0, H / 2)))
        coll["b2ue_height"] = H
        for name, pos in points.items():
            empty = bpy.data.objects.new(humanoid.MARKER_PREFIX + name, None)
            empty.empty_display_type = "SPHERE"
            empty.empty_display_size = H * 0.012
            empty.location = pos
            empty.show_in_front = True
            empty.hide_render = True
            empty.lock_rotation = (True, True, True)
            empty.lock_scale = (True, True, True)
            coll.objects.link(empty)
        if context.scene.b2ue.mirror_markers:
            found = overlay.markers_in(context.scene)
            for key in ("shoulder_l", "elbow_l", "wrist_l", "knee_l", "ankle_l", "neck", "pelvis"):
                if key in found:   # average both sides, then mirror
                    obj = found[key]
                    other = found.get(overlay.twin(key))
                    if other is not None:
                        plane = overlay.mirror_plane(context.scene)
                        obj.location = (obj.location + overlay.mirrored(other.location, plane)) / 2
                    context.view_layer.update()
                    overlay.mirror_from(obj, context.scene)
        overlay.front_view(context, rot, to_world @ Vector((0, 0, H / 2)), H)
        self.report({"INFO"}, f"Placed {len(points)} rig markers. Move any that are off." + note)
        return {"FINISHED"}


class B2UE_OT_marker_front_view(bpy.types.Operator):
    """Look at the character from the front (orthographic) to place the
    markers; their depth is found automatically"""
    bl_idname = "b2ue.marker_front_view"
    bl_label = "Front View"

    def execute(self, context):
        coll = bpy.data.collections.get(MARKER_COLLECTION)
        if coll is None or "b2ue_front" not in coll:
            self.report({"WARNING"}, "Place the rig markers first.")
            return {"CANCELLED"}
        f = list(coll["b2ue_front"])
        rot = Matrix([f[0:3], f[3:6], f[6:9]])
        overlay.front_view(context, rot, Vector(coll["b2ue_centre"]), float(coll["b2ue_height"]))
        return {"FINISHED"}


class B2UE_OT_clear_markers(bpy.types.Operator):
    """Remove the rig markers; the export goes back to automatic joint detection"""
    bl_idname = "b2ue.clear_markers"
    bl_label = "Clear Rig Markers"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        removed = _marker_objects(context.scene)
        for obj in removed:
            bpy.data.objects.remove(obj, do_unlink=True)
        coll = bpy.data.collections.get(MARKER_COLLECTION)
        if coll is not None and not coll.objects:
            bpy.data.collections.remove(coll)
        self.report({"INFO"}, f"Removed {len(removed)} rig markers")
        return {"FINISHED"}


class B2UE_OT_cancel(bpy.types.Operator):
    """Stop the running export"""
    bl_idname = "b2ue.cancel"
    bl_label = "Cancel"

    def execute(self, context):
        if JOB is not None:
            JOB.cancel()
        return {"FINISHED"}


class B2UE_OT_open_folder(bpy.types.Operator):
    """Open the output folder"""
    bl_idname = "b2ue.open_folder"
    bl_label = "Open Folder"

    def execute(self, context):
        folder = JOB.out_dir if JOB else _out_dir(context.scene.b2ue)
        if not Path(folder).exists():
            self.report({"WARNING"}, f"{folder} does not exist yet")
            return {"CANCELLED"}
        bpy.ops.wm.path_open(filepath=str(folder))
        return {"FINISHED"}


def _shorten_root(context, arm):
    """Display fix for previews. FBX stores where bones start, not how long
    they are, so Blender's importer makes the root bone reach all the way to
    the pelvis (about 1 m, lying backwards along the floor). Unreal does not
    use bone lengths; this only makes the preview look like the real rig."""
    root = arm.data.bones.get("root")
    if root is None or not root.children:
        return
    height = max((c.head_local - root.head_local).length for c in root.children)
    if root.length <= 0.25 * height:
        return
    previous = context.view_layer.objects.active
    context.view_layer.objects.active = arm
    with context.temp_override(object=arm, active_object=arm):
        bpy.ops.object.mode_set(mode="EDIT")
        bone = arm.data.edit_bones["root"]
        bone.length = 0.15 * height
        bpy.ops.object.mode_set(mode="OBJECT")
    context.view_layer.objects.active = previous


class B2UE_OT_preview(bpy.types.Operator):
    """Import the exported FBX files into a 'b2ue preview' collection to check them"""
    bl_idname = "b2ue.preview"
    bl_label = "Preview Result"

    @classmethod
    def poll(cls, context):
        return JOB is not None and bool(JOB.exported)

    def execute(self, context):
        coll = bpy.data.collections.get("b2ue preview") or bpy.data.collections.new("b2ue preview")
        if coll.name not in context.scene.collection.children:
            context.scene.collection.children.link(coll)
        layer = context.view_layer.layer_collection.children[coll.name]
        previous = context.view_layer.active_layer_collection
        context.view_layer.active_layer_collection = layer
        before = set(bpy.data.objects)
        try:
            for path in JOB.exported:
                bpy.ops.import_scene.fbx(filepath=path)
        finally:
            context.view_layer.active_layer_collection = previous
        for obj in set(bpy.data.objects) - before:
            if obj.type == "ARMATURE":
                _shorten_root(context, obj)
        self.report({"INFO"}, f"Imported {len(JOB.exported)} file(s) into 'b2ue preview'")
        return {"FINISHED"}


class B2UE_OT_clear(bpy.types.Operator):
    """Clear the result list"""
    bl_idname = "b2ue.clear"
    bl_label = "Clear"

    def execute(self, context):
        global JOB
        if JOB is not None and JOB.status != "running":
            JOB = None
        return {"FINISHED"}


# ---------------------------------------------------------------- panel

class B2UE_PT_main(bpy.types.Panel):
    bl_label = "Blender → Unreal"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "b2ue"

    def draw(self, context):
        s = context.scene.b2ue
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        layout.prop(s, "name", placeholder="from the object or file name")
        layout.prop(s, "preset")

        box = layout.box()
        box.prop(s, "human")
        col = box.column()
        col.enabled = s.human
        col.prop(s, "rig")
        col.prop(s, "keep_face")
        sub = col.column()
        sub.enabled = s.keep_face
        sub.prop(s, "face_detail")
        if s.human and s.rig:
            markers = len(_marker_objects(context.scene))
            row = col.row(align=True)
            row.operator("b2ue.place_markers", icon="EMPTY_AXIS",
                         text="Reset Rig Markers" if markers else "Place Rig Markers")
            if markers:
                row.operator("b2ue.marker_front_view", text="", icon="VIEW_ORTHO")
                row.operator("b2ue.clear_markers", text="", icon="X")
                col.prop(s, "mirror_markers", icon="MOD_MIRROR")
                _wrapped(col, context, f"{markers} markers. Drag the coloured dots onto the "
                         "joints in the front view (blue: left, red: right). Depth is found "
                         "automatically.", icon="INFO")
            else:
                _wrapped(col, context, "Optional: place markers to check or correct the "
                         "joints, like Mixamo. T-pose or A-pose humans only.", icon="INFO")

        box = layout.box()
        box.prop(s, "quality")
        box.prop(s, "target_tris", text="Max triangles" if s.quality != "OFF" else "Triangles")
        col = box.column()
        col.enabled = s.target_tris > 0 or s.quality != "OFF"
        col.prop(s, "bake_normals")
        col.prop(s, "drop_shape_keys")

        col = layout.column()
        col.prop(s, "height")
        col.prop(s, "texture_size")
        row = col.row(align=True)
        row.prop(s, "matte")
        sub = row.row(align=True)
        sub.enabled = s.matte
        sub.prop(s, "matte_min", text="")
        col.prop(s, "origin")
        col.prop(s, "embed_textures")
        col.prop(s, "out_dir")
        if not s.out_dir:
            col.label(text=f"→ {_out_dir(s)}", icon="FILE_FOLDER")
        col.prop(s, "overwrite")

        layout.separator()
        busy = JOB is not None and JOB.status == "running"
        row = layout.row(align=True)
        row.use_property_split = False
        row.prop(s, "source", expand=True)
        col = layout.column(align=True)
        col.enabled = not busy
        col.scale_y = 1.4
        col.operator("b2ue.export", icon="EXPORT")
        col.operator("b2ue.process_files", icon="FILEBROWSER")


STEP_ICONS = {"ok": "CHECKMARK", "warn": "ERROR", "fail": "CANCEL", "skip": "REMOVE",
              "running": "PLAY", "pending": "RADIOBUT_OFF"}


class B2UE_PT_status(bpy.types.Panel):
    bl_label = "Progress"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "b2ue"
    bl_parent_id = "B2UE_PT_main"

    @classmethod
    def poll(cls, context):
        return JOB is not None

    def draw(self, context):
        layout = self.layout
        job = JOB

        # 1. one headline that says what is happening
        if job.status == "running":
            files = f"file {job.file_index} of {job.files_total}: " if job.files_total > 1 else ""
            label = STEP_LABELS.get(job.step, job.step)
            text = f"{files}{label}…  {job.elapsed:.0f}s"
            if hasattr(layout, "progress"):
                layout.progress(factor=job.progress, text=text)
            else:
                layout.label(text=f"{job.progress:.0%}  {text}")
        elif job.status == "done":
            icon, text = (("INFO", f"Done with {job.warnings} warning"
                           f"{'s' if job.warnings != 1 else ''}") if job.warnings
                          else ("CHECKMARK", "Done"))
            layout.label(text=f"{text} in {job.elapsed:.0f}s", icon=icon)
        elif job.status == "cancelled":
            layout.label(text="Cancelled. Nothing was written.", icon="CANCEL")
        else:
            box = layout.box()
            box.label(text=f"Stopped after {job.elapsed:.0f}s", icon="ERROR")
            if job.problem:
                _wrapped(box, context, job.problem)
            _wrapped(box, context, "Files that failed were not written to the output folder.")

        # 2. the steps of the current (or last) file as a checklist
        if job.file_name:
            box = layout.box()
            box.label(text=job.file_name, icon="FILE_3D")
            col = box.column(align=True)
            for step in job.plan:
                status, detail = job.file_steps.get(step, ("pending", ""))
                row = col.row(align=True)
                row.label(text=STEP_LABELS.get(step, step), icon=STEP_ICONS.get(status, "DOT"))
                if detail and status in ("warn", "fail"):
                    _wrapped(col, context, detail, indent=4)

        # 3. what was made
        if job.status != "running":
            made = [e for r in job.results for e in r.get("exported", [])]
            if made:
                box = layout.box()
                box.label(text="Created", icon="EXPORT")
                for e in made:
                    tex = len(e.get("textures", []))
                    _wrapped(box, context, f"{Path(e['path']).name}: {e['triangles']:,} tris"
                             + (f" + {tex} texture{'s' if tex != 1 else ''}" if tex else ""))
                for r in job.results:
                    for q in r.get("quality", []) or []:
                        if q.get("quality"):
                            v = q["quality"]
                            _wrapped(box, context, f"{q['mesh']}: shape off by "
                                     f"{v['shape_mean']:.3f}% avg (worst {v['shape_max']:.2f}%), "
                                     f"normals {v['normal_deg']:.1f}°"
                                     + (", detail baked" if q.get("baked") else ""))
            row = layout.row(align=True)
            row.operator("b2ue.open_folder", icon="FILE_FOLDER")
            row.operator("b2ue.preview", icon="HIDE_OFF")
            row.operator("b2ue.clear", text="", icon="X")
            if job.status == "failed" and job.log_path:
                _wrapped(layout, context, f"Log: {job.log_path}", icon="TEXT")
        else:
            layout.operator("b2ue.cancel", icon="CANCEL")

        # 4. the full report, folded away
        s = context.scene.b2ue
        layout.prop(s, "show_details", icon="DISCLOSURE_TRI_DOWN" if s.show_details
                    else "DISCLOSURE_TRI_RIGHT", emboss=False)
        if s.show_details:
            box = layout.box()
            for line in job.lines[-30:]:
                _wrapped(box, context, line)


classes = (B2UE_Settings, B2UE_OT_place_markers, B2UE_OT_marker_front_view, B2UE_OT_clear_markers, B2UE_OT_export, B2UE_OT_process_files, B2UE_OT_cancel,
           B2UE_OT_open_folder, B2UE_OT_preview, B2UE_OT_clear, B2UE_PT_main, B2UE_PT_status)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.b2ue = PointerProperty(type=B2UE_Settings)
    overlay.register()


def unregister():
    global JOB
    if JOB is not None:
        JOB.cancel()
        JOB = None
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    overlay.unregister()
    del bpy.types.Scene.b2ue
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
