# Migration from `corp-vpn-gateway`

The generic installation uses new paths and commands, so the known-working macOS legacy installation can remain untouched during acceptance:

```text
legacy:  ~/.local/share/corp-vpn-gateway       corp-vpn / corp-browser
generic: ~/.local/share/isolated-openvpn-gateway  vpn-gateway / vpn-browser
```

Both gateways publish a loopback SOCKS port, so never run them at the same time when they use the same port.

## Safe migration order

1. Stop the legacy gateway with `corp-vpn stop`.
2. Build a private `gateway.toml` whose transport endpoints exactly match the original profiles.
3. Install the generic package without `--legacy-aliases`.
4. Start and validate the generic gateway, browser fail-closed behavior and repository-scoped Git.
5. Keep using `vpn-gateway` for a trial period.
6. Only after acceptance, remove the legacy installation with `corp-vpn uninstall`.

Do not copy legacy runtime credentials, derived profiles, logs, validation state or browser cookies. Supply the original IT profiles to the new installer so it can validate and copy them afresh.

The generic installer does not edit or remove the legacy installation. If `corp-vpn` already exists, `--legacy-aliases` intentionally refuses to overwrite it.

There is no automatic legacy-to-Windows migration. On Windows, use the original IT-issued profiles and a fresh private `gateway.toml`; do not copy macOS runtime state, credentials, browser data or validation logs.
