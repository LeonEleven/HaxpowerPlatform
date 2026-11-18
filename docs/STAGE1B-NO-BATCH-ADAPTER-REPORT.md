# Stage 1B — NO_BATCH 最小适配原型验证

2026-11-18（Asia/Shanghai）完成技术选型实验。**关闭自动分配后，ERPNext 标准单据能够精确收发内部系统 Batch。独立 App 将其映射为 `batch=null`。** 普通操作员经原型接口完成了持久化交易。带 ERPNext 原生库存角色的账号仍能在原生报表、详情和 CSV 中看到内部 Batch ID，读取边界尚未解决。

## 环境与隔离

- PR #5 已合并，`spike/erpnext-no-batch-adapter` 基于 `origin/main` 的 `bc8b9150e15fc3e6e91b9e483c32131ac23b9e57`。
- Frappe 16.55.0（`90c8b61afd60ee49f982ca1a9751008e4ae1e24c`）、ERPNext 16.55.0（`8e10a348daaa93853de8c36dc55ec789a440c219`）、Python 3.14.10、MariaDB 11.8.9。两个核心工作区均干净。沿用同一组实验容器和 Bench。 [容器镜像锁文件](../dev/erpnext-lab/versions.lock.json)
- 初查 `haxpower.localhost` 已完成向导，只有一个初始化公司、五个 Warehouse，Item、Batch、Stock Entry、SLE、Bin、Bundle、GL 和重估记录均为空。历史 Stage 1 证据摘要核对一致。新增站点前，frappe 容器使用 2.637 GiB / 4 GiB，数据库容器使用 228.8 MiB / 2 GiB，WSL 可用内存 4.6 GiB、文件系统剩余 950 GiB。
- 新建 `haxpower-stage1b.localhost` 及独立数据库 `haxpower_stage1b_lab`，仅在新站点安装 `haxpower_platform` 0.1.0。使用虚构公司、两种虚构 SKU 和 PC 整数单位。关闭永续盘存、自动出库分配、普通负库存和批次负库存，单据行允许零估值。测试 HTTP 只在容器内回环地址监听。结束后已关闭该验证进程并撤销两名测试账号的临时 API 凭据，保留实验单据。

## 1. 映射与调用

原型按 `站点 + 固定实验 SKU 标识 + 批次文本或 null` 生成 UUIDv5。内部系统 Batch 使用 `HP-NB-`，实体批次使用独立的 `HP-RB-` 命名空间，真实文本存于实验专用的 Batch 描述字段。每次复用均校验 Batch 归属、描述与启用状态，已有冲突记录时拒绝。实验中 SKU-A 和 SKU-B 各有独立系统 Batch，SKU-A 的两个库位共享其映射。固定的实验 SKU 标识不支持重命名迁移，正式产品需维护不可变标识和独立映射数据。 [库存模型](INVENTORY-MODEL.md)

`POST /api/method/haxpower_platform.stock.post_stock` 要求显式 `BATCH` 加有效 `batchNo`，或显式 `NO_BATCH`。缺失和 JSON 空值均视为未选择。系统 ID 无法作为真实批次标识使用。接口用 `Decimal` 解析 PC 数量。`GET /api/method/haxpower_platform.stock.balances` 只输出真实批次文本或 JSON `null`，数量用十进制字符串。当前仅覆盖两种 SKU、两个库位，单一原型角色可访问这两个库位。前端正式路径与仓库范围模型待验证。 [原型 App](../dev/erpnext-lab/haxpower_platform/haxpower_platform/stock.py) · [业务 API 契约](API-CONTRACTS.md)

