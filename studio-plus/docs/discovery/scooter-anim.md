# Scooter riding animations

Goal: on the scooter, the rider stands facing forward with both hands on the grips, the front foot on the deck,
and pushes with the back foot. Only standing, rolling and pushing matter (no tricks).

Written without the game at hand. The Blender side was tested on a stand-in skeleton (below); **nothing here has
been checked on the game's own clips or in game yet**.

## The pipeline: `studio-plus anim retarget-ride`

For each clip:

1. `anim export <clip>` writes the game's clip as an FBX (reskate_cli `animation-export`).
2. `blender/anim/ride_retarget.py` re-poses it in Blender with the targets in `blender/anim/scooter.json`.
3. `anim import <clip> <re-posed fbx>` puts it back (reskate_cli `animation-import`). Every clip after the first
   is added to the same project (`--project`).

At the end the project is written as `--output`: an `.fbmod` via `project export`, or the `.fbproject` itself.

```
studio-plus anim retarget-ride animation/dingo/c_proto_onb_push_regular_medium_full_static ^
    --output C:\mods\scooter_stance.fbmod --model razor_scooter_deck.glb --preview-dir C:\mods\previews
studio-plus anim retarget-ride <same clips> --output C:\mods\roundtrip.fbmod --passthrough
```

In the window, use ANIMATIONS: add clips to the ride list in step 1, then use step 5.

`--passthrough` sends the clips through Blender unchanged (test mod 1). If the game looks exactly as without the
mod, the export, Blender round trip and import are sound, and any difference in the re-posed mod comes from the
re-pose.

Clips known from earlier work: `animation/dingo/c_proto_onb_push_regular_medium_full_static` exports fine.
`subt_c_proto_onb_push_regular_medium_loop_02` is a subtractive controller and the engine refuses it. Find
others with `studio-plus asset find onb` (on board) and pick the idle, rolling and push clips for the rider's
stance (`regular` = left foot forward).

**Replacing a clip changes it for every rider, on the screen of whoever installs the mod**, skateboarders
included. In multiplayer that means: with this mod installed, every player you see rides in scooter stance; players
without it see everyone, you included, riding normally. So it does not fit "only the scooter rider looks like a
scooter rider". That would need the game to switch animations when the scooter item is worn, which is a change to
ReSkate itself (a fork), not a mod. Use the stance mod for riding on your own, and the scooter item
(`own-board.md`) for multiplayer.

## What ride_retarget.py does to a clip

Everything is worked out from the clip itself:

- **The board.** Its forward axis runs from the back foot to the lead foot on the first frame. Its height comes
  from the ankles (minus `ankle_height` and `skate_deck_top`). It follows the board bone (a bone named like
  `skateboard` / `board` / `deck`), else the root, through the clip, so root motion and board lean carry over.
- **Facing.** The hips turn about the board's up axis by the angle that makes the toes, which point across a
  skateboard, point along the board (90° on the stand-in). The spine, arms and head turn with them.
- **Reach.** The upper body leans forward, up to `lean_max_degrees`, and the hips drop, up to `hips_drop_max`,
  just enough for both arms to reach the grips. A warning says so if even that is not enough.
- **Hands.** IK on the forearm takes the wrist to each grip, plus `hand_offset`. The grips are measured on
  `--model` (the two ends of the topmost bar), else taken from the targets.
- **Feet.** The front foot is pinned on the deck. The back foot stands on the deck too, except on the frames
  where the clip itself takes it off the skateboard (a push). There it keeps the clip's own path along the
  ground, eased over `push_blend_frames`.
- **Knees and elbows.** Pole targets bend the knees forward and the elbows out and back. The pole angle is found
  per joint by trying the four quarter turns, because bone roll differs between skeletons.
- **Unchanged.** Timing, root motion and the motion of everything not mentioned (spine, head, bob).
- **Export.** The FBX goes out with the same skeleton, names and frame range. Keys are shifted one frame back
  first, because Blender's FBX import and export disagree by one frame, so timing is kept exactly.

Bones are found by common names (`pelvis`/`hips`, `l_hand`/`lefthand`, `l_calf`/`leftleg`...). The lower arm and
leg default to the hand's and foot's parents. The game's names are not known yet. If a run fails with
`bones_not_found`, list them and name them in the targets:

```
blender --background --factory-startup --python ride_retarget.py -- --input clip.fbx --list-bones
```

```json
"bones": {"pelvis": "Hips", "hand_l": "LeftHand", "hand_r": "RightHand", "foot_l": "LeftFoot", "foot_r": "RightFoot"}
```

## Tested here, on a stand-in

With Blender 5.2.2 (`bpy` from PyPI), the test was a 23-bone stand-in rig standing sideways on a board that
rolls 1.5 m forward. It had an idle clip and a push clip whose back foot steps off, sweeps back along the
ground and steps on again. The scooter was a box model with a 0.32 m bar at 0.79 m.
`tests/ride_retarget_test.py` repeats this.

- The hips turned 90°. The grips measured at (±0.13, 0.79, 0.24), exactly as built. All 30 push frames were found.
- Hands, front foot and back foot all ended on their targets (0.0 m worst miss).
- On re-import the bone names, hierarchy and frame range were identical, and the root and board motion was
  unchanged. `--passthrough` changed nothing (0.0 m on every bone).
- The previews show the rider facing the bar, leaning in, with hands on the grips, the front knee forward over
  the deck, and the back foot pushing behind.

## Testing on DA-PC

1. **Test mod 1.** Run the same clips with `--passthrough`, then `mod compile` and `mod deploy`. In game,
   standing, rolling and pushing should look exactly as without the mod.
2. **Test mod 2 ("scooter stance").** Run the clips with `--model razor_scooter_deck.glb --preview-dir ...`, and
   look at the previews first. Then compile and install alongside the scooter mod. In game, check that:
   - the rider faces forward with their hands on the bar;
   - the front foot is on the deck;
   - the push foot pushes behind.
3. If it looks wrong, report back what you saw, and adjust `scooter.json` to suit:
   - **Hands short of the bar:** the warning will say so. Raise `lean_max_degrees` / `hips_drop_max`.
   - **The rider faces backwards:** use `--lead-foot right`, or a goofy clip.
   - **Feet off the deck:** check `deck_top` / `front_foot` / `back_foot` against the model.
