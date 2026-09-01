# Quick start

Нужны три отдельные группы файлов:

1. Публичный обезличенный архив **Isolated OpenVPN Gateway**.
2. Приватный `gateway.toml` вашей организации.
3. Выданные IT файлы `.ovpn`. Логин и пароль в файлы не записываются.

## macOS

```bash
python3 install.py --check --config "/путь/gateway.toml" --profiles-dir "/путь/к/ovpn"
python3 install.py         --config "/путь/gateway.toml" --profiles-dir "/путь/к/ovpn"
export PATH="$HOME/.local/bin:$PATH"
vpn-gateway start
vpn-gateway test
vpn-browser https://private-host/
```

## Windows PowerShell

Сначала укажите характерную часть имени outer-VPN адаптера в `windows_outer_adapter_contains` по инструкции из `WINDOWS.md`.

```powershell
py -3.11 .\install.py --check --config "C:\Secure\gateway.toml" --profiles-dir "C:\Secure\profiles"
py -3.11 .\install.py         --config "C:\Secure\gateway.toml" --profiles-dir "C:\Secure\profiles"
$env:Path = "$env:LOCALAPPDATA\IsolatedOpenVPNGateway\bin;$env:Path"
vpn-gateway start
vpn-gateway test
vpn-browser https://private-host/
```

## Один Git-репозиторий

```text
vpn-gateway git-configure PATH_TO_REPOSITORY
git -C PATH_TO_REPOSITORY ls-remote origin
```

Остановка и удаление:

```text
vpn-gateway stop
vpn-gateway uninstall
```

Шлюз не запускается автоматически. Обычный браузер, обычный Git-трафик, DNS и маршруты host OS не перенаправляются.
