# Quick start

Нужны три отдельные группы файлов:

1. Публичный обезличенный release **Isolated OpenVPN Gateway**.
2. Приватный `gateway.toml` вашей организации.
3. Выданные IT файлы `.ovpn`.

Логин/пароль в эти файлы не записываются. Не кладите `gateway.toml`, `.ovpn`, сертификаты, ключи или диагностику в Git.

## Windows 11 Home — WSL2 без Docker Desktop

WSL2 должен быть установлен заранее (обычно одноразово из PowerShell администратора: `wsl --install --no-distribution`, затем перезагрузка). Полная роль Hyper-V и Hyper-V Manager не нужны.

```powershell
py -3.11 .\install.py --check --backend wsl --config "C:\Secure\gateway.toml" --profiles-dir "C:\Secure\profiles"
py -3.11 .\install.py         --backend wsl --config "C:\Secure\gateway.toml" --profiles-dir "C:\Secure\profiles"
$env:Path = "$env:LOCALAPPDATA\IsolatedOpenVPNGateway\bin;$env:Path"
vpn-gateway install --backend wsl
vpn-gateway start --backend wsl
vpn-gateway test
vpn-browser https://private-host/
```

Будет создан только выделенный дистрибутив `IsolatedOpenVPNGateway`. Docker не требуется; пользовательский Ubuntu/WSL не изменяется.

## Windows 11 Home/Pro — Docker Desktop Linux containers

```powershell
py -3.11 .\install.py --check --backend docker --config "C:\Secure\gateway.toml" --profiles-dir "C:\Secure\profiles"
py -3.11 .\install.py         --backend docker --config "C:\Secure\gateway.toml" --profiles-dir "C:\Secure\profiles"
$env:Path = "$env:LOCALAPPDATA\IsolatedOpenVPNGateway\bin;$env:Path"
vpn-gateway start --backend docker
vpn-gateway test
```

Docker Desktop должен работать с WSL2 backend, `desktop-linux` context и Linux containers. Windows containers и Hyper-V Manager не нужны.

## macOS

```bash
python3 install.py --check --backend docker --config "/путь/gateway.toml" --profiles-dir "/путь/к/ovpn"
python3 install.py         --backend docker --config "/путь/gateway.toml" --profiles-dir "/путь/к/ovpn"
export PATH="$HOME/.local/bin:$PATH"
vpn-gateway start
vpn-gateway test
```

## Git, browser, stop

```text
vpn-gateway git-configure PATH_TO_REPOSITORY
git -C PATH_TO_REPOSITORY ls-remote origin
vpn-browser https://private-host/
vpn-gateway stop
```

HTTPS использует repository-local `socks5h`; SSH — repository-local `core.sshCommand`. Глобальные Git/SSH proxy не создаются. Репозитории остаются на NTFS.

Для реальной Windows-приёмки выполните `tools\windows_acceptance.ps1` по `WINDOWS.md`. Для удаления только WSL backend: `vpn-gateway uninstall --backend wsl`. Полный rollback описан в `ROLLBACK.md`.
