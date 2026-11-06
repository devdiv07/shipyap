<# Offline tests: native command and browser opening are replaced by local stubs. #>
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'browser-launch.ps1')
$StartDirectory = (Get-Location).Path
$TestRoot = Join-Path ([IO.Path]::GetTempPath()) ('pros-wrapper-' + [guid]::NewGuid().ToString('N'))
[void](New-Item -ItemType Directory -Path $TestRoot)
$Config = Join-Path $TestRoot 'config with spaces.toml'
Set-Content -LiteralPath $Config -Value '# synthetic test only'
$script:Urls = @()
$script:Lines = @()
$script:Arguments = @()
function uv {
    $script:Arguments = @($args)
    $script:Lines | ForEach-Object { Write-Output $_ }
    $global:LASTEXITCODE = $script:Code
}
function Start-Process { param([string]$FilePath) $script:Urls += $FilePath }
function Require($Condition, $Reason) { if (-not $Condition) { throw $Reason } }
try {
    $Url = 'http://127.0.0.1:8765/?token=' + ('a' * 43)
    $Heading = 'Open this in your browser (Ctrl+click):'
    $script:Code = 0
    $script:Lines = @($Heading, $Url, $Heading, $Url)
    $Code = Invoke-ProsBrowser -Command demo
    Require ($Code -eq 0 -and $script:Urls.Count -eq 1 -and $script:Urls[0] -ceq $Url) 'Demo did not open exactly once'
    Require (($script:Arguments -join '|') -ceq 'run|--locked|--no-dev|pros|demo') 'Demo args changed'
    $script:Urls = @()
    $Code = Invoke-ProsBrowser -Command review -Config $Config -Port 0
    Require ($script:Arguments[6] -ceq (Resolve-Path -LiteralPath $Config).Path) 'Config path with spaces was split'
    Require ($script:Arguments[7] -ceq '--port' -and $script:Arguments[8] -ceq '0') 'Port lost'
    Require ((Get-Location).Path -ceq $StartDirectory) 'Working directory was not restored'
    foreach ($Bad in @('https://example.org', 'http://localhost:8765/?token=abc', $Url.Replace('8765', '65536'), ($Url + '&next=https://example.org'))) {
        $script:Urls = @()
        $script:Lines = @($Heading, $Bad)
        [void](Invoke-ProsBrowser -Command demo)
        Require ($script:Urls.Count -eq 0) 'Untrusted output opened a browser'
    }
    $script:Lines = @('startup failed')
    $script:Code = 2
    Require ((Invoke-ProsBrowser -Command demo) -eq 2) 'Startup exit code lost'
    Write-Host 'BROWSER_WRAPPER_CHECK: PASS (locked args, spaces, one open, URL validation, exit/cleanup)'
}
finally {
    # Only the checked, uniquely named test directory is removed.
    $Resolved = [IO.Path]::GetFullPath($TestRoot)
    $TempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    if (-not $Resolved.StartsWith($TempRoot, [StringComparison]::OrdinalIgnoreCase) -or
        -not ([IO.Path]::GetFileName($Resolved)).StartsWith('pros-wrapper-')) { throw 'Unsafe cleanup target' }
    Remove-Item -LiteralPath $Resolved -Recurse -Force
}
# The deliberate startup-failure case leaves LASTEXITCODE nonzero. Only a fully successful
# harness reaches this point; an assertion/cleanup exception remains a failing script.
exit 0
