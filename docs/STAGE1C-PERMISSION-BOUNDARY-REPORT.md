# Stage 1C — ERPNext 库存权限边界验证

2026-11-18（Asia/Shanghai）完成技术可行性验证。**专用业务角色与自定义 API 通过本轮权限验证。** 授权用户可读写指定实验仓库，普通业务角色无法使用已测原生入口读取或改变库存。带 ERPNext 原生库存角色的对照账号依然能通过多处原生入口读取内部批次 ID。这项约束应纳入 ERPNext 选型。

## 1. 环境与保留数据

PR #6 已合并。本任务分支 `spike/erpnext-permission-boundary` 基于 `origin/main` 的 `2a85d4622d9e7c5865d7c15921e73ecd1b8e868a`。四个实验容器均健康，使用原有 Bench 和站点 `haxpower-stage1b.localhost`，数据库为 `haxpower_stage1b_lab`。Frappe 16.55.0 / `90c8b61afd60ee49f982ca1a9751008e4ae1e24c`，ERPNext 16.55.0 / `8e10a348daaa93853de8c36dc55ec789a440c219`，Python 3.14.10，MariaDB 11.8.9。测试 HTTP 仅监听容器回环地址，使用真实非管理员令牌认证。 [镜像锁文件](../dev/erpnext-lab/versions.lock.json)

开始与结束时均保有 Stage 1B 的 8 笔已提交 Stock Entry、8 行明细、8 条有效 SLE、8 个 Bundle、8 条 Bundle 明细、4 个 Batch 和 3 条 Bin。Stock Reconciliation、GL、重估及 Data Import 均为 0。八笔单据仍为 `MAT-STE-2026-00001` 至 `MAT-STE-2026-00008`。原始证据的三个 SHA-256 与 Stage 1B 报告一致。 [Stage 1B 报告](STAGE1B-NO-BATCH-ADAPTER-REPORT.md)

## 2. 测试角色与仓库范围

下表的 L1、L2 是 ERPNext 中两个不同的叶级 Warehouse，业务上同属 W1，均有 Stage 1B 真实库存。原型采用用户级精确授权，父仓不自动扩张。四类业务账号及角色叠加账号都没有 Stock User、Stock Manager、System Manager 或 Administrator 权限。

| 身份 | 显式角色 | 服务端仓库授权 | 自定义读 | 自定义写 |
| --- | --- | --- | --- | --- |
| Haxpower Operator | Haxpower Stage 1B Operator | L1、L2 | 两库允许 | 允许，HTTP 重放原实验出库成功 |
| Haxpower Query | Haxpower Stage 1C Query | L1、L2 | 两库允许 | 对已授权的 L1，入库和出库均 403 |
| Haxpower Restricted | Query、Haxpower Stage 1C Restricted | L1 | L1 允许，L2 403 | 即使 L1 已授权，入库和出库仍为 403 |
| Haxpower None | Query、Haxpower Stage 1C None | 空 | 两库均 403 | 入库和出库均 403 |
| Restricted 叠加 Operator | Operator、Query、Restricted | L1 | L2 仍为 403 | L2 入库和出库均 403 |
| Native Stock User 对照 | Operator、Stock User、Stock Manager、Item Manager | L1 | 自定义 L2 仍为 403 | 自定义 L2 入库和出库均 403 |
| Guest | 未登录 | 空 | 403 | 未执行 |

