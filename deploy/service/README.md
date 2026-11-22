# Деплой сервиса (игровой сервер) на droplet

Срез 5 профиля `game` (решение владельца 20.11: «слить, потом деплой»; хост — расширенный droplet `68.183.2.193`, 4 vCPU /
8 ГБ, Ubuntu 24.04, где с 20.11 работает и диспетчер под пользователем `ubuntu`, Node 20 уже стоит). Статический
`publish.sh` (`deploy/droplet/`) подменяет симлинк и ничего не запускает; игре нужен долгоживущий процесс, сборка,
проверка здоровья и откат. Это делает `deploy-service.sh`: релиз на коммит, **blue/green на двух локальных портах**,
переключение nginx-upstream только после 200 от health-пути, откат на прошлый релиз одной командой.

Все файлы — в `/Users/Shared/files/info/agent-team/deploy/service/`:

| Файл | Куда ставится | Что делает |
|---|---|---|
| `install.sh` | запускается на droplet как root (`remote-install.sh` с Mac) | каталоги (`/opt/agent-team/services` и `/var/lib/agent-team/services` — root; внутри сервисному пользователю принадлежат только клон, релизы и данные), скрипты и помощник учётных данных в `/opt/agent-team/bin/`, шаблон вхоста `/etc/agent-team/service.conf.template`, systemd-шаблон `agent-team-svc@.service` (User=`ubuntu`), правило sudo `/etc/sudoers.d/agent-team-deploy` (ubuntu → `deploy-service.sh` без пароля, больше ничего); проверяет, что есть `runuser` |
| `add-service.sh` | `/opt/agent-team/bin/` (root, один раз на игру) | `add-service.sh <slug> <host> <repo> [--branch main] [--run "npm run start"] [--build "npm run build"] [--install CMD] [--health /healthz] [--ports A,B] [--no-deploy]`: проверка DNS → `/etc/agent-team/services/<slug>.conf` (команды и два порта; свободная пара 8101/8102, 8111/8112, …) → заготовка секретов `<slug>.secrets.env` (root:ubuntu 0640, значения вписывает владелец) → http-вхост с `/.well-known/acme-challenge/` → `certbot certonly --webroot /var/www/acme` → upstream-файл → полный вхост через `render-vhost.sh` с шаблоном сервиса → первый деплой |
| `deploy-service.sh` | `/opt/agent-team/bin/` (root; диспетчер зовёт `sudo -n … <slug> --sha <commit>`) | lock → fetch → `releases/<sha12>` (rsync без `.git`) → `npm ci`/`npm install` (если есть `package.json`) → `BUILD_CMD` — эти четыре шага от сервисного пользователя через `runuser`, см. «Кто что исполняет» → env свободного «цвета» → `systemctl start agent-team-svc@<slug>-<a|b>` → health `http://127.0.0.1:<port><HEALTH_PATH>` до 200 (90 с) → upstream `/etc/nginx/conf.d/agent-team-svc-<slug>.conf` → `nginx -t` → reload → старый цвет гасится после того, как на нём закончились матчи: скрипт опрашивает его `DRAIN_BUSY_PATH` (по умолчанию `/_team/metrics.json`, поле `DRAIN_BUSY_FIELD` = `now.rooms_playing`) каждые `DRAIN_POLL_SEC` секунд, не дольше `DRAIN_MAX_SEC` (240 с), затем ждёт `DRAIN_SEC` и останавливает; сервис без такого документа гасится через `DRAIN_SEC`, как раньше → хранит 3 релиза → state + журнал. `--rollback` — прошлый релиз без сборки; `--force`, `--no-fetch` |
| `svc-run.sh` | `/opt/agent-team/bin/` | `ExecStart` юнита: читает `/var/lib/agent-team/services/<slug>/instances/<slug>-<цвет>.env`, затем секреты владельца, `cd` в релиз, `exec bash -c "$RUN_CMD"` (`$PORT` и `{port}` подставляются) |
| `agent-team-svc@.service` | `/etc/systemd/system/` | шаблон инстанса `<slug>-a` / `<slug>-b`: User ubuntu, `Restart=always`, `ProtectSystem=full`, запись только в `/var/lib/agent-team/services` |
| `service.conf.template` | `/etc/agent-team/` | nginx-вхост: 80 → 301, 443 ssl, `proxy_pass http://svc_<slug_с_подчёркиваниями>` (плейсхолдер `__SLUG_U__`: дефисы слага → `_`, как в upstream-файле deploy-service.sh; nginx не допускает «-» в именах переменных) с WebSocket (`Upgrade`/`Connection`, `proxy_read_timeout 3600`), `/_team/metrics.json` без кеша — его считает сам сервер |
| `git-credential-dispatcher` | `/opt/agent-team/bin/` (root:root 0755, секрета внутри нет) | помощник учётных данных git для приватного репозитория по https: отдаёт токен из `.env` диспетчера в момент вызова, только для `https://github.com`, ничего не сохраняет. См. «Приватный репозиторий» |
| `agent-team-deploy.sudoers` | `/etc/sudoers.d/agent-team-deploy` | одно правило NOPASSWD на `deploy-service.sh`; аргументы скрипт проверяет сам (slug `[a-z0-9-]`, `--sha` hex), настройки берёт только из root-файла `<slug>.conf`. Текст команд install и build при этом приходит из репозитория продукта (`scripts` в `package.json`), поэтому эти шаги идут не от root |
| `services.conf.example` | `/etc/agent-team/services/example.conf.example` | формат определения сервиса |

