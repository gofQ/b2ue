# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Abdulkerim Tabakoğlu
"""Run the pipeline steps for one source file:

load -> cleanup -> humanoid -> scale -> decimate -> rig -> validate -> export
"""

from pathlib import Path

import bpy

import shutil

from . import cleanup, export, humanoid, ingest, optimize, output, quality, validate
from . import textures as textures_mod
from .console import Reporter
from .options import Options


def _meshes(scene):
    return [o for o in scene.objects if o.type == "MESH"]


def _tris(objs):
    return sum(cleanup.triangle_count(o.data) for o in objs)


def _cleanup_text(stats):
    labels = [("merged_verts", "merged {:,} duplicate verts"),
              ("loose_verts_removed", "removed {:,} loose verts"),
              ("loose_edges_removed", "removed {:,} loose edges"),
              ("transforms_applied", "applied {} transform(s)"),
              ("unparented", "unparented {} mesh(es)"),
              ("empties_removed", "removed {} empty object(s)")]
    parts = [text.format(stats[key]) for key, text in labels if stats.get(key)]
    return ", ".join(parts) or "nothing to fix"


def _shared_materials(meshes):
    seen, shared = {}, set()
    for obj in meshes:
        for slot in obj.material_slots:
            if slot.material is None:
                continue
            if slot.material.name in seen and seen[slot.material.name] != obj.name:
                shared.add(slot.material.name)
            seen.setdefault(slot.material.name, obj.name)
    return shared


def _decimate_step(scene, opts, landmarks, st, result):
    protect = humanoid.FACE_GROUP if (opts.keep_face and landmarks) else None
    meshes = _meshes(scene)
    shared = _shared_materials(meshes) if opts.bake_normals else set()
    done, kept = [], []
    result["quality"] = []
    for obj in meshes:
        had_keys = obj.data.shape_keys is not None
        can_bake = opts.bake_normals and not any(
            s.material and s.material.name in shared for s in obj.material_slots)
        try:
            r = optimize.optimize_mesh(
                obj, opts.target_tris, opts.quality, protect, opts.face_detail,
                opts.drop_shape_keys,
                min(opts.bake_size, opts.texture_size or opts.bake_size) if can_bake else 0,
                measure=opts.measure, texture_limit=opts.texture_size or 0)
        except cleanup.HasShapeKeys as exc:
            kept.append((obj, exc.count))
            continue
        if r is None:
            continue
        done.append(r)
        q = r["quality"]
        result["quality"].append({"mesh": obj.name, **r})
        line = f"{obj.name}: {r['before']:,} -> {r['after']:,} tris"
        if q:
            line += f", {quality.describe(q)}, normals {q['normal_deg']:.1f}°"
        if r["baked"]:
            line += ", detail baked into the normal map"
        if r["note"]:
            line += f" ({r['note']})"
        if opts.bake_normals and not can_bake:
            line += " (not baked: material shared with another mesh)"
        if had_keys and opts.drop_shape_keys:
            line += ", shape keys removed"
        st.lines.append(line)
    if done:
        before = sum(r["before"] for r in done)
        after = sum(r["after"] for r in done)
        st.detail = f"{before:,} -> {after:,} tris"
        worst = [r["quality"] for r in done if r["quality"]]
        if worst:
            st.detail += f", shape within {max(q['shape_p95'] for q in worst):.3f}% (p95)"
        if protect:
            st.detail += f", face kept {opts.face_detail:g}x denser"
        if opts.quality:
            st.detail += f", quality '{opts.quality}'"
    elif not kept:
        st.skip(f"already under {opts.target_tris:,} tris" if opts.target_tris else "nothing to reduce")
    for obj, count in kept:
        tris = cleanup.triangle_count(obj.data)
        st.lines.append(f"{obj.name}: not decimated, it has {count} shape keys "
                        f"(morphs) that decimation would destroy; kept at {tris:,} tris")
        result["notes"].append(f"{obj.name} kept its {count} shape keys and was not decimated")
    if kept:
        st.warn((st.detail + "; " if done else "")
                + f"{len(kept)} mesh{'es' if len(kept) != 1 else ''} with shape keys kept "
                "as they are (use --drop-shape-keys to decimate them anyway)")
    if any("exceed" in r["note"] or "bake skipped" in r["note"] for r in done):
        st.warn()


