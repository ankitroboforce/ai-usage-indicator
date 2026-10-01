# Install or upgrade AI Usage Tray for the current user. Re-run to upgrade.
#   powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
#   -Python C:\path\to\python.exe   use a specific interpreter (pythonw.exe must sit beside it)
#   -NoPin                          don't switch the icons to "show on taskbar"
[CmdletBinding()]
param([switch]$NoStart, [string]$Python, [switch]$NoPin)

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

function Set-TrayPromoted {
    # Windows 11 lists notification-area icons under
    # HKCU\Control Panel\NotifyIconSettings\<id>; IsPromoted=1 is the
    # "Other system tray icons" switch. Icons without a GUID get one entry
    # per executable, so this shows both icons (and any other pythonw tray
    # app). Explorer creates the entry shortly after the icon first appears.
    # Explorer creates the entry ~2 s after a new app's icon first appears
    # (measured), and applies IsPromoted immediately. Paths under known
    # folders are stored as "{KNOWNFOLDERID}\rest", e.g. Program Files.
    $root = 'HKCU:\Control Panel\NotifyIconSettings'
    if (-not (Test-Path -LiteralPath $root)) { return $false }   # Windows 10
    $programFiles = if ($env:ProgramW6432) { $env:ProgramW6432 } else { $env:ProgramFiles }
    $knownFolders = @{
        '{6D809377-6AF0-444B-8957-A3773F02200E}' = $programFiles
        '{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}' = ${env:ProgramFiles(x86)}
        '{F38BF404-1D43-42F2-9305-67DE0B28FC23}' = $env:windir
        '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}' = (Join-Path $env:windir 'System32')
        '{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}' = $env:LOCALAPPDATA
        '{3EB685DB-65F9-4CF6-A03A-E3EF65729F3D}' = $env:APPDATA
        '{5E6C858F-0E22-4760-9AFE-EA3317B67173}' = $env:USERPROFILE
    }
    $expand = {
        param([string]$stored)
        if ($stored -match '^(\{[0-9A-Fa-f-]{36}\})(\\.*)$' -and $knownFolders.ContainsKey($Matches[1].ToUpperInvariant())) {
            return $knownFolders[$Matches[1].ToUpperInvariant()] + $Matches[2]
        }
        return $stored
    }
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        $entries = @(Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue | Where-Object {
            $p = Get-ItemProperty -LiteralPath $_.PSPath -ErrorAction SilentlyContinue
            $p -and $p.ExecutablePath -and
            [string]::Equals((& $expand ([string]$p.ExecutablePath)), $pythonw, [StringComparison]::OrdinalIgnoreCase)
        })
        if ($entries.Count -gt 0) {
            foreach ($e in $entries) {
                Set-ItemProperty -LiteralPath $e.PSPath -Name IsPromoted -Value 1 -Type DWord
            }
            return $true
        }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

$id = Start-AndVerify
if ($id) {
    Write-Host "Running (pid $id). Log: $(Join-Path $Runtime 'tray.log')"
    $pinned = $false
    if (-not $NoPin) {
        try { $pinned = Set-TrayPromoted } catch { Write-Warning "Could not pin the icons: $($_.Exception.Message)" }
    }
    if ($pinned) {
        Write-Host 'Icons set to show on the taskbar (Settings > Personalization > Taskbar > Other system tray icons > Python).'
    } else {
        Write-Host 'Tip: to keep the icons visible, turn on "Python" under Settings > Personalization > Taskbar > Other system tray icons.'
    }
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
