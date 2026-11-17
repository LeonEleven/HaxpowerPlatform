# HaxpowerPlatform

船舶配件企业专用库存管理平台。文档版本 0.1.0，日期
2026-11-16。项目仍处于需求与技术验证阶段，尚未选定最终技术路线。

## 项目目标

建立一套简单、稳定的库存管理系统，支持动态产品属性、多品牌
SKU、多仓库/库位、可空批次、库存流水、移库在途、可配置审批、角色权限和仓库数据范围。V1
只管理数量，不启用库存金额、成本或财务核算，但保留未来扩展点。

## 文档导航

- `AGENTS.md`：Codex 协作规则
- `docs/PRD.md`：产品需求规格
- `docs/DECISIONS.md`：已确认决策和待确认事项
- `docs/PRODUCT-MODEL.md`：产品分类、规格、品牌、SKU、动态属性
- `docs/INVENTORY-MODEL.md`：库存、批次、流水、在途和盘点
- `docs/DATABASE-DESIGN.md`：逻辑数据表
- `docs/PERMISSIONS.md`：角色与仓库数据范围
- `docs/BUSINESS-FLOWS.md`：入库、出库、移库、盘点和审批
- `docs/API-CONTRACTS.md`：API-first 接口草案
- `docs/TECHNICAL-DESIGN.md`：技术架构
- `docs/ERPNext-FIT-GAP.md`：ERPNext 原型验证
- `docs/DEVELOPMENT-PLAN.md`：分阶段开发计划

## 建议阅读顺序

1.  DECISIONS
2.  PRD
3.  PRODUCT-MODEL、INVENTORY-MODEL
4.  PERMISSIONS、BUSINESS-FLOWS
5.  API-CONTRACTS、TECHNICAL-DESIGN
6.  ERPNext-FIT-GAP
7.  DEVELOPMENT-PLAN

## 当前技术路线

优先验证 ERPNext + 独立 HaxpowerPlatform Frappe App；同时保留
Frappe-only 和独立自研路线。先确定业务模型与 API，再决定技术栈。

## 核心约束

- 旧 ERP 源码仅作业务参考，不复用代码。
- 不在关键库存测试通过前认定 ERPNext 为最终方案。
- 所有库存变更必须通过正式业务单据和服务层完成。
- 禁止直接修改库存余额或库存流水。
- 正式库存流水不可直接编辑/删除，纠错通过冲销/调整。
- `batch_id = NULL` 表示无批次；必须区别“无批次”和“未选择批次”。
- 所有查询、报表、导出和 API 均执行仓库权限。
- AI 识别结果先生成草稿，经人工确认后才能形成正式业务单据。
