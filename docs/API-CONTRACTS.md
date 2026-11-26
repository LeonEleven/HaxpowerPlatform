# HaxpowerPlatform API 契约草案

## 1. 目标

桌面网页、未来微信小程序、OCR/AI 服务和其他系统统一通过 HaxpowerPlatform
业务 API 访问数据。公共 API 不直接暴露 ERPNext 内部
DocType、数据库字段或内部库存表。

建议基础路径：`/api/haxpower/v1`。

## 2. 通用约定

- HTTPS only，JSON 请求/响应。
- API 版本放在路径中。
- 所有写请求验证身份、角色、仓库范围和业务规则。
- 提交、发出、接收等命令支持 `Idempotency-Key` 或等价 requestId。
- 数量建议以十进制字符串传输，例如 `"12.000"`，避免 JavaScript
  浮点误差。
- 日期时间使用 ISO 8601 并明确时区。
- 分页参数统一：`page`, `pageSize`, `sort`, `filters`。
- 错误响应提供稳定错误码，不直接返回数据库异常。

## 3. 建议接口

### 产品

- `GET /products/search`
- `GET /products/{productId}`
- `GET /product-categories`
- `GET /brands`

### 仓库与库存

- `GET /warehouses`
- `GET /warehouses/{warehouseId}/locations`
- `GET /inventory/balances`
- `GET /inventory/items/{stockItemId}/distribution`
- `GET /inventory/ledger`
- `GET /inventory/batches`

### 库存单据

- `POST /stock-receipts`
- `GET /stock-receipts/{id}`
- `POST /stock-receipts/{id}/submit`
- `POST /stock-issues`
- `POST /stock-issues/{id}/submit`
- `POST /stock-transfers`
- `POST /stock-transfers/{id}/submit`
- `POST /stock-transfers/{id}/dispatch`
- `POST /stock-transfers/{id}/receive`
- `POST /stock-counts`
- `POST /stock-counts/{id}/submit`

### 审批和管理

- `POST /approvals/{documentType}/{documentId}/approve`
- `POST /approvals/{documentType}/{documentId}/reject`
- `GET /users/{id}/warehouse-scope`
- `PUT /users/{id}/warehouse-scope`
- `GET /system-settings/inventory-approval`

接口为草案，最终根据选定技术栈和页面确定。


V1-13 先交付内部领域服务。审批动作使用公司、业务单据 ID、submission ID、当前 revision、
幂等键及意见或原因。批准、驳回、撤回及发出前撤销都引用明确的某次提交。返回业务状态、
revision、submission ID 和可空的 operation ID。原键同负载重放先做当前授权，原键负载
不同返回冲突。待审时 operation ID 为空。移库提交、发出和接收各用独立动作。
配置读取与带 revision 的配置修改由系统维护授权，提交命令不提供开关参数。
历史查询只返回有仓库范围的审批快照及事件。过期盘点先撤回，再通过重盘新建快照。
正式员工数量写入及审批 HTTP 路由继续关闭。

## 4. 批次字段约定

有批次入库：

``` json
{
  "stockItemId": "SKU-6003",
  "warehouseLocationId": "LOC-SH-01",
  "quantity": "12",
  "uom": "PC",
  "batchSelection": { "mode": "BATCH", "batchNo": "STD61" }
}
```

无批次入库：

``` json
{
  "stockItemId": "SKU-6003",
  "warehouseLocationId": "LOC-SH-01",
  "quantity": "12",
  "uom": "PC",
  "batchSelection": { "mode": "NO_BATCH" }
}
```

入库、出库、移库和盘点正式提交都需显式传 `batchSelection`。缺少选择或 `mode` 时返回 `BATCH_SELECTION_REQUIRED`。未知模式和额外字段返回 `INVALID_BATCH_SELECTION`。`BATCH` 要求非空字符串，`NO_BATCH` 只接受 `mode`，库存响应中的 `batch` 为 `null`。

V1-06 的候选规则可通过内部预览返回原文、候选值、策略版本和确认状态。裁剪首尾空白时，写入要求确认同一预览。若既有记录的规范键相同且原文不同，返回 `BATCH_NORMALIZATION_COLLISION` 并保留原记录。B03 审定后，正式业务命令按对应仓库与操作权限接入，业务响应和错误只使用领域 ID、真实批次文本或 `null`。

