# Blender side of old ReSkate Studio: interface map

Sources: the 5 files in `Native\Blender\` (copied to `/tmp/claude-1000/-home-pi5-nas-storage-projects-reskate/d583d1ac-0529-4b7b-9832-8cd5917cc033/scratchpad/blender-scripts/`), strings from both exes, and real headless runs on DA-PC with Blender 5.2.2 LTS. Run outputs are in `C:\dev\studio-plus-discovery\blender\` (`runtime\` holds a copy of the scripts, `scenes\test.blend` is a test scene, `out\` holds the results). I did not change anything in the Studio folder, the game folder or Blender's user config.

## 1. How Blender gets invoked

There are two Blender jobs, and both run headless.

**A. Map conversion.** Both Studio and the CLI use this:
```
blender.exe --background --factory-startup --disable-autoexec --python <dir>\studio_map_import.py -- \
  --input <scene.blend|.fbx> --output <folder> --map-name <display> --map-id <id> --pause-map <2d|3d> [--timeout-seconds <s>]
```
- **Studio.exe** passes `--timeout-seconds` and has its own outer 2 hour limit. It runs Blender inside a Win32 Job Object and reads the output through a pipe, with no console window.
- **reskate_cli** builds the same command as one string ending in ` 2>&1"`, so it runs through cmd/popen. It does not pass `--timeout-seconds`.
- Neither one passes `--scale` or `--bake-size`, so those always use their defaults.
- **Script location:** the CLI looks for `Native\Blender\studio_map_import.py` next to `reskate_cli.exe`. If missing it errors with "Studio's Blender converter (studio_map_import.py) was not found near reskate_cli".
- **Finding blender.exe (CLI):** `--blender <path>`, then the `RESKATE_BLENDER` env var, then a scan of `%ProgramW6432%`/`%ProgramFiles%\Blender Foundation\*\blender.exe`. If all fail: "Blender was not found; pass --blender <path to blender.exe> or set RESKATE_BLENDER". `--blender` must point at a file named blender.exe.
- **Finding blender.exe (Studio):** the `blenderExecutable` key in `%LOCALAPPDATA%\ReSkateStudio\settings.db`. It is currently empty.
- The CLI reads `<output>\map.json` and `conversion-report.json` afterwards. Studio fails with "Blender completed without producing map.json" if the file is not there.

**B. Add-on install.** Studio only does this, from "Install Blender Skate Map add-on":
```
blender.exe --background --disable-autoexec --python install_studio_addon.py -- \
  --addon <...>\sk8_map_export.py --module sk8_map_export --result <result.json> --vfx-catalog <catalog.json>
```
It runs without `--factory-startup` on purpose, because it has to write user prefs.

## 2. studio_map_import.py (v2.11.2) arguments

Arguments come after `--`. Without a `--` the script sees no args and argparse exits 2.

| Arg | Default | Notes |
|---|---|---|
| `--input` (required) | | `.blend` (opened with `open_mainfile load_ui=False`) or `.fbx` (factory-empty scene, then `import_scene.fbx use_image_search=True`). Nothing else is accepted. |
| `--output` (required) | | Folder. It must be absent, empty, or contain the marker file `.reskate-studio-map-import` (text `ReSkate Studio map import v1\n`). Otherwise the run fails with "output directory is not owned by Studio". The script builds into a sibling temp folder `.<name>.building-*`, then does rmtree plus `os.replace` to publish. |
| `--map-name` | input file stem | Becomes `display_name`. |
| `--map-id` | slug of map-name | `[a-z0-9_]`, max 64 chars, gets a `map_` prefix if it starts with a digit. |
| `--scale` | 1.0 | Must be finite and above 0. |
| `--bake-size` | 1024 | Size of the Cycles CPU bake for procedural Base Color. |
| `--no-procedural-bake` | | Uses a constant colour and adds a diagnostic instead of baking. |
| `--pause-map {2d,3d}` | 3d | Style of the rendered pause-menu map. |
| `--timeout-seconds` (hidden) | 7140 | Overall deadline. |
| `--pause-map-only`, `--pause-map-tiles` (4), `--pause-map-texture-limit` (CLAMP_512), `--pause-map-samples` (8), `--pause-map-untextured` (all hidden) | | Child mode. |

