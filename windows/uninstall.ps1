# Stop AI Usage Tray, remove the Startup shortcut and the runtime folders.
# pystray/Pillow are left installed (other tools may use them).
#   powershell -NoProfile -ExecutionPolicy Bypass -File uninstall.ps1
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'tray-common.ps1')

Stop-Tray   # throws (and deletes nothing) if the instance won't exit
if (Test-Path -LiteralPath $Shortcut) { Remove-Item -LiteralPath $Shortcut -Force }
foreach ($dir in @($Runtime, $Stage, $Previous, "$Runtime.failed", $StateDir)) {
    if (Test-Path -LiteralPath $dir) { Remove-Item -LiteralPath $dir -Recurse -Force }
}
Write-Host 'AI Usage Tray removed.'
