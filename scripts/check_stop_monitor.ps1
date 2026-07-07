# List (and optionally kill) MEIC stop-monitor Python processes.
#
# Usage (from MEIC-with-Dash-main-V2):
#   .\scripts\check_stop_monitor.ps1              # status only
#   .\scripts\check_stop_monitor.ps1 -Kill        # kill all stop-monitor PIDs
#   .\scripts\check_stop_monitor.ps1 -Kill -Force # skip confirmation
#
# Safe to run before launcher start and after EOD shutdown.

param(
    [switch]$Kill,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$HeartbeatPath = Join-Path $Root 'trades\heartbeat.json'

function Get-StopMonitorProcesses {
    $patterns = @(
        'blocks\stop\run.py',
        'blocks.stop.run',
        'blocks/stop/run.py'
    )
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" |
        Where-Object {
            $cmd = $_.CommandLine
            if (-not $cmd) { return $false }
            foreach ($p in $patterns) {
                if ($cmd -like "*$p*") { return $true }
            }
            return $false
        } |
        Select-Object ProcessId, Name, CreationDate, CommandLine
}

function Show-Heartbeat {
    param([string]$Path)
    if (-not (Test-Path $Path)) {
        Write-Host 'heartbeat.json: (missing)' -ForegroundColor Yellow
        return
    }
    try {
        $hb = Get-Content $Path -Raw | ConvertFrom-Json
        Write-Host 'heartbeat.json:'
        Write-Host ("  ts           = {0}" -f $hb.ts)
        Write-Host ("  engine       = {0}" -f $hb.engine)
        Write-Host ("  loop_count   = {0}" -f $hb.loop_count)
        Write-Host ("  active_slots = {0}" -f $hb.active_slots)
        if ($hb.PSObject.Properties.Name -contains 'active_exit_jobs') {
            Write-Host ("  exit_jobs    = {0}" -f $hb.active_exit_jobs)
        }
    }
    catch {
        Write-Host "heartbeat.json: (unreadable) $_" -ForegroundColor Yellow
    }
}

Write-Host "MEIC stop-monitor process check - $Root"
Write-Host ''

$procs = @(Get-StopMonitorProcesses)
if ($procs.Count -eq 0) {
    Write-Host 'Stop-monitor processes: none found' -ForegroundColor Green
}
elseif ($procs.Count -eq 1) {
    Write-Host 'Stop-monitor processes: 1 (expected when launcher is running)' -ForegroundColor Green
    $procs | Format-Table ProcessId, CreationDate, CommandLine -AutoSize
}
else {
    Write-Host ("Stop-monitor processes: {0} - DUPLICATE / ORPHAN RISK" -f $procs.Count) -ForegroundColor Red
    $procs | Format-Table ProcessId, CreationDate, CommandLine -AutoSize
}

Write-Host ''
Show-Heartbeat -Path $HeartbeatPath

if ($Kill) {
    if ($procs.Count -eq 0) {
        Write-Host ''
        Write-Host 'Nothing to kill.'
        exit 0
    }
    Write-Host ''
    if (-not $Force) {
        $answer = Read-Host ("Kill {0} process(es)? [y/N]" -f $procs.Count)
        if ($answer -notmatch '^[yY]') {
            Write-Host 'Aborted.'
            exit 1
        }
    }
    foreach ($p in $procs) {
        Write-Host ("Stopping PID {0} ..." -f $p.ProcessId)
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
    $remaining = @(Get-StopMonitorProcesses)
    if ($remaining.Count -eq 0) {
        Write-Host 'All stop-monitor processes stopped.' -ForegroundColor Green
    }
    else {
        Write-Host ("{0} process(es) still running - check Task Manager" -f $remaining.Count) -ForegroundColor Red
        exit 1
    }
}
