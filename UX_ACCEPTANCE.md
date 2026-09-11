# Product acceptance: evidence, not promises

The CLI product layer is separate from previously recorded WSL gateway acceptance.
New setup/update/Firefox behavior needs its own real acceptance. Unit tests are not
a Windows Home first-use study or proof of browser DNS isolation.

## macOS implementation and live gateway validation — 2026-09-11

Host: macOS 15.6.1 (24G90), arm64; Python 3.11.9; Chrome 152.0.7977.83.
Version: 2026.09.11.1, migrated from the installed 2026.08.30.1 Docker deployment.

- Regression suite: 208 tests, 197 passed and 11 platform-specific skips on macOS,
  using the ordinary unittest command and default macOS temporary directory.
- Live migration: PASS. Versioned images built and checked before stopping; private
  backup mode checks passed, no auth file was backed up, original app image IDs
  retained, and installed package hashes matched the verified source.
- Live UDP connection: PASS. Initialization, owned containers, tun0 address and
  policy health, actual macOS IPv4-loopback listener and SOCKS handshake verified.
- Corporate DNS payload, corporate HTTPS connection and repository-local read-only
  Git remote check: PASS. No repository network settings were rewritten by migration.
- Independent firewall challenge: PASS. A direct outer Docker HTTPS control worked;
  a temporary destination route in the gateway namespace forced proxy UID 10000
  toward eth0, its request failed, and the OUTPUT REJECT packet counter increased.
  The exact probe route was removed. This was measured BEFORE tunnel loss because
  Docker tears down the VPN container's outer networking when it exits.
- Tunnel-loss test: PASS. After successful SOCKS DNS payload, only OpenVPN was
  terminated, tun0 disappeared and new SOCKS requests failed. A fresh outer-path
  preflight and UDP reconnect restored readiness and corporate DNS payload.
- Host DNS/default/static/public routes, outer egress and global Git configuration:
  PASS against the fresh connection baseline, including across the loss/restoration test.
- Chrome synthetic socket probe: PASS (direct positive control, SOCKS DOMAIN payload,
  rejection, browser-reported errors and no direct control-target hits). Dedicated
  corporate launcher and saved GitLab landing-page preference exercised separately.
- Packet-level local DNS observer: NOT RUN. macOS refused the filtered capture
  interface with Operation not permitted. Continuous browser request observation
  across real tunnel loss: NOT RUN. These are not inferred from the separate tests.
- Fresh Windows/WSL acceptance of this revision: NOT RUN on this Mac. Existing WSL
  implementation and platform-specific tests remain; mocks are not live acceptance.

The first diagnostic prototype could not measure the firewall after owner teardown;
it reported failure rather than passing. The final probe records its actual phase
and requires observed counters; no hardcoded Docker firewall PASS remains.

## Implementation validation — 2026-09-10

Machine: Windows Home Single Language (`CoreSingleLanguage`), 25H2,
build 26200.9168, AMD64; normal user-installed Python 3.11.0.
This is the existing developer machine, **not** a clean new-user installation.

- Regression suite: 196 tests, 193 passed and 3 platform-specific skips.
  Setup/resume/update/PATH/registry tests use temporary fixtures and mocks; they
  are not evidence of a fresh installed deployment or live registry mutation.
- Real CMD bootstrap: help from the checkout and a Unicode path with spaces;
  missing-Python message in a process with an empty PATH.
- Real headless Firefox 143 and Chrome 152.0.7977.77: synthetic loopback SOCKS
  payload and hostname transmission passed; explicit proxy rejection produced
  browser-reported errors with no direct control-target hits. The independent
  direct positive control succeeded for both browsers.
- Windows DNS observer and real tun-loss/browser restoration: **NOT RUN** for
  this product revision. Firefox remains experimental, not recommended by default.
- Clean Windows Home first-use, live product update/uninstall and current-revision
  corporate browser/Git checks: **NOT RUN**. Existing gateway was not stopped or
  updated by these development checks. Docker acceptance remains deferred;
  macOS compatibility has regression coverage, not a new real macOS run.

## Clean Windows Home journey

Use a separate disposable Windows Home test machine/account, not an uninstall of a
working corporate setup. Record Windows edition/build/architecture, Python, WSL,
browser versions and whether Docker is absent. Use IT-approved private inputs;
never attach them to an issue or commit them.

