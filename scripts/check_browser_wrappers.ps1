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
$script:StartArguments = ''
$script:SessionAtOpen = $null
$SavedLocalAppData = $env:LOCALAPPDATA
$env:LOCALAPPDATA = $TestRoot  # session files go to the test directory only
function uv {
    $script:Arguments = @($args)
    $script:Lines | ForEach-Object { Write-Output $_ }
    $global:LASTEXITCODE = $script:Code
}
function Start-Process {
    param([string]$FilePath, [string]$ArgumentList)
    $script:Urls += $FilePath
    $script:StartArguments = $ArgumentList
    $script:SessionAtOpen = (Get-ChildItem -LiteralPath (Join-Path $TestRoot 'Shipyap') -Filter 'review-*.json' -ErrorAction SilentlyContinue | Select-Object -First 1)
}
function Require($Condition, $Reason) { if (-not $Condition) { throw $Reason } }
function Test-OwnerOnly([string]$Path) {
    $Acl = Get-Acl -LiteralPath $Path
    $Rules = @($Acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
    $Me = [Security.Principal.WindowsIdentity]::GetCurrent().User
    return $Acl.AreAccessRulesProtected -and $Rules.Count -eq 1 -and
        $Rules[0].IdentityReference -eq $Me -and $Rules[0].AccessControlType -eq 'Allow' -and
        ($Rules[0].FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -eq [Security.AccessControl.FileSystemRights]::FullControl
}
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
    $script:Code = 0

    # --- review links: notification -> one draft, owner-only session file, reuse -------------
    $Sid = 'ab' * 32
    $Session = Get-ShipyapSessionPath $Config
    $script:Urls = @()
    $script:Lines = @($Heading, $Url)
    $script:SessionAtOpen = $null
    Require ((Invoke-ProsBrowser -Command review -Config $Config -Port 0 -Story $Sid) -eq 0) 'Review with a story failed'
    Require ($script:Urls.Count -eq 1 -and $script:Urls[0] -ceq "$Url&story=$Sid") 'Story link not opened'
    Require ($null -ne $script:SessionAtOpen -and $script:SessionAtOpen.FullName -eq $Session) 'Session not recorded while the page ran'
    Require (-not (Test-Path -LiteralPath $Session)) 'Session file left behind after the page stopped'
    $script:Urls = @()
    Require ((Invoke-ProsBrowser -Command review -Config $Config -Port 0) -eq 0 -and $script:Urls[0] -ceq $Url) 'Plain review link changed'
    $Refused = $false
    try { [void](Invoke-ProsBrowser -Command review -Config $Config -Story 'not-a-story') } catch { $Refused = $true }
    Require $Refused 'A malformed -Story was accepted'

    # The token file: owner-only ACL from creation, inheritance off, removed only by its writer.
    Write-ShipyapSession $Session $PID $Url
    Require (Test-OwnerOnly $Session) 'Session file is not restricted to the current user'
    Require (Test-OwnerOnly (Split-Path -Parent $Session)) 'Session directory is not restricted to the current user'
    $Refused = $false
    try { Write-ShipyapSession $Session $PID 'https://example.org/?token=x' } catch { $Refused = $true }
    Require $Refused 'An unexpected URL was recorded'
    Remove-ShipyapSession $Session ($PID + 1)
    Require (Test-Path -LiteralPath $Session) 'Another page removed this session file'

    # Reuse needs the recorded process alive AND the port listening; anything else is ignored.
    $Listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, 0)
    $Listener.Start()
    try {
        $LivePort = $Listener.LocalEndpoint.Port
        $LiveUrl = "http://127.0.0.1:$LivePort/?token=" + ('b' * 43)
        Write-ShipyapSession $Session $PID $LiveUrl
        Require ((Get-LiveShipyapSession $Session) -ceq $LiveUrl) 'A live page was not found'
        $script:Urls = @()
        Require ((Open-ShipyapLink -Arguments @("shipyap://review/$Sid") -Config $Config -ScriptsDirectory $PSScriptRoot) -eq 0) 'Live link refused'
        Require ($script:Urls.Count -eq 1 -and $script:Urls[0] -ceq "$LiveUrl&story=$Sid") 'The running page was not reused'
        Write-ShipyapSession $Session 2147483647 $LiveUrl
        Require ($null -eq (Get-LiveShipyapSession $Session)) 'A dead process was treated as live'
        [IO.File]::WriteAllText($Session, '{"pid":' + $PID + ',"url":"http://evil.example/?token=' + ('b' * 43) + '"}')
        Require ($null -eq (Get-LiveShipyapSession $Session)) 'A foreign URL was trusted'
        [IO.File]::WriteAllText($Session, 'not json')
        Require ($null -eq (Get-LiveShipyapSession $Session)) 'A malformed session file was trusted'
    }
    finally { $Listener.Stop() }
    Write-ShipyapSession $Session $PID $LiveUrl
    Require ($null -eq (Get-LiveShipyapSession $Session)) 'A closed port was treated as live'
    Remove-ShipyapSession $Session $PID
    Require (-not (Test-Path -LiteralPath $Session)) 'The writer could not remove its session file'

    # No live page: a new console runs review.ps1 for that draft, the config quoted whole.
    $script:Urls = @()
    Require ((Open-ShipyapLink -Arguments @("shipyap://review/$Sid/") -Config $Config -ScriptsDirectory $PSScriptRoot) -eq 0) 'Fresh link refused'
    $ReviewScript = Join-Path $PSScriptRoot 'review.ps1'
    Require ($script:Urls[0] -ceq 'powershell.exe') 'No review console started'
    Require ($script:StartArguments -ceq "-NoProfile -NoExit -ExecutionPolicy Bypass -File `"$ReviewScript`" -Config `"$Config`" -Story $Sid") 'Review console arguments changed'

    # The handler takes one argument that is exactly one link; config comes from the registry.
    $BadLinks = @(
        @(), @("shipyap://review/$Sid", '-Config'), @("shipyap://review/$Sid", "shipyap://review/$Sid"),
        @("shipyap://review/$($Sid.ToUpper())"), @("shipyap://review/$($Sid.Substring(1))"),
        @("shipyap://review/$Sid" + 'a'), @("shipyap://review/$Sid`" -Config `"x"),
        @("shipyap://review/$Sid?x=1"), @("http://review/$Sid"), @("SHIPYAP://review/$Sid"),
        @("shipyap://review/../$Sid"), @("shipyap://review/$Sid//"), @("shipyap://review/$Sid`n")
    )
    foreach ($Bad in $BadLinks) {
        $script:Urls = @()
        Require ((Open-ShipyapLink -Arguments $Bad -Config $Config -ScriptsDirectory $PSScriptRoot) -eq 2 -and $script:Urls.Count -eq 0) "A bad link was followed: $($Bad -join ' ')"
    }
    foreach ($BadConfig in @('', (Join-Path $TestRoot 'missing.toml'), 'C:\a"b.toml', $TestRoot)) {
        Require ((Open-ShipyapLink -Arguments @("shipyap://review/$Sid") -Config $BadConfig -ScriptsDirectory $PSScriptRoot) -eq 2) "A bad config was used: $BadConfig"
    }

    # What setup.ps1 registers (checked as text; the registry is not touched here).
    $Command = Get-ShipyapProtocolCommand $PSScriptRoot
    $Handler = Join-Path $PSScriptRoot 'open-review.ps1'
    Require ($Command.StartsWith('"' + (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') + '" ')) 'Handler does not use Windows PowerShell by full path'
    Require ($Command.EndsWith(" -File `"$Handler`" `"%1`"")) 'Handler must pass the link as one quoted argument, last'
    Require ($Command -cmatch ' -NoProfile ' -and $Command -cmatch ' -NonInteractive ') 'Handler flags changed'
    Require ($ShipyapClassKey -ceq 'HKCU:\Software\Classes\shipyap' -and $ShipyapConfigKey -ceq 'HKCU:\Software\Shipyap') 'Handler is not per-user'
    $Refused = $false
    try { [void](Get-ShipyapProtocolCommand 'C:\a"b') } catch { $Refused = $true }
    Require $Refused 'A quote in the repository path was accepted'
    $SetupText = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'setup.ps1') -Raw
    Require ($SetupText -match 'Register-ShipyapProtocol' -and $SetupText -match 'Unregister-ShipyapProtocol') 'setup.ps1 does not register and remove the handler'
    Require ($SetupText -notmatch 'HKLM|HKEY_LOCAL_MACHINE') 'setup.ps1 must not touch HKLM'

    # Toasts: text always escaped; only a valid link gets a click target and a Review button.
    $Toast = [xml](New-ShipyapToastXml 'Shipyap: 2 drafts <ready> & "set"' 'Body' "shipyap://review/$Sid")
    Require ($Toast.toast.visual.binding.text[0] -ceq 'Shipyap: 2 drafts <ready> & "set"') 'Toast title not preserved as text'
    Require ($Toast.toast.activationType -ceq 'protocol' -and $Toast.toast.launch -ceq "shipyap://review/$Sid") 'Toast click target missing'
    Require ($Toast.toast.actions.action.content -ceq 'Review' -and $Toast.toast.actions.action.arguments -ceq "shipyap://review/$Sid") 'Review button missing'
    foreach ($NoLink in @('', 'https://example.org', "shipyap://review/$Sid&x")) {
        $Plain = [xml](New-ShipyapToastXml 'Title' 'Text' $NoLink)
        Require ($null -eq $Plain.toast.actions -and -not $Plain.toast.HasAttribute('launch')) "A toast without a valid link got a target: $NoLink"
    }
    Write-Host 'BROWSER_WRAPPER_CHECK: PASS (locked args, spaces, one open, URL validation, exit/cleanup, review links, owner-only session, handler)'
}
finally {
    $env:LOCALAPPDATA = $SavedLocalAppData
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
