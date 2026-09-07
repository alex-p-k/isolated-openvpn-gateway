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

Владелец тестовой машины подтвердил открытие корпоративного task tracker в отдельном Chrome и позднее подтвердил, что при выключенном gateway страница не открывается. Это ручная положительная/отрицательная проверка, не автоматическая UI-проверка. После интерактивной HTTPS-авторизации полное клонирование на NTFS завершилось: HEAD доступен, tracked worktree чистый. Повторный read-only запрос прошёл через repository-local SOCKS без process-level proxy override; push не выполнялся. Эти результаты не переносят авторизацию на другого пользователя и не заменяют его собственную приёмку. Не прерывайте активные Git-передачи ради обновления forwarder или negative test. Исправление всплывающих WSL-консолей установлено после завершения клонирования.

Ранние Python-проверки сохранности Windows DNS/routes охватывали IPv4. Теперь CLI требует и успешное сравнение IPv6 DNS/routes; старый baseline без IPv6 не считается достаточным. После завершения клонирования реальная парная проверка IPv4/IPv6 DNS/routes прошла при остановке и повторном запуске VPN, включая новый outer preflight и успешный private HTTPS-запрос. Старый baseline сохранён отдельно, новый сформирован контролируемым preflight. Позднейшие попытки public IPv6 egress не дали результата: основной probe получил DNS error/timeout, а независимый numeric HTTPS control также не сработал по IPv4. Это не доказательство отсутствия IPv6 и не подтверждение равенства IPv6 egress.

Последующая повторная проверка UDP1194 прошла positive/negative fail-closed, но сравнение остальных transport не завершилось и исходный сеанс не восстановился. Итоговое состояние проверено отдельно: OpenVPN остановлен, credentials удалены, SOCKS listener отсутствует. Позднейшая диагностика без credentials снова показала совпадение Windows/WSL egress и сохранность host IPv4/IPv6 DNS/routes; нестабильность переподключения остаётся ограничением. Для восстановления нужен новый интерактивный `start`, а не auth-файл из backup. После нового интерактивного запуска и отдельного согласия установленный PowerShell listener acceptance прошёл с process-only `RemoteSigned`; сохранён исходный policy snapshot, постоянные scopes после проверки не изменились. Это не разрешение менять системную policy на компьютере следующего владельца.

Актуальный завершённый повтор после исправления namespace-start race: полный `compare-transports` на установленной WSL-копии прошёл для обоих UDP-ключей, восстановил UDP1194 и подтвердил сохранность Windows IPv4/IPv6 DNS/routes, public IPv4 egress и глобальных Git-настроек относительно исходного baseline. Дополнительный private HTTPS-запрос вернул 302 с проверкой TLS. TCP443 был реально запущен, но не инициализировался: соединение с HTTP-proxy из этого профиля истекло по timeout как в root WSL, так и внутри namespace, при успешном независимом внешнем HTTPS-контроле. Это не успешная TCP-приёмка; требуется проверка доступности control endpoint по текущему внешнему пути. Приватные адреса и исходные логи остаются вне публичного отчёта и Git.

Последующий запуск Docker Desktop завершился ошибкой его Ingest service до появления Linux engine: Windows 1920 при доступе к устаревшему AF_UNIX socket. Разрешённое переименование этого socket не удалось из агентской сессии, но позднее было выполнено владельцем в обычном PowerShell. Backup сохранён и проверен. Повторный запуск Docker остановился на другом служебном объекте `dockerInference`; его не меняли. Сброс Docker, удаление данных, изменение ACL или перезагрузка WSL не выполнялись. Работающий UDP-шлюз WSL и Windows IPv4/IPv6 DNS/routes, внешний public IPv4 egress и глобальные Git-настройки сохранены. Владелец явно отложил Docker-приёмку и выбрал WSL-only; сохранённый backend уже был `wsl`. Docker не удалён и не объявлен проверенным. Подробности и границы возможного rollback socket — в `WINDOWS.md`; сырые Docker logs не передавать автоматически.

Исправлен отдельный дефект отчёта: WSL comparison раньше читал Windows-путь журнала Docker. Теперь `events` берётся из выбранного backend; хвост ограничен 200 строками, `event_log.available` показывает доступность. У WSL `scope=backend_history`: это история нескольких запусков, а не доказательство событий только текущего транспорта. Проверка reader на реальном существующем журнале прошла без перезапуска VPN и чтения credentials. Прежние результаты comparison не переписывались; новый reader не означает успешное TCP-подключение. В отчёте не публиковать текст событий, даже если журнал называется `safe.log`.

Если обычная PowerShell-консоль не находит `vpn-gateway`, повторите две session-only строки PATH, которые вывел установщик, или используйте `& 'ACTUAL-INSTALLED-ROOT\bin\vpn-gateway.cmd' status`. Нужен именно фактический путь `Installed:`, в том числе при перенаправленной packaged/MSIX-установке; отсутствие команды в PATH не означает, что VPN остановлен. Постоянный User/Machine PATH и PowerShell profile установщик не меняет.

## Снятие доступа

При cleanup Linux PID сверяется с ожидаемой командой, root UID и network namespace; сигналы отправляются через pidfd, чтобы повторное использование номера PID не перенаправило остановку на другой процесс. Девять дополнительных тестов прошли внутри WSL, включая реальные временные процессы с положительным и отрицательным контролем. Работающий VPN для этого не останавливался. Этот тест сам по себе не заменяет uninstall-приёмку. При ошибке безопасной остановки не применяйте массовый kill; auth обычного `stop` удаляется и при такой ошибке.

```text
vpn-gateway stop
vpn-gateway uninstall --backend wsl   # только project-owned WSL distro
vpn-gateway uninstall                 # полный project-owned cleanup
```

Uninstall не удаляет WSL как Windows-компонент, Docker Desktop, другие WSL-дистрибутивы, внешний VPN, репозитории или глобальные настройки host. Если ownership marker отсутствует или изменён, удаление останавливается для ручной проверки.

После отдельного согласия владельца реальная полная WSL-only uninstall-приёмка прошла: удалены credentials, tun0, namespace, listener, управляемый дистрибутив, установленные файлы и четыре записанных Git-ключа. Сохранены исходные IT-профили, файлы/HEAD репозитория на NTFS, остальные Git-настройки, другие WSL-дистрибутивы, Windows IPv4/IPv6 routes/DNS, внешний IPv4 egress и `.wslconfig`. Browser profile оставлен по выбору владельца; Docker не вызывался. Выявленная ошибка CMD после удаления собственного launcher исправлена; повторный uninstall только Windows-слоя завершился с кодом 0. Для восстановления используются исходные IT-профили, не копия auth или VHDX. Новое подключение и его приёмка требуют свежего интерактивного ввода credentials: результаты прежней сессии не переносятся на новую автоматически.

Повторная установка завершена на прежнем physical root с сохранённым выбором WSL и восстановленными локальными Git-ключами. Новый preflight без credentials подтвердил равенство Windows/root WSL/namespace public IPv4, ожидаемый внешний маршрут и systemd. Диагностический namespace удалён; OpenVPN, credentials и listener отсутствуют, Windows IPv4/IPv6 routes/DNS и внешний IPv4 egress сохранились. Следующий шаг — интерактивный `start` владельца, затем новая проверка корпоративного подключения.
