# Быстрый старт для разработчика

Нужны Windows 11 Home/Pro, Python 3.11+ и выданный IT профиль `.ovpn`
либо комплект `gateway.toml` + профили. Docker для WSL-варианта не нужен.
Корпоративный OpenVPN запускается только внутри изолированного Linux.

## 1. Установить

Клонируйте этот репозиторий, откройте обычный PowerShell в его каталоге:

```powershell
.\setup.cmd
```

Мастер проверит Python и выбранный backend, предложит WSL для новой Windows-установки,
импортирует профиль, подтвердит внешний VPN-адаптер и подготовит отдельный дистрибутив.
Оригинал `.ovpn` не изменяется. Один endpoint с явным портом и протоколом поддерживается;
скрипты, неоднозначные профили и внешние security-файлы не импортируются автоматически.

Если Python отсутствует — установите поддерживаемый Python 3.11+ по
[официальной инструкции](https://www.python.org/downloads/windows/), откройте новый терминал
и повторите команду. Если WSL отсутствует, его включение выполняется отдельно в
PowerShell администратора:

```powershell
wsl --install --no-distribution
```

Перезагрузитесь, если Windows попросит, затем снова запустите мастер обычным
пользователем. Hyper-V Manager и upgrade до Pro не нужны.
[Официальная установка WSL](https://learn.microsoft.com/windows/wsl/install).
Сетевые режимы, Firewall, Windows routes/DNS мастер не меняет.

Согласитесь на User PATH, если хотите короткие команды. После установки откройте
**новое окно PowerShell**: повторять ручные `$env:Path` больше не нужно.
Загрузка Debian и сборка Dante могут занять несколько минут. При отмене Ctrl+C
или ошибке повторите `.\setup.cmd`: завершённые принадлежащие проекту этапы сохраняются.

Для заранее подготовленных входных параметров:

```powershell
.\setup.cmd --backend wsl --ovpn "C:\Secure\company.ovpn"
.\setup.cmd --backend wsl --config "C:\Secure\gateway.toml" --profiles-dir "C:\Secure\profiles"
```

Подтверждения изменений всё равно интерактивные; это не unattended installer.

## 2. Подключиться и открыть браузер

Включите внешний VPN, затем:

```powershell
vpn-gateway start --open-browser
```

Preflight проверит внешний маршрут и совпадение Windows/backend egress **до**
ввода корпоративного логина/пароля и повторно после него. Оба поля скрыты.
Не передавайте credentials в чат, аргументах, переменных среды или конфигурации.

При первой настройке выберите браузер и укажите свой корпоративный стартовый URL.
Для подключения без браузера используйте `vpn-gateway start`.
Для уже подключённого шлюза:

```powershell
vpn-gateway browser
vpn-gateway status
```

`vpn-browser` остаётся совместимым alias. Chrome может показывать предупреждение
о `--host-resolver-rules`: флаг блокировки локального DNS сохранён, предупреждение
не скрывается. Firefox добавлен как явный **экспериментальный** выбор: отдельный
профиль, без изменения основного Firefox. Его сетевые гарантии требуют приёмки;
настройки сами по себе не доказательство. Сменить сохранённый выбор:

```powershell
vpn-gateway setup --browser firefox --url https://YOUR-CORPORATE-HOST/
```

Подставьте свой адрес. Если Firefox-профиль уже открыт, используйте его окно либо
закройте только этот профиль и повторите запуск; основной браузер не закрывается.

## 3. Настроить Git — необязательно

Репозиторий остаётся на NTFS. Глобальный proxy не меняется.

```powershell
vpn-gateway git configure "C:\work\corporate-repository"
vpn-gateway git check "C:\work\corporate-repository"
vpn-gateway git check "C:\work\corporate-repository" --remote
```

Первая команда покажет план локальной настройки и запросит подтверждение.
Вторая проверит её без сети. Третья выполнит read-only `ls-remote` через proxy.
Git-авторизация/SSH host key настраиваются локально отдельно; отсутствие авторизации
не означает успешную проверку доступа. Никакого push или автоматического clone.

## Если что-то не работает

```powershell
vpn-gateway doctor
vpn-gateway doctor --json
vpn-gateway status --verbose
vpn-gateway logs
```

Doctor ничего не чинит и не прерывает VPN. Непроведённые проверки обозначаются явно.
Если команда не найдена — сначала откройте новый терминал. Для старой установки
используйте её фактический launcher, затем мастер; не переустанавливайте WSL ради PATH.
Особенности старых MSIX-путей описаны в [ROLLBACK.md](ROLLBACK.md).

`ready` означает готовность туннеля/SOCKS, не доступ к приложению. Проверьте свой URL
через `vpn-gateway test https://YOUR-CORPORATE-HOST/`. Отсутствующий pushed DNS
не подменяется выдуманным сервером.

## Остановить, обновить, удалить

```powershell
vpn-gateway stop
.\setup.cmd --update
vpn-gateway uninstall --backend wsl
vpn-gateway uninstall
```

Update запускается из нового checkout и требует подтверждения остановки. Backend-only
uninstall удаляет только управляемый дистрибутив; полный uninstall — принадлежащую
проекту установку, её PATH-запись и совпадающие локальные Git-изменения.
WSL/Docker целиком и чужие browser profiles не удаляются.
[Границы rollback](ROLLBACK.md).

`compare-transports` — отдельная **разрушающая текущие соединения** приёмка:
завершите Git-передачи перед ней. Не запускайте её как обычную диагностику.

## macOS и приёмка

На macOS сохранены Docker backend и исходная установка через
`python3 install.py --config ... --profiles-dir ...`; добавлен также
`python3 install.py --wizard --backend docker`.
Команды ставятся в `~/.local/bin`; shell PATH пользователь настраивает отдельно.

Новая Windows/Firefox-приёмка: [UX_ACCEPTANCE.md](UX_ACCEPTANCE.md).
Исторические сетевые результаты: [HISTORY.md](HISTORY.md).
Профили, сертификаты, cookies, credentials, backup и сырую диагностику в Git не добавляйте.
