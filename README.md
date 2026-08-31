# Isolated OpenVPN Gateway

**Isolated OpenVPN Gateway** is a local client-side network gateway for macOS. It runs an inner OpenVPN client inside Docker Desktop and exposes that tunnel as a loopback-only SOCKS5 proxy. It is also accurately described as an **OpenVPN-to-SOCKS5 gateway**.

It is not a VPN server, a system-wide macOS VPN, or a general public proxy.

```text
macOS applications selected by the user
  -> socks5h://127.0.0.1:1080
  -> Dante in the Docker network namespace
  -> tun0 only
  -> private network

all other macOS traffic
  -> unchanged host route / optional outer VPN
```

The source is company-neutral. Deployment-specific endpoints, transport names, profile filenames, DNS canary and SOCKS port live in a private `gateway.toml`. OpenVPN profiles and credentials are never included in a release archive.

## Security properties

- OpenVPN runs only in Docker Desktop with `/dev/net/tun` and `NET_ADMIN`; `privileged: true` is not used.
- Docker publishes only `127.0.0.1:PORT`; the host LAN never receives a listener.
- Dante binds outbound sockets to `tun0`. UID routing and container firewall rules reject `eth0` fallback if the tunnel disappears.
- macOS DNS, routes and global Git configuration are not changed.
- Credentials are requested with hidden terminal input, written to a mode `0600` runtime file, never passed as arguments/environment, and removed on stop/failure/stale start.
- `.ovpn` files are copied mode `0600`; originals are read-only inputs and are never edited.
- Profile directives are allowlisted. Script/plugin/management directives are rejected. The exact remote and optional HTTP proxy must match `gateway.toml`.
- Inline profile blocks are treated as opaque private data. The deployment specifies which security blocks are required.

## Requirements

- macOS on arm64 or x86_64
- Docker Desktop with the `desktop-linux` engine running
- Python 3.11 or newer
- Git
- Chrome or Chromium only for the optional isolated browser

Docker outbound traffic must use the same public egress as the host. `vpn-gateway start` checks this before sending any credentials to OpenVPN. If Docker bypasses the outer VPN, startup stops.

## Deployment configuration

Copy `gateway.example.toml` outside the source package and replace only its examples with values from the IT-issued profiles. Keep it private. Each transport has an arbitrary short name; `udp` and `tcp` are conventions, not hardcoded engine choices.

```toml
[gateway]
id = "example-company"
display_name = "Example Company VPN"
default_transport = "udp"
dns_canary = "internal.example.com"
socks_port = 1080
require_outer_vpn = true
outer_interface_prefix = "utun"

[transports.udp]
profile_file = "company-udp.ovpn"
remote_host = "vpn.example.com"
remote_port = 1194
remote_protocol = "udp4"
required_inline_blocks = ["ca", "tls-auth"]
```

Supported IPv4 client protocols are `udp`, `udp4`, `tcp-client`, and `tcp4-client`. Add `http_proxy_host` and `http_proxy_port` only when the profile itself has the matching `http-proxy` directive. Set `require_outer_vpn = false` only for a deployment that intentionally does not require nesting.

`dns_canary` is a harmless hostname expected to return an A record through pushed private DNS. Do not put credentials, tokens or URLs with query strings in this file.

## Verify and install

From the extracted release directory:

```bash
shasum -a 256 -c isolated-openvpn-gateway-YYYY-MM-DD.zip.sha256
python3 install.py --check \
  --config "/private/path/gateway.toml" \
  --profiles-dir "/private/path/profiles"

python3 install.py \
  --config "/private/path/gateway.toml" \
  --profiles-dir "/private/path/profiles"
```

`--profiles-dir` resolves each `profile_file` from the TOML. For profiles stored in different directories, repeat `--profile NAME=/path/file.ovpn` once for every configured transport.

Installation creates:

```text
~/.local/share/isolated-openvpn-gateway/
~/.local/bin/vpn-gateway
~/.local/bin/vpn-browser
```

It refuses to overwrite existing paths. Optional `--legacy-aliases` creates `corp-vpn` and `corp-browser` aliases only when those names are unused.

## Commands

```bash
vpn-gateway start                 # configured default transport
vpn-gateway start TRANSPORT
vpn-gateway stop
vpn-gateway restart [TRANSPORT]
vpn-gateway status
vpn-gateway logs
vpn-gateway test [https://private-host/]
vpn-gateway compare-transports
vpn-gateway git-configure /path/to/repository
vpn-gateway build
vpn-gateway uninstall
vpn-browser [https://private-host/]
```

`start` succeeds only after OpenVPN reports `Initialization Sequence Completed`, the route hook succeeds, `tun0` exists and a SOCKS5 handshake passes. `compare-transports` tests every configured transport, including a real fail-closed test, and saves the first fully usable transport as default.

## DNS, browser and Git

Pushed `dhcp-option DNS` and search domains are applied only inside the VPN/SOCKS namespace. `socks5h` and the isolated browser resolve target names on the proxy side. macOS system DNS remains untouched. If the server pushes no DNS, the status/report says so rather than inventing an address.

`vpn-browser` uses `~/.local/share/isolated-openvpn-browser`, disables QUIC and non-proxied WebRTC UDP, prevents local target DNS, and has no `direct://` fallback. It never edits the main browser profile.

`vpn-gateway git-configure` changes only the selected repository and remote:

- HTTPS: repository-local `http.<exact-url>.proxy=socks5h://127.0.0.1:PORT`
- SSH: repository-local `core.sshCommand` with a host-specific private config and `/usr/bin/nc -X 5`

It creates no global Git or SSH proxy. Test with `git -C /path/to/repo ls-remote REMOTE`; a push is unnecessary.

## Validation and rollback

Run `vpn-gateway test` after connecting. It checks host DNS/routes/public egress/global Git preservation, loopback-only publication, `tun0`, UID routing, forced-`eth0` blocking, SOCKS and pushed DNS. Add a real private URL for an application-level connection check.

`vpn-gateway uninstall` removes only the installation-owned containers, image tags, launchers, copied profiles, runtime files and matching repository-local Git changes. The isolated browser profile requires a second explicit confirmation. Docker Desktop, outer VPN software, repositories and global macOS settings remain.

For moving from the older `corp-vpn-gateway` package, see `MIGRATION.md`. For handing this to another person, see `HANDOFF.md`.
