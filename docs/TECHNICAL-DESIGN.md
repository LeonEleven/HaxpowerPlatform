# 技术架构设计

## 1. 架构目标

- 满足当前约 11 名用户、1000 种以内 SKU。
- 低复杂度、易维护、性能稳定。
- 阿里云 Linux 部署，互联网浏览器访问。
- 为微信小程序、AI/OCR 和外部系统预留 API。
- 核心业务模型尽量不绑定 ERPNext 内部结构。
- 不过早采用微服务。

## 2. 架构风格

V1 建议采用 API-first
模块化单体，而不是微服务。当前规模不需要先引入分布式部署、消息总线或服务网格。

模块建议： - Identity & Access - Product Catalog - Warehouse -
Inventory - Stock Documents - Approval - Search - Audit - Integration -
Future AI Document Processing

## 3. 分层

``` mermaid
flowchart TB
    WEB[桌面网页] --> API[Haxpower API]
    MINI[未来微信小程序] --> API
    AI[未来 OCR / AI 服务] --> API
    API --> APP[应用服务层]
    APP --> DOMAIN[领域模型与业务规则]
    DOMAIN --> PORTS[库存引擎适配接口]
    PORTS --> ERP[ERPNext Adapter / 自研库存引擎]
    ERP --> DB[(数据库)]
    APP --> JOBS[后台任务队列]
    JOBS --> OCR[OCR / AI 外部服务]
```

前端只调用 API。库存服务负责业务校验和事务，不允许前端直接写数据库。

## 4. 技术路线

### A. V1 实施基线 · ERPNext v16 + HaxpowerPlatform Frappe App

- 复用 ERPNext Item、Warehouse、Batch、Stock Entry、Stock Ledger
  等能力。
- 自己实现产品规格层、专属界面、API facade、审批开关和业务定制。
- 不修改 ERPNext 核心源码。
- 风险：同一 SKU 混合有批次/无批次、估值机制、报表权限和自定义库存流程。

### B. Frappe Framework + 自研库存引擎

- 复用 Frappe 用户、角色、DocType、API、文件和后台任务。
- 不依赖 ERPNext 库存模型。
- 需自行实现库存流水、余额、批次、在途、盘点、并发和一致性。

### C. 独立技术栈

- 可评估 Vue 3 + TypeScript、ASP.NET Core、PostgreSQL 等组合。
- 产品、库存、权限、审批、API、文件、任务和审计全部自研。
- 自主性最高，但初期工作量最大。
- 此组合只是候选，不是已经决定的技术栈。

## 5. 解耦策略

正式 App 的发行名、Frappe App 名和 Python 包名统一为 `haxpower_v1`。源码位于
[apps/haxpower_v1](../apps/haxpower_v1)，开发和 CI 独立安装并锁定上游依赖。
[开发入口与迁移说明](../dev/v1/README.md)

- HaxpowerPlatform 领域服务和 API facade 通过 Adapter 调用 ERPNext 标准库存操作。
- 公共 API 边界封装 ERPNext 内部 DocType。库存适配层通过原生单据和受支持接口读写，保留 ERPNext 核心源码原样。
- ERPNext 升级纳入回归验证。V1-01 的迁移测试覆盖空站安装、补丁重放及合成版本恢复。

领域模型使用自己的稳定
ID、SKU、批次选择模式、库存位置和单据状态；适配层负责映射到 ERPNext。

### V1-02 规格维护与事务

- `haxpower_v1.catalog.service` 提供以领域 DTO 为输入和输出的内部 Python 服务。调用方使用已认证的 Frappe 会话，工程角色 `HP V1 Catalog Maintainer` 须显式授予。内建 Administrator 可在受控服务器环境调用。普通员工无档案服务访问权。DocType 的 `System Manager` 权限仅用于读取诊断数据，常规 DocType 保存、删除及改名经过控制器防护。
- 规格主记录和属性子表由 Frappe 在同一事务写入。维护服务先锁规格 DocType 元数据行，之后读取最新配置和规格版本。该站点级锁串行化目录写入。规格更新核对调用方传入的版本，属性变化与审计写入使用同一保存点。调用方负责外层提交，任一审计写入失败回滚本次编辑和已写审计。
- 单列编码唯一约束来自 DocType `unique` 元数据。选项和模板的复合唯一约束由安装及迁移末尾的幂等钩子创建并核对列序，复跑保留已有数据。选项和模板采用独立 DocType，可按所属属性或分类查询。规格值作为子表，读取不依赖最新模板。Decimal 值保留原有精度，跨规格数值范围搜索需要 V1-15 定义查询表示。
- 审计保留明确编辑的前后值、操作者、时间、规格版本和操作 ID。记录只允许服务新增。数据库维护、备份恢复和受信任的服务器代码继续遵守运维控制。V1-05 结合 B02 复测角色及已启用入口后再对员工开放。
- V1-03 可以通过 `get_specification(specification_id)` 获取稳定 ID、分类、状态、版本和类型化属性。调用者用规范的规格 ID 关联 SKU，按其自身业务入口授权策略校验。未来公开接口由外层将 DTO 转为契约表示。

[产品模型和编辑语义](PRODUCT-MODEL.md#12-v1-02-规格层实现) · [Frappe 子表](https://docs.frappe.io/framework/user/en/basics/doctypes/child-doctype)

## 6. 库存事务

正式库存提交应在一个事务中： 1. 校验用户权限。 2. 校验单据状态。 3. 校验
SKU、仓库、库位、批次和单位。 4. 校验可用数量。 5.
并发保护并扣减/增加库存。 6. 写入不可变库存流水。 7. 更新库存余额。 8.
更新单据状态。 9. 提交事务。

任何一步失败，整体回滚。

## 7. 搜索

V1 产品数小于
1000，可先用数据库索引和查询优化，不必立即部署独立搜索集群。搜索字段包括
SKU
编码、产品名称、品牌、别名、件号和可搜索属性。中文分词/模糊匹配需按所选数据库实测。

## 8. 前端体验

- 适合老旧电脑的简洁界面。
- 服务端分页和搜索防抖。
- 大表格按需使用虚拟滚动。
- 批量表单减少重复录入。
- 关键错误显示可理解的原因和处理方法。
- 重要库存信息不只通过鼠标悬浮展示。

## 9. 异步任务与 AI

未来 OCR/AI 识别、文件解析和批量导入走后台队列： 上传文件 → 创建识别任务
→ 后台识别 → 生成结构化草稿 → 人工确认 → 通过正式业务 API 创建单据。

AI 不直接写库存余额或绕过审批。

## 10. 部署和安全

- 阿里云 Linux。
- Docker/Compose 可作为候选，最终遵循所选框架官方生产部署方案。
- 全站 HTTPS。
- 数据库和 Redis 不对公网开放。
- 安全组仅开放必要入口。
- 定时备份、异地备份和恢复演练。
- 开发、测试、生产环境隔离。
- 上传文件校验类型、大小和访问权限。
- 管理员强认证。

## 11. 库存估值扩展

V1 不启用金额、成本和财务报表。未来估值模块与数量流水分离。采用 ERPNext
时，必须验证其内置估值字段、计算和可能的会计联动；隐藏金额列不等于底层不进行估值。

## 12. 外部扩展

未来小程序、AI 服务和其他系统只依赖 HaxpowerPlatform 版本化 API，不把
ERPNext 内部接口作为长期对外契约。
