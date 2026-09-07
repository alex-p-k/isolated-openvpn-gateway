# Isolated OpenVPN Gateway

Isolated OpenVPN Gateway is a local OpenVPN-to-SOCKS5 gateway for selected macOS and Windows applications. Corporate OpenVPN runs only inside an isolated Linux environment; it is never installed or started on the Windows host.

Windows has two independent backends:

- `docker`: Docker Desktop, Linux containers and the `desktop-linux` context;
- `wsl`: a project-owned WSL2 Debian distro named `IsolatedOpenVPNGateway`, without Docker Desktop.

The existing command remains compatible and uses the backend saved at installation (default: `docker`):

```text
vpn-gateway start
vpn-gateway start --backend docker
vpn-gateway start --backend wsl
```

```text
selected Windows Git/browser process
  -> socks5h://127.0.0.1:1080
  -> Docker loopback publication OR Windows loopback-only WSL stdio forwarder
  -> Dante bound outbound to tun0
  -> corporate network

all ordinary host traffic
  -> unchanged Windows/macOS routes and DNS
  -> optional outer VPN
```

It is not a VPN server, a system-wide VPN, a public proxy, or a tool for installing corporate OpenVPN on Windows.

## Windows support matrix

| Backend | Windows Home | Windows Pro | Docker required |
|---|---:|---:|---:|
| docker-wsl2 | yes | yes | yes |
| native-wsl2 | yes | yes | no |
| Hyper-V VM | no | yes | no |

`Hyper-V VM` is an optional manual fallback and is not implemented by this CLI. The supported CLI backends are `docker` and `wsl`. Neither requires Hyper-V Manager or Windows containers. WSL2 still runs a real Linux kernel in a lightweight utility VM, even when Docker Desktop is absent.

Microsoft states that WSL2 is available on Windows 10/11 Home through the `Virtual Machine Platform` and `Windows Subsystem for Linux` optional components. Docker documents the WSL2 backend and notes that Home can run Linux containers, while Windows containers require Pro/Enterprise. Official references are at the end of this file.

## Security properties

- Docker uses Linux containers, `/dev/net/tun`, `NET_ADMIN`, no `privileged: true`, and publishes only `127.0.0.1:PORT`.
- Native WSL uses only the dedicated project-created distro. It never provisions the user's Ubuntu/Debian/default distro.
- WSL creates a nested Linux network namespace inside the managed distro. OpenVPN, Dante, `tun0`, routes and firewall live there; `slirp4netns` targets the fixed named-namespace path, not a still-starting keeper PID, and provides only outer control egress without adding routes/firewall rules to WSL's shared root network namespace.
- WSL Dante listens on `127.0.0.1:11080` inside the nested namespace. A credential-free Windows process binds `127.0.0.1:PORT` and relays each connection through `wsl.exe` stdio; it has no direct network fallback.
- Dante selects `tun0` as its outbound interface.
- A second independent layer routes UID `10000` only through table 100 and rejects that UID in a dedicated `iptables`/`ip6tables` chain when `tun0` is absent. The rules are named, deterministic, idempotent and confined to the nested namespace, so the shared root namespace and other WSL distros are not filtered.
- OpenVPN resolves and pins its public control endpoint before corporate DNS is applied. Its root control process retains the normal WSL route; the SOCKS UID does not.
- Host DNS, routes, Windows Firewall and global Git configuration are read for validation but never changed.
- The tool never edits `%USERPROFILE%\.wslconfig`, changes NAT/mirrored mode, runs `netsh portproxy`, creates Windows Firewall rules, or changes Windows NRPT.
- Credentials use hidden terminal input. They are never arguments or environment variables. Docker uses the protected host runtime file; WSL transfers them over stdin into a mode-`0600` runtime file. Stop, failed start and the next start remove stale credentials.
- Private deployment files receive mode `0600` on macOS. Windows files/directories receive a protected current-SID NTFS ACL with inheritance removed.
- Original `.ovpn` files are read-only installer inputs. The derived runtime profile is allowlisted and endpoint-checked; profile content, inline keys and credentials are never printed.

## Requirements

Common:

