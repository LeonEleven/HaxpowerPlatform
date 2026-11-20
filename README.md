# HaxpowerPlatform

HaxpowerPlatform 是面向船舶配件企业的库存管理平台。Stage 3A 已完成。正式 V1 基线使用 ERPNext v16 和独立的 `haxpower_v1` Frappe App，界面名为 HaxpowerPlatform。业务功能和生产验收按 V1 Backlog 推进。

## 项目目标

建立一套简单、稳定的库存管理系统，支持动态产品属性、多品牌
SKU、多仓库/库位、可空批次、库存流水、移库在途、可配置审批、角色权限和仓库数据范围。V1
只管理数量，不启用库存金额、成本或财务核算，但保留未来扩展点。

## 开发入口

V1-01 提供 [正式 App 源码](apps/haxpower_v1)、[独立开发环境与迁移说明](dev/v1/README.md) 和 [V1 工程 CI](.github/workflows/v1-engineering.yml)。

## 文档导航

### 项目进度与实施

- [项目交接](docs/PROJECT-HANDOFF.md) 提供项目背景、进度快照、实验索引和新会话阅读顺序。
- [协作规则](AGENTS.md) 与 [项目决策](docs/DECISIONS.md) 记录实施约束、已确认的业务规则和待确认事项。
- [架构决策记录](docs/ARCHITECTURE-DECISION-RECORD.md) 与 [ERPNext V1 适配边界](docs/ERPNEXT-V1-ADAPTATION-BOUNDARIES.md) 说明选型和职责。
- [V1 实施计划](docs/V1-IMPLEMENTATION-PLAN.md) 与 [开发 Backlog](docs/V1-DEVELOPMENT-BACKLOG.md) 定义正式工程基线和任务依赖。
- [V1 验收清单](docs/V1-ACCEPTANCE-CHECKLIST.md) 与 [生产就绪差距](docs/PRODUCTION-READINESS-GAPS.md) 记录上线门槛及待确认问题。

### 需求与设计

- [产品需求规格](docs/PRD.md) · [产品模型](docs/PRODUCT-MODEL.md) · [库存模型](docs/INVENTORY-MODEL.md)
- [逻辑数据库](docs/DATABASE-DESIGN.md) · [权限](docs/PERMISSIONS.md) · [业务流程](docs/BUSINESS-FLOWS.md)
- [API 契约草案](docs/API-CONTRACTS.md) · [技术设计](docs/TECHNICAL-DESIGN.md)

### 历史验证

- [阶段报告索引](docs/PROJECT-HANDOFF.md#4-历史技术验证) · [实验环境](dev/erpnext-lab/README.md) · [ERPNext 适配差距](docs/ERPNext-FIT-GAP.md)
- [首轮原型验证计划](docs/ERPNext-VALIDATION-PLAN.md) · [技术路线比较](docs/TECHNICAL-ROUTE-COMPARISON.md) · [早期分阶段开发计划](docs/DEVELOPMENT-PLAN.md)

## 当前技术路线

ERPNext 负责原生库存数量账，Haxpower App 提供受控业务流程及轻量操作日志。Frappe-only 保留为已完成对照实验的备选路线。生产放行前须验证审计、权限、库存对账和备份恢复。
[架构决策记录](docs/ARCHITECTURE-DECISION-RECORD.md)

## 核心约束

- 旧 ERP 源码仅作业务参考，不复用代码。
- 正式开发使用独立环境，上线前验证关键库存及生产就绪条件。
- 所有库存变更必须通过正式业务单据和服务层完成。
- 禁止直接修改库存余额或库存流水。
- 正式库存流水不可直接编辑或删除，普通业务纠错通过新增反向单据。
- `batch_id = NULL` 表示无批次；必须区别“无批次”和“未选择批次”。
- 所有查询、报表、导出和 API 均执行仓库权限。
- 未来接入 AI/OCR 时，识别结果先生成草稿，经人工确认后才能形成正式业务单据。
