# HaxpowerPlatform 项目交接

## 当前状态

**2026-11-28（Asia/Shanghai）V1-16A。** PR #30 于 09:54 手动合并，实施基线为 `2794a4556bd45bada286d25d2cb9e7e365eb8f57`。V1-16A 在 `feature/v1-16a-formal-master-data` 交付 App 0.17.0，工程运行结果以任务 PR 为准。架构评审与手动合并完成后，后续工程任务为 V1-16B 主数据迁移与审核，须单独授权。正式员工库存数量 HTTP 写入保持关闭。 [PR #30](https://github.com/LeonEleven/HaxpowerPlatform/pull/30) · [V1-16A Draft PR #31](https://github.com/LeonEleven/HaxpowerPlatform/pull/31)

**2026-11-27（Asia/Shanghai）核对。** V1-01 至 V1-15 已完成工程阶段验收及 ChatGPT 架构评审，PR #14–#28 均已合入 main。LeonEleven 于 15:44 手动合并 [V1-15 PR #28](https://github.com/LeonEleven/HaxpowerPlatform/pull/28)。核对时的 `origin/main` 为 [`b3cf05a06372be9e5e47653d243d250033d00f58`](https://github.com/LeonEleven/HaxpowerPlatform/commit/b3cf05a06372be9e5e47653d243d250033d00f58)，已合入的 App 版本为 0.16.0。正式员工库存数量写入 HTTP 入口继续关闭，开放须完成发布验收并取得明确授权。

