<#
  Shipyap: what Windows runs for a shipyap://review/<story id> link (the Review button on a
  "draft ready" notification). Registered for the current user only by setup.ps1.

  Deliberately no param() block: everything Windows passes lands in $args, and anything other
  than exactly one well-formed link is refused. The config path comes from HKCU\Software\Shipyap,
  never from the link. A review page that is already running is reused; otherwise one starts in
  a new console window.
#>
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'review-link.ps1')
$Config = $null
try { $Config = [string](Get-ItemProperty -LiteralPath $ShipyapConfigKey -Name 'Config').Config }
catch { exit 2 }
exit (Open-ShipyapLink -Arguments $args -Config $Config -ScriptsDirectory $PSScriptRoot)
