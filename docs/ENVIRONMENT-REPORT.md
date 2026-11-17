# Stage 0 — ERPNext 技术验证环境准备报告

检查日期：2026-11-17（Asia/Shanghai）。任务类型：环境检查、官方方案研究、原型验证规划。

## 1. 结论与执行边界

本地 Git、Docker Engine 和 Compose 基础检查通过；Ubuntu 在沙箱外经现有代理访问 GitHub 和公开 Git 仓库成功。具备继续准备隔离开发环境的基础，但尚未证明容器内部下载依赖、Dev Containers 启动或 ERPNext 业务适配可用。

建议第一轮采用 **Frappe v16.55.0 + ERPNext v16.55.0 + MariaDB 11.8 系列**作为可重复的验证基线。它是依赖声明相容的原型组合，不是最终生产选型，也不是“两个项目的最新版本”。依据见第 5 节。

本次仅新增 `.gitignore`、本报告及 [第一轮验证计划](ERPNext-VALIDATION-PLAN.md)。未安装 ERPNext/Frappe、拉取镜像、克隆上游仓库、启动容器、创建数据库或站点，未连接阿里云，未修改全局代理、DNS、Docker/Git 配置，未删除文件、容器或数据卷，未提交或推送 Git。

已阅读用户指定的 7 份文件，并补读产品、库存、权限、业务流程、API、数据库设计，以及 `manifest.json`。本次工作符合阶段 0 限制；不修改业务规则。官方批次约束与领域空批次语义存在已知适配差距，必须验证，不能通过改写现有需求解决。

## 2. 环境检查结果

项目路径：`/home/h-goal/projects/HaxpowerPlatform`，位于 WSL Linux 文件系统。

| 检查项 | 实测结果 | 结论/边界 |
| --- | --- | --- |
| Ubuntu | Ubuntu 24.04.5 LTS / Noble | 与用户提供的 24.04 一致 |
| 内核/架构 | `6.18.58.1-microsoft-standard-WSL2` / x86_64 | WSL 2 内核 |
| Windows | Windows 11（用户已确认） | 未独立查询 Windows 版本 |
| Git | `git version 2.43.0` | 可用 |
| Docker Desktop | `4.99.0 (245822)`，由服务端 Platform 返回 | 可访问 Desktop 后端 |
| Docker 客户端/服务端 | 均为 `29.9.3`，API `1.56`，linux/amd64 | 只读连接通过 |
| Docker Compose | `v5.7.0` | 版本命令通过 |
| Compose 配置解析 | 临时从 stdin 输入一个最小配置，执行 `docker compose -f - config --quiet`，退出码 0 | 未创建容器、网络或卷；不代表官方环境已运行通过 |
| VSCode 命令入口 | `code` 在 PATH 中 | 未启动 GUI；WSL/Dev Containers 扩展安装状态未独立验证 |
| 资源快照 | 12 个可用 CPU；内存约 9.6 GiB、available 7.8 GiB；swap 3.0 GiB | 不是 Docker 的固定资源配额 |
| 项目所在文件系统 | 可用约 952 GiB（`df -h .`） | 不代表 Windows 实体磁盘空间或 VHD 实际剩余空间 |
| Docker Hub 下载 | 用户此前已验证成功 | 本次未再次拉取镜像 |

最初沙箱内 `docker version` 报 Docker socket permission denied；在获准的沙箱外只读复核中取得完整服务端信息。无需据此执行 chmod socket、添加用户组或重装 Docker。

## 3. Git、文档与敏感文件

### 3.1 Git 状态

- 项目已初始化 Git，仓库根目录正是当前项目路径，因此未执行 `git init`。
- 分支状态为 `main...origin/main`；这是本地远端跟踪信息，未 fetch，不能保证远端当前同步状态。
- 最近提交：`619fee0 Initial commit`。
- 检查开始时，索引仅跟踪 `.gitattributes`；`AGENTS.md`、`README.md`、`docs/`、`manifest.json` 均未跟踪。报告生成不等于这些文档已有 Git 备份。
- `.gitattributes` 已有 `* text=auto`，本次保留原样。
- 未暂存、提交、推送或修改 remote。新增文件仍待人工审阅后纳入版本控制。

