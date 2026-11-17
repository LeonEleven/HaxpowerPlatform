# ERPNext v16 独立验证环境

只提供 Stage 0 基础服务，不自动创建 Bench、ERPNext 站点、自定义 App 或业务数据。

环境实测和已知限制见 [搭建报告](../../docs/STAGE0-BOOTSTRAP-REPORT.md)，业务用例见 [验证计划](../../docs/ERPNext-VALIDATION-PLAN.md)。

## 文件与隔离布局

- `compose.yaml`：由官方开发模板裁剪的 MariaDB、Redis、Frappe 开发服务。
- `devcontainer.json`：可使用相同 Compose 的 VSCode Dev Containers 配置，无 SSH 挂载或自动安装命令。
- `versions.lock.json`：上游和目标应用 Commit、依赖声明及三个镜像的完整 Digest；镜像固定 linux/amd64。
- `prepare.py`：只生成本地配置和随机密码；不下载、安装或运行 Docker/Bench。
- `verify.py`：检查安全配置、镜像一致性、健康状态、连接、工具、依赖源和日志；不安装包或创建站点。

```text
~/labs/haxpower-erpnext-v16/            # 不在 HaxpowerPlatform 仓库内
  .env                               # 仅路径和固定镜像引用，无密码，0600
  compose.yaml
  versions.lock.json
  .devcontainer/devcontainer.json
  secrets/mariadb-root-password       # 本地秘密，不提交
  workspace/                         # 唯一挂入开发容器的可写工作区
  upstream/frappe_docker/             # 官方审查副本，不挂入容器
  bootstrap-checks.json               # 本地脱敏检查结果
```

Compose Project 固定为 `haxpower-erpnext-v16-lab`，同一 Docker Engine 上仅运行这一份；修改 `--lab-root` 只改变文件位置，不创建第二个 Project。不要给另一套环境复用该名称。

## 重建基础环境

在本项目根目录运行，宿主 UID 应为 1000，需有 Docker Desktop WSL 集成、Python 3.11+ 和兼容 Compose（本轮实测 5.7.0）。脚本拒绝将实验目录放在项目内，拒绝路径经过符号链接，且不会覆盖内容不同的既有配置或轮换已存在的数据库密码。

```bash
HAXPOWER_LAB_ROOT="$HOME/labs/haxpower-erpnext-v16"
python3 dev/erpnext-lab/prepare.py --lab-root "$HAXPOWER_LAB_ROOT"
python3 dev/erpnext-lab/verify.py --lab-root "$HAXPOWER_LAB_ROOT" --config-only
docker compose --project-directory "$HAXPOWER_LAB_ROOT" -f "$HAXPOWER_LAB_ROOT/compose.yaml" pull
docker compose --project-directory "$HAXPOWER_LAB_ROOT" -f "$HAXPOWER_LAB_ROOT/compose.yaml" up -d --wait --wait-timeout 180
python3 dev/erpnext-lab/verify.py --lab-root "$HAXPOWER_LAB_ROOT" --output "$HAXPOWER_LAB_ROOT/bootstrap-checks.json"
```

必须等待 `up --wait` 成功结束再执行运行检查；启动期间的 `starting` 不能算健康。检查脚本是本阶段基线检查，明确要求工作区尚无 `frappe-bench`；下一阶段创建 Bench 后需调整检查范围，不能沿用它宣称本站点业务通过。

镜像均从锁定 Digest 拉取，不会因 `latest`、`11.8` 或 `7.4-alpine` 标签变化而漂移。若旧 Digest 不再可用，应先记录并评审新版本，不自动退回浮动标签。该锁定覆盖基础环境；未来 Bench 应用的完整 Python/Node 依赖锁定仍需在安装阶段补齐。

重新获取上游审查副本时，在实验目录的 `upstream/frappe_docker` 新目录内用 Git fetch 锁定的 Commit，随后 detached checkout；不执行下载的 `installer.py` 或 shell 安装器。此副本不参与 Compose 启动：重建服务仅需要本目录的已审阅模板与锁定镜像。

## 在 VSCode 中连接

在 Windows VSCode 安装 WSL 和 Dev Containers 扩展，然后从 WSL 打开实验目录：

```bash
code "$HOME/labs/haxpower-erpnext-v16"
```

选择 `Dev Containers: Reopen in Container`。服务为 `frappe`，工作目录 `/workspace/development`，用户 `frappe` / UID 1000；`overrideCommand=false` 保留 `sleep infinity`。本轮仅用同一 Compose 完成无 GUI 启动与检查，VSCode GUI attach 尚待用户验证。

只预设转发 8000、9000、6787，绑定 localhost，自动发现其他端口关闭；基础阶段尚无 Web 服务监听。不要手动将 3306/6379 转发或发布到公网。关闭该 Dev Container 窗口采用 `stopCompose`；需要保留服务运行时注意这一行为。

## 安全与持久化

