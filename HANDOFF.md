# Передача другому человеку

## Как объяснить в одном абзаце

Это локальный изолированный OpenVPN-to-SOCKS5 gateway. OpenVPN работает внутри Docker Desktop, а выбранные приложения macOS подключаются к приватной сети через `socks5h://127.0.0.1:1080`. Остальной трафик Mac не меняется. При падении туннеля proxy блокирует запросы и не выходит через Docker `eth0`.

## Что передать

Передавайте отдельно:

- release ZIP и его `.sha256`;
- приватный `gateway.toml` по одобренному организацией каналу;
- актуальные `.ovpn` напрямую от IT или по одобренному секретному каналу;
- `QUICKSTART.md`.

Не передавайте runtime, auth-файлы, browser profile/cookies, validation logs, Git-репозитории, логин или пароль. Release ZIP специально строится по allowlist и не включает `gateway.toml`, `config/`, `runtime/`, `.ovpn`, ключи или диагностику.

## Приёмка на новом Mac

1. Установить и запустить Docker Desktop.
2. Включить необходимый outer VPN.
3. Проверить checksum release ZIP.
4. Выполнить `python3 install.py --check ...`, затем ту же команду без `--check`.
5. Запустить `vpn-gateway start` и ввести собственные credentials.
6. Выполнить `vpn-gateway test https://REAL-PRIVATE-HOST/`.
7. Проверить отдельный browser и read-only `git ls-remote` нужного репозитория.
8. Остановить VPN и подтвердить, что isolated browser/private Git больше не проходят, а обычная сеть Mac работает.

На новый Mac переносится движок и deployment data, но не состояние сессии. Репозитории остаются обычными папками macOS и переносятся своим стандартным способом.