标准 Stock Entry 的 Material Receipt / Material Issue 经 Document API `insert()`、`submit()` 生效。ERPNext 把显式旧字段 `batch_no` 转换成正式 Inward / Outward Bundle。出库前检查所选批次余额，提交后检查 SLE 和唯一 Bundle 子行的批次及有符号数量。原型使用一个公司行锁串行化写请求，通过 Stock Entry 的 `remarks` 保存调用方与请求 ID 的摘要和负载摘要。请求成功后由 Frappe HTTP 请求事务提交。 [Document API](https://docs.frappe.io/framework/user/en/api/document) · [Bundle 文档](https://docs.frappe.io/erpnext/serial-and-batch-bundle)

## 2. 原生试验和实际交易

安装原型前，在事务内创建两个测试 Batch，依次原生入 `+12/+15`、按所选批次出 `−4/−5`。四次 `submit()` 均生成精确的单批 Bundle，阶段结束回滚。此项验证标准 ERPNext 的显式批次分配能力。将其中一个 Batch 表达为业务无批次由 App 实现。

安装 App 后，通过普通操作员令牌登录实际 Frappe HTTP 服务，共持久化六笔 Material Receipt 和两笔 Material Issue。SKU-A 的 W1/L1 先入 STD61 12、STD62 20、无批次 9+6，合计 47。W1/L2 无批次另入 12，SKU-B 的 W1/L1 无批次另入 1。SKU-A 的两个出库依次只扣 STD61 5 和无批次 4。

| SKU / 库位 | STD61 | STD62 | `batch=null` | Bin 合计 |
| --- | ---: | ---: | ---: | ---: |
| SKU-A W1/L1 | 7 | 20 | 11 | 38 |
| SKU-A W1/L2 | 0 | 0 | 12 | 12 |
| SKU-B W1/L1 | 0 | 0 | 1 | 1 |

| 要求 | 判定与证据 |
| --- | --- |
| A1–A6 唯一、复用、SKU 独立、领域空值 | **实际通过（App）**。原生 4 个 Batch，包括 2 个系统 Batch。SKU-A 的三笔无批次入库和一笔无批次出库引用同一 ID。SKU-B 引用另一 ID。对外 `batch=null`，查询响应无内部命名空间 |
| B1–B5 并存、重复入库及精确扣减 | **实际通过（原生分配＋App）**。无批次第二笔入库只将该桶 9→15，出库后 15→11。STD61 12→7，STD62、L2 和 SKU-B 未被扣减 |
| B6 无批次不足 | **实际通过（App）**。W1/L1 无批次剩 11 时请求 12，返回 `INSUFFICIENT_STOCK`，未使用同库位其他 27 PC，也未使用 L2 的 12 PC |
| B7–B8 缺失、无效及保留标识 | **实际通过（App）**。缺选择返回 `BATCH_SELECTION_REQUIRED`。缺标识、未知标识或系统 ID 冒充实体批次返回 `BATCH_NOT_FOUND` |
| B9–B10 自动分配关闭、对账 | **实际通过（原生分配＋App）**。批次专用 API 和标准 Bundles 在关闭自动分配时生效，见下一节 |
| C 最小适配服务 | **实际通过（App）**。普通操作员经原型 HTTP 接口完成批次解析、收发和余额读取 |
| F 零估值配置足以收发 | **实际通过（原生库存）**。8 条 SLE 保留估值字段，库存估值及变动额为 0，没有 GL 或重估条目 |

## 3. 提交后的对账、重试与恢复

**E 的持久化、对账和重复请求实际通过。** 新数据库连接读到 `MAT-STE-2026-00001` 至 `MAT-STE-2026-00008`，8 笔单据均为 `docstatus=1`。与 8 条有效 SLE、8 个已提交的 Bundle 和 8 条 Bundle 子行一一对应。以单据行、SKU、位置和批次归集的有符号数量与标准 Batch-Wise Balance History 和表中 Bin 一致。SKU-A 两个库位合计 50 PC。MariaDB 实际持久化列 `SLE.actual_qty`、`Serial and Batch Entry.qty`、`Bin.actual_qty` 均为 `decimal(21,9)`。

相同请求 ID 和负载重试返回原单号，复跑完整序列时八个请求全部重放。相同请求 ID 修改数量返回 `IDEMPOTENCY_CONFLICT`。请求被拒绝、重试和权限检查后从新连接读取，单据、明细对应的 SLE、Bundle、Batch、Bin、GL 及重估记录与检查前一致。高并发扣减、并发冲销和正式幂等存储仍 **待验证**。

八笔正式实验单据保留在专用站点。若需恢复到交易前余额，用新的请求 ID 通过标准 Material Receipt / Issue 为各原单做反向调整，指定相同 SKU、库位和批次，并在外部实验记录中关联原单及调整单。应先补回两笔出库，再冲减六笔入库。未执行恢复，保留原流水供审查。原型阻止直接取消受管 SKU 的原生 Stock Entry，正式冲销审计链 **待验证**。

## 4. 普通用户的原生入口边界

两名账号均为非 Administrator。操作员仅有原型角色。原生对照用户另外持有 Stock User、Stock Manager、Item Manager。所有结果通过实际 HTTP 请求取得。原型遇到 Bundle 原生权限校验时采用 Frappe `extend_doctype_class`，仅在完成业务角色、SKU 和库位校验的服务上下文内授予对应 Bundle 的 create/write/submit 能力。测试账号的原生角色不会开启该上下文。 [Frappe 文档权限 Hooks](https://docs.frappe.io/framework/user/en/python-api/hooks) · [Frappe REST](https://docs.frappe.io/framework/user/en/api/rest)

| 原生入口 | 仅原型角色 | 加原生角色 | 判定 |
| --- | --- | --- | --- |
| REST 创建保留 Batch、编辑已有保留 Batch | 403 | 403，命中 Batch Hook | **实际通过** |
| Desk 标准库存单据 Save/Submit，省略批次 | 403 | 403，命中 Stock Entry Hook | **实际通过** |
| REST 创建 Stock Entry（显式给内部批次或省略批次）及标准 submit 方法 | 403 | 403，命中 Stock Entry Hook | **实际通过** |
| REST 创建受管 SKU 的 Bundle | 403 | 403，由 Bundle 文档权限拦截 | **实际通过** |
| Batch REST 列表、Batch REST / Desk 详情、Bundle REST 详情 | 403 | 200，均含内部 ID | **失败（原生角色读取）** |
| Batch-Wise Balance History、原生 CSV 导出 | 403 | 200，均含内部 ID | **失败（原生角色读取）** |

原型角色对已测写入口和读入口实行最小授权。若正式需求要求同一操作员同时使用原生报表、详情或导出，仍需收紧角色或提供适配后的入口并重新测试。前端隐藏字段不能解决报表和 REST 的暴露。Stock Reconciliation、单据导入、其他库存单据、批量变更、重命名和未测读取入口 **待验证**。本轮没有证据表明需要修改核心才能准确扣减或阻断已测试的写入口，也没有证明原生角色的读取问题已能完整解决。

## 5. 复现及证据

从仓库根目录运行。以下命令沿用现有实验 Compose，仅对专用站点操作。新装时通过 Bench 交互提示输入现有实验数据库管理员凭据并设置专用站点 Administrator 密码。

```bash
stage1b_container=haxpower-erpnext-v16-lab-frappe-1
stage1b_bench=/workspace/development/frappe-bench
stage1b_site=haxpower-stage1b.localhost

# 仅首次执行。已经有持久化实验结果的站点从最终验证步骤复跑。
docker exec -it -w "$stage1b_bench" "$stage1b_container" \
  bench new-site "$stage1b_site" --db-name haxpower_stage1b_lab --db-type mariadb \
  --db-host mariadb --mariadb-user-host-login-scope=% --install-app erpnext
for stage1b_mode in configure native; do
  docker exec -i -w "$stage1b_bench/sites" "$stage1b_container" \
    "$stage1b_bench/env/bin/python" - "$stage1b_mode" < dev/erpnext-lab/stage1b_prepare.py
done
tar -C dev/erpnext-lab -cf - haxpower_platform | \
  docker exec -i -w "$stage1b_bench" "$stage1b_container" tar -xf - -C apps
docker exec -w "$stage1b_bench" "$stage1b_container" \
  uv pip install --offline --python env/bin/python -e apps/haxpower_platform
docker exec -w "$stage1b_bench" "$stage1b_container" env/bin/python -c \
  'from pathlib import Path; p = Path("sites/apps.txt"); p.write_text("\n".join(dict.fromkeys(p.read_text().splitlines() + ["haxpower_platform"])) + "\n")'
docker exec -w "$stage1b_bench" "$stage1b_container" \
  bench --site "$stage1b_site" install-app haxpower_platform
```

在另一个终端启动并保持专用回环验证服务。实验结果保留在数据库，验证脚本可以重跑。

```bash
docker exec -w /workspace/development/frappe-bench/sites haxpower-erpnext-v16-lab-frappe-1 \
  /workspace/development/frappe-bench/env/bin/python -c \
  'from frappe.app import application; from werkzeug.serving import run_simple; run_simple("127.0.0.1", 8011, application, use_reloader=False)'
```

```bash
umask 077
stage1b_evidence=$(mktemp /tmp/haxpower-stage1b.XXXXXX)
docker exec -i -w /workspace/development/frappe-bench/sites haxpower-erpnext-v16-lab-frappe-1 \
  /workspace/development/frappe-bench/env/bin/python - < dev/erpnext-lab/stage1b_verify.py > "$stage1b_evidence"
sha256sum "$stage1b_evidence"
```

详细证据在实验目录下的 `stage1b-20261118/`，包含虚构交易和接口结果。测试账户凭据未写入证据或仓库。

| 证据 | SHA-256 |
| --- | --- |
| `first-commit.json`，首次真正提交与初始验证 | `d7ce6e2325139b9be475f6f92cc47873d6f82fc9c6bff20bcf67c8d2f0bdb595` |
| `fresh-connection-replay.json`，新连接对账、八笔重放、最终权限矩阵和数量列 | `3a1456289da66b3e31bc3b039ce9d864e768a62b15f017e9cb79733e89e91327` |
| `original-site-recheck.json`，原站点完整复验 19/19 通过 | `6cdafc1f854f613c8e277bb1a086bbad3e2d319a66f4745c8ad7af36bab0f1b6` |

原生 App 初次注册遇到 Bench 应用列表末尾缺换行，修正后单站点安装成功。ERPNext 在批次兼容查询中输出 v16 弃用警告，已验证调用路径的原生行为仍正常。现有验证工具普通和优化模式分别 17/17 通过。

## 6. 技术路线评审

ERPNext 路线已复用标准单据、批次分配、总量与逐批账本，剩余工作集中在入口权限、公共报表和导出、正式批次映射与幂等、两阶段移库和部分接收、盘点与冲销，以及升级时对原生 Bundle 和权限路径的回归。默认估值与重估依赖仍需维护。

Frappe-only 路线能在领域存储中直接表达空批次，统一库存读写入口。对应工作量更大，需要独立实现和验证精确数量存储、扣减锁、事务流水、余额、在途、盘点、冲销、期初导入和报表。已有的通用认证和文档能力可继续复用。

建议先评审是否接受专用角色与受控报表作为 ERPNext 路线的入口约束。若可接受，下一轮优先在该约束下验证盘点、移库部分接收和并发，逐项覆盖原生入口。若正式业务必须向同一角色开放原生报表且无法避免内部映射暴露，或库存一致性要求必须改核心，应优先评估 Frappe-only。最终路线仍待审查。 [项目决策](DECISIONS.md) · [适配验收条件](ERPNext-FIT-GAP.md) · [后续验证计划](ERPNext-VALIDATION-PLAN.md)
