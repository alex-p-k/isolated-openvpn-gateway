# Безопасная передача другому разработчику

## Что это

Корпоративный OpenVPN работает в Docker Linux containers или отдельном WSL2-дистрибутиве.
Только явно настроенные приложения идут через локальный SOCKS5; обычные routes/DNS
Windows и внешний VPN не меняются. На Windows Home доступен Docker-free WSL-вариант.

## Что передать

- Ссылку на проверенный commit этого репозитория либо allowlisted release ZIP с checksum.
- [QUICKSTART.md](QUICKSTART.md), [WINDOWS.md](WINDOWS.md), [ROLLBACK.md](ROLLBACK.md).
- Через отдельный одобренный IT-канал: совместимый `.ovpn` или `gateway.toml` + профили.
- Сведения о требуемом внешнем VPN и корпоративном URL, без credentials.

Мастер `.\setup.cmd` рассчитан на нового разработчика: импорт, выбор backend/адаптера,
подготовка Linux, необязательный User PATH, browser и локальный Git.
Git требуется для работы с репозиториями, но не для browser-only шлюза.
Новый пользователь вводит свои credentials локально; состояние вашей сессии не переносится.

## Что не передавать

- `config/`, `runtime/`, `validation/`, `backups/`, WSL VHDX;
- `settings.json`, ownership/setup/update/PATH journals и registry exports;
- browser profiles, cookies, auth-файлы или Git credential-helper хранилища;
- corporate endpoints, сертификаты, private keys и сырые network traces в публичный issue.

Сборка release использует явный allowlist: она не обходит весь checkout и не захватывает
приватные файлы рядом с исходниками. Наличие checksum не заменяет проверку источника.

## Что проверить на новом компьютере

Пройдите [UX_ACCEPTANCE.md](UX_ACCEPTANCE.md): новый Windows Home-пользователь,
новый терминал с рабочей командой, оба input flow, прерывание/повтор, браузер и
read-only Git, update/rollback, uninstall ownership boundaries.
Сетевая приёмка требует положительного запроса, отрицательного теста при потере tun0,
работающего независимого control case и восстановления. Не делайте push ради теста.

Firefox остаётся экспериментальным до проверки proxy-side DNS и отсутствия direct
fallback на фактической версии. Chrome сохраняет защитный DNS-флаг с объяснённым
предупреждением. Основные профили браузеров не меняются.

Историческая Windows Home/WSL приёмка находится в [HISTORY.md](HISTORY.md).
Она не подтверждает новый мастер, нового пользователя или Firefox. Docker-приёмка
на исходной машине отложена владельцем; это не повод объявлять Docker проверенным.

## Восстановление и снятие доступа

`vpn-gateway doctor` — неразрушающая диагностика. При расхождении внешнего пути
подключение блокируется до ввода credentials. Никаких автоматических изменений
`.wslconfig`, Firewall, routes/DNS, глобального Git/SSH или ослабления TLS.

`vpn-gateway stop` удаляет временный VPN auth. `uninstall --backend wsl` удаляет
только owned distro; полный `uninstall` также убирает установленные команды,
совпадающую собственную PATH-запись и восстанавливает записанные предыдущие локальные
Git-значения. Чужие изменения не перетираются, WSL/Docker целиком не удаляются.

Git tokens/passwords в обычном credential helper — отдельная авторизация. Их отзыв
выполняется по правилам организации, а не удалением произвольных Windows credentials.
