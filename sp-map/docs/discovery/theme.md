# ReSkate ImGui UI and reusable engine code, for a standalone Studio+

Root: `/tmp/claude-1000/-home-pi5-nas-storage-projects-reskate/d583d1ac-0529-4b7b-9832-8cd5917cc033/scratchpad/ReSkate`. All paths below are relative to it.

## ImGui version and backends
- **Version:** Dear ImGui **1.91.9b** (`IMGUI_VERSION_NUM 19191`), pinned to upstream commit `f5befd2d29e66809cd1110a152e375a7f1981f06`. See `External/imgui/imgui.h:31`, `External/manifest.json` and `External/README.md`.
- **Backends:** **DX12 + Win32 only** (`External/imgui/backends/imgui_impl_dx12.*`, `imgui_impl_win32.*`). There is no DX11 backend in the tree.
- **Local patch:** `imgui_impl_dx12.cpp` is patched for a bounded, checked font upload with a 5 s GPU wait, and errors are passed back to the caller. Its hash is recorded under `local_patches` in the manifest.
- **How the launcher uses them:** it uses the `ImGui_ImplDX12_InitInfo` path with `LegacySingleSrvCpu/GpuDescriptor`. It calls `ImGui_ImplDX12_CreateDeviceObjects()` straight after init, so a failure shows up at startup instead of as a silently black window. See `Launcher/gui_renderer.cpp:105-131`.
- **Overlay:** the in-game overlay uses the same DX12/Win32 pair, hooked onto the game's own swapchain (`Extension/UI/Overlay/overlay_render.cpp:297-306`).

## Where the look is defined

**Shared palette and drawing helpers: `Extension/UI/skate_theme.h` (152 lines, header-only, depends only on imgui)**
- **Colours, lines 13-27:**
  - Tiles: `tile` 26,26,26,240, `tile_grey` 45,45,47, `tile_light` 61,62,66
  - Blues: `blue` 1,131,255, `blue_hover` 40,152,255, `blue_active` 0,110,215
  - Other: `white` 245, `grey_text` 150,152,158, `bar`/`warning` 255,177,11, `good` 46,184,92, `danger` 255,92,92, `avatar` 240,122,30
- **Hand-drawn helpers, lines 29-121:**
  - `noise`
  - `rotate_since`, which tilts already-drawn vertices (used for the slanted brush title)
  - `rough_rect`, the hand-cut tile edges
  - `scribble`
  - `status_icon` (check, busy, fail, warning)
  - `striped_bar`, the yellow RIP SCORE style meter
- **Widget colours, lines 123-141:** `push_widget_colours()` pushes a full `ImGuiCol_*` set: frame, button, header, table, scrollbar and others.
- **Primary button, lines 143-150:** `push_primary_button()` / `pop_primary_button()` give a blue button with black text.

**Launcher-specific colours: `Launcher/gui_internal.h:36-80`**
- `design_width/height` are 1440x840, and `rgba()` is a small colour helper.
- The `color::` namespace (lines 47-63) adds `background_top` 9,11,15, `background_bottom` 17,20,27, `text` 236,239,244, `muted` 128,137,151, `panel` 26,26,26 at 0.97 alpha and `outline` white at 0.12 alpha. The rest come from skate_theme.
- The `Fonts` struct is at lines 69-77.
- `g_scale` / `S()` handle DPI scaling (lines 79-80).

**Launcher style vars and ImGuiCol overrides: `Launcher/gui.cpp:264-298` (`apply_style()`)**
- All rounding is 0 and all border sizes are 0.
- `WindowPadding` is S(28,24), `FramePadding` S(12,8), `ItemSpacing` S(10,10), `ScrollbarSize` S(10).
- About 20 `ImGuiCol_*` entries are overridden, and `NavHighlight` is made transparent.

**Overlay style: `Extension/UI/Overlay/skate_menu.cpp:339-347`**
- The overlay sets its style per window with `PushStyleVar`: square corners, `FramePadding` px(10,7), `ItemSpacing` px(8,8).
- `Extension/UI/Overlay/skate_style.h` adds `halftone()` and `keycap()`.

**Fonts: `Launcher/gui.cpp:213-262`**
- **Files:** `External/fonts/Montserrat-SemiBold.ttf`, `Montserrat-ExtraBold.ttf` and `PermanentMarker-Regular.ttf` (OFL and Apache licences sit beside them).
- **Embedding:** the fonts are compiled in as RCDATA. `cmake/Apps.cmake` writes `generated/launcher_resources.rc` with entries `FONT_BODY`, `FONT_HEADING` and `FONT_BRUSH`. The same `.rc` also carries `LAUNCHER_BACKGROUND`, `LAUNCHER_ICON_MODS/SETTINGS` and icon 1 (`assets/launcher/icon.ico`).
- **Loading:** `embedded_font()` calls `FindResourceW` with RT_RCDATA (10), then `AddFontFromMemoryTTF` with `FontDataOwnedByAtlas=false`.
  - Glyph ranges: Latin plus Latin Ext, Cyrillic, General Punctuation and arrows.
  - Oversampling: `OversampleH` is 1 at 48 px and above, otherwise 2.
  - `TexGlyphPadding` is 3.
  - If a resource is missing it falls back to Segoe UI from `%WINDIR%\Fonts`.
