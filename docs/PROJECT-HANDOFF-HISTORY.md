# HaxpowerPlatform 历史交接与工程证据

本档案保留各阶段当时记录的验收范围、故障与恢复、PR、CI 和未决事项。文中的 Draft、待确认、等待 CI、下一任务及 main 均指各记录当时的状态。当前合并状态、决策和后续任务见 [项目交接](PROJECT-HANDOFF.md)；PR #28 最终复审见 [V1-15 最终架构评审](PROJECT-HANDOFF.md#v1-15-最终架构评审)。

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

## V1-01 至 V1-15 历次交接证据

**状态快照，2026-11-23（Asia/Shanghai）。** V1-01 PR #14、V1-02 PR #15 和 V1-03 PR #16 已合并。V1-03 交付品牌档案、独立 SKU 和 ERPNext Item/UOM 映射。V1-04 从 PR #16 的合并提交 `65e1992bed4a5ddaaf19d2096449ecf2b138055e` 开始，在 PR #17 交付仓库、库位和原生叶仓映射。LeonEleven 于 2026-11-23 09:17 合并，基线提交为 `cc5dbd3ed3e62d6990dd06ab3ef021cfa3d2d582`。

| 文档 | 当前范围 |
| --- | --- |
| [开发 Backlog](V1-DEVELOPMENT-BACKLOG.md) | V1-01 至 V1-22，共 22 项。 |
| [验收清单](V1-ACCEPTANCE-CHECKLIST.md) | A01 至 A19，共 19 项待正式验收。 |
| [生产就绪差距](PRODUCTION-READINESS-GAPS.md) | G01 至 G12 待关闭；B05 已批准，其余 B 项未决部分待业务确认。 |

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
审查后，LeonEleven 于 10:46 手动合并 PR #23，合并提交为
`a6d2bc4da1011922c78ff9ac06e88849e71695c0`。V1-11 从该提交开始。

