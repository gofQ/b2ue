# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Abdulkerim Tabakoğlu
"""Humanoid support: find body landmarks on an unrigged character mesh, mark
the face, and build an auto-weighted skeleton with Unreal Engine bone names.

Works on one mesh standing upright in T-pose or A-pose, the way AI tools such
as Tripo and Meshy deliver characters. Fingers, face bones and non-human body
plans are not handled.
"""

import math
from dataclasses import dataclass, field

import bpy
import numpy as np
from mathutils import Matrix, Vector

FACE_GROUP = "b2ue_face"

# (name, parent, deform) in UE4/UE5 mannequin naming, so Unreal's IK Retargeter
# can map the skeleton automatically.
BONES = [
    ("root", None, False),
    ("pelvis", "root", True),
    ("spine_01", "pelvis", True),
    ("spine_02", "spine_01", True),
    ("spine_03", "spine_02", True),
    ("neck_01", "spine_03", True),
    ("head", "neck_01", True),
]
for _s in ("l", "r"):
    BONES += [
        (f"clavicle_{_s}", "spine_03", True),
        (f"upperarm_{_s}", f"clavicle_{_s}", True),
        (f"lowerarm_{_s}", f"upperarm_{_s}", True),
        (f"hand_{_s}", f"lowerarm_{_s}", True),
        (f"thigh_{_s}", "pelvis", True),
        (f"calf_{_s}", f"thigh_{_s}", True),
        (f"foot_{_s}", f"calf_{_s}", True),
        (f"ball_{_s}", f"foot_{_s}", True),
    ]


MAX_ARM_ANGLE = 60  # degrees below horizontal; T-pose is 0, A-pose 30-50


class NotHumanoid(Exception):
    """The mesh does not look like an upright T/A-pose humanoid."""


@dataclass
class Landmarks:
    height: float
    pose: str
    joints: dict = field(default_factory=dict)  # name -> (head, tail) Vectors
    neck_z: float = 0.0
    notes: list = field(default_factory=list)
    tips: dict = field(default_factory=dict)  # side -> fingertip Vector


# ---------------------------------------------------------------- helpers

def _coords(obj):
    co = np.empty(len(obj.data.vertices) * 3, dtype=np.float64)
    obj.data.vertices.foreach_get("co", co)
    return co.reshape(-1, 3)


def _clusters(values, gap):
    """Split sorted 1D values into (low, high) runs separated by gaps."""
    if len(values) == 0:
        return []
    v = np.sort(values)
    breaks = np.where(np.diff(v) > gap)[0]
    starts = np.r_[0, breaks + 1]
    ends = np.r_[breaks, len(v) - 1]
    return [(v[a], v[b]) for a, b in zip(starts, ends)]


def _slice_x(co, z, thickness):
    return co[np.abs(co[:, 2] - z) < thickness, 0]


def _centre_cluster(co, z, H):
    """The x-run that contains x = 0 at height z (the torso / neck / head)."""
    for lo, hi in _clusters(_slice_x(co, z, 0.005 * H), 0.015 * H):
        if lo <= 0.0 <= hi:
            return lo, hi
    return None


def _y_near(co, x, z, radius):
    near = co[(np.abs(co[:, 0] - x) < radius) & (np.abs(co[:, 2] - z) < radius)]
    return float(np.median(near[:, 1])) if len(near) else 0.0


# ---------------------------------------------------------------- prepare

def join_meshes(meshes):
    """Join several meshes into the first one (characters are one skinned mesh)."""
    if len(meshes) < 2:
        return meshes[0]
    target = meshes[0]
    with bpy.context.temp_override(object=target, active_object=target,
                                   selected_objects=meshes, selected_editable_objects=meshes):
        bpy.ops.object.join()
    return target


def normalize(obj):
    """Bake the transform into the mesh, turn the character to face -Y with arms
    along X, and move it so its feet are centred on the world origin.

    Returns a list of notes about what was changed. The world-space transform
    applied is stored in obj["b2ue_normalize"] (see normalize_matrix()), so
    rig markers placed in the original scene can follow the character.
    """
    notes = []
    applied = Matrix()
    bpy.context.view_layer.update()  # make sure matrix_world is current
    obj.data.transform(obj.matrix_world, shape_keys=True)
    obj.matrix_world = Matrix()
    co = _coords(obj)
    zmin, zmax = co[:, 2].min(), co[:, 2].max()
    H = zmax - zmin
    if H <= 0:
        raise NotHumanoid("mesh has no height")

    # Arms should spread along X. If the widest spread is along Y, turn 90 deg.
    band = co[(co[:, 2] > zmin + 0.55 * H) & (co[:, 2] < zmin + 0.85 * H)]
    if len(band) and np.ptp(band[:, 1]) > 1.15 * np.ptp(band[:, 0]):
        obj.data.transform(Matrix.Rotation(math.radians(90), 4, "Z"), shape_keys=True)
        applied = Matrix.Rotation(math.radians(90), 4, "Z") @ applied
        notes.append("turned 90 deg so the arms lie along X")
        co = _coords(obj)

    # Toes point forward: compare the feet with the knees.
    feet = co[co[:, 2] < zmin + 0.04 * H]
    knees = co[(co[:, 2] > zmin + 0.2 * H) & (co[:, 2] < zmin + 0.3 * H)]
    if len(feet) and len(knees) and feet[:, 1].mean() > knees[:, 1].mean():
        obj.data.transform(Matrix.Rotation(math.radians(180), 4, "Z"), shape_keys=True)
        applied = Matrix.Rotation(math.radians(180), 4, "Z") @ applied
        notes.append("turned 180 deg so the character faces -Y")
        co = _coords(obj)

    torso = co[(co[:, 2] > zmin + 0.5 * H) & (co[:, 2] < zmin + 0.7 * H)]
    x0 = float(np.median(torso[:, 0])) if len(torso) else 0.0
    y0 = float(np.median(torso[:, 1])) if len(torso) else 0.0
    obj.data.transform(Matrix.Translation((-x0, -y0, -zmin)), shape_keys=True)
    applied = Matrix.Translation((-x0, -y0, -zmin)) @ applied
    obj.data.update()
    obj["b2ue_normalize"] = [v for row in applied for v in row]
    return notes


