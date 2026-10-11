# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Abdulkerim Tabakoğlu
"""All pipeline settings in one place, plus presets for common asset types."""

from dataclasses import dataclass, fields, replace

ORIGINS = ("bottom", "center", "object", "keep")


@dataclass
class Options:
    # what the asset is
    human: bool = False          # an upright humanoid character
    rig: bool = False            # auto-rig humanoids that have no skeleton
    keep_face: bool = False      # keep more triangles on the face when decimating
    face_detail: float = 3.0     # how many times denser the face stays

    # geometry
    target_tris: int = 0         # decimate meshes above this count (0 = off)
    max_tris: int = 0            # warn above this count (0 = same as target)
    height: float = 0.0          # scale to this height in metres (0 = keep)
    origin: str = "bottom"       # pivot of static meshes, see ORIGINS
    cleanup: bool = True
    merge_distance: float = 1e-5
    triangulate: bool = False
    drop_shape_keys: bool = False  # remove shape keys so those meshes can be decimated
    quality: str = ""            # "", high, balanced, light: lowest tris within that error
    bake_normals: bool = False   # bake lost detail into the normal map after decimating
    bake_size: int = 2048        # baked normal map size; 4096 needs about 2 GB more memory
    measure: bool = True         # report shape/texture error after decimating

    # textures and output
    texture_size: int = 0        # downscale textures larger than this (0 = keep)
    matte: bool = False          # raise roughness so the model does not look like glossy plastic
    matte_min: float = 0.6       # lowest roughness with matte (0 = mirror, 1 = fully matte)
    embed_textures: bool = False
    strict: bool = False         # skip assets with errors
    overwrite: bool = False      # replace existing output files (never sources)

    # used by the Blender add-on
    objects: tuple = ()          # only process these objects (by name)
    name: str = ""               # file name for a single exported asset
    image_files: str = ""        # JSON {image name: file} with unsaved image edits

    def __post_init__(self):
        if self.rig or self.keep_face:
            self.human = True
        if self.quality not in ("", "high", "balanced", "light"):
            raise ValueError("quality must be high, balanced or light")
        if not 0.0 <= self.matte_min <= 1.0:
            raise ValueError("matte_min must be between 0 and 1")
        if self.origin not in ORIGINS:
            raise ValueError(f"origin must be one of {', '.join(ORIGINS)}")

    @property
    def tri_warning(self):
        return self.max_tris or self.target_tris


PRESETS = {
    # fixed budgets
    "prop": dict(target_tris=20_000, texture_size=2048, bake_normals=True),
    "hero": dict(target_tris=100_000, texture_size=4096, bake_normals=True),
    "background": dict(target_tris=3_000, texture_size=1024),
    "mobile": dict(target_tris=8_000, texture_size=1024, bake_normals=True),
    # characters
    "character": dict(human=True, rig=True, keep_face=True, target_tris=50_000,
                      height=1.8, texture_size=2048, bake_normals=True),
    "mobile_character": dict(human=True, rig=True, keep_face=True, target_tris=15_000,
                             height=1.8, texture_size=1024, bake_normals=True),
    "creature": dict(target_tris=60_000, texture_size=2048, bake_normals=True),
    # measured: the lowest triangle count whose shape error stays under a limit
    "auto_high": dict(quality="high", target_tris=300_000, texture_size=4096, bake_normals=True),
    "auto_balanced": dict(quality="balanced", target_tris=150_000, texture_size=2048,
                          bake_normals=True),
    "auto_light": dict(quality="light", target_tris=60_000, texture_size=2048, bake_normals=True),
}

PRESET_HELP = {
    "prop": "everyday objects: 20k tris, 2K textures, baked normals",
    "hero": "close-up assets: 100k tris, 4K textures, baked normals",
    "background": "far away scenery: 3k tris, 1K textures",
    "mobile": "mobile games: 8k tris, 1K textures, baked normals",
    "character": "humanoid: auto-rig, face kept, 50k tris, 1.8 m, 2K textures, baked normals",
    "mobile_character": "humanoid for mobile: auto-rig, 15k tris, 1.8 m, 1K textures",
    "creature": "animals, monsters, non-human characters: 60k tris, no auto-rig",
    "auto_high": "fewest tris with shape error under 0.01% (p95), 4K textures",
    "auto_balanced": "fewest tris with shape error under 0.02% (p95), 2K textures",
    "auto_light": "fewest tris with shape error under 0.05% (p95), 2K textures",
}


def build(preset=None, **overrides):
    """Options from a preset, then explicit values on top (None = not given)."""
    base = dict(PRESETS.get(preset, {})) if preset else {}
    base.update({k: v for k, v in overrides.items() if v is not None})
    valid = {f.name for f in fields(Options)}
    unknown = set(base) - valid
    if unknown:
        raise ValueError(f"unknown option(s): {', '.join(sorted(unknown))}")
    return Options(**base)


def describe(opts):
    """Short human readable list of the settings that are switched on."""
    parts = []
    if opts.human:
        parts.append("humanoid")
        if opts.rig:
            parts.append("auto-rig")
        if opts.keep_face:
            parts.append(f"face x{opts.face_detail:g}")
    if opts.quality:
        parts.append(f"quality {opts.quality}"
                     + (f" (max {opts.target_tris:,} tris)" if opts.target_tris else ""))
    elif opts.target_tris:
        parts.append(f"{opts.target_tris:,} tris")
    if opts.bake_normals:
        parts.append("baked normals")
    if opts.height:
        parts.append(f"{opts.height:g} m")
    if opts.texture_size:
        parts.append(f"textures <= {opts.texture_size}px")
    if opts.matte:
        parts.append(f"matte (roughness >= {opts.matte_min:g})")
    if opts.origin != "bottom":
        parts.append(f"origin {opts.origin}")
    if opts.embed_textures:
        parts.append("embedded textures")
    return ", ".join(parts) or "clean up and export only"


__all__ = ["Options", "PRESETS", "PRESET_HELP", "ORIGINS", "build", "describe", "replace"]
