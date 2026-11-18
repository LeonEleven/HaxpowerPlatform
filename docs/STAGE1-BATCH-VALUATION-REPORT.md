# Stage 1 — ERPNext 原生批次与库存估值验证

2026-11-18（Asia/Shanghai）进行技术可行性验证。**库存实测阻塞，等待用户完成 Setup Wizard。**

`haxpower.localhost` 的 Frappe 与 ERPNext 初始化标志均为 `0`，数据库中没有 Company。此次执行了只读环境与数据基线检查，未创建测试档案、库存单据、Bundle 或系统 Batch。源码分析用于定位恢复后的测试路径，原生用例均按阻塞记录。

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
| 向导 | `frappe.is_setup_complete() == False`，两条 `Installed Application.is_setup_complete` 均为 0，`System Settings.time_zone` 为空 |
| 审批和负库存 | 没有 Stock Entry 活动 Workflow，`allow_negative_stock=0` |
| 批次设置 | `enable_serial_and_batch_no_for_item=0`，`use_serial_batch_fields=1`，`auto_create_serial_and_batch_bundle_for_outward=1`，拣选策略 `FIFO`，命名序列前缀 `BATCH-` |
| 估值与会计 | 没有 Company，可用的公司估值方法、永续盘存开关和默认科目尚无实测值 |

上述 Stock Settings 来自向导完成前，应在向导后重新读取。所用镜像见 [实验版本锁文件](../dev/erpnext-lab/versions.lock.json)。

通过 `frappe.get_all`、`frappe.db.count` 和 `frappe.db.get_single_value` 读取元数据、计数与设置。已检查的数据范围内未发现不明来源业务记录。

| 检查范围 | 基线与复查 |
| --- | --- |
| Company、Warehouse、Item、Batch、Bin、Brand、UOM、Account、Cost Center、Workflow | 各 0 |
| Stock Entry、Stock Reconciliation、Stock Ledger Entry、Serial and Batch Bundle、Serial and Batch Entry | 各 0 |
| GL Entry、Repost Item Valuation | 各 0 |
| Purchase Order、Sales Order、Purchase Invoice、Sales Invoice、Purchase Receipt、Delivery Note | 各 0 |

## 2. E01、E02、E04、E05 执行结果

