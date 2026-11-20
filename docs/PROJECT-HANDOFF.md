# HaxpowerPlatform 项目交接

新 ChatGPT 或 Codex 会话可从这里了解项目背景、决策状态和下一项工作。进度以最新 `main`、实际 PR 状态和验收证据为准。业务确认查阅项目决策及项目方明确结论，工程实施查阅现行计划和对应任务。

[GitHub 仓库](https://github.com/LeonEleven/HaxpowerPlatform) · [项目决策](DECISIONS.md) · [协作规则](../AGENTS.md)

## 1. 项目概况

HaxpowerPlatform 面向船舶配件企业，V1 的业务核心是库存数量管理。当前约有 1 名主要操作员、10 名查询人员，SKU 估计在 1000 种以内。当前约有 3 个仓库，仓库可动态新增，每仓可配置多个库位，V1 无需货架或货格层级。员工通过互联网浏览器访问，公司已有阿里云 Linux 服务器。

主要开发环境为 Windows 11、WSL2 Ubuntu 24.04、Docker Desktop、VSCode 和 Codex。旧系统仅用于理解需求，禁止复用旧源码。V1 用切换日库存快照初始化，完整旧流水迁移不作为前置条件。切换计划包含旧系统只读保留及期初核对。

[产品需求](PRD.md) · [项目决策 D001–D008、D021–D027](DECISIONS.md#1-已确认的业务决策) · [环境检查](ENVIRONMENT-REPORT.md) · [实施计划](V1-IMPLEMENTATION-PLAN.md)

## 2. 当前技术决策

**项目方原则批准 ERPNext v16 + 独立 HaxpowerPlatform Frappe App。** 当前实施采用 API-first 模块化单体，通过受支持的接口和 Hooks 集成。Frappe-only 对照实验保留为技术备选，当前任务以 ERPNext 路线规划。保持 Frappe/ERPNext 核心源码原样。

| 职责 | V1 分工 |
| --- | --- |
| ERPNext | 提供 Item、Warehouse、Batch、Stock Entry、Stock Reconciliation、Serial and Batch Bundle、Stock Ledger Entry（SLE）和 Bin。复用原生在途移库及部分接收，保留日后扩展估值的能力。 |
| Haxpower App | 提供产品分类、技术规格、动态属性、品牌 SKU、受控库存 API、批次规范化和 NO_BATCH 映射、业务角色及仓库授权、多行单据、审批、纠错、轻量操作日志、查询、报表、导出和浏览器界面。 |

Stage 3A 的实施基线以 ERPNext 原生有效 SLE 和已提交 Bundle 提供数量事实，以 Bin 核对原生库位汇总。Haxpower 的轻量日志记录操作者、时间、原因、请求和原生单据行关联。纠错使用新增反向业务单据并保留原始证据，普通业务原生取消入口须关闭。ERPNext 内部估值、后台重估与关闭期间仍须验证其数量影响。

Stage 2B 的单行纠错、事务回滚和部分权限实测支持上述基线。正式多行日志结构、幂等请求表、事务锁、盘点版本和环境路径属于设计提案，仍需在正式隔离环境验证。生产放行以验收清单及业务、工程和运维签核为准。

[架构决策](ARCHITECTURE-DECISION-RECORD.md) · [ERPNext 适配边界](ERPNEXT-V1-ADAPTATION-BOUNDARIES.md) · [路线对照](TECHNICAL-ROUTE-COMPARISON.md) · [V1 实施计划](V1-IMPLEMENTATION-PLAN.md) · [验收清单](V1-ACCEPTANCE-CHECKLIST.md)

## 3. 关键业务规则

### 已确认的业务决策与当前事实

| 决策编号 | 内容 |
| --- | --- |
| D001–D005、D026 | 项目名为 HaxpowerPlatform，V1 以库存数量为核心，旧源码仅供参考。采购订单、销售订单、库存价值和成本、财务等功能留作扩展。旧系统的价格、折扣率等 V1 不保留。 |
| D006–D008、D021–D025 | 仓库数量可配置，每仓多个库位，V1 无需货架或货格。人员数量、现有仓库、浏览器访问和已有阿里云服务器见项目概况。D023 的 SKU 数量为当前估计。 |
| D009–D011、D013–D014 | 批次自由文本且可空，同一 SKU 下相同批次号复用，同一 SKU 可有实批次和无批次库存。禁止负库存，数量通常为整数，部分单位可用小数。 |
| D016、D018–D020 | 移库必须发出和接收两次确认。审批提供全局开关，有审批权的创建者可自审。入库、出库无需额外拣货复核。 |
| D027–D028 | V1 以切换日快照初始化。普通查询人员目前可查询全部三个仓库，这是当前授权策略。 |

[项目决策 D001–D029](DECISIONS.md#1-已确认的业务决策)

### V1 设计基线与待确认事项

- **产品。** 分类、技术规格与品牌库存 SKU 分层，同规格可关联多个品牌 SKU，不同品牌默认分立。模板可配置，支持 100 个以上候选选项。分层在 PRD 草案和产品模型中有设计，待 PRD 评审确认。属性选项变更保留既有值，主动编辑须审计，禁止自动合并产品。首批模板及具体多选属性见 B01。
- **批次与数量。** 出库、移库、盘点须明确提交 `BATCH` 或 `NO_BATCH`（D012）；缺选择应拒绝，对外无批次返回 `null`，适配层解析为该 SKU 专用的系统 Batch。禁止跨批次补足，禁止泄漏内部系统 Batch ID。批次输入须校验空字符串、纯空白及疑似空引号，避免生成伪批次。Stage 2B 的大小写、去边、长度和异常处理是实验候选，正式判定及历史例外见 B03。数量用 Decimal/Numeric，按单位配置精度（D015），正式精度表见 B01。
- **移库。** 保留批次，发出进入在途，接收后进入目标库位。部分接收及余量留在途是 D017 的流程设计，PRD 草案已纳入，Stage 1D 已验证所列样例。部分退回也有实验样例。同仓跨库位、接收岗位和下游补偿见 B04、B07。
- **权限。** 操作权限与仓库范围独立，服务端默认拒绝并校验查询、详情、报表、导出和写操作，移库校验两侧。D029 的 ALL/SELECTED/NONE 为拟采用方案，具体授权及可见字段见 B02。审批开关默认配置、处理中切换和相关动作的审批见 B06。

B01–B12 均待确认。盘点冻结与截止、预留需求、留存、切换和生产运行条件也在清单中追踪。实验实现提供评审证据，业务结论须经项目方确认并录入决策记录。

[产品模型](PRODUCT-MODEL.md) · [库存模型](INVENTORY-MODEL.md) · [批次输入规范](ERPNEXT-V1-ADAPTATION-BOUNDARIES.md#批次输入规范) · [业务确认清单](PRODUCTION-READINESS-GAPS.md#业务确认清单)

## 4. 历史技术验证

下表中的实测来自各阶段锁定环境的实验报告。正式开发和生产验收使用 V1 清单逐项复核。

| 阶段 | 实测或规划及边界 | 证据 |
| --- | --- | --- |
| Stage 0 | 在 WSL、Docker Desktop 上验证独立 Compose、Frappe/ERPNext 建站、网页和 API；复核静态资源。成果是技术实验环境。 | [环境检查](ENVIRONMENT-REPORT.md) · [容器基线](STAGE0-BOOTSTRAP-REPORT.md) · [建站](STAGE0-ERPNEXT-INSTALL-REPORT.md) · [静态资源](STAGE0-ASSET-REPAIR-REPORT.md) |
| Stage 1 | 实测 ERPNext 原生批次、数量链路和估值行为，数量场景的 GL 结果限于报告中的样本配置。 | [原生批次与估值](STAGE1-BATCH-VALUATION-REPORT.md) · [PR #5](https://github.com/LeonEleven/HaxpowerPlatform/pull/5) |
| Stage 1B | 实测按 SKU 的 NO_BATCH 系统映射及有批次、无批次精确收发；正式报表、导出与期初另验。 | [无批次报告](STAGE1B-NO-BATCH-ADAPTER-REPORT.md) · [PR #6](https://github.com/LeonEleven/HaxpowerPlatform/pull/6) |
| Stage 1C | 实测专用角色、叶仓授权及原生访问边界，发现额外原生库存角色会扩大可见性；动态仓库和全部正式入口待验。 | [权限报告](STAGE1C-PERMISSION-BOUNDARY-REPORT.md) · [PR #7](https://github.com/LeonEleven/HaxpowerPlatform/pull/7) |
| Stage 1D | 实测原生关联的发出、在途和多次接收，有批次及无批次均有样例；正式多行并发、权限及异常流程待验。 | [移库报告](STAGE1D-TRANSFER-VALIDATION-REPORT.md) · [PR #8](https://github.com/LeonEleven/HaxpowerPlatform/pull/8) |
| Stage 1E | 实测按批次盘点、并发扣减、部分接收、精度和回滚。发现原生取消会改写历史 SLE、解除原 Bundle 关联，缺截止版本会让旧盘点覆盖结果。 | [库存完整性报告](STAGE1E-INVENTORY-INTEGRITY-REPORT.md) · [PR #9](https://github.com/LeonEleven/HaxpowerPlatform/pull/9) |
| Stage 1F | 实测完整业务事件同事务、反向纠错及剩余在途退回；用原生流水和业务事件校验基线并拒绝旧盘点。冻结、跨范围截止及轻量版本模型待验。 | [审计与盘点报告](STAGE1F-AUDIT-COUNT-REPORT.md) · [PR #10](https://github.com/LeonEleven/HaxpowerPlatform/pull/10) |
| Stage 2A | 在独立站点实测 Frappe-only 的真实 NULL、并发、数量事务、盘点、移库及部分权限。报告的路线建议为当时技术对照，正式扩展和恢复未验。 | [Frappe-only 报告](STAGE2A-FRAPPE-ONLY-SPIKE-REPORT.md) · [PR #11](https://github.com/LeonEleven/HaxpowerPlatform/pull/11) |
| Stage 2B | 实测 ERPNext 原生有效数量账、轻量日志、反向单据、幂等及故障回滚。批次候选规范经过 HTTP 和清洗预览验证；多行、全面后台隔离、备份恢复和下游补偿仍待验。 | [轻量审计报告](STAGE2B-LIGHTWEIGHT-AUDIT-REPORT.md) · [PR #12](https://github.com/LeonEleven/HaxpowerPlatform/pull/12) |
| Stage 3A | 形成正式 V1 工程隔离、实施 Backlog、验收门槛和长期交接，属于设计规划。 | [实施计划](V1-IMPLEMENTATION-PLAN.md) · [PR #13](https://github.com/LeonEleven/HaxpowerPlatform/pull/13) |

## 5. 开发环境与源码隔离

- GitHub 的 `LeonEleven/HaxpowerPlatform` 为唯一协作代码仓库。正式 App 源码、版本锁、配置模板和测试由本仓库管理；Frappe 和 ERPNext 上游源码由独立 Bench 或构建依赖获取。
- `dev/erpnext-lab/` 是技术实验，含已命名为 `haxpower_platform` 的原型。保留该原型、历史实验站点及 Frappe-only 对照。已有实验代码和脱敏证据用于复核，正式代码按业务契约独立实现。
- 正式 App 为 `haxpower_v1`，源码位于 `apps/haxpower_v1/`，正式开发环境由 `dev/v1/` 管理。正式开发与 CI 使用独立 Bench、Python 环境、站点、数据库、Redis、worker、网络和卷，端口与实验环境隔离。
- V1-01 的版本锁记录了 Frappe/ERPNext v16.55.0 的官方 tag 和对应提交、MariaDB 11.8.9 及 Redis 7.4.12 的镜像摘要和 Bench 工具。依据本节的本地与 CI 证据核对安装及迁移。
- 禁止将实验数据、数据库、密钥或站点凭据复制到正式环境。历史报告中的本地目录、站点名和端口均为该次开发参考，具体机器以其配置为准。

[正式开发说明](../dev/v1/README.md) · [版本锁](../dev/v1/versions.lock.json) · [实验目录](../dev/erpnext-lab/README.md) · [正式隔离方案](V1-IMPLEMENTATION-PLAN.md#2-正式工程隔离和迁移)

## 6. 当前项目进度

**状态快照，2026-11-20（Asia/Shanghai）。** 开工时 GitHub 确认 Stage 3A PR #13 已合并，重新 fetch 后的 `origin/main` 为 `3908c9aec0c5eda1d3f0d41c4ee18a70074b3038`。V1-01 的 Draft PR #14 在 `feat/v1-01-bootstrap` 实现正式 App、独立 Compose、合成迁移演练及 PR CI。

| 文档 | 当前范围 |
| --- | --- |
| [开发 Backlog](V1-DEVELOPMENT-BACKLOG.md) | V1-01 至 V1-22，共 22 项。 |
| [验收清单](V1-ACCEPTANCE-CHECKLIST.md) | A01 至 A19，共 19 项待正式验收。 |
| [生产就绪差距](PRODUCTION-READINESS-GAPS.md) | G01 至 G12 待关闭，B01 至 B12 待业务确认。 |

- **A01 本地证据。** WSL2 的 Docker 启动正式项目并核对镜像、上游提交、导入来源及现场数据库用户 `hpv1_dev`。11 项预检测试在 Python 正常和优化模式通过，并实测占用端口、实验项目和站点覆盖被拒绝。第二个工作区独立准备同一套锁定依赖。开发站通过 Web 和 Socket.IO 回环烟测及非法 Host 拒绝检查，worker 和 scheduler 实际运行。
- **A02 本地证据。** `haxpower-v1-dev.localhost` 从空站实际安装 ERPNext 与 `haxpower_v1`，两次 `bench migrate` 和 2 项 Frappe 集成测试完成。第二工作区空站安装及重复迁移通过，按表结构、样例记录和补丁日志比较的 SHA-256 均为 `3925938fe7bf3e3791781b8928b3f7f8f217af8a5dd5d674acd1256ae4c918fb`。合成前版本恢复和 PR CI 证据继续核对。A01/A02 和 G01 的正式签核保留至发布候选。

下一任务为 **V1-02 分类、属性与规格**。开始前复核 V1-01 的真实 CI 与独立工作区安装、迁移和恢复证据。B01–B12 保持待确认。

每次完成重要开发阶段时更新此节，记录日期、对应 PR 或提交、实际完成范围、验收证据、未决项及下一任务。PR 状态是带日期的核对结果，下一会话重新查询。

[V1-01](V1-DEVELOPMENT-BACKLOG.md#v1-01-正式-app-骨架及开发基线) · [Draft PR #14 及验收证据](https://github.com/LeonEleven/HaxpowerPlatform/pull/14) · [PR #13](https://github.com/LeonEleven/HaxpowerPlatform/pull/13)

## 7. 不可破坏的技术约束

- 通过独立 App 和受支持接口集成，禁止修改 Frappe/ERPNext 核心源码。
- 库存数量仅经正式业务服务和原生单据变动。严格区分无批次与未选择，禁止直接修改库存底表或从客户端直接访问数据库。
- 单据、流水、余额和操作记录须事务一致。测试覆盖幂等、并发、拒绝无副作用、权限及逐桶对账。正式库存流水和操作证据须保留，纠错追加新单。
- 普通用户使用受控接口，服务端对操作和仓库范围授权。原生库存读写、取消、导入、报表及后台路径必须纳入权限验收。对外 API 不公开原生库存结构及系统 Batch ID。
- 使用专属分支，禁止直接向 `main` 推送、强制推送或自动合并 PR。禁止提交密钥、密码、Token、真实业务数据或数据库备份到 GitHub。
- 未经批准不得访问或修改阿里云生产环境，已授权的生产操作前须备份。

[协作规则](../AGENTS.md) · [原生入口限制](ERPNEXT-V1-ADAPTATION-BOUNDARIES.md#入口限制) · [V1 验收清单](V1-ACCEPTANCE-CHECKLIST.md)

## 8. 新会话读取顺序

1. [项目交接](PROJECT-HANDOFF.md)
2. [AGENTS.md](../AGENTS.md)
3. [项目决策](DECISIONS.md)
4. [架构决策记录](ARCHITECTURE-DECISION-RECORD.md)
5. [V1 实施计划](V1-IMPLEMENTATION-PLAN.md)
6. [V1 开发 Backlog](V1-DEVELOPMENT-BACKLOG.md)
7. 按任务阅读设计文档，例如 [产品模型](PRODUCT-MODEL.md)、[库存模型](INVENTORY-MODEL.md)、[权限](PERMISSIONS.md)、[业务流程](BUSINESS-FLOWS.md) 或 [API 契约](API-CONTRACTS.md)。
8. 深入分析时按[历史技术验证](#4-历史技术验证)定位实验报告。

开始工作前获取最新 `origin/main`，核对当前工作区和相关 PR 的实际状态、分支及提交，区分已合并工作、审查中的改动和待开工任务。交接快照用于定位证据，进度以 Git 和 GitHub 的最新记录为准。
