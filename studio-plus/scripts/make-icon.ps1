# Draws the Studio+ app icon: a stylised "S+" in black on the ReSkate blue (1,131,255), on a tile
# with slightly rough, hand-cut edges like skate.'s menus. Writes a multi-size .ico (PNG entries).
#   powershell -ExecutionPolicy Bypass -File scripts\make-icon.ps1
param([string]$Out = (Join-Path $PSScriptRoot '..\assets\studio-plus.ico'))

Add-Type -AssemblyName System.Drawing
$ErrorActionPreference = 'Stop'

$fonts = New-Object System.Drawing.Text.PrivateFontCollection
$fonts.AddFontFile((Resolve-Path (Join-Path $PSScriptRoot '..\third_party\fonts\Montserrat-ExtraBold.ttf')).Path)
$family = $fonts.Families[0]

function Draw-Icon([int]$size) {
    $bmp = New-Object System.Drawing.Bitmap $size, $size, ([System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = 'AntiAlias'
    $g.TextRenderingHint = 'AntiAliasGridFit'
    $g.Clear([System.Drawing.Color]::Transparent)

    # The tile: a square with a few pixels of deterministic wobble along each edge.
    $m = [Math]::Max(1.0, $size * 0.04)
    $points = New-Object System.Collections.Generic.List[System.Drawing.PointF]
    $steps = 6
    $seed = 7
    $jitter = { param($i) ([Math]::Sin($i * 12.9898 + $seed) * 43758.5453 % 1.0) * $size * 0.018 }
    for ($i = 0; $i -lt $steps; $i++) { $points.Add([System.Drawing.PointF]::new($m + ($size - 2 * $m) * $i / $steps, $m + (& $jitter $i))) }
    for ($i = 0; $i -lt $steps; $i++) { $points.Add([System.Drawing.PointF]::new($size - $m + (& $jitter ($i + 10)), $m + ($size - 2 * $m) * $i / $steps)) }
    for ($i = 0; $i -lt $steps; $i++) { $points.Add([System.Drawing.PointF]::new($size - $m - ($size - 2 * $m) * $i / $steps, $size - $m + (& $jitter ($i + 20)))) }
    for ($i = 0; $i -lt $steps; $i++) { $points.Add([System.Drawing.PointF]::new($m + (& $jitter ($i + 30)), $size - $m - ($size - 2 * $m) * $i / $steps)) }
    $blue = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255, 1, 131, 255))
    $g.FillPolygon($blue, $points.ToArray())

    # "S+" tilted a little, like the brushed page titles.
    $ink = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255, 0, 0, 0))
    $font = New-Object System.Drawing.Font $family, ($size * 0.56), ([System.Drawing.FontStyle]::Regular), ([System.Drawing.GraphicsUnit]::Pixel)
    $format = New-Object System.Drawing.StringFormat
    $format.Alignment = 'Center'
    $format.LineAlignment = 'Center'
    $g.TranslateTransform($size / 2, $size / 2)
    $g.RotateTransform(-6)
    $g.DrawString('S+', $font, $ink, [System.Drawing.RectangleF]::new(-$size / 2, -$size / 2 + $size * 0.02, $size, $size), $format)
    $g.Dispose()

    $stream = New-Object System.IO.MemoryStream
    $bmp.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
    return , $stream.ToArray()
}

$sizes = 16, 24, 32, 48, 64, 128, 256
$images = foreach ($s in $sizes) { , (Draw-Icon $s) }

$dir = Split-Path $Out
if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
$file = New-Object System.IO.MemoryStream
$w = New-Object System.IO.BinaryWriter $file
$w.Write([uint16]0); $w.Write([uint16]1); $w.Write([uint16]$sizes.Count)
$offset = 6 + 16 * $sizes.Count
for ($i = 0; $i -lt $sizes.Count; $i++) {
    $s = $sizes[$i]
    $dim = if ($s -ge 256) { 0 } else { $s }
    $w.Write([byte]$dim); $w.Write([byte]$dim); $w.Write([byte]0); $w.Write([byte]0)
    $w.Write([uint16]1); $w.Write([uint16]32)
    $w.Write([uint32]$images[$i].Length); $w.Write([uint32]$offset)
    $offset += $images[$i].Length
}
foreach ($img in $images) { $w.Write([byte[]]$img) }
$w.Flush()
[System.IO.File]::WriteAllBytes((Join-Path (Resolve-Path $dir).Path (Split-Path $Out -Leaf)), $file.ToArray())
Write-Output "Wrote $Out ($($sizes -join ', ') px)"
