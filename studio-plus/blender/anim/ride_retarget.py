"""Re-poses a skate. on-board animation clip for another ride (a scooter, a BMX...), in Blender.

    blender --background --factory-startup --python ride_retarget.py -- \
        --input clip.fbx --output clip_ride.fbx --targets scooter.json [--model scooter.glb] [--preview DIR]
    ... -- --input clip.fbx --list-bones          print the skeleton as JSON and stop
    ... -- --input clip.fbx --output out.fbx --passthrough   import and export unchanged (tests the round trip)

The clip is an FBX from `studio-plus anim export`. What changes, frame by frame:
  * the hips turn about the board's up axis so the rider faces along the board instead of across it;
  * both hands reach the grips (IK), at positions from the targets JSON or measured from --model;
  * the front foot stands on the deck; the back foot stands on the deck too, except while the clip itself
    lifts it off the board (a push), where it keeps the clip's own path along the ground;
  * timing, root motion and everything else (spine, head, lean, bob) are kept.
The board is found from the clip: its forward axis from the feet (front foot = the lead foot), its height from
the ankles, and it follows the board bone (or the root) through the clip.

Targets are in the ride model's space, as the game uses for boards: metres, Y up, Z forward along the board,
X to the rider's left, origin on the ground under the board centre. See scooter.json.

Every run ends with one line RESKATE_RIDE_RESULT={...} (status, exit_code, error_code on failure, what was
found and done); progress lines are RESKATE_RIDE_PROGRESS={...}. Exit codes: 0 ok, 2 failed, 3 usage.
"""
import argparse
import json
import math
import os
import sys
import traceback

import bpy
from mathutils import Matrix, Vector

VERSION = "1.0.0"
EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 2, 3


class RideError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def progress(fraction, message):
    print("RESKATE_RIDE_PROGRESS=" + json.dumps({"progress": round(fraction, 3), "message": message}), flush=True)


# ---------------------------------------------------------------- arguments

def arguments(argv):
    p = argparse.ArgumentParser(prog="ride_retarget.py", description="Re-pose a skate clip for another ride")
    p.add_argument("--input", required=True, help="Clip FBX from studio-plus anim export")
    p.add_argument("--output", help="FBX to write (not needed with --list-bones)")
    p.add_argument("--targets", help="Targets JSON (grips, feet, bone names); see scooter.json")
    p.add_argument("--model", help="The ride's model (.glb/.fbx): grips measured from it, and shown in previews")
    p.add_argument("--preview", help="Folder for preview images (start, middle and end frames)")
    p.add_argument("--lead-foot", choices=("left", "right"), help="Front foot when the clip does not show it (default: targets, else left)")
    p.add_argument("--list-bones", action="store_true", help="Print the skeleton and stop")
    p.add_argument("--passthrough", action="store_true", help="Export the clip unchanged (round-trip test)")
    p.add_argument("--result", help="Also write the result JSON to this file")
    return p.parse_args(argv)


# ---------------------------------------------------------------- scene

def clear_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def import_clip(path):
    if not os.path.isfile(path):
        raise RideError("input_not_found", f"Clip FBX not found: {path}")
    before = set(bpy.data.objects)
    bpy.ops.import_scene.fbx(filepath=path, use_anim=True, ignore_leaf_bones=False,
                             automatic_bone_orientation=False, axis_forward="-Z", axis_up="Y")
    armatures = [o for o in bpy.data.objects if o not in before and o.type == "ARMATURE"]
    if not armatures:
        raise RideError("no_armature", "The FBX has no skeleton")
    arm = max(armatures, key=lambda o: len(o.data.bones))
    if not arm.animation_data or not arm.animation_data.action:
        raise RideError("no_animation", "The FBX skeleton has no animation take")
    return arm


def frame_range(arm):
    start, end = arm.animation_data.action.frame_range
    return int(round(start)), int(round(end))


def action_fcurves(action):
    # Blender 4.4+ keeps keys in layers / strips / channel bags; older versions on the action itself.
    if getattr(action, "layers", None):
        for layer in action.layers:
            for strip in layer.strips:
                for bag in getattr(strip, "channelbags", []):
                    yield from bag.fcurves
    elif hasattr(action, "fcurves"):
        yield from action.fcurves


