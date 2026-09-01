# Передача другому человеку

## Как объяснить в одном абзаце

Это локальный изолированный OpenVPN-to-SOCKS5 gateway для macOS и Windows. OpenVPN работает внутри Linux VM Docker Desktop, а только выбранные приложения подключаются к приватной сети через `socks5h://127.0.0.1:1080`. Остальной трафик компьютера не меняется. При падении `tun0` proxy блокирует запросы и не выходит напрямую через Docker `eth0`.

## Что передать

Передавайте отдельно:

- release ZIP и его `.sha256`;
- приватный `gateway.toml` по одобренному организацией каналу;
- актуальные `.ovpn` напрямую от IT или по одобренному секретному каналу;
- `QUICKSTART.md` и, для Windows, `WINDOWS.md`.

Не передавайте runtime, auth-файлы, browser profile/cookies, validation logs, Git-репозитории, логин или пароль. Release ZIP строится по allowlist и не включает `gateway.toml`, `config/`, `runtime/`, `.ovpn`, ключи или диагностику.

## Приёмка на новом компьютере

1. Установить и запустить Docker Desktop в режиме Linux containers и Python 3.11+.
2. Включить необходимый outer VPN.
3. На Windows определить его adapter и заполнить `windows_outer_adapter_contains`.
4. Проверить внешний checksum release ZIP.
5. Выполнить `install.py --check ...`, затем ту же команду без `--check`.
6. Запустить `vpn-gateway start` и ввести собственные credentials.
7. Выполнить `vpn-gateway test https://REAL-PRIVATE-HOST/`.
8. Проверить отдельный browser и read-only `git ls-remote` нужного репозитория.
9. Остановить gateway и подтвердить, что isolated browser/private Git больше не проходят, а обычная сеть работает.

На новый компьютер переносится движок и deployment data, но не состояние сессии. Репозитории остаются обычными папками host OS и переносятся стандартным способом. Для Windows обязательно прочитайте раздел ограничений в `WINDOWS.md`: часть приёмки зависит от конкретного VPN-клиента, Docker backend и корпоративных политик.
