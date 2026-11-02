# Деплой сайтов пилота на droplet

Статические сайты `<slug>.formachines.ai` / `<slug>.bymachines.ai`, каждый — публичный репозиторий на GitHub с **уже собранным** каталогом `public/` в коммите. Droplet (`root@68.183.2.193`, Ubuntu, 1 vCPU, 961 МБ, nginx + certbot + git + rsync + python3; Node нет и не ставим) ничего не билдит: раз в 5 минут тянет репозитории и подменяет симлинк на новый релиз.

Все файлы — в `/Users/Shared/files/info/agent-team/deploy/droplet/`:

| Файл | Куда ставится | Что делает |
|---|---|---|
| `install.sh` | запускается на droplet как root | каталоги, скрипты в `/opt/agent-team/bin/`, шаблон и примеры в `/etc/agent-team/`, nginx-сниппет, systemd-юниты, таймеры |
| `remote-install.sh` | запускается с Mac | `tar` этой папки + `team/sitemetrics.py` в `/tmp/agent-team-deploy` на droplet → `install.sh` |
| `publish.sh` | `/opt/agent-team/bin/publish.sh` | для каждой строки `sites.tsv`: clone/fetch → `reset --hard origin/<branch>` → если HEAD не менялся, пропуск → `rsync` в `/var/www/<slug>-releases/<sha>` → атомная подмена симлинка `/var/www/<slug>` → хранит 5 релизов → smoke `https://<host>/` через `127.0.0.1` → журнал и state |
| `add-site.sh` | `/opt/agent-team/bin/add-site.sh` | регистрирует сайт: строка в `sites.tsv`, проверка DNS, http-вхост → certbot → полный вхост из шаблона → первый `publish.sh` |
| `metrics.sh` | `/opt/agent-team/bin/metrics.sh` | раз в час запускает `python3 /opt/agent-team/bin/sitemetrics.py --all-hosts --log … --out /var/lib/agent-team/metrics/` |
| `site.conf.template` | `/etc/agent-team/site.conf.template` | nginx server block одного хоста (`__HOST__`, `__SLUG__`): 80 → 301, 443 ssl http2, root `/var/www/<slug>`, `/_team/` → `alias /var/lib/agent-team/metrics/<host>/` без кеша |
| `nginx-headers.snippet.conf` | `/etc/nginx/snippets/agent-team-headers.conf` | nosniff / SAMEORIGIN / referrer-policy — подключается в каждом location (nginx не наследует `add_header`) |
| `agent-team-publish.{service,timer}` | `/etc/systemd/system/` | `publish.sh` каждые 5 мин, `OnBootSec=2min`, root, `Nice=10` |
| `agent-team-metrics.{service,timer}` | `/etc/systemd/system/` | `metrics.sh` каждый час |
| `sites.tsv.example` | `/etc/agent-team/sites.tsv.example` | формат списка сайтов (реальный — `/etc/agent-team/sites.tsv`) |
| `env.example` | `/etc/agent-team/env` (создаётся, если нет) | `CERTBOT_EMAIL=` |

`sites.tsv` — пять колонок через TAB: `slug  host  repo  branch  public_dir`. Строки с `#` и пустые игнорируются. YAML сознательно не используется: на droplet нет PyYAML, а TSV читается одним `read`.

Модуль `team/sitemetrics.py` пишет другой агент; `install.sh` только копирует его в `/opt/agent-team/bin/sitemetrics.py`. Пока файла нет, `agent-team-metrics.service` падает с понятной ошибкой — на публикацию это не влияет.

## Первая установка

```bash
/Users/Shared/files/info/agent-team/deploy/droplet/remote-install.sh
# DROPLET_HOST=root@68.183.2.193 и DROPLET_KEY=~/.ssh/bymachines_lab_ed25519 — дефолты, переопределяются переменными
ssh -i ~/.ssh/bymachines_lab_ed25519 root@68.183.2.193 'nano /etc/agent-team/env'   # CERTBOT_EMAIL=
```

Повторный запуск безопасен: скрипты, юниты и шаблон перезаписываются, `sites.tsv` и `env` не трогаются.

## Новый сайт — порядок

