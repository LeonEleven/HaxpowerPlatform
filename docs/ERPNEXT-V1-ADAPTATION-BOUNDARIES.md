# ERPNext V1 适配边界

适用于项目方已原则批准的 ERPNext v16 + 独立 HaxpowerPlatform Frappe App。网页、小程序或 AI 调用版本化业务 API，员工库存操作经 Haxpower 界面。正式实施仍须满足审批约束及架构验收门槛。
[架构决策记录](ARCHITECTURE-DECISION-RECORD.md)

## 原生能力与 App 职责

| 能力 | ERPNext / Frappe 原生 | Haxpower App |
| --- | --- | --- |
| 物料、库位与单位 | Item、UOM、Warehouse 及受支持的主数据接口 | 产品分类、规格、品牌 SKU、属性模板和审计，领域仓库/库位至原生叶仓映射。 |
| 批次和数量 | Batch、Serial and Batch Bundle、Stock Entry、Stock Reconciliation、有效 SLE、Bin | 明确批次选择、规范化、保留 ID 隔离，精确批次及十进制精度校验。尽早拒绝不足或错误映射。 |
| 移库 | Material Transfer、在途仓、原生发出与 End Transit 关联、累计接收 | 源/目标仓授权，固化目标、SKU 与批次，隔离普通在途出库，管理退回及关闭。已完成后下游消耗的补偿须人工审核。 |
| 盘点 | 原生 Stock Reconciliation 或经审查的正式调整单据 | 独立盘点任务、截止或冻结策略、旧任务拒绝和幂等。Stage 1F 完整事件版本的实测不能直接覆盖轻量版。轻量版若采用版本，应在受保护事务中基于原生数量历史身份或可靠的桶级序号重验往返变化，并定义回填规则。 |
| 纠错与操作记录 | 保留原单据、原行及库存分配，追加新受支持单据 | 操作者、业务时点、原因、原单/新单关联、请求唯一性及权限。受控普通操作使用新调整，关闭原生取消入口。 |
| 身份、审批与附件 | Frappe 用户、角色、会话、可配置 Workflow / Workflow Action、私有 File | 角色与仓库范围的治理、全局审批策略、提交记录、附件验证、访问与导出边界，自定义界面和日志。 |
| 查询、报表与期初 | 标准批次数量或受支持的库存接口可提供服务端数据 | 逐桶读取及可见性过滤，领域 DTO、列表、详情、报表、导出、期初导入与清洗。所有入口须执行服务端权限检查。 |
| 估值 | 引擎保留内部估值、重估和未来标准财务能力 | V1 界面只开放数量，验收内部估值不会阻断数量流程或产生不需的财务影响。正式库存价值、采购、销售另行评审。 |

[原生能力验证](STAGE1D-TRANSFER-VALIDATION-REPORT.md) · [权限验证](STAGE1C-PERMISSION-BOUNDARY-REPORT.md) · [轻量审计证据](STAGE2B-LIGHTWEIGHT-AUDIT-REPORT.md)

## 入口限制

- **员工入口。** 普通业务身份使用最少专用角色。限制 Desk 原生库存入口和 Stock User、Stock Manager、Item Manager 等权限叠加。相同规则应用于 REST v1/v2、`frappe.client`、Desk 提交审批、库位报表、Prepared Report、打印、导入/导出、附件和系统 Batch 详情。拒绝不依赖菜单或路由是否可见。
- **库存写入。** 受管 SKU 正式库存路径由业务服务发放权限并验证源仓、目标仓、在途归属、幂等和锁顺序。限制原生 Stock Entry、Stock Reconciliation、交付、采购和其它能触发 SLE 的业务 DocType、Batch、Bundle、Bin 及 SLE CRUD，以及批量取消、批量改名和导入。
- **维护入口。** 禁止日常账号触发原生取消/修订、账簿重建、公司交易清理及工单外直接库存 SQL。后台只开放经过审查的范围与方式。受控维护需独立授权、记录、备份和前后对账，覆盖已排队的 Job 与管理员/DBA。标准文档守卫无法约束任意服务器代码或恢复操作。
- **失败和上线。** 意外取消、原单/Bundle 丢失或对账不符应阻断受影响的业务范围，保留证据，人工修复后重验。生产隔离和恢复演练仍待执行。
[权限模型](PERMISSIONS.md) · [原生入口实测](STAGE1C-PERMISSION-BOUNDARY-REPORT.md) · [后台风险](STAGE1F-AUDIT-COUNT-REPORT.md)