- Python 3.11 or newer;
- Git;
- x86_64/AMD64 or ARM64 Windows, or arm64/x86_64 macOS;
- Chrome, Chromium or Edge only if the isolated browser is wanted;
- an already connected outer VPN when `require_outer_vpn=true`.

Docker backend:

- Docker Desktop using Linux containers;
- `desktop-linux` context and Compose;
- WSL2 backend is the normal Windows Home path; full Hyper-V Manager is not required.

WSL backend:

- Windows 10/11 with WSL 2.4.4 or newer;
- WSL2 and hardware virtualization enabled;
- network access during provisioning for Debian `apt` packages;
- no Docker Desktop.

Startup always checks `/dev/net/tun`, the selected outer-VPN route and equal host/backend public egress before requesting corporate credentials. A mismatch is a blocker, not a warning.

## Private deployment configuration

Keep `gateway.toml` and IT-issued `.ovpn` files outside the source package. Never commit them.

```toml
[gateway]
id = "example-company"
display_name = "Example Company VPN"
default_transport = "udp"
dns_canary = "internal.example.com"
socks_port = 1080
require_outer_vpn = true
outer_interface_prefix = "utun"
windows_outer_adapter_contains = ["Distinctive outer VPN adapter name"]

[transports.udp]
profile_file = "company-udp.ovpn"
remote_host = "vpn.example.com"
remote_port = 1194
remote_protocol = "udp4"
required_inline_blocks = ["ca", "tls-auth"]
```

Do not guess corporate hostnames or DNS addresses. `dns_canary` is a harmless private name supplied by the deployment owner. If OpenVPN pushes no DNS, status reports that fact and does not invent an address.

## Verify and install

macOS (Docker backend):

```bash
shasum -a 256 -c isolated-openvpn-gateway-YYYY-MM-DD.zip.sha256
python3 install.py --check --backend docker --config "/private/gateway.toml" --profiles-dir "/private/profiles"
python3 install.py         --backend docker --config "/private/gateway.toml" --profiles-dir "/private/profiles"
```

Windows PowerShell, Docker backend:

```powershell
Get-FileHash .\isolated-openvpn-gateway-YYYY-MM-DD.zip -Algorithm SHA256
py -3.11 .\install.py --check --backend docker --config "C:\Secure\gateway.toml" --profiles-dir "C:\Secure\profiles"
py -3.11 .\install.py         --backend docker --config "C:\Secure\gateway.toml" --profiles-dir "C:\Secure\profiles"
```

Windows PowerShell, Docker-free WSL backend:

```powershell
py -3.11 .\install.py --check --backend wsl --config "C:\Secure\gateway.toml" --profiles-dir "C:\Secure\profiles"
py -3.11 .\install.py         --backend wsl --config "C:\Secure\gateway.toml" --profiles-dir "C:\Secure\profiles"
$env:Path = "$env:LOCALAPPDATA\IsolatedOpenVPNGateway\bin;$env:Path"
vpn-gateway install --backend wsl
vpn-gateway start --backend wsl
```

The first WSL command creates only `IsolatedOpenVPNGateway`. It installs OpenVPN, Dante, iproute2, iptables, `slirp4netns`, curl and Python inside that distro. It does not install into the default distro and does not change the default distro. Debian 13 does not ship Dante: the installer then builds official Dante 1.4.4 after verifying its pinned upstream SHA-256; it does not mix Debian releases. Network access to Debian and `www.inet.no` is needed for this fallback.

Installation paths:

| Host | Gateway | Commands | Browser profile |
|---|---|---|---|
| macOS | `~/.local/share/isolated-openvpn-gateway` | `~/.local/bin/vpn-gateway`, `vpn-browser` | `~/.local/share/isolated-openvpn-browser` |
| Windows | `%LOCALAPPDATA%\IsolatedOpenVPNGateway` | `...\bin\vpn-gateway.cmd`, `vpn-browser.cmd` | `%LOCALAPPDATA%\IsolatedOpenVPNBrowser` |

No command is added to a global PATH. `--profiles-dir` resolves `profile_file`; repeated `--profile NAME=PATH` supports separate private directories.

## Commands and backend choice

