# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Abdulkerim Tabakoğlu
"""Command line entry point.

Easiest: run the launcher with no arguments and answer the questions.
    b2ue.bat                      (Windows)
    ./b2ue.sh                     (macOS / Linux)

Or pass a file or folder and options:
    b2ue.bat model.glb --preset character

Without the launcher:
    blender -b -q --factory-startup -P b2ue/cli.py -- model.glb --preset prop
    python -m b2ue.cli model.glb --preset prop          (with `pip install bpy`)
"""

import argparse
import json
import shlex
import sys
import time
from pathlib import Path

# Make `import b2ue` work when Blender runs this file with -P.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from b2ue import __version__, ingest  # noqa: E402
from b2ue.console import ConsoleReporter, JsonReporter  # noqa: E402
from b2ue.options import ORIGINS, PRESET_HELP, PRESETS, build, describe  # noqa: E402
from b2ue.output import Batch, remove_stale_staging  # noqa: E402
from b2ue.pipeline import process_file  # noqa: E402

COMMANDS = {"export", "validate", "wizard"}

EXAMPLES = """\
examples:
  b2ue                                      ask questions step by step
  b2ue model.glb                            clean up and export to model's folder\\export
  b2ue model.glb --preset prop              20k tris, 2K textures
  b2ue hero.glb --preset character          humanoid: rig, keep face, 50k tris, 1.8 m
  b2ue hero.glb --human --rig --height 1.65 --target-tris 30000
  b2ue models\\ --preset mobile --out D:\\Game\\Import
  b2ue validate models\\ --max-tris 20000   check only, write nothing

presets:
""" + "\n".join(f"  {name:<11} {text}" for name, text in PRESET_HELP.items())


def _argv():
    if "--" in sys.argv:
        return sys.argv[sys.argv.index("--") + 1:]
    return sys.argv[1:]


def build_parser():
    p = argparse.ArgumentParser(
        prog="b2ue", description="Blender -> Unreal Engine 5 asset pipeline",
        epilog=EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=sorted(COMMANDS), help=argparse.SUPPRESS)
    p.add_argument("input", nargs="+",
                   help="one or more .glb/.gltf/.fbx/.obj/.blend files or folders")

    g = p.add_argument_group("asset type")
    g.add_argument("--preset", choices=sorted(PRESETS), help="start from a preset (see below)")
    g.add_argument("--human", action=argparse.BooleanOptionalAction, default=None,
                   help="the model is a humanoid character")
    g.add_argument("--rig", action=argparse.BooleanOptionalAction, default=None,
                   help="auto-rig a humanoid that has no skeleton (implies --human)")
    g.add_argument("--keep-face", action=argparse.BooleanOptionalAction, default=None,
                   help="keep more detail on the face when decimating (implies --human)")
    g.add_argument("--face-detail", type=float, metavar="X",
                   help="how many times denser the face stays (default 3)")

    g = p.add_argument_group("geometry")
    g.add_argument("--target-tris", type=int, metavar="N",
                   help="decimate meshes above N triangles down to N")
    g.add_argument("--max-tris", type=int, metavar="N",
                   help="warn above N triangles (default: the target)")
    g.add_argument("--height", type=float, metavar="M",
                   help="scale the model to this height in metres")
    g.add_argument("--origin", choices=ORIGINS,
                   help="pivot of static meshes (default: bottom)")
    g.add_argument("--triangulate", action="store_true", default=None)
    g.add_argument("--quality", choices=["high", "balanced", "light"],
                   help="pick the lowest triangle count whose shape error stays within this "
                        "level (high: 0.01%%, balanced: 0.02%%, light: 0.05%% of the size); "
                        "--target-tris is then the upper limit")
    g.add_argument("--bake-normals", action=argparse.BooleanOptionalAction, default=None,
                   help="bake the detail lost by decimation into the normal map")
    g.add_argument("--bake-size", type=int, metavar="PX",
                   help="size of the baked normal map (default 2048; 4096 needs about 2 GB "
                        "more memory)")
    g.add_argument("--measure", action=argparse.BooleanOptionalAction, default=None,
                   help="report shape and colour error after decimating (default on)")
    g.add_argument("--drop-shape-keys", action="store_true", default=None,
                   help="remove shape keys (morphs) so those meshes can be decimated; "
                        "by default such meshes are kept as they are")
    g.add_argument("--merge-distance", type=float, metavar="D")
    g.add_argument("--no-cleanup", dest="cleanup", action="store_false", default=None,
                   help="skip the cleanup step")

    g = p.add_argument_group("output")
    g.add_argument("--out", metavar="DIR",
                   help="output folder (default: an 'export' folder next to the input)")
    g.add_argument("--texture-size", type=int, metavar="PX",
                   help="downscale textures larger than PX (e.g. 2048)")
    g.add_argument("--embed-textures", action="store_true", default=None,
                   help="put textures inside the FBX instead of next to it")
    g.add_argument("--matte", action="store_true", default=None,
                   help="less shine: raise the roughness maps so the model does not look like "
                        "glossy plastic (lowest roughness --matte-min)")
    g.add_argument("--matte-min", type=float, metavar="R",
                   help="lowest roughness with --matte, 0..1 (default 0.6)")
    g.add_argument("--overwrite", action="store_true", default=None,
                   help="replace output files that already exist (source files are never replaced)")
    g.add_argument("--strict", action="store_true", default=None,
                   help="skip assets with errors and exit with code 1")
    g.add_argument("--recursive", action="store_true", help="include subfolders")
    g.add_argument("--report", metavar="FILE", help="also write a JSON report")
    g.add_argument("--verbose", action="store_true", help="show Blender's own output")
    g.add_argument("--objects", help=argparse.SUPPRESS)        # comma separated, add-on use
    g.add_argument("--name", help="name of the exported files for a single model (SM_NAME.fbx)")
    g.add_argument("--json-progress", action="store_true", help=argparse.SUPPRESS)
    g.add_argument("--image-files", help=argparse.SUPPRESS)    # add-on use
    return p


