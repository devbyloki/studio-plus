# Third-party code

| Folder | What | Licence | Origin |
|---|---|---|---|
| `nlohmann/` | nlohmann/json single header | MIT (`nlohmann/LICENSE.MIT`) | https://github.com/nlohmann/json |
| `imgui/` | Dear ImGui 1.91.9b (`IMGUI_VERSION_NUM 19191`), core plus the DX12 and Win32 backends | MIT (`imgui/LICENSE.txt`) | Upstream https://github.com/ocornut/imgui at `f5befd2d29e66809cd1110a152e375a7f1981f06`, copied from ReSkate's `External/imgui` |
| `fonts/` | Montserrat SemiBold and ExtraBold, Permanent Marker | SIL OFL 1.1 (`fonts/Montserrat-LICENSE.txt`), Apache 2.0 (`fonts/PermanentMarker-LICENSE.txt`) | Copied from ReSkate's `External/fonts` |

## ReSkate

`imgui/` and `fonts/` were copied unchanged from ReSkate (GPL-3.0, the same licence as Studio+):

- Repository: https://github.com/Dingo-Shenanigans/ReSkate
- Commit: `3d259667d706e76feb91b5faec2ba1e7f31c4805`

`imgui/backends/imgui_impl_dx12.cpp` carries ReSkate's local patch: the font upload checks resource
and command failures, bounds its GPU wait to five seconds, passes the failure back to the caller, and
keeps submitted resources alive on timeout. Everything else under `imgui/` is upstream as pinned.

The Studio+ look in `src/gui/` is ported from the same ReSkate commit:

- `src/gui/skate_theme.h` from `Extension/UI/skate_theme.h`
- `src/gui/skate_style.h` from `Extension/UI/Overlay/skate_style.h`
- `src/gui/renderer.*` from `Launcher/gui_renderer.*` (logging stubbed out, swapchain resize added)
- widgets, fonts, style, window procedure and background in `src/gui/look.*` and `src/gui/app.cpp`
  from `Launcher/gui.cpp`, `Launcher/gui_internal.h` and `Launcher/gui_home.cpp`

ReSkate's own brand assets (its icon and background photo) are not used. The Studio+ icon is
drawn by `scripts/make-icon.ps1`.

## ReSkate engine code (`reskate/`)

The asset browser reads the game's data with ReSkate's own engine code (GPL-3.0, `reskate/LICENSE`),
copied from the same repository and commit (`3d259667d706e76feb91b5faec2ba1e7f31c4805`), with the
folder layout kept so the files' own includes work unchanged:

| Files | What |
|---|---|
| `reskate/Engine/Resource/binary_io.*`, `binary_bundle.*`, `toc.*` | Readers for superbundle TOCs and bundle manifests |
| `reskate/Engine/Resource/cas_codec.*` | CAS block decoding; Oodle is loaded from the game folder's `oo2core_9_win64.dll` |
| `reskate/Engine/Resource/ebx_document.*`, `ebx_writer.*`, `ebx_carry.h` | EBX reader and writer |
| `reskate/Engine/Resource/texture.*` | Texture resource headers and BC decoding |
| `reskate/Engine/Vfs/native_db.*`, `game_archives.*`, `game_bundles.*` | layout.toc and the cas archives |
| `reskate/Engine/Core/Platform/path_text.h` | UTF-8 paths |
| `reskate/External/bcdec`, `lz4`, `miniz`, `zstd` | ReSkate's vendored copies, with their own licences |

One local addition, marked "Studio+ addition" in the source: `ebx::read_root_info()` in
`ebx_document.*` reads a document's file guid and root type without decoding its values, so the
asset index can type every EBX asset in seconds. Nothing else was changed.
