$ErrorActionPreference = 'Stop'
$exe = 'C:\dev\sp-modlist\build\release\studio-plus.exe'
$base = 'C:\dev\sp-modlist-tests'
if (Test-Path $base) { Remove-Item $base -Recurse -Force }
New-Item -ItemType Directory $base | Out-Null
$cur = 'fbce74d5e28ef525dbba2cb4adbebc13405bdbd88f31bc940bca45e4ae88b8f9'
$other = '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
$utf8 = New-Object System.Text.UTF8Encoding($false)
function W($path, $text) {
    New-Item -ItemType Directory -Force (Split-Path $path) | Out-Null
    [IO.File]::WriteAllText($path, $text, $utf8)
}
function Game($name) {
    $g = Join-Path $base $name
    W "$g\Skate.exe" 'dummy'
    W "$g\ReSkate.dll" 'dummy dll'
    return $g
}

# ---- A: a valid mods.json with every per-mod rule ------------------------------------------------
$a = Game 'A'
$m = "$a\Mods"
W "$m\Current\layout.toc" 'x'
W "$m\Current\.reskate-studio-patch" ("ReSkate Studio native Patch v1`r`nskate_sha256=" + $cur.ToUpper() + "`r`n")
W "$m\Current\manifest.json" '{"name":"Current Mod","author":"Someone","version_number":"2.1.0","version":"9.9","description":"A mod\twith a tab"}'
W "$m\Current\reskate-build.json" '{"schema":1,"tool":"ReSkateStudio","version":"1.0.0","built":"2026-09-24T18:35:00Z"}'
W "$m\Current\reskate-levels.json" '{"levels":[{"asset":"levels/game/a/a"},{"asset":5},{"asset":"levels/game/b/b"}]}'
W "$m\OldStudio\layout.toc" 'x'
W "$m\OldStudio\.reskate-studio-patch" "ReSkate Studio native Patch v1`n"
W "$m\OldStudio\reskate-mod.json" '{"name":"Old One","version":"0.5","author":"Old"}'
W "$m\OtherBuild\layout.toc" 'x'
W "$m\OtherBuild\.reskate-studio-patch" "ReSkate Studio native Patch v1`nskate_sha256=$other`n"
W "$m\Unstamped\layout.toc" 'x'
W "$m\Park\parks\sanvan.park.json" '{}'
W "$m\Park\parks\alpha.park.json" '{}'
W "$m\Park\parks\notes.txt" 'x'
W "$m\BadBuild\reskate-build.json" '{"schema":1}'
W "$m\BadBuild\manifest.json" '{"name":"Bad, "}'
W "$m\OffOld\layout.toc" 'x'
W "$m\OffOld\.reskate-studio-patch" "ReSkate Studio native Patch v1`nskate_sha256=$other`n"
W "$m\Unlisted B\reskate-levels.json" '{"levels":[]}'
W "$m\unlisted a\x.txt" 'x'
W "$m\.hidden\layout.toc" 'x'
W "$m\bad+name\layout.toc" 'x'
W "$m\caf$([char]0xE9)\layout.toc" 'x'
W "$m\mods.json" '{"schema":1,"mods":[{"name":"park","enabled":true},{"name":"OffOld","enabled":false},{"name":"Gone","enabled":true},{"name":"Current","enabled":true},{"name":"Unstamped","enabled":false},{"name":"OldStudio","enabled":true},{"name":"OtherBuild","enabled":true},{"name":"BadBuild","enabled":true}]}'

# ---- B: malformed mods.json variants (one install each) -------------------------------------------
$bad = [ordered]@{
  'B1-dupkey'   = '{"schema":1,"mods":[],"mods":[]}'
  'B2-float'    = '{"schema":1.0,"mods":[]}'
  'B3-extra'    = '{"schema":1,"mods":[],"x":1}'
  'B4-dupname'  = '{"schema":1,"mods":[{"name":"One","enabled":true},{"name":"one","enabled":false}]}'
  'B5-badname'  = '{"schema":1,"mods":[{"name":"../One","enabled":true}]}'
  'B6-rowextra' = '{"schema":1,"mods":[{"name":"One","enabled":true,"order":1}]}'
  'B7-syntax'   = '{"schema":1,"mods":[{"name":"One","enabled":true},]}'
  'B8-enabled'  = '{"schema":1,"mods":[{"name":"One","enabled":"yes"}]}'
  'B9-deep'     = '{"schema":1,"mods":[{"name":"One","enabled":true}],"d":[[[[[[[[[1]]]]]]]]]}'
}
foreach ($k in $bad.Keys) {
  $g = Game $k
  W "$g\Mods\One\layout.toc" 'x'
  W "$g\Mods\One\.reskate-studio-patch" "ReSkate Studio native Patch v1`nskate_sha256=$cur`n"
  W "$g\Mods\mods.json" $bad[$k]
}
# A valid one with a byte-order mark: accepted.
$g = Game 'B0-bom'
W "$g\Mods\One\layout.toc" 'x'
W "$g\Mods\One\.reskate-studio-patch" "ReSkate Studio native Patch v1`nskate_sha256=$cur`n"
[IO.File]::WriteAllText("$g\Mods\mods.json", '{"schema":1,"mods":[{"name":"One","enabled":false}]}', (New-Object System.Text.UTF8Encoding($true)))

