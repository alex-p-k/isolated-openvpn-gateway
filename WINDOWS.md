# Windows 11 Home installation and acceptance

Corporate OpenVPN must run only in a Linux environment. This project supports Docker Desktop Linux containers and a separate Docker-free WSL2 distro. It never installs or starts OpenVPN on Windows, changes Windows routes/DNS/Firewall, or provisions a user's ordinary WSL distro.

## Support matrix

| Backend | Windows Home | Windows Pro | Docker required |
|---|---:|---:|---:|
| docker-wsl2 | yes | yes | yes |
| native-wsl2 | yes | yes | no |
| Hyper-V VM | no | yes | no |

The Hyper-V VM row is an optional manual Pro-only fallback, not a CLI backend. Home supports both implemented paths because WSL2 uses the `Virtual Machine Platform` subset available on Home. WSL2 is still lightweight Linux-kernel virtualization; “Docker-free” does not mean “virtualization-free.”

## Recommended first use

Run `.\setup.cmd` as a normal user from the checkout; follow [QUICKSTART.md](QUICKSTART.md).
The wizard recommends WSL for a fresh Windows setup but never changes an existing
backend implicitly. It accepts a compatible single profile or IT kit. Git is
optional for browser-only use. BIOS/Windows feature enablement and a reboot still
require your explicit action; the wizard explains how to resume.

`vpn-gateway doctor --json` is the non-disruptive support entry point. It withholds
raw endpoints, DNS addresses, routes and credential-bearing child output.
`vpn-gateway setup` resumes owned stages. `.\setup.cmd --update` updates the CLI
with a public-file rollback journal; see ROLLBACK.md for scope and supported versions.

The detailed low-level commands below remain available for experienced operators.

## 1. Safe host diagnostics

Run from ordinary 64-bit PowerShell. These commands are read-only and do not reveal VPN configuration or credentials:

```powershell
$os = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
[pscustomobject]@{
  EditionId = $os.EditionID
  DisplayVersion = $os.DisplayVersion
  Build = "$($os.CurrentBuildNumber).$($os.UBR)"
  Architecture = $env:PROCESSOR_ARCHITECTURE
}

Get-CimInstance Win32_ComputerSystem |
  Select-Object @{n='RAM_GiB';e={[math]::Round($_.TotalPhysicalMemory/1GB,1)}},HypervisorPresent
Get-CimInstance Win32_Processor |
  Select-Object -First 1 VirtualizationFirmwareEnabled,SecondLevelAddressTranslationExtensions

wsl.exe --version
wsl.exe --status
wsl.exe --list --verbose

Get-NetAdapter | Where-Object Status -eq Up |
  Select-Object Name,InterfaceDescription,ifIndex,Status
Find-NetRoute -RemoteIPAddress 1.1.1.1 |
  Select-Object InterfaceIndex,InterfaceAlias,RouteMetric
Get-DnsClientServerAddress -AddressFamily IPv4 |
  Select-Object InterfaceIndex,InterfaceAlias,ServerAddresses
```

Build 22000 or later is Windows 11 even if the legacy registry `ProductName` compatibility string says Windows 10. Home normally has `EditionID` beginning with `Core`.

For Docker:

```powershell
docker --context desktop-linux version
docker context show
docker --context desktop-linux info --format '{{.OSType}}'
```

The last command must print `linux`. Windows containers are neither required nor supported by this project.

For the managed WSL distro after provisioning:

```powershell
wsl.exe -d IsolatedOpenVPNGateway -u root --exec cat /proc/1/comm
wsl.exe -d IsolatedOpenVPNGateway -u root --exec test -c /dev/net/tun
wsl.exe -d IsolatedOpenVPNGateway -u root --exec ip -4 route
```

`cat /proc/1/comm` may report `systemd`; otherwise the project uses its supervised fallback. `/dev/net/tun` is mandatory in both modes.

## 2. Install WSL2 on Home

