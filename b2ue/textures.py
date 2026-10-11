# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Abdulkerim Tabakoğlu
"""Write the textures an asset uses next to its FBX, with Unreal-style names.

GLB files from AI tools (Tripo, Meshy, ...) carry their textures packed
inside the file. The FBX exporter cannot reference packed images, so without
this step Unreal (or any other app) gets an FBX whose materials point to
files that do not exist.
"""

import shutil
from pathlib import Path

import bpy

EXTENSIONS = {"PNG": ".png", "JPEG": ".jpg", "TARGA": ".tga", "TIFF": ".tif",
              "OPEN_EXR": ".exr", "BMP": ".bmp", "WEBP": ".webp"}


def _role(image_node):
    """Guess what a texture is for from where its color output is plugged in.

    glTF metallic-roughness maps (G = roughness, B = metallic) already match
    Unreal's ORM channel layout, so they are named ORM.
    """
    for link in image_node.outputs["Color"].links:
        node, socket = link.to_node, link.to_socket.name
        if node.type == "BSDF_PRINCIPLED":
            return {"Base Color": "BaseColor", "Roughness": "Roughness",
                    "Metallic": "Metallic", "Emission Color": "Emissive",
                    "Alpha": "Opacity"}.get(socket)
        if node.type == "NORMAL_MAP":
            return "Normal"
        if node.type in {"SEPARATE_COLOR", "SEPARATE_RGB"}:
            return "ORM"
    return None


def image_loads(image):
    """True if Blender can read the image. Reading .size loads it on demand;
    .has_data alone is False for valid images that were not loaded yet."""
    return image.has_data or image.size[0] > 0


def _material_images(materials):
    """(image, role) pairs used by the given materials, in a stable order."""
    seen, out = set(), []
    for mat in materials:
        if mat is None or not mat.use_nodes:
            continue
        for node in mat.node_tree.nodes:
            if node.type != "TEX_IMAGE" or node.image is None:
                continue
            if node.image.name in seen:
                continue
            seen.add(node.image.name)
            out.append((node.image, _role(node)))
    return out


def save_pixels(image, path):
    """Write an image's current pixels (including unsaved edits) to path
    without touching the image itself.

    Blender's image.save(filepath=...) would also clear the image's
    "unsaved changes" flag, so a user could later quit without being warned
    about their edits. Copying into a temporary image avoids that.
    Returns the path written (PNG, or OpenEXR for float images).
    """
    path = Path(path)
    w, h = image.size
    is_float = image.is_float
    path = path.with_suffix(".exr" if is_float else ".png")
    tmp = bpy.data.images.new("b2ue_tmp_save", w, h, alpha=True, float_buffer=is_float)
    try:
        import numpy as np
        # colour space first: changing it later regenerates the buffer
        tmp.colorspace_settings.name = image.colorspace_settings.name
        tmp.alpha_mode = image.alpha_mode
        pixels = np.empty(w * h * 4, dtype=np.float32)
        image.pixels.foreach_get(pixels)
        tmp.pixels.foreach_set(pixels)
        tmp.update()
        tmp.file_format = "OPEN_EXR" if is_float else "PNG"
        tmp.filepath_raw = str(path)
        tmp.save()
    finally:
        bpy.data.images.remove(tmp)
    return path


def _write_image(image, out_dir, stem):
    """Write the image as out_dir/stem.<ext>. Returns the path, or None.

    Order matters: pixels edited in Blender (and never saved) win over the
    packed or on-disk bytes, which still hold the old picture. Untouched
    images are copied byte for byte, without re-encoding.
    """
    edited = image.is_dirty or image.source == "GENERATED"
    if edited and image_loads(image):
        return save_pixels(image, out_dir / stem)
    ext = EXTENSIONS.get(image.file_format, ".png")
    path = out_dir / f"{stem}{ext}"
    if image.packed_file is not None:
        path.write_bytes(image.packed_file.data)
        return path
    source = Path(bpy.path.abspath(image.filepath)) if image.filepath else None
    if source is not None and source.is_file():
        shutil.copyfile(source, path)
        return path
    if image_loads(image):  # in memory only
        return save_pixels(image, out_dir / stem)
    return None


