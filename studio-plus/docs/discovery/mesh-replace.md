# Replacing a game mesh (the old Studio's "replace original"), and per-item boards

Research for `mesh replace` and the long-term "scooter as its own board item". Game build 25414733.
Everything here was read from the installed game with Studio+'s own readers (`mesh export-raw`,
`mesh find`, ported from ReSkate's GPL TOC/bundle/CAS code into `src/native/`), from the old
ReSkate Studio's `Untitled.fbproject` (the Razor Scooter, `C:\Users\lokid\Downloads\reskate-scooter\out`),
and from projects `reskate_cli cosmetic-mesh-import` writes. Nothing was verified in the running game.

## 1. What the old Studio's scooter actually replaced

`Untitled.fbproject` holds 14 resources. The mesh part is two of them:

| kind | name | bytes | note |
|---|---|---|---|
| chunk (modified) | `6a76d5b0-f8e8-556c-454e-c038e7d63973` | 259 056 | the mesh's only geometry chunk (original 336 144) |
| res (modified) | `characters/skateboard/static/static_skateboard_mesh` | 2 992 | MeshSet, same size as the original |

Both carry the user data `reskate-cosmetic-original-mesh:v1|donor=characters/skateboard/static/static_skateboard_mesh`.
The rest is a new `items/board_bottomart/own_scooter` item (a deck graphic: appearance preset, two
thumbnails) plus the item collection and `cas_main_bundlereftable` edits that register it.

**`static_skateboard_mesh` is not the board players ride.** It is the cosmetic *preview* board
(`cosmetic list` reports it as `board_preview.mesh`), and the game loads it from exactly one bundle:
`win32/levels/game/dingolevel_ftue_island/activitypresentation_ftuemegarampstep` (a tutorial
presentation). The old Studio picks it because deck items have no geometry of their own (see 4), so
"replace original" on a deck item falls back to the preview mesh. The deployed `Razor_Scooter` mod
therefore most likely changes nothing on the ride board. It also routed the scooter's four glTF
primitives onto the four sections **in file order** (Blue frame -> `Wheel_mat`, Chrome ->
`Skateboard_base_mat`, Grey -> `mat_skateboard_top`, Black -> `Truck_Mat`), emptied
`Wheel_mat_Shadow`, and skinned the blue frame to the two wheel bones (10, 11), so if it ever shows,
the frame spins with the wheels. Not verified in game.

The meshes the ride board uses (each in its own on-demand bundle `<name>_cas_main_bundlereftable`):

| part | mesh | LODs | shared by |
|---|---|---|---|
| deck | `characters/skateboard/unlicensed/deck/generic/popsicle/2022/deck_gen_popsicle_mesh` | 6 | every deck graphic (`board_bottomart`) and grip colour item |
| trucks | `characters/skateboard/unlicensed/truck/generic/default/2022/truck_gen_default_mesh` | 6 | most truck items; licensed trucks have their own (`truck_royal_theroyal_mesh`, `truck_nhs_stagexi_mesh`, `truck_thunder_default_00001_mesh`) |
| wheels | `characters/skateboard/unlicensed/wheel/generic/classic/2022/wheel_gen_classic_mesh` | 6 | every wheel colour item |

## 2. The .fbproject format (ReSkate Studio, `RSPROJT1`)

Little endian. `str` = u32 byte length + UTF-8 (no terminator).

```
char[8] "RSPROJT1"   u32 version = 2   str profile ("Skate")   u32 head (625200)
str title, author, category, version, description, link
u8[40]  zero (icon / screenshot slots, always empty in the files seen)
u32 count, then per resource:
  u32  kind        1 ebx, 2 res, 3 chunk; | 0x100 when the asset is new (added)
  str  name        asset name, or the chunk id as text
  str  user_data   ReSkate Studio tag, e.g. reskate-cosmetic-original-mesh:v1|donor=<mesh>
  u32  res_type    u64 res_rid   u64 meta_len + meta bytes        (res only, else 0)
  guid id          ebx file guid / chunk id
  guid id2         chunk id again for a chunk, else zero
  u32  range_start, range_end, logical_offset; u64 logical_size (chunk: its byte size)
  i32  h32 / first mip (-1)
  u32 n + str[n]   bundles it is in
  u32 n + str[n]   superbundles (added chunks: "win32/items")
  u32 n + str[n]   linked assets (a MeshSet lists its chunk ids, an item its presets, ...)
  u8   0
  u64  data_len + data   the payload, uncompressed
```