**Child mode.** The converter starts itself again as a second Blender (`bpy.app.binary_path`, the same flags, `CREATE_NO_WINDOW`) to render the pause map with Workbench, so a GPU crash does not kill the main run. It tries up to 4 times with lighter settings each time. Each child gets 5 minutes and the total budget is 20 minutes. The child prints `RESKATE_PAUSE_MAP_RESULT={json}`. The render is skipped when `<scene dir>\assets\<scene stem>\map.(png|jpg|jpeg|dds)` already exists.

**Scene conventions the converter reads:**
- **Spawn:** an Empty named `spawn`. Only the first is used; extra ones give the warning `multiple_spawn_markers`.
- **Travel points:** Empties named `TravelPoint*`, with custom props `bus_stop_name` and `bus_stop_shelter`.
- **Add-on data:** the add-on's PropertyGroups (`obj.sk8_object`, `mat.sk8_material`, `sk8_light`, `sk8_audio`, `sk8_npc_route`, `sk8_grind_curve`, ...).
- **Plain custom props work instead of the add-on**, which makes FBX and scripted authoring possible: `sk8_collision_mode` (or `collision_mode`), `sk8_no_collision`, `sk8_placement_mode`, `sk8_retail_blueprint`, `sk8_collision_material[_packed]`, `sk8_surface`, `sk8_surface_profile_json`, `sk8_hide_from_pause_map`, `sk8_npc_enabled`, `sk8_guide`. I verified that `sk8_collision_mode="hull"` on a cube came out as `hull`.
- **Records for lights, audio, NPC routes, prefabs, VFX and grind curves** are built by loading `sk8_map_export.py` from the same folder with importlib. This works even when the add-on is not installed.

**Exit codes:** 0 means ok. 1 means `completed_with_errors`. 2 means the run failed, and argparse usage errors also give 2.

## 3. Output folder (the "handshake")

The real handshake is written by the converter. The add-on's own Export Map button writes an older, smaller `map.json`. A real run on `test.blend` produced:
```
map.json  conversion-report.json  .reskate-studio-map-import
meshes/NNNN_<obj>__tile_NNN.glb   (render, split into 64 m cells, sections of 65535 vertices or fewer)
collision/NNNN_<obj>.glb          (collision, plus grind_<name>.glb)
textures/<name>_c|_ny|_msk-<hash>.png  (base colour, generated normal and mask)
ui/map_image.png (3840x2160)  ui/map_clouds.png  ui/map_image.json {min_x,max_x,min_z,max_z}
```
It also writes `<output>.performance.json` as a sibling outside the output folder, even when the run fails.

**`map.json` top-level keys** (`format` 1):

| Key | Contents |
|---|---|
| `name`, `display_name` | Map ID and display name. |
| `units`, `up`, `forward` | `"meters"`, `"y"`, `"-z"`. |
| `generator` | Converter and Blender version string. |
| `source` | `file`, `type`, `world_transforms_baked`, `scale`, and the render partition parameters. |
| `materials` | Keyed by name. Fields include `surface`, `texture`/`base_color_texture`, `normal_texture`, `mask_texture` (r = metallic, g = smoothness, b = AO, a = opacity), `*_srgb`, `*_factor`, `alpha`, `alpha_source`, `alpha_cutoff`, `domain`/`shader_override`, `blend_method`, `cast_shadows`, `double_sided`, `collision_material(_packed)`. |
| `surface_profiles` | Surface profiles keyed by ID. |
| `objects` | Array, described below. |
| `travel_points`, `lights`, `audio_volumes`, `audio_emitters`, `interaction_prefabs`, `trigger_effects`, `vfx_prefabs`, `npc_routes`, `reflection_probes` | Arrays of entity records. |
| `lighting` | Lighting strategy and light counts. |
| `player_size` | `[0.5, 1.85, 0.4]`. |
| `stats` | Conversion counters. |
| `warnings` | Message strings only. |
| `spawn`, `spawn_points` | `{name, position[3], yaw}` plus source info. |

**Each entry in `objects` has:**
- `name`, `placement_mode` (`authored_mesh` or `retail_blueprint`, the latter with `retail_blueprint` as an EBX path)
- `mesh`, `material`, `collision_mesh`, `render` (false means collision only)
- `transform`: 12 floats, game-space right, up, forward, translation
- `collision_mode` (`triangle_mesh`, `convex_parts`, `hull`, `none`, `water`)
- `collision_material`, `collision_material_packed`
- `world_transform_baked`, `shared_geometry`, `native_render_origin`, `surface_profile`, `grind_curve`, `round_rail`

