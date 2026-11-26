# 产品模型设计

## 1. 设计目标

船舶配件的产品不能只靠名称区分：同一类别下有不同技术参数，同一技术规格也可能由不同品牌供货。建议采用三层结构：

1.  Product Category：产品分类。
2.  Product Specification：技术规格/产品规格。
3.  Stock Item（SKU）：实际参与库存核算的品牌库存产品。

## 2. 核心关系

``` mermaid
erDiagram
    PRODUCT_CATEGORY ||--o{ CATEGORY_ATTRIBUTE : defines
    ATTRIBUTE_DEFINITION ||--o{ CATEGORY_ATTRIBUTE : assigned
    ATTRIBUTE_DEFINITION ||--o{ ATTRIBUTE_OPTION : offers
    PRODUCT_CATEGORY ||--o{ PRODUCT_SPECIFICATION : groups
    PRODUCT_SPECIFICATION ||--o{ PRODUCT_ATTRIBUTE_VALUE : has
    ATTRIBUTE_DEFINITION ||--o{ PRODUCT_ATTRIBUTE_VALUE : describes
    PRODUCT_SPECIFICATION ||--o{ STOCK_ITEM : has_sku
    BRAND ||--o{ STOCK_ITEM : brands
    STOCK_ITEM ||--o{ PRODUCT_IDENTIFIER : identified_by
    STOCK_ITEM ||--o{ PRODUCT_ALIAS : aliases
```

## 3. ProductCategory 产品分类

示例：喷油嘴、活塞环、出油阀、柱塞。

建议字段：`id`, `code`（唯一）, `name`, `parent_id`（可空）,
`description`, `status`, `sort_order`, `created_at`, `updated_at`。

分类不直接保存库存数量。

## 4. AttributeDefinition 属性定义

示例：件号、冷却类型、刻印通用、喷孔规格、机型信息。

建议字段： - `id`, `code`, `name` - `data_type`: TEXT / DECIMAL /
SINGLE_SELECT / MULTI_SELECT / BOOLEAN / DATE - `unit`（数值属性可选） -
`description`, `status`

### AttributeOption 属性选项

单选/多选属性可以有大量选项，例如刻印通用可有 100+ 候选值。

字段：`id`, `attribute_definition_id`, `value`, `label`, `status`,
`sort_order`。

停用选项不删除历史引用；新增选项不自动修改已有产品。

### CategoryAttribute 分类属性配置

将属性分配给分类，并配置： - `required` - `value_mode`: SINGLE / MULTI -
`searchable` - `identity_relevant` - `display_order` -
`editable_for_technician`

候选选项数量与单个产品能选择几个值是两个不同概念。一个属性有 100+
候选值，不代表一个产品必须选多个值。每个属性应配置 SINGLE 或
MULTI；哪些具体属性使用 MULTI 仍待业务确认。

## 5. ProductSpecification 产品规格

代表不考虑品牌时的技术规格。

字段建议：`id`, `specification_code`, `category_id`, `name`,
`description`, `status`, `created_by`, `created_at`, `updated_at`。

动态属性值通过 ProductAttributeValue 关联，不将所有属性写死在产品主表。

## 6. ProductAttributeValue 产品属性值

字段建议： - `id` - `specification_id` - `attribute_definition_id` -
`option_id`（可空） - `value_text` / `value_decimal` / `value_boolean` /
`value_date` - `value_group_id`（多值分组，可空） - `display_value` -
`created_at`, `updated_at`

应用层校验值类型。SINGLE 属性最多一条有效值；MULTI 属性可有多条。

## 7. Brand 与 StockItem（SKU）

### Brand

字段：`id`, `code`, `name`, `status`。

### StockItem / SKU

每个 SKU 是实际库存管理单位。

字段建议： - `id` - `item_code`（企业内部物料编码，唯一） -
`specification_id` - `brand_id`（若确实存在无品牌产品则可空） -
`item_name` - `item_description` - `stock_uom_id` -
`quantity_precision` - `status` - `created_at`, `updated_at`

同一个 ProductSpecification 可以关联多个 StockItem。不同品牌默认建立不同
SKU，分别统计库存。

品牌放在 SKU 层，是因为业务需要分别查看、出库和追溯不同品牌的库存。

## 8. 产品标识与别名

建议独立管理： - ProductIdentifier：OEM
件号、厂家件号、刻印号、旧系统编号等。 -
ProductAlias：喷油嘴、油嘴、Nozzle 等名称别名。

标识可配置类型、来源和是否可搜索，不要全部塞入一个自由文本字段。

## 9. 产品编辑规则

- 新增属性选项不自动修改已有产品。
- 停用选项不删除已有产品历史值。
- 产品属性只有在明确编辑产品时才修改。
- 属性修改记录修改前后值、操作者和时间。
- 如果是录入错误，可按权限更正。
- 如果是实际技术规格变化，优先建立新规格/SKU，避免旧库存历史含义被覆盖。
- 不自动合并属性相同的产品。

## 10. 搜索

支持分类、SKU
编码、产品名称/说明、品牌、OEM/厂家件号、别名、可搜索属性和多关键词组合。

示例：`喷油嘴 DAIHATSU cooling 145`。

搜索结果显示
SKU、品牌、关键属性和库存摘要；库存摘要必须按用户仓库权限过滤。

## 11. V1 暂不纳入

产品替代关系、供应商报价/价格表、采购销售订单、自动合并相同规格、品牌之间自动替代或库存合并。

## 12. V1-02 规格层实现

