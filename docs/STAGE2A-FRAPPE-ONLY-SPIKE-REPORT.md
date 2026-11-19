# Stage 2A Frappe-only 对照原型报告

2026-11-19，Asia/Shanghai。原型验证与技术选型评审。对照范围为数量库存的批次、事务、在途、盘点、纠错和权限。选型建议及未通过的验收见比较文档。
[技术路线比较](TECHNICAL-ROUTE-COMPARISON.md) · [待审查决策草案](TECHNICAL-DECISION-DRAFT.md)

## 1. 实验环境

PR #10 已合并，任务分支 `spike/frappe-only-comparison` 从同步后的 `origin/main`（`0f00e5acb4e19ee2578d2eb67f9d9a486a653fe3`）创建。启动原有四个容器，未替换镜像、容器或卷。内部网络未发布数据库或 Redis 端口，宿主约有 7.3 GiB 可用内存和 950 GiB 可用磁盘，预检 MariaDB 容器占 132 MiB / 2 GiB，Frappe 容器占 1.26 MiB / 4 GiB。MariaDB 账号具备创建新 schema 和独立用户的权限，新站路径、schema 和账户名在创建前均不存在。

| 项目 | 环境 |
| --- | --- |
| Bench | 现有 `/workspace/development/frappe-bench` |
| Frappe | 16.55.0，`90c8b61afd60ee49f982ca1a9751008e4ae1e24c` |
| 原 ERPNext | 16.55.0，`8e10a348daaa93853de8c36dc55ec789a440c219`，只用于旧站 |
| Python、MariaDB | 3.14.10、11.8.9，实测 `REPEATABLE-READ` |
| Stage 2A | `haxpower-frappe-only.localhost`，独立 schema 和数据库账号 `haxpower_frappe_only_lab` |
| 新站已安装 | `frappe`、`haxpower_stock_spike`，scheduler disabled |
| 请求入口 | Frappe WSGI、真实令牌认证和请求事务，每个请求使用独立应用上下文 |