1. Clone the source; run `.\setup.cmd` as a normal desktop user. With Python absent,
   verify the actionable prerequisite message. Install supported Python explicitly.
2. Test both a compatible single `.ovpn` and an IT TOML kit. No manual TOML editing
   is needed for the single-profile path. Verify original file hashes are unchanged.
3. Select WSL and the real outer VPN adapter. Do not install Docker. BIOS changes,
   WSL enablement, reboot and NAT/mirrored changes remain separately approved actions.
4. Cancel at each wizard stage; rerun. Confirm owned completed stages survive and
   another distro with the same name is never adopted.
5. Opt into user PATH. Open a new terminal and run `Get-Command vpn-gateway` and
   `vpn-gateway --version`. Repeat setup: no duplicate PATH entry.
6. Connect locally using hidden credentials. Confirm Windows/backend egress match,
   route/DNS preservation, tun0 and only the IPv4 loopback SOCKS listener.
7. `vpn-gateway browser`, `vpn-gateway git check PATH --remote`: corporate page and
   read-only repository access work; no global Git/SSH/browser changes.
8. Run update from a newer checkout; cancel once, then accept. Verify rollback on
   an injected test-copy write/smoke failure. Stop/start requires fresh credentials.
9. Uninstall only the test installation. Check owned PATH removal, previous local
   Git values restored, unrelated PATH entries/distros/browser profiles preserved.

Success: a developer unfamiliar with this project follows the quickstart without
author intervention or manual PATH/TOML edits, and starts daily work with one command.
Record task completion and points of confusion, not just elapsed download time.

## Browser networking acceptance (Chrome and Firefox separately)

Use a fresh owned test profile and record executable version and exact generated
settings. Firefox remains experimental until ALL checks below pass on that version.

Start with the opt-in synthetic socket probe (does not use/stop the gateway):

```powershell
py -3 tools/browser_probe.py --browser firefox
py -3 tools/browser_probe.py --browser chromium
```

It launches only disposable headless profiles and loopback servers. It requires a
real browser direct positive control, a SOCKS payload with DOMAIN address type,
explicit SOCKS rejections, browser-reported request failures, and no direct target
hits. `socket_probe_passed` is deliberately separate from `acceptance_complete`;
local DNS tracing and real tun-loss checks below are still required.

- Positive: an uncached corporate hostname loads through the active gateway.
- Instrumented SOCKS: record CONNECT address type DOMAIN (ATYP 3) and the exact
  synthetic hostname. Do not log corporate URLs, cookies, headers or credentials.
- DNS observer: use a separately approved Windows DNS/packet trace with private
  output storage. Prove the observer detects a deliberate synthetic direct DNS
  query first. Confirm no local queries for the browser canary with proxying enabled.
  Cache inspection alone is insufficient. Do not change Windows DNS to run the test.
- Positive direct control: a synthetic non-corporate endpoint is reachable from a
  separate test client, and the endpoint's request counter actually increments.
- Negative A: keep browser and SOCKS alive, stop ONLY managed OpenVPN using the
  existing documented fail-closed acceptance procedure. Verify new uncached page,
  fetch/WebSocket and synthetic direct-control requests fail without DNS/direct
  traffic. Confirm the browser process remains alive and the observer still works.
- Negative B: stop the gateway/forwarder entirely. Reload an uncached page; no
  direct fallback. An already rendered page or service-worker cache is not evidence.
- Restore the gateway with fresh local credentials if needed; repeat the positive
  checks. Preserve Windows routes/DNS and ordinary outer-VPN egress throughout.
- Open the normal browser alongside the corporate browser; verify profile and
  session separation. Repeat a launcher call while the Firefox profile is busy;
  it must not redirect the URL into the normal profile.

Do not mark fail-closed/DNS acceptance complete when tracing is unavailable or only
the negative case was observed. Do not disable certificate checks, browser sandbox,
Chrome warning bars or global firewall protection to make these checks pass.

## Support evidence

Share `vpn-gateway doctor --json` only after reviewing its output. It intentionally
contains categorized results instead of public IPs, VPN endpoints, routes, DNS
addresses, browser history or credentials. Full network traces and private backups
stay local. Report manual checks as PASS / FAIL / NOT RUN with version and date.