`project info` (reskate_cli) reads it. A guid's text form swaps the first three fields
(`b0d5766a e8f8 6c55 ...` is `6a76d5b0-f8e8-556c-...`).

## 3. The .fbmod format (Frosty binary mod, version 6), for `mod compile`

```
u64 magic 0x01005954534F5246 ("FROSTY\0\x01")   u32 version 6
i64 data_offset   i32 data_count
.NET string profile (7-bit length prefix)   u32 head
cstr title, author, category, version, description, link
i32 count, then per resource:
  u8 type (0 embedded, 1 ebx, 2 res, 3 chunk)   i32 data index (-1: none)   cstr name
  if index != -1: u8[20] sha1 of the stored (compressed) payload, i64 decoded size,
                  u8 flags (8 = added, 0 = modified), i32 handler hash (0), cstr user data
  i32 n + u32[n] bundle hashes   (hash = h=5381; h = (h*33) ^ byte, over the full bundle name)
  res:   u32 res_type, u64 res_rid, i32 meta_len + meta
  chunk: u32 range_start, range_end, logical_offset, logical_size, i32 h32, i32 first_mip,
         i32 n + i32[n] superbundle hashes
at data_offset: data_count x (i64 offset, i64 size), then the payloads
```

Payloads are Frostbite CAS block streams (8-byte header: BE u32 decoded size, u8 type, u8 flags 0x70,
BE u16 size). The old Studio writes Kraken (0x11) blocks; raw (0x00) blocks also decode. Five
"Icon"/"Screenshot0-3" embedded entries with index -1 come first. Identical payloads share an index.

## 4. What decides "one item" versus "every board"

An item (`DelMarBaseItemAsset`) lists `AssetPaths`:

* `AssetTypeId=3` an appearance preset (`..._AP`): textures and shader parameters only.
* `AssetTypeId=2` a geometry name, e.g. `Truck_Royal_TheRoyal`, resolved to
  `.../truck_royal_theroyal` (blueprint) -> `.../truck_royal_theroyal_mesh`.

