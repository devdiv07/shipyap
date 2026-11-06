<#
  What the Shipyap timer runs: `shipyap run` (through its `pros` alias), then one Windows notification per line of notify.txt
  ("Title|Text"), using only what ships with Windows. Python never starts a process itself.
#>
param([Parameter(Mandatory)][string]$Config, [switch]$EnablePaid)

$Repo = Split-Path -Parent $PSScriptRoot
$Pros = Join-Path $Repo '.venv\Scripts\pros.exe'
$Arguments = @('run', '--config', $Config)
if ($EnablePaid) { $Arguments += '--allow-paid' }
& $Pros @Arguments | Out-Null

$Root = (Select-String -LiteralPath $Config -Pattern "^workspace_root\s*=\s*'(.+)'").Matches.Groups[1].Value
$Notify = Join-Path $Root 'notify.txt'
if (-not (Test-Path -LiteralPath $Notify)) { return }

[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$AppId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
foreach ($Line in Get-Content -LiteralPath $Notify -Encoding UTF8) {
    if (-not $Line) { continue }
    $Parts = $Line -split '\|', 2
    $Template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
        [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
    $Texts = $Template.GetElementsByTagName('text')
    [void]$Texts.Item(0).AppendChild($Template.CreateTextNode($Parts[0]))
    [void]$Texts.Item(1).AppendChild($Template.CreateTextNode($(if ($Parts.Count -gt 1) { $Parts[1] } else { '' })))
    $Toast = [Windows.UI.Notifications.ToastNotification]::new($Template)
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($AppId).Show($Toast)
}