**2026-11-28（Asia/Shanghai）核对。** [PR #29](https://github.com/LeonEleven/HaxpowerPlatform/pull/29) 已于 2026-11-27 16:29 手动合并，远端 main 为 [`1ed9e04a96a8ead29e6a17bc71e7affb97235306`](https://github.com/LeonEleven/HaxpowerPlatform/commit/1ed9e04a96a8ead29e6a17bc71e7affb97235306)。项目负责人已批准 B01、B03 和 B10 共 33 项业务规则。下一步为架构评审本次文档 PR，由项目负责人手动合并。拆分提案经合并成为规划基线后，从 V1-16A 正式主数据规则完善开始编写任务书并取得实施授权。V1-16A/B/C 全程不改变库存数量，V1-17 专项签核后执行正式原生入账与逐桶核对。 [项目决策](DECISIONS.md#12-b01-b03-b10-正式业务规则) · [V1-16 需求及规划](V1-16-REQUIREMENTS.md) · [开发 Backlog](V1-DEVELOPMENT-BACKLOG.md#v1-16-拆分规划)

## 项目与开发环境

- 项目面向船舶配件企业。V1 管理库存数量、动态产品属性、多品牌 SKU、多仓多库位、可空批次和两阶段移库。现有约 1 名主要操作员、10 名查询人员、3 个仓库，SKU 估计在 1000 种以内。员工通过互联网浏览器访问，企业已有阿里云 Linux 服务器。采购、销售、业务成本及财务留待后续。 [产品需求](PRD.md) · [项目决策](DECISIONS.md)
- 技术路线为 ERPNext v16 + 独立 `haxpower_v1` Frappe App，采用 API-first 模块化单体和受支持的接口及 Hooks。ERPNext 原生有效 SLE 和已提交 Batch Bundle 提供数量事实，Bin 用于汇总核对。Haxpower 提供领域主数据、权限、工作流、纠错、轻量审计及受控 API。外部接口使用领域标识，显式 NO_BATCH 对外为 null，适配层映射到 SKU 专用系统 Batch。 [架构决策](ARCHITECTURE-DECISION-RECORD.md) · [ERPNext 适配边界](ERPNEXT-V1-ADAPTATION-BOUNDARIES.md)
- 开发基线为 Windows 11、WSL2 Ubuntu 24.04、Docker Desktop、VSCode 和 Codex。正式 App 位于 `apps/haxpower_v1/`，`dev/v1/` 使用独立 Bench、Python 环境、站点、数据库、Redis、worker、网络、端口和卷。锁定 Frappe/ERPNext v16.55.0、MariaDB 11.8.9、Redis 7.4.12，依赖提交和镜像摘要以版本锁为准。 [App 源码](../apps/haxpower_v1) · [正式开发说明](../dev/v1/README.md) · [版本锁](../dev/v1/versions.lock.json)
- `dev/erpnext-lab/` 内的原型 `haxpower_platform` 与 Frappe-only 对照仅供查阅实验结果。正式 App 独立实现，保持上游核心源码原样。开发和 CI 使用隔离合成数据。生产环境访问、部署及真实数据操作须按发布治理另行授权。 [历史技术验证](PROJECT-HANDOFF-HISTORY.md#4-历史技术验证) · [协作规则](../AGENTS.md)

## 工程任务概览

| 任务 | 已合并交付 |
| --- | --- |
| V1-01 · [#14](https://github.com/LeonEleven/HaxpowerPlatform/pull/14) | 正式 App、独立开发/CI 基线及安装迁移恢复 |
| V1-02 · [#15](https://github.com/LeonEleven/HaxpowerPlatform/pull/15) | 分类、动态属性、规格及属性审计 |
| V1-03 · [#16](https://github.com/LeonEleven/HaxpowerPlatform/pull/16) | 品牌 SKU 与原生 Item/UOM 映射 |
| V1-04 · [#17](https://github.com/LeonEleven/HaxpowerPlatform/pull/17) | 仓库、库位和 ERPNext 叶仓映射 |
| V1-05 · [#18](https://github.com/LeonEleven/HaxpowerPlatform/pull/18) | 仓库范围和原生入口守卫 |
| V1-06 · [#19](https://github.com/LeonEleven/HaxpowerPlatform/pull/19) | 显式批次选择、批次规范和系统 Batch 映射 |
| V1-07 · [#20](https://github.com/LeonEleven/HaxpowerPlatform/pull/20) | 多行事务、幂等及轻量操作日志 |
| V1-08 · [#21](https://github.com/LeonEleven/HaxpowerPlatform/pull/21) | 多行入库及原生对账 |
| V1-09 · [#22](https://github.com/LeonEleven/HaxpowerPlatform/pull/22) | 多行出库、精确批次可用量及扣减守卫 |
| V1-10 · [#23](https://github.com/LeonEleven/HaxpowerPlatform/pull/23) | 移库发出与在途归属 |
| V1-11 · [#24](https://github.com/LeonEleven/HaxpowerPlatform/pull/24) | 部分接收及剩余在途返源 |
| V1-12 · [#25](https://github.com/LeonEleven/HaxpowerPlatform/pull/25) | 非冻结盘点、版本及原生盘点调整 |
| V1-13 · [#26](https://github.com/LeonEleven/HaxpowerPlatform/pull/26) | 审批开关、审批历史及原子库存生效 |
| V1-14 · [#27](https://github.com/LeonEleven/HaxpowerPlatform/pull/27) | 专项纠错、原行额度及轻量追溯 |
| V1-15 · [#28](https://github.com/LeonEleven/HaxpowerPlatform/pull/28) | 授权目录、库存查询、报表、Excel 和下载重验 |

历次测试、迁移、故障恢复、PR 与 CI 证据见 [历史交接与工程证据](PROJECT-HANDOFF-HISTORY.md)。旧快照中的 Draft、待确认及等待 CI 均以记录当时为准。

## V1-15 最终架构评审

ChatGPT 对 PR #28 进行了两轮架构评审，依据 GitHub 实际源码与 CI 日志，未独立运行本地环境测试。初次完整 CI 通过后，首轮审查发现 P1 权限问题：仅有 `INVENTORY_READ` 的员工可能通过库存摘要、动态技术属性搜索或 Excel 导出取得受 `PRODUCT_READ` 限制的字段。Codex 在原分支和原 PR 修复，最终复审提交为 [`3a9f6ec1b0f902d6a87cfbedcfcb77de2e74bd87`](https://github.com/LeonEleven/HaxpowerPlatform/commit/3a9f6ec1b0f902d6a87cfbedcfcb77de2e74bd87)。

- 产品与库存读取独立授权。仅有库存读取权时，摘要与搜索限于基础 SKU 字段和授权库存。完整技术属性及技术筛选要求 `PRODUCT_READ`，双权限用户保留完整 AND 搜索。
- Excel 记录实际字段及查询对技术字段的依赖。撤销 `PRODUCT_READ` 后，含技术字段或依赖技术筛选的旧文件拒绝状态查询与下载。合法且仅依赖库存授权的文件仍按其现有权限检查。无法证明字段授权的旧记录须重新生成。
- 最终完整 CI 通过。真实 Cookie HTTP 验证字段隔离、AND 搜索、Excel 字段来源、撤权与完整性，空站、升级站、恢复站各通过 48 项 Frappe 测试。ChatGPT 建议项目负责人手动合并，工程证据限于隔离环境，生产放行仍按验收清单签核。 [最终完整 CI](https://github.com/LeonEleven/HaxpowerPlatform/actions/runs/45931880342) · [隔离验证入口](../dev/v1/README.md#v1-15-目录库存读取与导出验证)

## 业务决策索引

| 状态 | 已确认规则与后续事项 |
| --- | --- |
| B05 已批准 | 非冻结盘点采用 LOCATION、SKU、BUCKET 三级范围版本，过期保留原实盘并重开，经授权可发现原账面为零的新桶。 [盘点决策](DECISIONS.md#8-b05-正式盘点规则2026-11-25) |
| B06 已批准 | 全局审批默认关闭，提交时固化，具权创建者可自审。移库发出和接收分两阶段，接收及剩余退回无需重复审批。已生效纠错始终专项审批，正式期初入账始终专项签核。 [审批决策](DECISIONS.md#9-b06-正式库存审批规则2026-11-25) |
| B07、B08 已批准 | 原行可部分、多次纠错，累计不超实际生效额度。有下游动作（含无差异盘点）进入人工审核。正式业务历史长期保留，采用 ERPNext 原生数量账加 Haxpower 轻量审计。生产备份实施由 B11 完善。 [纠错与留存决策](DECISIONS.md#10-b07-库存纠错与-b08-审计留存2026-11-26) |
| B02、B09 部分已批准 | 产品与库存读取独立，字段、角色矩阵、默认仓库范围及 Excel 独立授权已批准，生成及下载均重验。库存按品牌 SKU 汇总，在途独立。无库存预留，采用动态属性，无辅助数量账，设备原则和多关键词需求已批准。当前 AND 为 V1-15 架构实现，复杂布尔搜索未获业务批准。员工分配、仓库编码、职责及浏览器兼容版本待确认。 [查询与权限决策](DECISIONS.md#11-b02-和-b09-查询权限及功能范围) |
| B01、B03、B10 已批准 | 首批产品配置及统一新编码、严格批次身份及历史审核、切换日实盘和专项签核默认双人及明确授权例外。模板字段、状态机、例外授权主体与机制由后续工程设计。 [正式决策](DECISIONS.md#12-b01-b03-b10-正式业务规则) |
| B04、B11、B12 待确认 | 同仓跨库位与接收岗位、生产运行与备份恢复、正式估值及 GL、补录和关账。V1-17 相关实施前确认期初记账时点、最低估值及正式原生配置。 [业务确认清单](PRODUCTION-READINESS-GAPS.md#业务确认清单) |

## 生产风险与发布验收

| 风险 | 后续验收 |
| --- | --- |
| 正式员工数量写入 HTTP 入口关闭。原生审批执行身份桥接仅限隔离合成环境。 | 正式身份、授权与原生入口专项验收，通过发布验收并明确授权后开放（A10、A12、A19，G06/G07）。 |
| B12 正式估值、GL、补录、关账尚待确认。已接收移库异常及复杂跨单补偿须经单独批准，当前进入人工审核且阻断自动数量变动。 | 验证正式配置及数量影响，签定异常与补偿路径（A09、A14，G04/G08/G11）。 |
| 部分数量操作全量复核历史并持共享库存锁。查询设有 200,000 条原生流水等容量保护。CI 中 192 条真实合成 SLE 是功能及性能基线。 | V1-20 使用多年生产规模的合成 SLE，验证容量保护、持锁时间、吞吐、查询和写入并发以及超时恢复（A13、A15，G05/G06/G09）。 |
| Excel 固定字段和复杂 JSON 属性列仍需改善员工可读性。私有上传、正式业务附件、代理和下载安全尚待验收。 | V1-18/19 验证 Excel 展示与员工流程，V1-16C、V1-20/21 验证上传、撤权和反向代理入口（A12、A16–A18，G06/G09）。 |
| 最高权限破坏原生账时，轻量审计无法保证独立重建完整库存历史。 | B11 落实一致备份、异地存储、权限控制和恢复演练，按同一恢复点还原并逐桶对账（A13、A18–A19，G08/G12）。 |

[生产就绪差距](PRODUCTION-READINESS-GAPS.md) · [发布验收清单](V1-ACCEPTANCE-CHECKLIST.md)

## 协作流程与新会话读取顺序

ChatGPT 与项目负责人澄清业务、分析架构、编写开发任务书。项目负责人确认业务规则及实施授权。Codex 按任务书使用独立任务分支及会话开发、测试、Commit、Push 并创建 Draft PR。ChatGPT 核查实际源码、提交和 CI；补充修复继续在原任务分支及 PR 完成。项目负责人手动合并。GitHub 仓库文档是长期项目事实与业务决策的主要来源，候选方案保持待确认，交接记录保留有日期的证据。

1. 从本交接和 [AGENTS.md](../AGENTS.md) 了解进度及协作约束，获取最新 `origin/main` 并核对当前工作区、相关 PR 和 CI。
2. 阅读 [DECISIONS.md](DECISIONS.md) 和 [生产就绪差距](PRODUCTION-READINESS-GAPS.md) 核对业务批准范围及开放条件。
3. 从 [V1-16 需求及规划](V1-16-REQUIREMENTS.md) 开始新会话，按需查阅 [产品模型](PRODUCT-MODEL.md)、[库存模型](INVENTORY-MODEL.md)、[权限](PERMISSIONS.md) 和 [API 契约](API-CONTRACTS.md)。
4. 规划任务时阅读 [架构决策](ARCHITECTURE-DECISION-RECORD.md)、[适配边界](ERPNEXT-V1-ADAPTATION-BOUNDARIES.md)、[实施计划](V1-IMPLEMENTATION-PLAN.md)、[开发 Backlog](V1-DEVELOPMENT-BACKLOG.md) 和 [验收清单](V1-ACCEPTANCE-CHECKLIST.md)。需要复核既往用例时查阅 [历史交接](PROJECT-HANDOFF-HISTORY.md)。
