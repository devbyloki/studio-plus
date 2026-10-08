# ReSkate Studio+

A mod tool for **skate.** with [ReSkate](https://github.com/Dingo-Shenanigans/ReSkate): custom maps, cosmetics,
animations, mods and an asset browser in one window, in the ReSkate launcher's look. It does everything the
original ReSkate Studio did, and more. Every action is also a command for scripts or an AI assistant.

An unofficial community tool, free and open source (GPL-3.0).

## What it can do

- **Custom maps.** Drop in a `.blend` or `.fbx` and LOOK INSIDE it first: objects, collision, spawn points,
  problems. Then build it with every option explained, Quick test and Full quality presets and live progress, and
  install it.
- **Cosmetics.**
  - Browse every cosmetic in the game.
  - Put your own mesh on a new cosmetic.
  - Replace a game mesh, such as the deck, trucks or wheels.
  - Make your model **its own selectable board item** (a truck item), so other boards stay as they are.
  - Build native costumes.
- **Animations.** Find a clip, export it to FBX, edit it, put it back. You can also re-pose on-board clips for
  another ride (a scooter: facing forward, hands on the bar) with one button.
- **Project & Mods.**
  - See your installed mods the way ReSkate loads them.
  - Open ReSkate Studio projects (`.fbproject`) and export them as `.fbmod`.
  - Build and install mods.
  - **Take a mod out or put an older version back**: nothing is ever deleted.
- **Assets.** Search every asset in the game, with a folder tree. Read and edit EBX properties and save the edits
  into a mod. Preview and export textures (PNG/DDS), Lua scripts and raw files, and read meshes, sounds, videos,
  levels and clips.
- **For scripts and AI.** Every button runs a `studio-plus` command, and the ACTIVITY drawer shows its command line.
  `studio-plus.exe` runs the same commands from a terminal, and `studio-plus mcp` serves them to an AI assistant
  (MCP).

## Download

1. From the **Releases** page, download `ReSkate-Studio-Plus-<version>.zip`.
2. Unzip it into a folder of its own, for example `C:\Tools\ReSkate Studio+`. Do not put it inside the game folder,
   and keep the `blender` folder next to the `.exe` files.
3. Start **`ReSkate Studio+.exe`**.

## What you need

| | Needed for |
|---|---|
| **skate. with ReSkate installed** (the game build ReSkate supports) | everything |
| **ReSkate Studio's engine files**: `reskate_cli.exe` and its `Native` folder, from the ReSkate Studio download | building and installing mods and maps, cosmetics, animations, sounds and videos |
| **Blender** 4.2 to 5.2 | maps from `.blend` / `.fbx`, `.fbx` models, ride animations |
| Windows 10 or 11, 64-bit | |

Put the ReSkate Studio files in a folder called **`engine`** next to `ReSkate Studio+.exe`, or point SETTINGS at
wherever they are. Studio+ finds the ReSkate folder and Blender by itself in most setups.

These work without the engine files: browsing assets, editing EBX, exporting textures, replacing meshes, project
export, the mod list, and taking mods out or restoring them.

## First run

HOME checks your setup and shows what is missing, with a button to fix each. On **SETTINGS** set:

- **ReSkate folder**: where `ReSkateLauncher.exe` and `Skate.exe` are.
- **Blender**: `blender.exe`.
- **Engine folder**: the ReSkate Studio files, unless they are in `engine` next to Studio+.

Settings and caches live in `%LOCALAPPDATA%\ReSkateStudioPlus`.

## Safe by design

- Studio+ only **reads** the game. The commands that write go into the **Mods** folder only (installing, taking a
  mod out, restoring one), and the window asks first, naming the exact folder.
- Nothing is deleted. Installing over a mod keeps the old version, and taking a mod out moves it to
  `.ReSkateStudio-Mod-backup` in the game folder. RESTORE puts either back.
- Close the game before installing.
- A build is tied to your exact `Skate.exe`. After a game update, PROJECT & MODS marks old builds **outdated**:
  build them again.

## Multiplayer

Mods live on each player's own PC. Other players see your custom items, maps or animations only if they have the
same mod installed. Replacing an animation clip changes it for every rider on the screen of whoever has the mod.
A new cosmetic item, such as an own board item, only shows on players who pick it.

## Troubleshooting

| You see | Do this |
|---|---|
| `engine_missing` | Put the ReSkate Studio files in `engine` next to Studio+, or set the engine folder on SETTINGS |
| `blender_missing` | Install Blender, or set `blender.exe` on SETTINGS |
| `game_running` | Close skate. and try again |
| `game_version_mismatch`, or "outdated" | The game was updated since the build: build again |
| A mod does not load in game | PROJECT & MODS shows each mod's status: disabled, outdated, or left out by ReSkate, with the reason |
| A red line at the top of a page | Studio+ skipped part of the page instead of closing. Please report it with the page name |

When reporting a problem, open the ACTIVITY drawer at the bottom, copy the command line of the run that failed,
and include its error message.

## The command line

```
studio-plus commands                          every command
studio-plus <group> <command> --help          help for one
studio-plus mod list --json                   machine-readable output
studio-plus mcp                               serve every command to an AI assistant over MCP
```

For an MCP client, add `"studio-plus": {"command": "C:\\Tools\\ReSkate Studio+\\studio-plus.exe", "args": ["mcp"]}`
under its servers.

## Building from source

Windows, Visual Studio 2022 (C++20), CMake 3.24+ and Ninja. From a Developer PowerShell:

```powershell
cmake --preset release
cmake --build --preset release
```

`build\release` then holds both programs and the `blender` folder. The build uses `/W4 /WX`. GitHub Actions builds
every push and runs the tests in `tests\`:

- `project_export_test.py` and `mod_restore_test.py` need only `studio-plus.exe`;
- `ride_retarget_test.py` needs `pip install bpy==5.2.2`.

| Folder | |
|---|---|
| `src/core` | the command registry, settings, processes, `reskate_cli` |
| `src/commands` | every command, one file per area |
| `src/native` | Studio+'s own readers and writers: asset index, EBX, MeshSet, glTF, `.fbproject` / `.fbmod` |
| `src/gui` | the window and its pages |
| `src/cli`, `src/mcp` | the command line and the MCP server |
| `blender/` | the map converter and add-on, and `anim/` for ride animations |
| `docs/discovery/` | research notes: the original Studio's features, the engine's commands, file formats |
| `third_party/` | Dear ImGui, fonts, nlohmann/json, ReSkate's engine code (see `third_party/README.md`) |

## Credits and licence

GPL-3.0 (`LICENSE`). Studio+ builds on ReSkate's engine code and launcher look (GPL-3.0, Dingo-Shenanigans), Dear
ImGui, nlohmann/json, zstd, lz4, miniz and bcdec. It also uses the Montserrat (SIL OFL) and Permanent Marker
(Apache 2.0) fonts. Their licences are in `third_party/`, and in `licenses/` in the release zip.
