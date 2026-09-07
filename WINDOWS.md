# Windows 11 Home installation and acceptance

Corporate OpenVPN must run only in a Linux environment. This project supports Docker Desktop Linux containers and a separate Docker-free WSL2 distro. It never installs or starts OpenVPN on Windows, changes Windows routes/DNS/Firewall, or provisions a user's ordinary WSL distro.

## Support matrix

| Backend | Windows Home | Windows Pro | Docker required |
|---|---:|---:|---:|
| docker-wsl2 | yes | yes | yes |
| native-wsl2 | yes | yes | no |
| Hyper-V VM | no | yes | no |

The Hyper-V VM row is an optional manual Pro-only fallback, not a CLI backend. Home supports both implemented paths because WSL2 uses the `Virtual Machine Platform` subset available on Home. WSL2 is still lightweight Linux-kernel virtualization; “Docker-free” does not mean “virtualization-free.”

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

The installer does not change persistent User/Machine PATH. Copy its two PATH setup lines into the current PowerShell window. Use the actual root from `Installed:`, particularly if a packaged terminal redirected it under `Packages\...\LocalCache`. A short command not being recognized does not mean the VPN stopped. A full-path invocation also works without changing PATH (replace the placeholder):

```powershell
& 'ACTUAL-INSTALLED-ROOT\bin\vpn-gateway.cmd' status
```

PowerShell requires `&` to invoke a quoted path. The installer now prints that operator and single-quotes paths safely, including apostrophes and literal dollar signs. Reapply the session-only setup in each new terminal; closing it rolls back that PATH change. No persistent PATH, PowerShell profile, execution policy or registry entry is modified.

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

Installing a distro downloads packages and may be blocked by enterprise policy. A partial distro created by the same failed attempt is unregistered as rollback; pre-existing distros are never unregistered.

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

Backend-only uninstall unregisters the distro only when both ownership markers prove the project created it. WSL itself and every other distro remain. Full uninstall additionally removes only project-owned files/images/settings and matching repository-local Git changes. The separate browser profile requires a second confirmation.

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

## Current verification evidence and limitations

The development host is Windows 11 Home Single Language (`EditionID=CoreSingleLanguage`, 25H2, build `26200.9168`), AMD64, 31.8 GiB RAM. On 2026-09-07, after reboot, WSL 2.7.12 and the hypervisor were confirmed active. A separate Debian WSL2 distro was created and real checks confirmed systemd and `/dev/net/tun`.

WSL NAT timed out reaching public services; this was unavailable egress, not evidence of a bypass. After the machine owner's separate approval, the originally absent `.wslconfig` was recorded for rollback, a minimal `networkingMode=mirrored` file was created and WSL was restarted. Windows, root WSL and the nested gateway namespace then returned the same public IPv4; Windows selected the configured outer VPN adapter. Provisioning completed, including verified upstream Dante 1.4.4. Windows routes, DNS and ordinary egress were unchanged across the infrastructure tests. Subsequent transient egress failures correctly blocked tests; later repetitions passed. Public egress diagnostics now allow one bounded retry for connection errors; a valid mismatching IP still blocks immediately. Mirrored mode is not a universal VPN compatibility guarantee.

Real infrastructure checks passed: the root HTTPS outer control succeeded while SOCKS UID 10000 was rejected, the firewall counter increased, and the narrow test route was removed. A separate **synthetic** TUN test confirmed Dante configuration/startup and SOCKS handshake through the Windows loopback-only forwarder. `stop` removed the listener and credentials were absent. Corporate OpenVPN was never launched for these tests, so they do not prove corporate tunnel fail-closed operation. Linux assets and auth input are transferred as exact UTF-8 bytes to prevent Windows stdin CRLF conversion.

The source-built Dante fallback is pinned, not managed by Debian security updates. Updating it requires reviewing a new upstream release/checksum and repeating the runtime tests; do not silently download an unpinned latest version. Ordinary Debian dependencies continue to use Debian packages.

After the user entered credentials interactively, real corporate acceptance passed for two supplied UDP profiles: OpenVPN initialized, tun0 and UID routing were present inside the gateway namespace, and a TCP DNS payload reached a pushed DNS server through the Windows SOCKS endpoint. Each positive/negative test stopped only OpenVPN, confirmed tun0 absent and SOCKS blocked, observed a firewall REJECT counter increment for UID 10000 while root HTTPS through eth0 succeeded, removed its narrow probe route, and restored the VPN plus successful DNS payload. The DNS canary used a configured hostname; this alone does not prove access to every private application.