def normalize_matrix(obj):
    """World -> normalized character space, including scale_to_height."""
    flat = obj.get("b2ue_normalize")
    if flat is None:
        return Matrix()
    m = Matrix([flat[i:i + 4] for i in range(0, 16, 4)])
    return Matrix.Scale(obj.get("b2ue_scale", 1.0), 4) @ m


def scale_to_height(objects, height):
    """Uniformly scale meshes (about the world origin) to a real-world height in metres."""
    co = np.vstack([_coords(o) @ np.array(o.matrix_world.to_3x3()).T
                    + np.array(o.matrix_world.translation) for o in objects])
    current = float(np.ptp(co[:, 2]))
    if current <= 0:
        return None
    factor = height / current
    for obj in objects:
        obj["b2ue_scale"] = obj.get("b2ue_scale", 1.0) * factor
        obj.data.transform(obj.matrix_world, shape_keys=True)
        obj.matrix_world = Matrix()
        obj.data.transform(Matrix.Scale(factor, 4), shape_keys=True)
        obj.data.update()
    return current, height


# ---------------------------------------------------------------- landmarks

# Average adult arm proportions (shoulder joint -> fingertip): the elbow sits
# at 42% of the way, the wrist at 75% (upper arm 0.186 H, forearm 0.146 H,
# hand 0.108 H).
ELBOW_AT, WRIST_AT = 0.42, 0.755


def _surface_points(obj, count=200_000, seed=0):
    """Points spread evenly over the surface. Low-poly meshes have too few
    vertices to cut thin slabs from; surface samples work for any density."""
    me = obj.data
    me.calc_loop_triangles()
    co = _coords(obj)
    tri = np.empty(len(me.loop_triangles) * 3, dtype=np.int64)
    me.loop_triangles.foreach_get("vertices", tri)
    tri = tri.reshape(-1, 3)
    if not len(tri):
        return co
    a, b, c = co[tri[:, 0]], co[tri[:, 1]], co[tri[:, 2]]
    area = np.linalg.norm(np.cross(b - a, c - a), axis=1)
    total = area.sum()
    if total <= 0:
        return co
    rng = np.random.default_rng(seed)
    idx = rng.choice(len(tri), size=count, p=area / total)
    u, v = rng.random(count), rng.random(count)
    flip = u + v > 1
    u[flip], v[flip] = 1 - u[flip], 1 - v[flip]
    return a[idx] + (b[idx] - a[idx]) * u[:, None] + (c[idx] - a[idx]) * v[:, None]


def _polyline_point(points, t):
    """Point at fraction t (0..1) of the length of a polyline."""
    seg = np.linalg.norm(np.diff(points, axis=0), axis=1)
    total = seg.sum()
    if total <= 0:
        return Vector(points[0])
    target = t * total
    acc = np.r_[0, np.cumsum(seg)]
    i = int(np.clip(np.searchsorted(acc, target) - 1, 0, len(seg) - 1))
    f = (target - acc[i]) / max(seg[i], 1e-12)
    return Vector(points[i] + (points[i + 1] - points[i]) * f)


