$ErrorActionPreference = 'Continue'
$sp = 'C:\dev\sp-media\build\release\studio-plus.exe'
$t = 'C:\dev\sp-media-tests'
New-Item -ItemType Directory -Force -Path $t | Out-Null
Copy-Item 'C:\Users\lokid\Downloads\reskate-scooter\out\razor_scooter_deck.glb' $t -Force
Copy-Item 'C:\Users\lokid\Downloads\reskate-scooter\out\Untitled.fbproject' $t -Force
Copy-Item 'C:\Users\lokid\Downloads\reskate-scooter\anim\push_regular_medium.fbx' $t -Force
function T($label, [string[]]$a) { "===== $label : $($a -join ' ')"; & $sp @a 2>&1 | ForEach-Object { "$_" } | Select-Object -First 25; "exit=$LASTEXITCODE" }
T 'audio json' @('audio','info','audio/physicsprops/assets/dgo_wb_obj_metal_rail_impact_low','--json')
T 'audio human' @('audio','info','audio/vo/text-to-speech_shippable/id_vo_statement_hi')
T 'audio bad' @('audio','info','audio/nope/missing','--json')
T 'audio wrongtype' @('audio','info','ui/videos/splash/vid_skate_logo_lockup_withaudio_v2_1920x1080')
T 'audio noarg' @('audio','info')
T 'video info json' @('video','info','ui/videos/splash/vid_skate_logo_lockup_withaudio_v2_1920x1080','--json')
T 'video info human' @('video','info','ui/videos/splash/vid_skate_logo_lockup_withaudio_v2_1920x1080')
T 'video info wrongtype' @('video','info','audio/physicsprops/assets/dgo_wb_obj_metal_rail_impact_low','--json')
T 'video decode json' @('video','decode-test','ui/videos/splash/vid_skate_logo_lockup_withaudio_v2_1920x1080','--json')
T 'video decode human' @('video','decode-test','ui/videos/s01_intro/dgo_video_season01_video_01')
T 'video decode bad' @('video','decode-test','ui/videos/none','--json')
T 'anim info json' @('anim','info','animation/dingo/c_proto_onb_push_regular_medium_full_static','--json')
T 'anim info human' @('anim','info','animation/dingo/c_proto_onb_push_regular_medium_full_static')
T 'anim info subt' @('anim','info','animation/dingo/subt_c_proto_onb_push_regular_medium_loop_02','--json')
T 'anim info proj' @('anim','info','animation/dingo/c_proto_onb_push_regular_medium_full_static','--project',"$t\Untitled.fbproject",'--json')
T 'anim info projmissing' @('anim','info','animation/dingo/c_proto_onb_push_regular_medium_full_static','--project',"$t\nope.fbproject")
T 'anim export subt' @('anim','export','animation/dingo/subt_c_proto_onb_push_regular_medium_loop_02',"$t\subt.fbx",'--json')
T 'anim export json' @('anim','export','animation/dingo/c_proto_onb_push_regular_medium_full_static',"$t\push.fbx",'--json')
T 'anim export human proj' @('anim','export','animation/dingo/c_proto_onb_push_regular_medium_full_static',"$t\push_proj.fbx",'--project',"$t\Untitled.fbproject")
T 'anim export badext' @('anim','export','animation/dingo/c_proto_onb_push_regular_medium_full_static',"$t\push.glb")
T 'anim export nofolder' @('anim','export','animation/dingo/c_proto_onb_push_regular_medium_full_static',"$t\nodir\push.fbx",'--json')
T 'fbx-info json' @('anim','fbx-info',"$t\push.fbx",'--json')
T 'fbx-info human' @('anim','fbx-info',"$t\push_regular_medium.fbx")
T 'fbx-info glb' @('anim','fbx-info',"$t\razor_scooter_deck.glb",'--json')
T 'fbx-info missing' @('anim','fbx-info',"$t\missing.fbx",'--json')
T 'import json fbmod' @('anim','import','animation/dingo/c_proto_onb_push_regular_medium_full_static',"$t\push_regular_medium.fbx","$t\imp.fbmod",'--json')
T 'import human proj' @('anim','import','animation/dingo/c_proto_onb_push_regular_medium_full_static',"$t\push.fbx","$t\sub\imp.fbproject",'--project',"$t\Untitled.fbproject",'--take','0')
T 'import badtake' @('anim','import','animation/dingo/c_proto_onb_push_regular_medium_full_static',"$t\push.fbx","$t\imp2.fbproject",'--take','5','--json')
T 'import negtake' @('anim','import','animation/dingo/c_proto_onb_push_regular_medium_full_static',"$t\push.fbx","$t\imp2.fbproject",'--take','-1')
T 'import badext' @('anim','import','animation/dingo/c_proto_onb_push_regular_medium_full_static',"$t\push.fbx","$t\imp2.zip",'--json')
T 'import glb' @('anim','import','animation/dingo/c_proto_onb_push_regular_medium_full_static',"$t\razor_scooter_deck.glb","$t\imp3.fbmod",'--json')
T 'import takeword' @('anim','import','animation/dingo/c_proto_onb_push_regular_medium_full_static',"$t\push.fbx","$t\imp2.fbproject",'--take','x')
T 'help' @('anim','import','--help')
Get-ChildItem $t -Recurse | Select-Object FullName,Length | Format-Table -AutoSize | Out-String -Width 200