# ---- C: exclusions file ---------------------------------------------------------------------------
Add-Type -TypeDefinition @'
using System; using System.IO; using System.Linq; using System.Collections.Generic; using System.Text;
public static class Fp {
  public static string Of(string dir) {
    var files = new List<string>();
    foreach (var f in Directory.GetFiles(dir, "*", SearchOption.AllDirectories)) {
      var fi = new FileInfo(f);
      var rel = f.Substring(dir.Length).TrimStart('\\').Replace('\\', '/');
      var sb = new StringBuilder();
      foreach (var ch in rel) { if (ch < 0x80) sb.Append(ch >= 'A' && ch <= 'Z' ? (char)(ch + 32) : ch); else sb.Append("#" + ((int)ch).ToString()); }
      files.Add(sb.ToString() + "|" + fi.Length + "|" + fi.LastWriteTimeUtc.ToFileTimeUtc());
    }
    files.Sort(StringComparer.Ordinal);
    ulong h = 1469598103934665603UL;
    foreach (var f in files) foreach (var c in Encoding.ASCII.GetBytes(f + "\n")) { h ^= c; unchecked { h *= 1099511628211UL; } }
    return files.Count + "-" + h.ToString("x16");
  }
  public static string Sdk(string dll) {
    var fi = new FileInfo(dll);
    return fi.Length + "-" + fi.LastWriteTimeUtc.ToFileTimeUtc();
  }
}
'@
$c = Game 'C'
$m = "$c\Mods"
foreach ($n in 'Broken', 'Stale', 'Fixed', 'Fine', 'OldDll') {
  W "$m\$n\layout.toc" 'x'
  W "$m\$n\.reskate-studio-patch" "ReSkate Studio native Patch v1`nskate_sha256=$cur`n"
}
W "$m\Broken\sub\Data.CAS" 'abc'
$sdk = [Fp]::Sdk("$c\ReSkate.dll")
$ex = [ordered]@{ schema = 1; mods = [ordered]@{
  Broken = [ordered]@{ fingerprint = [Fp]::Of("$m\Broken"); sdk = $sdk; problems = @('levels/game/x: bundle missing', 'second problem') }
  Stale  = [ordered]@{ fingerprint = '1-0000000000000000'; sdk = $sdk; problems = @('old') }
  OldDll = [ordered]@{ fingerprint = [Fp]::Of("$m\OldDll"); sdk = '1-2'; problems = @('merged badly with an older dll') }
  Ghost  = [ordered]@{ fingerprint = '1-1111111111111111'; sdk = $sdk; problems = @('no folder') }
} }
W "$m\.reskate-excluded.json" ($ex | ConvertTo-Json -Depth 5)
W "$m\mods.json" '{"schema":1,"mods":[{"name":"Fine","enabled":true},{"name":"Broken","enabled":true},{"name":"Stale","enabled":true},{"name":"OldDll","enabled":true}]}'
"fingerprint Broken (C# port): " + [Fp]::Of("$m\Broken")

# ---- D: no Mods folder; E: ModData\Default\Mods ----------------------------------------------------
$d = Game 'D'
$e = Game 'E'
W "$e\Mods\ShouldNotShow\layout.toc" 'x'
W "$e\ModData\Default\Mods\Inside\parks\sanvan.park.json" '{}'

foreach ($k in @('A') + @($bad.Keys) + @('B0-bom', 'C', 'D', 'E')) {
  "=== $k json"
  & $exe mod list --game-root (Join-Path $base $k) --json
  "exit=$LASTEXITCODE"
}
foreach ($k in 'A', 'C') {
  "=== $k human"
  & $exe mod list --game-root (Join-Path $base $k)
}
"=== A check-hash on a dummy Skate.exe"
& $exe mod list --game-root (Join-Path $base 'A') --check-hash --json | ConvertFrom-Json | ForEach-Object { $_.result.game_sha256; $_.result.game_is_supported_build }