def default_out(target):
    target = Path(target)
    return (target if target.is_dir() else target.parent) / "export"


OPTION_KEYS = ("human", "rig", "keep_face", "face_detail", "target_tris", "max_tris",
               "height", "origin", "triangulate", "merge_distance", "cleanup",
               "texture_size", "embed_textures", "strict", "overwrite", "image_files",
               "drop_shape_keys", "quality", "bake_normals", "measure", "bake_size",
               "matte", "matte_min")


def run(args, reporter=None):
    extra = {}
    if getattr(args, "objects", None):
        extra["objects"] = tuple(n for n in args.objects.split(",") if n)
    if getattr(args, "name", None):
        extra["name"] = args.name
    opts = build(args.preset, **{k: getattr(args, k, None) for k in OPTION_KEYS}, **extra)
    inputs = args.input if isinstance(args.input, list) else [args.input]
    out_dir = None
    if args.command == "export":
        out_dir = Path(args.out) if args.out else default_out(inputs[0])
    files = []
    for target in inputs:
        for f in ingest.collect_inputs(target, args.recursive):
            if (out_dir is None or out_dir.resolve() not in f.resolve().parents) and f not in files:
                files.append(f)
    if reporter is None:
        reporter = (JsonReporter(verbose=args.verbose) if getattr(args, "json_progress", False)
                    else ConsoleReporter(verbose=args.verbose))
    rep = reporter
    if not files:
        rep.print(f"No .glb/.gltf/.fbx/.obj/.blend files found in {' '.join(map(str, inputs))}")
        return 2

    rep.header(__version__, len(files), out_dir, describe(opts), args.preset)
    started = time.perf_counter()
    results = []
    batch = Batch.for_files(files, overwrite=opts.overwrite)
    if out_dir is not None:
        remove_stale_staging(out_dir)
    same_name = _same_name_files(files)
    for i, path in enumerate(files, 1):
        rep.start_file(i, len(files), path)
        if path in same_name:
            result = {"file": str(path), "issues": [], "exported": [], "skipped": [],
                      "notes": [], "error": f"same file name as {same_name[path]}; "
                      "their outputs would collide, rename one or export them separately"}
            with rep.step("check") as st:
                st.status, st.detail = "fail", result["error"]
            results.append(result)
            continue
        result = process_file(path, out_dir, opts, rep, batch)
        if result["error"] and not args.verbose:
            rep.log_tail()
        results.append(result)
    rep.summary(results, time.perf_counter() - started)

    if args.report:
        Path(args.report).write_text(json.dumps(results, indent=2))
        rep.print(f"Report: {args.report}")
    failed = any(r["error"] for r in results)
    has_errors = any(i["level"] == "error" for r in results for i in r["issues"])
    return 1 if failed or (opts.strict and has_errors) else 0


