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
