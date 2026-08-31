# Quick start

Нужны три отдельные группы файлов:

1. Публичный обезличенный архив **Isolated OpenVPN Gateway**.
2. Приватный `gateway.toml` вашей организации.
3. Выданные IT файлы `.ovpn`. Логин и пароль в файлы не записываются.

Проверка и установка:

```bash
python3 install.py --check \
  --config "/путь/gateway.toml" \
  --profiles-dir "/путь/к/ovpn"

python3 install.py \
  --config "/путь/gateway.toml" \
  --profiles-dir "/путь/к/ovpn"
```

Затем:

```bash
export PATH="$HOME/.local/bin:$PATH"   # только если ~/.local/bin ещё не в PATH
vpn-gateway start
vpn-gateway test
vpn-browser https://private-host/
```

Для одного Git-репозитория:

```bash
vpn-gateway git-configure "/путь/к/repository"
git -C "/путь/к/repository" ls-remote origin
```

Остановка и удаление:

```bash
vpn-gateway stop
vpn-gateway uninstall
```

Шлюз не запускается автоматически при входе в macOS. Обычный браузер, обычный Git-трафик, DNS и маршруты macOS не перенаправляются.