## V1-05 原生入口矩阵

Frappe v16.55.0 的 `auth_hooks` 在 Cookie、API Key 和 OAuth 验证后应用专用站点
入口清单。HTTP 开放登录、受控业务读取、Desk 壳及清单内的原生主数据操作。
清单外 RPC、DocType 方法、网站视图与下载在分派之前统一拒绝。使用附加
Stock User、Stock Manager 或 Item Manager 角色的用户仍经过同一规则。

| 入口 | V1-05 规则与自动化检查 |
| --- | --- |
| REST v1/v2、`frappe.client`、Desk 表单及列表 | 受管 Item 和叶仓拒绝直接详情及写入，列表和计数排除受管记录。允许 Frappe 标准权限下未受管 Item、Warehouse、UOM、Item Group 和 Warehouse Type 的已审查操作。含受管引用的主数据详情拒绝。公司原生库存默认值、复杂查询、DocType 自定义方法、合并及动态链接查询保持关闭。 |
| Stock Entry、Stock Reconciliation、Purchase Receipt、Purchase Invoice、Delivery Note、Sales Invoice 等 | 原生库存及上下游单据的 HTTP 读写均关闭。已登记 ORM 守卫扫描单据及明细的 Item、Warehouse 和 Batch 链接，包括更新前的引用，并在提交、取消与生命周期操作时阻止受管引用。 |
| SLE、Bin、Batch、Serial No 和 Serial and Batch Bundle | HTTP 关闭。ORM 守卫识别受管 Item、库位、保留前缀、Batch 的 Item 归属和持久映射。V1-06 仅在绑定当前身份、SKU、映射和原生记录的内部范围内允许 Batch 创建。 |
| 取消、删除、改名、导入和批量动作 | 受管资源的 HTTP 生命周期操作及全部原生库存导入、批量操作关闭。Item、Warehouse 映射生命周期另有 ORM 守卫。 |
| Stock Balance、Stock Ledger、Prepared Report、打印、File、私有文件下载及导出 | HTTP 关闭。V1-15 接入按仓授权的报表与导出，V1-16 接入受控导入。公共静态文件目录仅承载公开页面资产，受限材料须按业务文件流程入库。 |
| 账簿重估、公司交易清理、排队任务 | 原生重估与交易清理 HTTP 入口关闭。迁移停止已登记的原生重估、账簿改名、批量重试、补货和序列号维护计划。Worker 拒绝已登记的计划任务及队列调用，维护 DocType 在存在受管映射时拒绝 ORM 操作。生产队列和站点运维权限由 B11 逐项复核。 |

