[CmdletBinding()]
param(
    [ValidateSet('docker','wsl')][string]$Backend = 'docker',
    [string]$GatewayCommand = "$env:LOCALAPPDATA\IsolatedOpenVPNGateway\bin\vpn-gateway.cmd",
    [string[]]$OuterAdapterContains = @(),
    [string]$PrivateUrl = ''
)

$ErrorActionPreference = 'Stop'
$Distro = 'IsolatedOpenVPNGateway'
$IpUrl = 'https://api.ipify.org'

function Invoke-Text([scriptblock]$Action) {
    $value = & $Action 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Diagnostic command failed before credentials were requested." }
    return ($value | Out-String).Trim()
}

function Get-PublicIp {
    return (curl.exe -4 --noproxy '*' --fail --silent --show-error --max-time 15 $IpUrl).Trim()
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
$beforeRoutes = Get-NetRoute -AddressFamily IPv4 | Where-Object Protocol -ne Local |
    Sort-Object DestinationPrefix,InterfaceIndex,NextHop | ConvertTo-Json -Compress -Depth 5
$beforeDns = Get-DnsClientServerAddress -AddressFamily IPv4 |
    Sort-Object InterfaceIndex | Select-Object InterfaceIndex,ServerAddresses |
    ConvertTo-Json -Compress -Depth 5
$publicRoute = Find-NetRoute -RemoteIPAddress 1.1.1.1
$upAdapters = Get-NetAdapter | Where-Object Status -eq Up
$outerIndexes = @($upAdapters | Where-Object {
    $identity = "$($_.Name)`n$($_.InterfaceDescription)"
    @($OuterAdapterContains | Where-Object { $identity -like "*$_*" }).Count -gt 0
} | ForEach-Object ifIndex)

$hostIp = Get-PublicIp
$wslVersion = (& wsl.exe --version 2>&1 | Out-String).Trim()
$wslList = (& wsl.exe --list --verbose 2>&1 | Out-String).Trim()
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

$routeMatchesOuter = $OuterAdapterContains.Count -gt 0 -and $outerIndexes -contains $publicRoute.InterfaceIndex
if (-not $routeMatchesOuter) { throw 'The selected Windows public route is not on the expected outer-VPN adapter.' }
if ($backendIp -ne $hostIp) { throw 'BLOCKER: backend egress bypasses or differs from Windows outer-VPN egress.' }
if (-not (Test-Path -LiteralPath $GatewayCommand -PathType Leaf)) { throw 'Installed vpn-gateway command not found.' }

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

& $GatewayCommand stop
if ($LASTEXITCODE -ne 0) { throw 'Gateway stop failed.' }
$afterRoutes = Get-NetRoute -AddressFamily IPv4 | Where-Object Protocol -ne Local |
    Sort-Object DestinationPrefix,InterfaceIndex,NextHop | ConvertTo-Json -Compress -Depth 5
$afterDns = Get-DnsClientServerAddress -AddressFamily IPv4 |
    Sort-Object InterfaceIndex | Select-Object InterfaceIndex,ServerAddresses |
    ConvertTo-Json -Compress -Depth 5
$hostIpAfter = Get-PublicIp

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
    WslVersionAvailable = [bool]$wslVersion
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
    SocksUnavailableAfterStop = -not (Test-Tcp '127.0.0.1' 1080)
    LanAddressesBlocked = $lanBlocked
    RealPositiveAndNegativeTest = $true
} | Format-List
