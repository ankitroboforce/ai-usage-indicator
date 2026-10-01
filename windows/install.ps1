# Install or upgrade AI Usage Tray for the current user. Re-run to upgrade.
#   powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
#   -Python C:\path\to\python.exe   use a specific interpreter (pythonw.exe must sit beside it)
[CmdletBinding()]
param([switch]$NoStart, [string]$Python)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'tray-common.ps1')

# 1. Interpreter: a python.org Python 3.10+ with pythonw.exe beside python.exe.
#    The Microsoft Store build is refused: its AppData writes are virtualized
#    and its WindowsApps aliases are unreliable as Startup targets.
function Get-PyVersion([string]$Exe) {
    # No quotes in the -c code: PS 5.1 strips embedded double quotes from
    # native arguments. try/catch: under ErrorActionPreference=Stop, PS 5.1
    # turns redirected native stderr into a terminating error.
    try {
        $v = @(& $Exe -c 'import sys; print(sys.version_info.major); print(sys.version_info.minor)' 2>$null)
        if ($LASTEXITCODE -eq 0 -and $v.Count -eq 2) { return [version]('{0}.{1}' -f $v[0].Trim(), $v[1].Trim()) }
    } catch { }
    return $null
}

function Find-Python {
    $candidates = New-Object System.Collections.Generic.List[string]
    foreach ($root in @((Join-Path $env:LOCALAPPDATA 'Programs\Python'), $env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if ($root -and (Test-Path -LiteralPath $root)) {
            Get-ChildItem -LiteralPath $root -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue |
                ForEach-Object { $candidates.Add((Join-Path $_.FullName 'python.exe')) }
        }
    }
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        try {
            & $py.Source -0p 2>$null | ForEach-Object {
                if ($_ -match '([A-Za-z]:\\.*python\.exe)\s*$') { $candidates.Add($Matches[1]) }
            }
        } catch { }
    }
    $best = $null; $bestVer = $null
    foreach ($exe in ($candidates | Select-Object -Unique)) {
        if ($exe -match '\\WindowsApps\\') { continue }
        if (-not (Test-Path -LiteralPath $exe)) { continue }
        if (-not (Test-Path -LiteralPath (Join-Path (Split-Path $exe) 'pythonw.exe'))) { continue }
        $ver = Get-PyVersion $exe
        if ($ver -and $ver -ge [version]'3.10' -and (-not $bestVer -or $ver -gt $bestVer)) { $best = $exe; $bestVer = $ver }
    }
    return $best
}

if ($Python) {
    $python = (Resolve-Path -LiteralPath $Python).Path
    if ($python -match '\\WindowsApps\\') { throw "Refusing Store Python: $python" }
    $ver = Get-PyVersion $python
    if (-not $ver -or $ver -lt [version]'3.10') { throw "$python is not Python 3.10+." }
} else {
    $python = Find-Python
    if (-not $python) { throw 'No python.org Python 3.10+ found (per-user, Program Files or py launcher). Install it from python.org, or pass -Python <path to python.exe>.' }
}
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) { throw "pythonw.exe not found beside $python" }
Write-Host "Python: $python"

# 2. Dependencies, installed into and checked against that exact interpreter.
& $python -m pip install --user --quiet --disable-pip-version-check pystray pillow
if ($LASTEXITCODE -ne 0) { throw 'pip install pystray pillow failed' }
& $python -c 'import pystray, PIL'
if ($LASTEXITCODE -ne 0) { throw 'pystray/Pillow not importable after install' }

# 3. Stage the new runtime and smoke-test it before touching the running one.
if (Test-Path -LiteralPath $Stage) { Remove-Item -LiteralPath $Stage -Recurse -Force }
New-Item -ItemType Directory -Path $Stage | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot $ScriptName) -Destination $Stage
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'README.md') -Destination $Stage
$config = Join-Path $Runtime 'config.json'
if (Test-Path -LiteralPath $config) { Copy-Item -LiteralPath $config -Destination $Stage }
New-Item -ItemType Directory -Path (Join-Path $Stage 'empty-cwd') | Out-Null

Write-Host 'Smoke test (one poll of each tool):'
& $python (Join-Path $Stage $ScriptName) --once
switch ($LASTEXITCODE) {
    0 { }
    3 { Write-Warning 'A tool could not be read (logged out? offline?). Installing anyway; its icon will show ?.' }
    4 { Write-Warning 'Claude polling is paused for safety (reason above). Installing anyway; it stays paused until you check the reason under Details and click Refresh now.' }
    default { Remove-Item -LiteralPath $Stage -Recurse -Force; throw "Smoke test crashed (exit $LASTEXITCODE); nothing changed." }
}
Remove-Item -LiteralPath (Join-Path $Stage 'tray.log*') -Force -ErrorAction SilentlyContinue

# 4. Stop the old instance (verified exit), then swap directories.
Stop-Tray
if (Test-Path -LiteralPath $Previous) { Remove-Item -LiteralPath $Previous -Recurse -Force }
if (Test-Path -LiteralPath $Runtime) { Move-Item -LiteralPath $Runtime -Destination $Previous }
Move-Item -LiteralPath $Stage -Destination $Runtime

# 5. Start at logon via a Startup-folder shortcut.
$target = Join-Path $Runtime $ScriptName
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($Shortcut)
$lnk.TargetPath = $pythonw
$lnk.Arguments = '"' + $target + '"'
$lnk.WorkingDirectory = $Runtime
$lnk.Description = 'Claude Code and Codex usage in the notification area'
$lnk.Save()
Write-Host "Startup shortcut: $Shortcut"

if ($NoStart) { Write-Host 'Installed (not started).'; return }

# 6. Start and verify; roll back to the previous runtime if it doesn't come up.
function Start-AndVerify {
    # Success = the process we launched wrote tray.ready (both icon loops up)
    # and is still alive a moment later.
    $proc = Start-Process -FilePath $pythonw -ArgumentList ('"' + $target + '"') -WorkingDirectory $Runtime -PassThru
    $readyFile = Join-Path $Runtime 'tray.ready'
    $deadline = (Get-Date).AddSeconds(25)
    while ((Get-Date) -lt $deadline) {
        if ($proc.HasExited) { return $null }
        if (Test-Path -LiteralPath $readyFile) {
            $ready = (Get-Content -LiteralPath $readyFile -Raw).Trim()
            if ($ready -ne [string]$proc.Id) { return $null }
            Start-Sleep -Seconds 2
            if (-not $proc.HasExited) { return $proc.Id }
            return $null
        }
        Start-Sleep -Milliseconds 250
    }
    return $null
}

$id = Start-AndVerify
if ($id) {
    Write-Host "Running (pid $id). Log: $(Join-Path $Runtime 'tray.log')"
    Write-Host 'Tip: pin the two icons via Settings > Personalization > Taskbar > Other system tray icons.'
    return
}

Write-Warning 'New version did not start; rolling back.'
$log = Join-Path $Runtime 'tray.log'
if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log -Tail 20 | Write-Warning }
Stop-Tray
if (Test-Path -LiteralPath $Previous) {
    $failed = "$Runtime.failed"
    if (Test-Path -LiteralPath $failed) { Remove-Item -LiteralPath $failed -Recurse -Force }
    Move-Item -LiteralPath $Runtime -Destination $failed
    Move-Item -LiteralPath $Previous -Destination $Runtime
    if (Start-AndVerify) { throw "Rolled back to the previous version (failed copy kept in $failed)." }
    throw "Rollback also failed to start; see $(Join-Path $Runtime 'tray.log')."
}
throw "First install failed to start; see $log."
