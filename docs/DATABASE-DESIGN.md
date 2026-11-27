# 数据库设计草案

本文件描述逻辑模型，不锁定数据库厂商。若采用 ERPNext，映射到 Frappe
DocType/ERPNext 标准对象；若独立自研，再按数据库生成 DDL。

## 1. 主数据表

### product_categories

`id PK, code UNIQUE, name, parent_id NULL, description, status, sort_order, created_at, updated_at`

### attribute_definitions

`id PK, code UNIQUE, name, data_type, unit NULL, description, status`

### attribute_options

`id PK, attribute_definition_id FK, value, label, status, sort_order`

### category_attributes

`id PK, category_id FK, attribute_definition_id FK, required, value_mode(SINGLE/MULTI), searchable, identity_relevant, display_order`

唯一约束：`category_id + attribute_definition_id`。

### product_specifications

`id PK, category_id FK, specification_code, name, description, status, created_by, created_at, updated_at`

### product_attribute_values

`id PK, specification_id FK, attribute_definition_id FK, option_id NULL, value_text NULL, value_decimal NULL, value_boolean NULL, value_date NULL, value_group_id NULL, display_value NULL, created_at, updated_at`

应用层必须校验值类型和 SINGLE/MULTI 规则。

### brands

`id PK, code UNIQUE, name, status`

### stock_items

`id PK, item_code UNIQUE, specification_id FK, brand_id FK NULL, item_name, item_description, stock_uom_id FK, quantity_precision, status, created_at, updated_at`

### product_identifiers

`id PK, stock_item_id FK, identifier_type, identifier_value, source, searchable, status`

### product_aliases

`id PK, stock_item_id FK, alias, language NULL, searchable, status`

## 2. 仓库与单位

### warehouses

逻辑模型由 `HP V1 Warehouse` 实现。Frappe 的 `name` 保存 32 位随机十六进制领域 ID。

`id PK, company, code, display_name, description, native_parent, kind(Physical/Transit), status, revision`

`(company, code)` 设数据库唯一键。编码格式用于候选工程，正式编码见 B02。`company` 链接 ERPNext Company，`native_parent` 链接同公司已启用的顶层分组仓。

### warehouse_locations

逻辑模型由 `HP V1 Warehouse Location` 实现，使用同样形式的独立领域 ID。

`id PK, warehouse Link (必填), code, display_name, description, status, native_warehouse UNIQUE, revision`

`(warehouse, code)` 设数据库联合唯一键，`native_warehouse` 设数据库唯一键。每条库位记录必须关联一个领域仓库，其公司与系统类型从该仓库继承。`Warehouse.custom_hp_v1_location_id` 保存受保护的反向标记。维护服务用当前读和事务锁验证双向关联、叶仓公司及原生树边界。

### units_of_measure

`id PK, code UNIQUE, name, decimal_precision, status`

## 3. 批次与库存

### batches

`id PK, stock_item_id FK, batch_no, display_batch_no, created_at, created_by, status`

逻辑唯一键：`stock_item_id + normalized_batch_no`。同一 SKU
下相同批次号复用记录；不同 SKU 可相同。无批次不创建普通 Batch。

### stock_balances

在 ERPNext V1 中，此节描述逻辑余额。物理数量事实来自 ERPNext 有效 SLE 和已提交 Bundle，Bin 提供原生汇总。App 不建立可写余额表。

`id PK, stock_item_id FK, warehouse_location_id FK, batch_id FK NULL, quantity_on_hand DECIMAL, row_version, updated_at`

逻辑唯一键：`stock_item_id + warehouse_location_id + batch_id`。数据库需为非
NULL batch 和 NULL batch 分别建立唯一约束，避免 NULL
导致重复无批次余额行。

### stock_ledger

ERPNext V1 使用原生数量流水。App 的轻量操作日志记录业务归属和原生引用，不存数量向量。

`id PK, transaction_id, document_type, document_id, document_line_id, stock_item_id FK, warehouse_location_id FK, batch_id FK NULL, quantity_delta DECIMAL, transaction_type, operator_id, occurred_at, created_at, remark, reversal_of_ledger_id NULL`

建议索引： - `(stock_item_id, occurred_at)` -
`(warehouse_location_id, stock_item_id)` - `(batch_id, occurred_at)` -
`(document_type, document_id)` - `transaction_id`

## 4. 单据表

### stock_documents

`id PK, document_no UNIQUE, document_type, status, created_by, submitted_at, approved_by NULL, approved_at NULL, approval_comment NULL, posted_at NULL, remark, idempotency_key, created_at, updated_at`

### stock_document_lines

`id PK, document_id FK, line_no, stock_item_id FK, source_location_id NULL, target_location_id NULL, batch_id FK NULL, quantity DECIMAL, uom_id FK, remark`

出库/移库行还需要明确的 batch selection
mode，区分明确选择无批次与未选择。

### V1-07 实际持久化