Truck items carry both, so a truck item can own its geometry. Deck (`board_bottomart`), grip and
wheel colour items carry only the preset, so every one of them draws the shared deck / wheel mesh.
Replacing a mesh asset in place (what `mesh replace` does, and the old Studio's "replace original")
changes it for **every** item and every player who has the mod. A per-item scooter would need a new
geometry asset with its own bundle registered in `cas_main_bundlereftable` (what
`cosmetic import-mesh` does for costumes) and an item pointing at it with `AssetTypeId=2`. For the
truck slot that is the game's own pattern; for the deck slot the game has no item that does it, so
whether the board composer honours a geometry entry on a deck item is unknown and cannot be checked
without running the game.

## 5. The MeshSet resource (type 0x49B156D4) as far as replacing needs it

Pointers are u64 offsets relative to byte 0x10. `res_meta` = (u32 relocation table offset, u32 its
size); the relocation table lists every pointer field. A replacement keeps the resource byte for byte
in size and layout (the old Studio does too: "must retain the exact native extent") and only rewrites
counts, sizes and bounds, so pointers and the relocation table never move.

* 0x10 / 0x20: mesh bounding box (min, max as float4).
* 0x30: u64 LOD pointers; u16 LOD count at 0xB4.
* LOD (0xC0 bytes): +0x08 u32 section count, +0x0C section pointer, +0x58 u32 index buffer bytes,
  +0x5C u32 vertex buffer bytes, +0x74 geometry chunk id. Vertex buffer first, index buffer after it,
  in that chunk; chunk size = both, padded to 16.
* Section (0x180 bytes): +0x08 name pointer (the material section: `Wheel_mat`, `Truck_Mat`, ...),
  +0x10 bone palette pointer, +0x18 u16 bone count, +0x1E u8 vertex stride, +0x1F primitive type,
  +0x20 u32 triangles, start index, vertex byte offset, vertex count; +0x54 vertex byte offset again;
  +0x70 two geometry declarations (16 x {usage, format, offset, stream}, 16 stream strides, element
  and stream counts); +0x140 start index again; +0x150 / +0x160 section bounds.
* Each section stores its vertices stream by stream (all positions, then all bone indices, ...).
  Indices are u16, relative to the section's first vertex.

Board meshes use: position Half4 (w = 1), bone indices UShort4, bone weights UByte4N, UV0 and UV1
Half2, and a 4-byte packed tangent frame (usage 0x34). Board parts are skinned to a 16-bone board rig:
the deck to bone 7, trucks to 8, wheels to 10-14.

### The packed tangent frame (usage 0x34, 32 bits), decoded and confirmed

Found with `cosmetic-mesh-import` as an oracle (synthetic triangles with known frames) and checked on
the stock board (normals agree to a median dot of 0.9998) and on the old Studio's scooter (0.999999):

* bits 24-26 `face`: the normal's largest axis: 0 +X, 1 -X, 2 +Y, 3 -Y, 4 +Z, 5 -Z.
* bits 0-7 + bit 28 `u`, bits 8-15 + bit 29 `v`: 9-bit values, 256 = 0, holding the two other normal
  components times sqrt(2) x 255 (faces X: u = z, v = y; faces Y: u = x, v = z; faces Z: u = x, v = y).
* bit 27: handedness, set when the UV bitangent is opposite to cross(normal, tangent).
* bits 16-23 + bits 30-31: 10-bit angle `a` of the tangent around the normal, from a reference axis
  projected onto the tangent plane (+X face: +Z, -X: -Z, +Y: +X, -Y: -X, +Z: -X, -Z: +X):
  tangent = cos(a) r + sin(a) (normal x r), a = A x 2pi / 1024.
* The tangent is the UV0 u direction, worked out from the triangles (glTF TANGENT is ignored).

## 6. Route chosen

(a) retargeting a `cosmetic import-mesh` result does not work: it encodes a 7-stream skinned
character layout (Float3 positions, two bone sets) that differs from the board meshes' 6-stream
Half4 layout, and its sections are the donor's. (b) was done instead: Studio+ reads the target mesh
from the game, keeps its MeshSet as a template, writes the .glb geometry (an .fbx is converted with
Blender first) into its own vertex format per section (material name -> section name, else --route,
else the largest section with UVs; shadow sections, which have no UVs, get the whole model), copies
skinning from the nearest original vertex of the same section (or the section's most common bone with
--rigid), fills every LOD with the same geometry, and writes the resource and chunks as modified assets
(same names and ids) into an .fbproject or .fbmod. That is `studio-plus mesh replace`.

## 7. What was tested (2026-10-07)

* `mesh replace static_skateboard_mesh razor_scooter_deck.glb` with the old Studio's section order
  (`--route`): the same triangles per section as `Untitled.fbproject` (1940 / 3092 / 1600 / 1098, shadow
  empty); the resource differs from the old Studio's only in counts and bounds; decoding our tangent words
  gives back the glTF normals and tangents exactly (median dot 1.0). `project info` (reskate_cli) reads it.
* The scooter on the ride board: `mesh replace deck_gen_popsicle_mesh razor_scooter_deck.glb
  Razor_Scooter.fbmod --hide truck_gen_default_mesh --hide wheel_gen_classic_mesh --rigid` (119 KB .fbmod,
  2.8 MB .fbproject). `mod compile` built it (21 resources, 3 bundles, Win32\items.toc); `mod deploy`
  installed it into a fake game folder (copied Skate.exe, Data\layout.toc and initfs only). Reading the
  compiled patch back with `mesh info --game-root` shows the scooter in all 6 deck LODs and the
  one-triangle trucks and wheels. Bounds 0.32 x 0.81 x 0.61 m.
* Not verified: anything in the running game (that the deck draws, lighting, the shadow, that skinning
  to the deck bone holds the scooter still while riding, and that the hidden trucks and wheels do not upset
  board physics or animation).

