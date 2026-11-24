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

**状态快照，2026-11-23（Asia/Shanghai）。** V1-01 PR #14、V1-02 PR #15 和 V1-03 PR #16 已合并。V1-03 交付品牌档案、独立 SKU 和 ERPNext Item/UOM 映射。V1-04 从 PR #16 的合并提交 `65e1992bed4a5ddaaf19d2096449ecf2b138055e` 开始，在 PR #17 交付仓库、库位和原生叶仓映射。LeonEleven 于 2026-11-23 09:17 合并，基线提交为 `cc5dbd3ed3e62d6990dd06ab3ef021cfa3d2d582`。

| 文档 | 当前范围 |
| --- | --- |
| [开发 Backlog](V1-DEVELOPMENT-BACKLOG.md) | V1-01 至 V1-22，共 22 项。 |
| [验收清单](V1-ACCEPTANCE-CHECKLIST.md) | A01 至 A19，共 19 项待正式验收。 |
| [生产就绪差距](PRODUCTION-READINESS-GAPS.md) | G01 至 G12 待关闭，B01 至 B12 待业务确认。 |

- **A01 本地证据。** WSL2 的 Docker 启动正式项目并核对镜像、上游提交、导入来源及现场数据库用户 `hpv1_dev`。11 项预检测试在 Python 正常和优化模式通过，并实测占用端口、实验项目和站点覆盖被拒绝。第二个工作区独立准备同一套锁定依赖。开发站通过 Web 和 Socket.IO 回环烟测及非法 Host 拒绝检查，worker 和 scheduler 实际运行。
- **A02 本地证据。** `haxpower-v1-dev.localhost` 从空站实际安装 ERPNext 与 `haxpower_v1`，两次 `bench migrate` 和 2 项 Frappe 集成测试完成。第二工作区空站安装及重复迁移通过，按表结构、样例记录和补丁日志比较的 SHA-256 均为 `3925938fe7bf3e3791781b8928b3f7f8f217af8a5dd5d674acd1256ae4c918fb`。另一个隔离站点从合成 0.1.0 升级，实测后置补丁失败时新增列和已提交补丁仍在。恢复数据库、站点配置和公私文件后，样例与故障前摘要 `b79dd23bf89309cd0e95c0699dc412d348855c14f124938b91a8466c39998e6f` 一致。重试并复跑后，1 条既有记录和 1 条补丁日志的摘要保持 `efffda3c97d7dafb67a6b08f97e8276a54b1a60b0a398334111af84301b3b8ee`。本地临时项目经归属校验完成清理，实验容器、网络和卷与启动前快照一致。
- **V1-02 本地工程证据。** 核对 Bench 副本与 checkout 的文件内容后，Frappe/ERPNext v16.55.0、MariaDB 11.8.9 的隔离空站通过 11 项目录测试和 2 项 V1-01 回归。覆盖 105 个候选选项、精确 Decimal、停用值保留、数据库唯一键及审计故障回滚。已登录的合成员工通过 8 项原生 REST v1/v2 和 `frappe.client` 写入拒绝检查，七张目录表无副作用。Web、Socket.IO、非法 Host 拒绝、worker 和 scheduler 的回环烟测通过。
- **V1-02 迁移证据。** 空站安装和重跑迁移的数据及表结构摘要一致，SHA-256 为 `04468ba2b9783c80c50bf94dcb705c3d15a039b91c204f4e9747a8b7a5b1baee`。带既有合成记录的 0.1.0 站验证了迁移故障及同一备份点的数据库、站点配置和公私文件恢复。升级至 0.3.0 后，既有记录和补丁历史保留，包含停用选项、规格属性与审计的 9 条合成目录记录在两次重跑后摘要保持 `f93886f1c99cf6b043d02453fc6863813b76021d2769b7cbc7fde27dd1ee6fa1`。升级站用当前代码复跑 13 项 Frappe 集成测试通过。
- **V1-03 隔离验证。** Frappe/ERPNext v16.55.0 和 MariaDB 11.8.9 上通过 21 项 Frappe 测试、两组独立连接争用、17 项认证 HTTP 拒绝、两项原生读取隐藏映射标记，以及 Web、Socket.IO、worker 和 scheduler 烟测。同规格的两个品牌 SKU 各自拥有稳定领域 ID 和独立原生 Item。
- **V1-03 迁移证据。** 合成规格历史、SKU 及原生 Item 映射在本地站点两次 migrate 后快照一致，SHA-256 为 `bf17f66f3f7af89eb363ef97041e39525fe4750f7d6c6e5ad0f6ec33e38ae736`。空站安装、固定 V1-02 应用升级及旧版故障恢复的 CI 记录见 PR #16。

