"""ride_retarget.py on a stand-in skeleton: re-poses an idle and a push clip for a scooter and checks the result.

    python tests/ride_retarget_test.py        (needs Blender's Python module: pip install bpy==5.2.2)

The stand-in stands sideways on a board rolling 1.5 m forward; the push clip's back foot steps off, sweeps back
along the ground and steps on again. The scooter is a box model with its bar 0.79 m up.
"""
import json
import math
import os
import runpy
import tempfile

import bpy
from mathutils import Vector

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, '..', 'blender', 'anim', 'ride_retarget.py')
TARGETS = os.path.join(HERE, '..', 'blender', 'anim', 'scooter.json')
ride = runpy.run_path(SCRIPT, run_name='ride_retarget')


def make_rig():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    data = bpy.data.armatures.new('Skeleton')
    arm = bpy.data.objects.new('Skeleton', data)
    bpy.context.scene.collection.objects.link(arm)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode='EDIT')

    def bone(name, head, tail, parent=None):
        b = data.edit_bones.new(name)
        b.head, b.tail = head, tail
        if parent:
            b.parent = data.edit_bones[parent]
    bone('Root', (0, 0, 0), (0, 0.2, 0))
    bone('Skateboard', (0, 0, 0.1), (0, 0.3, 0.1), 'Root')
    bone('Pelvis', (0, 0, 1.0), (0, 0, 1.12), 'Root')
    bone('Spine', (0, 0, 1.12), (0, 0, 1.3), 'Pelvis')
    bone('Spine1', (0, 0, 1.3), (0, 0, 1.45), 'Spine')
    bone('Neck', (0, 0, 1.45), (0, 0, 1.55), 'Spine1')
    bone('Head', (0, 0, 1.55), (0, 0, 1.75), 'Neck')
    for s, y in (('L', 1), ('R', -1)):  # facing +X, so the left side is +Y
        bone(f'{s}_Clavicle', (0, 0.03 * y, 1.42), (0, 0.18 * y, 1.42), 'Spine1')
        bone(f'{s}_UpperArm', (0, 0.18 * y, 1.42), (0.02, 0.24 * y, 1.14), f'{s}_Clavicle')
        bone(f'{s}_Forearm', (0.02, 0.24 * y, 1.14), (0.08, 0.27 * y, 0.9), f'{s}_UpperArm')
        bone(f'{s}_Hand', (0.08, 0.27 * y, 0.9), (0.11, 0.28 * y, 0.8), f'{s}_Forearm')
        bone(f'{s}_Thigh', (0, 0.1 * y, 1.0), (0.06, 0.2 * y, 0.6), 'Pelvis')
        bone(f'{s}_Calf', (0.06, 0.2 * y, 0.6), (0, 0.25 * y, 0.19), f'{s}_Thigh')
        bone(f'{s}_Foot', (0, 0.25 * y, 0.19), (0.12, 0.25 * y, 0.12), f'{s}_Calf')
        bone(f'{s}_Toe', (0.12, 0.25 * y, 0.12), (0.2, 0.25 * y, 0.11), f'{s}_Foot')
    bpy.ops.object.mode_set(mode='OBJECT')
    return arm


def make_clip(path, push):
    arm = make_rig()
    arm.animation_data_create()
    arm.animation_data.action = bpy.data.actions.new('take')
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 1, 60
    pb = arm.pose.bones
    targets = {}
    for s in ('L', 'R'):
        e = bpy.data.objects.new(f'tgt_{s}', None)
        scene.collection.objects.link(e)
        targets[s] = e
        c = pb[f'{s}_Calf'].constraints.new('IK')
        c.target, c.chain_count = e, 2
    for f in range(1, 61):
        t = (f - 1) / 59
        root_y = 1.5 * t
        pb['Root'].location = (0, root_y, 0)
        pb['Root'].keyframe_insert('location', frame=f)
        pb['Pelvis'].location = (0, 0, -0.02 * math.sin(t * math.pi * 4))
        pb['Pelvis'].keyframe_insert('location', frame=f)
        left, right = Vector((0, 0.25 + root_y, 0.19)), Vector((0, -0.25 + root_y, 0.19))
        if push and 0.25 < t < 0.75:  # off the board: beside it, sweeping back along the ground
            k = (t - 0.25) / 0.5
            right = Vector((0.28, -0.05 + root_y - 0.6 * k, 0.08 + 0.05 * math.sin(k * math.pi)))
        for s, p in (('L', left), ('R', right)):
            targets[s].location = p
            targets[s].keyframe_insert('location', frame=f)
    bpy.ops.object.select_all(action='DESELECT')
    arm.select_set(True)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode='POSE')
    bpy.ops.pose.select_all(action='SELECT')
    bpy.ops.nla.bake(frame_start=1, frame_end=60, only_selected=True, visual_keying=True, clear_constraints=True,
                     use_current_action=True, bake_types={'POSE'})
    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.export_scene.fbx(filepath=path, use_selection=True, object_types={'ARMATURE'}, add_leaf_bones=False,
                             bake_anim=True, bake_anim_use_all_actions=False, bake_anim_use_nla_strips=False,
                             axis_forward='-Z', axis_up='Y', armature_nodetype='NULL')


