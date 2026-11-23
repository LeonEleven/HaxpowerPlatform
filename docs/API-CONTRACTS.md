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
