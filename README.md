# Isolated OpenVPN Gateway

Isolated OpenVPN Gateway is a local OpenVPN-to-SOCKS5 gateway for selected macOS and Windows applications. Corporate OpenVPN runs only inside an isolated Linux environment; it is never installed or started on the Windows host.

Windows has two independent backends:

- `docker`: Docker Desktop, Linux containers and the `desktop-linux` context;
- `wsl`: a project-owned WSL2 Debian distro named `IsolatedOpenVPNGateway`, without Docker Desktop.

The existing command remains compatible and uses the backend saved at installation.
The Windows wizard recommends WSL for a new installation; legacy installations
and the low-level installer keep their existing Docker default:

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

## Start here — source install for developers

On Windows, install Python 3.11+ and the chosen backend prerequisites, clone this
repository, and run:

```powershell
.\setup.cmd
```

The terminal wizard imports one compatible IT-issued `.ovpn` or a prepared TOML
kit, confirms the outer VPN adapter, provisions the dedicated backend, and offers
user-PATH registration, a browser choice and repository-local Git setup.
No Docker is needed for WSL. No full Hyper-V role or Windows Pro upgrade is needed.

Open a new terminal after opting into PATH. Daily commands:

```text
vpn-gateway start --open-browser
vpn-gateway status
vpn-gateway doctor
vpn-gateway git check PATH_TO_REPOSITORY --remote
vpn-gateway stop
```

[Русский quickstart](QUICKSTART.md) · [Windows diagnostics](WINDOWS.md) ·
[Rollback/update](ROLLBACK.md) · [Safe handoff](HANDOFF.md)

`start` without `--open-browser` stays terminal-only. Repeated setup preserves
the selected backend and private deployment. To update the installed CLI from a
new verified checkout, use `.\setup.cmd --update`; no automatic update/download
of application code is performed. Firefox is explicit opt-in and experimental
until its real network acceptance is complete; Chrome remains supported.

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
- Git only for cloning the source or using Git integration; browser-only operation does not require Git;
- x86_64/AMD64 or ARM64 Windows, or arm64/x86_64 macOS;
- Chrome/Chromium/Edge, or explicitly selected Firefox 128+ (use a maintained release), only for browser integration;
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

## Advanced IT-kit installation (legacy interface)

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
```

Copy the two PowerShell PATH setup lines printed by the installer, which use the actual `Installed:` root. They apply only to that terminal window. Then run:

```powershell
vpn-gateway install --backend wsl
vpn-gateway start --backend wsl
```

The first WSL command creates only `IsolatedOpenVPNGateway`. It installs OpenVPN, Dante, iproute2, iptables, `slirp4netns`, curl and Python inside that distro. It does not install into the default distro and does not change the default distro. Debian 13 does not ship Dante: the installer then builds official Dante 1.4.4 after verifying its pinned upstream SHA-256; it does not mix Debian releases. Network access to Debian and `www.inet.no` is needed for this fallback.

Installation paths:

| Host | Gateway | Commands | Browser profile |
|---|---|---|---|
| macOS | `~/.local/share/isolated-openvpn-gateway` | `~/.local/bin/vpn-gateway`, `vpn-browser` | `~/.local/share/isolated-openvpn-browser` |
| Windows | `%LOCALAPPDATA%\IsolatedOpenVPNGateway` | `...\bin\vpn-gateway.cmd`, `vpn-browser.cmd` | `%LOCALAPPDATA%\IsolatedOpenVPNBrowser` |

The low-level legacy installer does not register PATH. The new setup wizard offers opt-in persistent **User** PATH and records the physical install location under HKCU; Machine PATH and PowerShell profiles remain untouched. If PowerShell reports `vpn-gateway` is not recognized, repeat the installer's two session-only PATH setup lines in that window, or invoke the full launcher path with `& 'ACTUAL-INSTALLED-ROOT\bin\vpn-gateway.cmd' status`. Quoting a path alone does not execute it in PowerShell. Packaged-terminal installations may have a physical root under `Packages\...\LocalCache`; use the returned path instead of reconstructing it from `%LOCALAPPDATA%`. `--profiles-dir` resolves `profile_file`; repeated `--profile NAME=PATH` supports separate private directories.

## Commands and backend choice

```text
vpn-gateway install --backend wsl
vpn-gateway install --backend docker
vpn-gateway backend set docker|wsl
vpn-gateway start [TRANSPORT] [--backend docker|wsl]
vpn-gateway stop
vpn-gateway restart [TRANSPORT]
vpn-gateway setup
vpn-gateway status [--json] [--verbose]
vpn-gateway doctor [--json]
vpn-gateway browser [URL] [--browser firefox|chromium]
vpn-gateway git configure PATH [--remote-name NAME]
vpn-gateway git check PATH [--remote] [--remote-name NAME]
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

