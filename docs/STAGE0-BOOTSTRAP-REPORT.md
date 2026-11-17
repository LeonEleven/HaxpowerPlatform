# Stage 0 — ERPNext/Frappe 本地基础环境搭建报告

日期：2026-11-17（Asia/Shanghai）。任务类型：环境实现与原型基础设施验证。范围：独立 Dev Container 基础服务，不创建 Bench 或 ERPNext 站点。

## 1. 结果与停止边界

独立 Compose Project **`haxpower-erpnext-v16-lab`** 已启动，Frappe 开发容器、MariaDB、两个 Redis 均为 running/healthy。开发容器内的数据库/Redis 连接、开发工具及 GitHub/PyPI/npm/Yarn 网络检查通过。

实验目录为 `/home/h-goal/labs/haxpower-erpnext-v16/`，与项目及生产服务器隔离。交付时保留四个基础容器运行，便于审查；不自动进入下一阶段。

本次**没有**执行 `bench init`、`bench new-site`、`install-app` 或 `new-app`，没有创建 HaxpowerPlatform 业务表或库存代码。MariaDB 只由官方入口初始化系统数据库；查询结果仅为 `information_schema`、`mysql`、`performance_schema`、`sys`。`workspace/` 仍为空，Frappe/ERPNext 应用尚未安装。没有访问阿里云、修改业务规则、修改核心源码、删除其他项目容器/卷或改动系统网络/认证配置。

可重建文件及说明见 [dev/erpnext-lab](../dev/erpnext-lab/README.md)，完整版本依据见 [versions.lock.json](../dev/erpnext-lab/versions.lock.json)。原环境报告是前一任务的日期快照，本报告补充实际运行结果。

## 2. Git 前置条件和任务基线

- 最初查询时 PR #1 尚为 OPEN，因此按要求停止。用户要求重试后，GitHub 返回 MERGED，合并时间为 **2026-11-17 15:36:27 +08:00**。
- 合并提交：`620429bb6ebc7cd1bdeba9247d9172e87cd73762`。
- 确认原工作区干净后 fetch 最新 `origin/main`，从上述提交创建 **`chore/erpnext-lab-bootstrap`**。未在 main 修改文件。
- origin 保持 `https://github.com/LeonEleven/HaxpowerPlatform.git`，不替换远端。按 AGENTS.md 完成文件检查后提交、普通推送及 Draft PR；不强制推送或自动合并。

## 3. 实际组件和版本

| 组件 | 实际值 | 状态 |
| --- | --- | --- |
| 宿主 | Windows 11 + Ubuntu 24.04 / WSL 2，x86_64 | 复用既有 Docker Desktop 集成 |
| Docker Engine | 29.9.3 | 可用 |
| Docker Compose | 5.7.0 | 配置解析、up --wait 和检查通过 |
| Python（开发容器） | **3.14.10** | 满足目标 Frappe 约束 |
| Node | **24.22.3** | 通过 |
| Yarn | **1.22.22** | 通过 |
| npm | **11.19.1** | 通过 |
| pip | **26.2.2** | 保留镜像内版本，未随更新提示升级 |
| Bench CLI | **5.31.1** | 镜像预装，尚无 Bench 工作区 |
| Bench 源码 Commit | `660bc2898500dc33828056c1228e5a1d538573b1` | 开发镜像内 `.bench` 只读查询 |
| MariaDB | **11.8.9-MariaDB-ubu2404** | 数据库连接、认证和健康检查通过 |
| Redis cache / queue | **7.4.12** / **7.4.12** | 两者连接和健康检查通过 |
| Frappe | 目标 **v16.55.0**，尚未安装 | tag 和依赖已复核 |
| ERPNext | 目标 **v16.55.0**，尚未安装 | tag 和依赖已复核 |

容器用户实测为 UID 1000，与宿主工作区一致。开发镜像内部操作系统与 Ubuntu 宿主分开，不能据 MariaDB 镜像版本推断所有容器都使用宿主发行版。

### 3.1 上游和应用固定提交