def _downscale(path, max_size):
    """Shrink an image file in place so its longest side is max_size.
    Returns the final (width, height)."""
    img = bpy.data.images.load(str(path), check_existing=False)
    try:
        w, h = img.size
        if max_size and max(w, h) > max_size:
            f = max_size / max(w, h)
            w, h = max(1, round(w * f)), max(1, round(h * f))
            img.scale(w, h)
            img.save(filepath=str(path), quality=92)
        return w, h
    finally:
        bpy.data.images.remove(img)


MATTE_MIN = 0.6   # default lowest roughness with --matte
# (file name, mean roughness before, after) for every map changed by the last
# export_textures() call, for the report
matte_log = []


def _matte_file(path, role, floor):
    """Remap a written roughness map so nothing is glossier than floor:
    r' = floor + (1 - floor) * r. The map's own detail (scratches, worn
    edges) stays, only shifted towards matte. ORM maps change in G only.
    Returns (mean roughness before, after)."""
    import numpy as np
    img = bpy.data.images.load(str(path), check_existing=False)
    try:
        img.colorspace_settings.name = "Non-Color"
        w, h = img.size
        px = np.empty(w * h * 4, dtype=np.float32)
        img.pixels.foreach_get(px)
        px = px.reshape(-1, 4)
        channels = [1] if role == "ORM" else [0, 1, 2]
        before = float(px[:, channels].mean())
        px[:, channels] = floor + (1.0 - floor) * px[:, channels]
        after = float(px[:, channels].mean())
        img.pixels.foreach_set(px.ravel())
        img.update()
        img.save(filepath=str(path), quality=92)
        return before, after
    finally:
        bpy.data.images.remove(img)


def matte_materials(materials, floor):
    """Roughness values typed into materials (no texture) get the same floor.
    Returns how many were raised."""
    raised = 0
    for mat in materials:
        if mat is None or not mat.use_nodes:
            continue
        for node in mat.node_tree.nodes:
            if node.type == "BSDF_PRINCIPLED":
                rough = node.inputs["Roughness"]
                if not rough.is_linked and rough.default_value < floor:
                    rough.default_value = floor + (1.0 - floor) * rough.default_value
                    raised += 1
    return raised


def export_textures(asset_objects, out_dir, asset_name, max_size=0, matte=0.0):
    """Write every texture used by the asset's materials to out_dir and point
    the images at the written files, so the FBX references real files.
    Textures larger than max_size (longest side, 0 = keep) are downscaled.
    matte (0 = off) is the lowest roughness of the written roughness maps:
    AI models often come out glossy, like plastic toys in sunlight.

    Returns a list of (written path, role, (width, height)) and a list of
    missing image names.
    """
    out_dir = Path(out_dir)
    materials = sorted({slot.material for obj in asset_objects if obj.type == "MESH"
                        for slot in obj.material_slots if slot.material},
                       key=lambda m: m.name)
    written, missing, used_names = [], [], set()
    matte_log.clear()
    if matte:
        matte_materials(materials, matte)

    for image, role in _material_images(materials):
        stem = f"T_{asset_name}_{role or bpy.path.clean_name(Path(image.name).stem)}"
        name, i = stem, 2
        while name in used_names:
            name, i = f"{stem}_{i}", i + 1
        used_names.add(name)

        out_dir.mkdir(parents=True, exist_ok=True)
        path = _write_image(image, out_dir, name)
        if path is None:
            missing.append(image.name)
            continue
        # Point the in-memory image at the written file (the source file on
        # disk is never touched) so the FBX gets a valid relative path.
        if image.packed_file is not None:
            image.unpack(method="REMOVE")
        size = _downscale(path, max_size)
        if matte and role in ("Roughness", "ORM"):
            matte_log.append((Path(path).name, *_matte_file(path, role, matte)))
        image.filepath = str(path)
        image.filepath_raw = str(path)
        image.reload()
        written.append((str(path), role or "Other", size))
    return written, missing