| DocType | 身份和关联 |
| --- | --- |
| `HP V1 Stock Write Guard` | 每站点一个固定守卫行。批次映射跨公司共享，因此首次建映射也锁此行。 |
| `HP V1 Stock Document` | `name` 为服务端生成的稳定 ID。Company 与业务编号联合唯一。记录类型、创建者、时间、备注、状态、版本及生效操作。 |
| `HP V1 Stock Document Line` | 每行独立生成 `name`。记录单据 ID、行序、SKU、源和目标库位、批次种类、可空映射 ID、数量、UOM、原因和备注。单据内行序唯一。同桶的行独立保存。数量使用经过精度和长度校验的规范十进制文本，数据库为 `VARCHAR(140)`。 |
| `HP V1 Stock Request` | 每站点的 Company、认证用户、命令和键摘要有联合唯一索引。记录原键、规范负载 SHA-256、单据 ID、状态、完成时间和结果操作及版本。事务内 Pending 在提交前转 Completed。失败尝试全部回滚，错误由执行接口返回。 |
| `HP V1 Operation Log` | 稳定操作 ID、动作、文档、请求、认证操作者、服务端创建时间、原因、可空原操作引用与结果版本。请求唯一，且单据和生效动作标识联合唯一。重复生效保护独立于请求记录留存。 |
| `HP V1 Operation Native Link` | 从操作关联到业务行、角色、原生父 DocType 和 ID，以及原生明细 DocType 和 ID。操作、角色和原生父子引用唯一。服务校验明细属于所记录的父单，且所有业务行均有关联。 |

`HP V1 Synthetic Native Document` 和 `HP V1 Synthetic Native Detail` 用于隔离测试的父子引用。V1-07 只验证合成的 Draft → Effective 和版本递增，普通维护入口不能改写、改名或删除这些记录。移库接收、盘点、纠错及审批的状态仍由对应任务定义。

