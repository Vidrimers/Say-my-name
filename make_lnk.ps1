$ws = New-Object -ComObject WScript.Shell
$root = 'C:\Users\vidri\Downloads\asrMimo'
$lnkPath = Join-Path $root 'SAY MY NAME.lnk'
$lnk = $ws.CreateShortcut($lnkPath)
$lnk.TargetPath = 'C:\Windows\System32\wscript.exe'
$lnk.Arguments = '"' + (Join-Path $root 'start.vbs') + '"'
$lnk.WorkingDirectory = $root
$lnk.IconLocation = (Join-Path $root 'app\app.ico') + ',0'
$lnk.Description = 'SAY MY NAME (Whisper) silent launch'
$lnk.Save()
Write-Output ("lnk ok -> " + $lnkPath)
Write-Output ("args: " + $lnk.Arguments)