1. **Владелец** создаёт DNS A-запись `<slug>.formachines.ai → 68.183.2.193` (или `.bymachines.ai`). Без неё `add-site.sh` выходит с кодом 2, ничего не изменив (проверка DNS идёт первой).
2. На droplet:
   ```bash
   /opt/agent-team/bin/add-site.sh foo foo.formachines.ai https://github.com/formachines-ai/foo.git main public
   ```
   Скрипт: проверка DNS → строка в `/etc/agent-team/sites.tsv` → (a) временный вхост только на :80 → `nginx -t` → reload; (b) `certbot --nginx -d foo.formachines.ai --non-interactive --agree-tos -m $CERTBOT_EMAIL --redirect` (если сертификат уже есть — шаг пропускается); (c) полный вхост из `/etc/agent-team/site.conf.template` → `nginx -t` → reload; (d) `publish.sh --site foo`.
3. Дальше любой push в `main` репозитория — через ≤ 5 минут сайт обновлён (таймер). Руками: `/opt/agent-team/bin/publish.sh --site foo` (`--force` — переопубликовать тот же HEAD).

## Как проверить

```bash
systemctl list-timers 'agent-team-*'                  # когда следующий запуск
systemctl status agent-team-publish.service           # последний прогон
journalctl -u agent-team-publish.service -n 50        # подробный вывод publish.sh
tail -n 20 /var/log/agent-team/publish.log            # журнал: дата slug sha action smoke
cat /var/lib/agent-team/state/foo.json                # {sha, release, published_at, smoke, smoke_http_code}
ls -la /var/www/foo /var/www/foo-releases/            # живой симлинк и релизы
curl -sI https://foo.formachines.ai/ | head -n 1      # 200
curl -s https://foo.formachines.ai/_team/metrics.json # метрики (после первого прогона metrics)
```

Smoke в `publish.sh` — `curl --resolve <host>:443:127.0.0.1 https://<host>/`, не зависит от DNS снаружи; `smoke=fail` в журнале + ненулевой код сервиса = сайт опубликован, но nginx не отдал 200 (чаще всего нет `index.html` в `public_dir` или ещё нет сертификата).

## Откат

Релизы лежат в `/var/www/<slug>-releases/<sha12>/` (последние 5), живой сайт — симлинк `/var/www/<slug>`:

```bash
ls -1t /var/www/foo-releases/                                  # какие есть
ln -sfn /var/www/foo-releases/<старый sha> /var/www/foo.tmp && mv -T /var/www/foo.tmp /var/www/foo
```

Таймер **вернёт** HEAD через ≤ 5 минут (сравнивает симлинк с `origin/<branch>`), поэтому откат надолго = `git revert` в репозитории сайта, либо `systemctl stop agent-team-publish.timer` на время разбора.

## Ограничения и договорённости

- **1 vCPU / 961 МБ:** на сервере ничего не собираем — `public_dir` должен быть закоммичен. `publish.sh` клонирует `--depth 50`; тяжёлые операции идут под `Nice=10`.
- Репозитории **только публичные** и только `https://` — на droplet нет ни ключей, ни токенов.
- `publish.sh` отказывается работать, если `/var/www/<slug>` существует и это не симлинк (чтобы не затереть чужую папку).
- Один прогон за раз: `flock /run/lock/agent-team-publish.lock` (таймер и ручной запуск не пересекаются; ждёт до 5 минут).
- Все вхосты пишут в общий `/var/log/nginx/access.log` форматом `full` (есть `$host`) — из него `sitemetrics.py` считает метрики по хостам. Если `log_format full` в nginx не найден, `install.sh` предупреждает: без него `nginx -t` не пройдёт.
- `/_team/` отдаётся из `/var/lib/agent-team/metrics/<host>/` через `alias`, ничего не копируется в релиз; `metrics.sh` после прогона делает `chmod -R a+rX`, чтобы www-data мог читать.
- Существующие вхосты `formachines.conf` и `bymachines.conf` скрипты не трогают; новые конфиги — `/etc/nginx/sites-available/<slug>.conf` + симлинк в `sites-enabled`.
- Убрать сайт: удалить строку из `/etc/agent-team/sites.tsv`, `rm /etc/nginx/sites-enabled/<slug>.conf`, `nginx -t && systemctl reload nginx`; релизы и клон остаются (`/var/www/<slug>*`, `/opt/agent-team/sites/<slug>`) до ручного удаления; сертификат — `certbot delete --cert-name <host>`.