[执行内核](TECHNICAL-DESIGN.md#v1-07-库存执行内核)

### stock_transfers

`id PK, document_id FK, source_location_id FK, target_location_id FK, status, dispatched_at, dispatched_by, created_at, updated_at`

### stock_transfer_receipts

`id PK, transfer_id FK, received_by, received_at, receipt_reference, remark`

### stock_transfer_receipt_lines

`id PK, transfer_receipt_id FK, original_document_line_id FK, batch_id FK NULL, received_quantity DECIMAL`

### stock_counts

`id PK, count_no UNIQUE, warehouse_id FK, status, count_scope, started_at, completed_at, created_by`

### stock_count_lines

`id PK, count_id FK, stock_item_id FK, warehouse_location_id FK, batch_id FK NULL, book_quantity DECIMAL, counted_quantity DECIMAL, difference_quantity DECIMAL, remark`

## 5. 权限与审计

- `roles(id, code, name, status)`
- `user_roles(user_id, role_id)`，唯一键 user_id + role_id
- `permissions(id, code, module, action)`
- `role_permissions(role_id, permission_id)`
- `user_warehouse_scope(user_id UNIQUE, access_mode ALL/SELECTED/NONE)`
- `user_warehouse_scope_items(user_id, warehouse_id)`
- `approval_records(id, document_type, document_id, action, actor_id, acted_at, comment)`
- `audit_logs(id, actor_id, action, entity_type, entity_id, before_snapshot, after_snapshot, occurred_at, request_id)`

## 6. 系统配置

`system_settings(key, value, updated_by, updated_at)`。

V1 使用站点级 `inventory_approval_enabled`。授权创建人自审遵循 D019。

## 7. 未来估值扩展（V1 不启用）

可增加： - `inventory_valuation_layers` - `inventory_cost_entries` -
`valuation_adjustments`

## 8. 实现注意事项

- 库存数量使用 Decimal/Numeric。
- 库存交易使用事务。
- 并发更新需要行锁、乐观锁或等价机制。
- 流水采用追加写入策略。
- 库存余额必须可从流水核对或重建。
- 本文是逻辑模型，不代表已选 PostgreSQL、MariaDB 或其他数据库。

### V1-08 入库选择与明细关联

`HP V1 Stock Document Line.batch_no` 保存入库草稿选择的领域批次文本。显式 `NO_BATCH` 时该字段为空；既有按领域 Batch ID 提交的 V1-07 行继续沿用原字段。提交解析出的 Batch 映射只在本次计划中用于定位原生批次。业务行通过 `HP V1 Operation Native Link` 关联 `Stock Entry Detail`，其父原生单据为 `Stock Entry`。Detail、Bundle 和 SLE 按同一原生明细 ID 关联。余额由 ERPNext 原生表维护。

### V1-09 出库草稿、有效记录与升级

出库复用 V1-07 的业务单头、行、请求、操作日志和原生关联表。草稿行保存源库位、显式批次类型和真实批次文本。BATCH 行同时固定已有的领域 Batch ID。NO_BATCH 行的 `batch_id` 为空，其系统 Batch 由本次执行计划解析。原生关联的 `role=Issue` 指向实际 Stock Entry Detail。该明细再关联已提交 Outward Bundle 及对应的有效负向 SLE。

V1-09 沿用 V1-08 表结构。升级验收比较应用迁移前后的合成历史快照并逐桶复核，覆盖领域 ID、既有入库与请求、原生关联、Batch、Bundle、SLE、Bin 及 GL。新增出库追加业务及原生记录。恢复演练使用同一备份点的数据库、站点配置及公私文件。

### V1-10 移库发出与在途归属

移库复用库存单头、逐行 ID、请求和轻量操作日志。`HP V1 Stock Document` 新增业务原因及 `In Transit` 状态。已有业务行字段保存两侧领域 Location、显式批次和精确数量。状态、版本和发出操作一次更新，日志记录实际员工、服务端创建时间和原因，并关联原幂等请求。`role=Dispatch` 的日志明细将业务行连到已提交的 Stock Entry 和 Stock Entry Detail。

`HP V1 Transit Ownership` 为每条原发出行创建独立 ID，保存发出操作、业务行、系统 Transit 位置、原生 Detail 和有效原生 Inward SLE。业务行、原生 Detail 和 Inward SLE 各有唯一约束。归属记录和关联在发出事务中一起写入，常规权限无维护入口。该表没有数量列。原始已发量由固化业务行与原生双向 SLE 和 Bundle 共同核对。

### V1-11 移库多动作历史

`HP V1 Transit Ownership` 保留原行、发出操作、原生发出 Detail 及在途入向 SLE。
接收与退回各添加独立 `HP V1 Operation Log`，通过 `original_operation` 指回发出。
`HP V1 Operation Native Link` 的 `ownership_id` 将每次操作行关联回原始归属。
后续原生单据与行沿用原生关联字段，操作者、原因、备注和时间保存在操作日志。
累计量由有效原生行与 Bundle、SLE 核对后投影。

发出占用业务单唯一 `APPLY` 效果键。后续效果键采用请求 ID，`(document_id, effect_key)`、
`(document_id, result_revision)` 和请求唯一约束防止产生重复效果。业务单在当前行锁下按原
状态和版本条件推进，`effective_operation` 始终保留发出操作。日志保存每次操作的历史结果状态
和版本。V1-10 发出日志的历史状态从动作推导为 In Transit，安装和迁移保留已有日志与请求。

### V1-12 库存版本与盘点持久化

`HP V1 Inventory Epoch` 是受保护的初始化代次。`HP V1 Inventory Version` 保存 Company、Location、scope、SKU、非空批次身份键、generation、精确整数文本 counter 和数量历史身份摘要，不存库存余额。LOCATION/SKU/BUCKET 分开计数，NO_BATCH 使用非空键；归零桶保留历史。

`HP V1 Stock Count` 与同 ID 的 Stock Document 组合成任务头，保留服务端截止、范围、快照身份、代次、范围版本及前后重盘引用。`HP V1 Stock Count Line` 保存稳定行 ID、明确批次、账面/实盘/差异的无损十进制文本、桶版本、确认标记、发现标记、人员/时间及备注。未确认数量为 NULL；实盘 0 是独立的显式确认。任务内 bucket_key 唯一，避免 NULL 唯一索引歧义。完成复用请求和日志，quantity_effect=None 专用于显式无数量完成，不伪造原生引用。系统记录无普通 CRUD 权限。


### V1-13 持久化映射

`HP V1 Approval Settings` 唯一站点行记录开关和 revision，`HP V1 Approval Setting Change`
追加前后值、操作者、服务端时间及原因。`HP V1 Approval Submission` 固化单据版本、人员、
配置版本、审批要求及规范化负载摘要。盘点负载包括原快照、代次、范围、账面、实盘、逐桶版本及
发现桶证据。`HP V1 Approval Event` 追加状态事件及批准失败，关联实际操作和原生身份。
`HP V1 Approval Request` 保存非数量动作的幂等结果；库存事实继续关联原有的库存请求与操作日志。
业务单保存当前提交 ID，迁移仅添加可空字段，对 V1-12 已完成记录保留原身份和原请求。

### V1-14 纠错关系及长期留存

纠错使用 Stock Document 和 Stock Document Line 保存请求内容。Stock Correction 共享业务单 ID，保存原操作、纠错行与原行映射及正常后续业务引用。不可变 Approval Submission 固化每次提交的关系、依据及专项政策。Correction Review 追加人工审核证据、意见、关闭信息和合法业务引用。Operation Log 和 Operation Native Link 连回原生调整，额度由有效原生事实复核。保护正式单据、盘点、审批、请求、审计和正式附件的长期留存，无自动删除任务或短期重放 TTL。

### V1-15 导出记录和附件边界

HP V1 Export 记录申请人、公司、报表、规范化条件、固定列、实际仓库覆盖、时间、文件字节、SHA-256、状态与期限。HP V1 Export Download 追加可核验下载记录。导出文件存于站点 `private/haxpower_v1/exports`，名称从领域 ID 派生。导出记录与文件可独立核对，恢复时使用同一数据库及文件备份点。V1-16 复用目录隔离、受控读取和完整性验证，另行配置其附件授权与生命周期。V1-15 不增加权威库存数量表。
