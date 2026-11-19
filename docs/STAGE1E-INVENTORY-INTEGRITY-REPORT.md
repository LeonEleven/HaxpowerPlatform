# Stage 1E 库存完整性与可靠性验证

2026-11-19，Asia/Shanghai。ERPNext 的库存数量在已测的盘点、并发扣减、部分接收及回滚场景中可以对账。两项差距需要技术审查。原生取消会改写历史 SLE 并解除原 Bundle 关联。两个有不同请求 ID 的绝对盘点会依次覆盖同一桶，服务端尚无实盘截止或账面版本约束。ERPNext 仍可作为候选，继续采用需满足第 8 节的条件。

## 1. 环境与隔离

使用现有 4 个容器和同一 Bench 的 `haxpower-stage1b.localhost`，数据库 `haxpower_stage1b_lab`。提交组使用虚构公司 `HP Stage 1E Lab`、仓库前缀 `HP1E` 和 9 个固定实验 SKU。原生组使用独立的 `HP1E-N-8157B10B` 公司、SKU 和批次，测试结束整体回滚，新连接确认公司未留存。只用 Document API 写库存。App 的新增 DocType 只用于保存盘点请求，没有修改 Frappe 或 ERPNext 核心。

| 组件 | 版本或配置 |
| --- | --- |
| Frappe | 16.55.0，`90c8b61afd60ee49f982ca1a9751008e4ae1e24c` |
| ERPNext | 16.55.0，`8e10a348daaa93853de8c36dc55ec789a440c219` |
| Python、MariaDB、Redis | 3.14.10、11.8.9、7.4.12 |
| 库存 | 启用批次及旧式批次字段，关闭出库自动分配与负库存 |
| 核算 | 独立公司关闭永续盘存，单据允许零估值，提交组 GL 为 0 |
| 事务 | HTTP 请求各用独立连接，MariaDB `REPEATABLE-READ` |

