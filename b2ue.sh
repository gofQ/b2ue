#!/usr/bin/env sh
# SPDX-License-Identifier: GPL-3.0-or-later
# b2ue launcher for macOS and Linux. Set B2UE_BLENDER to choose a Blender binary.
DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -z "$B2UE_BLENDER" ]; then
  if command -v blender >/dev/null 2>&1; then
    B2UE_BLENDER=blender
  elif [ -x "/Applications/Blender.app/Contents/MacOS/Blender" ]; then
    B2UE_BLENDER="/Applications/Blender.app/Contents/MacOS/Blender"
  else
    echo "Blender was not found. Set B2UE_BLENDER to the Blender executable." >&2
    exit 2
  fi
fi
exec "$B2UE_BLENDER" -b -q --factory-startup --python-exit-code 1 -P "$DIR/b2ue/cli.py" -- "$@"
