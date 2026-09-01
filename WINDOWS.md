# Windows installation and acceptance

The Windows host layer runs from normal 64-bit PowerShell. Docker Desktop still runs the actual OpenVPN and SOCKS processes in its Linux VM, so the security architecture is the same as on macOS.

## 1. Prerequisites

Install and start:

- Docker Desktop in Linux-container mode, preferably with the WSL 2 backend. Docker Desktop for Windows on Arm is currently an Early Access dependency;
- Python 3.11 or newer from python.org;
- Git for Windows;
- Chrome, Chromium or Microsoft Edge if the isolated browser is needed.

Run the gateway installer as the normal desktop user. Do not use **Run as administrator**. Enabling WSL 2 or installing Docker Desktop may require a separate one-time administrative action; the gateway itself neither enables Windows features nor changes Docker settings.

Check the environment:

```powershell
docker --context desktop-linux info
docker --context desktop-linux compose version
py -3.11 --version
git --version
```

If Docker shows Windows containers, switch Docker Desktop to Linux containers before continuing.

## 2. Identify the outer VPN adapter

Connect Windscribe, AmneziaVPN or the required outer VPN, then run this read-only command:

```powershell
Get-NetAdapter | Where-Object Status -eq Up |
  Format-Table Name, InterfaceDescription, ifIndex, Status -AutoSize
```

Put a distinctive substring of its `Name` or `InterfaceDescription` in the private `gateway.toml`:

```toml
require_outer_vpn = true
windows_outer_adapter_contains = ["Windscribe"]
```

Use at least three characters and avoid broad values such as `VPN` when several VPN adapters exist. Startup uses `Find-NetRoute` to find the selected public route and requires its interface index to belong to a connected matching adapter. It then requires the host and a minimal Docker container to have the same public IP.

Some VPN clients do not expose a visible routed adapter, or use split tunnelling that keeps the public route on Wi-Fi/Ethernet. In that case startup intentionally fails closed. Do not set `require_outer_vpn=false` merely to bypass this check; first confirm the intended outer path.

## 3. Verify and install

Keep the release, private TOML and `.ovpn` profiles separate. From the extracted release directory:

```powershell
Get-FileHash .\isolated-openvpn-gateway-YYYY-MM-DD.zip -Algorithm SHA256

py -3.11 .\install.py --check `
  --config "C:\Secure\gateway.toml" `
  --profiles-dir "C:\Secure\profiles"

py -3.11 .\install.py `
  --config "C:\Secure\gateway.toml" `
  --profiles-dir "C:\Secure\profiles"
```

The external checksum must equal the value in the accompanying `.sha256` file. The installer also verifies the internal allowlist manifest, validates profile structure and exact endpoints, and copies private data to:

```text
%LOCALAPPDATA%\IsolatedOpenVPNGateway
```

The root, configuration, profiles, runtime state and generated launchers receive an NTFS DACL that removes inherited entries and grants full control only to the current numeric user SID. The installer aborts and removes the new root if applying any ACL fails.

The installer does not modify machine or user PATH. For the current PowerShell window only:

```powershell
$env:Path = "$env:LOCALAPPDATA\IsolatedOpenVPNGateway\bin;$env:Path"
```

Alternatively, always invoke the complete path:

```powershell
& "$env:LOCALAPPDATA\IsolatedOpenVPNGateway\bin\vpn-gateway.cmd" status
```

## 4. Start and validate

With the outer VPN connected:

```powershell
vpn-gateway start
vpn-gateway status
vpn-gateway test https://REAL-PRIVATE-HOST/
```

The username and password prompts are hidden. Credentials are written only to the protected runtime auth file, remain available for OpenVPN re-authentication, and are removed by `stop`, a failed start, or the next start.

Check the host listener:

```powershell
Get-NetTCPConnection -State Listen -LocalPort 1080 |
  Select-Object LocalAddress, LocalPort, OwningProcess
```

`LocalAddress` must be `127.0.0.1`, never `0.0.0.0` or a LAN address.

The strongest routine acceptance is:

1. Record host public IP, DNS and route output with the outer VPN on.
2. Run `vpn-gateway start` and `vpn-gateway test REAL_PRIVATE_URL`.
3. Open the isolated browser and run a read-only `git ls-remote` for a configured repository.
4. Run `vpn-gateway stop`.
5. Confirm the isolated browser and configured private Git remote now fail while ordinary host networking still works.

`compare-transports` performs the positive and tunnel-failure tests for every configured transport and reconnects the best passing choice:

```powershell
vpn-gateway compare-transports
```

## 5. Isolated browser

```powershell
vpn-browser https://private-host/
```

The launcher selects Chrome, Chromium or Edge, creates `%LOCALAPPDATA%\IsolatedOpenVPNBrowser`, and never touches the main profile. Its flags send all web traffic to `socks5://127.0.0.1:1080`, force proxy-side target DNS, disable QUIC and direct WebRTC UDP, and specify no direct fallback.

## 6. Repository-only Git

Repositories stay on the Windows filesystem. Configure one selected remote interactively:

```powershell
vpn-gateway git-configure "C:\work\private-repository"
git -C "C:\work\private-repository" ls-remote origin
```

For HTTPS, the exact remote URL gets a repository-local `socks5h` proxy. For SSH, the repository gets a private `core.sshCommand`; the included Python helper performs SOCKS5 CONNECT and sends the target hostname to the proxy for private DNS resolution. Existing user and system OpenSSH settings are included for unrelated options, while the selected hostname cannot reuse a direct SSH control connection.

No global Git config, `%USERPROFILE%\.ssh\config`, repository contents or remote URL is changed. The repository config is backed up before editing.

## 7. Stop and uninstall

```powershell
vpn-gateway stop
vpn-gateway uninstall
```

Windows keeps open file locks from being deleted, so uninstall closes its lifecycle lock before removing the installation root. The isolated browser profile is removed only after a second explicit confirmation.

## What still requires a real Windows machine

The Python host layer, PowerShell command construction, NTFS ACL command construction, `.cmd` launchers, outer-route matching, OpenSSH SOCKS bridge, installer rollback and uninstall lock ordering are covered by platform-simulation tests that run on macOS. The Linux images, Compose namespace, `/dev/net/tun`, firewall and fail-closed route hooks are also tested through Docker Desktop on macOS.

The following cannot be proven on macOS and must be accepted on the target Windows installation:

- the installed VPN client's actual adapter name and route policy;
- Docker Desktop WSL 2/Hyper-V inheritance of that particular VPN path;
- `/dev/net/tun` exposure by that Docker Desktop backend and security policy;
- local enterprise endpoint protection, Group Policy and NTFS behavior;
- actual private OpenVPN authentication, corporate DNS, browser and Git reachability.

The runtime checks fail before credentials are requested when `/dev/net/tun`, host diagnostics, selected outer route, or matching host/Docker egress are unavailable.
