# HaxpowerPlatform V1 开发环境

正式发行包、Frappe App 和 Python import 均为 `haxpower_v1`，界面名为 HaxpowerPlatform。源代码位于 [apps/haxpower_v1](../../apps/haxpower_v1)。环境覆盖 V1-01 至 V1-08，并使用独立资源与两类技术实验隔离。

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

升级站恢复后，从已合并的 PR #21 提交 `841e3df4f046a44f63eb4787a1b8cee3c1a3d491` 打包
V1-08 App 0.9.0。在旧版本建立合成规格和审计、SKU、Item、仓库库位、员工范围、Batch 映射、
幂等记录、原生关联与真实 Material Receipt。先模拟升级失败，再从同一备份点恢复数据库、配置及
公私文件，重新升级并比较历史快照。空站、升级和恢复流程验证既有库存行为，并对真实出库及迁移重跑留证。
两个站点分别使用独立数据库和文件。CI 退出时仅清理该次运行所属资源。

### 规格层内部维护

已认证的工程调用方通过 `haxpower_v1.catalog.service` 和 `catalog.dto` 创建或编辑配置及规格。使用 `save_category`、`save_attribute`、`save_option`、`save_template`，编辑时带原记录 ID。使用 `create_specification`、`edit_specification`、`get_specification` 和 `get_specification_audit` 维护规格。属性输入按属性 ID 映射到值序列，单选和多选的元素均使用选项 ID。编辑需传读到的 `expected_revision`。维护角色 `HP V1 Catalog Maintainer` 需管理员显式授予，B01 和 B02 确认后由项目组集成真实模板和业务授权。

