# Makes the release folder and zip from a release build:
#   dist\ReSkate-Studio-Plus-<version>\  and  dist\ReSkate-Studio-Plus-<version>.zip
# Run from the repository root after: cmake --preset release; cmake --build --preset release
param([string]$Version = "")

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$build = Join-Path $root "build\release"
if (-not $Version) {
    $match = Select-String -Path (Join-Path $root "CMakeLists.txt") -Pattern 'project\(ReSkateStudioPlus VERSION ([0-9.]+)'
    $Version = $match.Matches[0].Groups[1].Value
}
$name = "ReSkate-Studio-Plus-$Version"
$dist = Join-Path $root "dist"
$out = Join-Path $dist $name
if (Test-Path $out) { Remove-Item -Recurse -Force $out }
New-Item -ItemType Directory -Force -Path $out | Out-Null

foreach ($exe in @("ReSkate Studio+.exe", "studio-plus.exe")) {
    $from = Join-Path $build $exe
    if (-not (Test-Path $from)) { throw "Missing ${from}: build the release preset first" }
    Copy-Item $from $out
}
Copy-Item -Recurse (Join-Path $build "blender") (Join-Path $out "blender")
Get-ChildItem -Path (Join-Path $out "blender") -Recurse -Include "__pycache__" -Directory | Remove-Item -Recurse -Force

# The engine is not ours to ship: say where it goes.
$engine = Join-Path $out "engine"
New-Item -ItemType Directory -Force -Path $engine | Out-Null
@"
Put ReSkate Studio's engine files here: reskate_cli.exe and its Native folder, from the ReSkate Studio download.
Studio+ looks for them in this folder. You can also keep them anywhere and set the engine folder on SETTINGS.
Browsing assets, editing EBX, exporting textures, replacing meshes and the mod list work without them.
"@ | Set-Content -Encoding UTF8 (Join-Path $engine "PUT RESKATE STUDIO FILES HERE.txt")

foreach ($doc in @("README.md", "CHANGELOG.md", "LICENSE")) { Copy-Item (Join-Path $root $doc) $out }
$licenses = Join-Path $out "licenses"
New-Item -ItemType Directory -Force -Path $licenses | Out-Null
$third = Join-Path $root "third_party"
@{
    "ReSkate-GPL-3.0.txt"          = "reskate\LICENSE"
    "DearImGui-MIT.txt"            = "imgui\LICENSE.txt"
    "nlohmann-json-MIT.txt"        = "nlohmann\LICENSE.MIT"
    "zstd-BSD.txt"                 = "reskate\External\zstd\LICENSE"
    "lz4-BSD.txt"                  = "reskate\External\lz4\LICENSE"
    "miniz-MIT.txt"                = "reskate\External\miniz\LICENSE.txt"
    "bcdec-MIT.txt"                = "reskate\External\bcdec\LICENSE.txt"
    "Montserrat-OFL.txt"           = "fonts\Montserrat-LICENSE.txt"
    "PermanentMarker-Apache-2.0.txt" = "fonts\PermanentMarker-LICENSE.txt"
}.GetEnumerator() | ForEach-Object { Copy-Item (Join-Path $third $_.Value) (Join-Path $licenses $_.Key) }

$zip = Join-Path $dist "$name.zip"
if (Test-Path $zip) { Remove-Item -Force $zip }
Compress-Archive -Path $out -DestinationPath $zip
Write-Output "Packaged $zip"