[候选规则](ERPNEXT-V1-ADAPTATION-BOUNDARIES.md#批次输入规范) · [内部服务](TECHNICAL-DESIGN.md#v1-06-批次映射和事务)

## 5. 库存查询响应示例

``` json
{
  "items": [
    {
      "stockItemId": "SKU-6003",
      "itemCode": "6003",
      "itemName": "喷油嘴",
      "brand": "平和",
      "warehouse": "上海办",
      "location": "正式库区",
      "batch": "STD61",
      "quantity": "12",
      "uom": "PC"
    },
    {
      "stockItemId": "SKU-6003",
      "itemCode": "6003",
      "itemName": "喷油嘴",
      "brand": "平和",
      "warehouse": "上海办",
      "location": "正式库区",
      "batch": null,
      "batchDisplay": "无批次",
      "quantity": "15",
      "uom": "PC"
    }
  ],
  "page": 1,
  "pageSize": 50,
  "total": 2
}
```

响应数据必须已经应用当前用户仓库权限。

## 6. 幂等与并发

- 同一个 Idempotency-Key 重试不能重复产生库存流水。
- 库存扣减在服务端事务内校验并锁定或使用并发控制。
- 并发导致库存不足时返回 `INSUFFICIENT_STOCK`。

V1-07 的内部执行契约按站点、Company、当前认证主体、命令及幂等键确定请求身份。键大小写敏感，使用 1–100 位 ASCII 字母、数字、点、下划线或连字符，第一位须为字母或数字。规范 JSON 使用 SHA-256，覆盖公司、主体、命令、目标单据、期望版本、稳定行 ID 和行序、SKU、源和目标库位、批次种类及映射身份、UOM、等值数量、原操作引用、原因与行备注。JSON 键顺序和已提供行的排列顺序可以改变。行序号和行身份参与摘要。数量须为十进制字符串，`1`、`1.0` 和 `1.000` 摘要相同。

- 同键重试按当前操作权限及原单据两侧仓库数据范围授权，重放原单据 ID、操作 ID 和结果版本。
- 同键异负载返回内部错误 `STOCK_IDEMPOTENCY_CONFLICT`。不同键提交同一已生效单据会被状态和动作唯一性约束拒绝。
- `STOCK_RETRY_REQUIRED` 时使用同一个键和负载重新执行完整校验。提交回包中断先在独立连接取锁核对已提交请求。核对仍无法确认时返回 `STOCK_COMMIT_UNKNOWN`。
- V1-07 合成入口只供隔离站点 Bench 内执行。后续员工命令的 DTO、授权及错误映射随各业务动作接入。B08 确认前，请求和操作日志保留且清理关闭。

## 7. 错误码建议

`UNAUTHENTICATED`, `FORBIDDEN`, `WAREHOUSE_ACCESS_DENIED`,
`PRODUCT_NOT_FOUND`, `BATCH_SELECTION_REQUIRED`, `BATCH_NOT_FOUND`,
`INSUFFICIENT_STOCK`, `QUANTITY_PRECISION_EXCEEDED`,
`INVALID_DOCUMENT_STATE`, `IDEMPOTENCY_CONFLICT`,
`TRANSFER_RECEIPT_EXCEEDS_REMAINING`。

## 8. 未来 AI 接口

- `POST /documents/recognition-jobs`
- `GET /documents/recognition-jobs/{id}`
- `POST /documents/recognition-jobs/{id}/confirm`

识别任务异步执行，结果先生成草稿。只有有权限的员工人工确认后，才创建正式业务单据。AI
服务不直接写库存余额或库存流水。

## 9. 身份认证

具体认证方案随技术栈确定： - 浏览器使用安全会话或受保护 Token。 -
小程序使用服务端登录/授权交换机制。 - 不在小程序前端存放管理员 API
Key/Secret。 - 外部集成使用独立服务账号和最小权限。 - 支持 Token
撤销、轮换和审计。

## 10. V1-08 内部入库 DTO

Bench 中的内部接口为 `haxpower_v1.inventory.receipt.service.create_draft` 和 `submit`。草稿行要求 SKU ID、目标库位 ID、库存 UOM、Decimal 字符串，以及 `{"mode":"BATCH","batchNo":"STD61"}` 或 `{"mode":"NO_BATCH"}`。草稿响应生成稳定单据 ID、版本和行 ID。提交携带草稿及幂等键，返回单据 ID、操作 ID、最终状态、版本和领域行。员工 HTTP 入口关闭。业务响应使用领域批次选择和 ID。

同一认证主体对同一操作的同键等效负载取得原结果。更改同键负载返回冲突。单据一旦生效，其他键也无法使其再次入库。授权在重放前重新检查。

## 11. V1-09 内部出库 DTO

`haxpower_v1.inventory.issue.service.create_draft` 接收 `IssueDraftInput`。每行提供 `sku_id`、`source_location_id`、`batch`、Decimal 字符串 `quantity`、库存 `uom`，以及业务原因和备注字段。批次选择为 `{"mode":"BATCH","batchNo":"STD61"}` 或 `{"mode":"NO_BATCH"}`，均要求已有映射。草稿返回稳定单据 ID、版本和行 ID。

`submit(SubmitIssue)` 需要草稿及幂等键。响应包含单据 ID、操作 ID、状态、版本及原行的领域信息。同键同负载在重新授权后返回原结果。并发竞争中，无法满足任一源桶需求的请求返回 `INSUFFICIENT_STOCK` 并回滚。未知或漂移的批次映射返回 `BATCH_MAPPING_UNAVAILABLE`。原生数量事实存在差异时返回 `ISSUE_NATIVE_RECONCILIATION_FAILED`。

`get_availability(company, sku_id, source_location_id, batch)` 检查 ISSUE 操作权限及源仓范围，返回指定桶的 Decimal 数量字符串、库存 UOM 和领域标识。无批次返回 `batch=null`。合同中的三个接口目前仅由明确启用的独立合成 Bench 使用。员工 HTTP 写入口待 B01、B03、B12 和正式授权确认。

## 12. V1-10 内部移库 DTO

`haxpower_v1.inventory.transfer.service.create_draft(TransferDraftInput)` 接收 Company、原因、备注和多行。每行提供 `sku_id`、`source_location_id`、`target_location_id`、显式 `batch`、库存 `uom`、Decimal 字符串 `quantity` 和可选原因与备注。批次传 `{"mode":"BATCH","batchNo":"STD61"}` 或 `{"mode":"NO_BATCH"}`，两种均要求已有映射。返回 `transfer_id`、`transfer_line_id` 和草稿版本。

`dispatch(DispatchTransfer)` 接收原草稿和幂等键，返回 `transfer_id`、`operation_id`、`revision` 和 `In Transit` 状态。重复发出按原请求身份处理。发出前按授权重新读取草稿，核对两侧库位及每个合计后的源桶。过量返回 `INSUFFICIENT_STOCK`，原生在途与归属不一致返回 `TRANSFER_TRANSIT_OWNERSHIP_INCONSISTENT`，无法唯一解析有效系统位置时返回 `TRANSFER_TRANSIT_UNAVAILABLE`。

`get_dispatch(company, transfer_id)` 是供 V1-11 使用的内部读取契约。它逐行返回稳定行 ID、源和目标、SKU、领域 Batch 选择和 ID、库存 UOM、已发数量、Transit 位置、归属 ID、原生发出单与明细及入向 SLE 引用。头部包含发出操作、版本、状态、操作者和时间。读取重验 Transfer 权限及两侧仓库范围。系统 Batch ID 保留在适配层，NO_BATCH 的 `batch_id` 为 `null`。原生引用仅供服务间关联。后续数量命令可在自己的事务中调用 `lock_original_dispatch` 重验同一快照。
原发出操作、原行和原生关联固定，读取同时投影当前状态、revision、逐行实收、已退和剩余量。
已关闭的历史也可按当前授权读取。这些方法只在明确启用的独立合成 Bench 可用。

## 13. V1-11 内部接收与退回 DTO

`receive(ReceiveTransfer)` 与 `return_remaining(ReturnTransfer)` 各接收 `company`、
`transfer_id`、`expected_revision`、`idempotency_key`、本次 `lines`、原因及备注。
每行只接收 `transfer_line_id` 和正数 Decimal 字符串 `quantity`。退回要求非空白原因。
SKU、单位、批次、库位和原生记录由原发出证据确定。输入行 ID 必须唯一并属于该移库单。
单行超出剩余在途返回 `TRANSFER_LINE_REMAINING_INSUFFICIENT`，旧版本的新操作返回
`STOCK_REVISION_CONFLICT`，已关闭单据当前版本的新数量操作返回 `TRANSFER_ALREADY_CLOSED`。

结果返回 `transfer_id`、本次 `operation_id`、本次结果 `status` 和 `revision`。
摘要涵盖动作、认证人、公司、单据、期望版本、按 ID 排序的逐行规范数量、原因和备注。
当前双侧权限通过后，同键同负载返回本次历史结果，后续版本推进或移库关闭时依然可重放。
同键异负载返回 `STOCK_IDEMPOTENCY_CONFLICT`。正式员工 HTTP 数量入口仍关闭。

## V1-12 内部盘点契约

`inventory.stock_count.service` 提供 `create(CreateCount)`、`get(company, task_id)`、`record(company, task_id, revision, tuple[Actual])`、`discover(company, task_id, revision, sku_id, batch)`、`recount(company, task_id, revision, reason)` 与 `complete(CompleteCount)`。均重验 COUNT 和单个物理库位仓库范围。尚无员工 HTTP 写入口。

create 只接收范围选择与备注；快照数量、时间、代次和版本由服务器生成。LOCATION 不接受 SKU/批次，SKU 不接受批次，BUCKET 要求明确选择。record 只接受稳定行 ID、十进制实盘字符串及备注；NULL 不代表零。discover 不扩大范围，不创建 SKU；复用 V1-06 受控批次服务。recount 追加任务并保留旧数据，不复制实盘。

complete 携带任务投影（含期望 revision、原快照身份和稳定行）、幂等键及必要原因。规范摘要涵盖当前主体、公司、范围、快照、代次、逐行确认/发现身份与规范数量。返回稳定单据 ID、完成操作、状态 Completed 和新 revision。范围、桶、代次改变分别报 COUNT_SCOPE_CHANGED / COUNT_BUCKET_CHANGED / COUNT_GENERATION_CHANGED；实盘遗漏报 COUNT_ACTUAL_INCOMPLETE。失败无数量或版本残留；历史成功重放先重新授权。
