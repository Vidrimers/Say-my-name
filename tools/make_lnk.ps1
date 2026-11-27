$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$ws = New-Object -ComObject WScript.Shell
$target = 'C:\Windows\System32\wscript.exe'
$arg = '"' + (Join-Path $root 'start.vbs') + '"'
$icon = (Join-Path $root 'app\app.ico') + ',0'
$desc = 'SAY MY NAME (Whisper) silent launch'

function Make-Lnk($path) {
    $lnk = $ws.CreateShortcut($path)
    $lnk.TargetPath = $target
    $lnk.Arguments = $arg
    $lnk.WorkingDirectory = $root
    $lnk.IconLocation = $icon
    $lnk.Description = $desc
    $lnk.Save()
    Write-Output "lnk -> $path"
}

Make-Lnk (Join-Path $root 'SAY MY NAME.lnk')

# Desktop (handles OneDrive redirect)
$desktop = [Environment]::GetFolderPath('Desktop')
if ($desktop -and (Test-Path $desktop)) {
    Make-Lnk (Join-Path $desktop 'SAY MY NAME.lnk')
} else {
    Write-Output 'desktop not found'
}
