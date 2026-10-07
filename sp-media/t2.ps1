$sp = 'C:\dev\sp-media\build\release\studio-plus.exe'
$t = 'C:\dev\sp-media-tests'
function T($label, [string[]]$a) { "===== $label : $($a -join ' ')"; & $sp @a 2>&1 | ForEach-Object { "$_" } | Select-Object -First 12; "exit=$LASTEXITCODE" }
T 'audio wrongtype' @('audio','info','ui/videos/splash/vid_skate_logo_lockup_withaudio_v2_1920x1080')
T 'anim export subt json' @('anim','export','animation/dingo/subt_c_proto_onb_push_regular_medium_loop_02',"$t\subt.fbx",'--json')
T 'video info human' @('video','info','UI\Videos\splash\vid_skate_logo_lockup_withaudio_v2_1920x1080')
T 'fake game root' @('audio','info','audio/x','--game-root',"$t\fakegame",'--json')
"===== MCP"
$req = @(
 '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}',
 '{"jsonrpc":"2.0","method":"notifications/initialized"}',
 '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"anim_fbx_info","arguments":{"file":"C:\\dev\\sp-media-tests\\push.fbx"}}}',
 '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"anim_export","arguments":{"clip":"animation/dingo/c_proto_onb_push_regular_medium_full_static","output":"C:\\dev\\sp-media-tests\\mcp_push.fbx"},"_meta":{"progressToken":"p1"}}}'
) -join "`n"
$req | & $sp mcp 2>&1 | ForEach-Object { "$_".Substring(0, [Math]::Min(600, "$_".Length)) }
"===== tools/list media entries"
('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}' + "`n" + '{"jsonrpc":"2.0","id":2,"method":"tools/list"}') | & $sp mcp | Select-Object -Last 1 | ConvertFrom-Json | ForEach-Object { $_.result.tools } | Where-Object { $_.name -match '^(anim|audio|video)_' } | ForEach-Object { $_.name + ' ' + ($_.inputSchema | ConvertTo-Json -Compress -Depth 6) }