Что получает сервер в окружении: `PORT`, `DATA_DIR` (`/var/lib/agent-team/services/<slug>/data` — SQLite и файлы живут тут,
переживают релизы), `RELEASE_SHA`, `NODE_ENV=production`, `SERVICE_SLUG`, `SERVICE_HOST` и всё из `<slug>.secrets.env`.
Сервер обязан слушать `PORT` и отвечать 200 на health-путь (`/healthz` → `{"version": "<sha>"}`).

## Кто что исполняет

Диспетчер зовёт `deploy-service.sh` через sudo, скрипт стартует от root. Сценарии `postinstall` и `build` из
`package.json` пишутся в репозитории продукта, а туда без человека сливает роль developer. Поэтому от root скрипт
делает только то, чему root нужен, а всё, что читает или исполняет репозиторий, отдаёт сервисному пользователю
(`SVC_USER`, по умолчанию `ubuntu` — от него же работает сам сервер).

| От кого | Что |
|---|---|
| root | проверка аргументов, lock, владелец каталогов сервиса, файл окружения инстанса `instances/<slug>-<цвет>.env`, `systemctl start/stop`, upstream-файл nginx, `nginx -t`, reload, state `svc-<slug>.json`, журнал `deploy.log` |
| сервисный пользователь | `git clone / fetch / checkout`, rsync в релиз, `INSTALL_CMD` (`npm ci`), `BUILD_CMD`, запись `.install.log`, `.build.log`, `.built`, удаление старых релизов |

Шаги сервисного пользователя идут как `runuser -u <user> -- env -i HOME=… USER=… PATH=… LANG=… <команда>`: окружение
чистое, из окружения root и диспетчера в сборку не попадает ничего. Если root не может сменить пользователя (`runuser`
нет или он отказал), выкладка останавливается до клона: запасного пути «исполнить от root» в скрипте нет. Root не
открывает и не пишет файлы внутри клона, релизов и данных — репозиторий может положить ссылку на месте лога.

Чьи каталоги: `/opt/agent-team/services/<slug>/` (в нём клон `repo`), `/var/lib/agent-team/services/<slug>/releases` и
`…/data` — сервисного пользователя; `…/<slug>/` и `…/instances` — root. Клон и релизы, которые до 22.11 делал root,
следующая выкладка передаёт сервисному пользователю сама (`chown -R` дерева, у которого верхний каталог не его; в
журнале строка `action=adopted`). Это же срабатывает при `--rollback`, `--force` и повторной выкладке собранного sha.
Файлы внутри `data`, созданные от root руками, скрипт не трогает — их владельца правит тот, кто их создал.

Что не изменилось: сборка и сервер работают от того же пользователя, что и диспетчер с ролями, и ему доступен `.env`
диспетчера. Отдельный пользователь для сервисов — следующий шаг; здесь закрыт только путь к root.

## Приватный репозиторий

Клон и fetch делает сервисный пользователь, поэтому доступ к приватному репозиторию — его, а не root. Два способа:

**1. Deploy-ключ (ssh).** Ключ лежит у сервисного пользователя, `REPO` в `<slug>.conf` — ssh-адрес.

