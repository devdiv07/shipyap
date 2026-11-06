<#
.SYNOPSIS
  Shipyap timer: runs `shipyap run` (alias `pros run`) every 3 hours while you are logged in to Windows (Task Scheduler).

.DESCRIPTION
  The task runs as you, so it sees your user environment variables (PROS_ANTHROPIC_API_KEY,
  PROS_BUFFER_API_KEY). No key is ever written into the task. Without -EnablePaid the task
  makes no paid model request: it recovers saved work, records stories and checks posts.

  It also registers a shipyap:// link handler for your Windows user only (HKCU\Software\Classes\
  shipyap and HKCU\Software\Shipyap, which holds the config path), so the Review button on a
  "draft ready" notification opens that draft. -Remove deletes the task and both keys.

.EXAMPLE
  .\scripts\setup.ps1 -Config C:\work\my-posts\pros.toml -EnablePaid
  .\scripts\setup.ps1 -Remove
#>
param(
    [string]$Config,
    [switch]$EnablePaid,
    [switch]$Remove,
    [int]$EveryHours = 3
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'review-link.ps1')
$TaskName = 'Shipyap'
$LegacyTaskName = 'Public Research OS'  # earlier name; removed so two timers never run

if ($Remove) {
    foreach ($Name in @($TaskName, $LegacyTaskName)) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue
    }
    Unregister-ShipyapProtocol
    Write-Host "Removed the '$TaskName' task and the shipyap:// link handler. Nothing will run on its own any more."
    return
}

if (-not $Config -or -not (Test-Path -LiteralPath $Config -PathType Leaf)) {
    throw "Give -Config with the full path of your pros.toml (for example C:\work\my-posts\pros.toml)."
}
if ($EveryHours -lt 1 -or $EveryHours -gt 24) { throw "-EveryHours must be 1 to 24." }

$Repo = Split-Path -Parent $PSScriptRoot
$Pros = Join-Path $Repo '.venv\Scripts\pros.exe'
if (-not (Test-Path -LiteralPath $Pros)) { throw "Not found: $Pros. Run 'uv sync' in the repository first." }

$Wrapper = Join-Path $PSScriptRoot 'run-and-notify.ps1'
$Arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Wrapper`" -Config `"$((Resolve-Path -LiteralPath $Config).Path)`""
if ($EnablePaid) { $Arguments += ' -EnablePaid' }

$Action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $Arguments -WorkingDirectory $Repo
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Hours $EveryHours)
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

Unregister-ScheduledTask -TaskName $LegacyTaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Principal $Principal -Force | Out-Null
# Per-user (HKCU) shipyap:// handler: a notification's Review button opens that draft.
Register-ShipyapProtocol -Config (Resolve-Path -LiteralPath $Config).Path -ScriptsDirectory $PSScriptRoot

$Mode = if ($EnablePaid) { 'paid runs ON (capped by max_cost_usd per run and weekly_draft_cap)' } else { 'free runs only' }
Write-Host "Scheduled '$TaskName': every $EveryHours hours while you are logged in, $Mode."
Write-Host "First run in about 2 minutes. Results: last-run.txt in your workspace, plus a notification."
Write-Host "A 'draft ready' notification has a Review button that opens that draft (shipyap:// link, this user only)."
Write-Host "Pause everything: create a file named STOP in your workspace. Remove the task: .\scripts\setup.ps1 -Remove"