V1-03 沿用 `catalog.service.get_specification(specification_id)` 的稳定 ID 和维护权限。A03 的授权库存摘要与带批次的 SKU 独立扣减由后续库存、V1-15 和 V1-20 衔接。B01 的真实模板、无品牌建档、单位精度和数量上限，以及 B02 的员工字段和仓库范围继续待确认。SKU 停用保持阻断，待 V1-07/V1-09 接入完整库存引用和写入协调。

**V1-04 隔离验证。** 锁定 Frappe/ERPNext v16.55.0 和 MariaDB 11.8.9 上通过 30 项 Frappe 集成测试，含 21 项既有回归。验证 W1–W4、八个物理叶仓、同公司映射、数据库唯一键、失败回滚、真实原生草稿引用及在途保护。两个独立 MariaDB 连接竞争同一叶仓仅提交一份映射，第三个事务复核。12 项真实鉴权 HTTP 拒绝后映射和库存快照一致。

**V1-04 迁移证据。** 空站结构与重跑快照一致。锁定 V1-03 版本的合成规格、审计、品牌、SKU 和 Item 映射从同一备份集恢复后重试升级，原记录和稳定 ID 可复核。升级站增加仓库、库位和在途后，两次 migrate 的本地快照 SHA-256 均为 `b1c35d23a4f6008486e9407589178e6fc3997dd6a31b2254a8930a637569d91c`。远端实测与脱敏日志见 PR #17 的 CI 检查。

V1-04 的仓库和库位使用稳定 ID。每个库位独占同公司 ERPNext 原生叶仓，系统在途单独配置并从普通选择中排除。停用保持安全拒绝，等待库存引用及写入协调。B02 仍待确认。V1-05 从 V1-04 的合并提交开始实现仓库授权和原生入口守卫。在途发出、接收、部分接收与退回由 V1-10/V1-11 接入。