[规格层数据与值语义](../../docs/PRODUCT-MODEL.md#12-v1-02-规格层实现) · [权限和事务](../../docs/TECHNICAL-DESIGN.md#v1-02-规格维护与事务)

### 品牌与 SKU 内部维护

服务在 `haxpower_v1.catalog.sku`，输入和输出定义在 `catalog.sku_dto`。内部调用方使用已有的目录维护会话。使用 `save_brand`、`get_brand`、`create_sku`、`edit_sku`、`get_sku` 和 `search_skus`。编辑传入读取时的 `expected_revision`，省略别名或标识保留已有值，传空列表清空对应子表。显式关联原生 Item 时以 `existing_item` 提供其记录名称，后续业务引用使用返回的稳定 SKU ID。站点 Administrator 可用 `diagnose_item_mapping` 检查原生记录。

站点管理员在 B01 审定后配置 `hp_v1_sku_settings`，包含现有叶子 `item_group` 和 `uom_precision`（ERPNext UOM 名称到整数精度的映射）。创建所用 UOM 必须启用，整数单位精度为 0，其他单位的精度须在原生站点允许的范围内。缺少配置时拒绝 SKU 创建。演练用站点临时配置只引用显式创建的 `SYNTHETIC-*` UOM 和组。正式库存记账以及数量精度的端到端验证由后续任务实施。

[SKU 模型](../../docs/PRODUCT-MODEL.md#13-v1-03-品牌库存-sku-和-item-映射) · [映射、权限和事务](../../docs/TECHNICAL-DESIGN.md#v1-03-sku-与原生主数据)

### 仓库与库位内部维护

批准的工程调用方在当前用户会话中使用 `haxpower_v1.warehouse.service`。管理员显式授予 `HP V1 Warehouse Maintainer`。创建仓库时传对应 ERPNext Company、已启用的顶层同公司分组仓名称、仓库编码和显示名称。用返回的 `warehouse.id` 创建库位，显式关联时传 `existing_native` 并先确认其为符合条件的空闲物理叶仓。使用 `list_warehouses`、`list_locations` 和按 ID 读取的 DTO 维护目录。物理选择通过 `resolve_location` 复核状态与映射。显示名称和描述编辑需带原记录的 `expected_revision`。

ERPNext 设置向导安装标准 Warehouse Type `Transit`。未运行向导的空站需先准备该原生类型。站点 Administrator 通过内部 `provision_system_transit` 原子创建系统仓库和库位，使用 `diagnose_mapping` 或 `diagnose_native_mapping` 复核映射。停用及改名保护、受控原生权限和后续接入见技术说明。B02 的编码和角色使用合成数据验证，实际员工范围由 V1-05 接入。

[库存模型](../../docs/INVENTORY-MODEL.md#4-仓库与库位) · [权限、事务与诊断](../../docs/TECHNICAL-DESIGN.md#v1-04-仓库库位与原生叶仓)

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

### V1-05 权限验证

在独立一次性项目中执行 `preflight → up → prepare → integration → start → smoke → ci-clean`，
命令均为 `python3 dev/v1/v1.py <操作>`。集成入口使用锁定的 Frappe/ERPNext v16.55.0、
MariaDB 11.8.9 和 Redis 7.4.12。GitHub Actions 使用同一入口并保存脱敏日志。
以下记录 PR #18 的验证范围。当前集成继续运行权限矩阵和并发探针。

- 空站检查技术角色、权限表及零授权。合成升级固定在 V1-04 已合并提交
  `cc5dbd3ed3e62d6990dd06ab3ef021cfa3d2d582`，在加入权限表前保存合成规格审计、SKU、Item、
  W1–W4、物理叶仓和在途映射。验证失败后同备份点恢复、重试和重复迁移。
- Frappe 测试检查角色合并、仓库双侧授权、失权、映射异常、原生写入拒绝与上下文恢复。
  原有 SKU、库位与新增的员工授权配置并发探针使用独立 MariaDB 连接，第三个事务复核结果。
  Worker 测试经 Frappe 作业入口验证已停止计划及已登记队列调用的拒绝。
- 回环 HTTP 探针登录每个合成用户并用会话 Cookie 访问实际 Frappe 应用。
  矩阵在日志按入口打印状态码，验证前后领域、原生映射和库存快照。既有权限探针继续验证
  原生接口拒绝。实际测试范围见 [原生入口矩阵](../../docs/ERPNEXT-V1-ADAPTATION-BOUNDARIES.md#v1-05-原生入口矩阵)。

站点操作员在 Bench 内调用 `haxpower_v1.access.config.set_scope` 设置用户的公司、模式及
可选领域仓库 ID。站点运行角色的实际使用由 B02 审定。正式用户及仓库范围无安装默认值。

### V1-06 批次验证

沿用上面的独立项目复现命令和锁定版本。应用测试覆盖三态输入、Unicode 首尾校验和确认、
合成批次复用、大小写及全半角分离、排序规则和数据库约束、父 SKU 归属、原生失败回滚及
访问拒绝。两组批次并发各由两个独立 MariaDB 连接发起，一个提交新建，另一个重用已
提交的身份。第三个事务核对领域映射及原生 Batch 各一条。真实员工会话验证 REST v1/v2、
`frappe.client`、Desk 和批量入口拒绝，响应中无原生 Batch 名称。权限矩阵快照也包括持久批次。

测试在运行进程显式设置 `hp_v1_batch_policy=b03-candidate-1`，仅针对合成 SKU。首次启用
Item 批次需在隔离站预先启用 ERPNext 的批次设置，服务通过原生文档保存验证兼容性。
系统管理员可在 Bench 调用 `haxpower_v1.adapters.erpnext.batch.diagnose_mappings`。
候选策略的正式业务配置由 B03 审定，数量记账和 Batch/Bundle 兼容由后续库存任务验收。

[域服务与事务](../../docs/TECHNICAL-DESIGN.md#v1-06-批次映射和事务) · [B03 候选规则](../../docs/ERPNEXT-V1-ADAPTATION-BOUNDARIES.md#批次输入规范)

### V1-07 库存事务验证

沿用本机复现 CI 命令。合成执行入口为 `haxpower_v1.inventory.operations.synthetic`，仅当隔离站点、Bench 环境和进程内显式配置的 `hp_v1_stock_synthetic_actors` 同时匹配时运行。配置把虚构员工邮箱映射到合成 Company。执行按同一员工检查 Receive 能力和两侧仓库权限。测试探针使用独立连接，校验站点不依赖 Administrator 代替员工执行。

探针验证同键重放、异负载冲突、同单异键、相反行序、REPEATABLE READ 下等锁前建立的旧读视图、同桶精确合计、尚无映射的桶和跨批次隔离。并发由两个连接发起，第三个连接检查结果。逐一注入非法数量、原生明细、日志头及关联、状态与请求写入故障、真实 MariaDB 锁等待超时、提交前断连和提交后回包丢失。每个故障用独立连接核对持久化状态及 ERPNext Stock Entry、SLE、Bundle、Bin 和会计记录。

[执行内核](../../docs/TECHNICAL-DESIGN.md#v1-07-库存执行内核)

### V1-08 多行入库验证

`integration` 在全新站和从已合并 V1-07（`d48e97115bb80acfae8b29cbef17ff18d5eaba7a`）升级的独立合成站运行真实 Material Receipt。升级保留既有目录审计、主数据映射、授权、草稿和幂等操作历史。迁移及故障恢复快照涵盖已提交的 Stock Entry、Bundle、有效 SLE、Batch、Bin 和 GL，恢复使用同一备份点的数据库、站点配置和公私文件。

内部服务仅可在一次性 CI Bench 的显式进程上下文中进行合成入库，保留认证测试员工及其仓库范围。探针从新连接核对原生单据、同桶逐行关系、数量及失败后的持久状态。正式员工写入等待 B01、B03、B12。

| 隔离配置或故障 | 实测结果 |
| --- | --- |
| 公司关闭永续库存，零入库估值 | 六行混合入库保留六条明细、六个 Bundle 和六条有效 SLE。五个库存桶与 Bin 对齐，GL 行数为 0。 |
| 独立公司启用永续库存，库存资产账户、库存调整账户和成本中心齐全 | 2 单位，每单位 4.25。原生 SLE 入向估值 4.25，库存价值增加 8.50，两条 GL 分别借记和贷记 8.50。 |
| 禁止 GL 的候选策略 | 同一非零估值入库在原生 GL 生成后拒绝，Stock Entry、Bundle、SLE、Batch、Bin、GL、请求和日志一并回滚。允许 GL 的合成候选可安全重试。 |
| 关闭期间、历史冻结与估值重算 | ERPNext 的关账保存钩子与库存冻结路径分别拒绝。受控收货事务内重跑原生估值计算，SLE 数量字段、Bundle 桶、Batch 总量和 Bin 保持一致。 |
| 故障和并发 | 注入第二条明细、Bundle 提交、日志头、关联、请求更新、非法回调、提前提交、队列及实时发送故障。独立连接核对状态。模拟 COMMIT 前断连和 COMMIT 后响应丢失，并检查同键和异键双连接竞争。 |

空站合成夹具准备标准 Material Receipt 类型、有效会计年度、三位显式数量精度、启用批次的 Stock Settings、
受管叶仓、测试 Item Group 以及整数和小数两个 UOM。以上配置仅用于 B01、B03、B12 的隔离验收。
[可复现探针](container/receipt_probe.py) · [原生适配与精度行为](../../docs/ERPNEXT-V1-ADAPTATION-BOUNDARIES.md#v1-08-入库适配)


## V1-09 多行出库验证

使用上面的本机 CI 复现命令。集成作业会在锁定 ERPNext v16 上通过 V1-08 先提交真实入库，
再调用 V1-09 Bench DTO 提交真实出库。第三方 MariaDB 连接核对业务单、请求、日志、逐行原生关联、
Outward Bundle、有效 SLE、Batch、Bin 和 GL。探针验证同桶合计、不同 SKU 和库位、无批次、零余额、
精确小数、不同幂等键竞争、断连回包、财务期间与事务故障。测试用数量、批次策略和会计设置仅用于
本次合成站点。完整验证日志随 Draft PR 的 V1 engineering CI 上传。
