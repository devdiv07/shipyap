<#
  Shipyap review links, shared by setup.ps1, run-and-notify.ps1, browser-launch.ps1 and
  open-review.ps1 (dot-source this file). Nothing here runs on load.

  - A toast for a ready draft carries shipyap://review/<64-hex story id>. Its Review button opens
    that link, which Windows hands to open-review.ps1 through a per-user (HKCU) protocol handler.
  - The handler never takes a config, port or URL from the link: the config path comes from
    HKCU\Software\Shipyap, written by setup.ps1, and the link must be exactly one story id.
  - While a review page runs, browser-launch.ps1 keeps a session file holding its sign-in URL so a
    later click reuses that page. The URL contains the page's token, which unlocks the page, so
    the file is created with an ACL that grants the current user alone, and is deleted when the
    page stops.
#>

# \z, not $: in .NET, $ also matches before a trailing newline.
$ShipyapLinkPattern = '^shipyap://review/([0-9a-f]{64})/?\z'
$ShipyapUrlPattern = '^http://127\.0\.0\.1:([0-9]{1,5})/\?token=[A-Za-z0-9_-]{32,128}\z'
$ShipyapConfigKey = 'HKCU:\Software\Shipyap'
$ShipyapClassKey = 'HKCU:\Software\Classes\shipyap'

function Get-ShipyapStoryId([string]$Uri) {
    if ($Uri -cmatch $ShipyapLinkPattern) { return $Matches[1] }
    return $null
}