def _arm_chain(co, tip, toward, H, torso_half):
    """Walk from the fingertip along the inside of the arm to the shoulder.

    Every step takes a thin slab of the surface across the arm and moves to
    its centre, so the path follows the middle of the arm whatever its pose.
    The arm ends where the slab suddenly widens or its centre swerves towards
    the body for two steps in a row (one step can be a seam or a bulge).
    Returns (centre line from shoulder joint to fingertip as an (n, 3) array,
    arm radius) or None."""
    step, reach = 0.015 * H, 0.06 * H
    d = np.array(toward, dtype=float) - tip
    d /= max(np.linalg.norm(d), 1e-12)
    c = np.array(tip, dtype=float)
    side = np.sign(tip[0]) or 1.0
    path, spreads, coses, dirs = [c.copy()], [], [], [d.copy()]
    walked = 0.0
    while walked < 0.6 * H:
        guess = c + d * step
        rel = co - guess
        along = rel @ d
        dist = np.linalg.norm(rel - np.outer(along, d), axis=1)
        slab = co[(np.abs(along) < step / 2) & (dist < reach)]
        if len(slab) < 3:
            break
        m = slab.mean(axis=0)
        rel = slab - m
        spread = float(np.percentile(np.linalg.norm(rel - np.outer(rel @ d, d), axis=1), 90))
        move = m - c
        norm = np.linalg.norm(move)
        coses.append(float(move @ d) / max(norm, 1e-12))
        spreads.append(spread)
        if norm > 1e-9:
            d = 0.7 * d + 0.3 * move / norm
            d /= np.linalg.norm(d)
        c = m
        walked += step
        path.append(c.copy())
        dirs.append(d.copy())
        if side * m[0] < torso_half * 0.4:
            break

    def ends_here(i):
        if (i + 1) * step <= 0.12 * H or i < 3:
            return False
        recent = max(spreads[i - 3:i])
        return spreads[i] > 1.5 * recent or coses[i] < 0.87   # 0.87 = 30 degrees

    def arm_radius(upto):
        arm = [sp for k, sp in enumerate(spreads[:upto]) if (k + 1) * step > 0.12 * H]
        return float(np.median(arm)) if arm else 0.0

    # The joint is never inside the torso: past the side of the chest at
    # armpit height (plus a little, the joint sits in the shoulder) the walk
    # is in the body. This is what stops a T-pose arm, whose slices widen
    # too gradually towards the shoulder to trigger the checks above.
    stop = None
    for i in range(len(spreads) - 1):
        if ends_here(i) and ends_here(i + 1):
            stop = i
            break
        limit = torso_half + 0.3 * arm_radius(i + 1)
        if side * path[i + 1][0] < limit:
            stop = i
            break
    if stop is None:
        stop = len(spreads) - 1   # the surface ended (arm pushed into the body)
    if stop < 6:
        return None
    radius = arm_radius(stop)
    if not radius:
        return None
    root, d = path[stop], dirs[stop]
    # the joint sits a little past where the arm meets the torso, but not
    # inside the torso
    limit = torso_half + 0.3 * radius
    inward = -side * d[0]
    room = (side * root[0] - limit) / inward if inward > 1e-6 else 0.8 * radius
    shoulder = root + d * float(np.clip(0.8 * radius, 0.0, max(room, 0.0)))
    line = np.array([shoulder] + path[stop:0:-1] + [np.array(tip, dtype=float)])
    return line, radius