```text
vpn-gateway install --backend wsl
vpn-gateway install --backend docker
vpn-gateway backend set docker|wsl
vpn-gateway start [TRANSPORT] [--backend docker|wsl]
vpn-gateway stop
vpn-gateway restart [TRANSPORT]
vpn-gateway status
vpn-gateway logs
vpn-gateway test [https://private-host/]
vpn-gateway compare-transports [--backend docker|wsl]
vpn-gateway git-configure PATH_TO_REPOSITORY
vpn-gateway build
vpn-gateway uninstall --backend wsl
vpn-gateway uninstall
vpn-browser [https://private-host/]
```

`backend set` changes only the saved future choice and never switches a running session. An explicit `--backend` overrides the saved choice for that command. `stop`, `status`, `logs` and `test` use the recorded active backend.

`start` succeeds only after OpenVPN reports `Initialization Sequence Completed`, the route/DNS hook succeeds, `tun0` exists and SOCKS5 handshake succeeds. `compare-transports` requires a successful SOCKS request before stopping OpenVPN and checking that proxy access fails. WSL additionally requires an outer-path positive control and an observed firewall REJECT-counter increase independently of the unreachable route. It attempts WSL session restoration even when a diagnostic raises an exception, then selects a transport only after all checks pass.

Transport comparison requires every named negative-test control to be explicitly true; empty or partial evidence cannot pass. Before saving the transport preference, the final connection must pass validation and host state must match the initial comparison baseline, not just the latest per-transport preflight. Failures trigger cleanup. Private `validation/comparison.json` checkpoints retain the failing stage and an allowlisted error category without exception text or subprocess arguments; `transports.json` retains completed and incomplete transport rows. A cleanup attempt is not proof of successful cleanup: verify current status separately.

`start`, `restart` and `compare-transports` check the outer path before requesting credentials and again after interactive input, immediately before the first transport starts. The pre-prompt result cannot authorize a connection after an arbitrarily long wait. A failed or cancelled recheck prevents OpenVPN startup and triggers credential cleanup; it never weakens the egress or expected-adapter checks.

## Corporate DNS, Git and browser

Pushed `dhcp-option DNS`, `DOMAIN` and `DOMAIN-SEARCH` are applied only to the Docker namespace or the nested managed WSL namespace. WSL uses a namespace-specific resolver bind mount for both systemd and fallback processes. Disconnect restores the namespace's outer resolver; the distro's WSL-generated resolver stays intact for the outer `slirp4netns` process and DNS tunneling. Windows DNS, outer-VPN DNS, global NRPT and other WSL distros are untouched.

Clients resolve target hostnames proxy-side:

- HTTPS Git: repository-local `http.<exact-url>.proxy=socks5h://127.0.0.1:PORT`;
- SSH Git: repository-local `core.sshCommand` and a hostname-preserving SOCKS stdio bridge;
- Chromium/Edge: separate profile, SOCKS5, proxy-side DNS rules, QUIC/direct WebRTC disabled, no `direct://` fallback;
- Firefox is not launched automatically; if configured manually, enable proxy DNS over SOCKS5 and use a separate profile.

No global Git proxy, global SSH `ProxyCommand`, remote URL or repository content is changed. Repositories remain on NTFS. Test with a read-only `git -C PATH ls-remote REMOTE`; never push merely for acceptance.

## Validation and rollback

Run `vpn-gateway test` after connecting. On Windows, run `tools\windows_acceptance.ps1` on the target Home machine for real positive/negative and host-preservation evidence. Unit tests and mocks do not prove real VPN behavior.

`tools\windows_acceptance.ps1 -ListenerOnly` checks the running Windows listener without stopping the gateway. It requires working IPv4/IPv6 loopback positive controls and blocked connections on non-loopback local addresses. This limited check does not replace corporate payload, tunnel-loss, or remote LAN-device tests.

`vpn-gateway uninstall --backend wsl` unregisters only a distro whose Windows and Linux ownership markers both match this installation. Full uninstall removes only project-owned containers/image tags, managed WSL distro, launchers, copied private files, runtime state and matching repository-local Git changes. Docker Desktop, WSL itself, other distros, outer VPN software, repositories, host routes/DNS/Firewall and global Git settings remain.