def make_scooter(path):
    # Model space: Y up, Z forward. glTF export maps Blender (x, y, z) to (x, z, -y), so build at (x, -z, y).
    bpy.ops.wm.read_factory_settings(use_empty=True)
    for size, (x, y, z) in (((0.11, 0.03, 0.46), (0, 0.085, -0.02)), ((0.03, 0.72, 0.03), (0, 0.44, 0.24)),
                            ((0.32, 0.025, 0.025), (0, 0.79, 0.24)), ((0.025, 0.1, 0.1), (0, 0.05, 0.26)),
                            ((0.025, 0.1, 0.1), (0, 0.05, -0.27))):
        bpy.ops.mesh.primitive_cube_add(size=1, location=(x, -z, y))
        bpy.context.object.scale = (size[0], size[2], size[1])
        bpy.ops.object.transform_apply(scale=True)
    bpy.ops.export_scene.gltf(filepath=path, export_format='GLB')


def retarget(*args):
    result = os.path.join(T, 'result.json')
    code = ride['main'](list(args) + ['--result', result])
    with open(result) as f:
        r = json.load(f)
    assert code == 0 and r['status'] == 'ok', r
    return r


def load(path):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.fbx(filepath=path, use_anim=True, ignore_leaf_bones=False, automatic_bone_orientation=False)
    arm = next(o for o in bpy.data.objects if o.type == 'ARMATURE')
    s, e = (int(round(v)) for v in arm.animation_data.action.frame_range)
    poses = {}
    for f in (s, (s + e) // 2, e):
        bpy.context.scene.frame_set(f)
        poses[f] = {pb.name: (arm.matrix_world @ pb.head).copy() for pb in arm.pose.bones}
    return {b.name: b.parent.name if b.parent else None for b in arm.data.bones}, poses, (s, e)


def moved_from(a, b, still=('Root', 'Skateboard')):
    """How far any bone moved between two clips; the skeleton, frames and `still` bones must be the same."""
    h1, p1, r1 = load(a)
    h2, p2, r2 = load(b)
    assert h1 == h2 and r1 == r2, 'skeleton or frame range changed'
    for f in p1:
        for bone in still:
            assert (p1[f][bone] - p2[f][bone]).length < 1e-3, f'{bone} moved at frame {f}'
    return max((p1[f][n] - p2[f][n]).length for f in p1 for n in p1[f])


T = tempfile.mkdtemp(prefix='sp-ride-')
make_clip(f'{T}/idle.fbx', False)
make_clip(f'{T}/push.fbx', True)
make_scooter(f'{T}/scooter.glb')
for clip, pushes in (('idle', 0), ('push', 30)):
    r = retarget('--input', f'{T}/{clip}.fbx', '--output', f'{T}/{clip}_ride.fbx', '--targets', TARGETS,
                 '--model', f'{T}/scooter.glb')
    assert abs(r['hips_turned_degrees'] - 90) < 1, r['hips_turned_degrees']
    assert r['grips'] == {'left': [0.13, 0.79, 0.24], 'right': [-0.13, 0.79, 0.24]}, r['grips']
    assert r['push_frames'] == pushes, r['push_frames']
    assert all(v < 0.01 for v in r['worst_miss_m'].values()), r['worst_miss_m']
    assert moved_from(f'{T}/{clip}.fbx', f'{T}/{clip}_ride.fbx') > 0.05, 'nothing was re-posed'
    print(f'{clip}: turned {r["hips_turned_degrees"]}, lean {r["lean_degrees"]}, push frames {r["push_frames"]}, '
          f'misses {r["worst_miss_m"]}')
retarget('--input', f'{T}/idle.fbx', '--output', f'{T}/idle_same.fbx', '--passthrough')
assert moved_from(f'{T}/idle.fbx', f'{T}/idle_same.fbx') < 1e-3, 'the round trip changed the clip'
code = ride['main'](['--input', f'{T}/missing.fbx', '--list-bones', '--result', f'{T}/bad.json'])
assert code != 0 and json.load(open(f'{T}/bad.json'))['error_code'] == 'input_not_found'
print('ride_retarget: all checks passed')