def find_landmarks(obj):
    """Locate joints on a normalized mesh (feet at origin, facing -Y)."""
    co = _coords(obj)
    H = float(co[:, 2].max())
    lm = Landmarks(height=H, pose="T")

    # Crotch: lowest height where the body is solid at x = 0.
    crotch = None
    for z in np.arange(0.25 * H, 0.62 * H, 0.005 * H):
        if _centre_cluster(co, z, H):
            crotch = float(z)
            break
    if crotch is None:
        crotch = 0.47 * H
        lm.notes.append("could not find the crotch, used average proportions")

    # Legs
    leg_x = {}
    runs = _clusters(_slice_x(co, crotch * 0.55, 0.01 * H), 0.015 * H)
    for side, sign in (("l", 1), ("r", -1)):
        centres = [(lo + hi) / 2 for lo, hi in runs if sign * (lo + hi) / 2 > 0]
        leg_x[side] = min(centres, key=abs) if centres else sign * 0.1 * H
    ankle_z = 0.045 * H
    knee_z = (crotch + ankle_z) / 2

    # Neck: narrowest part of the centre column between chest and head.
    best = None
    for z in np.arange(0.74 * H, 0.95 * H, 0.004 * H):
        run = _centre_cluster(co, z, H)
        if run and (best is None or run[1] - run[0] < best[1]):
            best = (float(z), run[1] - run[0])
    neck_z = best[0] if best else 0.86 * H
    if best is None:
        lm.notes.append("could not find the neck, used average proportions")
    lm.neck_z = neck_z

    # Chest half-width and hands
    chest = _centre_cluster(co, 0.62 * H, H)
    half_w = max(abs(chest[0]), abs(chest[1])) if chest else 0.12 * H
    upper = co[co[:, 2] > crotch * 0.9]
    tips = {"l": upper[np.argmax(upper[:, 0])], "r": upper[np.argmin(upper[:, 0])]}
    reach = min(abs(tips["l"][0]), abs(tips["r"][0]))
    if reach < 1.6 * half_w:
        raise NotHumanoid("the arms are close to the body; auto-rigging needs a T-pose or A-pose")

    joints = {}
    y_hips = _y_near(co, 0.0, crotch + 0.04 * H, 0.06 * H)
    pelvis = Vector((0, y_hips, crotch + 0.03 * H))
    neck_base = Vector((0, _y_near(co, 0, neck_z - 0.04 * H, 0.04 * H), neck_z - 0.04 * H))
    joints["root"] = (Vector((0, 0, 0)), Vector((0, 0.1 * H, 0)))
    spine_start = Vector((0, y_hips, crotch + 0.09 * H))
    joints["pelvis"] = (pelvis, spine_start)
    for i, name in enumerate(("spine_01", "spine_02", "spine_03")):
        a = spine_start.lerp(neck_base, i / 3)
        b = spine_start.lerp(neck_base, (i + 1) / 3)
        joints[name] = (a, b)
    head_base = Vector((0, _y_near(co, 0, neck_z + 0.02 * H, 0.04 * H), neck_z + 0.02 * H))
    joints["neck_01"] = (neck_base, head_base)
    joints["head"] = (head_base, Vector((0, head_base.y, H)))

    # Torso half-width at armpit height: going up from the chest, the last
    # slice before the arms join it (the width then jumps).
    armpit_half, previous = half_w, None
    for z in np.arange(0.6 * H, neck_z - 0.02 * H, 0.01 * H):
        run = _centre_cluster(co, z, H)
        if run is None:
            continue
        width = max(abs(run[0]), abs(run[1]))
        if previous is not None and width > 1.35 * previous:
            armpit_half = previous
            break
        previous = width

    arm_angles = []
    surface = _surface_points(obj)
    for side, sign in (("l", 1), ("r", -1)):
        tip = Vector(tips[side])
        rough = np.array((sign * half_w, y_hips, neck_z - 0.06 * H))
        chain = _arm_chain(surface, np.array(tip), rough, H, armpit_half)
        if chain is not None:
            line, _ = chain
            # centre each joint front-to-back the same way markers are
            shoulder = snap_depth(surface, Vector(line[0]), H)
            elbow = snap_depth(surface, _polyline_point(line, ELBOW_AT), H)
            wrist = snap_depth(surface, _polyline_point(line, WRIST_AT), H)
        else:
            lm.notes.append(f"could not trace the {'left' if sign > 0 else 'right'} arm, "
                            "used average proportions")
            col = co[(sign * co[:, 0] > half_w * 1.15) & (sign * co[:, 0] < half_w * 1.15 + 0.04 * H)
                     & (co[:, 2] > 0.6 * H) & (co[:, 2] < neck_z)]
            sh_z = float(np.median(col[:, 2])) if len(col) else neck_z - 0.07 * H
            shoulder = Vector((sign * half_w * 0.85, _y_near(co, sign * half_w, sh_z, 0.05 * H), sh_z))
            wrist = tip + (shoulder - tip).normalized() * 0.085 * H
            elbow = shoulder.lerp(wrist, 0.5) + Vector((0, 0.01 * H, 0))
        clav = Vector((sign * 0.02 * H, shoulder.y, shoulder.z))
        joints[f"clavicle_{side}"] = (clav, shoulder)
        joints[f"upperarm_{side}"] = (shoulder, elbow)
        joints[f"lowerarm_{side}"] = (elbow, wrist)
        joints[f"hand_{side}"] = (wrist, tip)
        lm.tips[side] = tip.copy()
        d = wrist - shoulder
        arm_angles.append(math.degrees(math.atan2(-d.z, abs(d.x))))

        x = leg_x[side]
        hip = Vector((x, y_hips, crotch - 0.02 * H))
        knee = _snap_marker(co, "knee_l", Vector((x, 0, knee_z)), H)
        ankle = _snap_marker(co, "ankle_l", Vector((x, 0, ankle_z)), H)
        foot = co[(sign * co[:, 0] > 0) & (co[:, 2] < 0.05 * H)
                  & (np.abs(co[:, 0] - x) < 0.08 * H)]
        toe_y = float(foot[:, 1].min()) if len(foot) else ankle.y - 0.12 * H
        ball = Vector((x, ankle.y + (toe_y - ankle.y) * 0.65, 0.015 * H))
        toe = Vector((x, toe_y, 0.015 * H))
        joints[f"thigh_{side}"] = (hip, knee)
        joints[f"calf_{side}"] = (knee, ankle)
        joints[f"foot_{side}"] = (ankle, ball)
        joints[f"ball_{side}"] = (ball, toe)

    # People are (nearly) symmetric: average each arm joint with its mirror
    # twin, which also evens out small measuring noise between the sides.
    for left, right in (("clavicle_l", "clavicle_r"), ("upperarm_l", "upperarm_r"),
                        ("lowerarm_l", "lowerarm_r"), ("hand_l", "hand_r")):
        (lh, lt), (rh, rt) = joints[left], joints[right]
        flip = Vector((-1, 1, 1))
        mh, mt = rh * flip, rt * flip
        if (lh - mh).length < 0.05 * H and (lt - mt).length < 0.05 * H:
            h, t = (lh + mh) / 2, (lt + mt) / 2
            joints[left] = (h, t)
            joints[right] = (h * flip, t * flip)

    angle = sum(arm_angles) / len(arm_angles)
    if angle > MAX_ARM_ANGLE:
        raise NotHumanoid(f"the arms hang {angle:.0f}° below horizontal; auto-rigging needs a "
                          f"T-pose (0°) or A-pose (up to {MAX_ARM_ANGLE}°)")
    lm.pose = "T-pose" if angle < 20 else "A-pose"
    lm.joints = joints
    return lm


# ---------------------------------------------------------------- rig markers
#
# Markers are small empties the user can place on the body in Blender to
# correct (or replace) the automatic joint detection. Each one sets a joint:

MARKER_PREFIX = "b2ue_marker_"
MARKERS = {
    "neck": "base of the neck",
    "pelvis": "centre of the hips, at crotch height",
    "shoulder_l": "left shoulder joint", "shoulder_r": "right shoulder joint",
    "elbow_l": "left elbow", "elbow_r": "right elbow",
    "wrist_l": "left wrist", "wrist_r": "right wrist",
    "knee_l": "left knee", "knee_r": "right knee",
    "ankle_l": "left ankle", "ankle_r": "right ankle",
}


def marker_points(lm):
    """Marker positions (normalized space) that reproduce lm's joints."""
    j = lm.joints
    pts = {"neck": j["neck_01"][0], "pelvis": j["pelvis"][0]}
    for s in ("l", "r"):
        pts[f"shoulder_{s}"] = j[f"upperarm_{s}"][0]
        pts[f"elbow_{s}"] = j[f"lowerarm_{s}"][0]
        pts[f"wrist_{s}"] = j[f"hand_{s}"][0]
        pts[f"knee_{s}"] = j[f"calf_{s}"][0]
        pts[f"ankle_{s}"] = j[f"foot_{s}"][0]
    return {k: v.copy() for k, v in pts.items()}


def default_landmarks(height):
    """Average adult proportions, A-pose, facing -Y. Used when detection fails
    and the user has placed every marker."""
    H = height
    lm = Landmarks(height=H, pose="from markers")
    j = {}
    crotch, neck = 0.47 * H, 0.86 * H
    j["root"] = (Vector((0, 0, 0)), Vector((0, 0.1 * H, 0)))
    j["pelvis"] = (Vector((0, 0, crotch + 0.03 * H)), Vector((0, 0, crotch + 0.09 * H)))
    j["neck_01"] = (Vector((0, 0, neck - 0.04 * H)), Vector((0, 0, neck + 0.02 * H)))
    j["head"] = (Vector((0, 0, neck + 0.02 * H)), Vector((0, 0, H)))
    for s, sign in (("l", 1), ("r", -1)):
        sh = Vector((sign * 0.12 * H, 0, 0.81 * H))
        el = sh + Vector((sign * 0.13 * H, 0, -0.09 * H))
        wr = el + Vector((sign * 0.12 * H, 0, -0.08 * H))
        j[f"clavicle_{s}"] = (Vector((sign * 0.02 * H, 0, sh.z)), sh)
        j[f"upperarm_{s}"] = (sh, el)
        j[f"lowerarm_{s}"] = (el, wr)
        j[f"hand_{s}"] = (wr, wr + (wr - el).normalized() * 0.1 * H)
        hip = Vector((sign * 0.06 * H, 0, crotch - 0.02 * H))
        knee = Vector((sign * 0.06 * H, -0.01 * H, 0.27 * H))
        ankle = Vector((sign * 0.06 * H, 0.01 * H, 0.045 * H))
        ball = Vector((sign * 0.06 * H, -0.07 * H, 0.015 * H))
        j[f"thigh_{s}"] = (hip, knee)
        j[f"calf_{s}"] = (knee, ankle)
        j[f"foot_{s}"] = (ankle, ball)
        j[f"ball_{s}"] = (ball, ball + Vector((0, -0.04 * H, 0)))
    lm.joints = j
    lm.neck_z = neck
    _rebuild_spine(lm)
    return lm


def _rebuild_spine(lm):
    j, H = lm.joints, lm.height
    pelvis, neck = j["pelvis"][0], j["neck_01"][0]
    spine_start = pelvis + Vector((0, 0, 0.06 * H))
    j["pelvis"] = (pelvis, spine_start)
    for i, name in enumerate(("spine_01", "spine_02", "spine_03")):
        j[name] = (spine_start.lerp(neck, i / 3), spine_start.lerp(neck, (i + 1) / 3))


def snap_depth(co, point, H, radius=0.03, probe_dz=0.0, offset=0.0):
    """Move point to the middle of the body along Y (front to back), keeping
    its X and Z. Markers are placed in the front view, like Mixamo: only their
    height and sideways position matter; the depth is found here.

    probe_dz measures a little higher (ankles: above the foot), offset moves
    the result forward (-) or back (+) in units of H (knees sit a little
    forward so they bend the right way)."""
    z = point.z + probe_dz * H
    near = co[(np.abs(co[:, 0] - point.x) < radius * H) & (np.abs(co[:, 2] - z) < radius * H)]
    if len(near) < 3:
        return point.copy()
    y = (float(near[:, 1].min()) + float(near[:, 1].max())) / 2 + offset * H
    return Vector((point.x, y, point.z))


# how each marker's depth is measured: (probe_dz, offset), see snap_depth
DEPTH_RULES = {"knee": (0.0, -0.01), "ankle": (0.03, 0.0)}


def _snap_marker(co, key, point, H):
    probe, offset = DEPTH_RULES.get(key.rsplit("_", 1)[0] if key[-2:] in ("_l", "_r") else key,
                                    (0.0, 0.0))
    return snap_depth(co, point, H, probe_dz=probe, offset=offset)