**Coordinates:** Blender (x, y, z) becomes game (x, z, -y). Yaw is `atan2` of the marker's local +Y, so a default Empty gives yaw 180.

**`reskate_cli` validation.** Its strings show it checks this file strictly. Some of the error messages:
- "map.json must use Y-up coordinates"
- "transform must contain 12 numbers"
- "no buildable objects"
- "asset escapes its export folder"
- "streaming distances must be ordered near, medium, far"

The CLI also accepts collision modes the converter never writes: `mesh`, `convex_hull`, `capsule`, `sphere`, `aggregate`. The `behavior` keys are `exclude_from_edge_generation`, `include_in_surface_analysis`, `exclude_from_grinding`, `jump_pad`, `boost_pad`, `wipeout`, `slide`, `do_not_align`, `stairs`, `camera_occluder`.

**`conversion-report.json`** (schema 1):
- `status`: `ok` or `completed_with_errors`
- `converter_version`, `blender_version`, `input`, `output`, `map_name`, `stats`
- `warnings[]` and `errors[]`, each as `{code, message, object?...}`
- `image_dependencies`, `duration_seconds`, `performance`

## 4. Progress and result line protocols on stdout

- **Converter progress:** `RESKATE_MAP_PROGRESS={"progress":0..1,"phase":"...","message":"...","current"?:n,"total"?:n}`. Updates within a phase are throttled to one per 0.1 s.
  - Phases: `inspect`, `load`, `geometry`, `collision`, `pause_map`, `manifest`, `complete`.
  - The first line is a perf note that gives the path of the performance file.
  - The pause-map child's progress lines are passed straight through.
- **Converter result:** the final line is `RESKATE_MAP_RESULT={compact json}`. On success it is the full report. On failure it is `{"schema":1,"status":"failed","error":..,"traceback":..}`.
- **Installer:** prints `CUSTOMMAP_PROGRESS\t<amount %.3f>\t<message>` at 0.25, 0.90 and 1.0, and `RESKATE_ADDON_RESULT={json}`. Neither exe contains the string `CUSTOMMAP`, so Studio ignores those lines. It reads the `--result` JSON file instead (`installedFile`, `sourceSha256` and the related keys).
- Everything else on stdout is plain Blender output, including DeprecationWarnings and the Blender banner.

## 5. Add-on install mechanics (install_studio_addon.py)

