# Shared helpers for install.ps1 / uninstall.ps1. Dot-source this file.

$Script:Runtime    = Join-Path $env:USERPROFILE '.ai-usage-tray'
$Script:Stage      = Join-Path $env:USERPROFILE '.ai-usage-tray.new'
$Script:Previous   = Join-Path $env:USERPROFILE '.ai-usage-tray.old'
$Script:ScriptName = 'ai_usage_tray.pyw'
$Script:QuitEvent  = 'Local\ai-usage-tray-quit'
$Script:Shortcut   = Join-Path ([Environment]::GetFolderPath('Startup')) 'AI Usage Tray.lnk'
# Claude safety-pause state; outside $Runtime so upgrades keep it.
$Script:StateDir   = Join-Path $env:LOCALAPPDATA 'ai-usage-tray'

function Find-TrayProcess {
    # The instance is exactly `<python(w).exe> "<canonical runtime script>"`:
    # the executable token must equal the process's ExecutablePath and the
    # only argument must be the canonical script. Same-named copies
    # elsewhere, or other processes merely mentioning the path, never match.
    $canonical = [regex]::Escape((Join-Path $Script:Runtime $Script:ScriptName))
    $pattern = '^\s*"?(?<exe>[^"]*\\pythonw?\.exe)"?\s+"?' + $canonical + '"?\s*$'
    $procs = @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object {
            $_.CommandLine -and $_.ExecutablePath -and
            ($_.CommandLine -match $pattern) -and
            ([string]::Equals($Matches['exe'], $_.ExecutablePath, [StringComparison]::OrdinalIgnoreCase))
        })
    $pidFile = Join-Path $Script:Runtime 'tray.pid'
    if ($procs.Count -gt 0 -and (Test-Path -LiteralPath $pidFile)) {
        $recorded = (Get-Content -LiteralPath $pidFile -Raw).Trim()
        if ($procs.Count -eq 1 -and [string]$procs[0].ProcessId -ne $recorded) {
            Write-Warning "tray.pid says $recorded but the running instance is $($procs[0].ProcessId); using the running one."
        }
    }
    return ,$procs
}

function Stop-Tray {
    # Graceful quit via the named event, then force, then abort.
    $procs = Find-TrayProcess
    if ($procs.Count -eq 0) { return }
    if ($procs.Count -gt 1) {
        throw "Found $($procs.Count) instances of the runtime script; refusing to guess. PIDs: $($procs.ProcessId -join ', ')"
    }
    $id = [int]$procs[0].ProcessId
    Write-Host "Stopping running tray (pid $id)..."
    try {
        $evt = [System.Threading.EventWaitHandle]::OpenExisting($Script:QuitEvent)
        [void]$evt.Set()
        $evt.Dispose()
    } catch {
        Write-Warning "Quit event not available ($($_.Exception.Message)); will force-stop."
    }
    if (-not (Wait-Exit $id 10)) {
        Write-Warning "pid $id did not exit in 10 s; force-stopping."
        Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
        if (-not (Wait-Exit $id 5)) { throw "pid $id is still running; aborting." }
    }
}

function Wait-Exit([int]$Id, [int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Process -Id $Id -ErrorAction SilentlyContinue)) { return $true }
        Start-Sleep -Milliseconds 250
    }
    return -not (Get-Process -Id $Id -ErrorAction SilentlyContinue)
}