### 3.2 文档完整性

`manifest.json` 列出的 13 份文档全部存在；用户指定的 7 份文件也全部存在。其余设计文档包括 PRODUCT-MODEL、INVENTORY-MODEL、PERMISSIONS、BUSINESS-FLOWS、API-CONTRACTS、DATABASE-DESIGN。

原有 15 个项目文件（不含 `.git`）均已建立 SHA-256 快照用于完成后的完整性核对。本次不覆盖现有设计文档，也不修改 `manifest.json`；两份新报告通过本报告相互链接。

### 3.3 敏感文件检查与忽略规则

在检查开始时的 15 个项目文件中，进行了敏感文件名与文本模式检查，未发现 `.env`、私钥、数据库备份、站点配置、常见 GitHub/云访问密钥或明显密码赋值。检查未输出任何密钥值。此结果是当前工作区的有限静态扫描，不是所有密钥格式或 Git 全历史的安全审计；未读取用户目录凭据或生产数据。

原项目无 `.gitignore`，本次新增，覆盖 Python 虚拟环境/缓存、Node 依赖、Bench 运行目录与站点密钥、Docker 本地数据与覆盖配置、环境文件、常见凭据和备份、编辑器临时状态。保留可审阅的 VSCode 共享配置及 `.env.example` 等脱敏模板；不忽略业务源代码、设计文档、Dockerfile、基础 Compose 或 `.devcontainer` 配置。被允许跟踪的模板与 VSCode 文件仍不得包含秘密。

## 4. Ubuntu 网络与 WSL 代理

| 检查 | 结果 | 判断 |
| --- | --- | --- |
| 沙箱内 HTTPS HEAD / Git ls-remote | 均无法连接 `127.0.0.1:7890` | 当前执行沙箱不能使用该 localhost 代理 |
| 沙箱外 `curl --head --location … https://github.com` | 代理 CONNECT 成功，最终 HTTP/2 200 | Ubuntu 当前通过现有路径可访问 GitHub |
| 沙箱外 `git … ls-remote … frappe_docker.git HEAD` | 退出码 0 | Git HTTPS 公开仓库读取成功，无需认证 |
| 返回的上游 HEAD | `0ad1a1152f93b1285104bcd0a8059e036dc5bd3d` | 仅记录检查时远端引用，不代表已克隆或所有网页内容均固定到该提交 |
| 当前代理环境 | `HTTP_PROXY`、`HTTPS_PROXY` 指向 `http://127.0.0.1:7890`，未见 URL 用户名/密码 | 只读检查，不记录无关环境变量 |
| Git HTTP 代理配置项 | 本次匹配查询为 0 项 | 不代表不存在其他网络中间层 |

WSL 曾提示“localhost 代理无法镜像到 NAT 网络”，但本次沙箱外实测连通，不能据该历史提示认定当前 WSL 网络故障，也不能据成功测试认定已切换 mirrored 模式。本次未查询或改写 `.wslconfig`、`resolv.conf`、路由、代理或 DNS。

这里证明的是**当前代理路径**，未验证无代理直连。容器内部的 `127.0.0.1` 指向容器自身，不应把 WSL 的 localhost 代理值直接复制到容器。下一阶段需分别验证容器访问 GitHub、PyPI、npm/Yarn 及镜像仓库；如失败，先记录容器层错误，再提出具体方案，不自动修改全局网络设置。网页研究工具访问成功也不等于 Ubuntu 或容器联网成功，两者证据分开记录。

## 5. 推荐版本与依据

### 5.1 第一轮版本基线

