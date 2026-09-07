# Безопасная передача другому человеку

## Как объяснить в одном абзаце

Это локальный изолированный OpenVPN-to-SOCKS5 gateway для macOS и Windows. На Windows корпоративный OpenVPN работает либо в Docker Desktop Linux containers, либо в отдельном управляемом WSL2-дистрибутиве `IsolatedOpenVPNGateway` без Docker. WSL-процессы дополнительно находятся во вложенном network namespace, поэтому firewall не затрагивает общий WSL namespace и другие дистрибутивы. Только выбранные приложения подключаются через `socks5h://127.0.0.1:1080`; обычные Windows routes/DNS и внешний VPN не меняются.

## Что передавать

Передавайте раздельно и по одобренным каналам:

- release ZIP и внешний `.sha256`;
- приватный `gateway.toml`;
- актуальные `.ovpn` напрямую от IT;
- `QUICKSTART.md`, `WINDOWS.md` и `ROLLBACK.md`.

Не передавайте:

- `runtime/`, `validation/`, `backups/`, `settings.json` или `installation.json`;
- auth-файлы, browser profile/cookies или WSL VHDX;
- Git-репозитории как часть gateway kit;
- логин, пароль, private keys, сертификаты или содержимое `.ovpn` в чат/issue;
- endpoint/DNS/adapter diagnostics без отдельного согласования.

Release строится по явному allowlist и не включает `gateway.toml`, `config/`, `runtime/`, `.ovpn`, ключи или диагностику.

## Что выбрать на Windows Home

| Backend | Когда использовать | Docker Desktop |
|---|---|---:|
| `docker` | Docker Desktop уже одобрен и работает с Linux containers/WSL2 | нужен |
| `wsl` | нужен отдельный Docker-free Linux gateway | не нужен |

Оба backend используют WSL2-compatible lightweight virtualization и не требуют Hyper-V Manager. Не устанавливайте OpenVPN в основной пользовательский Ubuntu/WSL. Полная Hyper-V VM — отдельный Pro-only manual fallback и не поддерживается CLI.

## Приёмка на новом компьютере

1. Проверить внешний checksum release ZIP.
2. Убедиться, что Windows edition/build/architecture и hardware virtualization подходят.
3. Установить Python 3.11+, Git и выбранный backend prerequisite: Docker Desktop Linux containers или WSL2.
4. Включить внешний VPN, определить его adapter и заполнить `windows_outer_adapter_contains`.
5. Выполнить `install.py --check --backend docker|wsl ...`, затем установку с тем же backend.
6. Для WSL выполнить `vpn-gateway install --backend wsl`; убедиться, что создан только `IsolatedOpenVPNGateway`.
7. Запустить `vpn-gateway start --backend ...` и ввести собственные credentials.
8. Выполнить `vpn-gateway test https://REAL-PRIVATE-HOST/`.
9. Выполнить `vpn-gateway compare-transports --backend ...`: это обязательный positive/negative fail-closed test.
10. Проверить отдельный browser и read-only `git ls-remote` нужного репозитория.
11. Остановить gateway и подтвердить, что private browser/Git перестали работать, а обычная сеть сохранила внешний VPN egress.
12. На Windows выполнить `tools\windows_acceptance.ps1` и сохранить только безопасный итоговый отчёт, не raw DNS/routes.

Не переносите состояние сессии. На новый компьютер устанавливаются только публичный движок и приватные deployment inputs; credentials вводит новый пользователь. Репозитории остаются обычными NTFS-папками и переносятся независимо.

Для Debian 13 установщик собирает Dante 1.4.4 из официального исходника с проверкой закреплённого SHA-256 (в Debian 13 нет пакета `dante-server`). Нужен доступ к Debian и `www.inet.no`; смешивание stable/testing репозиториев не выполняется. Corporate DNS живёт только в bind-mounted resolver сетевого namespace шлюза, а внешний WSL resolver сохраняет DNS tunneling.

## Что не автоматизируется

- изменение `%USERPROFILE%\.wslconfig`, NAT/mirrored mode, Windows Firewall, routes или DNS;
- отключение требований outer VPN;
- угадывание корпоративных hostnames/DNS;
- настройка глобального Git/SSH proxy;
- перенос browser profile/cookies;
- Git push ради проверки.

Если внешний VPN не наследуется WSL/Docker, это blocker. Возможный mirrored mode обсуждается отдельно, с backup, явным согласием, `wsl --shutdown`, повторным preflight и rollback из `ROLLBACK.md`.

## Границы подтверждённой проверки

На Windows 11 Home Single Language 25H2 (build 26200.9168, AMD64) 2026-09-07 подтверждены реальное корпоративное подключение WSL2, ответ pushed DNS через Windows SOCKS, positive/negative fail-closed с независимым внешним control case и восстановление сеанса. Windows routes/DNS/egress и глобальный Git proxy сохранились. Это не заменяет проверку конкретного private URL и Git-репозитория новым пользователем: доступ к приложению, его авторизация и отдельный browser проверяются отдельно. Полные диагностические JSON остаются приватными и не входят в release; актуальные ограничения перечислены в `WINDOWS.md`.

## Снятие доступа

```text
vpn-gateway stop
vpn-gateway uninstall --backend wsl   # только project-owned WSL distro
vpn-gateway uninstall                 # полный project-owned cleanup
```

Uninstall не удаляет WSL как Windows-компонент, Docker Desktop, другие WSL-дистрибутивы, внешний VPN, репозитории или глобальные настройки host. Если ownership marker отсутствует или изменён, удаление останавливается для ручной проверки.
