# Developer quickstart

You need Windows 11 Home or Pro, Python 3.11+, and an IT-issued `.ovpn` profile
or a kit containing `gateway.toml` and its profiles. The WSL backend does not
require Docker. Corporate OpenVPN runs only inside the isolated Linux environment.

## 1. Install

Clone this repository and open a normal, non-administrator PowerShell window in
the checkout directory:

```powershell
.\setup.cmd
```

The wizard checks Python and the selected backend, recommends WSL for a new
Windows installation, imports your profile, asks you to confirm the external VPN
adapter, and provisions a dedicated distro. The original `.ovpn` stays unchanged.
Single-endpoint profiles with an explicit port and protocol are supported; scripts,
ambiguous profiles, and external certificate/key files are not imported automatically.

If Python is missing, install a maintained Python 3.11+ release from the
[official Windows download page](https://www.python.org/downloads/windows/),
open a new terminal, and retry. If WSL is missing, enable it separately in an
administrator PowerShell window:

```powershell
wsl --install --no-distribution
```

Restart Windows if prompted, then rerun the wizard as your normal user. Neither
Hyper-V Manager nor an upgrade to Windows Pro is required. See the
[official WSL installation guide](https://learn.microsoft.com/windows/wsl/install).
The wizard does not change networking mode, Windows Firewall, host routes, or DNS.

Accept User PATH registration if you want to use short command names. After setup,
open a **new PowerShell window**; you will no longer need to set `$env:Path`
manually for each session. Downloading Debian and building Dante may take several
minutes. If you cancel with Ctrl+C or a stage fails, rerun `.\setup.cmd`:
completed project-owned stages are preserved.

You can supply input paths in advance:

```powershell
.\setup.cmd --backend wsl --ovpn "C:\Secure\company.ovpn"
.\setup.cmd --backend wsl --config "C:\Secure\gateway.toml" --profiles-dir "C:\Secure\profiles"
```

Changes still require interactive confirmation; this is not an unattended installer.

## 2. Connect and open the corporate browser

Connect your external VPN, then run:

```powershell
vpn-gateway start --open-browser
```

Preflight checks the external route and verifies that Windows and the selected
backend have the same public egress **before** asking for your corporate username
and password, and again after you enter them. Both inputs are hidden. Never share
credentials in chat or pass them through command arguments, environment variables,
or configuration files.

During setup, choose a browser and provide your corporate start URL. To connect
without opening a browser, use `vpn-gateway start`. If the gateway is already
connected:

```powershell
vpn-gateway browser
vpn-gateway status
```

`vpn-browser` remains a compatible alias. Chrome may warn about
`--host-resolver-rules`: the local-DNS protection flag is retained, and the warning
is not hidden. Firefox is an explicit, **experimental** option with a separate
profile; your main Firefox profile is not changed. Its network guarantees still
require acceptance testing; preferences alone are not proof of isolation.
To change your saved browser choice:

```powershell
vpn-gateway setup --browser firefox --url https://YOUR-CORPORATE-HOST/
```

Replace the placeholder with your own URL. If the separate Firefox profile is
already open, use its window or close only that profile and retry. The launcher
does not close your main browser.

## 3. Configure Git — optional

Your repository stays on NTFS. No global Git proxy is changed.

```powershell
vpn-gateway git configure "C:\work\corporate-repository"
vpn-gateway git check "C:\work\corporate-repository"
vpn-gateway git check "C:\work\corporate-repository" --remote
```

The first command previews repository-local changes and asks for confirmation.
The second checks configuration without network access. The third performs a
read-only `ls-remote` through the proxy. Set up Git authentication and SSH host-key
trust separately on your machine; an authentication failure is not a successful
access check. These commands never push or automatically clone a repository.

## Troubleshoot

```powershell
vpn-gateway doctor
vpn-gateway doctor --json
vpn-gateway status --verbose
vpn-gateway logs
```

Doctor does not repair settings or interrupt the VPN. Checks that were not run are
marked explicitly. If a command is not found, first open a new terminal. For a
legacy installation, use its actual launcher path and then the setup wizard;
do not reinstall WSL just to fix PATH. Legacy MSIX paths are covered in
[ROLLBACK.md](ROLLBACK.md).

`ready` means the tunnel and SOCKS are ready, not that a particular application is
reachable. Test your URL with `vpn-gateway test https://YOUR-CORPORATE-HOST/`.
If no corporate DNS was pushed, the gateway reports that fact; it does not invent
a DNS server address.

## Stop, update, or uninstall

```powershell
vpn-gateway stop
.\setup.cmd --update
vpn-gateway uninstall --backend wsl
vpn-gateway uninstall
```

These are separate operations, not a sequence to run together. Update runs from
a new checkout and asks for permission to stop the gateway. Backend-only uninstall
removes only the managed distro; full uninstall also removes the project-owned
installation and PATH entry, and rolls back matching repository-local Git changes.
It does not remove WSL or Docker themselves, or unrelated browser profiles.
See [rollback and ownership boundaries](ROLLBACK.md).

`compare-transports` is a separate acceptance test that **interrupts active
connections**. Finish Git transfers before running it; do not use it as routine
diagnostics.

## macOS and acceptance testing

The macOS Docker backend and the original
`python3 install.py --config ... --profiles-dir ...` installation remain supported.
You can also use `python3 install.py --wizard --backend docker`. Commands are
installed in `~/.local/bin`; configure your shell PATH separately.

Current Windows/Firefox acceptance: [UX_ACCEPTANCE.md](UX_ACCEPTANCE.md).
Historical network results: [HISTORY.md](HISTORY.md).
Never commit profiles, certificates, cookies, credentials, backups, or raw diagnostics.
