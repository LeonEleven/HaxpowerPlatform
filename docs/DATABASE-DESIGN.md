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

`id PK, stock_item_id FK, warehouse_location_id FK, batch_id FK NULL, quantity_on_hand DECIMAL, row_version, updated_at`

逻辑唯一键：`stock_item_id + warehouse_location_id + batch_id`。数据库需为非
NULL batch 和 NULL batch 分别建立唯一约束，避免 NULL
导致重复无批次余额行。

### stock_ledger

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

V1 至少包括 `inventory_approval_enabled` 和
`allow_creator_self_approval`。

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