def apply_markers(lm, markers, co=None):
    """Move lm's joints to the given marker positions (normalized space).
    With co (the character's vertex coordinates) each marker's depth is moved
    to the middle of the body first. Returns the names of the markers used."""
    j, H = lm.joints, lm.height
    if co is not None:
        markers = {k: _snap_marker(co, k, v, H) for k, v in markers.items()}
    used = []
    if "neck" in markers:
        n = markers["neck"]
        head_base = n + Vector((0, 0, max(j["neck_01"][1].z - j["neck_01"][0].z, 0.04 * H)))
        j["neck_01"] = (n, head_base)
        j["head"] = (head_base, Vector((head_base.x, head_base.y, H)))
        lm.neck_z = n.z + 0.04 * H
        used.append("neck")
    if "pelvis" in markers:
        j["pelvis"] = (markers["pelvis"], j["pelvis"][1])
        used.append("pelvis")
    _rebuild_spine(lm)
    for s, sign in (("l", 1), ("r", -1)):
        sh = markers.get(f"shoulder_{s}", j[f"upperarm_{s}"][0])
        el = markers.get(f"elbow_{s}", j[f"lowerarm_{s}"][0])
        wr = markers.get(f"wrist_{s}", j[f"hand_{s}"][0])
        direction = (wr - el).normalized()
        tip = lm.tips.get(s)
        if tip is not None:   # the hand ends at the fingertips
            hand_len = max((tip - wr).dot(direction), 0.04 * H)
        else:
            hand_len = (j[f"hand_{s}"][1] - j[f"hand_{s}"][0]).length
        j[f"clavicle_{s}"] = (Vector((sign * 0.02 * H, sh.y, sh.z)), sh)
        j[f"upperarm_{s}"] = (sh, el)
        j[f"lowerarm_{s}"] = (el, wr)
        j[f"hand_{s}"] = (wr, wr + direction * hand_len)
        knee = markers.get(f"knee_{s}", j[f"calf_{s}"][0])
        ankle = markers.get(f"ankle_{s}", j[f"foot_{s}"][0])
        hip = j[f"thigh_{s}"][0]
        if "pelvis" in markers:
            hip = Vector((knee.x, markers["pelvis"].y, markers["pelvis"].z - 0.05 * H))
        ball, toe = j[f"ball_{s}"]
        shift = Vector((ankle.x - ball.x, 0, 0))
        j[f"thigh_{s}"] = (hip, knee)
        j[f"calf_{s}"] = (knee, ankle)
        j[f"foot_{s}"] = (ankle, ball + shift)
        j[f"ball_{s}"] = (ball + shift, toe + shift)
        used += [k for k in (f"shoulder_{s}", f"elbow_{s}", f"wrist_{s}", f"knee_{s}",
                             f"ankle_{s}") if k in markers]
    lm.notes.append(f"{len(used)} joint{'s' if len(used) != 1 else ''} placed by rig markers")
    return used


def scene_markers(scene):
    """{marker name: world position} for the b2ue rig markers in the scene."""
    out = {}
    for obj in scene.objects:
        if obj.name.startswith(MARKER_PREFIX):
            key = obj.name[len(MARKER_PREFIX):].split(".")[0]
            if key in MARKERS:
                out[key] = obj.matrix_world.translation.copy()
    return out


def mark_face(obj, lm):
    """Vertex group with the front of the head (eyes, nose, mouth)."""
    co = _coords(obj)
    H = lm.height
    head = co[co[:, 2] > lm.neck_z + 0.02 * H]
    if not len(head):
        return 0
    mid_y = float(np.median(head[:, 1]))
    half = max(abs(head[:, 0].min()), abs(head[:, 0].max()))
    idx = np.where((co[:, 2] > lm.neck_z + 0.02 * H) & (co[:, 1] < mid_y)
                   & (np.abs(co[:, 0]) < half))[0]
    if FACE_GROUP in obj.vertex_groups:
        obj.vertex_groups.remove(obj.vertex_groups[FACE_GROUP])
    group = obj.vertex_groups.new(name=FACE_GROUP)
    group.add(idx.tolist(), 1.0, "REPLACE")
    return len(idx)


# ---------------------------------------------------------------- rig

def _segment_distance(points, a, b):
    ab = b - a
    t = np.clip(((points - a) @ ab) / max(ab @ ab, 1e-12), 0.0, 1.0)
    return np.linalg.norm(points - (a + t[:, None] * ab), axis=1)


def _bind(obj, arm, kind):
    """Parent obj to arm with an Armature modifier. kind: AUTO (heat) or NAME."""
    for other in bpy.context.scene.objects:
        other.select_set(False)
    obj.select_set(True)
    arm.select_set(True)
    bpy.context.view_layer.objects.active = arm
    with bpy.context.temp_override(object=arm, active_object=arm, selected_objects=[obj, arm],
                                   selected_editable_objects=[obj, arm]):
        bpy.ops.object.parent_set(type=f"ARMATURE_{kind}")


