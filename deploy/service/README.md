# Деплой сервиса (игровой сервер) на droplet

Срез 5 профиля `game` (решение владельца 20.11: «слить, потом деплой»; хост — расширенный droplet `68.183.2.193`, 4 vCPU /
8 ГБ, Ubuntu 24.04, где с 20.11 работает и диспетчер под пользователем `ubuntu`, Node 20 уже стоит). Статический
`publish.sh` (`deploy/droplet/`) подменяет симлинк и ничего не запускает; игре нужен долгоживущий процесс, сборка,
проверка здоровья и откат. Это делает `deploy-service.sh`: релиз на коммит, **blue/green на двух локальных портах**,
переключение nginx-upstream только после 200 от health-пути, откат на прошлый релиз одной командой.

Все файлы — в `/Users/Shared/files/info/agent-team/deploy/service/`:

| Файл | Куда ставится | Что делает |
|---|---|---|
| `install.sh` | запускается на droplet как root (`remote-install.sh` с Mac) | каталоги, скрипты в `/opt/agent-team/bin/`, шаблон вхоста `/etc/agent-team/service.conf.template`, systemd-шаблон `agent-team-svc@.service` (User=`ubuntu`), правило sudo `/etc/sudoers.d/agent-team-deploy` (ubuntu → `deploy-service.sh` без пароля, больше ничего) |
| `add-service.sh` | `/opt/agent-team/bin/` (root, один раз на игру) | `add-service.sh <slug> <host> <repo> [--branch main] [--run "npm run start"] [--build "npm run build"] [--install CMD] [--health /healthz] [--ports A,B] [--no-deploy]`: проверка DNS → `/etc/agent-team/services/<slug>.conf` (команды и два порта; свободная пара 8101/8102, 8111/8112, …) → заготовка секретов `<slug>.secrets.env` (root:ubuntu 0640, значения вписывает владелец) → http-вхост с `/.well-known/acme-challenge/` → `certbot certonly --webroot /var/www/acme` → upstream-файл → полный вхост через `render-vhost.sh` с шаблоном сервиса → первый деплой |
| `deploy-service.sh` | `/opt/agent-team/bin/` (root; диспетчер зовёт `sudo -n … <slug> --sha <commit>`) | lock → fetch → `releases/<sha12>` (rsync без `.git`) → `npm ci`/`npm install` (если есть `package.json`) → `BUILD_CMD` → env свободного «цвета» → `systemctl start agent-team-svc@<slug>-<a|b>` → health `http://127.0.0.1:<port><HEALTH_PATH>` до 200 (90 с) → upstream `/etc/nginx/conf.d/agent-team-svc-<slug>.conf` → `nginx -t` → reload → старый цвет гасится через `DRAIN_SEC` → хранит 3 релиза → state + журнал. `--rollback` — прошлый релиз без сборки; `--force`, `--no-fetch` |
| `svc-run.sh` | `/opt/agent-team/bin/` | `ExecStart` юнита: читает `/var/lib/agent-team/services/<slug>/instances/<slug>-<цвет>.env`, затем секреты владельца, `cd` в релиз, `exec bash -c "$RUN_CMD"` (`$PORT` и `{port}` подставляются) |
| `agent-team-svc@.service` | `/etc/systemd/system/` | шаблон инстанса `<slug>-a` / `<slug>-b`: User ubuntu, `Restart=on-failure`, `ProtectSystem=full`, запись только в `/var/lib/agent-team/services` |
| `service.conf.template` | `/etc/agent-team/` | nginx-вхост: 80 → 301, 443 ssl, `proxy_pass http://svc_<slug_с_подчёркиваниями>` (плейсхолдер `__SLUG_U__`: дефисы слага → `_`, как в upstream-файле deploy-service.sh; nginx не допускает «-» в именах переменных) с WebSocket (`Upgrade`/`Connection`, `proxy_read_timeout 3600`), `/_team/metrics.json` без кеша — его считает сам сервер |
| `agent-team-deploy.sudoers` | `/etc/sudoers.d/agent-team-deploy` | одно правило NOPASSWD на `deploy-service.sh`; аргументы скрипт проверяет сам (slug `[a-z0-9-]`, `--sha` hex), команды берёт только из root-файла `<slug>.conf` |
| `services.conf.example` | `/etc/agent-team/services/example.conf.example` | формат определения сервиса |