```bash
sudo -u ubuntu ssh-keygen -t ed25519 -N "" -f /home/ubuntu/.ssh/deploy_<slug>
# публичную часть добавить в репозиторий игры: Settings → Deploy keys (только чтение)
# /home/ubuntu/.ssh/config:  Host github.com / IdentityFile ~/.ssh/deploy_<slug> / IdentitiesOnly yes
sudo -u ubuntu ssh -T git@github.com                                # принять ключ хоста один раз
# /etc/agent-team/services/<slug>.conf:  REPO=git@github.com:<org>/<repo>.git
```

Ключ даёт доступ к одному репозиторию и только на чтение. Не подходит, когда политика организации запрещает
deploy-ключи (так у `formachines-ai`, трение F11 запуска ai-battles).

**2. Помощник `git-credential-dispatcher` (https).** `REPO` остаётся `https://github.com/<org>/<repo>.git`. Для https
`deploy-service.sh` зовёт git с `-c credential.helper= -c credential.helper=/opt/agent-team/bin/git-credential-dispatcher`:
остальные помощники отключены, отвечает только этот. Когда GitHub спрашивает логин, помощник читает `GITHUB_TOKEN` из
`.env` диспетчера (`~/agent-team/dispatcher/.env` сервисного пользователя) и отдаёт его git. Токен никуда не
копируется: его нет ни в помощнике, ни в адресе клона, ни в хранилище учётных данных git. Помощник отвечает только для
`https://github.com`; публичному репозиторию он не мешает (git его не вызывает).

Работает, пока сервисный пользователь может прочитать `.env` — сейчас это тот же `ubuntu`, файл `0600`. Нового доступа
помощник ему не даёт. Настройки (в `<slug>.conf`, обычно не нужны): `DISPATCHER_ENV_FILE=/путь/.env` — другой файл с
токеном; `GIT_CREDENTIAL_HELPER=""` — без помощника (тогда действует git-конфигурация самого пользователя).

Проверка на сервере, токен не печатается:

```bash
printf 'protocol=https\nhost=github.com\n\n' | sudo -u ubuntu /opt/agent-team/bin/git-credential-dispatcher get | grep -v '^password='
# username=x-access-token   и строка INFO в stderr: из какого файла взят токен
```

До 22.11 такой помощник стоял на сервере вручную (root, 0700), а клон делал root. `install.sh` заменяет его файлом из
репозитория и один раз сохраняет прежний как `git-credential-dispatcher.before-repo`.

## Первая установка и регистрация игры

```bash
/Users/Shared/files/info/agent-team/deploy/service/remote-install.sh
# DROPLET_HOST=root@68.183.2.193, DROPLET_KEY=~/.ssh/bymachines_lab_ed25519, SVC_USER=ubuntu — дефолты
```

1. **Владелец**: A-запись `<slug>.formachines.ai → 68.183.2.193`, репозиторий игры на GitHub (публичный; приватный — см. «Приватный репозиторий»).
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
остановлен, живой не тронут) · 21 `nginx -t` отверг upstream (прежний восстановлен) · 1 clone/install/build либо не
удалось сменить пользователя · 64 аргументы.

Кто собирал релиз: `stat -c '%U' /var/lib/agent-team/services/<slug>/releases/*/.built` — должен быть сервисный
пользователь, не root; первая строка вывода выкладки называет его: `repo-steps-as=ubuntu (runuser)`.

## Тесты

`tests/test_servicedeploy.py` гоняет настоящий `deploy-service.sh` в tmp: фейковый `systemctl` (python, запускает
`svc-run.sh` в своей группе процессов), фейковые `nginx`/`journalctl`, игра — `python3 -m http.server $PORT`; проверяются
первый деплой, blue/green, пропуск того же коммита, откат и возврат, провал health и сборки, отказ nginx; с поддельными
`runuser`, `npm` и `chown` — что клон, install и build идут через `runuser` в чистом окружении, что без смены
пользователя сценарий `build` не исполняется вовсе, что клон и релизы прежнего владельца передаются сервисному
пользователю, и что помощник отдаёт токен только для `https://github.com`. Диспетчерская
часть (`team/servicedeploy.py`, `Cycle.deploy_after_merge`, `team deploy`) — против фейкового скрипта деплоя. Без root и сети.
