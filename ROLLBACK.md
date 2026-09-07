# Rollback and uninstall

This document defines exactly what the project may remove and how to recover optional host-side choices. Corporate profiles and credentials are never rollback material: obtain fresh profiles from IT and re-enter credentials interactively.

## Stop without uninstalling

```text
vpn-gateway stop
```

This stops only the recorded active backend, removes ephemeral credentials and derived runtime profile selection, and removes the Windows WSL forwarder. It does not change the saved backend, Windows routes/DNS/Firewall, Docker Desktop, WSL or repositories.

## Remove only the WSL backend

```text
vpn-gateway uninstall --backend wsl
```

The command requires confirmation and two ownership checks:

- Windows `installation.json` must say this installation created `IsolatedOpenVPNGateway`;
- `/etc/isolated-openvpn-gateway/ownership.json` inside that distro must identify this project.

Only then does it stop services, remove credentials/rules and run `wsl --unregister IsolatedOpenVPNGateway`. WSL itself, every other distro and Docker remain. If either marker is absent, the command refuses deletion. Do not manually unregister an unrecognized distro based only on its name.

## Full uninstall

```text
vpn-gateway uninstall
```

Full uninstall removes only:

- project-owned Docker containers and local image tags, when Docker is present;
- the managed WSL distro, only after the ownership checks and a second explicit confirmation;
- installed gateway files/launchers and ephemeral runtime/validation data;
- repository-local Git keys only when their current values still exactly match the values recorded by the gateway;
- the separate browser profile only after a separate confirmation and ownership-marker check.

It preserves Docker Desktop, WSL components, other distros, outer VPN software, Windows Firewall/routes/DNS/NRPT, global Git/SSH settings, repository contents and remote URLs.

HTTPS Git authentication stored by the user's credential helper (for example Windows Credential Manager) is separate from the gateway's temporary OpenVPN auth file. Gateway stop/uninstall does not erase those credentials or a completed clone. To revoke Git access, use the organization's credential/token revocation procedure; do not delete unrelated Credential Manager entries.

## PowerShell acceptance policy

The installer and gateway do not change PowerShell execution policy. If acceptance is blocked, preserve the result of `Get-ExecutionPolicy -List` and request a separate owner decision. A separately approved process-only policy ends when that PowerShell process and its children exit; verify `CurrentUser`, `LocalMachine` and Group Policy scopes still match the recorded state. No persistent-policy rollback should be needed. Do not change persistent scopes or indirectly execute a blocked script merely to pass acceptance.

## `.wslconfig` NAT/mirrored rollback

The gateway never edits `%USERPROFILE%\.wslconfig`. If the machine owner manually tested mirrored mode, a backup must have been made first:

```powershell
Get-ChildItem "$env:USERPROFILE\.wslconfig*.backup"
Copy-Item "C:\exact\approved\.wslconfig.TIMESTAMP.backup" "$env:USERPROFILE\.wslconfig" -Force
wsl.exe --shutdown
```

If no file existed before the test, remove only the explicitly added `networkingMode=mirrored` line (and an otherwise empty `[wsl2]` section), then run `wsl --shutdown`. Do not replace an unknown `.wslconfig` wholesale. Repeat Windows/backend public-egress, route and DNS checks after rollback.

Record the absent-file case explicitly rather than claiming a file backup exists. An automated rollback helper must refuse to delete the file if it contains any edits newer than the approved proposal. Gateway uninstall deliberately does not undo an owner-approved host-wide networking experiment: it may now be used by other WSL workloads.

## Failed WSL provisioning

If `vpn-gateway install --backend wsl` created the distro in the same attempt and provisioning then failed, the command unregisters that newly created distro as transaction rollback and clears its Windows ownership metadata. It never unregisters a distro that existed before the attempt.

If enterprise tooling interrupted the process, inspect safely:

```powershell
wsl.exe --list --verbose
Get-Content "$env:LOCALAPPDATA\IsolatedOpenVPNGateway\installation.json"
```

Do not print or share `gateway.toml`, `config\*.ovpn`, `runtime`, `validation` or auth material. If ownership evidence is inconsistent, preserve the distro and installation for manual review.

## Credential recovery

`stop` and the next `start` remove stale host and WSL auth files. If the machine crashed, run:

```text
vpn-gateway stop
```

The WSL auth file lives only at `/run/isolated-openvpn-gateway/auth` mode `0600`; Docker uses the protected Windows runtime auth file. SSD/NTFS/ext4 cannot promise forensic secure erasure, so short lifetime and restrictive ACL/mode are the protection. Rotate credentials through the organization's normal process if exposure is suspected.

## Post-rollback checks

After stop/uninstall/rollback, confirm:

1. `127.0.0.1:1080` is not listening.
2. Ordinary Windows curl/browser still uses the expected outer VPN.
3. Windows default route and DNS match the pre-install snapshot.
4. Global Git proxy/SSH settings are unchanged.
5. Other WSL distros still appear in `wsl --list --verbose`.
6. A non-corporate Git repository has no gateway-local proxy unless it was explicitly configured.

Use the read-only portions of `tools\windows_acceptance.ps1` or the commands in `WINDOWS.md`; never weaken Firewall or change routes/DNS to make a check pass.

If an outer-path check fails during transport comparison, the command stops and removes ephemeral credentials. It must not restore a corporate session over an unverified outer path. Once the external VPN and backend egress pass again, run `vpn-gateway start --backend wsl` and enter credentials interactively; do not recover an auth file from a backup. A successful positive/negative test earlier in the comparison does not mean the final session was restored: check the final status separately.

Private `validation/comparison.json` records comparison stages, completion and cleanup attempts, including separate original and cleanup error categories. `cleanup_attempted` alone is not proof that credentials were removed or services stopped. Confirm with current gateway status and listener checks; a report left incomplete by a crash is not a successful acceptance. These files can contain private deployment diagnostics and must not be included in a handoff or committed.