1. Refuses unless `--module` is `sk8_map_export` and the file is named `sk8_map_export.py`.
2. Runs `compile()` on the source and takes its sha256.
3. Target is `bpy.utils.user_resource("SCRIPTS","addons")`, normally `%APPDATA%\Blender Foundation\Blender\<ver>\scripts\addons\`. A folder-based `sk8_map_export` there is a hard error.
4. Disables any existing copy, writes the file atomically, clears the `sys.modules` entry, then runs `addon_utils.enable(default_set=True, persistent=True)`. It checks that the loaded `__file__` is the new file.
5. If `--vfx-catalog` is given, it validates the catalog with the add-on's `_read_vfx_catalog`, writes it to `<CONFIG>\sk8_vfx_catalog.json`, and calls `_load_vfx_catalog()`.
6. Saves user prefs with `wm.save_userpref()`.
7. Rolls back on any failure: restores the previous add-on and catalog, then saves prefs again.
8. Writes the result JSON `{ok, module, version, sourceSha256, installedFile, blenderVersion, vfxEntries, vfxCatalogFile, vfxCatalogSha256}`, or `{ok:false, error, details}`. Exit code 0 or 1.

On DA-PC there is no `%APPDATA%\Blender Foundation` folder yet, so the add-on has never been installed. I did not run the installer because it writes Blender user prefs.

**The add-on itself** ("Skate. Map Export" v2.20.0, `bl_info` blender (3,0,0), sidebar tab View3D > Skate Map) contains:
- Operators: `sk8.export_map`, `add_spawn`, `add_bus_stop`, `add_grind_curve`, `grind_curve_from_edges`, `merge_by_material`, `apply_collision_selected`, `choose_collision_surface`, `add_audio_volume`, `add_audio_emitter`, `add_npc_route`, `add_native_behavior`, `find_native_vfx`, `import_vfx_catalog`.
- PropertyGroups on Scene, Object, Material and Light.

## 6. collision_surface_catalog.json

**Where it comes from:** format 1, taken from the Steam build's `levels/game/dingolevel_root/.../materialgrid_win32`.

**What it contains:**
- `packing`: flag mask 63, physics flag 0x20, material slot `>>6 & 0x1fff`, property slot `>>19 & 0x1fff`.
- `materialPropertyRows[116]` and `materialSoundIds[116]`.
- `intrinsicProperties`: slot to flags, for example 71 is IncludeInSurfaceAnalysis and 86 is JumpPad/Wipeout.
- `networkIdToPackedMaterial[279]`: the material declarations the game ships.

**How it is used:** only `studio_map_import.py` loads it, at import time. If the format, distribution or table sizes are wrong, the converter does not start at all.
- The 279 values with `|0x20` added are the set of packed collision materials the game accepts.
- `SMOOTH_GRIND_PACKED_BY_MATERIAL_SLOT` picks the variant with property slot 71 so round rails can be ground. It falls back to metal rail, slot 46.
- Default is 0x20. Water is 0x00F007A0.

The add-on has its own hardcoded copy of the same table (`NATIVE_COLLISION_MATERIAL_ROWS`), so there are two sources for the same data.

## 7. Blender versions

There is no version gate anywhere: Studio, the CLI and the scripts never compare versions. `bl_info` says 3.0 or later. The code handles:
- Blender 4.1+ light probes
- Blender 4.2+ `surface_render_method`
- Blender 5.0+ PropertyGroup behaviour (grind curve settings)

**On Blender 5.2.2 LTS (DA-PC) it works.** The `test.blend` conversion finished in 3.6 s with exit 0 and status ok, and included a 16-tile 2d pause map. The only noise was `DeprecationWarning: 'Material.use_nodes' is expected to be removed in Blender 6.0`, from 10 call sites. When Blender 6 drops that property, those lines will break.

Procedural bake forces Cycles on the CPU. The pause map uses Workbench.

## 8. Weak spots worth improving

1. **Exit codes run together.** Argparse usage errors and runtime failures both exit 2. Usage errors print no `RESKATE_MAP_RESULT` line at all, only text on stderr. A driver has to treat "no result line" as its own failure case.
2. **Error text is poor for some failures.** A missing input gives `"error":"C:\\...\\nope.blend"`, just the path from `FileNotFoundError`, with no code. Errors have no `code` field, unlike warnings.
3. **Side files outside `--output`.** The run writes `<output>.performance.json` beside the folder on every run, including failures, plus temporary `.building-*` folders. Anything that scans the parent folder will see them.
4. **Output ownership is fragile.** A non-empty folder without the marker is refused. A folder with the marker is wiped with rmtree when the new build is published. A wrong `--output` that happens to contain the marker is deleted.
5. **The scripts must stay together.** `studio_glbwrite.py`, `sk8_map_export.py` and the catalog are found next to the script through `__file__`. The add-on module is loaded 6 or more separate times, once per record type, with no caching except `_map_export_addon`.
6. **Two `map.json` writers drift apart.** The add-on's Export Map writes a smaller format: no name, ID, source, `world_transform_baked` or tiles, but it does write `streaming_distances`, which the converter does not. It also requires exactly one spawn, while the converter only warns. The CLI accepts both formats. Driving only the converter is safer.
7. **No machine-readable schema.** There is no JSON Schema for `map.json` or the report. The valid enums, such as collision modes, surfaces and behaviour flags, exist only in code and exe strings, and the converter's `warnings` are message strings without their codes.
8. **No dry run or validate mode.** There is no flag to just inspect a scene (objects, materials, collision modes, spawn) and print JSON without writing meshes or rendering the pause map. The pause-map render is the slowest and least reliable step, and it can only be skipped by authoring `assets/<stem>/map.png`, not with a flag. A `--no-pause-map` flag would help headless and AI use.
9. **Studio never reads the installer's progress lines.** The installer prints `CUSTOMMAP_PROGRESS`, but Studio only reads the result file. That progress output is wasted.
10. **The add-on install changes global Blender state.** It writes userpref and can change a user's other add-on settings. Conversion does not need the add-on, so a new tool can skip installing it and only offer it for authoring in the GUI.
11. **Blender discovery is fixed in two places.** Studio relies on settings.db and the CLI relies on env and Program Files. Neither supports Steam or portable installs. Neither checks the version or warns about Blender 6.
12. **Long silences in progress.** On big scenes, Cycles bakes and FBX import run synchronously and print no progress during the step. Each object is a single progress step.