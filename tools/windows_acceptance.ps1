[CmdletBinding()]
param(
    [ValidateSet('docker','wsl')][string]$Backend = 'docker',
    [string]$GatewayCommand = "$env:LOCALAPPDATA\IsolatedOpenVPNGateway\bin\vpn-gateway.cmd",
    [string[]]$OuterAdapterContains = @(),
    [string]$PrivateUrl = ''
)

$ErrorActionPreference = 'Stop'
$Distro = 'IsolatedOpenVPNGateway'
$IpUrl = 'https://checkip.amazonaws.com'

function Invoke-Text([scriptblock]$Action) {
    $value = & $Action 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Diagnostic command failed before credentials were requested." }
    return ($value | Out-String).Trim()
}

function Get-PublicIp {
    $value = Invoke-Text { curl.exe -4 --noproxy '*' --fail --silent --show-error --max-time 15 $IpUrl }
    $address = $null
    if (-not [Net.IPAddress]::TryParse($value, [ref]$address) -or
        $address.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) {
        throw 'Public IPv4 egress is unavailable.'
    }
    return $address.ToString()
}

function Get-RouteFingerprint {
    return (Get-NetRoute | Where-Object Protocol -ne Local |
        Select-Object AddressFamily,DestinationPrefix,InterfaceIndex,NextHop,RouteMetric,Protocol |
        Sort-Object AddressFamily,DestinationPrefix,InterfaceIndex,NextHop |
        ConvertTo-Json -Compress -Depth 5)
}

function Get-DnsFingerprint {
    return (Get-DnsClientServerAddress |
        Select-Object InterfaceIndex,AddressFamily,ServerAddresses |
        Sort-Object InterfaceIndex,AddressFamily | ConvertTo-Json -Compress -Depth 5)
}

function Test-Tcp([string]$Address, [int]$Port) {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $wait = $client.ConnectAsync($Address, $Port)
        return $wait.Wait(1500) -and $client.Connected
    } catch { return $false } finally { $client.Dispose() }
}

$os = Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
$computer = Get-CimInstance Win32_ComputerSystem
$processor = Get-CimInstance Win32_Processor | Select-Object -First 1
$editionHome = $os.EditionID -like 'Core*' -or $os.ProductName -like '* Home*'
$beforeRoutes = Get-RouteFingerprint
$beforeDns = Get-DnsFingerprint
$publicRoute = Find-NetRoute -RemoteIPAddress 1.1.1.1
$upAdapters = Get-NetAdapter | Where-Object Status -eq Up
$outerIndexes = @($upAdapters | Where-Object {
    $identity = "$($_.Name)`n$($_.InterfaceDescription)"
    @($OuterAdapterContains | Where-Object {
        $identity.IndexOf($_,[StringComparison]::OrdinalIgnoreCase) -ge 0
    }).Count -gt 0
} | ForEach-Object ifIndex)

$hostIp = Get-PublicIp
$wslVersion = ''
$wslList = ''
$wslAvailable = $false
if (Get-Command wsl.exe -ErrorAction SilentlyContinue) {
    try {
        $wslVersion = (& wsl.exe --version 2>&1 | Out-String).Replace([string][char]0,'').Trim()
        $wslAvailable = $LASTEXITCODE -eq 0 -and $wslVersion -match 'WSL[^0-9]*[0-9]+\.'
        $wslList = (& wsl.exe --list --verbose 2>&1 | Out-String).Replace([string][char]0,'').Trim()
    } catch {
        if ($Backend -eq 'wsl') { throw 'WSL diagnostics are unavailable.' }
    }
}
$wslMode = 'nat'
$wslConfig = Join-Path $env:USERPROFILE '.wslconfig'
if (Test-Path -LiteralPath $wslConfig) {
    $match = Select-String -LiteralPath $wslConfig -Pattern '^\s*networkingMode\s*=\s*([^\s#;]+)'
    if ($match) { $wslMode = $match.Matches[0].Groups[1].Value.ToLowerInvariant() }
}

$dockerVersion = $null
$dockerContext = $null
$dockerOs = $null
$backendIp = $null
$systemd = $null
$tun = $null
$routeIndexes = @($publicRoute | ForEach-Object InterfaceIndex | Sort-Object -Unique)
$routeMatchesOuter = $OuterAdapterContains.Count -gt 0 -and $routeIndexes.Count -gt 0 -and
    @($routeIndexes | Where-Object { $outerIndexes -notcontains $_ }).Count -eq 0
if (-not $routeMatchesOuter) { throw 'The selected Windows public route is not on the expected outer-VPN adapter.' }
if (-not (Test-Path -LiteralPath $GatewayCommand -PathType Leaf)) { throw 'Installed vpn-gateway command not found.' }