Microsoft's normal one-time installation command is run from Administrator PowerShell and may require a reboot:

```powershell
wsl.exe --install --no-distribution
```

After reboot, return to a normal unelevated PowerShell:

```powershell
wsl.exe --update
wsl.exe --version
wsl.exe --status
```

The gateway installer itself must not run as Administrator. It requires WSL 2.4.4 or newer because the project uses the official named-distro install form and a dedicated location. It does not enable Windows features automatically.

## 3. Identify the outer VPN route

Connect the required outer VPN, then list connected adapters and the selected public route:

```powershell
Get-NetAdapter | Where-Object Status -eq Up |
  Format-Table Name,InterfaceDescription,ifIndex,Status -AutoSize
Find-NetRoute -RemoteIPAddress 1.1.1.1 |
  Format-Table InterfaceIndex,InterfaceAlias,RouteMetric -AutoSize
```

Put one or more distinctive, case-insensitive substrings from the intended adapter `Name` or `InterfaceDescription` into private `gateway.toml`:

```toml
require_outer_vpn = true
windows_outer_adapter_contains = ["Distinctive outer VPN adapter name"]
```

Use at least three characters. Startup requires the selected public route to use a matching connected adapter, then requires Windows and the selected backend to have the same IPv4 public egress. Both checks run before credential prompts. If either fails, corporate OpenVPN is not started.

Some VPN clients deliberately exclude WSL/Docker or use split routing that leaves the public route on Wi-Fi/Ethernet. That is a blocker until the outer VPN policy is corrected. Do not disable `require_outer_vpn` merely to bypass it.

## 4. Install the host package

Keep the release, private TOML and `.ovpn` profiles separate:

```powershell
Get-FileHash .\isolated-openvpn-gateway-YYYY-MM-DD.zip -Algorithm SHA256
```

Docker backend:

```powershell
py -3.11 .\install.py --check --backend docker `
  --config "C:\Secure\gateway.toml" --profiles-dir "C:\Secure\profiles"
py -3.11 .\install.py --backend docker `
  --config "C:\Secure\gateway.toml" --profiles-dir "C:\Secure\profiles"
```

Docker-free WSL backend:

```powershell
py -3.11 .\install.py --check --backend wsl `
  --config "C:\Secure\gateway.toml" --profiles-dir "C:\Secure\profiles"
py -3.11 .\install.py --backend wsl `
  --config "C:\Secure\gateway.toml" --profiles-dir "C:\Secure\profiles"
```

Private data is copied under `%LOCALAPPDATA%\IsolatedOpenVPNGateway`. The root, configuration, profiles, runtime and launchers receive a protected DACL for the numeric current-user SID with inheritance removed. ACL failure aborts and rolls back the new root.

The low-level install.py interface does not register PATH. The new wizard offers persistent User PATH registration; it never modifies Machine PATH or PowerShell profiles. Copy its two PATH setup lines into the current PowerShell window. Use the actual root from `Installed:`, particularly if a packaged terminal redirected it under `Packages\...\LocalCache`. A short command not being recognized does not mean the VPN stopped. A full-path invocation also works without changing PATH (replace the placeholder):

```powershell
& 'ACTUAL-INSTALLED-ROOT\bin\vpn-gateway.cmd' status
```

PowerShell requires `&` to invoke a quoted path. The installer now prints that operator and single-quotes paths safely, including apostrophes and literal dollar signs. Reapply the session-only setup in each new terminal; closing it rolls back that PATH change. This legacy session-only method changes no persistent settings. In contrast, the wizard records its physical location in HKCU and optionally adds one owned User PATH entry, both removed on full uninstall.

## 5. Provision a backend

Docker:

```powershell
vpn-gateway install --backend docker
```

This verifies/builds Linux images through `desktop-linux`. The Docker backend does not call WSL lifecycle commands directly.

WSL:

```powershell
vpn-gateway install --backend wsl
```

This command:

1. refuses WSL versions that cannot create a custom named distro;
2. refuses to adopt an existing unowned `IsolatedOpenVPNGateway` distro;
3. installs a separate Debian distro under the protected gateway directory;
4. installs OpenVPN, Dante, iproute2, iptables, `slirp4netns`, curl, certificates and Python inside it;
   if the Debian release has no Dante package (including Debian 13), builds official Dante 1.4.4 with a pinned upstream SHA-256 using Debian's build tools, without adding another Debian release;
5. creates a fixed UID `10000` for SOCKS;
6. records matching Windows and Linux ownership markers;
7. creates a nested Linux network namespace for OpenVPN/Dante/tun0/firewall, because Microsoft documents that WSL2 distros share their root network namespace;
8. uses `slirp4netns` for outer control egress, without root-namespace routes or firewall rules;
9. configures systemd only inside that distro, with a safe daemon fallback;
10. preserves the distro's original resolver and disables generated `resolv.conf` only in that distro.

The command does not change the user's default WSL distro or install packages in it. It does not edit `%USERPROFILE%\.wslconfig`.

Installing a distro downloads packages and may be blocked by enterprise policy. A newly created distro with established matching ownership is preserved after provisioning failure so setup can resume. No pre-existing unowned distro is adopted. An incomplete ownership step is an explicit recovery boundary.

## 6. Start, status and backend choice

```powershell
vpn-gateway start --backend docker
vpn-gateway start --backend wsl
vpn-gateway status
vpn-gateway logs
vpn-gateway test https://REAL-PRIVATE-HOST/
```

`vpn-gateway start` uses the saved backend. Change only the future saved choice with:

```powershell
vpn-gateway backend set wsl
```

This never switches a running session. `status`, `logs`, `test` and `stop` use the recorded active backend.

Credentials are prompted with no echo. They never appear in arguments, environment variables, PowerShell history or logs. WSL receives credential bytes over stdin and stores them as `/run/isolated-openvpn-gateway/auth` mode `0600`. Stop, failed start and the next start delete stale copies.

## 7. Loopback-only publication

Docker publishes `127.0.0.1:PORT`. WSL does not rely on NAT/mirrored automatic listener exposure: Dante listens only on WSL loopback port 11080, and a Windows-side stdio forwarder binds only `127.0.0.1:PORT`.

Verify after start:

```powershell
Get-NetTCPConnection -State Listen -LocalPort 1080 |
  Select-Object LocalAddress,LocalPort,OwningProcess
Test-NetConnection 127.0.0.1 -Port 1080
```

There must be an IPv4 `127.0.0.1` listener and no `0.0.0.0`, LAN-address or IPv6 `::` listener. The acceptance script tries every preferred non-loopback local IPv4 and IPv6 address (including the scope ID for IPv6 link-local addresses), as well as `::1`, and requires failure. Temporary loopback listeners on automatically allocated ports first prove that each address-family probe can detect a successful connection; both controls are closed in `finally`. An unavailable IPv6 probe does not count as a passing negative test. No Firewall rules are changed.

To check the live listener without requesting credentials, restarting or stopping the gateway:

```powershell
.\tools\windows_acceptance.ps1 -ListenerOnly
```

This reports booleans/counts, not interface addresses. It verifies local exposure only, not access from another LAN device, corporate payloads or tunnel-loss behavior; use the full acceptance flow for those separate checks.

No `netsh portproxy` is created. Windows Firewall is not weakened globally or locally.

## 8. Fail-closed behavior

The two independent WSL controls are:

1. Dante `external: tun0`;
2. UID `10000` policy route table 100 plus dedicated `IOVG_WSL_PROXY`/`IOVG_WSL_PROXY6` owner chains that allow loopback/tun0 and reject other interfaces.

Both controls live in a nested namespace reached through `slirp4netns`. WSL2 distros share the root network namespace, so placing the owner rule there could affect an equal UID in another distro; this implementation deliberately does not do that.