- **Sizes at scale 1:**

| Role | Font | Size (px) |
|---|---|---|
| body | Montserrat SemiBold | 15 |
| caption | Montserrat SemiBold | 12 |
| bold | Montserrat ExtraBold | 16 |
| heading | Montserrat ExtraBold | 22 |
| tile | Montserrat ExtraBold | 30 |
| action | Montserrat ExtraBold | 50 |
| title | Permanent Marker | 86 |

- The atlas is built up front with `io.Fonts->Build()`.

## Files that can be copied near-verbatim
- **`Extension/UI/skate_theme.h`:** copies as is, with no dependencies beyond imgui.
- **`Extension/UI/Overlay/skate_style.h`:** copies as is.
- **`Launcher/gui_renderer.h` and `.cpp` (313 lines):** a self-contained D3D12 renderer. It has 2 frames in flight, a FLIP_DISCARD swapchain with a waitable object, and a hardware adapter with a WARP fallback. It includes a 160-slot texture uploader (`upload_texture` / `release_texture`), `decode_image` (WIC JPEG/PNG), `resource_bytes` and `background_bytes`. The only outside dependency is `Engine/Core/Log/logging.h`; replace those calls with a stub.
- **`Launcher/gui.cpp`, partly:**
  - Copy the widget helpers at lines 23-152: `panel_title`, `begin_panel`, `begin_page`, `tile_hit`, `nav_tile`, `badge`, `field`, `list_row`, `toggle`, `more_button`.
  - Copy the window procedure (167-211), `embedded_font`, `system_font`, `load_fonts`, `apply_style` (213-298), and the window, ImGui and renderer setup in `run()` (323-406).
  - Leave out `game_window_shown` and the `Launcher`/game-process logic in the main loop.
- **`Launcher/gui_internal.h`:** keep the "look" section (lines 36-153: colours, `Fonts`, scale, `Background`, widget declarations and the `virtual_rows` template). Leave out `Settings`, `Launcher`, `Ui`, `Store`, `Icons` and `ModsPanel`, which are tied to the launcher.
- **`Launcher/gui_home.cpp`:** `draw_background` (13-85), `draw_photo` (95-112), `page_title` (331-338) and `window_buttons` (340-354) can be lifted.
- **`External/imgui/`:** copy whole, with the patched DX12 backend.
- **`External/fonts/`:** copy whole.
- **`cmake/Apps.cmake`:** the RCDATA `.rc` generation block for the launcher's fonts and images.

## Window and chrome
- **Borderless popup window:** `CreateWindowExW(WS_EX_APPWINDOW, ..., WS_POPUP | WS_MINIMIZEBOX | WS_SYSMENU)`, centred in the work area, fixed size and not resizable (`gui.cpp:356-358`).
- **DWM attributes:** rounded corners (`DWMWA_WINDOW_CORNER_PREFERENCE`=33, value 2) and immersive dark mode (attribute 20) (`gui.cpp:360-363`).
- **Custom title bar:** `WM_NCHITTEST` returns `HTCAPTION` for the top S(44) px, except the last S(100) px on the right where the buttons are. Dragging can be turned off with `g_drag_allowed`. `SC_KEYMENU` is swallowed (`gui.cpp:181-190`).
- **Minimise and close buttons:** drawn by hand in `window_buttons` (`gui_home.cpp:340-354`), each 46x34. Close turns red (232,17,35) on hover. Full-window pages draw their own copy.
- **DPI:** the process is per-monitor V2 DPI aware. `g_scale` is the system DPI divided by 96, clamped between 0.62 and that value so the window fits 98% x 96% of the work area.
- **Layout:** every view is an ImGui window with `NoDecoration|NoMove|NoSavedSettings`, either full-screen (`begin_page`) or a centred modal (`begin_panel`).
- **Other details:** `IniFilename` is null, and drag-and-drop goes through `WM_DROPFILES`.

## Build setup
- **Top level:** `CMakeLists.txt` requires CMake 3.24 and, on Windows, MSVC x64 (otherwise it stops with a fatal error).
- **`CMakePresets.json` (version 3):**
  - Configure preset `vs2022-x64`: Visual Studio 17 2022, toolset v143, binary dir `build/vs2022-x64`, and vcpkg disabled through `VCLibPackagePath`.
  - Configure preset `linux-x64`: dedicated server only.
  - Build presets: `release` and `linux-release`.
