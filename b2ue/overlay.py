# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Abdulkerim Tabakoğlu
"""Rig marker helpers for the Blender UI, in the spirit of Mixamo's and
Auto-Rig Pro's joint placement:

  - a viewport overlay: a coloured dot on every marker (left blue, right red,
    centre yellow), a label, and lines between them that preview the skeleton
  - symmetry: moving a left marker moves its right twin (and the other way
    round); centre markers stay on the middle line
  - a front view of the character to place the markers in

Markers are ordinary empties, so they are moved with the usual tools (click,
G). Their depth does not matter: the pipeline moves every marker to the
middle of the body (humanoid.snap_depth).
"""

import bpy
from bpy.app.handlers import persistent
from mathutils import Matrix, Vector

from .humanoid import MARKER_PREFIX, MARKERS

MARKER_COLLECTION = "b2ue rig markers"
MIRROR_KEY = "b2ue_mirror"          # collection property: plane origin + normal
FRONT_KEY = "b2ue_front"            # collection property: char -> world rotation (3x3)

LABELS = {
    "neck": "Neck", "pelvis": "Pelvis",
    "shoulder_l": "Shoulder L", "shoulder_r": "Shoulder R",
    "elbow_l": "Elbow L", "elbow_r": "Elbow R",
    "wrist_l": "Wrist L", "wrist_r": "Wrist R",
    "knee_l": "Knee L", "knee_r": "Knee R",
    "ankle_l": "Ankle L", "ankle_r": "Ankle R",
}
# skeleton preview: marker pairs joined by a line
LINKS = [("neck", "pelvis"),
         ("neck", "shoulder_l"), ("shoulder_l", "elbow_l"), ("elbow_l", "wrist_l"),
         ("neck", "shoulder_r"), ("shoulder_r", "elbow_r"), ("elbow_r", "wrist_r"),
         ("pelvis", "knee_l"), ("knee_l", "ankle_l"),
         ("pelvis", "knee_r"), ("knee_r", "ankle_r")]
COLOURS = {"l": (0.25, 0.62, 1.0, 1.0), "r": (1.0, 0.35, 0.35, 1.0), "c": (1.0, 0.82, 0.2, 1.0)}

_handles = []
_busy = False


def side_of(key):
    return key[-1] if key.endswith(("_l", "_r")) else "c"


def twin(key):
    if key.endswith("_l"):
        return key[:-2] + "_r"
    if key.endswith("_r"):
        return key[:-2] + "_l"
    return None


def marker_key(obj):
    if obj is None or not obj.name.startswith(MARKER_PREFIX):
        return None
    key = obj.name[len(MARKER_PREFIX):].split(".")[0]
    return key if key in MARKERS else None


def markers_in(scene):
    out = {}
    for obj in scene.objects:
        key = marker_key(obj)
        if key:
            out[key] = obj
    return out


def mirror_plane(scene):
    """(origin, normal) of the character's symmetry plane, or None.

    Stored when the markers are placed. Markers from older versions have no
    stored plane: it is then worked out from the markers themselves (the
    middle markers lie on it, left-right pairs straddle it)."""
    coll = bpy.data.collections.get(MARKER_COLLECTION)
    if coll is not None and MIRROR_KEY in coll:
        v = list(coll[MIRROR_KEY])
        return Vector(v[:3]), Vector(v[3:6]).normalized()
    found = markers_in(scene)
    across = Vector((0, 0, 0))
    mids = []
    for key, obj in found.items():
        if key.endswith("_l") and twin(key) in found:
            left, right = obj.matrix_world.translation, found[twin(key)].matrix_world.translation
            across += left - right
            mids.append((left + right) / 2)
    mids += [found[k].matrix_world.translation for k in ("neck", "pelvis") if k in found]
    if across.length < 1e-6 or not mids:
        return None
    origin = sum(mids, Vector((0, 0, 0))) / len(mids)
    return origin, across.normalized()


def set_mirror_plane(coll, origin, normal):
    coll[MIRROR_KEY] = list(origin) + list(normal)


def mirrored(point, plane):
    origin, n = plane
    return point - 2 * (point - origin).dot(n) * n


def on_plane(point, plane):
    origin, n = plane
    return point - (point - origin).dot(n) * n


def mirror_from(obj, scene):
    """Make obj's twin (or obj itself, for centre markers) symmetric."""
    global _busy
    key = marker_key(obj)
    plane = mirror_plane(scene)
    if key is None or plane is None:
        return False
    _busy = True
    try:
        if side_of(key) == "c":
            target = on_plane(obj.matrix_world.translation, plane)
            if (target - obj.matrix_world.translation).length > 1e-6:
                obj.location = target
                return True
            return False
        other = markers_in(scene).get(twin(key))
        if other is None:
            return False
        target = mirrored(obj.matrix_world.translation, plane)
        if (other.matrix_world.translation - target).length > 1e-6:
            other.location = target
            return True
        return False
    finally:
        _busy = False


@persistent
def _on_depsgraph(scene, depsgraph):
    """Mirror the markers the user is moving onto their twins.

    persistent: Blender drops ordinary app handlers whenever a .blend file
    is opened; without this the mirror stopped working after File > Open."""
    if _busy or not getattr(scene, "b2ue", None) or not scene.b2ue.mirror_markers:
        return
    moved = set()
    for update in depsgraph.updates:
        if update.is_updated_transform:
            obj = getattr(update.id, "original", None)
            if isinstance(obj, bpy.types.Object) and marker_key(obj):
                moved.add(obj.name)
    if not moved:
        return
    layer = bpy.context.view_layer
    active = layer.objects.active if layer else None
    for name in sorted(moved, key=lambda n: n != (active.name if active else None)):
        obj = scene.objects.get(name)
        if obj is None:
            continue
        key = marker_key(obj)
        other = twin(key)
        # when both twins are being moved (both selected), the active one leads
        if other and (MARKER_PREFIX + other) in moved and obj != active \
                and active is not None and marker_key(active) == other:
            continue
        mirror_from(obj, scene)