The normal WSL route remains available to root OpenVPN for its resolved public control endpoint. No VPN endpoint IP is required in `gateway.toml`; DNS names are resolved before corporate DNS is applied and the selected public endpoint/proxy address is pinned for that session.

Run the real positive/negative test:

```powershell
vpn-gateway compare-transports --backend wsl
```

For each transport it proves a SOCKS request succeeds first, stops only OpenVPN, verifies `tun0` disappears, verifies SOCKS fails and UID `10000` cannot force the original outer interface, then restores the connection. A missing connection without a positive control is not accepted as proof.

For WSL, the firewall probe also requires a successful root control request through the same outer interface. A temporary rule directs only UID `10000`'s public canary request to the main table, independently of table 100's unreachable route. The request must fail and the dedicated firewall's REJECT counter must increase. The probe rule is removed in `finally`; the VPN restoration attempt also runs in `finally`. A failed diagnostic command, missing namespace, unchanged counter, failed control request or failed restoration is not a passing test.

The Docker backend retains its independent container firewall, UID route and `tun0` Dante binding.

## 9. NAT and mirrored mode

The gateway detects `%USERPROFILE%\.wslconfig` as `nat`, `mirrored`, another known mode or `unknown`. It never changes the file. The stdio forwarder makes SOCKS publication independent of automatic localhost forwarding.

Outer VPN inheritance can still differ: Microsoft's mirrored mode improves compatibility with many VPNs, but it is not universally required. First run the egress preflight in the existing mode. If Windows/WSL public egress differs, stop: do not request corporate credentials and do not claim success.

If the owner of the machine explicitly chooses to test mirrored mode:

```powershell
$stamp = Get-Date -Format yyyyMMdd-HHmmss
if (Test-Path "$env:USERPROFILE\.wslconfig") {
  Copy-Item "$env:USERPROFILE\.wslconfig" "$env:USERPROFILE\.wslconfig.$stamp.backup"
}
```

After separate user approval, make only the minimal merge under `[wsl2]`:

```ini
[wsl2]
networkingMode=mirrored
```

Then:

```powershell
wsl.exe --shutdown
```

Repeat route, DNS and Windows/backend public-egress preflight. Do not add Hyper-V Firewall allow-all rules for this project. Rollback: restore the timestamped backup (or remove only the added line when there was no previous file), run `wsl --shutdown`, and repeat preflight. See `ROLLBACK.md`.

## 10. Corporate DNS

OpenVPN hooks accept pushed `dhcp-option DNS`, `DOMAIN` and `DOMAIN-SEARCH`. While `tun0` is ready, only the selected Docker namespace or managed WSL distro uses those values. On disconnect, WSL restores its captured outer resolver so the public control endpoint can reconnect.

The project never changes Windows DNS, outer-VPN DNS, NRPT or other WSL distros. If no DNS is pushed, status says so. `socks5h`, the SSH bridge and browser resolver rules pass the hostname to the proxy side.

## 11. Repository-only Git and isolated browser

```powershell
vpn-gateway git-configure "C:\work\private-repository"
git -C "C:\work\private-repository" ls-remote origin
vpn-browser https://private-host/
```

HTTPS receives an exact repository-local `socks5h` key. SSH receives repository-local `core.sshCommand` and a private host-scoped config using the Python SOCKS stdio bridge. No global Git/SSH proxy is created. Repositories remain on NTFS.

The browser uses `%LOCALAPPDATA%\IsolatedOpenVPNBrowser`, SOCKS5, proxy-side target DNS, disabled QUIC/direct WebRTC UDP, and no `direct://` fallback. It must show a network error after gateway stop. The main browser profile is untouched.

### Explicit Firefox support and the Chrome warning

Use `vpn-gateway setup --browser firefox --url https://YOUR-CORPORATE-HOST/` to
save an explicit choice, or `vpn-gateway browser --browser firefox URL` for one
launch. A separate sibling profile ending in `-Firefox` is used. Firefox 128+ is a
compatibility floor, not a claim that every old release is secure; keep it current.
No browser is installed or switched silently. Busy Firefox profiles are not
force-closed and requests are not redirected into the main profile.

