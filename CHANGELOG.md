# Changelog

## 0.4.6

**Added**
- **Matte (less shine).** AI-generated textures often have a low roughness, so skin, leather and wood shine like plastic. `--matte` (or *Matte (less shine)* in the add-on) raises the roughness of the exported roughness maps (the G channel of ORM maps) and of material roughness values: new = amount + (1 − amount) × old, amount 0.6 by default (`--matte-min`, *Matte amount*). The texture's own variation is kept. The report gives the average before and after, for example `roughness 0.31 -> 0.72` on a Tripo character. Geometry, rig and the other textures do not change.

**Docs**
- README: the results now show only measurements of the current version: the prop, the character and the new matte comparison. How custom normals, the per-material bake and the arm tracing work is described in *How it works* and *Rigging*. Earlier measurements remain in this changelog.
- README: memory use re-measured (4.2 GB peak for a 1.9M-triangle model with 4096 px textures, 4.7 GB with a bake; the old note said about 3 GB).
- README: the planned Claude API integration (prompt to model, prompt to fix, report explanations) is described in the roadmap.

## 0.4.5

**Fixed**
- **Faceted skin after decimation.** Decimation left every face around a merged vertex with the normal of a different original vertex, so faces showed triangle-shaped patches (12.9° apart on average on a 50k character). Corners within 20° of their vertex's average are now averaged; creases stay sharp. The 20° was chosen by rendering a face and a carved prop at 0, 20, 35 and 60°.
- **T-pose shoulders inside the chest.** Sleeves that widen gradually towards the shoulder never triggered the "arm joins the torso" checks, and the shoulder joint ended up next to the neck (12 cm from the centre on a Tripo character, now 22 cm). The arm tracing now stops at the side of the chest at armpit height.

**Added**
- **Best-of skin weights.** b2ue computes its proxy weights and Blender's heat weights (both limited to 4 bones), poses both, and keeps the one that stretches the mesh less. The report names the winner and both numbers.

**Docs**
- README: a full before/after report on a 2M-triangle Tripo character. It covers both character presets, face close-ups, three test poses compared with v0.4.1 and with Blender's own automatic weights, joint placement and weight maps. The rig marker screenshot now uses this character.
- The music box report was re-run with this version.

## 0.4.4

**Fixed**
- A material's own normal map was kept as "sharper than the bake" even when the texture step would shrink it to the same size anyway (a 4096 px map exported at 2048 px next to a 2048 px bake). The comparison now uses the size that is actually exported.
- Clearer report when a bake is rejected: "bake came out noisy (8% of pixels missed the surface or face inwards), kept the original normal map". A rejected bake is no longer shown as a warning: the asset is fine and keeps its original map.

**Docs**
- README: a full before/after report on a 1.9M-triangle Tripo prop with five presets. It covers renders, close-ups, topology, shape, shading and colour error, texture sizes, texel density, file sizes and times.

## 0.4.3

**Fixed**
- **Mirror stopped working after opening a file.** Blender removes app handlers on *File → Open* unless they are marked persistent, so moving one shoulder no longer moved the other. The handler is now persistent, and a test reopens a file to check it.
- Mirroring works in both directions (move the right shoulder, the left one follows) and with several markers selected.
- Markers placed by older versions, which have no stored symmetry plane, can be mirrored too: the plane is worked out from the markers.

**Added**
- Turning **Mirror** on makes the markers symmetric at once, copying the selected marker's side.
- **Preview Result** shortens the `root` bone for display. Blender's FBX importer stretches it to the pelvis, which looks like a 1 m bone lying backwards on the floor. Unreal does not use bone lengths.

## 0.4.2

**Fixed**
- **Normal bake painted materials over each other.** Characters have several materials (skin, eyes, lashes, clothes) whose UVs share the same 0–1 square. All of them were baked into one image, which put cloth patterns on the skin and dark smears on the face.
  - Every material now gets its own baked image.
  - A material keeps its own normal map when its UVs overlap themselves, when its map is sharper than the bake size, or when the bake missed the surface on more than 3% of its pixels.
  - The report lists the decision for every material.
- **Shoulders landed on the chest, elbows beside the arm.** Arm joints are now found by tracing the centre of the arm from the fingertip to the torso. They are placed with average human proportions and centred front to back.
- The hand bone ends at the fingertips, also when the wrist is moved with a marker.

**Added**
- Rig marker overlay in the viewport, like Mixamo:
  - coloured dots (blue left, red right, yellow middle) with labels
  - a skeleton preview between the dots
- **Mirror**: moving a left marker moves the right one, and neck and pelvis stay on the middle line.
- **Front View** button; *Place Rig Markers* also turns the view to the front of the character.
- Marker depth is automatic: markers are moved to the middle of the body on export, so placing them in the front view is enough.
- Tests for per-material baking, the UV overlap check, mirroring, marker depth and arm joint placement.

## 0.4.1

- Custom normals are kept through cleanup and decimation. Reduced AI models no longer look lumpy in Solid view (fox mask: 0.42% → 0.09% difference to the original).
- The normal bake is skipped when the reduced mesh already shades within 4° of the original. Added `--bake-size` (default 2048).
- Lower memory use on 2M-triangle models (smaller texture copies for measuring, measurement data freed before baking).
- Rig markers: place, reset and clear joint markers in the add-on.
- Auto-rig limited to humans in T-pose or A-pose (arms at most 60° below horizontal).
- Fixed a rotation applied twice to rotated characters.

## 0.4.0

- Measured decimation: shape, normal and colour difference in every report.
- `auto_high`, `auto_balanced` and `auto_light` presets pick the lowest triangle count within an error limit.
- Normal map baking from the original after decimation.
- Proxy (voxel) skin weights for characters made of many pieces: tears 1.1% → 0.1%, worst stretch 38x → 8x.
- Asset name field, step checklist and clearer progress in the add-on; more presets.

## 0.3.x and earlier

- Blender add-on with background processing.
- Safe output: staging folder, no accidental overwrites, unsaved texture edits exported.
- Launcher with a question-based wizard.
- Texture export for GLB files, humanoid detection and auto-rig.