Host snapshots before and after the connected tests confirmed Windows DNS, default/static/public routes, public egress and global Git proxy unchanged. Windows OpenVPN processes were absent. Linux credentials were root-owned mode 600. After a later outer-preflight failure interrupted transport comparison, cleanup removed the real auth file and Windows listener. TCP443 was **not** tested: WSL public egress was unavailable both before that attempt and before restoring the original transport. Corporate OpenVPN was not started on an unverified outer path. Later root-WSL and nested-namespace DNS/HTTPS diagnostics succeeded with matching Windows egress. This intermittent failure remains unresolved; do not weaken preflight or claim stable compatibility for every VPN client.

`test` now executes WSL tunnel/route/firewall diagnostics inside `isolated-openvpn-gateway`, not the root/shared WSL namespace. `compare-transports` repeats outer preflight after each backend teardown and before its final connection, without asking for credentials again inside the controlled comparison; an error triggers cleanup.

Outer preflight also runs after interactive credential input, before the first OpenVPN start, for both backends and for `start`, `restart` and comparison. This closes the unbounded delay between the original preflight and a user's response. Regression tests verify call ordering and that a failed/cancelled recheck prevents startup and cleans temporary credentials. These are lifecycle mocks, not a real test that disconnects the user's outer VPN. An already-running CLI process keeps its loaded code; install an update only after that lifecycle command exits, then use a fresh invocation.

The comparison coordinator also checks the final validation result before saving its transport choice, requires all named positive/negative controls rather than accepting empty evidence, and compares host state across the entire comparison. Stage checkpoints and allowlisted failure categories are saved privately even when a later preflight, cancellation or cleanup fails; exception text and command arguments are not copied into these error records. New lifecycle mocks cover these failure paths and Docker/macOS independence. They are regression tests, not new corporate network acceptance; no cause is inferred for an earlier failure whose report did not retain it.

A subsequent interactive start passed again. The user-supplied private task-tracker URL returned HTTPS 302 through SOCKS with TLS certificate validation enabled; host preservation and gateway validation still passed. A direct SOCKS domain-name request received the corporate Git server's SSH banner. Fixing the stdio bridge to use `read1` for short packets removed the Git timeout. Initial SSH/HTTPS attempts required authentication; no keys were added and TLS/host-key checks were not bypassed. The separate Chrome profile was launched, but automatic UI inspection stopped because computer-use could not confidently identify the current URL.

The user subsequently authenticated HTTPS Git interactively. The helper's read-only precheck passed before a full clone into the requested NTFS directory. The exact-URL `socks5h` setting was verified locally, and the live Git HTTPS process had one established socket to `127.0.0.1:1080` with no direct established sockets at the observation time. The clone then completed: HEAD resolved, the tracked worktree was clean, and the remote matched the requested repository. A second read-only query passed using the repository-local proxy, with interactive prompts disabled and no process-level proxy override. No push was performed. The user explicitly confirmed that the supplied task-tracker page opens in the dedicated Chrome profile, and later confirmed that it did not open with the gateway off after being asked for a cache-bypassing reload. These are manual positive/negative browser checks, not automated visual results.

The non-disruptive listener acceptance also passed on the live corporate connection: the IPv4 and IPv6 positive controls worked, SOCKS accepted IPv4 loopback, and connections failed on three preferred non-loopback IPv4 addresses, two preferred non-loopback IPv6 addresses and `::1`. The temporary controls were removed without stopping OpenVPN. This is a real local-interface test, not a remote LAN-device test.

The Windows stdio forwarder starts each WSL bridge with `CREATE_NO_WINDOW`. This prevents a new terminal window for every SOCKS connection when the forwarder itself is detached. The change affects only process presentation; binary pipes and fail-closed behavior are unchanged. Updating an already running forwarder requires restarting that forwarder, which disconnects active proxy clients: finish Git transfers before applying the restart. A real Windows regression test checks that the bridge child has no console and still transfers binary bytes.

PowerShell JSON diagnostics explicitly write and decode UTF-8, including when launched without a console. A live-transfer check exposed legacy ANSI/UTF-8 mojibake in stored interface aliases: only display names differed; DNS server addresses, route destinations, gateways, metrics and interface indices matched, as did public egress and the outer adapter. The alias corruption was exactly reversible as CP1251 bytes decoded as UTF-8. Old baselines are not rewritten or silently accepted: an old-baseline comparison can still fail until the next controlled preflight captures a new baseline. A real Windows test checks identical Cyrillic/Japanese JSON with and without a console.

Earlier Python-generated host-preservation evidence covered IPv4 only. CLI snapshots now separately collect IPv6 DNS, non-local routes and default routes, and Windows validation requires their successful comparison. Missing legacy IPv6 data, failed cmdlets or malformed JSON cannot pass. The standalone PowerShell acceptance script already fingerprints both address families. Real Windows IPv6 collection passed and the selected IPv6 route used the configured outer interface; public IPv6 egress was not tested. After the clone completed, paired IPv4/IPv6 DNS/routes passed across a controlled stop, fresh outer preflight and reconnect, including a successful private HTTPS response. The old baseline was backed up, not rewritten to manufacture a pass. None of these diagnostics changes network settings.