[V1-10 PR #23](https://github.com/LeonEleven/HaxpowerPlatform/pull/23) · [PR #23 CI](https://github.com/LeonEleven/HaxpowerPlatform/pull/23/checks) · [V1-10 技术设计](TECHNICAL-DESIGN.md#v1-10-两阶段移库发出) · [隔离验收](../dev/v1/README.md#v1-10-两阶段移库发出验证)

**V1-11 部分接收与返源，2026-11-25 核对（Asia/Shanghai）。** App 0.12.0 从已合并的
V1-10 开始。支持逐原行部分、多次及多行接收、剩余多次退回，以及累计量和含退回的关闭结果。
接收使用原生 End Transit，返源追加独立 Material Transfer。原发出仍是单据的生效操作。
后续动作各自推进 revision，已提交请求按当时结果及当前双侧授权重放。

锁定合成站验证发 10、接 6、退 4，分三次接收 3、2、5，实批次与 NO_BATCH 混合，
同桶跨单归属、精确小数和同一业务动作跨两个原生接收单的原子性。三组双写争用、固定旧读
视图、接收/退回重放、权限撤销和断连恢复通过独立连接核对。接收、返源遇关闭期间、库存
冻结或 4.25 借贷原生 GL 均回滚。正式员工数量写入继续关闭，B01、B03、B04、B06、B07、
B08 和 B12 保持待确认。含原生库存的合成站连续两次 migrate，完整快照 SHA-256 均为
`fc0603c77fa796a9dd1e9e6d1206ad526180093ad631dbe38519ff1d9857bbfe`。

升级站在已合并的 V1-10 App 上生成两笔真实发出，升级时核对原单、归属、原生流水及请求。
V1-11 对历史移库部分接收和返源，保留已关闭与仍有在途各一笔。重复迁移及迁移故障后的
同备份集恢复通过，数据库、站点配置和公私文件均已核对。升级站的完整快照 SHA-256 为
`538816a3591007d985a254dc2edd3483b88e9e6714b98d57f0ac5dd517291a1d`。
恢复后原单剩余 4 件返源并关闭，历史发出请求仍返回原 revision 2。升级和恢复均通过 47 项
Frappe 回归，真实 Cookie 会话矩阵通过 128 项检查。
V1-11 经审查及手动合并后进入 V1-12 盘点版本集成。

[V1-11 任务分支](https://github.com/LeonEleven/HaxpowerPlatform/tree/feat/v1-11-transfer-receipt-return) · [V1-11 PR](https://github.com/LeonEleven/HaxpowerPlatform/pulls?q=is%3Apr+head%3Afeat%2Fv1-11-transfer-receipt-return) · [验证入口](../dev/v1/README.md#v1-11-部分接收及返源验证)

[V1-03](V1-DEVELOPMENT-BACKLOG.md#v1-03-品牌-sku-与原生-item-映射) · [V1-03 PR #16](https://github.com/LeonEleven/HaxpowerPlatform/pull/16) · [PR #16 CI](https://github.com/LeonEleven/HaxpowerPlatform/pull/16/checks) · [V1-02 PR #15](https://github.com/LeonEleven/HaxpowerPlatform/pull/15) · [PR #15 CI](https://github.com/LeonEleven/HaxpowerPlatform/pull/15/checks) · [V1-01 PR #14](https://github.com/LeonEleven/HaxpowerPlatform/pull/14) · [PR #14 CI](https://github.com/LeonEleven/HaxpowerPlatform/pull/14/checks)

**V1-12 PR #25 已合并，2026-11-25（Asia/Shanghai）。** 本次核对 V1-11 PR #24 已由
LeonEleven 于 09:54 手动合并，main 为 `2c8f42970112176b61e198081b82e084688c7649`。
V1-12 专属分支 `feat/v1-12-stock-count-versioning` 的 App 0.13.0 实现 LOCATION / SKU / BUCKET、
服务端快照、实盘显式确认与零、新发现桶、差异与无差异完成、版本陈旧拒绝及追加重盘。
B05-01–04 的正式批准日期和来源已写入 DECISIONS 第 8 节，其他 B 项不因此获批。

统一执行内核从实际有效 SLE / Bundle 与轻量关联提取数量动作，同事务推进库位、SKU 和桶版本，
包含移库源、Transit、目标两腿。安装/迁移在共享守卫及原生对账后初始化 generation，重复迁移不重置；
不连续的旧任务必须重盘。原生盘点使用锁定版 Stock Reconciliation，逐桶核验全部有效流水的净差。
无差异完成明确记录无数量动作，原生引用检查只对这条受控路径按真实差异集合执行。

本地隔离合成站已通过既有真实入库、并发、故障和估值回归；盘点主矩阵通过三种范围、混合批次/品牌、
实盘零、账面零盘盈、发现桶、无差异、无关范围变化、数量往返过期、重盘、双连接竞争、幂等/撤权、
日志/版本/状态/请求故障回滚及提交断连恢复。扩展写入竞争、原生期间/GL、回填中断零残留已通过。升级、一致恢复、Frappe 和 HTTP 全套验证通过，见 [V1-12 验证报告](V1-12-STOCK-COUNT-REPORT.md) 和 [PR #25 完整 CI](https://github.com/LeonEleven/HaxpowerPlatform/actions/runs/45561795379/job/139026998758)。A06 保留发布验收条件。

正式员工数量写入继续关闭；B01/B02/B03/B04/B07/B08/B11/B12 的未决部分保留。B06 见下方 V1-13 记录。
原生 helper 所需类型权限由支持的 Custom DocPerm 提供，受管详情、HTTP 与原生生命周期守卫继续执行。
当前使用共享锁及全量受管历史核对，生产放行前须评估大历史量时的锁耗时。
项目负责人手动合并后的 main 为 `66220768e4bfed72cf455a862acb74b0ff44b9c9`。V1-13 从该提交开始。

[V1-12 任务分支](https://github.com/LeonEleven/HaxpowerPlatform/tree/feat/v1-12-stock-count-versioning) ·
[V1-11 PR #24](https://github.com/LeonEleven/HaxpowerPlatform/pull/24) ·
[盘点验证入口](../dev/v1/README.md#v1-12-非冻结盘点和库存版本验证)


**V1-13 Draft PR #26，2026-11-25（Asia/Shanghai）。** App 0.14.0 接入站点级审批开关、不可变提交快照、
修订重提、批准/驳回/撤回、移库发出前撤销，以及原生记账与审批成功的原子事务。B06-01–08
由项目负责人在 V1-13 任务书第二节批准，见项目决策第 9 节。待审批盘点遵循 B05 代次和版本，
过期后通过撤回及重盘衔接。移库接收及带原因余量退回无需重复审批。项目的审批人和库存操作者
独立授权，原生执行身份及触发者分别入账。

已核对空站安装、47 项 Frappe 回归、空站重复迁移及原生收发回归。隔离审批探针通过开关与跨公司、
身份桥接、盘点版本、移库双侧、独立连接竞争、原生及审批写入故障、断连和失败竞争。
升级恢复与最新工程状态见 Draft PR 的 Actions。A06、A10、G06 保留发布、
附件和浏览器验收条件。员工正式数量 HTTP 写入继续关闭。B02/B04/B07/B08/B10/B12 保持待确认。
经本任务架构审查及负责人手动合并后，下一任务为 V1-14 纠错。V1-20 增加多年合成库存流水规模的持锁、
并发和性能验证。

[V1-13 任务分支](https://github.com/LeonEleven/HaxpowerPlatform/tree/feat/v1-13-approval-workflow) ·
[Draft PR #26](https://github.com/LeonEleven/HaxpowerPlatform/pull/26) ·
[Actions 检查](https://github.com/LeonEleven/HaxpowerPlatform/pull/26/checks) ·
[验收入口](../dev/v1/README.md#v1-13-库存审批验证)

**V1-14 PR #27 已合并，2026-11-27（Asia/Shanghai）。** V1-13 PR #26 已于 14:47 合并，
V1-14 从 `3358612b41c51c7294bdc8e3069dde8ae0408794` 开始。项目负责人在任务书第二节批准
B07-01–06 和 B08-01–03，见 [项目决策第 10 节](DECISIONS.md#10-b07-库存纠错与-b08-审计留存2026-11-26)。
App 0.15.0 在隔离候选中加入反向纠错、独立审批、下游人工审核、原行额度及轻量追溯。保留 V1-11 余量返源。
B02 岗位、B11 生产备份和 B12 正式估值待审。G04/G07/G08 保留生产验收条件。V1-20 须复核多年历史量、
共享锁耗时和正式审批权限。工程验收证据见任务 PR 的检查；合并后下一任务为 V1-15 查询和报表。

[Draft PR #27](https://github.com/LeonEleven/HaxpowerPlatform/pull/27) ·
[V1-14 工程检查](https://github.com/LeonEleven/HaxpowerPlatform/pull/27/checks) ·
[合成验证入口](../dev/v1/README.md#v1-14-专项纠错与轻量审计验证)

**V1-15 Draft PR #28，2026-11-27（Asia/Shanghai）。** PR #27 已由项目负责人合并。本任务以重新核对的
`5409db95919f8c86b7d003802a2cee1126a21ad8` 为分支基线，App 0.16.0 提供受控员工目录、
库存与报表、分级审计、私有 Excel 和下载重验。B02/B09 已批准部分及来源见
[项目决策第 11 节](DECISIONS.md#11-b02-和-b09-查询权限及功能范围)。任务分支包含
[隔离验证及恢复入口](../dev/v1/README.md#v1-15-目录库存读取与导出验证)。
隔离站的 48 项 Frappe 测试、员工 Cookie HTTP、原生一致读取和 Excel 撤权已通过。升级及发布
证据随 [Draft PR #28](https://github.com/LeonEleven/HaxpowerPlatform/pull/28) 的
[工程检查](https://github.com/LeonEleven/HaxpowerPlatform/pull/28/checks) 核对。审查及负责人
合并后，下一任务为 V1-16 期初文件预览和清洗。
