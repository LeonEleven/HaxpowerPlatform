# Stage 1 — ERPNext 原生批次与库存估值验证

2026-11-18（Asia/Shanghai）完成本轮技术可行性验证。**ERPNext 原生批次行为未满足同一 SKU 的无批次库存及显式选择要求。** 关闭永续盘存的隔离公司可以使用零金额的库存入库和出库，且本轮未产生 GL Entry。非零对照证明库存流水仍维护估值。每 SKU 一个内部 NO_BATCH Batch 的方案待验证。

首次检查时 `haxpower.localhost` 的两应用初始化标志均为 `0`，因此暂停库存写入。用户完成 Setup Wizard 后，两标志均为 `1`，时区为 Asia/Shanghai，检查范围内没有原有库存或财务交易。随后使用真实的 Document API `insert()` 和 `submit()` 执行测试，在同一数据库事务中读取证据并最终回滚所有测试档案、单据和临时配置。

## 1. 环境、配置与数据基线

GitHub 已确认 PR #1–#4 均合并至 `main`。开始时工作区及暂存区干净，已同步 `origin/main`，从 `dcdd34ee17af42579aefe9e4771baf554a5b9ce6` 创建 `spike/erpnext-native-stock-feasibility`。

| 项目 | 只读实测 |
| --- | --- |
| 实验范围 | `/home/h-goal/labs/haxpower-erpnext-v16`，Docker Desktop / WSL 2 / Ubuntu 24.04。站点为 `haxpower.localhost` |
| Frappe | 16.55.0，`90c8b61afd60ee49f982ca1a9751008e4ae1e24c` |
| ERPNext | 16.55.0，`8e10a348daaa93853de8c36dc55ec789a440c219` |
| 核心源码 | 两个工作区均干净，提交与 Stage 0 一致。站点只安装 Frappe、ERPNext |
| 运行时 | Bench venv Python 3.14.10，MariaDB `11.8.9-MariaDB-ubu2404`，两个 Redis 均为 7.4.12 |
| 容器 | `frappe`、`mariadb`、`redis-cache`、`redis-queue` 均 running / healthy，实际镜像引用与锁文件一致 |
| Bench | 同一组 Honcho（PID 7140），一个在线 worker。`bench doctor` 显示站点 scheduler disabled / inactive，站点任务列表无条目。沿用现有进程 |
| 向导 | 恢复时 `frappe.is_setup_complete() == True`，两条 `Installed Application.is_setup_complete` 均为 1，`System Settings.time_zone=Asia/Shanghai` |
| 审批和负库存 | 没有 Stock Entry 活动 Workflow，`allow_negative_stock=0` |
| 批次设置 | `enable_serial_and_batch_no_for_item=0`，`use_serial_batch_fields=1`，`auto_create_serial_and_batch_bundle_for_outward=1`，拣选策略 `FIFO`，命名序列前缀 `BATCH-` |
| 估值与会计 | 向导公司使用 CNY、FIFO 和永续盘存。测试使用四个新建虚构公司，两种估值对照公司的永续盘存分别为 0 和 1 |

脚本仅在实验事务内打开批次功能，并对照测试关闭和开启自动出库分配。原有公司的会计配置不参与提交。所用镜像见 [实验版本锁文件](../dev/erpnext-lab/versions.lock.json)。

通过 `frappe.get_all`、`frappe.db.count` 和 `frappe.db.get_single_value` 读取元数据、计数与设置。已检查的数据范围内未发现不明来源业务记录。

| 检查范围 | 向导完成后基线与实验回滚后 |
| --- | --- |
| Company、Warehouse、Account、Cost Center、UOM、Item Group | 分别为 1、5、95、2、239、6 |
| Item、Item Price、Batch、Bin、Brand、Workflow | 各 0 |
| Stock Entry、Stock Reconciliation、Stock Ledger Entry、Serial and Batch Bundle、Serial and Batch Entry | 各 0 |
| GL Entry、Repost Item Valuation | 各 0 |
| Purchase Order、Sales Order、Purchase Invoice、Sales Invoice、Purchase Receipt、Delivery Note、Journal Entry、Payment Entry | 各 0 |

## 2. E01、E02、E04、E05 执行结果