- 服务没有 `ports`。MariaDB 与两个 Redis 只接入内部 `backend` 网络；只有开发容器额外接入 `egress` 桥接网络访问依赖源。
- 不使用 host 网络、privileged、宿主 Docker socket、SSH 密钥挂载、项目源码挂载或生产服务器。开发容器丢弃全部 Linux capabilities 并启用 no-new-privileges。
- Frappe 命令只为 `sleep infinity`；无 Dev Container lifecycle 安装命令、扩展自动安装或站点初始化钩子。MariaDB 镜像仅初始化其系统库；Redis 队列仅初始化 AOF。这些基础数据不是 ERPNext 站点。
- 本地实验根目录和 `secrets` 目录均为 0700。密码文件为 0444，使 MariaDB 非 root 入口能读取 Compose bind-mounted secret；宿主其他用户不能穿过上级目录读取该文件。Compose 本地文件 secrets 的 uid/mode 重映射不可依赖，不声称密码文件本身为 0400。
- 随机密码不在 `.env`、Git、Compose environment 或命令参数中。检查时通过 stdin 送入开发容器，在 0600 临时客户端配置中使用并自动删除；仅 MariaDB 服务长期挂载密码文件。不要把该文件粘贴进终端历史、日志或 PR。
- MariaDB 的远程 root 仅用于隔离实验基础检查及下一阶段建站，允许来自内部网络；错误与空密码必须被拒绝。后续站点使用 Bench 创建的独立数据库账号，客户端不能取得 root 密码。
- Redis 无密码且关闭 protected mode，适用范围仅为本实验内部网络；不要接入不可信容器。队列卷独立持久化并采用 noeviction；缓存使用内存和可淘汰策略，不持久化。
- 专用命名卷仅有 `haxpower-erpnext-v16-lab_mariadb-data`、`haxpower-erpnext-v16-lab_redis-queue-data`；不使用 external 或固定全局共享卷。
- 内存上限：开发容器 4 GiB、MariaDB 2 GiB、Redis 各 256 MiB，总计 6.5 GiB；这不是资源预留。重建前仍要检查 Docker 内存、Windows 实体磁盘和 WSL 空间。
- 未注入 HTTP(S) 代理变量；不要复制宿主的 `127.0.0.1:7890` 进容器。若网络复测失败，先保存具体错误，只评估实验级配置，不修改全局代理、DNS、WSL 模式或系统认证。

只停止本实验、保留容器和数据卷：

```bash
docker compose --project-directory "$HOME/labs/haxpower-erpnext-v16" -f "$HOME/labs/haxpower-erpnext-v16/compose.yaml" stop
```

不运行 prune 或 `down -v`。更新配置时先检查差异与持久化影响，不通过删卷解决连接、权限或启动问题。`prepare.py` 内容不一致就停止，需要人工审阅后才更新实验副本。

## 下一阶段手动步骤（本次不执行）

在审查通过并获得后续建站任务后，于开发容器以 `frappe` 用户执行。先复核锁定 tag 对应的 Commit 和 `bench ... --help`，确认 Python 3.14.10、Node 24 的实际版本，再依次进行：

```bash
cd /workspace/development
bench init --frappe-path https://github.com/frappe/frappe.git --frappe-branch v16.55.0 --python /home/frappe/.pyenv/versions/3.14.10/bin/python --skip-redis-config-generation frappe-bench
cd frappe-bench
git -C apps/frappe rev-parse HEAD
bench set-config -g db_host mariadb
bench set-config -g redis_cache redis://redis-cache:6379
bench set-config -g redis_queue redis://redis-queue:6379
bench set-config -g redis_socketio redis://redis-queue:6379
bench set-config -g chromium_path /usr/lib/chromium/chromium-headless-shell
bench get-app --branch v16.55.0 https://github.com/frappe/erpnext.git
git -C apps/erpnext rev-parse HEAD
```

Frappe 必须为 `90c8b61afd60ee49f982ca1a9751008e4ae1e24c`，ERPNext 必须为 `8e10a348daaa93853de8c36dc55ec789a440c219`。如果 tag 改指向其他提交，停止并调查，不默默接受。

之后再创建站点（会初始化应用数据库）：

```bash
bench new-site --db-type mariadb --db-host mariadb --mariadb-user-host-login-scope=% haxpower.localhost
bench --site haxpower.localhost install-app erpnext
bench --site haxpower.localhost list-apps
bench use haxpower.localhost
bench start
```

新站点命令交互询问数据库 root 密码与站点管理员密码；通过本地安全渠道输入，不把密码写入命令参数。浏览器经 localhost 转发访问 `http://haxpower.localhost:8000`；如解析失败，先核查本地转发与 Host 路由，不擅自修改全局 DNS。

创建独立 `haxpower_platform` App、业务数据表或验证数据属于后续任务。安装期间产生的站点配置、源码、依赖、上传文件及数据库都留在实验环境；提交到本仓库前仅提取脱敏证据和经过审阅的自定义资源，不复制整个 Bench。