HTTP 请求由当前 Bench 内的 Frappe WSGI 应用处理，包括认证、权限校验、提交和回滚。业务调用使用虚构 Operator、Query 和只有另一个库位权限的 Operator。原生对照由实验站点管理员使用 `insert()`、`submit()`、`cancel()` 和原生 End Transit mapper。故障和锁的测量代码只在验证脚本进程内运行。 [Stage 1C 权限边界](STAGE1C-PERMISSION-BOUNDARY-REPORT.md) · [Frappe 事务模型](https://docs.frappe.io/framework/user/en/api/database)

## 2. E10 按批次盘点

ERPNext 的 `Stock Reconciliation` 可用 `use_serial_batch_fields=1` 和明确 Batch，或用选定的 `Serial and Batch Bundle` 限定盘点范围。原生实验关闭 `reconcile_all_serial_batch`。STD61 12 → 11、STD62 保持 20、用于模拟无批次的普通测试 Batch 15 → 17。STD62 无差异行被标准单据省略。另将 STD61 盘至零，其他桶保持原值。旧式字段缺 Batch 返回 `Please enter Batch No`，不存在的 Batch 返回 `LinkValidationError`。两次拒绝均经保存点回滚和表摘要比对。 [ERPNext Stock Reconciliation](https://docs.frappe.io/erpnext/stock-reconciliation)

专用 Operator 直接走原生隐式余额读取时，`get_stock_balance_for` 返回 403，该函数要求 Stock Reconciliation 原生写权限。原型在授权库位内读取并锁定选中桶，随单提交对应的标准当前 Bundle。沿用 Stage 1B 的保留 Batch 和三态选择，无批次由 `NO_BATCH` 显式指定，内部 ID 未出现在成功响应。PC 数量在 App 保持 Decimal，向使用整数 PC 的盘点单传入精确整数。

| 场景 | 判定 | 持久化及余额证据 |
| --- | --- | --- |
| STD61、STD62 和无批次在同一库位独立盘点 | 适配后通过 | `MAT-RECO-2026-00001`。W1/L1 为 11 / 20 / 17，Bin 47 → 48，W1/L2 无批次保持 2。STD61 的 SLE 为 −12/+11，无批次为 −15/+17，各有对应 Bundle。STD62 无新增 SLE。 |
| 独立真批次盘至零 | 原生通过、适配后通过 | `MAT-RECO-2026-00002`。独立 SKU 的 STD61 4 → 0，STD62 保持 8。SLE `6f47201bca` 为 −4，唯一新增 Bundle 为出向，Bin 12 → 8。 |
| 缺选择、批次不存在 | 原生通过、适配后通过 | App 分别返回 417 `BATCH_SELECTION_REQUIRED`、`BATCH_NOT_FOUND`。 |
| 查询角色、操作员无该仓库授权 | 适配后通过 | HTTP 403 `INVENTORY_OPERATION_DENIED`、`WAREHOUSE_ACCESS_DENIED`，拒绝前后单据、SLE、Bundle、Bin、幂等表摘要一致。 |
| 用实盘截止版本拒绝第三方交易后提交的旧盘点 | 未执行 | 当前实验以服务端提交时刻记账。尚未定义实盘截止、发出／接收如何计入、冻结或版本检查，也未验证回填盘点。 |
| 盘点为正数且当前桶为零 | 未执行 | 原型显式拒绝，对零库存建立期初、空桶转正的盘点业务需另行验证。 |

测试验证器曾以长读事务观察 HTTP 提交，盘至零时仍读到 4、真批次接收后仍读到上阶段在途 9。暂停相关写入后，通过新连接核对前者为 0，后者为在途 1，与正式单据、Bin、Bundle 和 SLE 一致。修正验证器每个提交后对账先结束旧读事务。原两笔盘点用相同请求重放，未重入账。真批次移库保留已提交记录，从确认后的剩余量继续。 [盘点与回归脚本](../dev/erpnext-lab/stage1e_verify.py)

## 3. S02 并发扣减

两个请求先在 HTTP 边界同步，再在应用锁前同步。每对请求由两个实际数据库连接执行。验证脚本让拿到公司行锁的请求额外持有 300 ms 以提供可观测的争用。服务用公司行 `FOR UPDATE` 串行化实验写入，锁内取 SLE 与 Batch allocation 的当前读，避免复用认证阶段已建立的快照。等待时间含实验延时和第一笔正常写入时间。

| 批次 | 请求与连接 | 结果 | 锁等待与复核 |
| --- | --- | --- | --- |
| STD61 | `hp1e-RACE-STD61-0/1`，23364 / 23363 | 适配后通过。两笔各请求 7，仅 `MAT-STE-2026-00036` 成功，余额 10 → 3。 | 10 / 611 ms，另一个返回 417 `INSUFFICIENT_STOCK`，重试仍拒绝。STD62 保持 12。 |
| NO_BATCH | `hp1e-RACE-NULL-0/1`，23372 / 23371 | 适配后通过。两笔各请求 7，仅 `MAT-STE-2026-00037` 成功，余额 10 → 3。 | 8 / 609 ms，另一个返回 417 `INSUFFICIENT_STOCK`，重试仍拒绝。STD61 保持 12。 |

每组仅增加 1 笔提交的 Stock Entry、1 条明细、1 条有效 SLE、1 个 Bundle 及其明细，负库存为零。本轮没有观察到死锁或超时，没有触发并验证数据库死锁后的应用重试策略。公司级锁会串行化同公司不同 SKU 的业务，还依赖当前锁内批次查询扫描历史记录。生产锁粒度、锁顺序、原生后台任务并行、索引和高并发吞吐未执行验证。

## 4. S04 正式纠错与审计链

正式取消的原生数量效果通过。测试中原单保持可查、`docstatus=2`，修订通过 `amended_from` 关联。取消直接由 ERPNext 完成。以下单号属于回滚组。

| 场景 | 原生数量结果与证据 |
| --- | --- |
| 已提交入库更正 | 原生通过。原入 12，取消后为 0，`MAT-STE-2026-00061-1` 引用原单并改入 11。 |
| 已提交出库冲销 | 原生通过。期初 12，`MAT-STE-2026-00063` 出 4 后余 8，取消恢复 12。 |
| 已完成盘点纠正 | 原生通过。盘点至 11，取消恢复 12，`MAT-RECO-2026-00008-1` 关联原单并纠正为 10。 |
| 在途尚未接收时撤销发出 | 原生通过。取消 `MAT-STE-2026-00066` 后，源 / 在途 / 目标从 2 / 10 / 0 恢复 12 / 0 / 0。 |
| 已部分接收时纠错 | 原生通过。源 / 在途 / 目标为 2 / 4 / 6。直接取消发出因在途 Batch 将为 −6 被拒绝。先取消入向单 `MAT-STE-2026-00069` 恢复 2 / 10 / 0，再取消发出单 `MAT-STE-2026-00068` 恢复 12 / 0 / 0。未测试接收后目标库存已消耗或另有在途库存时的取消顺序。 |

**业务流水追加要求判定为失败。** 原始 SLE 主键均保留，观察到 `is_cancelled` 由 0 变 1、`serial_and_batch_bundle` 由原 ID 变为空、`modified` 更新。ERPNext 同时生成相反数量的新 SLE，原行与反向行在本轮均标记取消。即时有效余额以 `is_cancelled=0` 的 SLE 重建，并以有效 Bundle 汇总逐批核对 Bin。取消后原始 SLE 的批次链无法仅靠原 SLE 自身重建。累计产生 9 条 `Repost Item Valuation`，取证时均为 `Queued`，随原生测试事务回滚。重估工作进程实际执行后的历史变化未验证。 [ERPNext Immutable Ledger](https://docs.frappe.io/erpnext/immutable-ledger-in-erpnext) · [业务流程](BUSINESS-FLOWS.md)

已停止把原生取消当作业务冲销路径。本轮保留原始、取消后和反向记录的对照。最小补偿方案待验证。业务层在库存单据生效的同一事务写追加式事件，持久保存原单／行、SKU、库位、对外批次、不可变数量差、操作者及时间。更正生成带 `reversal_of_event_id` 的新反向标准库存单据并写对应事件。部分移库更正应以原发出行和实收记录为边界，依赖单据先校验目标及在途可扣余额。需要验证反向调整能覆盖下游已耗用、关账和晚到事务等约束。 [库存模型](INVENTORY-MODEL.md)

## 5. S05 精度及 S06 事务原子性

| 用例 | 判定 | 观察 |
| --- | --- | --- |
| 1.125 入、0.125 出 | 原生通过、适配后通过 | 小数 UOM 允许小数，换算系数 1，`MAT-STE-2026-00039/00040`，精确余额 1.000。Bin、SLE、Bundle 和单据数量列均为 `DECIMAL(21,9)`，原始 DB 值以字符串读取后做 Decimal 相等检查。 |
| 超过 3 位拒绝 | 原生失败、适配后通过 | 原生接受 0.1254，将库存扣减舍入为 0.125。原生对 0.0001 因舍入后为零拒绝。设置是 3 位、Banker's Rounding。App 在生成单据前对超精度字符串返回 417，无库存副作用。 |
| PC 0.5、零、负数出库 | 原生通过、适配后通过 | PC 启用 Must Be Whole Number。原生分别返回整数单位、零数量和正数约束错误，App 返回 417 `INVALID_STOCK_QUANTITY` 并核对全表摘要。多单位换算比例和精度组合未执行。 |
| 两行单据，第一行出 3 可用 10，第二行出 3 可用 2 | 原生通过 | 第二行触发 `BatchNegativeStockError`。异常当时未提交事务可见 1 张 Stock Entry、2 行、2 个 Bundle 和 2 条 SLE 的中间写入。回滚至保存点后所有库存表摘要匹配，两个桶分别保持 10、2。原生单据无独立幂等表。 |
| 独立 App 受控异常 | 适配后通过 | 在收、发、移库发出、部分接收和盘点各选一笔，在正式单据写入后抛出异常。真实 HTTP 返回 500，外部事务的单据、SLE、Bundle、Bin 与幂等状态全部不变。盘点先插入请求记录再抛出异常，请求表也回滚。同键成功重试仅形成一份提交。 |

## 6. E15 幂等与盘点并发

| 操作 | 同键重放 | 同键改负载 | 同键并行 | 不同键并行 | 异常后同键重试 |
| --- | --- | --- | --- | --- | --- | --- |
| 入库 | 适配后通过 | 适配后通过 | 适配后通过 | 适配后通过，两次独立入库 +1/+1 都落账 | 适配后通过 |
| 出库 | 适配后通过 | 适配后通过 | 适配后通过，PC 仅出 1 | 适配后通过，见 S02 | 适配后通过 |
| 移库发出 | 适配后通过 | 适配后通过 | 适配后通过 | 适配后通过，剩源库 10 时两笔争抢各 8，仅一笔成功 | 适配后通过 |
| 部分接收 | 适配后通过 | 适配后通过 | 适配后通过 | 适配后通过，同一发出行余 4 时两笔各接 3，仅一笔成功 | 适配后通过 |
| 盘点 | 适配后通过 | 适配后通过 | 适配后通过，`MAT-RECO-2026-00003` 仅提交一次 | 失败（旧实盘覆盖），两个不同键顺序成功，账面依次 11 → 8 → 9 | 适配后通过 |

同键并行均返回一笔首次成功和同一结果 ID 的重放响应。同键改负载返回 417 `IDEMPOTENCY_CONFLICT`，拒绝时全表摘要保持不变。盘点的请求记录使用独立 DocType，`request_key` 有数据库唯一索引，保存用户／请求键摘要、规范化负载摘要及已提交盘点关联。盘点使用公司锁串行，但接口只有绝对实盘量，无法识别第二个请求对应旧实盘截止点。两个请求串行时账面一致，生产验收仍需要截止批次或带账面版本的前置条件。

入库、出库、移库依赖 `Stock Entry.remarks` 加锁按前缀查找。备注上的键无独立唯一约束，备注编辑、原单取消、不同动作的键范围及角色外入口均需考虑。发出／接收重放返回当前累计状态，正式接口若需要完全相同的原始响应须单独约定。建议统一原子的幂等记录，数据库唯一键为（租户、调用方、操作、请求 ID），同事务保存负载摘要、结果和单据关联。完成状态仅随正式单据提交，冲销另用新请求键。错误映射、成功响应缓存范围和生产死锁重试均待验证。 [API 草案](API-CONTRACTS.md)

## 7. 同批次多张在途单

STD61 和 NO_BATCH 各使用独立 SKU。每组 A 发 10、B 发 8，初始源库 20，另有 STD62 在源库 9 和选定桶在其他位置 1，两项对照保持不变。

| 阶段 | 源 / 在途 / 目标 | A 余量 / B 余量 |
| --- | --- | --- |
| 初始 | 20 / 0 / 0 | — |
| 两张发出完成 | 2 / 18 / 0 | 10 / 8 |
| A 接收 6 | 2 / 12 / 6 | 4 / 8 |
| A 的两笔并发各接 3，仅一笔成功 | 2 / 9 / 9 | 1 / 8 |
| B 接收 8 | 2 / 1 / 17 | 1 / 0 |
| A 接收剩余 1 | 2 / 0 / 18 | 0 / 0 |

**逐行归属适配后通过。** STD61 的发出单 A/B 为 `MAT-STE-2026-00043/00044`，原行 `6eag7jmkc6/6en305lgj8`。NO_BATCH 的发出单为 `MAT-STE-2026-00052/00053`，原行 `7hlgbevlo1/7i38lg9r2i`。所有正式接收的 `outgoing_stock_entry`、`against_stock_entry`、`ste_detail` 均指向各自发出及原行，Batch、SLE 和 Bundle 对账守恒。A 余 4 时，即使共享 Transit 有 12，接 5 仍在受控 API 拒绝。相同数据下，用原生 End Transit mapper 强行将 A 的接收数量设为 5，返回 `Transferred quantity cannot be greater than the requested quantity`，保存点回滚后库存摘要不变。**原生累计数量上限通过。**

共享在途的 Bin 是物理总量，逐单归属依赖原发出行链接和累计值。新的写入口、冲销和批量接收必须保持这两套约束，避免用公共 Transit 余额替代原行限额。Stage 1D 中曾观察到高权限原生单据可以操作 Transit。正式方案应只开放受控写入口，并评估维护逐单在途事件或使用分隔在途位置的成本。跨单取消和多行并发接收未执行。 [Stage 1D 报告](STAGE1D-TRANSFER-VALIDATION-REPORT.md)

## 8. 风险及技术建议

- **ERPNext + 独立 App。** 原生单据、Batch/Bundle 和 End Transit 可复用。已测的精确扣减、分次接收和按批次盘点均无需改核心。采用前需验证追加式业务审计与反向调整能覆盖全部冲销场景，定义盘点截止点或冻结并补测并发，收紧原生入口，统一幂等与锁策略。同时把跨版本 Bundle、舍入和取消行为纳入回归。原生账仍执行内部估值，后台重估、原生角色读取暴露和未经验证的入口仍要管理。 [适配差距](ERPNext-FIT-GAP.md) · [权限回归](STAGE1C-PERMISSION-BOUNDARY-REPORT.md)
- **Frappe-only。** 可将可空批次、仓库范围、盘点版本和追加式账簿直接建模。对应工作包括设计 nullable 批次唯一性、事务性余额与持久化幂等、并发与锁顺序、逐单在途及部分接收、事后补偿、报表与迁移，并补齐同等的对账和故障测试。
- 建议把业务审计可追溯性作为路线评审的首项。如果可接受 ERPNext 是数量执行引擎、独立事件是业务审计事实，就先验证反向单据的可执行性，再审核其他条件。如果要求 ERPNext 原生 SLE 自身提供完整且不可改的批次链，当前版本在该要求下失败，优先比较 Frappe-only。最终路线待审查，期初导入、动态仓库权限范围和其它既定验收项沿用原计划。 [决策记录](DECISIONS.md) · [验证计划](ERPNext-VALIDATION-PLAN.md)

## 9. 复现、回归及证据

在已有实验安装上同步仓库内的独立 App。仅对 `haxpower-stage1b.localhost` 执行 `bench --site haxpower-stage1b.localhost reload-doc haxpower_platform doctype stage_1e_count_request`，并清该站点缓存。把下列脚本及其 `stage1b_verify.py`、`stage1c_verify.py`、`stage1d_native.py`、`stage1d_verify.py` 依赖复制到实验容器临时目录。在现有 Bench 的 `sites` 目录用 Bench Python 运行。

```bash
hp1e_python=/workspace/development/frappe-bench/env/bin/python
hp1e_tools=/tmp/haxpower-stage1e-tools
"$hp1e_python" "$hp1e_tools/stage1e_evidence.py"
"$hp1e_python" "$hp1e_tools/stage1e_native.py"
for hp1e_mode in count issue precision transit-real transit final; do
    "$hp1e_python" "$hp1e_tools/stage1e_verify.py" "$hp1e_mode" || break
done
```

`stage1e_evidence.py` 和 `final` 可只读复核。`stage1e_native.py` 使用随机标识及事务回滚，可以独立重复。其余模式在固定实验组只运行一次，通过基线与请求记录保护已提交证据。当前实验已完成，应使用只读复核。临时实验 HTTP 密钥已撤销。 [原生验证](../dev/erpnext-lab/stage1e_native.py) · [HTTP 验证](../dev/erpnext-lab/stage1e_verify.py) · [只读取证](../dev/erpnext-lab/stage1e_evidence.py)

从新连接核对持久化实验公司，37 张 Stock Entry、5 张 Stock Reconciliation、60 条有效 SLE、60 个 Bundle、16 条 Bin、16 个 Batch，5 条盘点请求记录，GL 与重估均为 0。Stage 1B 的 8 笔单和 Stage 1D 的 12 笔单及其库存表全行摘要前后一致。三个阶段的 10 个历史证据文件的摘要与历史报告相同。Stage 1B 八笔请求重放结果逐字节相同。Stage 1C 复跑 225 个探针，角色和结果矩阵相同。Stage 1D 两组移库再次重放，证据逐字节相同。仓库授权记录摘要一致。仓库既有 Python 测试在常规和优化模式均 17/17 通过。

四个既有容器均健康。Docker image ID 的 SHA-256 分别为 frappe `258c5e0d91ebaf83cf314ac6573b1a04a5ce40353a6092bae86550d4299c0782`、MariaDB `d36f18d65a0459510b8b0f643b226a6d299e3206486e2b64ff20754a6d291707`、两个 Redis 容器 `7f7d30b7fdd09467bac58198a1f166dd69789d348d4fe859cccb20e0cf287fbe`。

脱敏证据保存在实验目录 `stage1e-20261119/`。

| 证据 | SHA-256 |
| --- | --- |
| `historical-baseline.json` | `c9b2d59b6e84ffb6a4ec0fc69130fc26547ddb53623f967f294461b92fab1f05` |
| `native.json` | `4be25ce0386a6dec03884e75a8508909af67de5ab9741ad239392a92e6b49406` |
| `count-fresh-connection.json` | `57da916dc34f0030e7df128eb5f91c6e8675584ff91845e3fea95c678b432228` |
| `transit-fresh-connection.json` | `7e00df3b7bd69ce6990e011866670e04ecb1897aec1ca1ce057ccdbc28ff6cd5` |
| `count.json` | `3a486ae2be58f8e27a18232b1dc1e093373eeb3319dc1dcc8e604300462ee056` |
| `concurrent-issue.json` | `cc2919926eddcb3dd56432168a7e01e7cec4c309cb2f079efa39f2b2ea0a5e4d` |
| `precision.json` | `ef59a4454330ee10b7a68b49e7fce47db10b11446cb28f3a10881f219896071d` |
| `shared-transit-std61.json` | `4ea5f90e84177b606334755856386e8b9d2a7a43ed5873d0afca4e83e1372c8c` |
| `shared-transit-no-batch.json` | `c50688930d43e6b96ca570a81b66db955cedde9345e2fe668cc573305c1f06a2` |
| `final.json` | `75aaedc531742f038bd2e031d9bfeffa29ba556a334aff0ac5f716cda84f3ec1` |
| `stage1b-regression.json` | `3a1456289da66b3e31bc3b039ce9d864e768a62b15f017e9cb79733e89e91327` |
| `stage1c-regression.json` | `4fcd3eccb9622aa4bf97d9c21987b71a4bdb155602204a876eac084e726c492c` |
| `stage1d-regression.json` | `6eed6f44077de0238405d4895798a6adad5f9efd5c2a22a52465ce5dc88041a4` |
