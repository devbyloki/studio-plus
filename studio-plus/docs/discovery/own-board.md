# The scooter as its own board item

Goal: a scooter players pick like any other board part, so everyone else's boards stay as they are. Today's
working mod, Razor_Scooter_v2 (`mesh replace` of `deck_gen_popsicle_mesh` with the trucks and wheels hidden),
changes the shared deck mesh, so every board becomes the scooter for everyone with the mod.

Written from the research in `mesh-replace.md` section 4, without the game at hand. **Nothing here has been
checked in the game yet**; the test steps are at the end.

## Which slot can carry a model of its own

An item (`DelMarBaseItemAsset`) lists `AssetPaths`: `AssetTypeId=3` is an appearance preset (textures),
`AssetTypeId=2` a geometry name that resolves to a mesh (`Truck_Royal_TheRoyal` ->
`.../truck_royal_theroyal_mesh`).

| Slot | Own geometry? | So |
|---|---|---|
| Trucks | yes: licensed trucks each have their own mesh | a new truck item with a new mesh is the game's own pattern |
| Deck (`board_bottomart`), grip, wheels | no: only a preset; all of them draw the one shared mesh | an item there cannot carry a model; `reskate_cli cosmetic-mesh-import` refuses them ("no item-specific geometry slot") |

So the scooter becomes a **truck item**.

## How `cosmetic new-board-part` makes it

`reskate_cli cosmetic-mesh-import` already makes new items with their own geometry asset, bundle and
`cas_main_bundlereftable` entry (that is how costumes work). For a truck donor it failed earlier only with
"Cannot route FBX mesh Razor_Scooter-2 to a native material section". It sends each material to the donor
mesh's section of the same name, and the scooter's materials (Blue, Chrome, Grey, Black) match none.
`cosmetic new-board-part` does the following:

1. Picks the truck to clone: `--donor`, else the first generic truck in `cosmetic audit` (slot `truck`, a name
   with `gen` and `default`), else the first truck.
2. Reads the donor's EBX with Studio+'s own readers, takes its `AssetTypeId=2` geometry name, and finds the
   MeshSet named after it (`<geometry>_mesh`).
3. Reads that mesh's sections and takes the largest visible one (for example `Truck_Mat`), or `--section`.
4. Writes a copy of the model to `<data>\work\new-board-part\<name>.glb` with one material named after that
   section, used by every part (meshes and mesh nodes get the name too). The geometry is unchanged; this was
   checked by loading both files in Blender 5.2.
5. Runs `cosmetic import-mesh <donor> <that copy> <output>`. The engine names the item after the file:
   `items/truck/own_<name>`.

```
studio-plus cosmetic new-board-part razor_scooter_deck.glb C:\mods\Razor_Scooter_Item.fbmod --name "Razor Scooter"
studio-plus mod compile C:\mods\stage-scooter-item C:\mods\Razor_Scooter_Item.fbmod
studio-plus mod deploy C:\mods\stage-scooter-item Razor_Scooter_Item
```

In the window: COSMETICS > OWN BOARD ITEM. Alternatively, pick a truck in BROWSE and click CLONE AS MY OWN ITEM.

## What to expect, and what is still open

- The item is in the **trucks** list. Picking it draws the scooter where the trucks go. The deck and wheels the
  player picks **still draw**, so the scooter shows on top of a skateboard deck and wheels. Hiding those for one
  item only is not possible: they are shared meshes.
- The scooter's parts all get the donor's one material section, so it takes the truck's look (its appearance
  preset). The separate colours of the model do not survive.
- **Multiplayer.** Mods live on each player's own PC; a server only passes the item each player wears.
  - Players who **have this mod too** see the scooter on whoever picked it, and normal boards on everyone else.
    That is the point of a new item rather than a replaced mesh.
  - Players **without the mod** do not have the scooter's model, so they cannot see it. What they see on you
    instead (a default truck, nothing, or a refusal) depends on how the game and ReSkate's server handle an item
    ID they do not know. That is untested.
  - Share the mod with the people you ride with.
- Remove Razor_Scooter_v2 first (PROJECT & MODS > TAKE IT OUT), or every deck is still the scooter.

## Testing on the test PC

1. Build the item as above (or in the window), then `mod compile` and `mod deploy`.
2. In game, open the board customisation and look in the **trucks** list for the new item. Check that:
   - it is listed (with the donor's thumbnail);
   - picking it shows the scooter frame on the board;
   - other trucks look normal;
   - riding and pushing still work.
3. Multiplayer, with a second player:
   - if they also install the mod, they should see the scooter on you and their own board as normal;
   - if they don't have the mod, note what they see on you, and whether the server lets you join at all.
3. If it fails, report back what happened:
   - **The import fails with "to a native material section"**: try the other section names `mesh info <donor
     mesh>` lists, with `--section`.
   - **The item is missing in game**: try another donor truck (a licensed one, such as Royal) with `--donor`.
   - **The item shows the old truck**: the engine may not have pointed the item at the new geometry. Read the
     result's `ebx[].paths`, which should show an `AssetTypeId 2` path with the new name.
