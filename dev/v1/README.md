# HaxpowerPlatform V1 开发环境

正式发行包、Frappe App 和 Python import 均为 `haxpower_v1`，界面名为 HaxpowerPlatform。源代码位于 [apps/haxpower_v1](../../apps/haxpower_v1)。这个环境支持 V1-01 工程骨架、合成迁移演练及 V1-02 规格服务测试。它可与 ERPNext 技术实验及 Frappe-only Spike 并行使用。

## 启动条件与版本

使用 linux/amd64 Docker Engine 和带 `compose config --format json`、`up --wait`、`gw_priority` 支持的 Docker Compose。宿主需要 Python 3.11+、Git 和 Bash。Windows 用户在启用 Docker Desktop WSL 集成的 Ubuntu 终端运行，仓库宜放在 WSL 文件系统。脚本仅使用 Docker `default` context 的本机 Unix socket。容器需要能访问 GitHub、PyPI、容器和 Yarn 包仓库。

[版本锁](versions.lock.json)记录 Frappe 和 ERPNext 的官方 HTTPS 地址、tag、Git SHA、三种镜像 digest 和工具实际版本。Frappe 与 ERPNext 均为 v16.55.0，MariaDB 为 11.8.9，Redis 为 7.4.12。Bench 镜像包含 Python 3.14.10、Node 24.22.3、Yarn 1.22.22、Bench 5.31.1 和 uv 0.13.8。准备时核对远端 tag 和检出 SHA、上游工作区及 import 来源。上游包声明和 Yarn 锁随固定提交获取，Python 的传递依赖仍由包仓库按上游声明解析。

## 从全新 clone 建立开发站点

```bash
git clone https://github.com/LeonEleven/HaxpowerPlatform.git
cd HaxpowerPlatform
# 复现审查中的改动时，先切换到该 PR 的任务分支。

python3 -m unittest discover -s dev/v1 -v
python3 dev/v1/v1.py config
python3 dev/v1/v1.py preflight
python3 dev/v1/v1.py up
python3 dev/v1/v1.py prepare
python3 dev/v1/v1.py new-site
python3 dev/v1/v1.py install-erpnext
python3 dev/v1/v1.py install-app
python3 dev/v1/v1.py migrate
python3 dev/v1/v1.py migrate
python3 dev/v1/v1.py test
python3 dev/v1/v1.py check
python3 dev/v1/v1.py start
python3 dev/v1/v1.py smoke
```

