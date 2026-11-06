function Invoke-ProsBrowser {
    param(
        [ValidateSet('demo', 'review')][string]$Command,
        [string]$Config,
        [ValidateRange(0, 65535)][int]$Port = 8765
    )
    $ErrorActionPreference = 'Stop'
    $Repo = Split-Path -Parent $PSScriptRoot
    $Arguments = @('run', '--locked', '--no-dev', 'pros', $Command)
    if ($Command -eq 'review') {
        $Config = (Resolve-Path -LiteralPath $Config -ErrorAction Stop).Path
        $Arguments += @('--config', $Config, '--port', [string]$Port)
    }
    Get-Command uv -ErrorAction Stop | Out-Null
    $Opened = $false
    $ExpectUrl = $false
    Push-Location -LiteralPath $Repo
    try {
        $global:LASTEXITCODE = 0
        & uv @Arguments | ForEach-Object {
            $Line = [string]$_
            Write-Host $Line
            if ($ExpectUrl -and -not $Opened) {
                # Accept only the exact launch URL format, never arbitrary command output.
                if ($Line -cmatch '^http://127\.0\.0\.1:([0-9]{1,5})/\?token=[A-Za-z0-9_-]{32,128}$' -and
                    [int]$Matches[1] -ge 1 -and [int]$Matches[1] -le 65535) {
                    $Opened = $true
                    try { Start-Process -FilePath $Line | Out-Null }
                    catch { Write-Warning 'The browser could not open. Ctrl+click the printed URL.' }
                }
            }
            $ExpectUrl = $Line -ceq 'Open this in your browser (Ctrl+click):'
        }
        return $LASTEXITCODE
    }
    finally { Pop-Location }
}