def symmetrize(scene, from_side="l"):
    """Make every pair symmetric by copying one side onto the other."""
    found = markers_in(scene)
    count = 0
    for key, obj in found.items():
        if side_of(key) in (from_side, "c") and mirror_from(obj, scene):
            count += 1
    return count


# ---------------------------------------------------------------- drawing

def _disc(x, y, radius, segments=20):
    import math
    pts = []
    for i in range(segments):
        a0 = 2 * math.pi * i / segments
        a1 = 2 * math.pi * (i + 1) / segments
        pts += [(x, y), (x + radius * math.cos(a0), y + radius * math.sin(a0)),
                (x + radius * math.cos(a1), y + radius * math.sin(a1))]
    return pts


def _draw_3d():
    """Skeleton preview lines between the markers (3D, drawn over the mesh)."""
    found = markers_in(bpy.context.scene)
    if not found:
        return
    import gpu
    from gpu_extras.batch import batch_for_shader
    pos = {k: o.matrix_world.translation.copy() for k, o in found.items()}
    coords = [pos[k] for a, b in LINKS if a in pos and b in pos for k in (a, b)]
    if not coords:
        return
    gpu.state.depth_test_set("NONE")
    gpu.state.blend_set("ALPHA")
    line = gpu.shader.from_builtin("POLYLINE_UNIFORM_COLOR")
    line.uniform_float("viewportSize", gpu.state.viewport_get()[2:])
    line.uniform_float("lineWidth", 3.0)
    line.uniform_float("color", (1.0, 1.0, 1.0, 0.75))
    batch_for_shader(line, "LINES", {"pos": coords}).draw(line)
    gpu.state.blend_set("NONE")


def draw_markers_2d(points, selected=(), scale=1.0):
    """Round dots and labels. points: {marker key: (x, y) in pixels}.
    Used by the viewport overlay (and the documentation screenshots)."""
    import blf
    import gpu
    from gpu_extras.batch import batch_for_shader
    gpu.state.blend_set("ALPHA")
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    for key, (x, y) in points.items():
        big = key in selected
        ring = (1, 1, 1, 1) if big else (0.05, 0.05, 0.05, 1)
        for radius, colour in (((11 if big else 9) * scale, ring),
                               ((8 if big else 6) * scale, COLOURS[side_of(key)])):
            shader.uniform_float("color", colour)
            batch_for_shader(shader, "TRIS", {"pos": _disc(x, y, radius)}).draw(shader)
    size = 13 * scale
    font = 0
    blf.size(font, size)
    blf.enable(font, blf.SHADOW)
    blf.shadow(font, 3, 0, 0, 0, 0.9)
    for key, (x, y) in points.items():
        text = LABELS.get(key, key)
        width = blf.dimensions(font, text)[0]
        side = side_of(key)
        # left-side labels go to the right of the dot, right-side to the left
        tx = x + 14 * scale if side != "r" else x - 14 * scale - width
        blf.color(font, *COLOURS[side][:3], 1.0)
        blf.position(font, tx, y - size / 3, 0)
        blf.draw(font, text)
    blf.disable(font, blf.SHADOW)
    gpu.state.blend_set("NONE")


def _draw_labels():
    context = bpy.context
    found = markers_in(context.scene)
    if not found or context.region_data is None:
        return
    from bpy_extras.view3d_utils import location_3d_to_region_2d
    points = {}
    for key, obj in found.items():
        xy = location_3d_to_region_2d(context.region, context.region_data,
                                      obj.matrix_world.translation)
        if xy is not None:
            points[key] = (xy.x, xy.y)
    selected = {k for k, o in found.items() if o.select_get()}
    draw_markers_2d(points, selected, context.preferences.system.ui_scale)


def front_view(context, rotation, centre, height):
    """Turn every 3D view to look at the character from the front, orthographic."""
    screen = context.screen
    if screen is None:
        return 0
    turned = 0
    look = rotation.to_quaternion() @ Matrix.Rotation(1.5707963, 4, "X").to_quaternion()
    for area in screen.areas:
        if area.type != "VIEW_3D":
            continue
        for space in area.spaces:
            if space.type == "VIEW_3D" and space.region_3d is not None:
                r3d = space.region_3d
                r3d.view_perspective = "ORTHO"
                r3d.view_rotation = look
                r3d.view_location = centre
                r3d.view_distance = height * 1.3
                turned += 1
    return turned


def register():
    if not bpy.app.background:
        _handles.append((bpy.types.SpaceView3D.draw_handler_add(
            _draw_3d, (), "WINDOW", "POST_VIEW"), "WINDOW"))
        _handles.append((bpy.types.SpaceView3D.draw_handler_add(
            _draw_labels, (), "WINDOW", "POST_PIXEL"), "WINDOW"))
    if _on_depsgraph not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph)


def unregister():
    while _handles:
        handle, region = _handles.pop()
        bpy.types.SpaceView3D.draw_handler_remove(handle, region)
    if _on_depsgraph in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph)