新 schema 的授权仅授予该站数据库账号。在首次脚本缺少 Frappe 新站初始化标志时，停止并确认新建 schema 为空，再续建站点。仅为该新账号更新初始化凭据。凭据通过本地内部管道传入，测试身份只使用随后撤销的临时 API 密钥。独立 App 使用离线缓存安装，现有 Frappe/ERPNext 版本和原站点的已安装 App 未改动。
[原实验环境说明](../dev/erpnext-lab/README.md) · [Frappe 新建站点](https://docs.frappe.io/framework/user/en/bench/reference/new-site)

## 2. 数据模型与实现

| DocType | 数据职责 |
| --- | --- |
| HP Spike SKU / Warehouse / Location | SKU 及单位精度，仓库与库位独立，`in_transit` 标识系统位置。 |
| HP Spike Scope | 用户仓库范围 ALL / SELECTED / NONE，操作角色由 Frappe 账号单独提供。SELECTED 存试验仓库列表。 |
| HP Spike Balance | 每 SKU、库位、批次一行，Decimal 数量与桶版本。真实批次是 SKU 内文本标识，`batch_id` 可以是 SQL NULL。 |
| HP Spike Operation | 每个生效业务操作的单据和幂等记录，含类型、操作者、请求摘要、结果，接收带发出引用 `transfer_ref`。 |
| HP Spike Event | 追加式库存数量事件，含业务操作、SKU、库位、可空批次、Decimal 差额、桶版本、操作者、时间与反向关联。每次移库动作写扣减和增加两条事件。 |
| HP Spike Transfer | 发出单关联、源/目标、SKU、批次、发出量和累计接收量。 |
| HP Spike Count Task | 盘点范围、账面基线、创建版本、创建人、已应用单据引用。 |

- **数据库唯一性。** 余额有确定性的桶主键。MariaDB 另外生成二进制 `batch_slot`，NULL 使用独立标签，真实批次使用另一标签及其字节。索引 `(sku, location, batch_slot)` 唯一，独立于桶主键防止重复。生成值不属于业务批次，API 仍返回 JSON `null`。批次在本实验区分大小写并拒绝首尾空白，正式规则待审查。事件有 `(bucket, bucket_version)` 和 `reversal_of` 唯一索引。事件、余额和盘点数量列使用 `DECIMAL(21,9)`。入口解析十进制字符串，内部 DML 绑定精确值并用数据库 `CAST` 读取，避免框架的浮点转换。
- **事务和锁序。** 先以操作主键预留并锁定请求，随后获取接收的发出状态行或盘点任务行，再按确定性桶主键顺序创建或锁定全部余额行。锁内使用当前读。纠错读取不可修改的原事件，在锁定对应桶后再读取后续事件。每腿余额和版本更新与事件插入一起生效，请求结果及移库累计或盘点消费标记也在同一事务。服务没有独立提交，由 Frappe 成功 POST 提交，异常触发回滚。
- **权限与边界。** 业务入口先检查角色和服务端仓库授权，移库检查双方，普通接口拒绝在途位置。实验库存 DocType 无普通 REST 权限，文档写入守卫也拒绝绕开服务的通用写操作。DBA、应用代码、直接数据库权限和恢复工具仍在受信任边界内。
- **扩展。** 事件 ID 可作为日后独立估值层的关联依据。本轮只写数量、批次和来源关系，估值、财务和成本层尚未实现。

[业务 API](../dev/frappe-stock-spike/haxpower_stock_spike/api.py) · [存储与约束](../dev/frappe-stock-spike/haxpower_stock_spike/store.py) · [Frappe 事务](https://docs.frappe.io/framework/user/en/api/database) · [MariaDB 生成列](https://mariadb.com/docs/server/reference/sql-statements/data-definition/create/generated-columns)

## 3. 实际执行结果

结果由下方验证器从独立连接核对。只有实际运行的用例列为通过。
[集成验证器](../dev/frappe-stock-spike/verify_spike.py)

| 用例 | 实测和结果 |
| --- | --- |
| A1 分批入出库 | **通过。** 同一 SKU 的 STD61、STD62、NULL 入库到 12/20/15，分别精确出 5/2/4 后为 7/18/11。无批次由两笔 9+6 入库形成同一余额行，API 返回 JSON `null`，直接 SQL 核实只有一行 `batch_id IS NULL`。 |
| A2 限量与数据库约束 | **通过。** 缺选择、指定桶库存不足、负数及小数精度超限均拒绝，邻近桶不补足。用不同物理主键直接插入相同 NULL 维度，唯一索引报 1062。直接向测试余额插入负值，约束报 4025。两次探针均在保存点内回滚。1.125 入、0.125 出，存储余额为 1.000000000。 |
| B 两阶段移库、共享在途和幂等 | **通过。** STD61 与 NULL 各组的源 / 在途 / 目标均为 12/0/0 → 2/10/0 → 2/4/6 → 2/0/10。两次接收持久化引用同一发出记录，部分接收重放无变化。各组最后 4 由两条独立请求竞争，一笔成功、一笔返回 `TRANSFER_COMPLETED`。另有两张发出各 10、8，共享物理在途 12 时，已接 6 的第一张再接 5 仍被拒绝。 |
| C1 两笔并发扣减 | **通过。** STD61 与 NULL 各从 10 并发扣 7，且仅一笔成功，终值均为 3。STD61 使用连接 551/550，NULL 使用 555/554。输家为 417 `INSUFFICIENT_STOCK`。 |
| C2 同键并发重放和不同负载 | **通过。** 连接 559/558 提交同一扣 7 请求，返回同一单据和事件；一笔为原提交，另一笔为重放，终值 3。用同键改数量返回 417 `IDEMPOTENCY_CONFLICT`，所有库存表无变动。 |
| C3 多行第二行失败 | **通过。** 第一行扣 3 后，在失败连接内看到余额由 10 变 7 及事件 `f234fc07e14b…`。第二行只能扣到 2，要求扣 3 返回 417。新连接核对两桶为 10/2，单据、事件、余额、在途及任务的快照摘要一致。 |
| C4 写审计事件失败 | **通过。** 连接 572 已更新余额并预留请求记录，进入审计插入时抛出异常。HTTP 500 后新连接确认所有库存表摘要与请求前相同。用原键重试完成一次正式扣减，再重放未变。 |
| D 盘点版本与盘盈 | **通过。** 入库使真批次基线 10 过期，出库使 NULL 基线 10 过期。NULL 8→6→8 时版本从 2 提升至 4，旧任务返回 `COUNT_STALE`。基线相同的两张任务并发提交，仅一笔盘至 6，另一笔过期。换键重复提交成功任务被拒绝，同键重放不改数据。无差异任务保留完成单据且不动余额版本。零库存 NULL 盘到 3，STD61 盘到 2，均形成 COUNT 事件。 |
| E 增量反向纠错 | **通过。** 入库 0→12→0，出库 12→8→12。原事件前后逐字段一致，反向事件保留引用。另组入 10 后下游出库 7，纠错返回 `DOWNSTREAM_STOCK_REVIEW_REQUIRED`。补入后总额达 13，仍然阻塞，避免只凭同桶余量认定原物料未被使用。 |
| F 非管理员角色与仓库范围 | **通过。** Operator 在授权库位读写，Query 读授权库位但写入 403。叠加角色不扩大仓库范围，NONE 及无操作角色拒绝。新增 W4 后 ALL 可直接入库与读取，SELECTED 仍为 403。普通 API 拒绝把在途当作可用位置。Operator、Query、无库存角色在余额和事件通用 REST 做 24 个读、建、改、删探针，均 403。原型也从 Administrator 的通用 Document 写入路径拒绝事件编辑。 |

并发通过两个真实数据库事务、屏障和锁内 250 ms 插桩确认重叠。出库、同键、盘点及部分接收各组的竞争等待约 254–269 ms，含人工延时。该数据仅用于验证等待与提交次序。

第二轮 `S2A02` 在应用内核对及再次独立运行取证，两条数据库连接 707、709 的实质结果完全一致。59 条生效业务操作、59 条事件、27 个余额、4 条移库和 8 条盘点任务互相可重建和核对。按每桶事件版本重放，无负库存、无版本缺口，每条事件可定位到操作与操作者。无差异盘点和任务创建有已提交记录，可不产生数量事件。

## 4. 未通过、未验证及生产差距

- **阻塞。** 入库被后续消耗时需要业务审查，未定义通过退货、新入库、跨批次或补偿单据纠错的规则。部分移库退回和完成移库后的下游更正尚未实现；Stage 1F 只证实 ERPNext 适配的未收在途可退回，完成后下游消耗同样阻塞。
- **代码支持或设计条件仍待验证。** 多行成功及跨桶高竞争未实测；SELECTED 空列表的默认拒绝未加独立账号验证；未注入提交连接中断、提交确认丢失、数据库死锁与恢复重试。盘点冻结、多库位统一截止、补录和按在途统计尚未实现。
- **生产必须补齐。** E11 期初导入、E16 产品和属性搜索、版本化外部 API、完整 E13 权限（报表、导出、附件和后台）、审批、审计留存、恢复演练及跨版本迁移。项目还需为库存引擎建立监控、对账修复和维护责任。原型数据只有虚构 SKU、仓库和作业，缺少生产负载证据。

建议在 V1 保持数量优先并明确库存内核维护责任的条件下，将 Frappe-only 列为首选评审方案。ERPNext 原生库存、移库及估值的复用价值仍成立，尤其在近期扩展成本或标准 ERP 流程时。完整的比较、估算和业务决策项见相应文档。
[与 Stage 1B–1F 逐项比较](TECHNICAL-ROUTE-COMPARISON.md) · [技术决策草案](TECHNICAL-DECISION-DRAFT.md)

## 5. 回归、隔离与复现

- 以仅对新 schema 有授权的账号连接，只能看到该 schema 和 `information_schema`。读取两个旧站的 Stock Entry 分别返回数据库 1142，且新站没有 ERPNext Stock Entry 表。
- 两套旧库在新站创建前及最终取证后以只读事务逐表生成行数与摘要。旧主站有 760 张可读表，Stage 1B–1F 所在站有 763 张可读表，前后完全一致。后者包含 86 张 Stock Entry、125 条 SLE、31 条 Stage 1F 审计事件及 14 条盘点任务。两库的 `__global_search` 和 `tabError Log` 各有一处既有错误 145，表分别属于旧主站与 Stage 1B 站。仅记录相同的前后读取错误，未修复，无法核验这两表的行内容。45 份历史实验文件通过 SHA-256 校验。Frappe/ERPNext 两套工作区均干净，提交与预检相同。
- 仓库 Python 测试常规和优化模式各 17/17 通过。两轮新站集成验证通过，最后另用新连接重建余额。虚构账号的 API 密钥均已撤销。
- 复验仅针对现有新站。源码放入该 Bench 独立 App 路径并注册后，用站点专属 `bench --site haxpower-frappe-only.localhost install-app haxpower_stock_spike` 初装一次。验证器放在容器临时目录。在 Bench 的 `sites` 目录执行，使用全新虚构运行号避免覆盖旧证据。

```bash
spike_python=/workspace/development/frappe-bench/env/bin/python
spike_probe=/tmp/haxpower-stage2a-verify.py
"$spike_python" "$spike_probe" S2A03
"$spike_python" "$spike_probe" final S2A03
```

本轮脱敏证据保存在既有实验目录的 `stage2a-20261119/` 子目录。

| 证据 | SHA-256 |
| --- | --- |
| `S2A01.json` | `a8df0bda8ae906d1137918f5d949f0fe0b7526c89e5a67f7d428cb3065437ce6` |
| `S2A02.json` | `1ffaacc962b93e0ff0025e2074b9b5c724ecd8ea67ecf1e5021f140299440ae1` |
| `final.json` | `a9d4ce6df6be27b32f62139209a8368fc035dc0da5c153abe787e6971a3e719f` |
| `database-isolation.json` | `73da97648ed8d90355c01e0edda008e7edaee405b1475e366ce31bb308d472f8` |
| `old-sites-before.json` / `old-sites-after.json` | `29141ab9561a2c92995118b2113453a50c6c548412530f4720e9cb04035bf89c` |
| `historical-files-verified.txt` | `0df5599a3d1b35c2dfa4e772f5c5498be76b3adda89fdfd103fef4a2ecb65335` |