def shift_keys(action, frames):
    for fc in action_fcurves(action):
        for k in fc.keyframe_points:
            k.co.x += frames
            k.handle_left.x += frames
            k.handle_right.x += frames
        fc.update()


def export_clip(arm, path):
    # Blender's FBX import puts time 0 on frame 1 but its export writes frame f at time f: one frame earlier on
    # the way out keeps the clip's timing exactly as it came in.
    shift_keys(arm.animation_data.action, -1)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    bpy.ops.object.select_all(action="DESELECT")
    keep = [arm] + [c for c in arm.children_recursive if c.type == "MESH"]
    for o in keep:
        o.select_set(True)
    bpy.context.view_layer.objects.active = arm
    start, end = frame_range(arm)
    bpy.context.scene.frame_start, bpy.context.scene.frame_end = start, end
    bpy.ops.export_scene.fbx(filepath=path, use_selection=True, object_types={"ARMATURE", "MESH"},
                             add_leaf_bones=False, bake_anim=True, bake_anim_use_all_actions=False,
                             bake_anim_use_nla_strips=False, bake_anim_force_startend_keying=True,
                             bake_anim_simplify_factor=0.0, axis_forward="-Z", axis_up="Y",
                             primary_bone_axis="Y", secondary_bone_axis="X", armature_nodetype="NULL")


# ---------------------------------------------------------------- the skeleton

DEFAULT_BONES = {
    # Name candidates per role, tried in order: exact (case-insensitive), then "contains".
    "root": ["root", "trajectory", "reference"],
    "board": ["skateboard", "board", "deck"],
    "pelvis": ["pelvis", "hips", "hip", "spine0"],
    "hand_l": ["lefthand", "l_hand", "hand_l", "lhand", "hand.l", "l hand", "leftwrist", "wrist_l"],
    "hand_r": ["righthand", "r_hand", "hand_r", "rhand", "hand.r", "r hand", "rightwrist", "wrist_r"],
    "forearm_l": ["leftforearm", "l_forearm", "forearm_l", "leftlowerarm", "lowerarm_l", "l_lowerarm", "leftelbow", "l_elbow"],
    "forearm_r": ["rightforearm", "r_forearm", "forearm_r", "rightlowerarm", "lowerarm_r", "r_lowerarm", "rightelbow", "r_elbow"],
    "foot_l": ["leftfoot", "l_foot", "foot_l", "lfoot", "foot.l", "leftankle", "ankle_l", "l_ankle"],
    "foot_r": ["rightfoot", "r_foot", "foot_r", "rfoot", "foot.r", "rightankle", "ankle_r", "r_ankle"],
    "shin_l": ["leftleg", "l_calf", "calf_l", "leftshin", "shin_l", "l_shin", "leftlowerleg", "lowerleg_l", "l_knee", "leftknee"],
    "shin_r": ["rightleg", "r_calf", "calf_r", "rightshin", "shin_r", "r_shin", "rightlowerleg", "lowerleg_r", "r_knee", "rightknee"],
    "toe_l": ["lefttoebase", "lefttoe", "l_toe", "toe_l", "l_ball", "ball_l"],
    "toe_r": ["righttoebase", "righttoe", "r_toe", "toe_r", "r_ball", "ball_r"],
    "head": ["head"],
}
REQUIRED = ("pelvis", "hand_l", "hand_r", "foot_l", "foot_r")


def find_bones(arm, overrides):
    names = {b.name.lower(): b.name for b in arm.data.bones}
    found = {}
    for role, candidates in DEFAULT_BONES.items():
        wanted = overrides.get(role)
        options = ([wanted] if isinstance(wanted, str) else list(wanted or [])) + candidates
        hit = next((names[c.lower()] for c in options if c and c.lower() in names), None)
        if hit is None:
            for c in options:
                if not c:
                    continue
                matches = [n for low, n in names.items() if c.lower() in low and "twist" not in low and "end" not in low]
                if matches:
                    hit = min(matches, key=len)
                    break
        if hit:
            found[role] = hit
    # The lower arm / leg is the hand's / foot's parent when no name matched.
    for side in ("l", "r"):
        for part, child in (("forearm", "hand"), ("shin", "foot")):
            role = f"{part}_{side}"
            if role not in found and f"{child}_{side}" in found:
                parent = arm.data.bones[found[f"{child}_{side}"]].parent
                if parent:
                    found[role] = parent.name
    if "root" not in found:
        found["root"] = next(b.name for b in arm.data.bones if b.parent is None)
    missing = [r for r in REQUIRED if r not in found]
    if missing:
        raise RideError("bones_not_found", "Could not find the " + ", ".join(missing) + " bone(s); name them under "
                        "\"bones\" in the targets JSON (list them with --list-bones)", missing=missing)
    return found