See `WINDOWS.md` for acceptance and NAT/mirrored handling, `ROLLBACK.md` for exact recovery boundaries, `QUICKSTART.md` for short commands and `HANDOFF.md` for safe transfer.

## Official platform references

- Microsoft: [Install WSL](https://learn.microsoft.com/windows/wsl/install)
- Microsoft: [WSL basic commands, import/export/unregister](https://learn.microsoft.com/windows/wsl/basic-commands)
- Microsoft: [WSL networking, NAT and mirrored mode](https://learn.microsoft.com/windows/wsl/networking)
- Microsoft: [systemd on WSL](https://learn.microsoft.com/windows/wsl/systemd)
- Microsoft: [WSL FAQ — Home support and Virtual Machine Platform](https://learn.microsoft.com/windows/wsl/faq)
- Microsoft: [What is WSL — shared and isolated namespaces](https://learn.microsoft.com/windows/wsl/about)
- Docker: [Install Docker Desktop on Windows](https://docs.docker.com/desktop/setup/install/windows-install/)
- Docker: [Docker Desktop WSL2 backend](https://docs.docker.com/desktop/features/wsl/)

## Verification status of this revision

The development host is Windows 11 Home Single Language, 25H2, build `26200.9168`, AMD64. On 2026-09-07, WSL 2.7.12, dedicated Debian WSL2, systemd, `/dev/net/tun`, OpenVPN and Dante 1.4.4 were installed/checked without Docker. NAT egress timed out; after separate owner approval and a recorded absent-file backup, mirrored mode passed Windows/root-WSL/nested-namespace egress equality and outer-adapter route checks.

Actual corporate OpenVPN initialization and pushed-DNS payloads through the Windows SOCKS forwarder passed on two supplied UDP profiles. For each, stopping only OpenVPN removed tun0 and blocked SOCKS; a working root outer HTTPS control, blocked proxy UID and increased firewall REJECT counter independently proved enforcement. The tunnel and successful SOCKS payload were then restored. Windows routes/DNS, ordinary IPv4 egress and global Git proxy stayed unchanged. The listener was IPv4 loopback-only. Validation enters the nested gateway namespace for tunnel/route/firewall checks. After the clone completed, paired IPv4/IPv6 DNS and route snapshots passed across a controlled stop, fresh outer preflight and successful reconnect, including a private HTTPS payload. This does not establish public IPv6 egress equality.

Repeated WSL outer egress timeouts blocked the TCP transport check and restoration after an earlier comparison; safe cleanup removed credentials and the listener. Subsequent unauthenticated outer diagnostics passed again. The intermittent failure remains a limitation, not evidence of an outer-VPN bypass. After another interactive start, the supplied private task-tracker URL returned HTTPS 302 through SOCKS with TLS verification enabled. The SSH bridge's short-packet buffering bug was fixed. After interactive HTTPS Git authentication, the full NTFS clone completed: HEAD resolved, the tracked worktree was clean, and a second read-only remote query passed using only the repository-local `socks5h` setting, without a process-level proxy override. No push was performed. The user confirmed that the tracker opens in the dedicated Chrome profile; this is manual positive evidence, not automated visual inspection.

The console-free WSL forwarder update was applied after the clone finished. A fresh UDP1194 positive/negative test then passed, but a later multi-transport attempt did not restore the session; final checks confirmed OpenVPN stopped, credentials removed and no SOCKS listener. Credential-free Windows/root-WSL/nested-namespace egress checks subsequently matched again, with host IPv4/IPv6 DNS/routes preserved. Do not infer current connectivity from an earlier successful test. The owner subsequently confirmed that the dedicated browser cannot open the tracker with the gateway off, completing the manual browser positive/negative check. After a new interactive connection and separate approval, the installed PowerShell listener script passed with process-only `RemoteSigned`; persistent policy was unchanged. The live check confirmed IPv4/IPv6 probe controls, IPv4 loopback access, and blocked `::1`/non-loopback local interfaces. TCP transport, remote LAN-device and full uninstall acceptance remain incomplete. A Docker CLI is present, but its Linux engine is stopped; no real Docker-backend acceptance was performed. macOS compatibility is covered by tests/mocks, not a real macOS run. See `WINDOWS.md` for the evidence boundaries.