| DocType | 存储及引用 |
| --- | --- |
| `HP V1 Product Category` | 独立分类，拥有稳定 ID、唯一编码和可选父分类。服务拒绝分类环。 |
| `HP V1 Attribute Definition` | 独立属性，拥有稳定 ID、唯一编码、六种类型及可选 Decimal 单位说明。 |
| `HP V1 Attribute Option` | 独立选项，以属性 ID 和选项键联合唯一。选项 ID、键和所属属性固定，显示名、排序和启停可维护。 |
| `HP V1 Category Attribute` | 独立模板行，以分类和属性联合唯一。模板配置必填、值数量、搜索、规格标识、排序和技术员编辑标记。 |
| `HP V1 Product Specification` | 独立规格，拥有稳定 ID、唯一编码、固定分类及编辑版本。相同属性内容的规格可并存。 |
| `HP V1 Product Attribute Value` | 规格子表，一行对应一个值。保存属性 ID、值类型及对应类型字段，选项行同时保存选项 ID 和稳定选项键。 |
| `HP V1 Attribute Edit Audit` | 独立追加记录，每个改变的属性一行。保存规格 ID、版本、属性 ID、规范化前后值、操作者、服务器时间和操作 ID。 |

工程编码和选项键使用 1–100 位 ASCII 字母、数字、点、下划线或连字符，以字母或数字开头，入库前转为大写。名称、说明和文本值可使用中文。文本去除首尾空白，日期使用 ISO 日历日期，布尔使用真实布尔值。技术规格小数使用 Decimal 校验并以最长 140 字符的精确十进制字符串保存。规格小数的单位说明用于理解技术参数，库存单位和数量精度由后续任务配置。

`TEXT`、`DECIMAL`、`BOOLEAN`、`DATE` 和 `SINGLE_SELECT` 最多一个值。`MULTI_SELECT` 由模板限制为单值或多个不同选项。重复值拒绝，输入顺序不影响规格内容或审计。选项查询按配置排序，可包含停用项以显示历史名称。

新增规格要求分类处于启用状态，且所有启用的必填属性都有值。显式提交属性集合时以当前模板校验全量替换，省略属性集合仅编辑规格基本资料。模板新增或改为必填时既有数据可继续读取，下一次提交完整属性集合须补齐现有必填项。停用属性或选项允许在原规格中保留或移除既有值，新增及移除后重新选择均须满足启用条件。属性已被引用时拒绝变更类型或单位，禁止重挂模板或转移既有规格的分类。将模板收紧为单值前须先显式处理具有多值的规格。

B01 首批真实模板和多选字段仍待确认。B02 权限矩阵及字段范围由 V1-05 集成，`editable_for_technician` 当前保存为模板元数据。品牌与库存 SKU 的实现见下节。

[服务与事务设计](TECHNICAL-DESIGN.md#v1-02-规格维护与事务) · [合成集成测试](../dev/v1/README.md#合成升级和失败恢复) · [B01、B02](PRODUCTION-READINESS-GAPS.md#业务确认清单)


## 13. V1-03 品牌、库存 SKU 和 Item 映射

| DocType | 领域数据与约束 |
| --- | --- |
| `HP V1 Brand` | 稳定 ID、唯一编码、显示名、说明和状态。存在启用 SKU 时拒绝停用品牌。 |
| `HP V1 Stock Item` | 稳定 SKU ID、唯一内部编码、规格和品牌关联、名称说明、库存 UOM、整数精度元数据、状态和编辑版本。每个 SKU 独占一个 ERPNext Item。 |
| `HP V1 Product Identifier` | SKU 的子表，保存 OEM、厂家件号、刻印号、旧系统编号或其他标识的原文、来源、说明和规范化搜索文本。 |
| `HP V1 Product Alias` | SKU 的子表，保存别名原文、可选语言和来源，以及搜索标记。 |

一个规格可关联多个品牌的独立 SKU，每个 SKU 产生独立库存主数据身份。企业编码使用与规格层一致的 ASCII 字符规则，统一转为大写。外部标识与别名允许跨 SKU 重复，搜索返回全部匹配项。维护查询支持中文、英文、SKU 编码、名称说明、品牌、可搜索别名及标识，可用多个关键词组合并分页。

创建时要求显式引用处于启用状态的品牌、规格与分类。品牌字段预留可空存储。B01 确认无品牌建档方式前，创建服务拒绝空品牌。创建 SKU 的单位与精度须匹配显式站点配置和原生 UOM 能力。生产单位、精度表和单行数量上限仍属 B01。

既有 SKU 的编码、品牌、规格、库存单位、精度及 Item 映射固定。可按版本编辑显示名、说明、别名和标识。SKU 停用检查原生余额、流水历史和已知未结引用。完整引用检查及库存写入协调接通前，入口拒绝停用启用中的 SKU。查询 DTO 使用领域 ID 和映射校验状态，库存摘要与仓库权限由后续任务接入。

[适配与事务设计](TECHNICAL-DESIGN.md#v1-03-sku-与原生主数据) · [B01、B02](PRODUCTION-READINESS-GAPS.md#业务确认清单)

## 14. V1-15 员工搜索

员工产品档案提供分类路径、规格、品牌、SKU 代码和名称、说明、OEM/厂家件号/刻印及其他标识、别名、技术属性和单位、库存单位及精度。停用但被引用的选项保留显示。图片和附件使用预留的私有业务对象列表，当前没有关联时返回空列表。读取投影排除审计、原生标识、搜索文本及配置。

搜索规范化为 NFKC 和 casefold，按任意空白拆分。一个关键词可命中同一 SKU 的任一业务字段，所有关键词按 AND 相交。支持中文子串、完整或部分件号和件号内部标点。可搜索属性涵盖文本、Decimal、单选、多选、布尔和日期。精确属性筛选使用类型值，选择项使用稳定领域选项 ID。支持分类子树、品牌和精确 SKU 代码。使用完整代码或件号优先，再按代码和领域 ID 排序。同规格的品牌、别名和标识各自独立。
