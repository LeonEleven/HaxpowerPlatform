# Stage 2B ERPNext 轻量审计验证报告

2026-11-19，Asia/Shanghai。**在已测受控路径上，ERPNext 原生有效 SLE 与 Bundle 可作为唯一有效数量账，标准单据加轻量日志足以追溯本轮更正。** 建议将方案 A 作为 V1 审计基线。采用前仍须验收全面写入口隔离、备份恢复、盘点截止和下游纠错。
[架构决策记录](ARCHITECTURE-DECISION-RECORD.md) · [适配边界](ERPNEXT-V1-ADAPTATION-BOUNDARIES.md)

## 1. 环境和原生证据

PR #11 已合并，任务分支从同步后的 `origin/main`（`0d0bd4a95c302637c6c72e89d460545073834d3c`）创建。沿用现有 Bench 的 `haxpower-stage1b.localhost`，只在新增虚构公司 `HP Stage 2B Lab` 使用 7 个整数 PC SKU 写入库存。Frappe 16.55.0 为 `90c8b61afd60ee49f982ca1a9751008e4ae1e24c`，ERPNext 16.55.0 为 `8e10a348daaa93853de8c36dc55ec789a440c219`。MariaDB 11.8.9 为 `REPEATABLE-READ`。关闭永续盘存和自动出库分配，禁止负库存。测试走 Frappe WSGI 的真实令牌认证、授权、提交和回滚，每条请求使用独立上下文。
[PR #11](https://github.com/LeonEleven/HaxpowerPlatform/pull/11) · [事务模型](https://docs.frappe.io/framework/user/en/api/database)

| 原生记录 | 可复用的实测字段或历史 | Haxpower 补充 |
| --- | --- | --- |
| Stock Entry / Stock Reconciliation | `owner`、`creation`、`modified_by`、`modified`、记账日期/时间、`docstatus`。原生元数据无通用 `submitted_by`、`submitted_at`。两张发出单的 `owner` 仍为发出 Operator，`modified_by` 在接收后变为接收 Operator。 | 业务日志记录真实操作者与服务端操作时间，同事务持久化。记账时点允许与操作时点不同，最后修改人和最后修改时间不能作稳定提交证据。 |
| 原生单据行与状态 | Stock Entry 行有物料、源/目标仓位、数量与批次，盘点行有物料、仓位、账面量、实盘量和差额。Stock Entry 启用 Track Changes，实验每单有一条 Version。Stock Reconciliation 未启用父单 Track Changes，本轮该单无 Version。 | 保留业务操作类型、原因及关系。原生版本作为辅助历史，关键追溯由轻量记录补齐。 |
| 关联 | `amended_from` 适用于原生取消后的修订。两条接收单实测保存 `outgoing_stock_entry`、`against_stock_entry` 和 `ste_detail`。 | 新反向单需独立关联原操作及原单原行。部分退回需固化计划目标、退回后关闭接收，原生累计量仍表示已实收。 |
| 流水、批次与余额 | SLE 有原生单号/行号、SKU、仓位、变动量、记账时点、取消标志和 Bundle 指针。Bundle 子行标记批次与数量，Bin 汇总 SKU/仓位，标准批次查询提供逐批次余额。 | 对外映射 `NO_BATCH` 为 `null`，统一权限、对账及异常阻断。 |
| 备注、附件、审批及历史 | Stock Entry 有 `remarks`，锁定版本的 Stock Reconciliation 无此字段。元数据可用 Comment、File（含私有标记和单据关联）、Workflow 与 Workflow Action。实验未配置库存 Workflow，未上传附件或测试完整审批。 | 纠错原因独立持久化。按业务规则保存审批人与决策时点，实施附件访问、留存、下载和审计策略。 |

## 2. 轻量实现和结果

独立 App 新增 `Stage 2B Stock Operation`，记录请求键/负载摘要、业务类型、原生单据类型及 ID 和行、操作者、操作时点、关联操作、计划目标及原因。日志没有 SKU、批次、差额或余额字段，数量和维度从原生单据、SLE、Bundle 读取。每次成功库存操作一条日志，纠错通过关系引用原操作，再定位原单和原行。业务普通角色没有日志写权限，文档守卫阻止通用创建、改写、删除和改名。

公司行锁覆盖本实验的受控提交。请求键按公司、认证用户和请求 ID 计算，数据库唯一。负载摘要含规范化业务内容，纠错摘要含原操作和原因。同键同负载返回原结果，改负载或新键重复纠错拒绝。原生单据、SLE、Bundle、Bin 和日志随成功 POST 一并提交。库存错误和日志异常随请求回滚。
[轻量验证服务](../dev/erpnext-lab/haxpower_platform/haxpower_platform/lightweight.py) · [执行验证器](../dev/erpnext-lab/stage2b_verify.py)

| 场景 | 正式单据与新连接结果 |
| --- | --- |
| A 错误入库 | **通过。** STD61 原入库 `MAT-STE-2026-00087`，新增出库 `MAT-STE-2026-00088`。0 → 12 → 0，SLE 为 +12、−12。 |
| B 错误出库 | **通过。** 无批次原出库 `MAT-STE-2026-00090`，新增入库 `MAT-STE-2026-00091`。12 → 8 → 12，SLE 为 −4、+4。 |
| C 盘点差异 | **通过。** 无批次 `MAT-RECO-2026-00008`，原盘点 8 → 5，原生 SLE −8、+5，新增 `MAT-STE-2026-00093` 入 3，恢复 8。用例覆盖立即纠错，实际盘点任务与截止版本不在本轮原型中。 |
| D 部分接收后的退回 | **通过。** 无批次 `MAT-STE-2026-00095/00096/00097` 发 10、接 6、退 4，源 / 在途 / 目标从 2/10/0 到 2/4/6 再到 6/0/6。原生累计实收维持 6，返回单保留原发出关系，业务服务拒绝继续接收。 |
| E 完成移库且下游出库 | **更正阻塞，服务拒绝通过。** STD61 `MAT-STE-2026-00099/00100/00101` 发 10、接 10、目标出 7。最终 2/0/3。请求返回 `STAGE2B_TRANSFER_MANUAL_REVIEW`，提示人工核对目标库位和下游出库再确认补偿单据。无新增单据或日志。完整退回即使碰巧有可用余额也需业务审核。 |

A–D 在各自更正前后比对了原单、原行、原 SLE、Bundle 和日志全行。更正保留原始证据并记录原因、操作者、时点和原单关系，批次匹配。相同键重放稳定，改原因或换键重复提交均无副作用。最终新连接 5392 对账了 21 张 Stock Entry、1 张 Stock Reconciliation、22 条轻量日志、28 条有效 SLE 和 28 个 Bundle。原生批次查询与逐批流水相符，汇总与 11 条 Bin 一致，GL 为 0。

| 故障或争用 | 实际结果 |
| --- | --- |
| 更正单已提交，日志写失败 | **通过。** 连接 5336 内看到更正单、SLE 和 Bundle，日志尚无新行。日志控制器插入前抛异常，HTTP 417。新连接的原生记录与日志摘要等于请求前。 |
| 日志已插入，数据库提交失败 | **通过。** 连接 5340 内四类记录均可见。连接 5341 仅终止该测试请求连接，执行 COMMIT 时收到数据库 2013。新连接摘要相同，没有半完成记录。 |
| 同键并发重试 | **通过。** 连接 5345 / 5344 各发一次纠错，争用等待分别为 6 / 503 ms，包含实验延时。最终同一操作 ID，一次入账一次重放，库存 10 → 0。回滚后原生单号复用，以持久化对账和摘要判断实际结果。 |

## 3. 权限和后台边界

Query 写、只有一侧仓授权的 Operator 移库纠错、无批次桶不足均被拒绝。Operator、Query 和 Stock Manager 对日志通用 REST 创建、更新、删除的 9 个请求均为 403，Stock Manager 原生 Stock Entry 写入也命中守卫。Administrator 的标准原生取消、日志编辑/删除、带账簿重建的重估单创建及公司交易清理请求均被拦截。未执行实际清账或账簿重建。

运行了一个只针对实验单据的正常原生重估 `r5mmpb639i`。状态 Completed，2 条 SLE 的 `modified` 改变，SLE 身份、批次数量、Bundle、Bin 和日志摘要保持一致。Stage 1E 的原生取消证据显示原 SLE 会被标记取消、历史 Bundle 关联被清除及形成反向 SLE。在发生绕过时，轻量日志仍有原单引用，但无法用它独立重建数量或丢失的批次。从标准文档入口阻止取消和破坏性重估是必要条件。
[原生取消实测](STAGE1E-INVENTORY-INTEGRITY-REPORT.md#4-s04-正式纠错与审计链) · [原生重估边界](STAGE1F-AUDIT-COUNT-REPORT.md#4-原生写入和信任边界) · [ERPNext Immutable Ledger](https://docs.frappe.io/erpnext/immutable-ledger-in-erpnext)

应用层保护依赖受信任的服务代码、任务和数据库写权限。系统管理员的角色配置、后台已有队列、受信任服务器代码、DBA 和恢复工具能绕过文档生命周期。意外取消、版本重建等差异须在上线巡检中检测、阻断受影响范围并人工处理。本轮已测的是列出的入口与一次正常重估，未通过全面生产权限、定时任务、备份还原或追加审计外部留存验收。

## 4. 方案 A 与方案 B

| 维度 | A 原生有效账与轻量日志 | B Stage 1F 完整业务事件 |
| --- | --- | --- |
| 日常查询及追溯 | 原生账/标准查询给数量，日志给动作语义。纠错从关联原单、原行、Bundle 查回，已实测。授权详情和导出仍需开发。 | 同样使用原生执行，可从业务事件直接查询 SKU、库位、域批次及净变化，并与原生数量逐单对账。 |
| 异常与取消 | 同事务回滚和受控阻止通过。若原生取消绕开管控，日志能显示缺失/无效引用，数量明细只能从仍保留的原生事实恢复。 | 有独立业务差额和批次坐标，能检测原生与事件漂移并保留一份证据。恢复原生库存仍需正式流程。 |
| 业务角色、管理员与后台 | 原生角色、重估、重建和清理均需治理。普通重估样本只更新元数据，意外 SLE/Bundle 损坏对 A 的追溯影响更大。 | 同样要隔离所有高权限路径，还需保护完整事件及对账逻辑。业务纯追加不能阻止 DBA 或任意后台写。 |
| 备份恢复 | 同一恢复点保留原生单据、Bundle、SLE、Bin、Batch、幂等、日志和附件，恢复后检查缺少引用、已生效纠错、在途和逐批余额。 | 同样恢复原生对象，另恢复完整业务事件和版本相关表，并核对两套账和在途关系。两方案均未做实际恢复。 |
| 升级与长期维护 | 保留原生适配、锁、幂等、SLE/Bundle 查询、控制与对账巡检。实验日志 11 个字段，没有第二份数量向量。 | 升级外还需维护每类业务事件的维度、逐单/逐腿净额、两套账的漂移检测和重建工具。原型单行场景两方案均每单一条记录，当前样本不能量化生产容量的节省。 |

如业务只需在受控场景中查原单、责任与更正，应优先验收 A。若必须独立于原生账记录原始逐批数量，或无法可靠约束原生取消、重建及后台高权限，当前 A 风险不可接受。此时保留 B 或另外进行受审查的外部留存。恢复演练、旧盘点/回填、动态仓库完整检验、原生所有写入口以及下游补偿仍是门槛。
[Stage 1F 双账实测](STAGE1F-AUDIT-COUNT-REPORT.md) · [Frappe 恢复](https://docs.frappe.io/framework/user/en/bench/reference/restore)

## 5. 批次测试与复现

5 个独立 Python 测试覆盖三态、空白、空引号、保留前缀、不可打印字符和有意义的原文。真实 HTTP 另做 7 个拒绝探针，全部无数据残留。入库实测 `　STD61 ` 归一到 `STD61`，同键改用归一文本重放。无批次对外为 `null`，在同 SKU 下与 `STD61`、`Std61`、`Ａ-"B"` 各自对账。另用 10 条虚构旧值生成只读的清洗预览，异常未被静默改为无批次。正式清洗报告和归并复核要求见适配边界。
[批次输入和导入规范](ERPNEXT-V1-ADAPTATION-BOUNDARIES.md#批次输入规范)

在现有隔离 Bench 同步独立 App，仅对 `haxpower-stage1b.localhost` 加载新增的 `stage_2b_stock_operation`，随后清该站点缓存。将验证器及 `stage1b_verify.py`、`stage1c_verify.py`、`stage1d_native.py`、`stage1d_verify.py`、`stage1e_evidence.py` 放在同一容器临时目录。从 Bench 的 `sites` 目录运行。

```bash
hp_python=/workspace/development/frappe-bench/env/bin/python
hp_probe=/tmp/haxpower-stage2b-tools/stage2b_verify.py
for hp_mode in simple transfer batch fault permissions repost final; do
    "$hp_python" "$hp_probe" "$hp_mode" || break
done
```

写入模式使用固定虚构夹具，正常只运行一次。已有提交数据时可用 `final` 只读对账。重估模式会新增一条实验重估，故障模式仅终止其自己的负载连接。临时 API 密钥已撤销。

## 6. 回归和留存

Python 回归常规与优化模式各 22/22。Stage 1B 八笔请求和 Stage 1D 两组移库重放与历史结果逐字节相同。Stage 1C 复跑 225 个权限探针，角色和矩阵相同。Stage 1E 只读回归与历史证据逐字节相同。Stage 1F 从新连接核对库存、任务和事件，连接编号之外的证据与历史相同。Stage 1B–1F 的库存和业务摘要一致，54 份已有实验文件校验通过。

Frappe-only 站点的 272 张表前后一致。旧 ERPNext 主站 760 张可读表一致，已知的 `__global_search` 既有错误 145 前后相同，未读取其行内容。两套核心源码工作区在实验前后均干净。原有容器保持健康。

本轮脱敏证据与完整 `SHA256SUMS` 保存在实验目录的 `stage2b-20261119/` 子目录。

| 证据 | SHA-256 |
| --- | --- |
| `native-meta.json` | `93c455d266ebdab5ad8b725986e9a71d1bda838a79742a89d8157dc9401a3e5b` |
| `simple.json` | `73474505e713ed9f8fe90de03d8c1f9241e4ce9a2e6e61f9b2efc106565b5695` |
| `transfer.json` | `74dd9e668064e0514d37e93274c5efcfae74ee6bf020ea360cd78bcd58e0e0fb` |
| `batch.json` | `aa900e041795150b5e4c44d9d5347557e8665f4cfe12711ba4fe733c1b0e8247` |
| `fault.json` | `17b413fef4ceafe621e051c0c0f416ea8cc63c5819c63e443b5165da0d7b90bd` |
| `permissions.json` | `ad84052533ecc2b8cb5355a9596945c3518f434b4945e6b1ecbe846d69abb72f` |
| `repost.json` | `a819b3778744b4634cd318b442245912de1854884d5371f08160724e37093434` |
| `final.json` | `d96f9286d63625f3a07b9f8638a3a4a8831e114fcc6e4e20b29ca5120ed8a649` |
| `history-before.json` / `history-after.json` | `6c769e5258dc23d5b74d7c733b9d0dddac8dcb732077302c140952e51041e1a6` |
| `other-sites-before.json` / `other-sites-after.json` | `a54a0031f66027a171cb6330e555d78d338467caaadf1ab96ad4639e5d0e0b6d` |
