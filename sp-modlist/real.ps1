$exe = 'C:\dev\sp-modlist\build\release\studio-plus.exe'
$g = 'C:\Users\lokid\Downloads'
Get-ChildItem "$g\Mods" -Force | Select-Object Mode, Name | Format-Table -AutoSize | Out-String -Width 200
"ModData exists: " + (Test-Path "$g\ModData")
"--- human"
& $exe mod list --game-root $g --check-hash
"exit=$LASTEXITCODE"
"--- json"
& $exe mod list --game-root $g --json
