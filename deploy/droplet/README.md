# Деплой сайтов пилота на droplet

Статические сайты `<slug>.formachines.ai` / `<slug>.bymachines.ai`, каждый — публичный репозиторий на GitHub с **уже собранным** каталогом `public/` в коммите. Droplet (`root@68.183.2.193`, Ubuntu, 1 vCPU, 961 МБ, nginx + certbot + git + rsync + python3; Node нет и не ставим) ничего не билдит: раз в 5 минут тянет репозитории и подменяет симлинк на новый релиз.

Все файлы — в `/Users/Shared/files/info/agent-team/deploy/droplet/`:

| Файл | Куда ставится | Что делает |
|---|---|---|
| `install.sh` | запускается на droplet как root | каталоги, скрипты в `/opt/agent-team/bin/`, шаблон и примеры в `/etc/agent-team/`, ключ IndexNow (`/etc/agent-team/indexnow.key`, генерируется один раз) и keyfile для nginx, nginx-сниппет, **перерендер вхостов всех сайтов из `sites.tsv`** через `render-vhost.sh`, systemd-юниты, таймеры |
| `remote-install.sh` | запускается с Mac | `tar` этой папки + `team/sitemetrics.py` в `/tmp/agent-team-deploy` на droplet → `install.sh` |
| `publish.sh` | `/opt/agent-team/bin/publish.sh` | для каждой строки `sites.tsv`: clone/fetch → `reset --hard origin/<branch>` → если HEAD не менялся, пропуск → `rsync` в `/var/www/<slug>-releases/<sha>` → атомная подмена симлинка `/var/www/<slug>` → хранит 5 релизов → smoke `https://<host>/` через `127.0.0.1` → **IndexNow** (URL из `sitemap.xml` релиза, см. раздел ниже; `--no-indexnow` выключает) → журнал и state |
| `add-site.sh` | `/opt/agent-team/bin/add-site.sh` | регистрирует сайт: строка в `sites.tsv`, проверка DNS, http-вхост → certbot → полный вхост через `render-vhost.sh` → первый `publish.sh` |
| `render-vhost.sh` | `/opt/agent-team/bin/render-vhost.sh` | `render-vhost.sh <slug> <host> [--no-reload]`: рендер `site.conf.template` (`__HOST__`, `__SLUG__`, `__INDEXNOW_KEY__`) → пишет `/etc/nginx/sites-available/<slug>.conf` только если текст изменился → `nginx -t` → при ошибке возвращает прежний файл (exit 1) → reload. Exit 0 = без изменений, 3 = обновлён. Общий для `add-site.sh` и `install.sh` |
| `metrics.sh` | `/opt/agent-team/bin/metrics.sh` | раз в час запускает `python3 /opt/agent-team/bin/sitemetrics.py --all-hosts --log … --out /var/lib/agent-team/metrics/` |
| `site.conf.template` | `/etc/agent-team/site.conf.template` | nginx server block одного хоста (`__HOST__`, `__SLUG__`, `__INDEXNOW_KEY__`): 80 → 301, 443 ssl http2, root `/var/www/<slug>`, `/_team/` → `alias /var/lib/agent-team/metrics/<host>/` без кеша, `= /<key>.txt` → `alias /etc/agent-team/indexnow-keyfile.txt` |
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

Повторный запуск безопасен: скрипты, юниты и шаблон перезаписываются, `sites.tsv`, `env` и `indexnow.key` не трогаются. После обновления шаблона `install.sh` перерендеривает вхост каждого сайта из `sites.tsv`, у которого уже есть сертификат (`render-vhost.sh … --no-reload` на каждый: пишет только при изменении, `nginx -t` на каждый, при ошибке возвращает прежний конфиг и идёт дальше), затем один `nginx -t && systemctl reload nginx`, если что-то поменялось. Итог — строка `vhosts: updated=… unchanged=… skipped=… errors=…`.

## Новый сайт — порядок

1. **Владелец** создаёт DNS A-запись `<slug>.formachines.ai → 68.183.2.193` (или `.bymachines.ai`). Без неё `add-site.sh` выходит с кодом 2, ничего не изменив (проверка DNS идёт первой).
2. На droplet:
   ```bash
   /opt/agent-team/bin/add-site.sh foo foo.formachines.ai https://github.com/formachines-ai/foo.git main public
   ```
   Скрипт: проверка DNS → строка в `/etc/agent-team/sites.tsv` → (a) временный вхост только на :80 → `nginx -t` → reload; (b) `certbot --nginx -d foo.formachines.ai --non-interactive --agree-tos -m $CERTBOT_EMAIL --redirect` (если сертификат уже есть — шаг пропускается); (c) полный вхост через `render-vhost.sh foo foo.formachines.ai` (шаблон + ключ IndexNow → `nginx -t` → reload, откат при ошибке); (d) `publish.sh --site foo`.
3. Дальше любой push в `main` репозитория — через ≤ 5 минут сайт обновлён (таймер). Руками: `/opt/agent-team/bin/publish.sh --site foo` (`--force` — переопубликовать тот же HEAD, `--no-indexnow` — без пинга IndexNow).

