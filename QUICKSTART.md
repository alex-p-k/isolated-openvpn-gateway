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

В Debian 13 Dante собирается из официального исходника с проверкой SHA-256; установке нужен доступ к Debian и `www.inet.no`. При запуске из packaged/MSIX-приложения Windows может перенаправить LocalAppData: используйте фактический путь к `bin`, выведенный установщиком, а не угадывайте его. Успешный preflight проверяет внешний VPN до скрытого интерактивного ввода credentials; не передавайте пароль в командной строке или чате.

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

Сообщение `Connected` подтверждает туннель и SOCKS handshake, но не доступ к приложению. Выполните `vpn-gateway test https://REAL-PRIVATE-HOST/` со своим адресом без пароля/query; без URL приложение явно остаётся `NOT TESTED`. `compare-transports` временно разрывает корпоративные соединения, запрашивает credentials интерактивно и проверяет успешный SOCKS-запрос до остановки OpenVPN, блокировку после остановки и восстановление WSL-сеанса. Не присылайте credentials в чат.

Дождитесь завершения Git-передач перед `compare-transports` или обновлением forwarder. Если сравнение не восстановило сеанс, проверьте `vpn-gateway status`: прежний успешный тест не означает, что gateway сейчас подключён. Новый запуск выполняется через `vpn-gateway start --backend wsl` с новым preflight и интерактивным вводом credentials. После `stop` проверяйте ошибку отдельного браузера принудительным обновлением `Ctrl+Shift+R`, а не по ранее загруженной странице.

Если PowerShell блокирует acceptance script, не меняйте системную execution policy и не обходите блокировку косвенным запуском. Порядок отдельного согласования process-only policy и rollback описан в `WINDOWS.md` и `ROLLBACK.md`.