The console-free forwarder update was deployed after the clone finished, and its SOCKS handshake passed. The installed `windows_acceptance.ps1 -ListenerOnly` rerun initially stopped under Windows PowerShell 5.1 `LocalMachine: Restricted`. After separate owner approval and a new interactive VPN start, the actual installed script passed in a child process using `RemoteSigned`. The policy snapshot was saved beforehand; all scopes matched afterward, including persistent `Restricted` in Windows PowerShell 5.1. The live result confirmed positive IPv4/IPv6 controls, only IPv4 loopback access, blocked `::1`, three non-loopback IPv4 and two non-loopback IPv6 addresses, and a still-ready gateway. No remote LAN device was tested. PowerShell 7 has a separate policy view; do not use its effective policy as evidence about Windows PowerShell 5.1.

A separate launch-context bug was reproduced: Windows PowerShell 5.1 invoked through Python inherited PowerShell 7 module paths and failed to autoload its standard Security module. Removing `PSModulePath` case-insensitively only from that child's environment restored loading; parent/system environment and execution policy were unchanged. The gateway now applies this to `powershell.exe` children only, preserving `pwsh`, Docker and WSL environments. Tests cover default/explicit environments, case variants and actual Windows module loading. This follows Microsoft's [documented intermediate-process behavior](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_psmodulepath?view=powershell-7.6); it does not establish the cause of WSL egress failures.

A fresh UDP1194 validation and positive/negative fail-closed test passed after the reconnect, but the next transport attempt did not complete and did not restore the original session. Its saved report contains only the successful original transport, so it cannot establish the later profiles' results or the exact subsequent error. Final state checks confirmed no running corporate OpenVPN, no credentials and no SOCKS listener. Credential-free diagnostics afterward again matched Windows, root WSL and gateway-namespace egress; Windows IPv4/IPv6 DNS/routes and global Git settings remained unchanged. The temporary diagnostic namespace was removed. This does not establish reliable repeated reconnects or justify relaxing preflight.