`up` 启动数据库、两套缓存和管理用 Bench 容器。`prepare` 在项目卷内独立安装依赖、构建资源并从只读挂载复制正式 App 到 Bench。修改正式 App 后，先 `stop`，再依次执行 `up`、`prepare`、`migrate`、`test`、`start` 和 `smoke`。首次启动后访问 [开发站点](http://haxpower-v1-dev.localhost:8100)。`start` 启用站点调度器并启动 Web、Socket.IO、worker 和 scheduler。登录前在本地受控终端读取 `dev/v1/.runtime/haxpower-v1-dev/site-admin`，用 `Administrator` 和生成的密码登录。所有站点仅使用合成记录。

结束工作时停止 V1 项目，保留站点和卷。恢复时运行 `up`、`start` 和 `smoke`。

```bash
python3 dev/v1/v1.py stop
```

## 隔离核查

- 开发 project 为 `haxpower-v1-dev`，站点为 `haxpower-v1-dev.localhost`，数据库及其站点用户为 `hpv1_dev`。每个 CI job 从 run ID 与 attempt 派生 project、站点和用户。管理服务的 DB 路由固定为内部 `mariadb:3306`。
- `<project>_bench-data` 保存 Bench、虚拟环境、站点文件和日志。`<project>_db-data` 保存数据库，`<project>_queue-data` 保存专有 Redis queue。Redis cache 使用临时存储。数据库和 Redis 仅在项目内部网络，管理用 Bench 另接项目专用依赖下载网络。Web 与 Socket.IO 另接专用前端桥接网络，用于向宿主回环地址发布端口。
- 预检先核对渲染后的服务、挂载、镜像、DB/站点身份、项目资源标签和所属源码根，再检查回环地址和端口。运行态核查要求上游检出干净，正式 App 的 Python import 与 editable 安装来源均指向项目 Bench 内的正式副本。已有站点上检查实际数据库及账户、Hooks、模块和 DocType。
- 开发和实验的只读对照可运行以下命令。输出应显示不同的容器、网络、卷和挂载根。

```bash
for hp_project in haxpower-v1-dev haxpower-erpnext-v16-lab; do
  docker ps -a --filter "label=com.docker.compose.project=$hp_project" --format '{{.Names}} {{.Ports}}'
  docker network ls --filter "label=com.docker.compose.project=$hp_project" --format '{{.Name}}'
  docker volume ls --filter "label=com.docker.compose.project=$hp_project" --format '{{.Name}}'
done
python3 dev/v1/v1.py check
python3 dev/v1/v1.py smoke
```

Compose、预检和运行态站点校验均能拒绝其他身份。测试覆盖错误 project、错误站点、实验包挂载或符号链接、错误数据库用户/主机、外部网络和卷以及数据库端口发布。旧实验源码和运行资源保留在各自环境内。

## 合成升级和失败恢复

[V1 工程 CI](../../.github/workflows/v1-engineering.yml) 对面向 `main` 的 PR 从干净检出创建两个全新站点。第一个安装当前版本、运行 Frappe 测试和两次真实 `bench migrate`，比较样例表结构、记录及补丁日志。第二个安装保存在 [夹具目录](container/fixtures/app-0.1.0) 的合成前版本 0.1.0，保留一个带稳定 ID 的样例和公有、私有文件。建一致备份后启用故障补丁，验证 migrate 返回失败并定位补丁。此时新增列及此前成功的补丁已经提交，注入补丁尝试的数据修改未提交。

恢复时回到同一合成前版本，使用同一备份集还原数据库、站点配置和文件，逐项与故障前核对。再用当前版本重试迁移，核对样例 ID、文本、创建及修改时间和补丁记录，复跑迁移并校验第一个站点的样例状态。CI 日志记录数据库和版本身份、各阶段状态的 SHA-256、脱敏诊断和回环访问结果。

V1-02 在上述流程中自动运行 [规格层 Frappe 集成测试](../../apps/haxpower_v1/haxpower_v1/catalog/test_catalog.py)。恢复后的升级站由合成探针显式建立分类、模板、规格、停用选项及审计，用真实员工会话验证原生 HTTP 写入被拒绝且数据无副作用，再两次运行 migrate 并比较七张目录表的结构、记录和引用。App 的 Bench 副本与当前 checkout 按文件内容核对，检查通过后才运行集成及站点命令。测试记录由临时站点生命周期管理，安装和迁移钩子只建立结构及工程角色。

### 规格层内部维护

已认证的工程调用方通过 `haxpower_v1.catalog.service` 和 `catalog.dto` 创建或编辑配置及规格。使用 `save_category`、`save_attribute`、`save_option`、`save_template`，编辑时带原记录 ID。使用 `create_specification`、`edit_specification`、`get_specification` 和 `get_specification_audit` 维护规格。属性输入按属性 ID 映射到值序列，单选和多选的元素均使用选项 ID。编辑需传读到的 `expected_revision`。维护角色 `HP V1 Catalog Maintainer` 需管理员显式授予，B01 和 B02 确认后由项目组集成真实模板和业务授权。

[规格层数据与值语义](../../docs/PRODUCT-MODEL.md#12-v1-02-规格层实现) · [权限和事务](../../docs/TECHNICAL-DESIGN.md#v1-02-规格维护与事务)

### 本机复现 CI

本机也可用独立临时项目运行同一演练。只为新演练选用不同的正整数运行号，`ci-clean` 按项目及检出归属核查并删除这次演练的站点和卷。

```bash
(
  set -e
  export HP_V1_PROJECT=haxpower-v1-ci-2026112001-1 HP_V1_WEB_PORT=8101 HP_V1_SOCKET_PORT=9101
  trap 'python3 dev/v1/v1.py ci-clean' EXIT
  python3 dev/v1/v1.py preflight
  python3 dev/v1/v1.py up
  python3 dev/v1/v1.py prepare
  python3 dev/v1/v1.py integration
  python3 dev/v1/v1.py start
  python3 dev/v1/v1.py smoke
)
```

## 故障排查

| 现象 | 排查和恢复 |
| --- | --- |
| Docker socket 不可用 | 在同一终端运行 `docker --context default version`。在 WSL 检查 Docker Desktop 集成。记录实际失败，静态检查只记录为静态证据。 |
| 端口冲突或 Host 错误 | 首次启动前复制 [.env.example](.env.example) 为 `dev/v1/.env`。Web 选 8100–8199，Socket.IO 选 9100–9199。已有 V1 环境更换端口前，先用原配置停止，再执行 `up` 和 `prepare`。用 `curl --resolve haxpower-v1-dev.localhost:8100:127.0.0.1 http://haxpower-v1-dev.localhost:8100/api/method/ping` 检查 Host 路由，替换成配置的端口。 |
| 项目、卷或挂载归属报错 | 核对完整项目名和 checkout 路径。不同 checkout 若要并行试验，使用不同的临时演练项目。未知卷和实验资源保留原状。 |
| tag、digest 或准备失败 | 准备步骤会显示失败的官方依赖和锁定值。检查网络和磁盘后重试 `prepare`。不完整的上游检出先保留诊断，使用新演练项目复现。锁变更应随验证提交。 |
| migrate 返回失败 | 停止 Web、worker 和 scheduler，保留失败补丁、退出状态和已完成阶段。MariaDB 的 DDL 和已提交补丁可以在失败后保留。开发/上线恢复采用同一恢复点的数据库、匹配版本、站点配置和文件，先在隔离站复核再重试。 |
| 无法安全截图或分享日志 | 只分享脱敏阶段结果。状态目录保存生成的本地凭据，项目卷保存站点数据、备份和私有文件，均留在本机或短期 CI 作业内。 |

[Bench 建站](https://docs.frappe.io/framework/user/en/bench/reference/new-site) · [Bench 恢复](https://docs.frappe.io/framework/user/en/bench/reference/restore)
