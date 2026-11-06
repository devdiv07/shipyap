<# Shipyap: opens your local review page (on one draft with -Story). Approving does not schedule or send a post. #>
param(
    [Parameter(Mandatory)][string]$Config,
    [ValidateRange(0, 65535)][int]$Port = 8765,
    [ValidatePattern('^([0-9a-f]{64})?$')][string]$Story = ''
)
. (Join-Path $PSScriptRoot 'browser-launch.ps1')
exit (Invoke-ProsBrowser -Command review -Config $Config -Port $Port -Story $Story)
