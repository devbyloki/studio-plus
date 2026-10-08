# Changelog

## 0.9.0 (first public beta)

Everything the original ReSkate Studio did, rebuilt with the ReSkate launcher's look, plus the following.

**Maps**
- LOOK INSIDE a `.blend` / `.fbx` before building: objects, collision, materials, spawn and travel points, lights,
  grind curves, size and problems (`map inspect-scene`).
- Every build option grouped and explained, with Quick test and Full quality presets. A build has live progress,
  cancel and a result summary. A "None" pause-map choice skips the slowest step.
- The improved map converter (2.12): clear exit codes and error codes, never writes outside its output folder,
  `--inspect`, and Blender 5.2 support.

**Cosmetics**
- Browse every cosmetic, with filters.
- New cosmetic from your mesh; replace a game mesh, with title, author, scale and hide options.
- **Own board item** (`cosmetic new-board-part`): your model as a new truck item that only shows on players who
  pick it.
- Native costumes.

**Animations**
- Find, read, export and re-import clips.
- **Ride animations** (`anim retarget-ride`): re-poses on-board clips for another ride, such as a scooter: facing
  forward, hands on the bar, pushing. It comes with a no-change test mode and preview pictures.

**Project & Mods**
- Installed mods read exactly as ReSkate loads them: order, status and reasons.
- Open `.fbproject` files and export them as `.fbmod` (`project export`). `mod compile` takes projects directly.
- Build and install with a confirmation that names the target folder. **TAKE IT OUT** (`mod uninstall`) and
  **RESTORE** (`mod restore`) mean nothing is ever deleted.
- Health checks for the game install.

**Assets**
- An index of every asset in the game, with a folder tree and fast search.
- EBX properties you can edit and save into a project or mod (`ebx set --output x.fbproject`).
- Texture preview and PNG/DDS export, Lua source, raw export.
- Views for meshes, sounds, videos, levels and clips, with hand-offs to Cosmetics and Animations.

**Everywhere**
- Every action is a `studio-plus` command, also served to AI assistants over MCP. ACTIVITY shows the command line
  of every run.
- A page that hits an error skips the broken part instead of closing the window.

**Known limits**
- No 3D mesh viewer, animation player, sound or video playback, or texture import yet.
- Own board items are trucks: the deck and wheels a player picks still draw with them.
- Replacing an animation clip changes it for every rider on the screen of whoever has the mod.
