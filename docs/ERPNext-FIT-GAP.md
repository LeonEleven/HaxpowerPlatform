# ERPNext / Frappe 适配性与原型验收方案

研究快照：2026-11-16。状态：待原型验证，不是最终选型结论。

## 1. 目标

验证 ERPNext 能否在不修改核心源码的情况下满足 HaxpowerPlatform
核心库存需求。若关键规则无法稳定实现，评估 Frappe-only 或独立自研。

## 2. 官方资料

- ERPNext Releases：https://github.com/frappe/erpnext/releases
- Batch：https://docs.frappe.io/erpnext/batch
- Serial and Batch
  Bundle：https://docs.frappe.io/erpnext/serial-and-batch-bundle
- Stock
  Reconciliation：https://docs.frappe.io/erpnext/stock-reconciliation
- Goods in
  Transit：https://docs.frappe.io/erpnext/how-to-manage-material-transfers-and-goods-in-transit
- Frappe REST
  API：https://docs.frappe.io/framework/user/en/guides/integration/rest_api
- Users and
  Permissions：https://docs.frappe.io/framework/user/en/basics/users-and-permissions
- Hooks：https://docs.frappe.io/framework/user/en/python-api/hooks
- Background
  Jobs：https://docs.frappe.io/framework/user/en/guides/app-development/running-background-jobs

本次查阅的官方发布记录可见 ERPNext v16.55.0 和 v15
系列更新。不要只因版本号较新就直接选型；需验证框架兼容、Docker
部署、自定义应用和升级策略。

## 3. 初步适配结论

| 需求                        | ERPNext 原生能力                              | 判断                          |
|-----------------------------|-----------------------------------------------|-------------------------------|
| 多仓库                      | 支持                                          | 高匹配                        |
| 仓库层级/库位               | Warehouse 树形结构                            | 高匹配，需设计映射            |
| 批次                        | Batch、Serial and Batch Bundle                | 高匹配，但无批次是关键风险    |
| 同一 SKU 有批次和无批次并存 | 批次 Item 的每笔库存交易要求批次              | 需特殊适配，尚未证明可行      |
| 移库在途                    | Material Transfer / Goods in Transit          | 高匹配                        |
| 部分接收                    | 需目标版本实测                                | 待验证                        |
| 禁止批次负库存              | v15 起批次/序列号 Item 不允许负库存           | 高匹配                        |
| 盘点/期初                   | Stock Reconciliation 支持批次                 | 高匹配，需测无批次映射        |
| 动态属性                    | Item Attribute/Variant 可用但模型未必完全匹配 | 需自定义规格层                |
| 多品牌 SKU                  | Item + Brand 可组合                           | 可实现，需自定义规格关联      |
| 角色和用户权限              | Frappe Role/User Permissions                  | 基础能力匹配，报表/API 要补测 |
| API/小程序                  | Frappe REST API                               | 高匹配                        |
| OCR/AI                      | 文件、后台任务、外部 API 可集成               | 高匹配                        |
| 只管理数量不考虑估值        | ERPNext 有库存估值机制                        | 需重点评估                    |

## 4. 批次为空：第一优先级风险

官方 Batch 文档说明：Item 启用批次后，每笔库存交易都必须指定批次。v15
引入 Serial and Batch Bundle 管理批次分配。

HaxpowerPlatform 领域模型要求 `batch_id = NULL` 表示真正的无批次库存。

### 候选适配：特殊系统批次

可在 ERPNext 适配层为每个 SKU 建立保留的系统 Batch，例如内部代码
`__NO_BATCH__`，用户界面显示为“无批次”。

必须验证： - 特殊 Batch 能否被标准入库、出库、移库、盘点完整使用。 -
Batch Bundle 是否支持该映射。 - 是否影响标准 Batch
报表、批次追溯和估值。 - 普通员工能否被阻止创建/编辑保留 Batch。 -
期初导入是否正确。 - 标准报表能否显示为“无批次”而非真实生产批次。 -
导出时能否还原为领域模型的 null batch。

不能仅凭创建特殊 Batch 就判定可行。如果需要修改 ERPNext
核心批次处理代码，应视为关键适配失败，优先评估 Frappe-only
或独立库存引擎。

