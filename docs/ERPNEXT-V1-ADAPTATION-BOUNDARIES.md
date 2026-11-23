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
| SLE、Bin、Batch、Serial No 和 Serial and Batch Bundle | HTTP 关闭。ORM 守卫识别受管 Item、库位、Batch 保留前缀及关联受管 Item 的批次。V1-06 将接入正式批次映射及受控执行。 |
| 取消、删除、改名、导入和批量动作 | 受管资源的 HTTP 生命周期操作及全部原生库存导入、批量操作关闭。Item、Warehouse 映射生命周期另有 ORM 守卫。 |
| Stock Balance、Stock Ledger、Prepared Report、打印、File、私有文件下载及导出 | HTTP 关闭。V1-15 接入按仓授权的报表与导出，V1-16 接入受控导入。公共静态文件目录仅承载公开页面资产，受限材料须按业务文件流程入库。 |
| 账簿重估、公司交易清理、排队任务 | 原生重估与交易清理 HTTP 入口关闭。迁移停止已登记的原生重估、账簿改名、批量重试、补货和序列号维护计划。Worker 拒绝已登记的计划任务及队列调用，维护 DocType 在存在受管映射时拒绝 ORM 操作。生产队列和站点运维权限由 B11 逐项复核。 |

合成验证命令及结果见 [V1 开发环境](../dev/v1/README.md#v1-05-权限验证)。校验结果覆盖
在隔离站点实测的入口，V1-06、V1-15、V1-16、V1-19 的业务入口由对应任务负责。
任意服务器代码、DBA 和恢复操作属于独立的信任边界。

## 批次输入规范

这是供 V1 评审的规范。Stage 2B 实验按下表执行。批次大小写、上下游表格清洗策略及正式长度上限仍待确认。

| 输入 | 服务端处置 |
| --- | --- |
| `{"mode":"BATCH","batchNo":"STD61"}` | 真实批次。只去首尾 Unicode 空白并保留可以解释的原始文本和对照；有变化时在录入或导入预览中提示。内部空格、大小写、全半角及有内容的引号均保留。 |
| `{"mode":"NO_BATCH"}` | 明确无批次。对外返回 `batch=null`，内部只能由适配层解析成该 SKU 专用系统 Batch。禁止额外携带批次文本。 |
| 缺选择或缺 `mode` | 未选择，拒绝提交。读取和正式变动的请求不能用缺字段代表无批次。入库允许无批次，调用方显式选择。 |
| `BATCH` 内为空字符串、全空白或 JSON null | 拒绝作为真实批次。确认无批次时改传 `NO_BATCH`。 |
| 单个或多个空引号及引号之间纯空白 | 返回需确认的错误，覆盖常见 ASCII、弯引号、全角和角引号。人工确认无批次后改传 `NO_BATCH`，确有意义的历史编码进入例外复核。保存 `"STD61"` 等有内容引号的文本，拒绝自动剥除引号。 |
| 命名空间或控制字符异常 | 任何大小写的 `HP-NB-` / `HP-RB-` 都属于适配层，不能通过业务输入绕入原生表。不可打印字符、潜在标记文本或超过实验长度 80 的输入明确拒绝，禁止截断或替换。 |

适配层用 SKU 的稳定标识及批次种类计算或维护映射，复用前核验归属、真实文本、启用状态及唯一性。用户选择无批次需唯一匹配系统桶；实批次须对应实体映射。API、报表、导出、日志与错误响应仅返回域批次或 `null`。若真实批次名称可能重复或被更改，应以不可变批次标识管理，从验收覆盖映射迁移及回滚。

旧数据导入应先生成**异常批次清洗报告**。每行至少包含文件 / 工作表 / 行号、SKU、库位、原始批次及字符展示、原数量、候选值或拟用 `NO_BATCH`、异常类型、冲突组、审核人、处理决定及时间。区分真实源 NULL、空字符串、全空白、疑似空引号、去边后变化、保留前缀冲突、不可打印与超长。同一 SKU 的多种输入归并到同一候选值时标记冲突，先核对实物和余额再决定。一次审核通过的清单再进入幂等导入并逐桶对账。实验仅用虚构输入生成异常预览，完整 E11 文件导入未执行。
[批次模型](INVENTORY-MODEL.md) · [实验规范实现](../dev/erpnext-lab/haxpower_platform/haxpower_platform/batch_input.py)