计划数据为虚构 SKU-A（喷油嘴，品牌甲），单位 PC，整数数量。W1/L1 的目标为 STD61 12、STD62 20、无批次 15。验证计划还要求 W1/L2 独立测试无批次 12。库存单据拟使用 `frappe.get_doc({...}).insert()` 与 `doc.submit()`，真实批次由标准 Batch DocType 建档并通过单据批次字段或 Bundle 分配。各组提交前后均须留存独立快照。 [用例与通过标准](ERPNext-VALIDATION-PLAN.md) · [Frappe Document API](https://docs.frappe.io/framework/user/en/api/document)

| 用例 | 标准单据和输入 | 预期业务结果 | 实际状态 |
| --- | --- | --- | --- |
| E01 | Material Receipt，先向 W1/L1 入 STD61 12、STD62 20，再尝试空批次 15。W1/L2 尝试空批次 12 | W1/L1 逐桶独立且合计 47，W1 合计 59，查询和导出能表示领域空值 | **阻塞**。未建立 SKU、仓库、真实批次或库存基线 |
| E02 | 独立组 Material Receipt，空批次入 3 并重复，再入 STD61。另用独立 Item 对照启用自动建批 | 空批次反复进入同一无批次桶，真实批次独立，观察是否创建 Batch | **阻塞**。无入库提交，未观测拒绝、自动生成或入账 |
| E04 | Material Issue，请求明确扣无批次 4。无批次 15 基线依赖 E01 | 只扣无批次 15→11，STD61 仍为 12、STD62 仍为 20 | **阻塞**。未建立无批次基线。原生空字段与显式选择语义待实测 |
| E05 | 原生 Material Issue 和 Material Transfer，源仓位有真实批次，省略批次字段与 Bundle。另测旧字段空值及自动分配关闭时的负例 | 拒绝提交，源、目的、单据及流水无变更。缺少批次标识的领域 BATCH 请求也应拒绝 | **阻塞**。无原生提交。领域 API 负例 **未执行** |

各阻塞项的观测错误是前置检查的 `setup_complete=false`。没有提交库存单据，因此没有库存接口错误、单据 ID、流水增量或 Bundle 分配可供记录。NO_BATCH 适配一律 **待验证**。E03、E06–E13、E15–E16、S01–S06 **未执行**。

## 3. 锁定版本源码所示的原生批次限制

- **启用批次的 Item 在入账时要求 Bundle。** 对普通 Material Receipt，Batch 和 Bundle 都未指定时，源码允许自动建批的 Item 进入创建分支。关闭自动建批且没有可复用的在途 Bundle 时，SLE 在后续校验要求批号或序列号。预期失败信息包含 `Serial No / Batch No are mandatory for Item …`，实际异常位置与消息仍须从提交请求取证。 [ERPNext Batch 文档](https://docs.frappe.io/erpnext/batch) · [入账批次处理](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/stock/serial_batch_bundle.py#L410) · [SLE 强制校验](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/stock/doctype/stock_ledger_entry/stock_ledger_entry.py#L332)
- **自动出库分配可能接受空字段。** `StockEntry.make_serial_and_batch_bundle_for_outward` 在适用的出库行、缺少 Bundle 且打开自动创建时可构造 Outward Bundle，批次选取读取 Stock Settings 的拣选策略。当前原生配置有自动创建和 FIFO，预计会从可用真实批次分配。应分别复测 Bundle 模式和旧批次字段模式，记录选中的批次和数量。 [Stock Entry 自动分配](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/stock/doctype/stock_entry/stock_entry.py#L2382) · [原生 Bundle 文档](https://docs.frappe.io/erpnext/serial-and-batch-bundle)
- **SLE 的单列空值无法确认无批次。** 批号分配可以来自 `serial_and_batch_bundle` 对应的 `Serial and Batch Entry.batch_no`。必须按单据行、仓位、Bundle 明细对账。禁用 Item 批次管理会同时失去该 Item 的真实批次分配能力。 [SLE 批次校验](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/stock/doctype/stock_ledger_entry/stock_ledger_entry.py#L365) · [原生逐批报表查询](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/stock/report/batch_wise_balance_history/batch_wise_balance_history.py#L186)

直接允许一个批次 Item 的库存流水缺少 Bundle 将触及核心校验。源代码支持通过真实 Batch 和 Bundle 入账的候选映射。两条路径的业务结果仍需在指定版本实测。

## 4. 每 SKU 一个内部 NO_BATCH Batch

**状态为待验证。** 候选方案在独立 Frappe App 中维护映射，经标准单据与 Bundle 进入 ERPNext 库存。设计上无需修改核心。若不能通过受支持的接口和 Hooks 管控，应按项目验收条件重新评审库存引擎路线。 [适配性验收方案](ERPNext-FIT-GAP.md)

- **身份与冲突。** 为每个 SKU 绑定不可变 UUID，以保留前缀 `HP-NB-` 加 32 位 UUID 十六进制生成内部 ID。映射分别对 SKU 和 Batch 设唯一约束，并校验 Batch 属于同一 Item。并发初始化复用已提交映射，遇到已有未归属记录时中止并报告。真实批次的自由文本按 SKU 管理，内部主键另行分配。ERPNext `Batch.autoname` 使用手工 `batch_id` 作为 DocType 全局名称，因此两个 SKU 不能共享同一个内部主键。 [Batch 命名源码](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/stock/doctype/batch/batch.py#L120)
- **输入与输出。** 按现有 API 草案接受显式 `batchSelection.mode=NO_BATCH`，并按当前 SKU 查映射。收到 `BATCH` 时必须提供真实批次标识，解析失败或指向系统 Batch 时拒绝。出库、移库、盘点缺少 `batchSelection` 时在创建原生单据前拒绝。公共 API、详情、库存汇总和导出统一输出 `batch=null`，通过已登记的映射识别系统 Batch。入库依照草案显式表达空批次。 [项目 API 契约](API-CONTRACTS.md)
- **写保护与权限。** 仅允许受控服务创建和引用内部 Batch。用独立 App 服务端校验与文档事件限制创建、复制、改名、合并、停用、删除、过期日期变更及手工 Bundle 绑定。普通用户只能通过受仓库范围约束的业务入口选择“无批次”。标准单据、导入、表单、REST 和后台路径都要验证服务端权限。提交前验证 Bundle 的 SKU、仓位、唯一批次与精确数量，避免 FIFO 分到系统桶或跨桶补足。 [Frappe Hooks 文档](https://docs.frappe.io/framework/user/en/python-api/hooks)
- **库存单据影响。** 入库为目标库位创建 Inward Bundle，重复空批次入库复用 SKU 映射。出库使用限定源库位的 Outward Bundle 并拒绝数量不足。盘点要测试同仓位其他批次保留、指定桶盘零以及调整明细。移库发出和接收分别保留同一系统 Batch 的仓位分配，核对部分接收余量、在途权限和并发超收。这些行为均待实测。
- **报表和 API 可见性。** 源码显示标准 Batch-wise Balance History 直接读取 Bundle 的 `batch_no`，Stock Ledger 的逐批展示也读取它。标准 REST 对有权限的调用方返回 DocType 字段。因此预测原生报表、详情、REST 或导出会暴露内部 Batch ID，实际权限与角色范围待验证。面向外部仅开放受控查询和导出，并验证普通角色无法从标准入口绕行。内部诊断报表可保留真实原生记录。 [逐批余额报表源码](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/stock/report/batch_wise_balance_history/batch_wise_balance_history.py#L186) · [SLE 报表源码](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/stock/report/stock_ledger/stock_ledger.py#L284) · [Frappe REST 详情源码](https://github.com/frappe/frappe/blob/90c8b61afd60ee49f982ca1a9751008e4ae1e24c/frappe/api/v1.py#L75)

## 5. E14 第一部分 — 库存估值

**状态为阻塞。** 只读基线中的 SLE、GL Entry、Repost Item Valuation 均为 0，并无已生效的 Stock Entry。当前零行数不能回答交易是否会记账或触发重估。

向导完成后应使用新建的虚构公司分别验证 `enable_perpetual_inventory=0`、`=1`，在首笔库存业务前确定配置。在每个公司内为各入库条件使用不同的虚构 Item 和库位，使用非批次 Item 消除批次门槛的干扰。库存单据只用 Material Receipt/Issue。记录 Company 币种和估值方法、Warehouse 科目、成本中心、差异科目及 Item 的基础价格来源。原先已有交易的公司保持原配置。

| 子项 | 可复现输入及观测 | 状态 |
| --- | --- | --- |
| E14-1 未填估值率入库 | 收 10 PC，省略 `basic_rate`，`allow_zero_valuation_rate` 关闭，无历史、标准率或物料价格。检查保存与提交时是否回填或拒绝 | **阻塞** |
| E14-2 显式零估值入库 | 独立组收 10 PC，`basic_rate=0`。先测常规校验，再在独立组按原生字段显式允许零估值，记录手工估值标志及最终持久化字段 | **阻塞** |
| E14-3 非零对照 | 独立组收 10 PC，手工使用 `basic_rate=7.50` 且无附加费用，核对预期金额 75.00 与实际金额 | **阻塞** |
| E14-4 零估值出库 | 从已确认的零估值组出 4 PC，预期余额 6，记录源流水及出库前后金额、出库行零估值选项 | **阻塞** |
| E14-5 账簿与总账 | 每单按 voucher 读取 SLE 的 `incoming_rate`、`outgoing_rate`、`valuation_rate`、`stock_value`、`stock_value_difference` 和 GL Entry 借贷金额。逐例核对是否要求人工补配财务科目及成本中心 | **阻塞** |
| E14-6 后台与单据依赖 | 读取本组相关 Repost Item Valuation 及队列/worker 状态。核对采购/销售订单、发票、收货单和发货单无新增 | **阻塞** |
| E14 后续矩阵 | 移库、部分接收、盘点、冲销、纠错与未来成本切换 | **未执行** |

锁定源码显示需要实测的几处配置和行为。

- `StockEntry.set_basic_rate` 对目标行可能回查历史、Item 估值、标准率或物料价格。零估值选项和手工估值会改变计算分支。无可用估值率时，抛错条件还取决于是否允许零估值、调用时是否要求抛错、公司是否启用永续盘存。 [Stock Entry 基础估值](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/stock/doctype/stock_entry/stock_entry.py#L1944) · [估值回查与报错](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/stock/stock_ledger.py#L2562)
- Stock Entry 提交先更新库存流水，再调用总账和重过账逻辑。普通库存行的 GL 路径与公司永续盘存有关。启用永续盘存时还会校验差异科目。默认公司初始化会生成标准科目，应区分公司建档依赖、交易是否必须手工选科目及交易实际生成的 GL。已存在有效库存流水的公司受关闭永续盘存的保护校验。关闭永续盘存仍需核对 SLE 的库存价值字段。 [Stock Entry 提交流程](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/stock/doctype/stock_entry/stock_entry.py#L695) · [总账分支](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/controllers/stock_controller.py#L336) · [公司保护校验](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/setup/doctype/company/company.py#L646) · [永续盘存文档](https://docs.frappe.io/erpnext/perpetual-inventory)
- 有后续 SLE、强制重过账或满足队列重过账条件时，可创建 Repost Item Valuation。当前 scheduler 关闭，后续复测若产生任务，应记录任务状态与最终账簿，再得出结论。 [重过账触发条件](https://github.com/frappe/erpnext/blob/8e10a348daaa93853de8c36dc55ec789a440c219/erpnext/controllers/stock_controller.py#L2173)

## 6. SLE、Bundle 与余额对账

已观察到空站点的 `Bin=0`、`SLE=0`、`Bundle=0`、`Serial and Batch Entry=0`。这四项是行数，SKU-A 尚不存在，逐批库存与金额实测为 **阻塞**。

恢复后每笔记录 Stock Entry 名称、行 ID、提交状态和错误。以 `voucher_type`、`voucher_no`、`voucher_detail_no` 从 SLE 关联 Bundle 子行并确认 SKU、库位、方向。使用 Decimal，校验同一 SLE 下有符号数量之和等于 `actual_qty`，各批次有效流水求和等于逐批报表余额，仓位所有批次合计等于 `Bin.actual_qty` 及最新 SLE 的 `qty_after_transaction`。同时核对其他批次、SKU、库位。被拒绝的请求也应从新事务重读单据、Batch、Bundle、SLE、Bin，E14 同时重读 GL 与重过账记录。先前已单独保存的草稿应维持原状态。单次事务中的新建加提交失败应回滚整笔。

## 7. 复现当前阻塞与恢复条件

以只读方式复现向导标志和库存基线。命令沿用现有容器和实验站点，输出只含标志、应用名与计数。

```bash
docker exec -i --workdir /workspace/development/frappe-bench/sites \
  haxpower-erpnext-v16-lab-frappe-1 /workspace/development/frappe-bench/env/bin/python - <<'PY'
import frappe

frappe.init(site="haxpower.localhost", sites_path=".")
frappe.connect()
try:
    print("setup_complete", frappe.is_setup_complete())
    print(frappe.get_all("Installed Application", fields=["app_name", "is_setup_complete"]))
    for doctype in (
        "Company", "Warehouse", "Item", "Batch", "Bin", "Stock Entry",
        "Stock Ledger Entry", "Serial and Batch Bundle", "Serial and Batch Entry",
        "GL Entry", "Repost Item Valuation",
    ):
        print(doctype, frappe.db.count(doctype))
finally:
    frappe.db.rollback()
    frappe.destroy()
PY
```

1. 用户通过现有 VSCode 本地转发打开 `http://localhost:8000`，以自己的现有会话或 Administrator 登录，进入 Setup Wizard。完成站点初始化时只使用虚构测试公司名、缩写和必要资料，时区选 Asia/Shanghai。登录凭据由用户自行保管。
2. 用户完成向导并确认后，重新运行只读检查，要求 `frappe.is_setup_complete()` 为 `True` 且两应用标志均为 `1`。核对向导所建公司和仓库的来源、时区与初始库存。如果发现不明来源业务记录，保留现场并报告。
3. 前置复验通过后，为各用例组分配唯一运行编号，建立虚构 Brand、PC 单位、物料、库位及批次，记录运行编号和各单据 ID。原生空值探针先分别测试 `create_new_batch=0` 和 `=1`，以标准 Material Receipt 提交，随后从独立真实批次基线测试无批次出库和省略选择。复用既有运行的证据时，先按运行编号查已提交单据。遇到部分完成的编号先对账。重开实验使用新编号和隔离物料/库位，通过正式调整或冲销进行纠错。

现有验证工具单元测试在普通模式、`python -O` 模式分别 17/17 通过。现有 `verify.py --phase site` 在 10:47:33（Asia/Shanghai）开始复验，19/19 项通过，`configuration_only=false`。该命令检查安装、站点服务和资源。证据在实验目录的 `stage1-site-preflight-20261118.json`，SHA-256 为 `79aae9b0c53470b77eba5a5da176ee31165a7133193bf4879ba8bc8cded97a8a`。

## 8. 技术选型与下一轮验证

D009–D012 要求同 SKU 的真实批次与领域空值并存，出库、移库、盘点保留显式桶选择。锁定源码显示与 ERPNext 的 Item 级强制批次及自动拣选存在结构差距。D004–D005 要求 V1 只管理数量，ERPNext 的流水估值路径与库存提交耦合，是否可以避免财务配置负担和不需要的 GL 仍需实测。 [项目决策](DECISIONS.md) · [库存模型](INVENTORY-MODEL.md)

建议保留 ERPNext 作为候选，完成当前轮次的最早否决项再启动路线评审。先执行原生 E01/E02/E04/E05 和 E14 第一部分，取得成功及失败的事务级证据。在单独授权下，再验证 NO_BATCH 的精确扣减和绕行权限，并覆盖盘点、在途部分接收、并发、精度和冲销。若关键场景需要修改核心，或估值与权限负担无法符合验收标准，应比较 Frappe-only 与独立库存引擎。 [验证计划与停止条件](ERPNext-VALIDATION-PLAN.md)
