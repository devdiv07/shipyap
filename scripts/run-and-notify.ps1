<#
  What the Shipyap timer runs: `shipyap run` (through its `pros` alias), then one Windows notification per line of notify.txt
  ("Title|Text", or "Title|Text|shipyap://review/<story id>" for a toast with a Review button that
  opens that draft), using only what ships with Windows. Python never starts a process itself.

  The settings come from `pros settings` (pros reads its own TOML; this script parses none).

  With provider = "claude-plan" in the config (C3), model calls go through your Claude plan:
  the sign-in is checked first, then `pros run`; while it exits 3 (NEEDS_MODEL),
  scripts\model-plan.ps1 answers the requests it left in model-outbox and `pros run --resume`
  continues, up to plan_calls_per_day + 1 rounds. Notifications from every round are shown once,
  at the end. -EnablePaid is refused on that route: it never uses API credits.

  Any failure of this script itself (unreadable config, pros missing, a crashed round, the wrapper
  failing) still ends in a "Shipyap needs you" notification and a non-zero exit code.
#>
param(
    [Parameter(Mandatory)][string]$Config,
    [switch]$EnablePaid,
    # Tests only: pros as this program (after this first argument), the CLI the plan wrapper
    # runs, and a file that receives the notification lines instead of Windows.
    [string]$ProsFile,
    [string]$ProsPrefix,
    [string]$PlanLauncherFile,
    [string]$PlanLauncherPrefix,
    [string]$NotifyLog
)
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'review-link.ps1')
$Repo = Split-Path -Parent $PSScriptRoot
$Lines = New-Object System.Collections.Generic.List[string]
$ExitCode = 0

function Add-Line([string]$Line) {
    if ($Line -and -not $Lines.Contains($Line)) { $Lines.Add($Line) }
}

function Invoke-Pros([string[]]$Arguments) {
    $prefix = @()
    if ($ProsPrefix) { $prefix = @($ProsPrefix) }
    $output = @(& $script:Pros @prefix @Arguments)
    return @{ Code = $LASTEXITCODE; Output = $output }
}

function Invoke-Round([string[]]$Arguments) {
    # One `pros run`: its notify.txt lines, or a failure line when it wrote none and failed.
    Remove-Item -LiteralPath $script:Notify -ErrorAction SilentlyContinue  # never re-show an old one
    $run = Invoke-Pros $Arguments
    $wrote = $false
    if (Test-Path -LiteralPath $script:Notify) {
        foreach ($line in Get-Content -LiteralPath $script:Notify -Encoding UTF8) { Add-Line $line; $wrote = $true }
    }
    if ($run.Code -notin @(0, 2, 3) -or ($run.Code -eq 2 -and -not $wrote)) {
        $said = (@($run.Output) | Where-Object { $_ -like 'NEEDS_ATTENTION*' } | Select-Object -First 1)
        if (-not $said) { $said = "exit code $($run.Code)" }
        Add-Line "Shipyap needs you|pros run failed ($said); run it by hand to see why."
    }
    return $run.Code
}

function Invoke-Plan([string[]]$Arguments) {
    $plan = Join-Path $PSScriptRoot 'model-plan.ps1'
    $extra = @()
    if ($ProsFile) { $extra += @('-ProsFile', $ProsFile) }
    if ($ProsPrefix) { $extra += @('-ProsPrefix', $ProsPrefix) }
    if ($PlanLauncherFile) { $extra += @('-LauncherFile', $PlanLauncherFile) }
    if ($PlanLauncherPrefix) { $extra += @('-LauncherPrefix', $PlanLauncherPrefix) }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $plan -Config $Config @Arguments @extra | Out-Null
    return $LASTEXITCODE
}

function Show-Lines {
    if ($Lines.Count -eq 0) { return }
    if ($NotifyLog) {
        [IO.File]::AppendAllText($NotifyLog, (($Lines -join "`n") + "`n"), (New-Object Text.UTF8Encoding($false)))
        return
    }
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
    $AppId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
    foreach ($Line in $Lines) {
        $Parts = @($Line -split '\|', 3) + @('', '')
        $Xml = New-Object Windows.Data.Xml.Dom.XmlDocument
        $Xml.LoadXml((New-ShipyapToastXml $Parts[0] $Parts[1] $Parts[2]))
        $Toast = [Windows.UI.Notifications.ToastNotification]::new($Xml)
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($AppId).Show($Toast)
    }
}

try {
    $script:Pros = $ProsFile
    if (-not $script:Pros) { $script:Pros = Join-Path $Repo '.venv\Scripts\pros.exe' }
    if (-not (Test-Path -LiteralPath $script:Pros -PathType Leaf)) {
        throw "PROS_NOT_FOUND: $script:Pros is missing; run uv sync --locked --no-dev first"
    }
    $settingsRun = Invoke-Pros @('settings', '--config', $Config)
    if ($settingsRun.Code -ne 0) {
        throw ("the config could not be read (" + ((@($settingsRun.Output) | Select-Object -First 1)) + ")")
    }
    $Settings = (@($settingsRun.Output) -join "`n") | ConvertFrom-Json
    $script:Notify = Join-Path ([string]$Settings.workspace_root) 'notify.txt'

    if ($Settings.provider -eq 'claude-plan') {
        if ($EnablePaid) { throw 'the claude-plan route never uses API credits; run it without -EnablePaid' }
        $MaxRounds = [int]$Settings.plan_calls_per_day + 1
        [void](Invoke-Plan @('-AuthOnly'))
        $Arguments = @('run', '--config', $Config)
        $Code = 3
        for ($Round = 1; $Round -le $MaxRounds; $Round++) {
            $Code = Invoke-Round $Arguments
            if ($Code -ne 3) { break }
            if ((Invoke-Plan @()) -ne 0) {
                Add-Line 'Shipyap needs you|scripts\model-plan.ps1 could not answer the model requests; run it by hand to see why.'
                $Code = 1
                break
            }
            $Arguments = @('run', '--config', $Config, '--resume')
        }
        if ($Code -eq 3) {
            Add-Line "Shipyap needs you|the run still needed the model after $MaxRounds rounds; run scripts\run-and-notify.ps1 again."
        }
    } else {
        $Arguments = @('run', '--config', $Config)
        if ($EnablePaid) { $Arguments += '--allow-paid' }
        $Code = Invoke-Round $Arguments
    }
    if ($Code -ne 0) { $ExitCode = $Code }
} catch {
    $message = [string]$_.Exception.Message
    if ($message.Length -gt 180) { $message = $message.Substring(0, 180) }
    Add-Line "Shipyap needs you|run-and-notify failed: $message"
    $ExitCode = 1
}
try { Show-Lines } catch { if ($ExitCode -eq 0) { $ExitCode = 1 } }
exit $ExitCode
