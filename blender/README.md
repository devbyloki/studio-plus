# Blender scripts

The Blender side of map building. `reskate_cli.exe compile-map` runs `studio_map_import.py` headless
to turn a `.blend` or `.fbx` scene into a normalized map folder (`map.json`, meshes, collision,
textures, pause-map picture), then compiles that folder into a Skate level.

## Origin

Copied from ReSkate Studio's `Native\Blender\` folder (the ReSkate Studio zip, files dated
2026-09-17 to 2026-10-05):

| File | Version | What it is |
|---|---|---|
| `studio_map_import.py` | converter 2.11.2 | The converter the engine calls. |
| `sk8_map_export.py` | add-on 2.20.0 | The Blender add-on; the converter also loads it for light, audio, NPC, VFX and grind records. |
| `studio_glbwrite.py` | | Minimal GLB writer used by the converter. |
| `collision_surface_catalog.json` | format 1 | Collision materials the game accepts. |
| `install_studio_addon.py` | | Installs the add-on into the user's Blender. |

The first commit holds the files exactly as shipped. Later commits change them; see the git log
and `docs/discovery/blender.md` for the weak spots they address.

## What changed from the shipped converter (2.11.2 to 2.12.0)

What reskate_cli relies on is unchanged: the arguments it passes (`--input --output --map-name
--map-id --pause-map`), the `RESKATE_MAP_PROGRESS=` lines, `map.json` and `conversion-report.json`
in `--output`, and exit codes 0 (ok) and 1 (completed with errors). A test conversion gives the same
`map.json` as 2.11.2 apart from the version strings.

- **Exit codes.** 0 ok, 1 completed with errors, 2 failed, 3 usage error (argparse used to share 2),
  4 Blender too old. `--describe` prints all of them, and every error code, as JSON.
- **A result line, always.** Every run ends with one `RESKATE_MAP_RESULT={...}` line, usage errors
  included, with `mode`, `status`, `exit_code` and, on failure, `error_code` plus `errors:
  [{code, message, ...}]`. `error` stays the plain message string older drivers read. `--result
  <file>` also writes it to a file (even when the other arguments do not parse).
- **Coded errors.** `input_not_found`, `input_unsupported`, `input_load_failed`, `invalid_option`,
  `output_not_directory`, `output_not_owned`, `output_unsafe`, `output_write_failed`,
  `blender_unsupported`, `runtime_incomplete` (a sibling file missing or broken), `usage_error`,
  `internal_error` (with a traceback).
- **Nothing outside `--output`.** The build happens in `<output>\.building-*`, and the timings are
  `<output>\performance.json` (they used to be `<output>.performance.json` beside it).
- **Safer output ownership.** `--output` is refused when it is a drive root, the home folder, a link
  or junction, or holds the input. The marker is written before anything else goes in. Publishing
  replaces only the entries the converter writes (`map.json`, `conversion-report.json`, `meshes`,
  `collision`, `textures`, `ui`); anything else in the folder is left alone and named in the
  `output_has_other_files` warning. It no longer deletes the whole folder.
- **Readable output.** The stage is made with `mkdir`, not `tempfile.mkdtemp`, which since Python
  3.13 (Blender 5) gives an owner-only ACL: an elevated run left a map an unelevated process could
  not read.
- **`--inspect` / `--dry-run`.** Opens the scene and reports, without writing anything and without
  `--output`: objects (placement and collision mode, triangles, materials), materials (surface,
  base colour kind, invisible), spawn and travel points, light, audio, NPC, prefab, probe and grind
  curve counts, collision and placement mode totals, bounds in game space, and how the pause map
  would be made.
- **`--no-pause-map`**, or `RESKATE_MAP_NO_PAUSE_MAP=1` in the environment (which reaches the
  converter through reskate_cli), skips the pause-map render, the slowest step. The map keeps San
  Van's pause map.
- **Progress.** A "Still in <phase>" line every 5 s (was 30 s) during long silent steps, a line before
  each Cycles bake, and the bar now moves through the pause-map tiles instead of sitting at 93%.
- **Blender versions.** Below 3.6 fails with `blender_unsupported` and a plain message; outside the
  tested 4.2 to 5.2 range it warns `blender_untested`. `Material.use_nodes` (deprecated in 5.0, gone
  in 6.0) is no longer read on Blender 5, here and in the add-on, so Blender 5.2 runs without
  deprecation warnings.
- The add-on module is loaded once for all record types instead of once per type.

Run `blender --background --factory-startup --python studio_map_import.py -- --describe` for the
contract as JSON.

## Deploying them

Studio+ itself runs `studio_map_import.py --inspect` from the `blender\` folder the build copies beside
`studio-plus.exe` (`map inspect-scene`), so looking inside a scene works whatever converter the engine has.
Building still goes through the engine's own copy:

The engine looks for `Native\Blender\studio_map_import.py` next to `reskate_cli.exe`, and the
scripts find each other through `__file__`, so copy the whole folder over `Native\Blender\` of
the engine folder.
