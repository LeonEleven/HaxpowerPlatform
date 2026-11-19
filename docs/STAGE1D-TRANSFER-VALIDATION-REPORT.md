# Stage 1D — 两阶段移库与部分接收验证

2026-11-18（Asia/Shanghai）完成 ERPNext 技术选型实验。**ERPNext 16.55.0 原生支持对同一发出行分次接收。** STD61 与映射为 `batch=null` 的 NO_BATCH 均通过 10 PC 发出、6 PC 接收、4 PC 接收。原生普通出库可使用在途仓，原生关联接收也可改目标或改有库存的批次。这些约束由独立 App 补齐。

## 1. 环境和隔离

PR #7 已合并，任务分支 `spike/erpnext-transit-partial-receipt` 基于 `origin/main` 的 `75ec80b88bef22926b51feb4451061d303c2d498`。四个原有容器健康，Bench 仍由同一组 Honcho 管理。Frappe 16.55.0 锁定在 `90c8b61afd60ee49f982ca1a9751008e4ae1e24c`，ERPNext 16.55.0 锁定在 `8e10a348daaa93853de8c36dc55ec789a440c219`。两套核心工作区均干净。 [镜像锁文件](../dev/erpnext-lab/versions.lock.json)

沿用 `haxpower-stage1b.localhost` 及其 MariaDB 数据库。原生组使用随机标识的虚构公司和单据，在事务内提交并整体回滚。适配组在同站点的独立虚构公司 `HP Stage 1D Lab` 内建立 W1/L1、W1/L2、W2/L1 及 Transit 仓，以及彼此独立的两种 SKU。新公司关闭永续盘存。实验沿用站点已启用的显式批次字段，负库存、批次负库存以及自动出库 Bundle 均关闭，Stock Entry 无启用审批流程。共享库存设置未修改。

测试通过容器内的 Frappe WSGI 应用处理 HTTP 请求，包括令牌认证、路由和每次请求的提交或回滚。每个请求使用独立上下文，未启动第二组开发服务。Stage 1B 的八笔正式单据及 Stage 1B、1C 的历史证据保留。 [Stage 1B 报告](STAGE1B-NO-BATCH-ADAPTER-REPORT.md) · [Stage 1C 报告](STAGE1C-PERMISSION-BOUNDARY-REPORT.md)

## 2. 原生在途流程

1. 发出使用标准 `Stock Entry`（Material Transfer），设置 `add_to_transit=1`，源为 W1/L1，目标为 ERPNext 类型为 Transit 的仓库。
2. 页面 **End Transit** 调用 `make_stock_in_entry`。新 Material Transfer 的 **Stock Entry (Outward GIT)** 对应 `outgoing_stock_entry`。接收行的 `against_stock_entry` 和 `ste_detail` 指回原单、原行。未关联 Material Request 的目标库位由接收方填写。
3. 映射器按发出行的 `transfer_qty - transferred_qty` 生成未收数量，复制显式批号。第一次映射得 10，提交 6 后同一原行再次映射得 4，提交 4 后映射无明细。ERPNext 将已提交接收数量按 `ste_detail` 累加，更新原行累计量和 `per_transferred`。两组原单百分比均为 0→60→100。

