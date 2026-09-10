# Safe handoff to another developer

## What the project does

Corporate OpenVPN runs in Docker Linux containers or a dedicated WSL2 distro.
Only explicitly configured applications use the local SOCKS5 proxy; ordinary
Windows routes, DNS, and the external VPN remain unchanged. Windows Home supports
the Docker-free WSL backend.

## What to share

- A link to a reviewed repository commit, or an allowlisted release ZIP with its checksum.
- [QUICKSTART.md](QUICKSTART.md), [WINDOWS.md](WINDOWS.md), and [ROLLBACK.md](ROLLBACK.md).
- Through a separate IT-approved channel: a compatible `.ovpn`, or `gateway.toml` and its profiles.
- The required external VPN and corporate start URL, without credentials.

The `.\setup.cmd` wizard guides a new developer through importing inputs, selecting
a backend and adapter, provisioning Linux, and optionally configuring User PATH,
a browser, and repository-local Git. Git is needed for repository operations, not
for browser-only gateway use. The new user enters their own credentials locally;
your session state is not transferred.

## What not to share

- `config/`, `runtime/`, `validation/`, `backups/`, or the WSL VHDX;
- `settings.json`, ownership/setup/update/PATH journals, or registry exports;
- browser profiles, cookies, auth files, or Git credential-helper stores;
- corporate endpoints, certificates, private keys, or raw network traces in a public issue.

The release builder uses an explicit allowlist. It does not walk the entire
checkout or collect private files alongside the source. A checksum verifies
integrity; it does not replace verification of the source's trustworthiness.

## What to verify on the new machine

Follow [UX_ACCEPTANCE.md](UX_ACCEPTANCE.md): a new Windows Home user, command
discovery in a fresh terminal, both input flows, interruption and resume, browser
and read-only Git access, update/rollback, and uninstall ownership boundaries.
Network acceptance requires a successful request, a negative test after losing
`tun0`, a working independent control case, and successful restoration. Never push
to a repository just to test connectivity.

Firefox remains experimental until proxy-side DNS and the absence of direct
fallback are verified on the actual browser version. Chrome retains its DNS
protection flag with an explanation of the warning. Main browser profiles remain
unchanged.

Historical Windows Home/WSL acceptance is recorded in [HISTORY.md](HISTORY.md).
It does not validate the new wizard, a new user's environment, or Firefox.
The original machine owner deferred Docker acceptance; do not report Docker as
verified on that basis.

## Recovery and access revocation

`vpn-gateway doctor` provides non-destructive diagnostics. An external-path
mismatch blocks connection before credentials are requested. The gateway does
not automatically change `.wslconfig`, Windows Firewall, routes/DNS, or global
Git/SSH settings, and does not weaken TLS.

`vpn-gateway stop` removes temporary VPN auth material. `uninstall --backend wsl`
removes only the owned distro. Full `uninstall` also removes installed commands
and a matching owned PATH entry, and restores recorded previous repository-local
Git values. Unrelated changes are not overwritten; WSL and Docker themselves
are not removed.

Git tokens/passwords stored by a normal credential helper are separate from VPN
authentication. Revoke them through your organization's procedures, not by
deleting arbitrary Windows credentials.