合成验证命令及结果见 [V1 开发环境](../dev/v1/README.md#v1-05-权限验证)。校验结果覆盖
在隔离站点实测的入口，V1-06、V1-15、V1-16、V1-19 的业务入口由对应任务负责。
任意服务器代码、DBA 和恢复操作属于独立的信任边界。

## 批次输入规范

V1-06 的 `b03-candidate-1` 源于 Stage 2B。它只在明确标记的合成样本上开放写入。正式字符等价、长度、历史例外和归并签核均待 B03 审定。

| 输入 | 服务端处置 |
| --- | --- |
| `{"mode":"BATCH","batchNo":"STD61"}` | 真实批次。只去首尾 Unicode 空白并保留可以解释的原始文本和对照；有变化时在录入或导入预览中提示。内部空格、大小写、全半角及有内容的引号均保留。 |
| `{"mode":"NO_BATCH"}` | 明确无批次。对外返回 `batch=null`，内部只能由适配层解析成该 SKU 专用系统 Batch。禁止额外携带批次文本。 |
| 缺选择或缺 `mode` | 未选择，拒绝提交。读取和正式变动的请求不能用缺字段代表无批次。入库允许无批次，调用方显式选择。 |
| `BATCH` 内为空字符串、全空白或 JSON null | 拒绝作为真实批次。确认无批次时改传 `NO_BATCH`。 |
| 单个或多个空引号及引号之间纯空白 | 返回需确认的错误，覆盖常见 ASCII、弯引号、全角和角引号。人工确认无批次后改传 `NO_BATCH`，确有意义的历史编码进入例外复核。保存 `"STD61"` 等有内容引号的文本，拒绝自动剥除引号。 |
| 命名空间或控制字符异常 | 保留前缀区分为 `HP-NB-` 和 `HP-RB-`，同时保留其大小写和兼容字符变体。拒绝控制字符、不可打印字符和尖括号，允许 Unicode 空格分隔符。候选长度上限为 80 个 Unicode 码点，原始输入上限为 256 个码点。 |
| 确认或碰撞 | 只裁剪首尾空格分隔符，原文和候选值一起返回。有变化时须确认对应策略版本和本次预览。同一 SKU 的既有键若对应不同原文，拒绝复用或改写，留待 B03 复核。 |

适配层为每个稳定映射 ID 创建一个原生 Batch，填入固定原生 Item，查回复用时复核正反向归属、映射状态和原生禁用标志。受管 Batch 的 ORM 改绑、禁用、删除、改名和合并均拒绝。站点管理员可在 Bench 检查孤立 Batch、命名空间冲突及状态异常。员工 API、报表、导出、业务日志和普通错误仅返回领域信息。

首次建映射前检查 ERPNext 的 `enable_serial_and_batch_no_for_item` 和受管 Item。已有原生库存、SLE 历史、已知在用单据或序列批次引用的 Item 保持关闭。安全的 Item 以原生保存操作启用 `has_batch_no`，保持 `create_new_batch=0`，后续复用保持幂等。升级只增加结构。原生记账、Bundle 绑定和库存权限由库存内核与后续收发任务验收。

旧数据导入应先生成**异常批次清洗报告**。每行至少包含文件 / 工作表 / 行号、SKU、库位、原始批次及字符展示、原数量、候选值或拟用 `NO_BATCH`、异常类型、冲突组、审核人、处理决定及时间。区分真实源 NULL、空字符串、全空白、疑似空引号、去边后变化、保留前缀冲突、不可打印与超长。同一 SKU 的多种输入归并到同一候选值时标记冲突，先核对实物和余额再决定。一次审核通过的清单再进入幂等导入并逐桶对账。实验仅用虚构输入生成异常预览，完整 E11 文件导入未执行。

## V1-07 执行边界

执行内核接收领域 ID。原生适配器在受控执行身份中解析 Item、Warehouse 和 Batch，提供原生父单及明细稳定 ID。V1-07 的唯一执行适配器使用合成父子 DocType。Stock Entry、SLE、Bin 和 Bundle 的员工写入口仍受 V1-05 守卫保护。

正式适配前按锁定源码复核 ERPNext 的即时和后台路径。v16.55.0 的 `stock_ledger.py` 中重估进度保存会显式提交并发布实时消息，`repost_item_valuation.py` 的 `repost` 及失败尾部也会提交，后台入口会入队。现有原生重估守卫继续生效。适配层数据库边界拒绝嵌套提交、开启或结束事务、DDL、自动提交和事务回调。V1-08/V1-09 须额外验证原生 ORM、缓存、通知和后台副作用、失败回滚及当前读，再注册正式适配器。[重估进度源码](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/stock/stock_ledger.py#L508-L529) · [重估任务源码](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/stock/doctype/repost_item_valuation/repost_item_valuation.py#L630-L725)

[事务和后续接入](TECHNICAL-DESIGN.md#v1-07-库存执行内核)
[批次模型](INVENTORY-MODEL.md) · [实验规范实现](../dev/erpnext-lab/haxpower_platform/haxpower_platform/batch_input.py)

## V1-08 入库适配

原生适配向 ERPNext v16.55.0 的 `Stock Entry.insert()` 与 `submit()` 提交 Material Receipt。每行设置原生目标叶仓、库存 UOM、单位换算率 1、受管 Item 和精确批次。锁定版本在 `make_bundle_using_old_serial_batch_fields` 生成独立的入向 Bundle，并在 SLE 的提交路径中提交它。入库的 NO_BATCH 行使用 SKU 独立的系统 Batch。用户输出保留领域批次选择。

原有守卫仅在当前收货事务和已核准的原生对象上允许对应写入。普通员工原生 REST/Desk 和正式收货 HTTP 仍关闭。禁止正式 Stock Entry 或 Bundle 被普通路径取消、删除或改量。受控连接保留提前提交与任意回调的拒绝，兼容首次 Bin 创建的保存点及 Frappe 文档缓存失效机制。B01、B03、B12 仍需业务审定。

锁定版 `update_entries_after.set_precision` 在未配置原生精度时为 SLE 累计量使用两位小数。合成站设定三位前，实测两行的有效 SLE 为 1.125 和 0.125，但 Bin 被逐次舍入为 1.26。明确配置三位后，累计量、有效 SLE、Bundle、Batch 和 Bin 对齐。适配器在过账前拒绝缺少原生精度或原生精度低于 SKU 精度的站点。 [ERPNext 锁定源码](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/stock/stock_ledger.py#L765-L769)

ERPNext 内置 Bundle 创建调用当前员工的文档权限检查。仅当 Bundle 指向该事务中的 Stock Entry 和原生明细，且 Item、叶仓、批次与数量均匹配时，允许创建、保存和提交。首次建 Bin 只兼容原生 `SAVEPOINT create_bin`。缓存失效在连接关闭后重复一次，事务中的新文档不进入共享 Redis 缓存。提交、回滚、DDL、其他回调及即时队列或实时发送保持受限。

## V1-09 出库适配

Material Issue 通过 `Stock Entry.insert()` 和 `submit()` 过账。每行业务数量为正，库存 UOM 换算率为 1，显式填写 `s_warehouse`、`use_serial_batch_fields=1` 和已验证的 `batch_no`。锁定版的 `StockController.update_bundle_details` 将指定 Batch 传为单项批次集合，`SerialBatchCreation` 使用该集合生成 Outward Bundle 和负向子行。适配器核对明细、Bundle、SLE 的批次、归属、方向、数量及稳定引用。Stock Settings、Item 或目标 Batch 启用负库存时拒绝提交。[Stock Controller 源码](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/controllers/stock_controller.py#L817-L889) · [Bundle 源码](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/stock/serial_batch_bundle.py#L1370-L1412)

原生受控范围绑定当前员工、业务单、Stock Entry 及明细。已有的数量出处须能与已提交 Bundle、Batch 总量、Bin 和精度互相核对，才能出库。ERPNext 继续执行原生估值和关闭期间检查。允许财务候选配置的权限仅用于独立合成环境。普通员工原生 REST/Desk、取消、删除、维护和后台重估继续受原守卫约束。

出库估值使用 ERPNext 从源库存计算的原生费率。锁定版本的批次估值探针从 Detail 的估值率和 Bundle 子行的 `incoming_rate` 取费率，从 Bundle 子行及 SLE 取库存价值变动，并与凭证 GL 借贷核对。该路径的 SLE `outgoing_rate` 为零。