Comparison events now come from the selected backend, with a maximum 200-line sanitized tail. `event_log.available` distinguishes an inaccessible log from an empty one. WSL `event_log.scope=backend_history` means the tail spans multiple starts; it must not be attributed entirely to one transport or used instead of its positive/negative controls. Docker remains WSL-independent. The event text can still contain private network diagnostics and is not for public handoff.

`start`, `restart` and `compare-transports` check the outer path before requesting credentials and again after interactive input, immediately before the first transport starts. The pre-prompt result cannot authorize a connection after an arbitrarily long wait. A failed or cancelled recheck prevents OpenVPN startup and triggers credential cleanup; it never weakens the egress or expected-adapter checks.

## Corporate DNS, Git and browser

Pushed `dhcp-option DNS`, `DOMAIN` and `DOMAIN-SEARCH` are applied only to the Docker namespace or the nested managed WSL namespace. WSL uses a namespace-specific resolver bind mount for both systemd and fallback processes. Disconnect restores the namespace's outer resolver; the distro's WSL-generated resolver stays intact for the outer `slirp4netns` process and DNS tunneling. Windows DNS, outer-VPN DNS, global NRPT and other WSL distros are untouched.

Clients resolve target hostnames proxy-side:

- HTTPS Git: repository-local `http.<exact-url>.proxy=socks5h://127.0.0.1:PORT`;
- SSH Git: repository-local `core.sshCommand` and a hostname-preserving SOCKS stdio bridge;
- Chromium/Edge: separate profile, SOCKS5, proxy-side DNS rules, QUIC/direct WebRTC disabled, no `direct://` fallback;
- Firefox: explicit choice, separate owned profile, manual SOCKS5 with remote DNS, direct bypass/DoH/HTTP3/WebRTC disabled in that profile. No global policies or main-profile changes.

No global Git proxy, global SSH `ProxyCommand`, remote URL or repository content is changed. Repositories remain on NTFS. Test with a read-only `git -C PATH ls-remote REMOTE`; never push merely for acceptance.

Chrome's warning about `--host-resolver-rules` is intentional upstream behavior;
the flag is retained to block local DNS. It is not hidden with testing flags.
See [Chromium's warning list](https://github.com/chromium/chromium/blob/main/chrome/browser/ui/startup/bad_flags_prompt.cc).
Firefox avoids that particular Chrome flag, but its settings are not by themselves
proof of isolation. Existing Chrome profiles are not migrated or deleted.
Mozilla defines SOCKS5 remote DNS separately in its
[network preferences](https://github.com/mozilla/gecko-dev/blob/master/modules/libpref/init/StaticPrefList.yaml).
Our browser acceptance procedure checks hostname transmission and requires DNS
observation; writing that preference alone is not treated as a passing network test.

`git configure` previews a local change, requires confirmation for writes, and
returns “already configured” for matching effective settings. It refuses conflicting
environment and `remote.proxy` overrides. `git check` is local-only unless `--remote` is requested;
authentication failures are distinct from successful repository access. Neither
command clones or pushes.

`status` and `doctor` provide JSON with `schema_version: 1`, a state, categorized
checks and the next action. States are `not_configured`, `stopped`, `connecting`,
`ready`, `blocked`, `error`. A ready tunnel does not prove a private application
works. Doctor performs read-only inspection and never calls disruptive preflight,
transport comparison, repairs, or session teardown. Skipped checks say `not_checked`.

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

## Verification status

Prior Windows Home/WSL evidence and limitations are preserved in [HISTORY.md](HISTORY.md).
The new setup/update/Firefox layer is not covered by that historical acceptance.
See [UX_ACCEPTANCE.md](UX_ACCEPTANCE.md) for the new, explicitly separated release gates.
