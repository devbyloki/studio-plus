# ReSkate Studio+

A rebuilt mod tool for **skate.** under [ReSkate](https://github.com/Dingo-Shenanigans/ReSkate): everything the
old ReSkate Studio did, in the ReSkate launcher's look, with every action also available from the command line
and as an MCP tool, so a script or an AI can do anything a person can.

- **`ReSkate Studio+.exe`**: the window. Custom maps, cosmetics, animations, project and mods, an asset browser,
  settings, and every command in one list. The Activity drawer shows the `studio-plus` command line for every
  action, so anything done by hand can be repeated by a script.
- **`studio-plus.exe`**: the same commands from a terminal (`studio-plus <group> <command> ... [--json]`).
- **`studio-plus mcp`**: an MCP server on stdin/stdout. Every command is a tool.

Nothing writes into the game folder except the commands marked as writing it (`mod deploy`, `mod uninstall`,
`mod restore`, `map compile --deploy`), and the window asks first, naming the exact folder.

## What it needs

| | |
|---|---|
| The ReSkate folder | where `Skate.exe` and `ReSkateLauncher.exe` are (the game, build 25414733, with ReSkate) |
| The engine | `reskate_cli.exe` and its `Native\` folder, from the ReSkate Studio zip |
| Blender | 4.2 to 5.2, for maps (`.blend` / `.fbx`) and for `.fbx` models |

Set them on the Settings page or with `studio-plus studio set game-root|engine-dir|blender <path>`;
`studio-plus studio doctor` says what is missing. Settings, caches and window state live in
`%LOCALAPPDATA%\ReSkateStudioPlus` (or the folder in `RSSP_DATA_DIR`, which tests use to stay isolated).

## The pages

| Page | What it does | Commands |
|---|---|---|
| Custom Maps | Look inside a `.blend` / `.fbx`, build it with grouped options and presets, live progress, install | `map inspect-scene`, `map compile`, `map inspect`, `mod deploy` |
| Cosmetics | Browse every cosmetic, make a new one from a mesh, replace a game mesh (decks, trucks, wheels...), native costumes | `cosmetic list/audit/import-mesh`, `mesh find/info/replace`, `costume ...` |
| Animations | Find a clip, export it to FBX, put an edited take back | `asset find`, `anim info/export/fbx-info/import` |
| Project & Mods | Installed mods as ReSkate reads them, take a mod out or restore a kept version, open a `.fbproject`, build and install | `mod list/uninstall/restore/compile/deploy/info`, `project info/export`, `game ...` |
| Assets | Index of every game asset with tree and search; EBX properties (editable, saved into a project), textures, Lua, meshes, sounds, videos, levels, clips | `asset index/search/tree/info`, `ebx get/set`, `texture export`, `mesh info`, `audio info`, `video info`, `level preview`, `anim info` |

`studio-plus commands` lists all of them; `studio-plus <group> <command> --help` explains one.

A `.fbproject` (ReSkate Studio's project format) builds like an `.fbmod`: `mod compile` exports it on the way,
and `project export` writes the `.fbmod` to share.

## Building

Windows, Visual Studio 2022 (C++20, MSVC), CMake 3.24+ and Ninja. From a Developer PowerShell:

```powershell
cmake --preset release
cmake --build --preset release
```

`build\release` then holds `studio-plus.exe`, `ReSkate Studio+.exe` and `blender\` (the map converter, used by
`map inspect-scene`). The code builds with `/W4 /WX`: warnings are errors. GitHub Actions builds both
executables on every push and runs the tests in `tests\` (they need no game):

```powershell
python tests\project_export_test.py build\release\studio-plus.exe
python tests\mod_restore_test.py build\release\studio-plus.exe
```

## Layout

| Folder | |
|---|---|
| `src/core` | the command registry, settings, running processes and `reskate_cli` |
| `src/commands` | every command, one file per area, registered in `all.cpp` |
| `src/native` | Studio+'s own readers and writers: asset index, EBX views, MeshSet, glTF, `.fbproject` / `.fbmod` |
| `src/gui` | the window: shell, pages, command runner, jobs, look |
| `src/cli`, `src/mcp` | the two other front ends over the same registry |
| `blender/` | the Blender map converter and add-on (see `blender/README.md`) |
| `docs/discovery/` | research notes: the old Studio's features, the engine's commands, formats, theme |
| `third_party/` | ImGui, fonts, nlohmann/json and ReSkate's engine code (see `third_party/README.md`) |

## Licence

GPL-3.0 (`LICENSE`), the same as ReSkate, whose engine code and look it builds on.
