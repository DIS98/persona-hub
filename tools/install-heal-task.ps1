# persona-hub: register a scheduled task that re-applies the host template patch.
# Why: WorkBuddy upgrades overwrite resources/templates/*.tpl, silently killing the
# SOUL slot and the contract tail. Nothing errors - the persona just stops applying.
# This task runs every 30 minutes; tools/tpl-heal.py decides whether to act.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File tools\install-heal-task.ps1
#   powershell -ExecutionPolicy Bypass -File tools\install-heal-task.ps1 -Uninstall
#   powershell -ExecutionPolicy Bypass -File tools\install-heal-task.ps1 -Show

param(
    [switch]$Uninstall,
    [switch]$Show
)

$ErrorActionPreference = 'Stop'
$TaskName = 'persona-hub-tpl-heal'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Script = Join-Path $Root 'tools\tpl-heal.py'

if ($Show) {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $t) { Write-Output "not-registered"; exit 1 }
    $i = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Output "state=$($t.State)"
    Write-Output "last=$($i.LastRunTime) result=$($i.LastTaskResult)"
    Write-Output "next=$($i.NextRunTime)"
    exit 0
}

if ($Uninstall) {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $t) { Write-Output "not-registered"; exit 0 }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "unregistered"
    exit 0
}

if (-not (Test-Path $Script)) { throw "heal script not found: $Script" }

# Prefer pythonw.exe: no console window pops up every 30 minutes.
$py = $null
foreach ($c in @(
        (Join-Path $Root '.build\venv\Scripts\pythonw.exe'),
        (Join-Path $Root '.build\venv\Scripts\python.exe'))) {
    if (Test-Path $c) { $py = $c; break }
}
if ($null -eq $py) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($null -eq $cmd) { $cmd = Get-Command python.exe -ErrorAction SilentlyContinue }
    if ($null -ne $cmd) { $py = $cmd.Source }
}
if ($null -eq $py) { throw "no python interpreter found" }

$action = New-ScheduledTaskAction -Execute $py `
    -Argument ('"' + $Script + '" --quiet') -WorkingDirectory $Root

# Repeat forever: PS 5.1 needs an explicit duration, 3650 days is close enough.
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes 30) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

# StartWhenAvailable: a laptop asleep at the trigger time still heals on wake.
# IgnoreNew: the file lock in tpl-heal.py already covers overlap, this is belt and braces.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Force `
    -Description 'persona-hub: re-apply SOUL slot + contract tail after WorkBuddy upgrade' | Out-Null

Write-Output "registered: $TaskName"
Write-Output "python:     $py"
Write-Output "script:     $Script"
Write-Output "interval:   every 30 minutes"