def _weighted_share(obj, arm):
    deform = {b.name for b in arm.data.bones if b.use_deform}
    idx = {g.index for g in obj.vertex_groups if g.name in deform}
    if not len(obj.data.vertices):
        return 0.0
    hit = sum(1 for v in obj.data.vertices
              if sum(g.weight for g in v.groups if g.group in idx) > 1e-4)
    return hit / len(obj.data.vertices)


def _proxy_weights(obj, arm, height):
    """Compute weights on a watertight voxel copy and transfer them to obj.

    Automatic (heat) weights need one closed surface around the bones. Real
    characters are many separate pieces (eyes, lashes, boots, hair cards,
    clothes), and heat weighting fails on them, often for the whole mesh.
    A voxel remesh fuses everything into one closed shell where it works;
    the weights are then copied to the real mesh from the nearest proxy face.
    """
    proxy = obj.copy()
    proxy.data = obj.data.copy()
    proxy.name = "b2ue_weight_proxy"
    bpy.context.scene.collection.objects.link(proxy)
    proxy.parent = None
    proxy.modifiers.clear()
    proxy.vertex_groups.clear()
    if proxy.data.shape_keys is not None:
        proxy.shape_key_clear()
    remesh = proxy.modifiers.new("remesh", "REMESH")
    remesh.mode = "VOXEL"
    remesh.voxel_size = height / 180.0
    remesh.adaptivity = 0.0
    with bpy.context.temp_override(object=proxy, active_object=proxy,
                                   selected_objects=[proxy], selected_editable_objects=[proxy]):
        bpy.ops.object.modifier_apply(modifier=remesh.name)
    _bind(proxy, arm, "AUTO")
    share = _weighted_share(proxy, arm)

    _bind(obj, arm, "NAME")  # armature modifier + one empty group per bone
    transfer = obj.modifiers.new("b2ue_weights", "DATA_TRANSFER")
    transfer.object = proxy
    transfer.use_vert_data = True
    transfer.data_types_verts = {"VGROUP_WEIGHTS"}
    transfer.vert_mapping = "POLYINTERP_NEAREST"
    transfer.layers_vgroup_select_src = "ALL"
    transfer.layers_vgroup_select_dst = "NAME"
    with bpy.context.temp_override(object=obj, active_object=obj, selected_objects=[obj],
                                   selected_editable_objects=[obj]):
        bpy.ops.object.modifier_move_to_index(modifier=transfer.name, index=0)
        bpy.ops.object.modifier_apply(modifier=transfer.name)
    bpy.data.objects.remove(proxy, do_unlink=True)
    return share


# Blender's own heat weights are tried as well when the mesh is small enough
# to do it quickly (a decimated game mesh); above this the proxy is used alone.
HEAT_MAX_VERTS = 150_000


def _bone_groups(obj, arm):
    names = {b.name for b in arm.data.bones}
    return [g for g in obj.vertex_groups if g.name in names]


def _snapshot_weights(obj, arm):
    """{bone name: (vertex indices, weights)} for the bone vertex groups."""
    idx = {g.index: g.name for g in _bone_groups(obj, arm)}
    data = {name: ([], []) for name in idx.values()}
    for v in obj.data.vertices:
        for g in v.groups:
            name = idx.get(g.group)
            if name is not None:
                data[name][0].append(v.index)
                data[name][1].append(g.weight)
    return data


def _unbind(obj, arm):
    """Remove the skin (armature modifier, parent, bone vertex groups) but keep
    every other vertex group, such as the face group."""
    for mod in list(obj.modifiers):
        if mod.type == "ARMATURE":
            obj.modifiers.remove(mod)
    if obj.parent is not None:
        world = obj.matrix_world.copy()
        obj.parent = None
        obj.matrix_world = world
    for group in _bone_groups(obj, arm):
        obj.vertex_groups.remove(group)


def _restore_weights(obj, arm, snapshot):
    _unbind(obj, arm)
    _bind(obj, arm, "NAME")
    for name, (verts, weights) in snapshot.items():
        group = obj.vertex_groups.get(name) or obj.vertex_groups.new(name=name)
        for i, w in zip(verts, weights):
            group.add([i], w, "REPLACE")


def _finish_weights(obj, arm, smooth):
    filled = _fill_unweighted(obj, arm)
    if smooth:
        smooth_weights(obj, arm)
    bpy.context.view_layer.objects.active = obj
    with bpy.context.temp_override(object=obj, active_object=obj, selected_objects=[obj],
                                   selected_editable_objects=[obj]):
        bpy.ops.object.vertex_group_limit_total(group_select_mode="BONE_DEFORM", limit=4)
        bpy.ops.object.vertex_group_normalize_all(group_select_mode="BONE_DEFORM",
                                                  lock_active=False)
    return filled