def list_bones(arm):
    start, _ = frame_range(arm)
    bpy.context.scene.frame_set(start)
    unit = unit_of(arm)
    rows = []
    for pb in arm.pose.bones:
        head = arm.matrix_world @ pb.head
        rows.append({"name": pb.name, "parent": pb.parent.name if pb.parent else None,
                     "head_m": [round(v * unit, 3) for v in head], "length_m": round(pb.length * unit, 3)})
    return rows


def unit_of(arm):
    # Metres per scene unit: a skeleton exported in centimetres is ~170 units tall.
    start, _ = frame_range(arm)
    bpy.context.scene.frame_set(start)
    zs = [(arm.matrix_world @ pb.head).z for pb in arm.pose.bones]
    height = (max(zs) - min(zs)) if zs else 1.0
    return 0.01 if height > 10 else 1.0


# ---------------------------------------------------------------- the ride model

def load_model(path):
    if not path:
        return None
    if not os.path.isfile(path):
        raise RideError("model_not_found", f"Ride model not found: {path}")
    before = set(bpy.data.objects)
    if path.lower().endswith(".fbx"):
        bpy.ops.import_scene.fbx(filepath=path)
    else:
        bpy.ops.import_scene.gltf(filepath=path)
    meshes = [o for o in bpy.data.objects if o not in before and o.type == "MESH"]
    if not meshes:
        raise RideError("model_empty", f"No mesh in {path}")
    return meshes


def model_points(meshes):
    # Back from Blender (Z up) to the model's own space (Y up, Z forward): (x, y, z) = (bx, bz, -by).
    pts = []
    for o in meshes:
        for v in o.data.vertices:
            w = o.matrix_world @ v.co
            pts.append(Vector((w.x, w.z, -w.y)))
    return pts


def measure_grips(meshes, inset):
    pts = model_points(meshes)
    top = max(p.y for p in pts)
    bar = [p for p in pts if p.y > top - 0.06]
    reach = max(abs(p.x) for p in bar)
    out = {}
    for side, sign in (("left", 1), ("right", -1)):
        end = [p for p in bar if p.x * sign > reach * 0.6]
        if not end:
            raise RideError("grips_not_measured", "Could not find the grips at the top of the model; give \"grips\" in the targets JSON")
        c = sum(end, Vector()) / len(end)
        c.x -= sign * inset  # the hand holds the grip a little in from the end
        out[side] = [round(c.x, 3), round(c.y, 3), round(c.z, 3)]
    return out


# ---------------------------------------------------------------- the board, frame by frame

def world_head(arm, bone):
    return arm.matrix_world @ arm.pose.bones[bone].head


def world_tail(arm, bone):
    return arm.matrix_world @ arm.pose.bones[bone].tail


def horizontal(v):
    return Vector((v.x, v.y, 0.0))


def board_frames(arm, bones, start, end, unit, targets, lead):
    """The board's frame (origin on the ground under its centre, X left, Y up, Z forward) for every frame."""
    scene = bpy.context.scene
    scene.frame_set(start)
    up = Vector((0, 0, 1))
    feet = {s: world_head(arm, bones[f"foot_{s}"]) for s in ("l", "r")}
    front = "l" if lead == "left" else "r"
    back = "r" if front == "l" else "l"
    forward = horizontal(feet[front] - feet[back])
    if forward.length < 1e-6:
        raise RideError("feet_together", "The feet are on top of each other in the first frame; cannot tell the board's direction")
    forward.normalize()
    left = up.cross(forward)
    ankle = targets.get("ankle_height", 0.08) / unit
    skate_deck = targets.get("skate_deck_top", 0.11) / unit
    centre = (feet["l"] + feet["r"]) / 2
    origin = Vector((centre.x, centre.y, min(feet["l"].z, feet["r"].z) - ankle - skate_deck))
    board0 = Matrix((left.to_4d(), up.to_4d(), forward.to_4d(), origin.to_4d())).transposed()
    board0[3][3] = 1.0
    for i in range(3):
        board0[3][i] = 0.0
    follow = bones.get("board") or bones["root"]

    def follow_matrix():
        return arm.matrix_world @ arm.pose.bones[follow].matrix

    f0 = follow_matrix()
    frames = {}
    for f in range(start, end + 1):
        scene.frame_set(f)
        frames[f] = follow_matrix() @ f0.inverted() @ board0
    return frames, front, back