def _same_name_files(files):
    """Files whose name (without extension) was already used by an earlier file
    in the batch. Their single-asset outputs would get the same name."""
    seen, clashes = {}, {}
    for f in files:
        key = f.stem.lower()
        if key in seen:
            clashes[f] = seen[key]
        else:
            seen[key] = f
    return clashes


# ---------------------------------------------------------------- wizard

def _ask(prompt, default=None, cast=str, choices=None):
    hint = f" [{default}]" if default not in (None, "") else ""
    while True:
        try:
            raw = input(f"{prompt}{hint}: ").strip().strip('"').strip("'")
        except EOFError:
            raise SystemExit("\nNo input available. Run with a file, e.g.: b2ue model.glb")
        if not raw and default is not None:
            raw = str(default)
        if not raw:
            continue
        if choices and raw.lower() not in choices:
            print(f"  please answer one of: {', '.join(choices)}")
            continue
        try:
            return cast(raw)
        except ValueError:
            print("  that does not look right, try again")


def _yes(prompt, default=True):
    answer = _ask(prompt + " (y/n)", "y" if default else "n",
                  choices=["y", "n", "yes", "no", "e", "h", "evet", "hayir"])
    return answer in ("y", "yes", "e", "evet")


def wizard():
    print(f"\nb2ue {__version__}: answer a few questions, press Enter to accept [defaults].\n")
    while True:
        target = _ask("Model file or folder (you can drag it here)")
        if Path(target).exists():
            break
        print(f"  not found: {target}")

    print("\nWhat is it?\n  1  prop / object\n  2  human character\n"
          "  3  other character or creature (no auto-rig)\n  4  scenery far from the camera\n"
          "  5  close-up hero asset\n  6  let b2ue pick the triangle count from a quality level")
    kind = _ask("Choose", "1", choices=["1", "2", "3", "4", "5", "6"])
    if kind == "6":
        level = _ask("Quality: high, balanced or light", "balanced",
                     choices=["high", "balanced", "light"])
        preset = f"auto_{level}"
    else:
        preset = {"1": "prop", "2": "character", "3": "creature", "4": "background",
                  "5": "hero"}[kind]
    values = dict(PRESETS[preset])
    argv = [target, "--preset", preset]

    if kind == "2":
        rig = _yes("Auto-rig it with an Unreal mannequin skeleton?", True)
        face = _yes("Keep extra detail on the face?", True)
        if not rig:
            argv.append("--no-rig")
        if not face:
            argv.append("--no-keep-face")
        height = _ask("Height in metres", values["height"], float)
        if height != values["height"]:
            argv += ["--height", f"{height:g}"]
    else:
        height = _ask("Real-world height in metres (0 = keep as is)", 0, float)
        if height:
            argv += ["--height", f"{height:g}"]

    tris = _ask("Triangle budget (0 = do not reduce)", values["target_tris"], int)
    if tris != values["target_tris"]:
        argv += ["--target-tris", str(tris)]
    size = _ask("Largest texture size in pixels (0 = keep)", values["texture_size"], int,
                choices=["0", "512", "1024", "2048", "4096", "8192"])
    if size != values["texture_size"]:
        argv += ["--texture-size", str(size)]
    out = _ask("Output folder", str(default_out(target)))
    if Path(out) != default_out(target):
        argv += ["--out", out]

    quote = (lambda a: f'"{a}"' if " " in a else a) if sys.platform == "win32" else shlex.quote
    print("\nNext time you can run the same thing with:")
    print("  b2ue " + " ".join(quote(a) for a in argv))
    return ["export"] + argv


# ---------------------------------------------------------------- main

def main(argv=None, reporter=None):
    argv = list(_argv() if argv is None else argv)
    if not argv or argv[0] == "wizard":
        argv = wizard()
    elif argv[0] not in COMMANDS and not argv[0].startswith("-"):
        argv = ["export"] + argv
    args = build_parser().parse_args(argv)
    try:
        return run(args, reporter)
    except ValueError as exc:  # bad option combinations
        print(f"b2ue: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