最终实验编号 `HP-S1-19D9916B3B`，于 11:07:09（Asia/Shanghai）开始。虚构 SKU-A 是品牌甲喷油嘴，使用仅允许整数的 PC。通过标准 Item、Batch 和 Stock Entry DocType 建档和提交。STD61 与 STD62 为报告中的显示简写，原生 Batch ID 带实验前缀。E02 使用独立 Item，未动用 E01 的批次库存。 [用例与通过标准](ERPNext-VALIDATION-PLAN.md) · [Frappe Document API](https://docs.frappe.io/framework/user/en/api/document)

| 用例 | 标准单据、输入与预期 | 实测行为与库存变化 | 判定 |
| --- | --- | --- | --- |
| E01 | Material Receipt，W1/L1 入 STD61 12、STD62 20 和空批次 15。W1/L2 入空批次 12。目标为 L1 合计 47，W1 合计 59 | 原生两笔有批次对照均通过。空批次 15 和 12 提交均被拒绝。L1 总量 32，STD61=12、STD62=20。L2 总量 0 | **失败** |
| E02 | 独立组两次 Material Receipt，每次 3 PC 且省略批次，分别传 `use_serial_batch_fields=0/1`。随后入指定 STD61 3。预期两次空批次入同一桶 | 未启用自动建批时，两次空批次提交均被拒绝。指定 STD61 入 3 通过。独立自动建批对照收到 3，但生成了实体 Batch `HP-S1-19D9916B3B-AUTO-0001` 并全数分配到其 Inward Bundle | **失败** |
| E04 | Material Issue 显式 `batch_no=null` 出 4 PC。预期独立扣减无批次 15→11 | 原生无批次基线无法建立。关闭自动分配时拒绝，开启时从 STD61 扣 4，STD61 12→8，STD62 仍为 20。原生空字段无法表达独立无批次出库 | **失败** |
| E05 | Material Issue 分别走旧字段和 Bundle 模式，Material Transfer 走 Bundle 模式，均省略批次，数量 4。预期拒绝 | 自动分配关闭时三个负例均拒绝。默认开启时三个均提交，源库 STD61 12→8，STD62 仍为 20。直接移库的目标 W2/L1 得 STD61 4 | **失败**。关闭自动分配的三个原生子项通过 |

批次拒绝均在 `submit()` 抛出 `ValidationError`，消息为 `Serial No / Batch No are mandatory for Item <测试 Item>`。每笔拒绝回滚至请求开始前，单据及明细、Batch、Bundle、SLE、Bin、逐批余额、GL 与重估状态快照完全相同。成功的 E04、E05 探针及其流水先取证，再回滚各自保存点，以同一批次余额比较下一种模式。E05 领域请求缺失 `batchSelection` 或缺少 BATCH 标识的服务端校验 **未执行**。NO_BATCH 适配 **待验证**。E03、E06–E13、E15–E16、S01–S06 **未执行**。

## 3. 锁定版本源码所示的原生批次限制

- **启用批次的 Item 在入账时要求 Bundle。** Material Receipt 无 Batch、无 Bundle 时，关闭自动建批会在提交时碰到 SLE 强制校验。启用自动建批后，系统走正常批号创建和分配。两种行为均已实际复现。 [ERPNext Batch 文档](https://docs.frappe.io/erpnext/batch) · [入账批次处理](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/stock/serial_batch_bundle.py#L410) · [SLE 强制校验](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/stock/doctype/stock_ledger_entry/stock_ledger_entry.py#L332)
- **默认自动出库分配接受空字段。** 旧字段和 Bundle 模式下都实测得到 Outward Bundle，分配先入的 STD61。关闭自动出库分配让无选择原生负例拒绝，但仍需由业务 API 明确区分缺失和 NO_BATCH。 [Stock Entry 自动分配](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/stock/doctype/stock_entry/stock_entry.py#L2382) · [原生 Bundle 文档](https://docs.frappe.io/erpnext/serial-and-batch-bundle)
- **SLE 的单列空值无法确认无批次。** 批号分配可以来自 `serial_and_batch_bundle` 对应的 `Serial and Batch Entry.batch_no`。必须按单据行、仓位、Bundle 明细对账。禁用 Item 批次管理会同时失去该 Item 的真实批次分配能力。 [SLE 批次校验](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/stock/doctype/stock_ledger_entry/stock_ledger_entry.py#L365) · [原生逐批报表查询](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/stock/report/batch_wise_balance_history/batch_wise_balance_history.py#L186)

直接让批次 Item 的库存流水缺少 Bundle 会触及核心校验。一个符合 ERPNext 原生约束的适配方向是通过内部 Batch 和 Bundle 入账，具体行为见下节验证条件。本轮原生实验采用标准单据，核心源码保持干净。

## 4. 每 SKU 一个内部 NO_BATCH Batch

**状态为待验证。** 候选方案在独立 Frappe App 中维护映射，经标准单据与 Bundle 进入 ERPNext 库存。设计上无需修改核心。若不能通过受支持的接口和 Hooks 管控，应按项目验收条件重新评审库存引擎路线。 [适配性验收方案](ERPNext-FIT-GAP.md)

- **身份与冲突。** 为每个 SKU 绑定不可变 UUID，以保留前缀 `HP-NB-` 加 32 位 UUID 十六进制生成内部 ID。映射分别对 SKU 和 Batch 设唯一约束，并校验 Batch 属于同一 Item。并发初始化复用已提交映射，遇到已有未归属记录时中止并报告。真实批次的自由文本按 SKU 管理，内部主键另行分配。ERPNext `Batch.autoname` 使用手工 `batch_id` 作为 DocType 全局名称，因此两个 SKU 不能共享同一个内部主键。 [Batch 命名源码](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/stock/doctype/batch/batch.py#L120)
- **输入与输出。** 按现有 API 草案接受显式 `batchSelection.mode=NO_BATCH`，并按当前 SKU 查映射。收到 `BATCH` 时必须提供真实批次标识，解析失败或指向系统 Batch 时拒绝。出库、移库、盘点缺少 `batchSelection` 时在创建原生单据前拒绝。公共 API、详情、库存汇总和导出统一输出 `batch=null`，通过已登记的映射识别系统 Batch。入库依照草案显式表达空批次。 [项目 API 契约](API-CONTRACTS.md)
- **写保护与权限。** 仅允许受控服务创建和引用内部 Batch。用独立 App 服务端校验与文档事件限制创建、复制、改名、合并、停用、删除、过期日期变更及手工 Bundle 绑定。普通用户只能通过受仓库范围约束的业务入口选择“无批次”。标准单据、导入、表单、REST 和后台路径都要验证服务端权限。提交前验证 Bundle 的 SKU、仓位、唯一批次与精确数量，避免 FIFO 分到系统桶或跨桶补足。 [Frappe Hooks 文档](https://docs.frappe.io/framework/user/en/python-api/hooks)
- **库存单据影响。** 入库为目标库位创建 Inward Bundle，重复空批次入库复用 SKU 映射。出库使用限定源库位的 Outward Bundle 并拒绝数量不足。盘点要测试同仓位其他批次保留、指定桶盘零以及调整明细。移库发出和接收分别保留同一系统 Batch 的仓位分配，核对部分接收余量、在途权限和并发超收。这些行为均待实测。标准批次估值也会处理该桶，应核对混合成本、期初和重估下的行为。
- **报表和 API 可见性。** 源码显示标准 Batch-wise Balance History 直接读取 Bundle 的 `batch_no`，Stock Ledger 的逐批展示也读取它。标准 REST 对有权限的调用方返回 DocType 字段。因此预测原生报表、详情、REST 或导出会暴露内部 Batch ID，实际权限与角色范围待验证。面向外部仅开放受控查询和导出，并验证普通角色无法从标准入口绕行。内部诊断报表可保留真实原生记录。 [逐批余额报表源码](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/stock/report/batch_wise_balance_history/batch_wise_balance_history.py#L186) · [SLE 报表源码](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/stock/report/stock_ledger/stock_ledger.py#L284) · [Frappe REST 详情源码](https://github.com/frappe/frappe/blob/90c8b61afd60ee49f982ca1a9751008e4ae1e24c/frappe/api/v1.py#L75)

## 5. E14 第一部分 — 库存估值

**数量最小链路原生通过，是否接受内置估值仍需架构评审。** 两家隔离的虚构公司均使用 CNY、FIFO、标准科目表及各自的成本中心。`Periodic` 关闭永续盘存，`Evergreen` 开启永续盘存。每种入库输入使用独立的非批次 Item，无历史成本、标准率或 Item Price。所有收发只使用标准 Stock Entry。差异科目负例在另一家尚无交易的虚构公司中配置。

| 子项 | 关闭永续盘存 | 开启永续盘存 | 判定 |
| --- | --- | --- | --- |
| E14-1 省略估值率入 10 PC | 提交成功。余额 10，估值率 0，SLE 库存价值和变动额均为 0，无 GL | `submit()` 拒绝。错误指出记账需要估值率，余额不变 | 关闭时 **原生通过**，开启时 **失败** |
| E14-2 显式 `basic_rate=0` 入 10 PC | 无需允许零估值标志即可提交。显式启用标志和手工估值的对照也成功，均为零估值、无 GL | 单独给 0 仍在提交时拒绝。用 `allow_zero_valuation_rate=1`、`set_basic_rate_manually=1` 的对照提交，余额 10，估值率和金额均为 0，无 GL | 不设置标志时关闭 **原生通过**、开启 **失败**。显式允许零估值的两组均 **原生通过** |
| E14-3 非零对照 | 入 10 PC，手工单价 7.50，SLE 库存价值和变动额均为 75.00 CNY，无 GL | 相同 SLE 金额，生成两条 GL。借记库存 75.00，贷记库存差异 75.00 | **原生通过**，证实内置估值仍运行 |
| E14-4 零估值出库 | 出库 4 PC，余额 10→6。SLE 数量 −4，估值率、库存价值和金额变动均为 0，无 GL | 同左。出库行启用和关闭允许零估值标志的独立对照均成功 | **原生通过** |
| E14-5 财务科目依赖 | 原生建公司仍初始化 95 个科目和成本中心。公司库存科目、库存差异科目、仓库科目及交易 `expense_account` 均为空，收发成功 | 正常组使用公司默认库存科目和库存差异科目，单据自动带出差异科目与成本中心，仓库无需单独指定科目。独立负例移除默认差异科目且行上未指定科目，即使明确允许零估值也在 `insert()` 拒绝 | 关闭时数量操作 **原生通过**，开启时无差异科目的请求 **失败** |
| E14-6 重估与单据依赖 | 正常顺序记账未产生重估记录。未创建采购订单、销售订单、发票、采购收货单或销售发货单 | 正常顺序同左。先入 2 PC，再补录时间早 5 秒的 1 PC，产生一条已提交且为 `Queued` 的 Repost Item Valuation，`Bin.actual_qty=3` | **原生通过**，实测触发条件和单据独立性。重估任务消费与完成后的余额和估值 **未执行** |
| E14 后续矩阵 | 移库、部分接收、盘点、冲销、纠错与未来成本切换 | 同左 | **未执行** |

失败记录保留了原始异常和阶段。未填费率或零费率未开标志的提示以 `Valuation Rate for the Item … is required to do accounting entries for Stock Entry …` 开头。无差异科目时报 `Please enter Difference Account or set default Stock Adjustment Account for company …`。负例保存点回滚后均无单据、库存、Bundle 或 GL 变化。公司在已有流水时变更永续盘存受到原生保护，本轮在不同公司查验开关效果。 [入库估值源码](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/stock/doctype/stock_entry/stock_entry.py#L1944) · [差异科目校验](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/stock/doctype/stock_entry/stock_entry.py#L1022) · [GL 分支](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/controllers/stock_controller.py#L336) · [公司开关保护](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/setup/doctype/company/company.py#L646)

常规提交和补录探针均在事务内取证。补录探针中的 `Queued` 为事务内的单据状态。站点 scheduler 处于禁用状态，Redis 任务列表为空。实验结束时回滚了该重估记录，任务执行和完成后的账簿待验证。 [重过账触发条件](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/controllers/stock_controller.py#L2173)

## 6. SLE、Bundle 与余额对账

共执行 31 次原生单据探针，20 次通过 `submit()`，11 次拒绝。每次成功都按 `voucher_no` 和 `voucher_detail_no` 连接 SLE、Bundle 与子行，使用 Decimal 对账。Bundle 子行的有符号数量和等于 SLE 的 `actual_qty`，逐批汇总等于原生 Batch-wise Balance History，按 Item 与仓位汇总的 SLE 等于 `Bin.actual_qty`。E01 的 SLE 批次列为空，但 Bundle 子行给出了真实批次。出库与移库对照中 STD62、E02 的两个独立 Item 和未参与仓位均保持原数量。

| 关键探针 | 单据编号后缀 | SLE 与 Bundle 方向 | 仓位余额 |
| --- | --- | --- | --- |
| STD61 入库 | 00001 | SLE +12，Inward Bundle STD61 +12 | W1/L1 12 |
| STD62 入库 | 00002 | SLE +20，Inward Bundle STD62 +20 | W1/L1 32 |
| E04 空字段出库，自动开 | 00005 | SLE −4，Outward Bundle STD61 −4 | W1/L1 28 |
| E05 省略字段移库，自动开 | 00005 | 源与目标 SLE −4/+4，各有独立的 STD61 Outward/Inward Bundle | W1/L1 28，W2/L1 4，合计 32 |
| E14 关闭永续盘存的非零入库 | 00009 | SLE +10，估值变动 +75.00，无 Bundle | 10 |
| E14 开启永续盘存的非零入库 | 00012 | SLE +10，估值变动 +75.00，无 Bundle，两条 GL | 10 |

同一单据后缀可在回滚后复用。以实验编号、探针标签、SLE 与明细 ID 识别记录。最终总回滚前保留了 14 笔成功提交及其 14 条 SLE、4 个 Bundle、6 条 GL 和 1 条 Repost Item Valuation。另 6 次成功提交已按探针保存点独立复位。总回滚后重查记录计数、配置及队列，结果与实验前一致。使用新数据库连接独立复核了向导标志、站点配置与空库存基线。

## 7. 复现与证据

从仓库根目录向现有容器传入脚本。脚本固定实验站点、目录及数据库主机，检查向导、版本、核心提交和业务表基线。各次运行使用唯一实验编号。数据写入使用标准文档 API，单据、流水、Bundle 与余额使用应用 API 或报表取证。测试以本地 Administrator 权限运行，普通员工权限、公共 API、跨请求提交后的可见性及重估消费均留待后续。 [实验脚本](../dev/erpnext-lab/stage1_native_stock.py)

```bash
umask 077
stage1_evidence=$(mktemp /tmp/haxpower-stage1-evidence.XXXXXX)
docker exec -i --workdir /workspace/development/frappe-bench/sites \
  haxpower-erpnext-v16-lab-frappe-1 /workspace/development/frappe-bench/env/bin/python - \
  < dev/erpnext-lab/stage1_native_stock.py > "$stage1_evidence"
sha256sum "$stage1_evidence"
```

复现批次拒绝时查看 `E01 empty L1`、`E02 empty legacy=0/1` 和 `E04 explicit null auto=0` 的输入、异常及前后快照。对比 `E02 auto-create`、`E04 explicit null auto=1` 和 `E05 omit … auto=1` 的自动分配。E14 的费率、科目和补录对照均可从相应标签定位。ERPNext 自身在逐批报表和估值兼容路径输出 v16 弃用警告，两次复跑得到一致的主要结果。

实验目录保留私有证据文件 `stage1-HP-S1-19D9916B3B.json`，SHA-256 为 `8caf5a80cb1fa029c5e5dbe28e487aae472355e68842218e42e8e3d7ccec69f5`。记录了每笔输入、单据状态与错误、单据和行 ID、SLE、Bundle 子行、余额、GL、重估和回滚后的基线。现有验证工具单元测试在普通模式、`python -O` 模式各 17/17 通过。站点完整复验 19/19 通过，证据位于实验目录的 `stage1-site-postrun-20261118.json`，SHA-256 为 `b093a07593a0512241496eb1565339104a66f8a89217778b0785a7a9fa4d6864`。

## 8. 技术选型与下一轮验证

D009–D012 要求同 SKU 的真实批次与领域空值并存，出库、移库、盘点须显式选择库存桶。原生实测显示必填 Bundle 和自动 FIFO 与这些规则冲突。D004–D005 要求 V1 只管理数量。关闭永续盘存可满足本轮最小收发的零金额和无 GL 目标，仍有公司科目表、库存估值和潜在重过账依赖，需在选型时评审。 [项目决策](DECISIONS.md) · [库存模型](INVENTORY-MODEL.md)

建议继续保留 ERPNext 候选，只进入单独授权的最小 NO_BATCH 适配验证。首要是证明服务端能拒绝未选择请求并精确扣减内部桶，随后验证标准报表、REST 和导出的越权访问、盘点、在途部分接收及并发和冲销。若这些能力需要修改核心，或会计及权限负担无法满足验收，应比较 Frappe-only 与独立库存引擎。 [验证计划与停止条件](ERPNext-VALIDATION-PLAN.md)