服务端读取当前认证用户的角色和 `User Permission`，要求存在指向指定叶级 Warehouse 且适用于所有 DocType 的授权。授权不从请求参数推导。直接请求 L2 和父仓原生 ID 均经过强制测试。授权读取经实际 HTTP 返回域数据 `STD61=7`、`STD62=20`、`batch=null, quantity=11`（L1）及 `batch=null, quantity=12`（L2）。响应中没有 `HP-NB-` 或 `HP-RB-`。没有实现动态 ALL 或完整仓库层级继承。 [权限设计](PERMISSIONS.md) · [Frappe User Permissions](https://docs.frappe.io/erpnext/user-permissions)

角色权限取证记录了 Batch、Bundle、Stock Entry、Stock Reconciliation、SLE、Bin、Data Import 与 User Permission 的实际权限位。所有业务角色均没有这些 DocType 的原生读、写、Report、Export、Import 或 Print 权限。对照账号具备 Batch 和 Stock Entry 的读写、报告、导出、导入及打印权限，具备 Bundle 的读写、提交及导出权限，具备 Stock Reconciliation 的创建、提交及报告权限。对照账号没有 Data Import 和 User Permission DocType 权限。HTTP 矩阵进一步验证了行级结果和报表入口。

## 3. 原生入口矩阵

最终复验于 15:29:27（Asia/Shanghai）开始。共 225 个权限探针，其中 30 个针对自定义 API、160 个针对四名业务用户的原生入口、35 个针对原生角色对照。四名业务用户的 160 个原生请求全部返回 403。对照的 22 个读请求成功，其中 15 个响应包含内部 ID。拒绝用例有强制断言，预期命中 Handler 的原生写入还核验了 Hook 标记。Excel 证据检查压缩包内 XML。详细证据记录每个入口的请求方法、路径、状态和 Hook。

| 入口 | 四类业务账号 | Native Stock User 对照 |
| --- | --- | --- |
| Batch、Bundle 的 REST 列表与详情，REST v2 Batch 详情 | 403 | 200，Batch 列表、详情与 Bundle 详情含内部 ID。默认 Bundle 列表只返回 Bundle 名称 |
| Stock Entry 详情、SLE 和 Bin REST 列表 | 403 | 200。Stock Entry 详情含内部 ID，默认 SLE 和 Bin 列表仅返回记录名称 |
| Batch 创建、编辑、禁用、改名、拆批 | 403 | 403，命中 Batch 文档事件 |
| Batch 删除 | 403 | 已提交历史批次的原生角色删除对照未执行 |
| Bundle 创建、修改 | 403 | 403，受管 Bundle 文档权限拦截 |
| Stock Entry 创建、修改、Desk 提交 | 403 | 403，命中受管库存文档事件 |
| Stock Entry 取消 | 403 | 历史已提交单据的原生角色取消对照未执行 |
| Stock Reconciliation 创建、通用提交 | 403 | 403，命中新加的 Stock Reconciliation 文档事件 |
| 通用 `frappe.client` 的读取、列表、计数、取字段，Desk 列表和详情、原生批次数量 | 403 | 200，列表和详情可见内部 ID，计数和数量接口返回原生数据 |
| Stock Ledger、Stock Balance、Batch-Wise Balance History | 403 | 200，逐批报表含内部 ID。默认 Stock Ledger 和 Stock Balance 返回库存数据，响应未直接包含内部批次 ID |
| 逐批报表 CSV、Excel | 403 | 200，两种导出均含内部 ID |
| Data Import 列表、创建与含记录的模板下载 | 403 | 列表 403，含记录模板 200 且含内部 ID。创建对照未执行 |
| 通过 REST 为自己添加未授权 Warehouse | 403，授权快照一致 | 未执行 |
| Batch 打印预览及 PDF | 403 | HTML 打印预览 200 且含内部 ID，PDF 渲染未执行 |

已测原生写入均被拒绝，原生角色的读取限制则未达到业务接口的隔离效果。即使对照账号有与业务账号相同的自定义 L1 授权，它仍可在获准的原生入口读取内部 ID。正式推广需禁止向普通业务身份叠加原生库存、物料或相关读权限，并为报表、详情和导出建立受控入口及角色配置回归。

## 4. App 变更、Hook 覆盖与事务证据

- 自定义读角色与写角色独立检查，再检查用户对请求叶仓的授权。查询用授权校验后的仓位执行服务端查询。写入先验权，再解析批次、获取锁和处理幂等。Stage 1B 回归脚本增加了同一套授权夹具。 [库存服务](../dev/erpnext-lab/haxpower_platform/haxpower_platform/stock.py) · [Stage 1B 回归](../dev/erpnext-lab/stage1b_verify.py)
- 独立 App 为 Stock Reconciliation 增加原生文档事件，受管库存文档加上提交后更新检查，并检查更新前的物料。保留 Batch 的创建、编辑、改名及删除保护、Bundle 文档权限和 SLE 插入保护。Frappe 与 ERPNext 核心源码均保持干净。 [独立 App Hooks](../dev/erpnext-lab/haxpower_platform/haxpower_platform/hooks.py) · [Frappe Hooks](https://docs.frappe.io/framework/user/en/python-api/hooks)
- Operator 的实际 HTTP 请求重放了原出库。再以同一个 Operator 身份在独立事务内成功执行新的 NO_BATCH 入 1、出 1，观察到两条新 SLE，随即回滚。新连接确认原八笔持久化交易和当前余额一致。此对照验证新的服务调用能够完成入账并回滚，HTTP 新单持久化证据见 Stage 1B。
- 每个原生写探针及自定义写拒绝后，从另一数据库事务比较 14 张相关表的行数和全行摘要。测试完成后用新连接重新核对，库存记录和权限夹具对账一致。独立运行 Stage 1B 回归，八笔请求全部重放，复验文件的 SHA-256 与历史回归证据相同。 [自动化验证](../dev/erpnext-lab/stage1c_verify.py)

## 5. 待验证与安全成本

本轮未覆盖附件与文件直链、全局搜索、Prepared Report 文件和后台导出、实际导入文件与后台导入任务、打印格式自定义、批量改名、直接修改 Batch 数量的维护方法、交付单和发票等其他业务单据、管理员维护与后台重过账。通用文档事件无法覆盖所有应用内部直接保存的方法，SLE 保护也不是完整原生写入口的证明。待补测跨业务仓库、父子仓继承、角色与授权实时变更、停用用户、会话认证和公共报表。现有测试只使用本地 Frappe HTTP 令牌及两个存有数据的叶节点。

采用 ERPNext 可复用已验证的批次收发、Bundle 和账本。持续成本包括维护最小授权角色、限制 Dashboard/Report/Export 的原生权限、自建受控读入口、验证所有新库存单据的权限及每次升级重跑关键矩阵。Frappe-only 可统一业务 API 与授权策略，减少原生库存入口的治理范围，同时需要自己实现并验证库存流水、余额并发、在途、盘点与冲销。 [项目决策](DECISIONS.md) · [适配验收方案](ERPNext-FIT-GAP.md)

## 6. 复验与证据

将独立 App 源码同步至已安装的实验 Bench App 目录，清理该站点缓存。沿用 Stage 1B 报告的回环 HTTP 启动命令。两个独立脚本均从 `/workspace/development/frappe-bench/sites` 运行，使用同一 Python 环境。不要重建或重跑站点初始配置。

```bash
stage1c_container=haxpower-erpnext-v16-lab-frappe-1
stage1c_bench=/workspace/development/frappe-bench
stage1c_tools=$(docker exec "$stage1c_container" mktemp -d /tmp/haxpower-stage1c.XXXXXX)
tar -C dev/erpnext-lab -cf - stage1b_verify.py stage1c_verify.py |
  docker exec -i "$stage1c_container" tar -xf - -C "$stage1c_tools"
umask 077
stage1c_evidence=$(mktemp /tmp/haxpower-stage1c-evidence.XXXXXX)
docker exec -w "$stage1c_bench/sites" "$stage1c_container" \
  "$stage1c_bench/env/bin/python" "$stage1c_tools/stage1c_verify.py" > "$stage1c_evidence"
sha256sum "$stage1c_evidence"
```

本轮证据保存于实验目录 `stage1c-20261118/`。验证工具既有单元测试在普通和优化模式各 17/17 通过，Python 语法、报告链接和命令语法检查通过。业务测试仅访问指定站点。测试结束后已独立确认六名测试账号的临时凭据撤销，并停止回环 HTTP 验证进程。

| 证据 | SHA-256 |
| --- | --- |
| `permission-matrix.json` | `f1c664738c10d1cf71bdf8212b06358afe2493313ed3c96d42e5437b2131c8a5` |
| `stage1b-replay.json` | `3a1456289da66b3e31bc3b039ce9d864e768a62b15f017e9cb79733e89e91327` |

## 7. 结论

- **A. 可以。** 在专用业务角色、服务端仓库授权和受控自定义 API 的条件下，已测权限边界可在不改核心的情况下满足。
- **B. 当前没有迫使停止 ERPNext 路线的核心权限问题。** 这个判断依赖业务账号没有原生库存读取权限。如果正式业务要求同一批用户直接使用原生报表、详情、导入模板或导出，当前方案无法证明内部批次隔离，需要重新比较 Frappe-only。
- **C. 值得继续技术验证。** 建议下一轮用专用业务角色验证两阶段移库、部分接收及盘点，每个新入口都纳入同一套权限和事务断言。本轮结论仍需审查，技术路线尚未决定。 [验证计划](ERPNext-VALIDATION-PLAN.md)
