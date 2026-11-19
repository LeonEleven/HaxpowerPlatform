# Stage 1F 业务审计与盘点截止版本验证

2026-11-19，Asia/Shanghai。**原型验证与测试。** 在 ERPNext 16.55.0 上，受控业务服务可以同事务提交原生库存单据和追加式事件。已验证入库、出库、盘点差异和部分移库的剩余在途可用新单据更正。已完成移库遇到下游消耗时仍阻塞。盘点版本能识别期间交易，包括数量恢复原值。盘点冻结规则和最终技术路线均待审查。
[库存模型](INVENTORY-MODEL.md) · [业务流程](BUSINESS-FLOWS.md) · [决策记录](DECISIONS.md) · [验证计划](ERPNext-VALIDATION-PLAN.md)

## 1. 环境和边界

PR #9 已合并，任务分支从同步后的 `origin/main`（`f4a3562078a20a12542c05b16d7d0f454329e707`）建立。复用原 Bench 和 `haxpower-stage1b.localhost`，只在新增虚构公司 `HP Stage 1F Lab` 写入实验库存。场景使用 6 个整数 PC SKU，3 个普通库位和 1 个在途库位。公司关闭永续盘存，启用明确批次选择和零估值，禁用负库存与出库自动分批。实际数据库是 MariaDB 11.8.9，隔离级别为 `REPEATABLE-READ`。Frappe、ERPNext 的锁定提交为 `90c8b61afd60ee49f982ca1a9751008e4ae1e24c`、`8e10a348daaa93853de8c36dc55ec789a440c219`。
[PR #9](https://github.com/LeonEleven/HaxpowerPlatform/pull/9) · [Stage 1E 环境和证据](STAGE1E-INVENTORY-INTEGRITY-REPORT.md)

## 2. 追加式审计和事务

独立 App 新增 `Stage 1F Audit Event` 和 `Stage 1F Count Task`。每个成功库存请求有一条业务事件。事件记录业务事件 ID、类型、原生单据类型 / ID / 行 ID、SKU、源和目标库位、批次、Decimal 对账数量、操作者和服务端时间。单向库存变化记录带符号差额。移库记录同一数量的源扣减和目标增加。更正以 `reversal_of` 引用原事件，接收以 `dispatch_event` 引用原发出事件，同时保留 ERPNext 的原单和原行链接。内部 Batch 键保留在适配层，对外无批次为 JSON `null`。原生备注仅有固定实验标识。事件表以公司、操作者、请求 ID 导出的主键约束请求唯一性，记录负载摘要，用于重试与不同负载冲突判断。盘点任务使用独立键空间，每个成功调整只关联一个任务。
[实验服务](../dev/erpnext-lab/haxpower_platform/haxpower_platform/audit.py) · [审计 DocType](../dev/erpnext-lab/haxpower_platform/haxpower_platform/haxpower_platform/doctype/stage_1f_audit_event/stage_1f_audit_event.json)

事件提供 Haxpower 业务事实，ERPNext SLE 和 Bundle 提供原生库存执行事实，Bin 提供 SKU / 库位汇总。Stock Reconciliation 的两条 SLE 可以分别扣除账面量和加入实盘量，业务事件只记同一桶的净差。服务使用 ERPNext Document API 提交原生单据，校验新 SLE 和批次分配，再追加事件。原生单据、明细、SLE、Bundle、Bin、业务事件及其幂等信息使用同一个数据库连接，由 Frappe 在成功 POST 的末尾执行提交。盘点任务创建有自己的事务，盘点提交读取该任务并在库存事务内完成版本校验和调整。
[Frappe 数据库事务模型](https://docs.frappe.io/framework/user/en/api/database)

| 故障位置 | 实际执行结果 |
| --- | --- |
| 原生库存单据 `submit()` 已完成，事件插入失败 | **通过。** 请求连接 24547 内可见已提交状态的单据、1 条 SLE 和 1 个 Bundle，审计表尚无该事件。在 Audit Event 的 `before_insert` 抛出异常，HTTP 500。新连接对库存及实验表重取摘要，与请求前一致。 |
| 事件已经插入，数据库提交失败 | **通过。** 在连接 24554 内已看到单据、SLE、Bundle 和事件。独立且同属该站点的数据库连接 24556 在提交前终止该请求连接。真正执行 `COMMIT AND CHAIN` 时驱动报 2013，新连接确认全表摘要与请求前一致。Frappe WSGI 在这个位置将异常传播给调用方。 |

两笔失败请求用原键重试，各只产生一个有效入库和一个事件，再重放仍只有一份。两次重试后，STD61 和无批次各为 3。失败事务内观察到的单号由回滚后的重试复用，按全表摘要和持久化行证据判断回滚。该试验覆盖 COMMIT 到达服务器前连接中断。服务器已成功提交但客户端未收到确认的故障窗口未注入，同键重放已单独验证。
[故障注入和新连接取证](../dev/erpnext-lab/stage1f_verify.py)

## 3. 正向操作、反向更正和对账

表内事件 ID 显示前 12 位，完整 ID、单据行、SLE 与 Bundle 主键见 `final.json`。

| 场景 | 单据及事件 | 新连接核对 |
| --- | --- | --- |
| STD61 入库后更正 | 入库 `MAT-STE-2026-00058`，事件 `21d1e323bb46`。反向出库 `MAT-STE-2026-00059`，事件 `6700c62ebfc9`。 | **通过。** 源桶 0 → 12 → 0，SLE 为 +12、−12。 |
| 无批次出库后更正 | 出库 `MAT-STE-2026-00061`，事件 `867018304868`。反向入库 `MAT-STE-2026-00062`，事件 `f8383c867412`。 | **通过。** 12 → 8 → 12，SLE 为 −4、+4，原事件全行保持一致。 |
| 无批次盘点差异后更正 | 盘点 `MAT-RECO-2026-00006`，事件 `65a132764245`。反向入库 `MAT-STE-2026-00081`，事件 `bb65acb60a81`。 | **通过。** 8 → 5 → 8，盘点 SLE 为 −8 / +5，业务差额 −3，反向 SLE 为 +3。 |
| 无批次移库部分接收后更正 | `MAT-STE-2026-00064/00065/00066`，分别发 10、接 6、返回剩余 4。返回事件 `0dfafe555487` 引用发出事件 `3cae251f2e46`。 | **通过。** 源 / 在途 / 目标为 2 / 10 / 0 → 2 / 4 / 6 → 6 / 0 / 6。已接收 6 保持有效。服务拒绝对原发出继续接收，数据库摘要未变。原生发出行累计接收仍为 6，业务剩余量还需计入返回 4。 |
| STD61 已完成移库且发生下游出库 | `MAT-STE-2026-00068/00069/00070`，发 10、接 10、目标出库 7。 | **更正阻塞，拦截测试通过。** 目标仅余 3，完整退回会超出目标库存。服务拒绝并保持 0 / 0 / 3，未尝试强制冲销。具备目标余量时的已完成移库更正也超出该原型范围。 |

最终从新连接一一匹配 31 条业务事件、29 张 Stock Entry、2 张 Stock Reconciliation 及其明细。39 条有效 SLE 均保留，每条匹配已提交 Bundle 和分配行，逐批净额与 ERPNext 批次查询相符，汇总与 10 条 Bin 一致。另有 14 条盘点任务、12 个 Batch、1 条重估任务，GL 为 0。Audit Event 和盘点基线的数据库数量列均为 `DECIMAL(21,9)`。首次取证发现 Frappe 驱动把数量读为浮点对象，原型已修正为将本次整数 PC 输入转为 Decimal 再比较，并从新连接继续验证先前已提交的入库。小数单位读取精度未纳入本轮验收。
[对账验证器](../dev/erpnext-lab/stage1f_verify.py)

## 4. 原生写入和信任边界

**可在已验证场景中避开 ERPNext 原生取消，同时保留业务审计。** 原单和原业务事件保留，数量更正作为新单据和新事件追加。Stage 1E 已观察到原生取消将旧 SLE 的 `is_cancelled` 设为 1、清除原 Bundle 关联并更新旧行。若绕过服务执行原生取消，业务事件仍在，原生有效流水会发生变化。Stage 1F 的守卫在已测文档入口拒绝原生操作，不能仅凭 SLE 原记录仍存在就推断业务链完整。
[Stage 1E 原生取消证据](STAGE1E-INVENTORY-INTEGRITY-REPORT.md#4-s04-正式纠错与审计链) · [ERPNext Immutable Ledger](https://docs.frappe.io/erpnext/immutable-ledger-in-erpnext)

| 路径 | 实测及剩余风险 |
| --- | --- |
| 业务角色及仓库范围 | **通过。** 查询角色、无该库位授权的操作员、仅有移库一侧权限的操作员被拒绝。缺批次选择和精确桶库存不足也被拒绝。Operator、Query、Stock Manager 各对事件 REST 做创建、修改、删除，共 9 次均为 403。 |
| 原生 Stock Manager 和站点 Administrator | **已测入口通过。** Stock Manager 原生 Stock Entry REST 在守卫处返回 403。Administrator 用标准文档入口创建库存单、取消原单、直接创建 / 修改 / 删除审计事件均被拒绝。仅业务服务内部可追加两个实验 DocType。验收仍需覆盖所有已启用的原生库存文档、批量入口、权限配置和其它安装 App。 |
| ERPNext 后台正常重估 | **该样本数量对账通过。** 对 Stage 1F 单据定向运行重估 `qjr1f9lfpd`，状态 Completed，5 条 SLE 的 `modified` 改变，逐批数量、Bin、有效 SLE 的身份与数量及业务审计摘要相同。尚未验证与盘点提交并行的重估、非零估值、跨期间重估和定时调度。 |
| 原生 SLE 重建和批量清理 | **已测入口通过，后台直接执行阻塞。** 拒绝创建带账簿重建选项的 Stage 1F 重估单，拒绝创建实验公司的 Transaction Deletion Record。锁定源码显示两种后台路径会直接写原单状态或批量删表行，业务 DocType 生命周期无法约束已在执行的作业。实质重建和清理未执行。继续评审前需确认生产禁用/收紧这些入口并核查已有队列。 |
| DBA、恢复和服务器任务 | 数据库管理员、持有站点数据库凭据的程序、安装 App、补丁、后台维护和恢复工具属于受信任边界。它们可以绕过普通文档权限。必须同一时间点恢复原生库存、任务和事件，恢复后重新对账、核查作业并要求未提交的旧盘点重新核对。若要求抵御管理员事后修改，还需独立留存和校验机制。恢复及独立留存未实测。 |

原生后台禁止重建的守卫只约束本次所测文档入口。普通 DocType 权限和 App 守卫提供的是应用层追加约束。
[App Hooks](../dev/erpnext-lab/haxpower_platform/haxpower_platform/hooks.py) · [锁定版本重估源码](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/stock/doctype/repost_item_valuation/repost_item_valuation.py) · [锁定版本批量清理源码](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/setup/doctype/transaction_deletion_record/transaction_deletion_record.py)

## 5. 盘点截止版本

创建任务时由服务端固化 SKU、库位、明确选择的批次或无批次、账面量和版本。版本由该桶的业务事件 ID 和有效 SLE / 批次分配身份及数量共同计算。会先按原生单据行核对两套账的净变化，不一致时中止。服务用公司行锁串行化该实验公司的受控写入，锁内用当前读重新计算版本。不同历史即使余额相同也会判为旧任务。请求 ID 只用于幂等，任务的版本值只从服务端记录读取。其它库位或其它批次的变动不会改变同一盘点桶的版本。

| 用例 | 实际结果 |
| --- | --- |
| 创建任务后入库、出库或移库发出 | **通过。** 三组基线均为 10，账面分别变为 12、8、8。三个旧任务均返回 417 `STAGE1F_COUNT_STALE`，拒绝前后全表摘要一致。 |
| 无批次数量 8 → 6 → 8 | **通过。** 两个任务的账面均为 8，版本不同，旧任务返回 417。 |
| 不同请求 ID、两张任务使用相同旧基线 | **通过。** 无批次两张任务同为 8。其它批次、其它库位各合法入库一次后，首张仍能盘至 5，第二张返回 417。真批次并发用连接 24528 / 24529 实际提交，两张任务同为 10，仅一笔成功至 7，另一笔因版本过期拒绝。 |
| 同请求 ID 重放、改负载及任务二次提交 | **通过。** 重放返回同一事件，库存和任务摘要没变。同键改实盘量返回 `IDEMPOTENCY_CONFLICT`，换请求 ID 重用已完成任务返回 `STAGE1F_COUNT_ALREADY_SUBMITTED`。 |
| 账面零盘盈 | **通过。** 相同 SKU 与库位的无批次 0 → 3（`MAT-STE-2026-00082`），STD61 0 → 2（`MAT-STE-2026-00083`）。两单用正式 Material Receipt 写入，业务语义为 COUNT，各有独立任务与事件。零当前 Bundle 对应的原生 Stock Reconciliation 路径未运行。 |

本轮只验证单桶、整数 PC、提交时刻记账。无差异任务显式拒绝写库存。尚待定义只登记实盘无差异、多个桶任务、跨班次截止、回填和旧任务重新核对的业务规则。原型每次读取该桶历史，公司的行锁粒度较粗。上线前应评估索引、桶级序号与锁顺序，并覆盖所有写路径及异常重试。

| 方案 | 当前约 1 名主操作员时的维护成本 |
| --- | --- |
| A. 冻结选定范围，未执行 | 若现场盘点短且可停收发，操作流程直观。系统仍需定义冻结范围、持锁前已在途请求、超时/解冻、权限、异常恢复及 ERPNext 后台遵守冻结的方式。盘点较久或涉及到货时，冻结会推迟业务。 |
| B. 服务端版本，已执行 | 收发无需等待现场盘点，过期任务由操作员重新核对。维护成本是所有写入原子推进版本、独立幂等、防漏写、跨事务锁和对账。若单人通常在短时段内完成盘点，过期频率预计较低，实际操作数据尚未采集。 |

## 6. 技术选型建议与未满足条件

- **ERPNext + 独立 App。** 可复用标准库存单据、逐批 SLE / Bundle、原生在途和累计接收。长期还需维护双账对账、无批次映射、盘点版本、幂等、逐单返回在途的业务状态和反向单据。原生取消、账簿重建、事务批量清理、后台重估及角色升级均进入维护与回归范围。固定版本已出现旧批次字段的弃用提示，升级需重新验证 Bundle、关闭日、重估与数量舍入。
- **Frappe-only。** 可直接用同一数量模型建立可空批次、域账簿和盘点版本，省去 ERPNext 专有的双账、估值和取消联动。代价是自行设计并验证 Decimal 流水、余额事务、可空批次唯一性、并发扣减、幂等、仓库权限、两段/部分移库、下游补偿、报表、恢复和期初迁移。DBA、备份和后台任务仍然属于信任边界。
- **建议技术评审优先以 Frappe-only 做对照，再决定 ERPNext 的复用收益是否覆盖上述长期成本。** ERPNext 可保留为有条件候选。采用前需通过完整写入口和后台隔离、已完成移库的业务补偿、恢复后审计对账、生产级盘点截止与多桶并发，同时完成既有验证计划中的期初导入、动态权限等验收。若要求原生 SLE 自身历史全字段不可更新，当前 ERPNext 路径已观察到不符合。冻结与版本方案由业务审查决定。
[技术选型状态](DECISIONS.md#5-技术路线状态) · [验证计划](ERPNext-VALIDATION-PLAN.md)

## 7. 复现和回归

只在已有隔离 Bench 同步仓库内的独立 App，将验证脚本和 `stage1b_verify.py`、`stage1c_verify.py`、`stage1d_native.py`、`stage1d_verify.py`、`stage1e_evidence.py` 放入同一临时目录。对 `haxpower-stage1b.localhost` 分别执行 `bench --site haxpower-stage1b.localhost reload-doc haxpower_platform doctype stage_1f_count_task` 和 `stage_1f_audit_event` 的同类命令，再清该站点的缓存。使用 Bench Python，在 Bench 的 `sites` 目录运行。

```bash
hp_python=/workspace/development/frappe-bench/env/bin/python
hp_probe=/tmp/haxpower-stage1f-tools/stage1f_verify.py
for hp_mode in stock transfer count concurrent fault permissions repost final history; do
    "$hp_python" "$hp_probe" "$hp_mode" || break
done
```

写入场景使用固定虚构数据和请求 ID，按顺序运行一次。已有完整证据时用 `final` 和 `history` 只读取证，`permissions` 可再次执行拒绝探针。`repost` 会新增一次定向重估，`fault` 只终止该次虚构请求的自有连接。临时实验 API 密钥已撤销。
[执行验证器](../dev/erpnext-lab/stage1f_verify.py)

仓库 Python 回归常规模式和优化模式各 17/17 通过。Stage 1B 八笔请求、Stage 1D 两组移库重放的证据与上轮逐字节相同。Stage 1C 复跑 225 个权限探针，角色和结果矩阵相同。Stage 1E 复跑原生回滚组，持久化组的 37 张 Stock Entry、5 张 Stock Reconciliation、60 条有效 SLE 和 5 条盘点请求前后相同。Stage 1B / 1D 的全库存行摘要、Stage 1C 的仓库授权摘要、Stage 1E 的全库存行及请求摘要均一致，原实验目录的 23 份 JSON 校验通过。Frappe 和 ERPNext 的工作区在实验前后均为 clean，提交 ID 一致。

本轮脱敏证据及完整 `SHA256SUMS` 保存在既有实验目录的 `stage1f-20261119/` 子目录。

| 证据 | SHA-256 |
| --- | --- |
| `stock.json` | `647ce7bae2b3c6bb93a1f7a09c6aff022a1232dc808e4b2f5c00a37346534a2c` |
| `transfer.json` | `895b5fc4a622292cacf3e8f4d702fb07a89cd215a629fc4c7df8ecd8f26b7afe` |
| `count.json` | `9febe34e0eeda62ca549af87b42069deb84f00992ac566b8013878332caad9b5` |
| `concurrent.json` | `b2cd03f88156b8ced75002da4ec2e3700f21620524dc692899dc9d3869633812` |
| `fault.json` | `a5ba797f8f07f1dcb79d8ec46d1436881833ec876bcc89326dd3d1fa8aab3418` |
| `permissions.json` | `8b756cd14ba2535e9daa915e38a7141b0ded653ea62d55af37e35034e52f6296` |
| `repost.json` | `d91da18384622aa7e2138012fddda8cf3e0f72c8c5b28fe721a540953927e085` |
| `final.json` | `bb8587681654782d71dfc33025c750be7d81436598f0e9ac11c52fcaeb2d4269` |
| `history-before.json` / `history.json` | `b0b1723c42433d058beebcb944f67339a2233b2077bcbb10d3411d19805e25dd` |
