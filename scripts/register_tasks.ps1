# Optional: start the BUY+SELL pair automatically with Windows Task Scheduler.
#
#   powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1                 # at logon, --loop (recommended)
#   powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1 -Mode Weekly -At 14:20
#   powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1 -DryRun         # rehearse: order_check only
#   powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1 -Unregister
#
# The task runs "only when the user is logged on": the MT5 terminals need the
# interactive desktop session, so keep the Windows user logged in (lock the
# screen instead of signing out) and keep the PC from sleeping.
param(
    [ValidateSet('Logon', 'Weekly')]
    [string]$Mode = 'Logon',
    # Weekly mode: start time in the PC's OWN clock/timezone. Must be before the
    # schedule time (minus prearm_seconds) converted to the PC's timezone.
    [string]$At = '14:20',
    [ValidateSet('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')]
    [string[]]$DaysOfWeek = @('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'),
    [string]$TaskName = 'MT5 Hedge Timer',
    [switch]$DryRun,
    [switch]$Unregister
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'."
    return
}

$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) { throw "No .venv found. Run scripts\setup.ps1 first." }
foreach ($f in 'config\buy.yaml', 'config\sell.yaml', '.env') {
    if (-not (Test-Path (Join-Path $Root $f))) { throw "$f is missing. Run scripts\setup.ps1 and fill it in." }
}

$arguments = 'run_pair.py --buy config\buy.yaml --sell config\sell.yaml'
if ($Mode -eq 'Logon') { $arguments += ' --loop' }
if ($DryRun) { $arguments += ' --dry-run' }

$action = New-ScheduledTaskAction -Execute $Python -Argument $arguments -WorkingDirectory $Root
if ($Mode -eq 'Logon') {
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
} else {
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $DaysOfWeek -At $At
}
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -WakeToRun -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'MT5 Hedge Timer: runs run_pair.py (BUY + SELL apps)' -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName' ($Mode$(if ($DryRun) { ', dry-run' }))."
Write-Host "  Command: $Python $arguments"
Write-Host "  Folder:  $Root"
Write-Host "Check it in Task Scheduler (taskschd.msc). Remove with: scripts\register_tasks.ps1 -Unregister"