def build_rig(obj, lm, name="Armature", method="best"):
    """Create the skeleton and skin the mesh to it.

    method:
      "proxy"  heat weights on a watertight voxel copy, transferred to the
               mesh (works on characters made of many pieces) and smoothed
      "heat"   Blender's automatic weights on the mesh itself
      "best"   (default) both, then keeps the one that stretches the mesh
               less in a test pose (rigcheck.TEST_POSE). One-piece
               characters often do slightly better with plain heat weights,
               multi-piece ones much better with the proxy.
    Any vertex still without weights gets its nearest bone; weights are
    limited to 4 bones per vertex and normalized.

    The armature object is called "Armature" so Unreal does not add an extra
    root bone for it. Returns (armature, bone count, filled vertex count).
    The method used and the test-pose numbers are stored on the armature
    object as "b2ue_weights" and "b2ue_weight_test".
    """
    from . import rigcheck
    arm = bpy.data.objects.new(name, bpy.data.armatures.new(name))
    bpy.context.scene.collection.objects.link(arm)
    for other in bpy.context.scene.objects:
        other.select_set(False)
    arm.select_set(True)
    bpy.context.view_layer.objects.active = arm
    with bpy.context.temp_override(object=arm, active_object=arm,
                                   selected_objects=[arm], selected_editable_objects=[arm]):
        bpy.ops.object.mode_set(mode="EDIT")
        edit = arm.data.edit_bones
        for bone_name, parent, deform in BONES:
            head, tail = lm.joints[bone_name]
            bone = edit.new(bone_name)
            bone.head, bone.tail = head, tail
            bone.use_deform = deform
            if parent:
                bone.parent = edit[parent]
        bpy.ops.object.mode_set(mode="OBJECT")

    if method == "best" and len(obj.data.vertices) > HEAT_MAX_VERTS:
        method = "proxy"
    if method in ("proxy", "best"):
        _proxy_weights(obj, arm, lm.height)
        filled = _finish_weights(obj, arm, smooth=True)
        chosen = "proxy"
    else:
        _bind(obj, arm, "AUTO")
        filled = _finish_weights(obj, arm, smooth=False)
        chosen = "heat"

    if method == "best":
        proxy_test = rigcheck.pose_stretch(obj, arm)
        snapshot = _snapshot_weights(obj, arm)
        _unbind(obj, arm)
        _bind(obj, arm, "AUTO")
        heat_filled = _finish_weights(obj, arm, smooth=False)
        heat_test = rigcheck.pose_stretch(obj, arm)
        score = lambda t: (round(t["tears"], 5), t["p99"])  # noqa: E731
        if score(heat_test) < score(proxy_test):
            chosen, filled = "heat", heat_filled
        else:
            _restore_weights(obj, arm, snapshot)
        arm["b2ue_weight_test"] = {
            "proxy_tears_pct": proxy_test["tears"] * 100, "proxy_max": proxy_test["max"],
            "heat_tears_pct": heat_test["tears"] * 100, "heat_max": heat_test["max"]}
    arm["b2ue_weights"] = chosen
    return arm, len(BONES), filled


def smooth_weights(obj, arm, factor=0.5, repeat=2):
    """Blend every vertex's bone weights with its neighbours' (Laplacian
    smoothing). Softens the hard steps a nearest-face transfer can leave."""
    deform = [b.name for b in arm.data.bones if b.use_deform]
    col = {obj.vertex_groups[n].index: i for i, n in enumerate(deform) if n in obj.vertex_groups}
    if not col:
        return
    n = len(obj.data.vertices)
    w = np.zeros((n, len(deform)), dtype=np.float32)
    for v in obj.data.vertices:
        for g in v.groups:
            j = col.get(g.group)
            if j is not None:
                w[v.index, j] = g.weight
    edges = np.empty(len(obj.data.edges) * 2, dtype=np.int64)
    obj.data.edges.foreach_get("vertices", edges)
    a, b = edges.reshape(-1, 2).T
    degree = np.bincount(np.r_[a, b], minlength=n).astype(np.float32)
    has = degree > 0
    for _ in range(repeat):
        acc = np.zeros_like(w)
        np.add.at(acc, a, w[b])
        np.add.at(acc, b, w[a])
        avg = np.where(has[:, None], acc / np.maximum(degree, 1)[:, None], w)
        w = (1 - factor) * w + factor * avg
    for name, j in ((nm, col[obj.vertex_groups[nm].index]) for nm in deform if nm in obj.vertex_groups):
        group = obj.vertex_groups[name]
        nz = np.nonzero(w[:, j] > 1e-4)[0]
        zero = np.nonzero(w[:, j] <= 1e-4)[0]
        if len(zero):
            group.remove(zero.tolist())
        for i in nz:
            group.add([int(i)], float(w[i, j]), "REPLACE")


def _fill_unweighted(obj, arm):
    deform = [b for b in arm.data.bones if b.use_deform]
    groups = {}
    for bone in deform:
        groups[bone.name] = (obj.vertex_groups.get(bone.name)
                             or obj.vertex_groups.new(name=bone.name))
    deform_idx = {g.index for g in groups.values()}
    missing = [v.index for v in obj.data.vertices
               if sum(g.weight for g in v.groups if g.group in deform_idx) < 1e-4]
    if not missing:
        return 0
    co = _coords(obj)[missing]
    dist = np.stack([_segment_distance(co, np.array(b.head_local), np.array(b.tail_local))
                     for b in deform], axis=1)
    nearest = np.argmin(dist, axis=1)
    for i, bone in enumerate(deform):
        verts = [missing[j] for j in np.where(nearest == i)[0]]
        if verts:
            groups[bone.name].add(verts, 1.0, "REPLACE")
    return len(missing)