try {
if ($Backend -eq 'docker') {
    $dockerVersion = Invoke-Text { docker.exe --context desktop-linux version --format '{{.Server.Version}}' }
    $dockerContext = Invoke-Text { docker.exe context show }
    $dockerOs = Invoke-Text { docker.exe --context desktop-linux info --format '{{.OSType}}' }
    if ($dockerOs -ne 'linux') { throw 'Docker is not using Linux containers.' }
    $backendIp = Invoke-Text {
        docker.exe --context desktop-linux run --rm --cap-drop ALL --read-only curlimages/curl:latest -4 --noproxy '*' -fsS --max-time 15 $IpUrl
    }
} else {
    if ($wslList -notmatch [regex]::Escape($Distro) -or $wslList -notmatch "(?m)^\s*\*?\s*$([regex]::Escape($Distro))\s+\S+\s+2\s*$") {
        throw 'The project-managed WSL2 distro is not installed as WSL version 2.'
    }
    wsl.exe -d $Distro -u root --exec sh -c 'test "$(cat /proc/1/comm)" = systemd' 2>$null
    $systemd = $LASTEXITCODE -eq 0
    wsl.exe -d $Distro -u root --exec test -c /dev/net/tun 2>$null
    $tun = $LASTEXITCODE -eq 0
    if (-not $tun) { throw '/dev/net/tun is unavailable in the managed WSL2 distro.' }
    $backendIp = Invoke-Text {
        wsl.exe -d $Distro -u root --exec python3 /opt/isolated-openvpn-gateway/wsl_manager.py network-egress
    }
}

if ($backendIp -ne $hostIp) { throw 'BLOCKER: backend egress bypasses or differs from Windows outer-VPN egress.' }

& $GatewayCommand compare-transports --backend $Backend
if ($LASTEXITCODE -ne 0) { throw 'Positive/negative gateway transport acceptance failed.' }
if ($PrivateUrl) {
    & $GatewayCommand test $PrivateUrl --backend $Backend
} else {
    & $GatewayCommand test --backend $Backend
}
if ($LASTEXITCODE -ne 0) { throw 'Gateway validation failed.' }

$listeners = @(Get-NetTCPConnection -State Listen -LocalPort 1080 -ErrorAction SilentlyContinue)
$loopbackOnly = $listeners.Count -gt 0 -and @($listeners | Where-Object LocalAddress -ne '127.0.0.1').Count -eq 0
$lanAddresses = @(Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
    $_.IPAddress -ne '127.0.0.1' -and $_.AddressState -eq 'Preferred'
} | ForEach-Object IPAddress)
$lanBlocked = @($lanAddresses | Where-Object { Test-Tcp $_ 1080 }).Count -eq 0
if (-not $loopbackOnly -or -not $lanBlocked) { throw 'SOCKS listener is not proven loopback-only.' }
} finally {
    & $GatewayCommand stop --backend $Backend
    if ($LASTEXITCODE -ne 0) { throw 'Gateway stop/credential cleanup failed.' }
}
$afterRoutes = Get-RouteFingerprint
$afterDns = Get-DnsFingerprint
$hostIpAfter = Get-PublicIp
$socksStopped = -not (Test-Tcp '127.0.0.1' 1080)

[pscustomobject]@{
    WindowsEdition = $os.ProductName
    EditionId = $os.EditionID
    WindowsHome = $editionHome
    DisplayVersion = $os.DisplayVersion
    Build = "$($os.CurrentBuildNumber).$($os.UBR)"
    Architecture = $env:PROCESSOR_ARCHITECTURE
    RamGiB = [math]::Round($computer.TotalPhysicalMemory / 1GB, 1)
    HypervisorPresent = $computer.HypervisorPresent
    VirtualizationFirmwareEnabled = $processor.VirtualizationFirmwareEnabled
    WslVersionAvailable = $wslAvailable
    WslManagedDistroPresent = $wslList -match [regex]::Escape($Distro)
    WslNetworkingMode = $wslMode
    SystemdAvailable = $systemd
    TunAvailable = $tun
    DockerVersion = $dockerVersion
    DockerContext = $dockerContext
    DockerOs = $dockerOs
    OuterRouteMatched = $routeMatchesOuter
    HostAndBackendEgressMatched = $hostIp -eq $backendIp
    HostPublicIpPreserved = $hostIp -eq $hostIpAfter
    HostRoutesPreserved = $beforeRoutes -eq $afterRoutes
    HostDnsPreserved = $beforeDns -eq $afterDns
    SocksLoopbackOnly = $loopbackOnly
    SocksUnavailableAfterStop = $socksStopped
    LanAddressesBlocked = $lanBlocked
    RealPositiveAndNegativeTest = $true
} | Format-List
if ($beforeRoutes -ne $afterRoutes -or $beforeDns -ne $afterDns -or
    $hostIp -ne $hostIpAfter -or -not $socksStopped) {
    throw 'Host preservation or post-stop isolation acceptance failed.'
}
