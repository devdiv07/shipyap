. (Join-Path $PSScriptRoot 'review-link.ps1')

function Invoke-ProsBrowser {
    param(
        [ValidateSet('demo', 'review')][string]$Command,
        [string]$Config,
        [ValidateRange(0, 65535)][int]$Port = 8765,
        [ValidatePattern('^([0-9a-f]{64})?$')][string]$Story = ''
    )
    $ErrorActionPreference = 'Stop'
    $Repo = Split-Path -Parent $PSScriptRoot
    $Arguments = @('run', '--locked', '--no-dev', 'pros', $Command)
    $Session = $null
    if ($Command -eq 'review') {
        $Config = (Resolve-Path -LiteralPath $Config -ErrorAction Stop).Path
        $Arguments += @('--config', $Config, '--port', [string]$Port)
        $Session = Get-ShipyapSessionPath $Config
    }
    Get-Command uv -ErrorAction Stop | Out-Null
    $Opened = $false
    $ExpectUrl = $false
    $Recorded = $false
    Push-Location -LiteralPath $Repo
    try {
        $global:LASTEXITCODE = 0
        & uv @Arguments | ForEach-Object {
            $Line = [string]$_
            Write-Host $Line
            if ($ExpectUrl -and -not $Opened) {
                # Accept only the exact launch URL format, never arbitrary command output.
                if ($Line -cmatch $ShipyapUrlPattern -and
                    [int]$Matches[1] -ge 1 -and [int]$Matches[1] -le 65535) {
                    $Opened = $true
                    if ($Session) {
                        # Lets a notification's Review button reuse this page (owner-only file).
                        try { Write-ShipyapSession $Session $PID $Line; $Recorded = $true }
                        catch { Write-Warning 'Notification links will start a new page (session not recorded).' }
                    }
                    $Target = if ($Story) { "$Line&story=$Story" } else { $Line }
                    try { Start-Process -FilePath $Target | Out-Null }
                    catch { Write-Warning 'The browser could not open. Ctrl+click the printed URL.' }
                }
            }
            $ExpectUrl = $Line -ceq 'Open this in your browser (Ctrl+click):'
        }
        return $LASTEXITCODE
    }
    finally {
        if ($Recorded) { Remove-ShipyapSession $Session $PID }
        Pop-Location
    }
}