Что получает сервер в окружении: `PORT`, `DATA_DIR` (`/var/lib/agent-team/services/<slug>/data` — SQLite и файлы живут тут,
переживают релизы), `RELEASE_SHA`, `NODE_ENV=production`, `SERVICE_SLUG`, `SERVICE_HOST` и всё из `<slug>.secrets.env`.
Сервер обязан слушать `PORT` и отвечать 200 на health-путь (`/healthz` → `{"version": "<sha>"}`).

## Первая установка и регистрация игры

```bash
/Users/Shared/files/info/agent-team/deploy/service/remote-install.sh
# DROPLET_HOST=root@68.183.2.193, DROPLET_KEY=~/.ssh/bymachines_lab_ed25519, SVC_USER=ubuntu — дефолты
```

1. **Владелец**: A-запись `<slug>.formachines.ai → 68.183.2.193`, репозиторий игры на GitHub (публичный или с deploy-ключом droplet).
2. На droplet (root):
   ```bash
   /opt/agent-team/bin/add-service.sh fleets fleets.formachines.ai https://github.com/formachines-ai/fleets.git --run "npm run start" --build "npm run build"
   nano /etc/agent-team/services/fleets.secrets.env     # если игре нужны секреты (SESSION_SECRET=…); значения никогда не в git
   ```
3. В `products/<slug>/product.yaml` диспетчера: `deploy: {mode: service, host: local}` (вместо `pending`) и `site_base: https://fleets.formachines.ai`.
   С этого момента каждый merge задачи developer/designer → `sudo -n deploy-service.sh <slug> --sha <commит>` (`Cycle.deploy_after_merge`),
   tester играет против живого хоста, а при провале health задача блокируется с вопросом PO, прошлый релиз остаётся в строю.
   Руками: `team deploy <slug> [--sha S | --rollback] [--show]`.

## Как проверить

```bash
systemctl status 'agent-team-svc@fleets-*'                       # какой цвет живой
journalctl -u agent-team-svc@fleets-a -n 50                       # лог сервера
tail -n 20 /var/log/agent-team/deploy.log                         # журнал: slug sha action colour port
cat /var/lib/agent-team/state/svc-fleets.json                     # {sha, release, colour, port, deployed_at, previous}
cat /etc/nginx/conf.d/agent-team-svc-fleets.conf                  # upstream → живой порт
curl -s https://fleets.formachines.ai/healthz                     # версия = sha релиза
/opt/agent-team/bin/deploy-service.sh fleets --rollback           # откат на previous (и обратно той же командой)
```

Коды выхода `deploy-service.sh`: 0 задеплоено · 10 этот коммит уже живой и здоров · 20 health не ответил (новый цвет
остановлен, живой не тронут) · 21 `nginx -t` отверг upstream (прежний восстановлен) · 1 clone/install/build · 64 аргументы.

## Тесты

`tests/test_servicedeploy.py` гоняет настоящий `deploy-service.sh` в tmp: фейковый `systemctl` (python, запускает
`svc-run.sh` в своей группе процессов), фейковые `nginx`/`journalctl`, игра — `python3 -m http.server $PORT`; проверяются
первый деплой, blue/green, пропуск того же коммита, откат и возврат, провал health и сборки, отказ nginx. Диспетчерская
часть (`team/servicedeploy.py`, `Cycle.deploy_after_merge`, `team deploy`) — против фейкового скрипта деплоя. Без root и сети.
