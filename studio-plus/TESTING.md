# Testing Studio+ on your PC

## Setting up

1. Unzip `studio-plus-windows.zip` into a folder of its own, for example `C:\dev\studio-plus-test`.
   Keep `blender\` next to the two `.exe` files.
2. Close the game. Start `ReSkate Studio+.exe`.
3. On SETTINGS, set:
   - the ReSkate folder (where `ReSkateLauncher.exe` and `Skate.exe` are);
   - Blender (`blender.exe`, 5.2);
   - the engine folder (the ReSkate Studio zip folder with `reskate_cli.exe`).

   HOME should then show everything green.

Every button runs a `studio-plus` command, and the ACTIVITY drawer at the bottom shows each one's command line.
If something goes wrong, the command line and the error message are what to send back.

## 1. The scooter as its own item (multiplayer-friendly)

1. **Take the old scooter out first.** In PROJECT & MODS, open Razor_Scooter_v2 in the mod list and click TAKE IT
   OUT. Otherwise every deck is still the scooter. It is kept as a backup, and RESTORE puts it back.
2. In COSMETICS, open OWN BOARD ITEM:
   - **Model:** `C:\Users\lokid\Downloads\reskate-scooter\out\razor_scooter_deck.glb`
   - **Item name:** `Razor Scooter`
   - **Truck to clone:** leave it empty. Finding one takes about a minute.

   Click MAKE THE ITEM, then BUILD AND INSTALL IN PROJECT & MODS, then BUILD, then INSTALL as `Razor_Scooter_Item`.
3. In game, look in the board customisation's **trucks** list for the new item. Check that:
   - picking it shows the scooter;
   - other trucks are normal;
   - riding and pushing work.

   The deck and wheels you pick still draw too.
4. Multiplayer, with a friend:
   - if they install the same mod, they should see the scooter on you and nothing else changed;
   - if they don't have it, note what they see on you.

More detail and what to try if it fails: `docs/discovery/own-board.md` in the repository.

## 2. Scooter riding animations (for riding on your own)

These replace clips, so **every rider you see** rides like this while the mod is installed. Leave it out for
multiplayer.

1. In ANIMATIONS step 1, find on-board clips (`onb`, `push`). For each idle, rolling and push clip of your stance,
   click ADD TO THE RIDE LIST. Start with `animation/dingo/c_proto_onb_push_regular_medium_full_static`.
2. **Test mod 1.** In step 5, turn on "Same clips", then MAKE THE TEST MOD, build and install it. In game, riding
   should look exactly as before. Then take it out again.
3. **Test mod 2.** In step 5:
   - turn "Same clips" off and "Preview pictures" on;
   - set the scooter model;
   - click MAKE THE RIDE MOD.

   Open the previews first: the rider should face the bar with both hands on it. Then build and install it with
   the scooter item.
4. If a clip fails with `bones_not_found`, the game's bone names need adding to `blender\anim\scooter.json`. Send
   back the error, which lists what was missing.

More detail: `docs/discovery/scooter-anim.md`.

## 3. The rest (quick checks)

| Page | Try |
|---|---|
| CUSTOM MAPS | Drop a `.blend` and click LOOK INSIDE. Check that the INSTALLED list shows your maps. |
| PROJECT & MODS | Open a `.fbproject`, click EXPORT .FBMOD, and add the project itself to the build list. Check that RESTORE appears beside kept versions. |
| ASSETS | Pick a mesh, a sound and a clip, and check that each gets its own tab. Edit a value on PROPERTIES, then SAVE INTO PROJECT... |
| COSMETICS | REPLACE A GAME MESH: title and author fields, "Hide the deck" |

Anything that installs only writes into the Mods folder, and asks first. Nothing is deleted: taking a mod out
keeps it as a backup.
