# Isolated OpenVPN Gateway

**Isolated OpenVPN Gateway** is a local client-side OpenVPN-to-SOCKS5 gateway for macOS and Windows. It runs the private OpenVPN client inside Docker Desktop and publishes that tunnel only as a loopback SOCKS5 proxy.

It is not a VPN server, a system-wide VPN, or a public proxy.

```text
selected host applications
  -> socks5h://127.0.0.1:1080
  -> Docker loopback publication
  -> Dante (same network namespace as OpenVPN)
  -> tun0 only
  -> private network

all other host traffic
  -> unchanged host route / optional outer VPN
```

The source is organization-neutral. Deployment endpoints, transport names, profile filenames, DNS canary and SOCKS port live in a private `gateway.toml`. OpenVPN profiles and credentials are never included in a release archive.

## Security properties

- OpenVPN runs only in a Docker Desktop Linux VM with `/dev/net/tun` and `NET_ADMIN`; `privileged: true` is not used.
- Docker publishes only `127.0.0.1:PORT`; the LAN receives no proxy listener.
- Dante binds outbound sockets to `tun0`. UID routing and firewall rules reject `eth0` fallback if the tunnel disappears.
- Host DNS, routes and global Git configuration are read for validation but are never changed.
- Credentials use hidden terminal input and a short-lived private runtime file. They are never passed in arguments or environment variables and are removed on stop, failure and the next start.
- Private deployment files use mode `0600` on macOS. Windows files and directories receive a protected NTFS ACL for the current user SID with inherited access removed.
- Original `.ovpn` files are read-only installer inputs. Profile directives are allowlisted and the exact remote and optional HTTP proxy must match `gateway.toml`.
- The installer refuses Administrator/root execution, existing install paths, symlinks, malformed manifests and incompatible profiles.

## Requirements

- macOS arm64/x86_64, or Windows 10/11 AMD64. Windows ARM64 additionally depends on Docker Desktop Arm Early Access.
- Docker Desktop running **Linux containers** with the `desktop-linux` context
- Python 3.11 or newer
- Git
- Chrome, Chromium or Microsoft Edge for the optional isolated browser

On Windows, Docker Desktop's WSL 2 backend is the expected lightweight backend. The Hyper-V Linux backend can also work, but the Linux VM must expose `/dev/net/tun`. Startup tests that device before asking for credentials.

Docker outbound traffic must have the same public egress as the host. When `require_outer_vpn=true`, startup also verifies the selected public route: `utun*` on macOS or a configured connected adapter on Windows. If Docker bypasses the outer VPN, startup stops before OpenVPN authentication.

## Deployment configuration

Copy `gateway.example.toml` outside the source package and replace the examples with values from IT-issued profiles. Keep it private. Transport names are arbitrary; `udp` and `tcp` are conventions.

```toml
[gateway]
id = "example-company"
display_name = "Example Company VPN"
default_transport = "udp"
dns_canary = "internal.example.com"
socks_port = 1080
require_outer_vpn = true
outer_interface_prefix = "utun" # macOS
windows_outer_adapter_contains = ["Distinctive VPN adapter name"] # Windows

[transports.udp]
profile_file = "company-udp.ovpn"
remote_host = "vpn.example.com"
remote_port = 1194
remote_protocol = "udp4"
required_inline_blocks = ["ca", "tls-auth"]
```

`windows_outer_adapter_contains` contains one or more distinctive, case-insensitive substrings from the Windows adapter `Name` or `InterfaceDescription`. The public route must use one of the matching interface indexes. See `WINDOWS.md` for the discovery command.

Supported IPv4 client protocols are `udp`, `udp4`, `tcp-client`, and `tcp4-client`. Add `http_proxy_host` and `http_proxy_port` only when the profile contains the matching `http-proxy` directive. Set `require_outer_vpn=false` only when nesting is intentionally unnecessary.

`dns_canary` is a harmless private hostname expected to return an A record through pushed private DNS. Do not put credentials, tokens or URLs with query strings in this file.

## Verify and install

From an extracted release on macOS:

```bash
shasum -a 256 -c isolated-openvpn-gateway-YYYY-MM-DD.zip.sha256
python3 install.py --check --config "/private/path/gateway.toml" --profiles-dir "/private/path/profiles"
python3 install.py         --config "/private/path/gateway.toml" --profiles-dir "/private/path/profiles"
```

On Windows PowerShell:

```powershell
Get-FileHash .\isolated-openvpn-gateway-YYYY-MM-DD.zip -Algorithm SHA256
py -3.11 .\install.py --check --config "C:\Secure\gateway.toml" --profiles-dir "C:\Secure\profiles"
py -3.11 .\install.py         --config "C:\Secure\gateway.toml" --profiles-dir "C:\Secure\profiles"
```

`--profiles-dir` resolves each `profile_file` from the TOML. For profiles in different directories, repeat `--profile NAME=PATH` once for every transport.

Installation creates:

| Host | Gateway | Commands | Browser profile |
|---|---|---|---|
| macOS | `~/.local/share/isolated-openvpn-gateway` | `~/.local/bin/vpn-gateway`, `vpn-browser` | `~/.local/share/isolated-openvpn-browser` |
| Windows | `%LOCALAPPDATA%\IsolatedOpenVPNGateway` | `...\bin\vpn-gateway.cmd`, `vpn-browser.cmd` | `%LOCALAPPDATA%\IsolatedOpenVPNBrowser` |

No command is added to a global PATH. Optional `--legacy-aliases` creates `corp-vpn` and `corp-browser` only when those names are unused.

## Commands

```text
vpn-gateway start                 # configured default transport
vpn-gateway start TRANSPORT
vpn-gateway stop
vpn-gateway restart [TRANSPORT]
vpn-gateway status
vpn-gateway logs
vpn-gateway test [https://private-host/]
vpn-gateway compare-transports
vpn-gateway git-configure PATH_TO_REPOSITORY
vpn-gateway build
vpn-gateway uninstall
vpn-browser [https://private-host/]
```

Use the `.cmd` path or add its directory to the current PowerShell session on Windows. `start` succeeds only after OpenVPN reports `Initialization Sequence Completed`, the route hook succeeds, `tun0` exists and a SOCKS5 handshake passes. `compare-transports` also performs a real fail-closed test and selects the first fully usable transport.

## DNS, browser and Git

Pushed `dhcp-option DNS` and search domains are applied only inside the VPN/SOCKS namespace. `socks5h`, the SSH bridge and the isolated browser send target hostnames to the proxy side. Host system DNS remains untouched. If the server pushes no DNS, status reports that fact.

`vpn-browser` creates a separate profile, disables QUIC, direct WebRTC UDP, browser DNS-over-HTTPS and local target DNS. It has no `direct://` fallback and never edits the main browser profile.

`vpn-gateway git-configure` changes only the selected repository and remote:

- HTTPS: repository-local `http.<exact-url>.proxy=socks5h://127.0.0.1:PORT`
- SSH on macOS: repository-local `core.sshCommand` plus a host-specific private config and system `nc -X 5`
- SSH on Windows: repository-local `core.sshCommand` plus a host-specific private config and the bundled Python SOCKS5 stdio bridge

No global Git or SSH proxy is created. Test with `git -C PATH ls-remote REMOTE`; a push is unnecessary.

## Validation and rollback

Run `vpn-gateway test` after connecting. It checks preservation of host DNS, routes, public egress and global Git settings; loopback-only publication; `tun0`; UID routing; forced-`eth0` blocking; SOCKS; and pushed DNS. Add a real private URL for an application-level connection test.

`vpn-gateway uninstall` removes only installation-owned containers, image tags, launchers, copied profiles, runtime files and matching repository-local Git changes. Deleting the isolated browser profile requires a second confirmation. Docker Desktop, outer VPN software, repositories and global host settings remain.

See `WINDOWS.md` for Windows installation and acceptance, `QUICKSTART.md` for short commands, `HANDOFF.md` for transfer, and `MIGRATION.md` for the older package.
