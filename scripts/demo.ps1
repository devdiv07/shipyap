<# Shipyap: starts a fresh offline demo and opens its local page. Ctrl+C stops it. #>
. (Join-Path $PSScriptRoot 'browser-launch.ps1')
exit (Invoke-ProsBrowser -Command demo)