[V1-04 PR #17](https://github.com/LeonEleven/HaxpowerPlatform/pull/17) · [PR #17 CI](https://github.com/LeonEleven/HaxpowerPlatform/pull/17/checks) · [V1-04 Backlog](V1-DEVELOPMENT-BACKLOG.md#v1-04-仓库与库位映射) · [V1-04 技术设计](TECHNICAL-DESIGN.md#v1-04-仓库库位与原生叶仓)

**V1-05 权限基础。** 技术能力映射为未分配的 Frappe 角色，员工范围按所属公司和稳定领域 ID
计算。受控读取返回最小投影，Frappe 认证后的入口清单及 ORM 守卫保护受管原生资源。
本地锁定环境的空站及合成升级站各通过 39 项 Frappe 测试，含 30 项既有回归。真实 Cookie
会话矩阵通过 94 项检查，其中 57 项拒绝。验证拒绝前后的领域、原生映射、库存及审计快照，
并以独立 MariaDB 连接验证 SKU、叶仓和授权配置的并发一致性。
升级从已合并的 V1-04 开始，注入故障、恢复数据库及公私文件后保留已有规格审计、SKU、Item、
仓库、库位和在途映射。升级站两次迁移复跑快照一致。Web、Socket.IO、非法 Host、
worker 和 scheduler 烟测通过。B02、B04、B11 仍待确认。项目负责人 LeonEleven 于
2026-11-23 10:20（Asia/Shanghai）合并 PR #18，合并提交为
`36d77f9ae6b5bee17a74f23b689996632748c644`。

[V1-05 PR #18](https://github.com/LeonEleven/HaxpowerPlatform/pull/18) · [PR #18 CI](https://github.com/LeonEleven/HaxpowerPlatform/pull/18/checks) · [权限设计](PERMISSIONS.md#10-v1-05-技术机制) · [入口矩阵](ERPNEXT-V1-ADAPTATION-BOUNDARIES.md#v1-05-原生入口矩阵) · [V1-05 验证](../dev/v1/README.md#v1-05-权限验证)

**V1-06 批次映射，2026-11-23（Asia/Shanghai）。** LeonEleven 于 11:11 手动合并 PR #19，合并提交为
`ea015de74fd6c6201425c2e71a575bd04b39ac17`。从 V1-05 合并提交开始实现三态选择、逐 SKU 的
实批次和无批次映射、原子启用 Item 批次、原生 Batch 权限和管理员诊断。App 版本为
0.7.0。锁定环境空站通过 47 项 Frappe 测试，含 39 项既有回归。B03 候选版本只用于合成数据。
V1-07 在共用库存事务中接入批次、操作权限和仓库范围。
正式收发的 Batch/Bundle 记账及逐桶数量验收由 V1-08/V1-09 继续完成。

[V1-06 PR #19](https://github.com/LeonEleven/HaxpowerPlatform/pull/19) · [PR #19 CI](https://github.com/LeonEleven/HaxpowerPlatform/pull/19/checks) · [V1-06 开发验证](../dev/v1/README.md#v1-06-批次验证) · [批次服务](TECHNICAL-DESIGN.md#v1-06-批次映射和事务)

**V1-07 库存执行内核，2026-11-23（Asia/Shanghai）。** 从 PR #19 的已合并提交开始。App 0.8.0
包含稳定多行身份、精确桶合计、当前读、一次提交的事务边界、授权后重放与轻量原生关联。
真实数量仍由 ERPNext 维护。LeonEleven 于 14:37 手动合并 PR #20，提交为
`d48e97115bb80acfae8b29cbef17ff18d5eaba7a`。B01、B03、B08 待确认。

本地隔离 MariaDB 11.8.9 验证四组双连接争用、旧读视图、真实锁等待超时、提交前断连和提交后
回包丢失。新连接核对 11 个已提交请求及 82 条原生替身关联。114 项真实 Cookie 会话检查通过，
其中新增 6 项库存单据、请求、日志或内部执行入口拒绝。连续两次迁移、故障恢复及恢复后迁移的
合成操作快照一致，SHA-256 为 `1af3cdaed67bb4262fb27c7b4b0ee6b1437c86fe5438ceefff964584381f9d07`。
通过同一备份点恢复数据库、站点配置和公私文件。空站、升级站和恢复站均通过 47 项 Frappe 回归。
恢复后再次执行并发及故障探针，核对累计 22 个已提交请求和 164 条原生替身关联。Web、Socket.IO、
非法 Host 拒绝、worker 和 scheduler 烟测通过。专属隔离项目资源已验证归属并清理。

[V1-07 PR #20](https://github.com/LeonEleven/HaxpowerPlatform/pull/20) · [PR #20 CI](https://github.com/LeonEleven/HaxpowerPlatform/pull/20/checks) · [V1-07 执行内核](TECHNICAL-DESIGN.md#v1-07-库存执行内核) · [隔离验收路径](../dev/v1/README.md#v1-07-库存事务验证)

**V1-08 多行入库，2026-11-23（Asia/Shanghai）。** App 0.9.0 从已合并的 PR #20 开始，
实现显式批次与无批次的收货草稿、一次提交边界、原生 Material Receipt 与逐行业务关联。
在锁定的隔离环境跟踪每条 Detail、Bundle 和有效 SLE，按域 SKU、物理库位和批次对账并复核 Bin。
在第一条原生流水前检查 ERPNext 的显式精度，避免原生累计余额按两位失配。
已核对六行、五桶及零估值样本的零 GL 结果。独立永续库存候选以 2 单位、4.25 单价生成
借贷各 8.50 的两条 GL。双连接同键和异键竞争、原生故障回滚、断连恢复和 47 项既有
Frappe 测试通过。空站含原生库存的重复迁移快照一致，完整升级与恢复证据查看 PR 的检查。
真实员工入库等待 B01、B03、B12 放行。V1-09 从 V1-08 审查和手动合并后的提交复用
事务、权限和行关联，并接入原生 Material Issue、精确扣减及可用量校验。

[V1-08 PR #21](https://github.com/LeonEleven/HaxpowerPlatform/pull/21) · [PR #21 CI](https://github.com/LeonEleven/HaxpowerPlatform/pull/21/checks) · [入库技术设计](TECHNICAL-DESIGN.md#v1-08-多行-material-receipt) · [入库验收路径](../dev/v1/README.md#v1-08-多行入库验证)

**V1-09 多行出库，2026-11-24 核对（Asia/Shanghai）。** LeonEleven 于 2026-11-23 16:42 手动合并
V1-08 PR #21，合并提交为 `841e3df4f046a44f63eb4787a1b8cee3c1a3d491`。App 0.10.0 从该
基线接入多行 Material Issue、精确可用量、两种明确批次选择、共用库存锁及原生逐行核对。
空站锁定环境通过 47 项 Frappe 回归、V1-08 原生入库和 V1-09 实际出库。独立连接核对
同 SKU 的 STD61、STD62、NO_BATCH，以及不同品牌和库位的数量隔离。两个写连接各读到
10 件后争用扣 7 与 6，READ COMMITTED 和保留旧读视图的 REPEATABLE READ 试验均只有
一笔生效。验证了 `1.125 − 0.125 = 1.000`、原生及日志故障回滚、COMMIT 前断连和
响应丢失重放。隔离永续库存样例扣 1 单位、估值减少 4.25，GL 借贷各 4.25。关闭期间、
库存冻结和 GL 拒绝后状态一致，估值重放保持数量。

本地从已合并的 V1-08 构造带真实入库的升级站，升级前后的快照 SHA-256 同为
`48b1ef5f41615d0cb86ec2130c1d29527b8ed17378d40a85c8fa48a2f605629e`。
新出库后两次 migrate 的快照 SHA-256 同为
`59468fa5858e3a6f22750b918083f6aca6451501051140df3439fe1f00157a40`。
从同一备份点恢复数据库、站点配置及公私文件后，重新逐桶核对并完成出库。空站、升级站和
恢复站各通过 47 项 Frappe 回归。121 项真实 Cookie 会话检查覆盖原生出库 REST/Desk
拒绝和内部出库 HTTP 入口。Web、Socket.IO、非法 Host、worker 和 scheduler 烟测通过。

真实员工写入等待 B01、B03、B12 及业务授权。B08 和 B09 待业务决定。LeonEleven 于
2026-11-24 09:38（Asia/Shanghai）手动合并 PR #22，合并提交为
`b6a56e39c40bdd550d505c8cdf6507f45bc6142e`。V1-10 从该提交开始。

[V1-09 PR #22](https://github.com/LeonEleven/HaxpowerPlatform/pull/22) · [PR #22 CI](https://github.com/LeonEleven/HaxpowerPlatform/pull/22/checks) · [V1-09 技术设计](TECHNICAL-DESIGN.md#v1-09-多行-material-issue) · [出库验收路径](../dev/v1/README.md#v1-09-多行出库验证)

**V1-10 移库发出，2026-11-24 核对（Asia/Shanghai）。** App 0.11.0 接入多行草稿、
源和目标仓库双侧授权、原生 Material Transfer 和逐原行在途归属。发出 Detail 关联源
Outward 与 Transit Inward 两条有效流水，原目标库位固化供接收使用。空站 47 项 Frappe
回归、精确两腿及跨单归属对账、双侧授权、独立连接争用和断连恢复已实测通过。独立永续库存
候选生成 4.25 的双向库存账户 GL 后触发完整回滚。B04 同仓策略及接收岗位、
B01、B03、B08 和 B12 保持待确认，发出仅在隔离合成环境启用。下一项为 V1-11。由项目负责人
审查并手动合并 V1-10 后，接入逐原行部分接收与剩余退回。

[V1-10 Draft PR #23](https://github.com/LeonEleven/HaxpowerPlatform/pull/23) · [PR #23 CI](https://github.com/LeonEleven/HaxpowerPlatform/pull/23/checks) · [V1-10 技术设计](TECHNICAL-DESIGN.md#v1-10-两阶段移库发出) · [隔离验收](../dev/v1/README.md#v1-10-两阶段移库发出验证)

每次完成重要开发阶段时更新此节，记录日期、对应 PR 或提交、实际完成范围、验收证据、未决项及下一任务。PR 状态是带日期的核对结果，下一会话重新查询。

[V1-03](V1-DEVELOPMENT-BACKLOG.md#v1-03-品牌-sku-与原生-item-映射) · [V1-03 PR #16](https://github.com/LeonEleven/HaxpowerPlatform/pull/16) · [PR #16 CI](https://github.com/LeonEleven/HaxpowerPlatform/pull/16/checks) · [V1-02 PR #15](https://github.com/LeonEleven/HaxpowerPlatform/pull/15) · [PR #15 CI](https://github.com/LeonEleven/HaxpowerPlatform/pull/15/checks) · [V1-01 PR #14](https://github.com/LeonEleven/HaxpowerPlatform/pull/14) · [PR #14 CI](https://github.com/LeonEleven/HaxpowerPlatform/pull/14/checks)

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
