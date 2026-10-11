# Contributing to b2ue

Thank you for helping. Bug reports with a model that fails are just as welcome as code.

## Reporting a problem

Open an issue with the *Bug report* template. The most useful things to include:
- the JSON report (`--report report.json`, or *Details* in the add-on)
- the preset and options you used
- the Blender version and your OS
- the model, or a link to it, if you can share it

## Setting up

You do not need a Blender install to work on the code. The `bpy` module from PyPI is enough:

```bash
pip install bpy==5.2.2 pytest     # bpy 5.2 needs Python 3.13
python -m pytest tests
```

The full test run takes a few minutes. While working on one area, run a single file, e.g. `python -m pytest tests/test_quality.py`.

To try the add-on in Blender, build the zip and install it from disk:

```bash
python tools/build_addon.py      # writes dist/b2ue-<version>.zip
```

## Where things are

| File | What it does |
|---|---|
| `b2ue/cli.py` | Command line and the question-based wizard |
| `b2ue/addon.py` | Blender add-on panel; runs the pipeline in a background Blender |
| `b2ue/console.py` | One readable line per step in the terminal, Blender's own log moved to a file |
| `b2ue/pipeline.py` | Runs the steps for each input file and writes the report |
| `b2ue/options.py` | Options and presets |
| `b2ue/ingest.py` | Loading GLB, FBX, OBJ and .blend files |
| `b2ue/cleanup.py` | Transforms, merging, normals, custom normals |
| `b2ue/optimize.py`, `b2ue/quality.py` | Decimation and the shape, shading and colour measurements |
| `b2ue/bake.py` | Per-material normal map baking and its checks |
| `b2ue/humanoid.py`, `b2ue/rigcheck.py` | Body detection, joints, skeleton and skin weights |
| `b2ue/overlay.py` | Rig marker drawing and mirroring in the viewport |
| `b2ue/textures.py` | Texture export, resizing and the matte option |
| `b2ue/validate.py` | Checks before export |
| `b2ue/export.py`, `b2ue/output.py` | FBX export and safe, all-or-nothing output |

## Pull requests

- Keep each pull request about one thing.
- Add a test for a fix or a feature. The tests build small sample scenes in code, so no model files are needed (see `tests/humanoid_fixture.py`).
- If a change affects the results on real models, say which model and preset you tried, and what changed in the report.
- Add a line to `CHANGELOG.md` under a new *Unreleased* heading.
- The code must run in Blender's own Python. Do not add dependencies outside Blender.

## License

By contributing you agree that your contribution is licensed under GPL-3.0-or-later, like the rest of the project.