两次接收均由同一原生发出行生成，提交的收发两腿均具已提交 Bundle。原生 NO_BATCH 对照以独立测试 Batch 代表内部桶。真正的 `HP-NB-` 映射使用下节 App。 [ERPNext Goods in Transit](https://docs.frappe.io/erpnext/how-to-manage-material-transfers-and-goods-in-transit) · [End Transit 映射器](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/stock/doctype/stock_entry/stock_entry.py#L5166) · [已接收数量校验](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/stock/doctype/stock_entry/stock_entry.py#L4906)

原生异常对照在保存点内进行，逐笔回滚后快照相同。均对 STD61 和内部桶执行。

| 原生对照 | 观察 | 判定 |
| --- | --- | --- |
| 剩余在途 4，尝试收 5 | 在途批次库存不足，抛出 `BatchNegativeStockError` | **原生通过** |
| 临时增加同批次 1，使物理在途可用量为 5，再对原行尝试收 5 | 原行累计量仍超过发出 10，抛出 `Transferred quantity cannot be greater than the requested quantity` | **原生通过**，证实独立于仓库总可用量 |
| 对在途仓进行普通 Material Issue | 原生有权限的身份可以提交 | **失败**，在途隔离需要补充 |
| 接收改为其他目标仓位 | 原生接受该目标 | **失败**，发出时需固定目标 |
| 在途另有 STD62 时，将关联接收行改成 STD62 | 原生允许扣减 STD62 | **失败**，必须强制验证发出批次 |
| 完成后再次运行 End Transit 映射 | 没有可生成的收货行 | **原生通过** |
| 业务请求 ID 与普通账号仓库权限 | 原生对照没有业务 requestId 或专用角色 | **未执行**，由适配实验覆盖 |

原生探针还发现 Stage 1C 的独立 Hook 在无 `items` 子表的非受管 SLE 或 Bundle 上会迭代空值。已修复该 App 兼容性问题，复跑原生流程通过。Frappe 与 ERPNext 核心保持不变。

## 3. 最小适配

独立 App 复用 Stage 1B 的实体批次和 NO_BATCH 映射。对外 `batch` 为 STD61 或 JSON `null`。两条命令为 `POST /api/method/haxpower_platform.transit.dispatch` 和 `POST /api/method/haxpower_platform.transit.receive`。入口只接受配置的实验 SKU 和普通叶级仓位，Transit 属于内部路径。发出和接收均要求 Operator 角色及源、目标叶仓授权，收货还校验 SKU、批次、目标和原发出行。

原生 Stock Entry 与原行提供累计量，适配接口由 `发出量 − 累计接收量` 返回 `In Transit`、`Partially Received` 或 `Completed`。原型将固定目标、域批次和请求信息记录在受保护的 Stock Entry `remarks` 中，接收单继续使用原生的三处关联字段。App 仅在服务上下文内为受管单据生成 Bundle，普通接口和原生写入口均不可直接取用受管在途库存。 [移库适配代码](../dev/erpnext-lab/haxpower_platform/haxpower_platform/transit.py) · [库存权限 Hook](../dev/erpnext-lab/haxpower_platform/haxpower_platform/stock.py)

输入及业务比较使用 Decimal。MariaDB 实测为 `REPEATABLE-READ`。每次实验移库命令先锁定实验公司行，再以 `FOR UPDATE` 读取请求标记、原发出行与指定批次流水的当前状态。接收提交后核对累计量，收发后核对两条 SLE、仓位和 Bundle 子行。Frappe 将这些检查与原生提交、幂等记录放在同个请求事务。相同身份和请求 ID 重试返回原收货标识，修改其负载返回冲突。重新使用已经完成的移库发起新接收则拒绝。

## 4. E07、E08、E09 数量与对账

以下为各 SKU 主批次的数量，单位 PC。两组在原生和适配试验中均得到同样的数量变化。

| 阶段 | STD61 源 / 在途 / 目标 | NO_BATCH 源 / 在途 / 目标 | 判定 |
| --- | --- | --- | --- |
| 初始 | 12 / 0 / 0 | 12 / 0 / 0 | 期初已核对 |
| E07 发出 10 | 2 / 10 / 0 | 2 / 10 / 0 | **原生通过**（在途入账）。**适配后通过**（含普通出库隔离和仓库权限） |
| E08 接收 6 | 2 / 4 / 6 | 2 / 4 / 6 | **原生通过**（原单分次接收与超收校验）。**适配后通过**（固定目标、批次及重试） |
| E09 接收 4 | 2 / 0 / 10 | 2 / 0 / 10 | **原生通过**（累计完成）。**适配后通过**（锁定收货、完成后拒绝新接收） |

每组指定主批次各阶段合计 12。STD62 在 W1/L1 始终 20，W1/L2 始终 2，未移入在途或目标仓。因此包括其他批次的每组总量始终 34。SLE 有符号数量与各 Bundle 子行完全一致，逐批查询之和与 Bin 一致。每次移库产生两条方向相反的 SLE 和两组精确 Bundle。零估值配置下库存价值和变动额为 0，新实验公司无 GL Entry。

| 适配组 | 发出 Stock Entry / 原行 | 第一、二次接收 Stock Entry | 最终结果 |
| --- | --- | --- | --- |
| STD61 | `MAT-STE-2026-00015` / `bqchnqlg6j` | `MAT-STE-2026-00016`、`MAT-STE-2026-00017` | 累计 10、在途 0、100%、Completed |
| NO_BATCH | `MAT-STE-2026-00018` / `cqsj0f5ami` | `MAT-STE-2026-00019`、`MAT-STE-2026-00020` | 累计 10、在途 0、100%、Completed |

适配组的 12 笔正式 Stock Entry（6 笔实验入库和 6 笔移库）、18 条 SLE、18 个 Bundle 已从新连接核对。STD61 的初始数量由已提交期初 SLE 和 Bundle 重建，NO_BATCH 还在发出前实测初始余额。两条接收单均指回各自发出单的同一原行。详细证据保留每阶段单据、SLE、Bundle 和原行标识。

## 5. 适配异常、权限和并发

| 用例 | 两组实测 | 判定 |
| --- | --- | --- |
| 未明确批次、所选批次数量不足（同仓 STD62 充足） | 417，`BATCH_SELECTION_REQUIRED` 或 `INSUFFICIENT_STOCK` | **适配后通过** |
| 首次接收 6 后再收 5、完成后再发起新收货 | 417，`TRANSFER_RECEIPT_EXCEEDS_REMAINING` 或 `TRANSFER_COMPLETED` | **适配后通过** |
| 同请求 ID 重复接收、同 ID 改数量 | 复用原接收标识，库存不变。不同负载 417 `IDEMPOTENCY_CONFLICT` | **适配后通过** |
| 错误 SKU、批次、已授权但错误的目标库位 | 417，`TRANSFER_RECEIPT_MISMATCH` | **适配后通过** |
| Query 发出/接收 | 403，`INVENTORY_OPERATION_DENIED` | **适配后通过** |
| 仅有源权限、仅有目标权限的 Operator | 两阶段均 403，`WAREHOUSE_ACCESS_DENIED` | **适配后通过** |
| 普通接口把 Transit 当源仓 | 403，`WAREHOUSE_ACCESS_DENIED` | **适配后通过** |
| 原生 Stock Entry 尝试直接出库、发出或关联收货 | 业务 Operator 均 403。有原生库存和在途仓权限的对照也均 403，命中受管文档 Hook | **适配后通过** |
| 两个使用不同请求 ID 的请求同时竞争最后 4 PC | 每组恰有一个 200，另一个 417 `TRANSFER_COMPLETED`。每个发出单最终只有两笔有效接收 | **适配后通过** |

每组 22 个顺序拒绝探针，逐一比对 14 张相关表，无单据、批次、Bundle、余额或流水变化。并发组经两个独立请求上下文运行，核对实际终值和单据数量。后续复跑重用同一发出和接收单据。公司行锁串行化本实验公司内的适配移库。多行、同一 SKU 多张发出单、不同公司锁、异常重试、撤销、后台重估和高并发压力 **未执行**。生产化需要单独评估锁粒度、带唯一约束的请求记录及与其他库存入口的锁顺序。 [权限设计](PERMISSIONS.md) · [库存模型](INVENTORY-MODEL.md) · [API 契约](API-CONTRACTS.md)

## 6. 回归与复现

仅需已初始化的 Stage 1B 实验站点。同步独立 App 到现有 Bench 的 `apps/haxpower_platform`，清理该站点缓存。原生脚本使用独立虚构公司和保存点，结束后整体回滚。适配脚本首次提交独立实验单据，复跑进入幂等分支。脚本通过 WSGI 在进程内发请求，沿用现有容器和数据库。

```bash
stage1d_container=haxpower-erpnext-v16-lab-frappe-1
stage1d_bench=/workspace/development/frappe-bench
stage1d_tools=$(docker exec "$stage1d_container" mktemp -d /tmp/haxpower-stage1d.XXXXXX)
tar -C dev/erpnext-lab -cf - stage1b_verify.py stage1c_verify.py stage1d_native.py stage1d_verify.py |
  docker exec -i "$stage1d_container" tar -xf - -C "$stage1d_tools"
docker exec -w "$stage1d_bench/sites" "$stage1d_container" \
  "$stage1d_bench/env/bin/python" "$stage1d_tools/stage1d_native.py"
for stage1d_mode in transfers stage1b stage1c; do
  docker exec -w "$stage1d_bench/sites" "$stage1d_container" \
    "$stage1d_bench/env/bin/python" "$stage1d_tools/stage1d_verify.py" "$stage1d_mode"
done
```

证据保存在实验目录 `stage1d-20261118/`。权限测试仅使用虚构用户，临时认证凭证不写入证据。

Stage 1B 的八笔请求全部重放，回归文件与历史文件逐字节相同。Stage 1C 的 225 个权限探针全部复跑，角色、权限及请求结果与历史矩阵相同。Stage 1D 两组再次运行后复用相同发出与接收单据。现有验证工具测试在普通和优化模式下各 17/17 通过，报告链接、命令语法和证据摘要检查通过。

| 证据 | SHA-256 |
| --- | --- |
| `native.json` | `618da8238e303d72311fe53e5a1d7b95ff4c8f7f7c3478c9e738c495854797b8` |
| `transfers.json` | `e5d127058ba20a02ced52a890a5dc27efb8da4c34e85fdd474e58782ed4d3b98` |
| `transfer-replay.json` | `6eed6f44077de0238405d4895798a6adad5f9efd5c2a22a52465ce5dc88041a4` |
| `stage1b-regression.json` | `3a1456289da66b3e31bc3b039ce9d864e768a62b15f017e9cb79733e89e91327` |
| `stage1c-regression.json` | `208f022aad00e49e1310fb51d7f66fa66de49e845ef651dae1a9a446de256a76` |

## 7. 技术选型影响

ERPNext 原生在途与分次接收可复用，无需核心改造。ERPNext 路线的主要剩余工作是受控入口、目标及批次约束、跨流程锁和可追踪的冲销。Frappe-only 可统一库存权限和批次约束，但需自行维护在途、部分接收、余额、并发及账簿一致性。建议保留 ERPNext 作为候选，优先审查本轮权限和锁的覆盖范围，再决定是否授权盘点等后续验证。 [库存业务流程](BUSINESS-FLOWS.md) · [项目决策](DECISIONS.md) · [实验验收计划](ERPNext-VALIDATION-PLAN.md)