## Как проверить

```bash
systemctl list-timers 'agent-team-*'                  # когда следующий запуск
systemctl status agent-team-publish.service           # последний прогон
journalctl -u agent-team-publish.service -n 50        # подробный вывод publish.sh
tail -n 20 /var/log/agent-team/publish.log            # журнал: дата slug sha action smoke · строка action=indexnow indexnow=<код> urls=<n>
cat /var/lib/agent-team/state/foo.json                # {sha, release, published_at, smoke, smoke_http_code, indexnow_http_code, indexnow_urls}
ls -la /var/www/foo /var/www/foo-releases/            # живой симлинк и релизы
curl -sI https://foo.formachines.ai/ | head -n 1      # 200
curl -s https://foo.formachines.ai/_team/metrics.json # метрики (после первого прогона metrics)
```

Smoke в `publish.sh` — `curl --resolve <host>:443:127.0.0.1 https://<host>/`, не зависит от DNS снаружи; `smoke=fail` в журнале + ненулевой код сервиса = сайт опубликован, но nginx не отдал 200 (чаще всего нет `index.html` в `public_dir` или ещё нет сертификата).

## IndexNow

После каждой публикации (новый sha или `--force`, при `smoke=ok`) `publish.sh` сообщает IndexNow (Bing, Yandex, Seznam, Naver и др. — один endpoint) список URL сайта, чтобы страницы попали в индекс, не дожидаясь обхода. Образец — Errata (`/Users/Shared/files/info/formachines/errata/errata/indexnow.py`), здесь то же самое на bash + curl.

- **Ключ один на все сайты пилота:** `/etc/agent-team/indexnow.key` (32 hex, `openssl rand -hex 16`). Генерирует `install.sh` при первом запуске, дальше не трогает; ключ публичный по протоколу (его и так отдаёт сайт), права 0644. Рядом `/etc/agent-team/indexnow-keyfile.txt` — тот же ключ без перевода строки; это файл, который nginx отдаёт как `https://<host>/<key>.txt` (`location = /<key>.txt { alias /etc/agent-team/indexnow-keyfile.txt; }` в `site.conf.template`, плейсхолдер `__INDEXNOW_KEY__`). Сборка сайта ничего про ключ знать не должна — в отличие от Errata, где `/errata/<key>.txt` кладёт сборка.
- **Что отправляется:** `POST https://api.indexnow.org/indexnow`, `Content-Type: application/json; charset=utf-8`, тело `{host, key, keyLocation: https://<host>/<key>.txt, urlList: [...]}`. `urlList` — все `<loc>` из `/var/www/<slug>-releases/<sha>/sitemap.xml` (grep/sed, XML-сущности раскрываются, не больше 10 000); если sitemap нет или в нём нет `<loc>` — только `https://<host>/`. Таймаут 20 с. Ответ 200/202 = принято; любой другой код, таймаут или отсутствие ключа — предупреждение в журнале, публикация не ломается. 200 от IndexNow означает только «принято», не «проиндексировано».
- **Выключить / подменить:** `publish.sh --no-indexnow` (строка в журнале `indexnow=off urls=0`); `INDEXNOW_ENDPOINT=http://127.0.0.1:8080/x publish.sh --force` — для тестов на свой приёмник; `INDEXNOW_MAX_URLS`, `INDEXNOW_TIMEOUT_SEC`, `INDEXNOW_KEY_FILE`, `INDEXNOW_SITEMAP` — переменные окружения в начале `publish.sh`.
- **Как проверить:**
  ```bash
  cat /etc/agent-team/indexnow.key                                   # ключ
  curl -s https://foo.formachines.ai/$(cat /etc/agent-team/indexnow.key).txt   # должен напечатать тот же ключ (text/plain, 200)
  /opt/agent-team/bin/publish.sh --site foo --force                  # принудительная публикация → в выводе "[foo] indexnow: HTTP 200 — accepted (N urls)"
  grep 'action=indexnow' /var/log/agent-team/publish.log | tail -n 5 # 2026-11-02T20:30:00Z slug=foo sha=… action=indexnow indexnow=200 urls=12 host=foo.formachines.ai
  journalctl -u agent-team-publish.service -n 50 | grep -i indexnow  # тело ответа при коде ≠ 200/202
  ```
  `indexnow=nokey` — нет `/etc/agent-team/indexnow.key` (повторить `install.sh`); `indexnow=skip` — smoke не прошёл, пинг не отправлялся; `indexnow=000` — IndexNow не ответил за 20 с; `indexnow=422` — IndexNow не смог прочитать `https://<host>/<key>.txt` (проверить curl выше и `nginx -t`); `429` — слишком часто, следующая публикация отправит снова.

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
- Ключ IndexNow общий: сменить — `rm /etc/agent-team/indexnow.key` и повторить `remote-install.sh` (новый ключ, keyfile и перерендер всех вхостов); старый `<key>.txt` перестанет отвечать.