Chrome retains `--host-resolver-rules`; its warning is explained, not suppressed.
Firefox remains experimental pending the paired browser/DNS checks in
[UX_ACCEPTANCE.md](UX_ACCEPTANCE.md). The synthetic `tools/browser_probe.py`
uses disposable profiles only and does not substitute for corporate tun-loss tests.

## 12. Real Windows Home acceptance

Run from the source/release directory after provisioning and connecting the outer VPN. Supply a distinctive adapter substring; optionally supply a real private URL:

```powershell
.\tools\windows_acceptance.ps1 -Backend wsl `
  -OuterAdapterContains 'Distinctive outer VPN adapter name' `
  -PrivateUrl 'https://REAL-PRIVATE-HOST/'
```

Or use `-Backend docker`. The script safely captures edition/build/architecture/RAM/virtualization, WSL/Docker state, default-route match, DNS/routes before and after, Windows/backend egress equality, systemd/tun, loopback/LAN listener isolation, positive/negative transport result and cleanup. It does not print raw route, DNS, adapter names or public IP.

If PowerShell refuses the local `.ps1` under `Restricted` or another policy, stop and inspect `Get-ExecutionPolicy -List`. Do not change `LocalMachine`/`CurrentUser`, remove an internet-zone marker, or execute the blocked script indirectly. An owner may separately approve a reviewed local script under process-only `RemoteSigned`; first record the policy state, respect Group Policy and verify persistent scopes unchanged afterward. Closing that process and its children is the rollback for a process-only setting. Without approval, record the script as blocked, not passed. See Microsoft's [execution-policy documentation](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_execution_policies?view=powershell-5.1).

Manual checks still required when applicable:

- actual corporate authentication and `Initialization Sequence Completed`;
- pushed corporate DNS and a real private hostname;
- read-only Git operation for the real remote;
- isolated browser success when up and failure when stopped;
- ordinary non-corporate Git/browser/curl continuing over the outer VPN;
- enterprise endpoint-protection and policy behavior.

Do not perform a Git push for acceptance.

## 13. Stop, uninstall and rollback

```powershell
vpn-gateway stop
vpn-gateway uninstall --backend wsl
vpn-gateway uninstall
```

Backend-only uninstall unregisters the distro only when both ownership markers prove the project created it. WSL itself and every other distro remain. Full uninstall additionally removes only project-owned files/settings and matching repository-local Git changes; Docker image cleanup runs only for a selected Docker deployment. A WSL-only uninstall never contacts Docker Desktop. The separate browser profile requires its own confirmation and can be retained.

See `ROLLBACK.md` for ownership boundaries, `.wslconfig` recovery and manual verification.

## Official sources

- [Microsoft — Install WSL](https://learn.microsoft.com/windows/wsl/install)
- [Microsoft — Basic WSL commands](https://learn.microsoft.com/windows/wsl/basic-commands)
- [Microsoft — WSL networking](https://learn.microsoft.com/windows/wsl/networking)
- [Microsoft — systemd on WSL](https://learn.microsoft.com/windows/wsl/systemd)
- [Microsoft — WSL FAQ/Home support](https://learn.microsoft.com/windows/wsl/faq)
- [Microsoft — What is WSL/shared namespaces](https://learn.microsoft.com/windows/wsl/about)
- [Docker — Windows installation and editions](https://docs.docker.com/desktop/setup/install/windows-install/)
- [Docker — WSL2 backend](https://docs.docker.com/desktop/features/wsl/)

## Verification status

Prior Windows Home/WSL evidence and limitations are preserved in [HISTORY.md](HISTORY.md).
The new setup/update/Firefox layer is not covered by that historical acceptance.
See [UX_ACCEPTANCE.md](UX_ACCEPTANCE.md) for the new, explicitly separated release gates.