## 5. 库存估值风险

ERPNext 库存模块包含估值和金额相关逻辑。HaxpowerPlatform V1
只想管理数量。

原型必须验证： - 新建 Item 和 Warehouse 是否需要估值参数。 -
入库/移库/出库是否必须提供估值率或金额。 -
是否产生库存价值或会计分录。 -
是否可以不启用会计应用或不产生财务影响。 -
隐藏金额字段后，后台是否仍然进行估值计算。 -
未来启用成本核算是否有合理迁移路径。

若估值机制使数量管理复杂度明显增加，需重新比较 Frappe-only 和独立自研。

## 6. 产品模型适配

候选映射： - 产品分类 → Item Group 或自定义分类。 - 产品规格 → 自定义
Product Specification DocType。 - 属性模板 → 自定义 DocType。 - 品牌 →
Brand。 - 品牌库存 SKU → Item。 - 批次 → Batch（特殊无批次方案待验证）。

不要把 Item Variant 当成唯一产品模型；验证一个规格关联多个品牌 SKU
的自定义关系。

## 7. 权限适配

Frappe 支持多角色、DocType 权限、字段权限、User
Permissions。原型验证： - ALL 用户能查询所有仓库。 - SELECTED
用户只查询授权仓库。 - NONE 用户看不到库存。 - 新仓库自动对 ALL
可见，不自动授权给 SELECTED。 - Stock Balance、Stock
Ledger、批次报表、单据、导出和自定义 API 都执行相同规则。 -
移库检查源仓库和目标仓库权限。

## 8. 原型数据

至少建立： - 2 个产品规格。 - 3 个品牌 SKU。 - 2 个仓库。 - 每个仓库至少
2 个库位。 - 同一 SKU 同时有 STD61、STD62 和无批次库存。 -
至少一个同规格不同品牌 SKU。

## 9. 必须通过的测试

| 编号 | 测试                             | 通过标准                             |
|------|----------------------------------|--------------------------------------|
| E01  | 同一 SKU 有 STD61、STD62、无批次 | 三种库存并存且数量独立               |
| E02  | 入库批次为空                     | 映射无批次桶，不自动生成真实批次号   |
| E03  | 出库 STD61                       | 只扣 STD61                           |
| E04  | 出库无批次                       | 只扣无批次桶                         |
| E05  | 未选择批次出库                   | 拒绝提交                             |
| E06  | STD61 不足但 STD62 充足          | 不跨批次扣减                         |
| E07  | 移库发出                         | 源库存减少、在途增加                 |
| E08  | 移库部分接收                     | 目标只增加实收数量，剩余在途         |
| E09  | 接收完成                         | 批次维度保持不变                     |
| E10  | 批次盘点                         | 可分别调整真实批次和无批次           |
| E11  | 期初导入                         | 有批次/无批次均正确                  |
| E12  | 新增第四仓库                     | 无需修改代码                         |
| E13  | ALL/SELECTED/NONE                | 报表/API/导出均遵循权限              |
| E14  | 库存估值                         | 无不需要的财务影响，记录内部估值行为 |
| E15  | 重复提交                         | 不重复产生库存流水                   |
| E16  | 中文属性搜索                     | 多关键词检索正确                     |

E01-E08、E10-E14 必须通过，才建议继续采用 ERPNext 库存引擎。

## 10. 开发约束

- 使用独立 `haxpower_platform` Frappe App。
- 不直接修改 ERPNext 核心源码。
- 不直接写 ERPNext 库存余额、库存流水和批次底层表。
- 使用受支持的 DocType、Document API、Hooks 和库存业务接口。
- 公共接口使用 `/api/haxpower/v1`，不直接暴露 ERPNext 内部 API。
- 对选定版本升级建立回归测试。

## 11. 失败时的决策

- 仅界面不符合：保留 ERPNext，开发独立 UI。
- 产品档案不符合：自定义 Product Specification 和 SKU 关联层。
- 批次空值需要修改核心代码：停止 ERPNext 库存引擎改造，评估
  Frappe-only。
- 库存估值与数量管理冲突严重：评估 Frappe-only 或独立自研。
- Frappe 基础设施也不合适：采用独立技术栈。