| 组件 | 建议 | 依据与限制 |
| --- | --- | --- |
| ERPNext | 固定 `v16.55.0` tag，再记录实际 commit | 官方存在该发布版本 |
| Frappe | 固定 `v16.55.0` tag，再记录实际 commit | 满足该 ERPNext tag 明确声明的 `frappe >=16.55.0,<17.0.0` |
| Python | 容器内 3.14.x | 该 Frappe tag 要求 `>=3.14,<3.15`；ERPNext 要求 `>=3.14` |
| Node / Yarn | Node 24.x / Yarn 1.22+ | 当前官方 v16 安装要求；最终记录实际补丁版本 |
| MariaDB | 11.8 系列，搭建时锁定可用补丁与镜像 digest | 当前官方 v16 要求及 devcontainer 示例一致 |
| Redis | 6+，搭建时锁定实际 tag/digest | 官方要求；示例中的 `redis:alpine` 是可变标签 |
| Bench 开发镜像 | 官方 `frappe/bench`，审查并锁定 digest | 示例是 `latest`，不直接将其视作固定版本 |
| frappe_docker | 下一阶段获取后固定经审查的 commit | `main` 会变；本次未创建本地 checkout |

兼容依据是版本化源码的依赖约束，而非两个版本号恰巧相同：[ERPNext v16.55.0 pyproject](https://github.com/frappe/erpnext/blob/v16.55.0/pyproject.toml)、[Frappe v16.55.0 pyproject](https://github.com/frappe/frappe/blob/v16.55.0/pyproject.toml)。两者的发布存在性分别见 [ERPNext release](https://github.com/frappe/erpnext/releases/tag/v16.55.0) 和 [Frappe release](https://github.com/frappe/frappe/releases/tag/v16.55.0)。

运行时要求见 [Frappe 官方安装矩阵](https://docs.frappe.io/framework/user/en/installation)。当前 [Bench Dockerfile](https://github.com/frappe/frappe_docker/blob/main/images/bench/Dockerfile) 已包含 Python 3.14 和 Node 24 配置；镜像是否实际包含预期版本仍需启动后核验。Ubuntu 系统 Python 不需要因此升级。

这只是声明层面兼容，尚未实装、运行测试或审计这些版本的全部安全修复。开始安装前重新检查同一 v16 系列补丁和发布说明；如更新基线，应记录新的 tag、依赖范围与 commit，整轮用例使用同一组版本。不能把两个项目各自的 `/releases/latest` 自动拼接；本次该入口分别返回 ERPNext v16.55.0 和 Frappe v15.126.1。

不建议为了沿用旧教程而直接降到 v15。若 v16 环境确有不可解决的问题，可另立 Frappe v15 + ERPNext v15 的对照原型，并重新查依赖、锁定版本和复测；不得跨主版本混装。

## 6. 官方 Dev Containers 方案及项目隔离

### 6.1 官方路径

官方 [开发指南](https://github.com/frappe/frappe_docker/blob/main/docs/05-development/01-development.md) 推荐 VSCode Dev Containers：将 `devcontainer-example` 复制成 `.devcontainer`，将 `development/vscode-example` 复制成 `development/.vscode`，再在容器内管理 Bench。默认数据库是 MariaDB。文档中仍有 Node 10/14、Frappe 13/14 的历史示例，运行时选择以第 5 节版本约束为准。

[官方 Compose 模板](https://github.com/frappe/frappe_docker/blob/main/devcontainer-example/docker-compose.yml) 的服务为 `frappe`、`mariadb`、`redis-cache`、`redis-queue`；[Dev Container 配置](https://github.com/frappe/frappe_docker/blob/main/devcontainer-example/devcontainer.json) 指向 `frappe`、用户 `frappe`、工作目录 `/workspace/development`，转发 8000、9000、6787，关闭窗口时采用 `stopCompose`。

Windows VSCode → WSL 文件夹 → Reopen in Container 是 VSCode 官方支持的组合，见 [VSCode Dev Containers / WSL 2](https://code.visualstudio.com/docs/devcontainers/containers)。继续复用 Docker Desktop WSL 集成，不在 Ubuntu 内额外安装一套 Docker daemon；参见 [Docker WSL 后端说明](https://docs.docker.com/desktop/features/wsl/)。

不使用 `pwd.yml`：上游 [README](https://github.com/frappe/frappe_docker) 明确将它定位为可丢弃演示，且不支持该示例中的自定义应用安装。生产用 `compose.yaml` 也不代替本轮开发用 Dev Container。

### 6.2 隔离布局（方案，尚未创建）

建议后续在独立目录建立验证工作区，例如：

```text
/home/h-goal/projects/HaxpowerPlatform/       # 现有需求、设计和未来正式代码
/home/h-goal/labs/haxpower-erpnext-v16/
  frappe_docker/                            # 独立上游 checkout
    .devcontainer/                         # 本地审阅过的配置
    development/frappe-bench/
      apps/frappe/                         # 上游，保持干净
      apps/erpnext/                        # 上游，保持干净
      apps/haxpower_platform/               # 独立 App / 独立原型 Git 仓库
      sites/                               # 本地站点、上传文件与秘密，不提交
```

该示例目录位于当前可写项目范围之外，下一阶段实际创建前需满足工作区权限；本次未创建目录。App 原型代码应独立跟踪，不能因整个 Bench 被忽略而失去版本管理。验证通过后再决定正式项目中的 App 归属，不把整个 Bench、Frappe 或 ERPNext 源码复制进当前仓库。

使用独立 Compose project 名称（建议 `haxpower-erpnext-v16-lab`）、项目专用网络及命名卷；不使用共享 external 卷或固定全局卷名。MariaDB/Redis 不发布主机端口；Web 通过 VSCode localhost 转发使用 `haxpower.localhost:8000`（实际端口冲突时记录映射）。在途仓作为系统位置，领域库位可候选映射为 Warehouse 树叶节点，尚待验证。

### 6.3 启动前必须审阅的模板细节

- 模板使用固定示例数据库密码，Dev Container SQLTools 也保存示例连接配置。后续统一改为本地秘密注入，并避免在版本化 JSON 中保存密码，不提交含秘密的解析后 Compose 输出。
- Dev Container 默认包含宿主 `.ssh` bind mount 和 `postCreateCommand`。公开仓库读取不需要挂载整套 SSH 私钥；下一阶段启动前移除不需要的挂载，审阅自动执行命令。Reopen 本身会启动服务，**本次不执行**。
- 镜像 `latest`、`alpine`、数据库系列标签都可能漂移；后续记录 digest、实际版本及获取时间。不得把旧示例运行时要求与新版本依赖混搭。
- 不挂载宿主 Docker socket 到业务开发容器，不使用 privileged 或 host 网络；本轮无需可选 PostgreSQL、邮件或 Cypress 服务。
- `db_host` 使用 Compose 服务名 `mariadb`，Redis 使用 `redis-cache` / `redis-queue`，不使用容器 localhost 连接其他服务。

以上是 HaxpowerPlatform 的本地开发方案约束，不是已经执行的配置修改。自定义逻辑仅通过独立 App、Hooks、受支持的 Document/业务接口实现；公共 API 保留 Haxpower 契约，不直接暴露或写入 SLE、Bin、Batch Bundle 等底层库存对象。

## 7. 下一阶段具体执行顺序（等待后续任务授权）

1. 确认独立工作区、VSCode WSL/Dev Containers 扩展、Windows 实体磁盘空间和端口占用；复核网络及同主版本补丁。
2. 在独立目录 clone 官方 `frappe_docker`，记录 commit；审阅后复制两组示例配置，设置独立 Compose project、秘密注入、固定镜像与 localhost 转发，去除不需要的 SSH 挂载。先运行 `docker compose … config --quiet`，不打印秘密。
3. 配置审阅通过后才 Reopen in Container；这是**下一阶段首次允许启动容器的步骤**。核验 `python --version`、`node --version`、`yarn --version`、`bench --version`、MariaDB 版本及容器网络。网络失败先报告，不改全局配置。
4. 在容器的 `/workspace/development` 手动建立 Bench：`bench init --skip-redis-config-generation --frappe-branch v16.55.0 frappe-bench`，显式使用 Python 3.14；核对实际 checkout。若 Bench 对 tag 参数支持有变化，先核对当前 CLI，再调整，不静默改用 develop。
5. 设置 Bench 的数据库、Redis 服务地址；按当前指南设置 v16 Chromium 路径，避免直接照搬旧 Procfile 修改步骤。获取 `erpnext` 的 `v16.55.0`，记录两个 App 的 commit。
6. 仅在独立开发数据库创建 `haxpower.localhost` 站点并安装 ERPNext，密码交互输入或安全注入。该步骤会初始化数据库，本次未执行。
7. 用 `bench new-app haxpower_platform` 创建独立原型 App、安装到验证站点、开启开发模式；单独跟踪 App 源码，保留 Frappe/ERPNext 核心干净状态。仅编写验证所需最小适配与 API 草案。
8. 按 [验证计划](ERPNext-VALIDATION-PLAN.md) 建立虚构数据，先做空批次、估值和部分接收，再验证权限及库存完整性；每个用例记录原生与适配后的结果。
9. 产出测试证据、版本锁定记录、差距和路线建议；由用户决定继续 A、切换 B 或 C。关键用例失败时不以修改核心源码或放宽业务规则补救。

本次交付后即停止，不自动执行以上步骤。

## 8. 风险、待解决问题与验收状态

| 项目 | 当前状态 | 后续要求 |
| --- | --- | --- |
| 同 SKU 的空批次与真实批次 | 官方启用批次的 Item 每笔库存交易都要指定 Batch，与领域 null 有差距 | 特殊系统 Batch 只能放在适配层；完整链路验证失败则重新选型 |
| 两阶段移库/部分接收 | 官方描述在途流程，未证明选定版本多次部分接收完整满足需求 | 验证 10→6→4，超收、重复接收、批次保留与单据关联 |
| 数量管理/库存估值 | ERPNext 库存与估值、公司/会计配置有关 | 区分无 GL 与无估值，不以隐藏金额字段作为通过 |
| 仓库数据权限 | 原生角色/User Permissions 不等于所有入口安全 | 查询、报表、导出、详情、写 API 及在途均验证 |
| Decimal、并发、幂等与不可变流水 | 尚未验证 | 核对实际列类型、精度、事务回滚、冲销与重算行为，不直接改底层记录 |
| 依赖/镜像固定 | 仅推荐版本，尚未拉取或记录 digest | 安装前补齐实际版本，复核同系列修复 |
| 容器网络和 Dev Container | 未实测 | 宿主 localhost 代理不能直接照搬；扩展和容器内网络待测 |
| 文档版本管理 | 原设计文档尚未跟踪 | 后续审阅后单独提交；本次不代替用户执行提交 |
| 业务未决项 | 批次大小写、同仓库跨库位两阶段规则、盘点冻结等仍未确认 | 不在本报告裁决；首轮盘点使用隔离用例期间无其他交易的数据，不能据此确定生产冻结策略 |

批次约束依据：[官方 Batch 文档](https://docs.frappe.io/erpnext/batch)。在途流程依据：[官方 Goods in Transit 文档](https://docs.frappe.io/erpnext/how-to-manage-material-transfers-and-goods-in-transit)。这些是研究证据，不是业务测试通过证据。

**本次通过的检查**：Git 可用、仓库存在、文档清单完整、有限敏感文件扫描、Ubuntu 沙箱外 GitHub HTTPS/Git 访问、Docker 服务端连接、Compose 版本与配置解析。完整性与 `.gitignore` 规则验证见交付检查结果。

**未执行的验证**：所有 ERPNext 库存/权限用例、Dev Container 启动、数据库初始化和真实依赖安装。因此阶段 0 的技术选型退出条件尚未满足。

## 9. 交付检查结果

- 原有 15 个文件的 SHA-256 与修改前快照一致，无删除或覆盖；新增文件恰为 `.gitignore`、本报告和验证计划。
- 使用 `git check-ignore --no-index --stdin` 检查 17 个应忽略路径与 17 个应保留路径，全部符合预期；未实际创建这些测试路径。
- 两份新文档的本地链接均有效，3 个新增文件无行尾空白；`git diff --check` 通过。由于新文件未跟踪，另用直接读取覆盖了其格式检查。
- Git 保持 `main...origin/main`，新增报告及原设计文档仍处于未跟踪状态；未执行暂存或提交。
- 本次环境检查结束，后续安装与业务验证保持未执行状态。