function Get-ShipyapProtocolCommand([string]$ScriptsDirectory) {
    $Handler = Join-Path $ScriptsDirectory 'open-review.ps1'
    if ($Handler.Contains('"')) { throw 'The repository path must not contain a double quote.' }
    $PowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    # "%1" is the whole link as one argument; open-review.ps1 refuses anything else.
    return "`"$PowerShell`" -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Handler`" `"%1`""
}

function Register-ShipyapProtocol([string]$Config, [string]$ScriptsDirectory) {
    if ($Config.Contains('"')) { throw 'The config path must not contain a double quote.' }
    $Command = Get-ShipyapProtocolCommand $ScriptsDirectory
    New-Item -Path $ShipyapConfigKey -Force | Out-Null
    Set-ItemProperty -LiteralPath $ShipyapConfigKey -Name 'Config' -Value $Config
    New-Item -Path "$ShipyapClassKey\shell\open\command" -Force | Out-Null
    Set-ItemProperty -LiteralPath $ShipyapClassKey -Name '(default)' -Value 'URL:Shipyap review link'
    Set-ItemProperty -LiteralPath $ShipyapClassKey -Name 'URL Protocol' -Value ''
    Set-ItemProperty -LiteralPath "$ShipyapClassKey\shell\open\command" -Name '(default)' -Value $Command
}

function Unregister-ShipyapProtocol {
    foreach ($Key in @($ShipyapClassKey, $ShipyapConfigKey)) {
        if (Test-Path -LiteralPath $Key) { Remove-Item -LiteralPath $Key -Recurse -Force }
    }
}

function Get-ShipyapSessionPath([string]$Config) {
    $Full = [IO.Path]::GetFullPath($Config).ToLowerInvariant()
    $Sha = [Security.Cryptography.SHA256]::Create()
    try { $Digest = $Sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Full)) } finally { $Sha.Dispose() }
    $Name = 'review-' + (-join ($Digest[0..7] | ForEach-Object { $_.ToString('x2') })) + '.json'
    return Join-Path (Join-Path $env:LOCALAPPDATA 'Shipyap') $Name
}

function New-ShipyapOwnerOnlyRule {
    $Me = [Security.Principal.WindowsIdentity]::GetCurrent().User
    return New-Object Security.AccessControl.FileSystemAccessRule($Me, 'FullControl', 'Allow')
}

function Write-ShipyapSession([string]$Path, [int]$ProcessId, [string]$Url) {
    if ($Url -cnotmatch $ShipyapUrlPattern) { throw 'Refusing to record an unexpected review URL.' }
    $Directory = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $Directory -PathType Container)) {
        $DirectorySecurity = New-Object Security.AccessControl.DirectorySecurity
        $DirectorySecurity.SetAccessRuleProtection($true, $false)
        $Me = [Security.Principal.WindowsIdentity]::GetCurrent().User
        $DirectorySecurity.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
            $Me, 'FullControl', 'ContainerInherit, ObjectInherit', 'None', 'Allow')))
        [void][IO.Directory]::CreateDirectory($Directory, $DirectorySecurity)
    }
    if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Force }
    # The ACL is part of creation, so the token is never in a file anyone else can read:
    # inheritance off, one rule (current user, full control).
    $Security = New-Object Security.AccessControl.FileSecurity
    $Security.SetAccessRuleProtection($true, $false)
    $Security.AddAccessRule((New-ShipyapOwnerOnlyRule))
    $Json = ConvertTo-Json -Compress -InputObject ([ordered]@{ pid = $ProcessId; url = $Url })
    $Bytes = (New-Object Text.UTF8Encoding($false)).GetBytes($Json)
    $Stream = New-Object IO.FileStream($Path, [IO.FileMode]::CreateNew,
        [Security.AccessControl.FileSystemRights]::Write, [IO.FileShare]::None, 4096,
        [IO.FileOptions]::None, $Security)
    try { $Stream.Write($Bytes, 0, $Bytes.Length) } finally { $Stream.Dispose() }
}

function Remove-ShipyapSession([string]$Path, [int]$ProcessId) {
    # Only the page that wrote the file removes it; a newer page's file is left alone.
    try {
        $Session = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
        if ([int]$Session.pid -eq $ProcessId) { Remove-Item -LiteralPath $Path -Force }
    }
    catch { }
}

function Test-ShipyapPort([int]$Port) {
    $Client = New-Object Net.Sockets.TcpClient
    try { return ($Client.ConnectAsync('127.0.0.1', $Port).Wait(1000) -and $Client.Connected) }
    catch { return $false }
    finally { $Client.Dispose() }
}

function Get-LiveShipyapSession([string]$Path) {
    # The sign-in URL of a review page that is running now, or $null. A stale, foreign or
    # malformed file is ignored (and a page that died is noticed by its process and port).
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try { $Session = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { return $null }
    $Url = [string]$Session.url
    if ($Url -cnotmatch $ShipyapUrlPattern) { return $null }
    $Port = [int]$Matches[1]
    if ($Port -lt 1 -or $Port -gt 65535 -or -not ($Session.pid -is [int] -or $Session.pid -is [long])) {
        return $null
    }
    $Process = Get-Process -Id ([int]$Session.pid) -ErrorAction SilentlyContinue
    if ($null -eq $Process -or $Process.ProcessName -notin @('powershell', 'pwsh')) { return $null }
    if (-not (Test-ShipyapPort $Port)) { return $null }
    return $Url
}

function Open-ShipyapLink([object[]]$Arguments, [string]$Config, [string]$ScriptsDirectory) {
    # Exit code for open-review.ps1: 0 opened, 2 refused. Exactly one argument, one story id.
    if ($Arguments.Count -ne 1) { return 2 }
    $Story = Get-ShipyapStoryId ([string]$Arguments[0])
    if (-not $Story) { return 2 }
    if (-not $Config -or $Config.Contains('"') -or -not (Test-Path -LiteralPath $Config -PathType Leaf)) {
        return 2
    }
    $Url = Get-LiveShipyapSession (Get-ShipyapSessionPath $Config)
    if ($Url) {
        Start-Process -FilePath "$Url&story=$Story"
        return 0
    }
    $Review = Join-Path $ScriptsDirectory 'review.ps1'
    # A visible console: the page runs there until Ctrl+C, and any startup error stays readable.
    Start-Process -FilePath 'powershell.exe' -ArgumentList (
        "-NoProfile -NoExit -ExecutionPolicy Bypass -File `"$Review`" -Config `"$Config`" -Story $Story")
    return 0
}

function New-ShipyapToastXml([string]$Title, [string]$Text, [string]$Link) {
    # ToastGeneric text is set through the DOM, so it is always escaped. Only a valid review link
    # gets a click target and a Review button.
    $Doc = New-Object Xml.XmlDocument
    $Toast = $Doc.AppendChild($Doc.CreateElement('toast'))
    $Binding = $Toast.AppendChild($Doc.CreateElement('visual')).AppendChild($Doc.CreateElement('binding'))
    $Binding.SetAttribute('template', 'ToastGeneric')
    foreach ($Value in @($Title, $Text)) {
        [void]$Binding.AppendChild($Doc.CreateElement('text')).AppendChild($Doc.CreateTextNode($Value))
    }
    if (Get-ShipyapStoryId $Link) {
        $Toast.SetAttribute('activationType', 'protocol')
        $Toast.SetAttribute('launch', $Link)
        $Action = $Toast.AppendChild($Doc.CreateElement('actions')).AppendChild($Doc.CreateElement('action'))
        $Action.SetAttribute('content', 'Review')
        $Action.SetAttribute('activationType', 'protocol')
        $Action.SetAttribute('arguments', $Link)
    }
    return $Doc.OuterXml
}
