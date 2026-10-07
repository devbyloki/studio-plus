#!/usr/bin/env bash
# Mirror the source tree from DA-PI to DA-PC and build it there.
# Usage: [DEST=C:/dev/other-dir] scripts/sync-build.sh [preset]   (default preset: release, DEST: C:/dev/studio-plus)
# Give each parallel builder its own DEST so builds never share a folder.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
preset="${1:-release}"
dest="${DEST:-C:/dev/studio-plus}"
ssh -o BatchMode=yes DA-PC "powershell -NoProfile -Command \"New-Item -ItemType Directory -Force -Path '$dest' | Out-Null\""
tar -C "$here" --exclude=./build --exclude=./.git --exclude=./.claude -czf - . | ssh -o BatchMode=yes DA-PC "tar -xzf - -C $dest"
ps=$(cat <<PS
\$vs = & "\${env:ProgramFiles(x86)}\\Microsoft Visual Studio\\Installer\\vswhere.exe" -latest -products * -property installationPath
Import-Module "\$vs\\Common7\\Tools\\Microsoft.VisualStudio.DevShell.dll"
Enter-VsDevShell -VsInstallPath \$vs -SkipAutomaticLocation -DevCmdArguments '-arch=x64 -host_arch=x64' | Out-Null
Set-Location '$dest'
cmake --preset $preset; if (\$LASTEXITCODE) { exit \$LASTEXITCODE }
cmake --build --preset $preset; exit \$LASTEXITCODE
PS
)
enc=$(printf '%s' "$ps" | iconv -t UTF-16LE | base64 -w0)
ssh -o BatchMode=yes DA-PC "powershell -NoProfile -EncodedCommand $enc" 2>&1 | grep -v -e CLIXML -e '^<Objs'