def _check_outputs_early(scene, path, out_dir, opts, batch):
    """Fail before the slow steps when the output name is already taken.
    Only for files that become one asset, whose name is known now; the final
    check happens again when the files are moved into place."""
    assets = export.collect_assets(scene)
    if len(assets) != 1:
        return
    asset = assets[0]
    asset.label = opts.name or path.stem
    if opts.rig and opts.human and asset.kind == "static":
        asset.kind = "skeletal"  # the rig step will turn it into a skeletal mesh
    _, problems = output.plan([Path(asset.file_name)], out_dir, batch, path)
    if problems:
        raise output.OutputConflict("; ".join(problems))


def process_file(path, out_dir=None, opts=None, reporter=None, batch=None):
    """Process one file. Pass out_dir=None to only validate.

    batch (output.Batch) carries what a multi-file run must not overwrite;
    without it only this source file is protected.

    Returns a dict with the steps' results, the validation issues and the
    exported files. A step that fails stops this file and sets "error"; in
    that case nothing is written to out_dir.
    """
    opts = opts or Options()
    rep = reporter or Reporter()
    batch = batch or output.Batch.for_files([path], overwrite=opts.overwrite)
    result = {"file": str(path), "issues": [], "exported": [], "skipped": [],
              "notes": [], "error": None}
    try:
        _run(Path(path), out_dir, opts, rep, result, batch)
    except Exception as exc:  # reported by the failed step
        result["error"] = str(exc) or type(exc).__name__
    return result