def to_world(board, p, unit):
    return board @ (Vector(p) / unit)


def on_deck(board, ankle_point, unit, deck):
    """Whether an ankle is over the deck: inside its outline (5 cm slack) and no lower than standing on it."""
    local = board.inverted() @ ankle_point
    return abs(local.x) * unit < deck["half_width"] + 0.05 and abs(local.z) * unit < deck["half_length"] + 0.05 \
        and local.y * unit > deck["top"] + deck["ankle"] - 0.04


# ---------------------------------------------------------------- re-posing

def facing_of(arm, bones, side):
    # Toes point where the rider faces: foot -> toe, else along the foot bone.
    if f"toe_{side}" in bones:
        d = world_tail(arm, bones[f"toe_{side}"]) - world_head(arm, bones[f"foot_{side}"])
    else:
        d = world_tail(arm, bones[f"foot_{side}"]) - world_head(arm, bones[f"foot_{side}"])
    return horizontal(d)


def signed_angle(a, b):
    a, b = a.normalized(), b.normalized()
    return math.atan2(a.cross(b).z, a.dot(b))


def hips_move(board, pivot, angle, lean, offset):
    """The world move of the hips: turn about the board's up axis, lean forward about its left axis, then shift
    by `offset` (board space, scene units), all about the hips' own position `pivot`."""
    axes = board.to_3x3()
    up = (axes @ Vector((0, 1, 0))).normalized()
    left = (axes @ Vector((1, 0, 0))).normalized()
    return (Matrix.Translation(pivot + axes @ offset) @ Matrix.Rotation(lean, 4, left) @ Matrix.Rotation(angle, 4, up)
            @ Matrix.Translation(-pivot))


def solve_reach(arm, bones, board, angle, offset, grip_points, targets):
    """The forward lean (radians) and hip drop (scene units) that let both arms reach the grips, smallest first:
    lean up to lean_max_degrees, then drop the hips up to hips_drop_max. Read on the current frame."""
    pivot = world_head(arm, bones["pelvis"])
    shoulders, lengths = {}, {}
    for side in ("l", "r"):
        forearm = arm.pose.bones[bones[f"forearm_{side}"]]
        upper = forearm.parent
        shoulders[side] = arm.matrix_world @ (upper.head if upper else forearm.head)
        lengths[side] = forearm.length + (upper.length if upper else 0.0)

    def shortfall(lean, drop):
        move = hips_move(board, pivot, angle, lean, offset + Vector((0, -drop, 0)))
        return max((move @ shoulders[s] - grip_points[s]).length - 0.97 * lengths[s] for s in ("l", "r"))

    lean_max = math.radians(targets.get("lean_max_degrees", 25))
    lean = 0.0
    while shortfall(lean, 0.0) > 0 and lean < lean_max:
        lean = min(lean_max, lean + math.radians(1))
    drop = 0.0
    drop_max = targets.get("hips_drop_max", 0.15) / unit_of(arm)
    while shortfall(lean, drop) > 0 and drop < drop_max:
        drop = min(drop_max, drop + 0.005 / unit_of(arm))
    return lean, drop, shortfall(lean, drop)