| 仓库 | 固定 Commit | 用途 |
| --- | --- | --- |
| frappe/frappe_docker | `0ad1a1152f93b1285104bcd0a8059e036dc5bd3d` | 独立实验目录中的官方审查副本，保持干净 |
| frappe/frappe v16.55.0 | `90c8b61afd60ee49f982ca1a9751008e4ae1e24c` | 下一阶段 Bench 目标 |
| frappe/erpnext v16.55.0 | `8e10a348daaa93853de8c36dc55ec789a440c219` | 下一阶段 ERPNext 目标 |

安装前重新通过 Git 查询 tag，并读取对应 Commit 的依赖文件。Frappe 要求 Python `>=3.14,<3.15`；ERPNext 要求 Python `>=3.14`、Frappe `>=16.55.0,<17.0.0`。两者声明兼容，但不代替实际安装及业务验证。[Frappe 依赖](https://github.com/frappe/frappe/blob/90c8b61afd60ee49f982ca1a9751008e4ae1e24c/pyproject.toml)、[ERPNext 依赖](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/pyproject.toml)。

上游 Compose、Dev Container、Bench Dockerfile、开发指南的 SHA-256 已保存在版本锁定文件，便于核对模板来源。

### 3.2 下载镜像和固定 Digest

全部为官方镜像，实际平台 linux/amd64。先下载标签，再读取并锁定 Digest；运行时 Compose 引用 Digest，不使用浮动标签。

| 初始获取标签 | 固定运行引用 |
| --- | --- |
| `frappe/bench:latest` | `frappe/bench@sha256:258c5e0d91ebaf83cf314ac6573b1a04a5ce40353a6092bae86550d4299c0782` |
| `mariadb:11.8` | `mariadb@sha256:d36f18d65a0459510b8b0f643b226a6d299e3206486e2b64ff20754a6d291707` |
| `redis:7.4-alpine` | `redis@sha256:7f7d30b7fdd09467bac58198a1f166dd69789d348d4fe859cccb20e0cf287fbe` |

Redis 选用满足官方 6+ 要求的 7.4 系列，代替模板的浮动 `redis:alpine`；这是基础环境锁定选择，不改变业务模型。未使用 `pwd.yml`，也未用生产 Compose 代替开发环境。

## 4. 配置安全审查

本方案基于 [固定上游开发 Compose](https://github.com/frappe/frappe_docker/blob/0ad1a1152f93b1285104bcd0a8059e036dc5bd3d/devcontainer-example/docker-compose.yml) 和 [Dev Container 配置](https://github.com/frappe/frappe_docker/blob/0ad1a1152f93b1285104bcd0a8059e036dc5bd3d/devcontainer-example/devcontainer.json)，对本实验进行了以下调整：

| 检查面 | 实际处理 |
| --- | --- |
| 启动命令 | Frappe 显式清空继承 entrypoint，命令只为 `sleep infinity`；保留官方 MariaDB/Redis 标准入口，仅初始化基础服务 |
| 自动执行 | 无 postCreate/postStart/其他 lifecycle 安装命令，不运行上游 installer，也不自动装 VSCode 扩展 |
| SSH 与 Docker socket | 无宿主 SSH 密钥挂载，无 Docker socket 挂载，无 privileged/host 网络 |
| 开发目录 | 仅实验 `workspace/` 可写挂入开发容器；项目源码、上游副本、秘密目录均不挂入开发容器 |
| 数据库密码 | 本地随机生成，secrets 文件挂载，仅 MariaDB 长期读取；不写入 environment、Git 或参数 |
| 宿主秘密权限 | 实验根与 secrets 目录均0700；密码文件0444以适配 MariaDB 非 root 入口，宿主其他用户无法穿过上级目录读取 |
| 连接检查用密钥 | stdin 传入开发容器，临时0600客户端配置文件，使用后删除；未输出秘密 |
| 暴露端口 | 实测四容器 PortBindings 均为空，无主机监听发布 |
| 网络 | 实验专用 backend 内部网络承载四服务，只有 frappe 另接 egress；无 external 网络或其他项目成员 |
| 数据持久化 | 两个项目命名卷，不使用 external 卷；缓存为 tmpfs，不持久化；队列独立 AOF |
| 权限 | 开发用户 UID1000，丢弃 capabilities，启用 no-new-privileges；数据库 root 只用于隔离实验检查和后续建站 |
| Redis 信任边界 | 内网无密码、protected mode关闭；仅此实验内部使用，不得加入不可信容器或发布端口 |
| VSCode | localhost 转发8000/9000/6787，关闭自动发现其他端口，无数据库端口转发；关闭窗口会 stopCompose |
| 代理/DNS/认证 | 没有注入宿主 localhost 代理，没有修改 Docker Desktop、WSL、DNS、sysctl 或系统认证配置 |

Compose 本地文件型 secrets 通过 bind mount 提供，因此不依赖 Compose 的 uid/mode 字段自动改变宿主文件权限。密码安全边界及本地重建规则已写入 README；不能将本开发配置当生产部署模板。

## 5. 容器、网络和卷运行状态

最终运行检查：四个服务均 running/healthy，实际 Image ID 与锁定文件一致。

| 服务 | 容器名称 | 健康 | 发布端口 |
| --- | --- | --- | --- |
| frappe | `haxpower-erpnext-v16-lab-frappe-1` | healthy | 无 |
| mariadb | `haxpower-erpnext-v16-lab-mariadb-1` | healthy | 无 |
| redis-cache | `haxpower-erpnext-v16-lab-redis-cache-1` | healthy | 无 |
| redis-queue | `haxpower-erpnext-v16-lab-redis-queue-1` | healthy | 无 |

网络：`haxpower-erpnext-v16-lab_backend`（internal，四服务）、`haxpower-erpnext-v16-lab_egress`（仅开发容器）。命名卷：`haxpower-erpnext-v16-lab_mariadb-data`、`haxpower-erpnext-v16-lab_redis-queue-data`。

本轮使用同一 Dev Containers 配置对应的 Compose 进行无 GUI 启动，没有打开 VSCode GUI 或安装新扩展。因此基础开发服务已验证，VSCode 实际 Reopen/attach 仍待用户验证；操作方式见 README。

## 6. 数据连接与容器网络检查

所有以下检查从 Frappe 开发容器发起，没有以宿主连通性替代容器结果。

| 检查 | 实测结果 |
| --- | --- |
| MariaDB TCP `mariadb:3306` | root 使用正确随机密码连接成功，查询版本/设置及系统库成功 |
| 错误/空数据库密码 | 均返回 access denied (1045)，没有无密码放行 |
| 数据库字符集/排序规则 | `utf8mb4` / `utf8mb4_unicode_ci` |
| `innodb_snapshot_isolation` | 0，与官方开发模板一致 |
| 数据库会话 TLS | `TLS_AES_256_GCM_SHA384`，未主动关闭 SSL |
| Redis cache / queue `:6379` | 两者 PING 均为 PONG |
| GitHub HTTPS | `https://github.com` 返回200 |
| GitHub Git | 实际 `git ls-remote` 读取 Frappe v16.55.0，Commit 与锁定值一致 |
| PyPI HTTPS | 官方 `pypi.org/simple/frappe-bench/` 返回200 |
| Python 包索引命令 | `pip index versions frappe-bench` 成功，未安装或更新包 |
| npm HTTPS / CLI | npm registry返回200，`npm ping`成功 |
| Yarn | `yarn info frappe-ui version`成功（当时返回1.2.0），未安装包 |

容器配置与实际 environment 均未设置代理变量。成功结果说明当前 Docker 网络路径可访问这些源，不保证其他所有下载站点、未来时间或 ERPNext 完整依赖安装均成功。未使用跳过 TLS 校验、全局 DNS 修改或代理回退来通过测试。

## 7. 资源使用

启动前：Docker 可见12 CPU、约9.65 GiB内存；WSL available约7.8 GiB，swap3.0 GiB。WSL项目文件系统可用约952 GiB；Windows C盘可用约111 GiB，满足本轮镜像与基础服务需求。

基础服务稳定后的 `docker stats --no-stream` 样本（不是业务负载测试）：

| 服务 | 内存使用 / 上限 | CPU样本 |
| --- | --- | --- |
| frappe | 25.32 MiB / 4 GiB | 0.01% |
| mariadb | 93.82 MiB / 2 GiB | 0.04% |
| redis-cache | 4.96 MiB / 256 MiB | 0.51% |
| redis-queue | 4.25 MiB / 256 MiB | 0.54% |

合计约128 MiB，内存上限合计6.5 GiB。镜像元数据解压尺寸约4.31 GB合计，实际层共享和 Docker 磁盘占用不同，不能等同为新增宿主磁盘用量。下载启动后 C盘可用约104 GiB，WSL available约7.6 GiB；内存 free 较低主要伴随缓存增加，判断使用 available，不据此误判内存耗尽。

下一阶段构建资产和运行完整 Bench 的资源占用尚未测量，不能据空闲服务样本证明实际业务性能。

## 8. 遇到的问题和处理

1. **PR #1 初次未合并**：停止任务；用户要求重试后确认已合并，再同步并创建任务分支。无自行合并或绕过前置条件。
2. **执行沙箱权限**：网络/Docker socket、Git元数据和实验目录写入按执行权限在沙箱外完成；没有为此修改 socket 权限、代理或系统认证。
3. **官方示例默认密码、SSH挂载和自动命令**：在项目自有脱敏模板中移除/替换，保持上游审查副本干净。
4. **首次运行检查过早**：检查开始时 frappe 仍处于 starting；`compose up --wait` 后成功转为 healthy。等待完成后复核全部健康，不删卷或重建服务来掩盖检查时序问题。
5. **MariaDB客户端密码提示**：最初用 MYSQL_PWD 环境读取时出现“passwordless login”相关客户端警告；改用 stdin和临时0600配置文件后消失，并增加错误/空密码拒绝测试。最终正确连接协商到 TLS。
6. **io_uring EPERM**：日志明确记录自动回退 libaio，最终使用 Linux native AIO，健康和连接通过。未更改 sysctl、seccomp、WSL内核或增加特权。
7. **memory.pressure不可写**：MariaDB入口报告该cgroup监测能力不可用；记录为环境限制，不影响本轮启动/连接，未改宿主cgroup配置。
8. **预期的Access denied日志**：错误/空密码测试各产生对应警告，是认证负例证据，不是建站或正确密码连接失败。

## 9. 复现检查与下一阶段

本轮检查包括：固定镜像/Commit和模板哈希、安全配置、实际容器与网络/卷归属、Python/Node/Yarn/Bench版本、数据库正反向认证、Redis连接、Git/包源网络、日志与资源；脚本输出脱敏证据至实验目录 `bootstrap-checks.json`，不提交本地秘密或运行数据。

最终完整运行检查于 **2026-11-17 15:54:27 +08:00** 开始，结果 `passed=true`、`failed_checks=[]`：四容器健康、两网络成员及两卷归属核对通过，15项命令检查全部通过。本地脱敏证据文件 SHA-256：`5bf819e5a07a7ffcbde398214373b750ce5f79abb327d2766686d546396e45e8`。表中资源数据取前一轮稳定样本，不要求瞬时值与最终样本完全一致。

配置生成器的新目录生成、幂等运行、密码保留、权限和拒绝覆盖不同内容检查均通过。7个新增文件的Python语法、JSON、本地Markdown链接、UTF-8、行尾空白、文件范围及敏感信息检查通过，且逐文件确认不含实际实验密码。忽略规则符合预期，现有项目文档和上游审查副本未被修改；提交前再核对暂存Diff。

**等待本次审查后**，下一阶段按 [README的完整命令](../dev/erpnext-lab/README.md) 执行：

1. 连接开发容器，以frappe用户在 `/workspace/development` 创建 `frappe-bench`，明确使用Python3.14.10和Frappe v16.55.0；核对其Commit。
2. 设置 `db_host=mariadb`、两个Redis服务地址及v16 Chromium路径；不套用旧版本教程的环境版本。
3. 通过 `bench get-app --branch v16.55.0` 获取ERPNext，核对Commit，不修改两个核心仓库。
4. 用 `bench new-site --db-type mariadb --db-host mariadb --mariadb-user-host-login-scope=% haxpower.localhost` 创建**开发测试站点**，交互输入本地秘密；安装ERPNext，检查应用版本后启动Bench。
5. 确认站点可用后，再以独立后续任务创建 `haxpower_platform` App及最小适配，按原验证计划测空批次、估值、部分接收、权限和库存完整性。

未完成事项：VSCode GUI attach、Bench/ERPNext实际安装与完整依赖锁定、业务用例、业务性能及生产部署均未验证。技术路线尚未最终确定；本轮仅完成可重建的基础服务验证，至此停止。