def _run(path, out_dir, opts, rep, result, batch):
    scene = None

    with rep.step("load") as st:
        ingest.load(path)
        scene = bpy.context.scene
        if opts.image_files:
            ingest.use_image_files(opts.image_files)
        if opts.objects:
            ingest.keep_only(scene, opts.objects)
        meshes = _meshes(scene)
        images = {n.image.name for o in meshes for s in o.material_slots
                  if s.material and s.material.use_nodes
                  for n in s.material.node_tree.nodes if n.type == "TEX_IMAGE" and n.image}
        rigged = any(o.type == "ARMATURE" for o in scene.objects)
        st.detail = (f"{len(meshes)} mesh{'es' if len(meshes) != 1 else ''}, "
                     f"{_tris(meshes):,} tris, {len(images)} texture{'s' if len(images) != 1 else ''}"
                     + (", has a skeleton" if rigged else ""))
        if not meshes:
            raise ValueError("no meshes in the file")
        if out_dir is not None:
            _check_outputs_early(scene, path, out_dir, opts, batch)

    if opts.cleanup:
        with rep.step("cleanup") as st:
            stats = cleanup.cleanup_scene(scene, opts.merge_distance, opts.triangulate)
            result["cleanup"] = stats
            st.detail = _cleanup_text(stats)

    rigged = any(o.type == "ARMATURE" for o in scene.objects)
    landmarks = None
    character = None

    if opts.human:
        with rep.step("humanoid") as st:
            if rigged:
                st.skip("already has a skeleton, left as it is")
            else:
                meshes = _meshes(scene)
                markers_world = humanoid.scene_markers(scene)
                character = humanoid.join_meshes(meshes)
                notes = humanoid.normalize(character)
                if opts.height:
                    humanoid.scale_to_height([character], opts.height)
                to_char = humanoid.normalize_matrix(character)
                markers = {k: to_char @ v for k, v in markers_world.items()}
                try:
                    landmarks = humanoid.find_landmarks(character)
                except humanoid.NotHumanoid as exc:
                    if len(markers) == len(humanoid.MARKERS):
                        # every joint placed by hand: trust the markers
                        H = float(humanoid._coords(character)[:, 2].max())
                        landmarks = humanoid.default_landmarks(H)
                        notes.append(f"automatic detection failed ({exc}); using the rig markers")
                    else:
                        st.warn(f"not treated as a humanoid: {exc}"
                                + (f" (place all {len(humanoid.MARKERS)} rig markers to rig it "
                                   "anyway)" if markers else ""))
                        result["notes"].append(str(exc))
                if landmarks is not None:
                    if markers:
                        humanoid.apply_markers(landmarks, markers, humanoid._coords(character))
                    notes += landmarks.notes
                    face = humanoid.mark_face(character, landmarks) if opts.keep_face else 0
                    st.detail = f"{landmarks.pose}, {landmarks.height:.2f} m tall"
                    if len(meshes) > 1:
                        st.detail += f", joined {len(meshes)} meshes"
                    if face:
                        st.detail += f", face marked ({face:,} verts)"
                    if any("could not" in n for n in landmarks.notes):
                        st.warn()
                st.lines += notes
                result["notes"] += notes

    if opts.height and character is None:
        with rep.step("scale") as st:
            if rigged:
                st.skip("has a skeleton; scale it in Blender to keep the rig intact")
            else:
                before, after = humanoid.scale_to_height(_meshes(scene), opts.height)
                st.detail = f"{before:.2f} m -> {after:.2f} m tall"

    if opts.target_tris or opts.quality:
        with rep.step("decimate") as st:
            _decimate_step(scene, opts, landmarks, st, result)

    if opts.rig and opts.human:
        with rep.step("rig") as st:
            if rigged:
                st.skip("already has a skeleton")
            elif landmarks is None:
                st.skip("no humanoid found, nothing to rig")
            else:
                arm, bones, filled = humanoid.build_rig(character, landmarks)
                st.detail = f"{bones} bones with Unreal mannequin names, auto weights"
                if filled:
                    st.detail += f" ({filled:,} verts set to nearest bone)"
                test = arm.get("b2ue_weight_test")
                if test:
                    kind = "Blender heat" if arm["b2ue_weights"] == "heat" else "proxy"
                    line = (f"weights: {kind} (test pose, edges stretched >2x: proxy "
                            f"{test['proxy_tears_pct']:.2f}%, heat {test['heat_tears_pct']:.2f}%)")
                    st.lines.append(line)
                    result["notes"].append(line)
                    del arm["b2ue_weight_test"]
                if "b2ue_weights" in arm:
                    del arm["b2ue_weights"]

    with rep.step("validate") as st:
        issues = validate.validate_scene(scene, opts.tri_warning)
        result["issues"] = [i.to_dict() for i in issues]
        errors = [i for i in issues if i.level == validate.ERROR]
        warnings = [i for i in issues if i.level == validate.WARNING]
        if errors or warnings:
            st.warn(", ".join(p for p in (
                f"{len(errors)} error{'s' if len(errors) != 1 else ''}" if errors else "",
                f"{len(warnings)} warning{'s' if len(warnings) != 1 else ''}" if warnings else "",
            ) if p))
        else:
            st.detail = "no problems found"
        if hasattr(rep, "issue_line"):
            st.lines += [rep.issue_line(i.to_dict()) for i in errors + warnings]

    if out_dir is None:
        return

    with rep.step("export") as st:
        errors_by_object = {i.object for i in issues if i.level == validate.ERROR}
        assets = export.collect_assets(scene)
        if len(assets) == 1:
            # AI tools give meaningless object names (tripo_node_<uuid>);
            # a single-asset file is named after the file instead.
            assets[0].label = opts.name or path.stem
        export.make_names_unique(assets)

        # Everything is written to a private staging folder first and only
        # moved into out_dir when the whole file succeeded without conflicts.
        stage = output.new_staging(out_dir)
        staged, exported = [], []
        try:
            for asset in assets:
                if opts.strict and any(o.name in errors_by_object for o in asset.objects):
                    result["skipped"].append(asset.name)
                    continue
                fbx, textures, missing = export.export_asset(
                    asset, stage, origin=opts.origin, embed_textures=opts.embed_textures,
                    texture_size=opts.texture_size, matte=opts.matte_min if opts.matte else 0.0)
                staged += [Path(fbx)] + [Path(p) for p, _, _ in textures]
                exported.append((asset, Path(fbx), textures, missing))
                for name, before, after in textures_mod.matte_log:
                    line = f"matte: {name} roughness {before:.2f} -> {after:.2f} on average"
                    st.lines.append(line)
                    result["notes"].append(line)
            moved = output.commit(staged, out_dir, batch, path)
        finally:
            shutil.rmtree(stage, ignore_errors=True)

        for asset, fbx, textures, missing in exported:
            result["exported"].append({
                "asset": asset.name,
                "kind": asset.kind,
                "triangles": asset.triangles,
                "path": str(moved[fbx]),
                "textures": [{"path": str(moved[Path(p)]), "role": r, "size": list(s)}
                             for p, r, s in textures],
                "missing_textures": missing,
            })
            tex = ""
            if textures:
                largest = max(max(s) for _, _, s in textures)
                tex = f" + {len(textures)} texture{'s' if len(textures) != 1 else ''} ({largest}px)"
                if opts.embed_textures:
                    tex += " embedded"
            st.lines.append(f"{Path(fbx).name}{tex}")
            for name in missing:
                st.lines.append(f"texture '{name}' could not be read and was not written")
        exported = len(result["exported"])
        st.detail = f"{exported} asset{'s' if exported != 1 else ''}"
        if result["skipped"]:
            st.warn(st.detail + f", skipped {len(result['skipped'])} with errors")
        elif any(e["missing_textures"] for e in result["exported"]):
            st.warn()
