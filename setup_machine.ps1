# One-time machine setup for the AI traffic tracker.
# Run this AFTER copying the project folder to a new machine (e.g. a teammate's).
#
#   .\setup_machine.ps1              -> python deps + acme-refresh:// protocol
#                                       (dashboard REFRESH + ASK buttons work)
#   .\setup_machine.ps1 -WithTasks   -> ALSO register the scheduled refreshes.
#                                       Only ONE machine should run the weekly GEO
#                                       task, or trend histories diverge!
param([switch]$WithTasks)

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$ps1 = Join-Path $Here "refresh.ps1"

# 1. python dependencies
python -m pip install -r (Join-Path $Here "requirements.txt")

# 2. acme-refresh:// protocol -> THIS folder's refresh.ps1 (per-machine, per-path)
New-Item -Path "HKCU:\Software\Classes\acme-refresh\shell\open\command" -Force | Out-Null
Set-ItemProperty -Path "HKCU:\Software\Classes\acme-refresh" -Name "(Default)" `
    -Value "URL:Acme dashboard refresh"
New-ItemProperty -Path "HKCU:\Software\Classes\acme-refresh" -Name "URL Protocol" `
    -Value "" -PropertyType String -Force | Out-Null
Set-ItemProperty -Path "HKCU:\Software\Classes\acme-refresh\shell\open\command" `
    -Name "(Default)" `
    -Value ('powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $ps1 + '" -FromProtocol "%1"')
Write-Host "protocol registered: acme-refresh:// -> $ps1"

# 3. optional scheduled refreshes
if ($WithTasks) {
    $s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries
    $a1 = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ps1`" -Mode ga4"
    Register-ScheduledTask -TaskName "Acme Dashboard - AI traffic GA4 daily" `
        -Action $a1 -Trigger (New-ScheduledTaskTrigger -Daily -At 8:10am) -Settings $s -Force | Out-Null
    $a2 = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ps1`" -Mode geo"
    Register-ScheduledTask -TaskName "Acme Dashboard - GEO monitor weekly" `
        -Action $a2 -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 8:40am) -Settings $s -Force | Out-Null
    Write-Host "scheduled tasks registered (GA4 daily 08:10, GEO Monday 08:40)"
}

Write-Host "done - open dashboard.html (first button click: allow PowerShell in the browser prompt)"