def turn_hips(arm, bones, frames, start, end, angle, lean, offset):
    """Moves the hips by hips_move on every frame; everything above and below them follows."""
    scene = bpy.context.scene
    pelvis = arm.pose.bones[bones["pelvis"]]
    pelvis.rotation_mode = "QUATERNION"
    inv = arm.matrix_world.inverted()
    poses = {}
    for f in range(start, end + 1):
        scene.frame_set(f)
        m = arm.matrix_world @ pelvis.matrix
        poses[f] = inv @ hips_move(frames[f], m.translation, angle, lean, offset) @ m
    for f, mat in poses.items():
        scene.frame_set(f)
        pelvis.matrix = mat
        pelvis.keyframe_insert("rotation_quaternion", frame=f, group=pelvis.name)
        pelvis.keyframe_insert("location", frame=f, group=pelvis.name)


def make_target(name, positions):
    empty = bpy.data.objects.new(name, None)
    bpy.context.scene.collection.objects.link(empty)
    for f, p in positions.items():
        empty.location = p
        empty.keyframe_insert("location", frame=f)
    return empty


def add_ik(arm, bone, target, chain):
    c = arm.pose.bones[bone].constraints.new("IK")
    c.target = target
    c.chain_count = chain
    c.use_tail = True
    c.use_stretch = False
    return c


def aim_joint(arm, constraint, joint_bone, pole, frame, start_point, end_point, wanted):
    """Gives the IK a pole so the middle joint (knee, elbow) bends towards `wanted`. Bone roll differs between
    skeletons, so the pole angle that does it is found by trying the four quarter turns on one frame."""
    constraint.pole_target = pole
    scene = bpy.context.scene
    best, best_score = 0.0, -1e9
    for degrees in (0, 90, -90, 180):
        constraint.pole_angle = math.radians(degrees)
        scene.frame_set(frame)
        joint = world_head(arm, joint_bone)
        mid = (start_point + end_point) / 2
        score = (joint - mid).dot(wanted)
        if score > best_score:
            best, best_score = constraint.pole_angle, score
    constraint.pole_angle = best
    return math.degrees(best)


def bake(arm, start, end):
    bpy.ops.object.select_all(action="DESELECT")
    arm.select_set(True)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="POSE")
    bpy.ops.pose.select_all(action="SELECT")
    bpy.ops.nla.bake(frame_start=start, frame_end=end, only_selected=True, visual_keying=True,
                     clear_constraints=True, use_current_action=True, bake_types={"POSE"})
    bpy.ops.object.mode_set(mode="OBJECT")


