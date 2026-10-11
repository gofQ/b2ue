# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Abdulkerim Tabakoğlu
"""Safe output: files are written to a private staging folder first and only
moved into the output folder when everything for a source file succeeded and
no file would be overwritten by accident.

Rules:
  * a source file is never overwritten, not even with overwrite=True
  * two sources in the same run may not write the same output file
  * existing files are only replaced with overwrite=True
  * a failed or cancelled file leaves the output folder as it was
"""

import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

STAGING_PREFIX = ".b2ue-staging-"


class OutputConflict(Exception):
    """Writing the outputs would overwrite something it must not."""


def _key(path):
    """Comparable form of a path (Windows paths are case-insensitive)."""
    return os.path.normcase(str(Path(path).resolve()))


@dataclass
class Batch:
    """What one run has to protect and has already written."""
    sources: set = field(default_factory=set)   # keys of every input file
    claimed: dict = field(default_factory=dict)  # output key -> source that wrote it
    overwrite: bool = False

    @classmethod
    def for_files(cls, files, overwrite=False):
        return cls(sources={_key(f) for f in files}, overwrite=overwrite)


def new_staging(out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stage = out_dir / f"{STAGING_PREFIX}{os.getpid()}-{uuid.uuid4().hex[:8]}"
    stage.mkdir()
    return stage


def remove_stale_staging(out_dir, older_than=3600):
    """Delete staging folders left behind by runs that were killed."""
    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        return 0
    removed = 0
    for path in out_dir.glob(STAGING_PREFIX + "*"):
        try:
            if path.is_dir() and time.time() - path.stat().st_mtime > older_than:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            pass
    return removed


def plan(staged_files, out_dir, batch, source):
    """Map staged files to their final paths and list every conflict."""
    out_dir = Path(out_dir)
    targets, problems = {}, []
    for staged in staged_files:
        target = out_dir / Path(staged).name
        key = _key(target)
        if key in batch.sources:
            problems.append(f"{target.name} would overwrite a source file ({target})")
        elif key in batch.claimed:
            problems.append(f"{target.name} was already written in this run by "
                            f"{Path(batch.claimed[key]).name}; rename one of the sources")
        elif target.exists() and not batch.overwrite:
            problems.append(f"{target.name} already exists in {out_dir} "
                            f"(use --overwrite to replace it)")
        targets[Path(staged)] = target
    return targets, problems


def commit(staged_files, out_dir, batch, source):
    """Move staged files into out_dir, or raise OutputConflict and move nothing."""
    targets, problems = plan(staged_files, out_dir, batch, source)
    if problems:
        raise OutputConflict("; ".join(problems))
    for staged, target in targets.items():
        os.replace(staged, target)
        batch.claimed[_key(target)] = str(source)
    return targets