Further credential-free diagnostics reproduced namespace-start failures on the second and third of three cycles, before any corporate VPN. Instrumentation showed that the keeper had not yet entered the target namespace when slirp was launched by PID. The launch now uses the fixed `/run/netns/isolated-openvpn-gateway` path with `--netns-type=path`, eliminating that scheduling dependency. The installed slirp advertises this option, documented by [upstream](https://github.com/rootless-containers/slirp4netns/blob/master/slirp4netns.1.md). After deployment, all three uninstrumented create/HTTPS/teardown cycles passed, with matching Windows/root-WSL/namespace egress, successful DNS and pinned-address HTTPS controls, unchanged Windows IPv4/IPv6 DNS/routes, and no remaining namespace or credentials. This bounded infrastructure check is not a corporate TCP acceptance result or a guarantee for all outer VPN clients.

The subsequent full installed CLI comparison completed: both UDP transport keys passed every positive/negative control, UDP1194 was restored, final validation passed, and all Windows IPv4/IPv6 DNS/routes, public IPv4 egress and global Git comparisons passed against the initial baseline. The command exited and its follow-up private HTTPS test returned 302 with TLS verification enabled. This new completed run supersedes the incomplete comparison outcomes above; it does not erase them or prove universal VPN compatibility.

TCP443 was actually attempted in this completed run but did not initialize. Read-only classification within its recorded log time window found two TCP connection attempts, no established connection and a fatal exit, without an authentication-rejection or hook-error event. A separate non-disruptive test of the configured HTTP-proxy control endpoint timed out from both root WSL and the gateway namespace. In each environment, independent public HTTPS controls matched Windows egress; UDP stayed ready and host IPv4/IPv6 DNS/routes were preserved. This narrows the problem to control-endpoint reachability on the current outer path, rather than the SOCKS UID rules, but cannot distinguish remote service availability from upstream filtering. No profile, endpoint or firewall rule was changed. TCP positive/negative tunnel acceptance remains unproven.

A Docker CLI (29.7.2, context `desktop-linux`) is present, but the Docker Linux engine is not running. Actual Docker-backend, remote LAN-device and full uninstall acceptance remain incomplete. Existing macOS behavior is tested with mocks on Windows, not an actual macOS machine. The automated suite was rerun: 144 tests, 142 pass and 2 platform-specific cases skipped on Windows. It includes actual Windows IPv4/IPv6 TCP probe controls, a console-free binary bridge check, locale-independent PowerShell JSON output, Windows PowerShell module loading and IPv6 network queries; these Windows-only cases are skipped on macOS/Linux. Automated tests cannot substitute for missing network checks. Diagnostic JSON and private deployment inputs remain outside Git.

The WSL comparison's `events` field previously read the Docker-side Windows log path and could be empty despite existing Linux events. It now reads the managed WSL sanitized log, like `logs`, with a maximum 200-line tail and explicit availability/scope metadata. The real read-only reader check found initialization and TCP-attempt history without printing event text or accessing credentials; no VPN restart was needed. WSL history spans starts, so `event_log.scope=backend_history` is not evidence that every event belongs to that row. A read failure stays explicit and never falls back to another backend or substitutes for network acceptance. Nine additional regression tests cover source selection, unavailable/empty logs, bounded tails, timeout-detail suppression and Docker/macOS independence. Existing comparison artifacts are not rewritten into claimed new test results.

### Docker Desktop startup blocker on the test machine

Docker Desktop 4.88.1.237512 was launched once using `docker desktop start --detach`. Its backend failed before a Linux engine became available: initializing the Ingest listener could not remove `%LOCALAPPDATA%\Docker\run\sailor-ingest.sock`, with Windows error 1920 (the file cannot be accessed by the system). Directory-entry metadata identifies a zero-byte AF_UNIX reparse point, tag `0x80000023`, predating this launch. The AF_UNIX driver reports running; this alone does not explain why the object cannot be opened. Do not infer a licensing failure, WSL gateway failure, damaged Docker volumes or a need for Windows Pro from this error.

After all Docker processes exited, the owner approved renaming only this socket to a same-directory backup, with no overwrite. Both PowerShell `Rename-Item` and native `MoveFileExW` with flags zero returned 1920 inside the agent session. A metadata-only `CreateFileW` probe using `OPEN_EXISTING` and `FILE_FLAG_OPEN_REPARSE_POINT` also failed. At that stage the original remained, no backup was created, and Docker was not relaunched after the failed rename. Nothing was deleted; no ACL, driver, service, registry, firewall or WSL configuration was changed. No delayed-on-reboot operation was scheduled. A factory reset, reparse-tag removal or replacement of Docker directories is outside this approved repair and must not be automated as a fallback.

The existing UDP WSL gateway remained ready, with only `127.0.0.1` listening on the configured SOCKS port. Post-attempt checks matched the pre-Docker baseline for Windows IPv4/IPv6 DNS/routes, ordinary public IPv4 egress and global Git proxy settings. The Docker backend remains blocked until Desktop can start; successful WSL tests do not establish Docker acceptance. Keep raw Desktop logs private and do not upload a diagnostic bundle automatically.

The owner subsequently completed the same rename in ordinary PowerShell outside the agent session. The backup `sailor-ingest.sock.before-gateway-repair-20260907` was verified as the preserved zero-byte AF_UNIX object; one authorized Desktop retry created a new `sailor-ingest.sock` but failed while accessing the existing `run\dockerInference` object. That second object was not modified. Read-only package-identity queries found no package identity on the sampled Docker/diagnostic processes, so package identity or MSIX virtualization must not be presented as a proven cause. No reset, deletion or further socket rename was performed. Do not overwrite the new socket with the backup while Docker is running; keep the backup until a separately planned repair establishes whether rollback is needed.

The owner explicitly chose to defer Docker acceptance and use WSL only. Later inspection found no Docker processes, a ready UDP1194 WSL session and the already saved `wsl` backend; no implicit backend switch was made. This choice does not uninstall Docker or certify its backend. WSL operation does not require repairing Docker Desktop.

The owner's ordinary PowerShell did not recognize the short `vpn-gateway` name because its installed `bin` was absent from User/Machine/Process PATH. The actual full-path `.cmd status` invocation succeeded and confirmed the ready WSL session. The installer now emits copyable `&` invocations and session-only PATH setup using the returned physical root. Four additional tests cover both backends, unusual path characters and a real PowerShell run of a synthetic launcher through both the full path and the short command name; the test verifies persistent User/Machine PATH remains unchanged. This launcher test does not start a real backend.

A later cleanup audit found that Linux PID files were trusted without process ownership checks. Cleanup now validates the command, root UID and namespace, then rechecks after opening a pidfd; TERM and timeout KILL target that same descriptor. Normal stop removes auth even if process cleanup raises. The latest Windows suite has 153 tests: 150 passed and 3 skipped. All 9 process-cleanup tests also passed in the managed Linux distro, including a real positive stop of a disposable matching child and a negative stale-PID case that left an unrelated child alive. The live namespace keeper and slirp process passed read-only identity checks; the real VPN stayed ready and Windows IPv4/IPv6 DNS/routes, public IPv4 egress and global Git settings were preserved. This disposable-process test does not establish full uninstall acceptance or repeat the earlier VPN-stop network test.

The bounded file operations follow Microsoft's documented [reparse-point open semantics](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew) and [MoveFileExW flags](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexw); these references do not establish the underlying cause of this machine's error.