def retarget(arm, bones, targets, model_meshes, unit, lead, report):
    start, end = frame_range(arm)
    scene = bpy.context.scene
    ankle = targets.get("ankle_height", 0.08)
    deck = {"top": targets.get("deck_top", 0.1), "half_width": targets.get("deck_width", 0.12) / 2,
            "half_length": targets.get("deck_length", 0.5) / 2, "ankle": ankle}
    progress(0.2, "Finding the board")
    frames, front, back = board_frames(arm, bones, start, end, unit, targets, lead)

    # Where the feet and the clip's own back foot are, before anything moves.
    original_back = {}
    original_front = {}
    for f in range(start, end + 1):
        scene.frame_set(f)
        original_back[f] = world_head(arm, bones[f"foot_{back}"])
        original_front[f] = world_head(arm, bones[f"foot_{front}"])

    # Face along the board: the toes point across it on a skateboard.
    scene.frame_set(start)
    facing = (facing_of(arm, bones, "l") + facing_of(arm, bones, "r"))
    board_forward = frames[start].to_3x3() @ Vector((0, 0, 1))
    angle = signed_angle(facing, horizontal(board_forward)) if facing.length > 1e-6 else 0.0
    offset = Vector(targets.get("hips_offset", [0.0, 0.0, -0.08])) / unit

    # Grips: measured on the model when there is one (unless measure_grips is false), else from the targets.
    grips = targets.get("grips")
    if model_meshes and targets.get("measure_grips", True):
        grips = measure_grips(model_meshes, targets.get("grip_inset", 0.03))
        report["grips_measured"] = grips
    if not grips:
        raise RideError("grips_missing", "Give \"grips\" in the targets JSON or a --model to measure them on")
    report["grips"] = grips
    hand_offset = targets.get("hand_offset", [0.0, 0.03, 0.0])  # wrist above the grip centre

    # Lean forward (and crouch if that is not enough) so the arms reach the bar, then turn and move the hips.
    scene.frame_set(start)
    grip_start = {s: to_world(frames[start], [grips[k][i] + hand_offset[i] for i in range(3)], unit)
                  for s, k in (("l", "left"), ("r", "right"))}
    lean, drop, short = solve_reach(arm, bones, frames[start], angle, offset, grip_start, targets)
    offset = offset + Vector((0, -drop, 0))
    progress(0.35, f"Turning the hips {math.degrees(angle):.0f} degrees, leaning {math.degrees(lean):.0f}")
    turn_hips(arm, bones, frames, start, end, angle, lean, offset)
    report["hips_turned_degrees"] = round(math.degrees(angle), 1)
    report["lean_degrees"] = round(math.degrees(lean), 1)
    report["hips_drop_m"] = round(drop * unit, 3)
    if short > 0:
        report.setdefault("warnings", []).append(
            f"The arms are {short * unit:.2f} m short of the grips even leaning {math.degrees(lean):.0f} degrees and "
            f"crouching {drop * unit:.2f} m: raise lean_max_degrees / hips_drop_max or lower the grips")
    foot_front = targets.get("front_foot", [0.0, deck["top"], 0.12])
    foot_back = targets.get("back_foot", [0.0, deck["top"], -0.12])

    def deck_point(p):
        return [p[0], p[1] + ankle, p[2]]

    hand_pos = {"l": {}, "r": {}}
    foot_pos = {front: {}, back: {}}
    pushing = 0
    blend = {}
    frames_list = list(range(start, end + 1))
    # The skateboard the clip was made on: its deck is where the clip's feet stand.
    skateboard = {"top": targets.get("skate_deck_top", 0.11), "half_width": 0.105, "half_length": 0.41, "ankle": ankle}
    off = {f: not on_deck(frames[f], original_back[f], unit, skateboard) for f in frames_list}
    # Ease between the deck and the clip's own path over a few frames, so the foot does not jump.
    ease = int(targets.get("push_blend_frames", 4))
    for f in frames_list:
        near = [abs(f - g) for g in frames_list if off[g]]
        d = min(near) if near else ease + 1
        blend[f] = 1.0 if off[f] else max(0.0, 1.0 - d / max(1, ease + 1))
        pushing += 1 if off[f] else 0
    for f in frames_list:
        b = frames[f]
        for side, key in (("l", "left"), ("r", "right")):
            g = grips[key]
            hand_pos[side][f] = to_world(b, [g[0] + hand_offset[0], g[1] + hand_offset[1], g[2] + hand_offset[2]], unit)
        foot_pos[front][f] = to_world(b, deck_point(foot_front), unit)
        on = to_world(b, deck_point(foot_back), unit)
        foot_pos[back][f] = on.lerp(original_back[f], blend[f])
    report["push_frames"] = pushing
    report["frames"] = len(frames_list)

    progress(0.55, "Reaching for the grips and the deck")
    # Where the hips and shoulders are once turned, for the knee and elbow poles.
    hips, shoulders = {"l": {}, "r": {}}, {"l": {}, "r": {}}
    for f in frames_list:
        scene.frame_set(f)
        for side in ("l", "r"):
            shin = arm.pose.bones[bones[f"shin_{side}"]]
            forearm = arm.pose.bones[bones[f"forearm_{side}"]]
            hips[side][f] = arm.matrix_world @ (shin.parent.head if shin.parent else shin.head)
            shoulders[side][f] = arm.matrix_world @ (forearm.parent.head if forearm.parent else forearm.head)
    empties = []
    report["pole_angles"] = {}
    for side, sign in (("l", 1), ("r", -1)):
        hand = make_target(f"ride_hand_{side}", hand_pos[side])
        foot = make_target(f"ride_foot_{side}", foot_pos[side])
        empties += [hand, foot]
        arm_ik = add_ik(arm, bones[f"forearm_{side}"], hand, int(targets.get("arm_chain", 2)))
        leg_ik = add_ik(arm, bones[f"shin_{side}"], foot, int(targets.get("leg_chain", 2)))
        if targets.get("knee_poles", True):
            # Knees bend forward, along the board.
            ahead = {f: (hips[side][f] + foot_pos[side][f]) / 2 + frames[f].to_3x3() @ (Vector((0, 0, 0.6)) / unit)
                     for f in frames_list}
            pole = make_target(f"ride_knee_{side}", ahead)
            empties.append(pole)
            report["pole_angles"][f"knee_{side}"] = aim_joint(
                arm, leg_ik, bones[f"shin_{side}"], pole, start, hips[side][start], foot_pos[side][start],
                frames[start].to_3x3() @ Vector((0, 0, 1)))
        if targets.get("elbow_poles", True):
            # Elbows out to the side and a little back, as when holding a handlebar.
            out = Vector((0.5 * sign, 0, -0.3))
            behind = {f: (shoulders[side][f] + hand_pos[side][f]) / 2 + frames[f].to_3x3() @ (out / unit) for f in frames_list}
            pole = make_target(f"ride_elbow_{side}", behind)
            empties.append(pole)
            report["pole_angles"][f"elbow_{side}"] = aim_joint(
                arm, arm_ik, bones[f"forearm_{side}"], pole, start, shoulders[side][start], hand_pos[side][start],
                frames[start].to_3x3() @ out.normalized())
    progress(0.7, "Baking")
    bake(arm, start, end)
    for e in empties:
        bpy.data.objects.remove(e, do_unlink=True)

    # How close the result came, for the report.
    def reach(bone, targets_by_frame):
        worst = 0.0
        for f in frames_list[:: max(1, len(frames_list) // 20)]:
            scene.frame_set(f)
            worst = max(worst, (world_head(arm, bone) - targets_by_frame[f]).length * unit)
        return round(worst, 3)
    report["worst_miss_m"] = {
        "hand_l": reach(bones["hand_l"], hand_pos["l"]), "hand_r": reach(bones["hand_r"], hand_pos["r"]),
        "front_foot": reach(bones[f"foot_{front}"], foot_pos[front]),
        "back_foot": reach(bones[f"foot_{back}"], foot_pos[back]),
    }
    report["front_foot"] = "left" if front == "l" else "right"
    return frames


# ---------------------------------------------------------------- previews

def previews(arm, folder, frames, model_meshes, unit, start, end):
    os.makedirs(folder, exist_ok=True)
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 8
    scene.cycles.device = "CPU"
    scene.render.resolution_x, scene.render.resolution_y = 640, 640
    scene.render.image_settings.file_format = "PNG"
    world = bpy.data.worlds.new("ride_world")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.75, 0.78, 0.82, 1.0)
    world.node_tree.nodes["Background"].inputs["Strength"].default_value = 1.0
    scene.world = world

    def material(name, colour):
        m = bpy.data.materials.new(name)
        m.use_nodes = True
        m.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = colour
        return m
    ride = material("ride_model", (0.85, 0.25, 0.1, 1.0))
    if model_meshes:
        for o in model_meshes:
            o.data.materials.clear()
            o.data.materials.append(ride)
    # A stand-in body on the skeleton: a thin stick per bone.
    sticks = []
    mat = material("ride_body", (0.1, 0.3, 0.9, 1.0))
    for pb in arm.pose.bones:
        if pb.length * unit < 0.02:
            continue
        bpy.ops.mesh.primitive_cylinder_add(radius=0.018 / unit, depth=pb.length, vertices=8)
        stick = bpy.context.object
        # Along the bone: the cylinder's Z becomes the bone's Y, from its head to its tail.
        stick.data.transform(Matrix.Translation((0, pb.length / 2, 0)) @ Matrix.Rotation(math.radians(-90), 4, "X"))
        stick.data.materials.append(mat)
        c = stick.constraints.new("COPY_TRANSFORMS")
        c.target, c.subtarget = arm, pb.name
        sticks.append(stick)
    # The ride model follows the board.
    if model_meshes:
        model_to_blender = Matrix(((1, 0, 0, 0), (0, 0, -1, 0), (0, 1, 0, 0), (0, 0, 0, 1)))  # (x,y,z) -> (x,-z,y)
        rest = {o: o.matrix_world.copy() for o in model_meshes}
    sun = bpy.data.objects.new("ride_sun", bpy.data.lights.new("ride_sun", "SUN"))
    sun.data.energy = 3.0
    sun.rotation_euler = (math.radians(50), 0, math.radians(30))
    scene.collection.objects.link(sun)
    cam = bpy.data.objects.new("ride_cam", bpy.data.cameras.new("ride_cam"))
    scene.collection.objects.link(cam)
    scene.camera = cam
    shots = []
    for name, f in (("start", start), ("middle", (start + end) // 2), ("end", end)):
        scene.frame_set(f)
        b = frames[f]
        if model_meshes:
            for o in model_meshes:
                o.matrix_world = b @ Matrix.Scale(1 / unit, 4) @ model_to_blender.inverted() @ rest[o]
        target = b @ Vector((0, 0.8 / unit, 0))
        for view, offset in (("side", Vector((2.6, 0.4, 0.2))), ("front", Vector((0.0, 0.6, 2.6)))):
            eye = b @ (offset / unit)
            cam.location = eye
            cam.rotation_euler = (target - eye).to_track_quat("-Z", "Y").to_euler()
            path = os.path.join(folder, f"{name}_{view}.png")
            scene.render.filepath = path
            bpy.ops.render.render(write_still=True)
            shots.append(path)
    for s in sticks:
        bpy.data.objects.remove(s, do_unlink=True)
    return shots


# ---------------------------------------------------------------- main

def emit(result, path=None):
    print("RESKATE_RIDE_RESULT=" + json.dumps(result, separators=(",", ":")), flush=True)
    if path:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)


def run(o):
    report = {"schema": 1, "version": VERSION, "input": o.input, "status": "ok", "exit_code": EXIT_OK}
    clear_scene()
    progress(0.05, "Importing the clip")
    arm = import_clip(o.input)
    unit = unit_of(arm)
    report["metres_per_unit"] = unit
    report["frame_range"] = list(frame_range(arm))
    report["take"] = arm.animation_data.action.name
    if o.list_bones:
        report["mode"] = "list-bones"
        report["bones"] = list_bones(arm)
        return report
    if not o.output:
        raise RideError("usage_error", "--output is required")
    if o.passthrough:
        report["mode"] = "passthrough"
        export_clip(arm, o.output)
        report["output"] = o.output
        return report
    if not o.targets:
        raise RideError("usage_error", "--targets is required (or --passthrough / --list-bones)")
    try:
        with open(o.targets, encoding="utf-8") as f:
            targets = json.load(f)
    except (OSError, ValueError) as e:
        raise RideError("targets_unreadable", f"Cannot read the targets JSON: {e}")
    report["mode"] = "retarget"
    report["ride"] = targets.get("name", "")
    bones = find_bones(arm, targets.get("bones", {}))
    report["bones"] = bones
    meshes = load_model(o.model)
    lead = o.lead_foot or targets.get("lead_foot", "left")
    frames = retarget(arm, bones, targets, meshes, unit, lead, report)
    progress(0.85, "Writing " + os.path.basename(o.output))
    if o.preview:
        report["previews"] = previews(arm, o.preview, frames, meshes, unit, *frame_range(arm))
    if meshes:
        for m in meshes:
            bpy.data.objects.remove(m, do_unlink=True)
    export_clip(arm, o.output)
    report["output"] = o.output
    progress(1.0, "Done")
    return report


def main(argv):
    result_file = None
    try:
        o = arguments(argv)
        result_file = o.result
        result = run(o)
        emit(result, result_file)
        return EXIT_OK
    except SystemExit as e:  # argparse
        code = EXIT_USAGE if e.code else EXIT_OK
        if code:
            emit({"schema": 1, "status": "failed", "exit_code": code, "error_code": "usage_error",
                  "error": "Bad arguments (see --help)"}, result_file)
        return code
    except RideError as e:
        emit({"schema": 1, "status": "failed", "exit_code": EXIT_FAILED, "error_code": e.code, "error": e.message,
              **e.details}, result_file)
        return EXIT_FAILED
    except Exception as e:  # noqa: BLE001 - the result line must always come out
        emit({"schema": 1, "status": "failed", "exit_code": EXIT_FAILED, "error_code": "internal_error",
              "error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()}, result_file)
        return EXIT_FAILED


if __name__ == "__main__":
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    code = main(args)
    if bpy.app.background:
        sys.exit(code)