- **`cmake/ProjectOptions.cmake`:**
  - C++20 with no extensions, static CRT (`MultiThreaded$<$<CONFIG:Debug>:Debug>`).
  - `dingosdk_configure_target()` sets `/W4 /WX /MP4 /permissive- /EHsc` and the defines `WIN32_LEAN_AND_MEAN NOMINMAX UNICODE _UNICODE`.
  - It also stamps a version resource (`templates/version.rc.in`) and generates `reskate_version.h`.
- **`cmake/Dependencies.cmake`:** every External library becomes its own static target (`dingosdk_imgui`, `_detours`, `_miniz`, `_lz4`, `_zstd`, `_sqlite`). These targets do **not** go through `dingosdk_configure_target`, so `/W4 /WX` does not apply to them. `dingosdk_imgui` links `d3d12 dxgi d3dcompiler dwmapi`.
- **Pinning:** libraries are vendored source with no package manager and no network access at build time. `External/manifest.json` records the repo URL, commit and per-file SHA-256 for each library, plus `local_patches` hashes. `External/README.md` is the human-readable table.
- **Launcher target (`cmake/Apps.cmake`):** `add_executable(... WIN32 ...)`, linking `winhttp shell32 dwmapi windowscodecs ole32`.

## Engine pieces a standalone asset browser can reuse

| Piece | Files | Links | Ties to the game process |
|---|---|---|---|
| Binary reader | `Engine/Resource/binary_io.*` | none | None |
| Bundle manifests | `Engine/Resource/binary_bundle.*` | none | None |
| Superbundle TOC read and write | `Engine/Resource/toc.*` | binary_bundle | None |
| Native DB container (layout.toc, InitFS, TOC envelope) | `Engine/Vfs/native_db.*` | none | None |
| CAS block codec | `Engine/Resource/cas_codec.*` | lz4, zstd, miniz | Oodle (kraken, selkie, leviathan) is resolved from `oo2core_*_win64.dll`. It tries `GetModuleHandleW` first (true inside Skate.exe), then falls back to `LoadLibraryExW` from `CasDecodeOptions::gameRoot`. Works standalone if `gameRoot` is set. |
| EBX | `Engine/Resource/ebx_document.*`, `ebx_writer.*`, `ebx_merge.*`, `ebx_carry.h` | binary_io | None. Reads RIFF EBX/EBXS with its own reflection chunk (REFL). |
| Textures | `Engine/Resource/texture.*` | `External/bcdec` (`BCDEC_IMPLEMENTATION`) | None. Header versions 11-13. Decodes BC1-3, BC7 and RGBA/BGRA8 to RGBA, and has a box resize. |
| Misc | `Engine/Resource/bundle_ref_table.*`, `material_grid.*`, `shader_lookup.*`, `protobuf_wire.h` | none | None |
| Game data access | `Engine/Vfs/game_archives.*` (layout.toc, then `cas_NN.cas`), `game_bundles.*` (`GameData(gameRoot)`, `read_toc`, `read_bundle`) | native_db, frostbite | Reads files on disk under the install folder only |
| Thumbnails | `Engine/Vfs/item_thumbnails.*` | game_archives, texture | Disk only. A ready-made example of the full path from bundle to texture to RGBA. |
| JSON | `Engine/Core/Json/json.*` (RapidJSON underneath) | none | None |
| Path helpers | `Engine/Core/Platform/path_text.h` | std only | None |

- **CMake targets:**
  - `dingosdk_frostbite` (`cmake/Core.cmake:67-80`) holds all of `Engine/Resource`.
  - `dingosdk_game_archives` (`Core.cmake:95-97`) holds `game_archives`, `game_bundles` and `item_thumbnails`.
  - Neither target needs the runtime, hooks or detours.

**Avoid, or treat with care:**
- **`Engine/Vfs/initfs.*`:** reads the InitFS AES key from Skate.exe *on disk* at a fixed RVA (`addr::initfs::key` from `Engine/Game/Build/addresses.h`). It only works with the one supported build, `Engine/Game/Build/20260929`, and pulls in `launcher_support` and `supported_build` (bcrypt).
- **`Engine/Vfs/world_layer_scan.*`:** pulls in `Engine/Game/World` types and content_cache.
- **Everything under `Engine/Core/Hooks`, `Engine/Game/*`, `Profiling`, `Debug` and `Console`:** runs inside the game process (detours and addresses).
- **`Engine/Core/Log`:** links console_core, shell32 and ole32. It is not tied to the game, but it is heavier than a standalone tool needs.

Several resource files say they were ported from ReSkateStudio (`sdk/frostbite/toc`, `sdk/compression`, `sdk/resources/texture_resource`), so Studio+ would be taking that code back in a cleaned-up form.