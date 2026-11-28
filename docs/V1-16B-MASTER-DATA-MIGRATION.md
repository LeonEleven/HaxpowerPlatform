# V1-16B 主数据迁移契约

App 0.18.0 通过 `haxpower_v1.migration.service` 提供同步后端服务。开发、测试和演示使用合成资料。迁移通过目录服务生成分类、属性配置、规格、品牌与 SKU，新 SKU 可建立对应的无库存原生 Item。V1-16C 负责仓库库位关联、历史批次清洗、盘点、对账和冻结。V1-17 负责专项签核、正式入账和逐桶核对。

## 工作簿

每项任务指定来源格式和稳定的数据集标识。数据集标识使用 1–100 个 ASCII 字母、数字、下划线、点或短横线，以字母或数字开头。后续导出继续使用同一标识。不同来源保留独立身份。系统按工作表名称识别内容，表头按字段名匹配，行顺序可变。源行由文件 ID、表名与 Excel 行号定位证据；业务关系使用来源键。

### 旧系统 `legacy-material-1`

可提交包含四张表的一份文件，或上传最多四份文件。不允许重名工作表。表头均必需，单元格可按原样为空，适配器对关键字段报错或提出审核。具体完整表头由 [formats.py](../apps/haxpower_v1/haxpower_v1/migration/formats.py) 中 `LEGACY_FIELDS` 固定。

| 工作表 | 映射与留证 |
|---|---|
| `t_material_spec_code_config` | `company_id`、`id` 识别行。只接受旧编码中的单一分隔段与同公司的已批准分类编码及配置名称相符时作为分类候选。名称冲突、编码未知或多义时走审核。父级、自引用及非产品大类配置保留原文。 |
| `t_material_spec` | `id` 为规格来源身份，`company_id` 与数据集组成命名空间。旧规格代码归入历史标识，规格名称、库存单位、备注形成候选。 |
| `t_material_spec_attribute` | 按同公司 `material_spec_id` 关联规格。以核准的 23 个属性名称映射类型，保存 `prop_name`、`prop_value` 和 `prop_type` 原文。选项按技术属性加精确原值提取，来源于规格实际引用。多选逐值保存。单选多值、重复引用、缺失技术属性及分类模板冲突进入审核。Decimal 读取数值 XML 或文本，按目录约束精确校验。布尔接受 Excel 布尔、`TRUE`、`FALSE`、`是`、`否`。 |
| `t_material` | `id` 为 SKU 来源身份，按同公司 `material_spec_id` 关联。旧 SKU 代码进入 `LEGACY`。代码前缀冲突须审核有效的 ID 关系。原品牌文本形成候选，空值须明确选择，例如 `code:UNBRANDED` 或 `code:UNKNOWN-BRAND`。备注作为候选描述，清理差异须明确审核。 |

旧源 ID 允许文本或最多 15 位无符号整数内容的原始数字单元格。旧编码、公司声明中的分类配置编码、件号、IMO 和其他选项标识必须提供文本。表中版本、人员、日期、历史状态以及 SKU 价格、折扣、预警字段留作受权原始证据。空状态按候选的当前目录默认值处理。旧备注、特殊字符及空白在证据中完整保留。HTML 形态、超长或首尾裁剪产生审核项，须提供明确的目标文本、豁免相关行或提供修正的源数据。相似机型、参数名称、企业别名和复合技术参数保持各自身份。可用审核决定将参数或品牌指向经核实的既有目录记录。

### Haxpower 标准 `haxpower-master-1`

`Instructions` 必须含 `version`、`usage` 两列和一行 `haxpower-master-1`，`usage` 可留空。其余工作表可按需要提供。七张实体表均包含 `key`、`action`、`target_id` 和 `target_code` 四列。实体关联键用纯文本，遵循数据集标识的字符范围，唯一性按表约束。可用表内键或 `key:键` 建立关联，也可用 `id:领域ID` 或适用的 `code:既有正式编码` 直接引用。例如 `code:0011` 指向已存在的分类，参数选项和分类模板使用领域 ID。带 `key:` 的外键以目标实体表中的键为准。

| 表 | 必填创建字段 | 其余列 |
|---|---|---|
| `Categories` | `code display_name` | `parent_category description status sort_order` |
| `Attributes` | `code display_name data_type` | `unit description status` |
| `Options` | `attribute option_key display_name` | `status sort_order` |
| `Templates` | `category attribute` | `is_required value_mode searchable identity_relevant display_order editable_for_technician` |
| `Specifications` | `category display_name stock_uom` | `description status` |
| `Brands` | `code display_name` | `description status` |
| `SKUs` | `specification brand display_name` | `stock_uom quantity_precision description` |
| `Specification Values` | `key specification attribute` | `value` 或 `option` 二选一。逐行引用规格及属性选项。 |
| `Historical Identifiers` | `key target_type target source value` | `target_type` 为 `Specifications` 或 `SKUs`。目标用本表版本的实体键。 |

- `create` 的目标列留空。新产品正式编码由目录服务分配。相同已映射身份的 `create` 行可重放。变更字段使用 `update`。
- `reference` 只填目标 ID 或既有正式编码。两者同时填写时必须一致。后续版本可沿用已有来源映射。
- `update` 使用已映射来源键或核实的目标。修改字段才填写。属性值按属性更新提供的值组，缺列、空白和本次文件缺失的实体保留既有结果。历史标识追加。
- 文本和编码用文本单元格。顺序与精度为非负整数。布尔值为 Excel 布尔或大写 `TRUE`／`FALSE`。日期使用 `YYYY-MM-DD` 文本或 OOXML 明确日期类型。Decimal 可用精确十进制文本或 Excel 已序列化的数值。类型为 `TEXT DECIMAL BOOLEAN DATE SINGLE_SELECT MULTI_SELECT`，模板模式为 `SINGLE MULTI`，状态为 `Active Inactive`。单位及精度使用已配置的目录规则。
- 错误示例包括将 `00123` 作为数字存储后尝试恢复文本件号，把两个不同选项写入同一单选属性，使用 `target_code` 指定新 SKU 编码，或填写公式作为技术值。用修订后的文件或具体审核决定替换错误数据。

## 文件与证据

只接受 .xlsx OOXML 数据包。限制为每批最多 4 个文件、每个文件压缩后 4 MiB、解压后 24 MiB、最多 24 个成员、每文件 10 张表、每表 5001 行含表头、32 列、每单元格 8192 字符、每批 6500 条数据行、13000 个候选组和 800 万字符。读取器限制成员解压比，校验包成员、关系目标及内容类型，拒绝公式及缓存、宏、嵌入、外链、路径穿越、文本实体声明和超限文件。仅本地解析 XML。数字保持 Excel 序列化的原始十进制文本。Excel 写入之前已丢失的前导零须由人工从来源证据核实。

原文件及报告按受控 ID 写入站点私有 `private/haxpower_v1/migration`。附件元数据包含原名、大小、SHA-256、任务和来源格式。文件读取重新校验当前授权、归属与摘要。上传失败清理已创建且未登记的私有对象。详情分页返回源行、原始类型、候选、目标现状、历史来源、依赖及阻断原因。报告中的文本均以文本单元格写入。站点备份恢复须同时覆盖数据库和该私有目录。

## 权限和状态

| 能力 | 能执行的后端操作 |
|---|---|
| Migration Operate + Product Read | 上传、替换来源版本、预览、读取行证据和下载私有源文件 |
| Migration Review + Product Read | 预览、核对源行、提交审核决定及批准计划摘要 |
| Migration Execute + Product Read | 预览、核对源行、执行并读取结果，按组复查目录权限 |
| Parameter Maintain | 参数组执行时还需此权限，涉及分类、属性、选项、模板、品牌 |
| Product Maintain | 规格和 SKU 建档或编辑执行时还需此权限 |

迁移报告的生成和下载还需要 Export。角色可兼任，迁移角色独立配置且安装时零分配。访问范围为本站目录，由具当前迁移能力的人员审核和执行。没有迁移 HTTP 或通用 DocType 写入口。后端作业应使用经授权的执行人会话，服务每次重新查询当前授予。源行及文件访问同样复查授权。此审核属于主数据准备。

上传成功生成 `Pending Review`。每次换文件或提交处理决定创建新版本，以前的版本和历史证据保留。审核者对组提供原因，可批量提交同类决定。系统为每个组保存其具体行、人员、服务器时间和决定，批准的整个摘要覆盖组及文件清单。批准 `Approved` 版本后可分组执行。任务按结果进入 `Executing`、`Failed`、`Invalidated` 或 `Completed`。完成要求所有计划组均已确认。

常用 API 如下。所有修改方法使用独立事务，可在后端应用或本地 Bench 控制台调用。

| 方法 | 数据与结果 |
|---|---|
| `upload(source, dataset, uploads, task_id=None, expected_version=None)` | 输入 `[(原文件名, bytes)]`。返回任务、版本、摘要、组数和阻断数。旧任务换文件需要当前版本。 |
| `preview(task_id, version=None, offset=0, limit=50)` | 分页读候选、上次来源、当前目标、差异、判断、批准和结果。 |
| `source_rows(task_id, version, offset=0, limit=50)` | 分页读各来源行及原始类型。 |
| `revise(task_id, expected_version, decisions)` | 提交本次版本完整决定集，展开后的决定证据上限为 200 万 ASCII JSON 字符。每项包含 `keys`、`reason`，可选 `fields`，或 `target_id`／`target_code` 加 `action`，或 `drop_rows`／`skip`。修改明确关联须核实后的 `code:` 或 `id:` 目标。纯确认适用于重复选项、已有 ID 的前缀冲突，以及确认保留缺失的可选历史属性。 |
| `approve(task_id, version, digest, reason)` | 仅当前待审版本、无阻断且含可执行组才批准。 |
| `execute(task_id, version, group=None, max_groups=1)` | 指定组或最多 25 个就绪组，每组各自提交，返回实际目标或代码。 |
| `report(task_id, version)`、`download(task_id, file_id)` | 私有报告元数据，或文件名和经过校验的字节。 |

## 幂等、差异和失败恢复

数据库主键为规范化 JSON 身份组合的 SHA-256，按来源系统、稳定数据集／旧公司、实体类型和原始来源 ID 唯一。映射回读验证该组合及来源摘要。证据记录首个和最近确认版本、历史编码、来源业务值、执行时目标及批准者。文件指纹、Excel 行号、名称和历史编码均可重复出现。历史编码用授权目录搜索，迁移身份使用持久化来源键。

对比上次来源、本次来源和当前目标。相同来源跳过正式写入。未变的上游字段保留本地值，双方改动时提出审核。相似名称显示为候选，须通过明确目标身份及审核才能复用。迁移阻断 SKU 规格、品牌、目录编码、精度单位和原生 Item 变更。已映射目标缺失、歧义、原生映射漂移或改绑同样阻断。

目录锁保护建档及映射，执行时复查目标快照、依赖目标、模板与单位配置。每组由一个外层事务保存正式目录写入、映射与执行回执，目录服务在其内使用原有 savepoint。失败回滚该组并记录原因，其他已提交组保留。调用者可重试指定失败组或继续其他就绪组。数据库死锁后开启新事务并重验授权，自动有限次重试。丢失响应后的同版本重复调用返回已保存回执。目标或配置变化使版本失效，提交新预览并重新批准。历史证据继续可查。

## 合成演示

在已有 openpyxl 的开发 Python 环境中运行。以新建的 Git 忽略路径生成，可选择独立异常夹具。

```sh
PYTHONPATH=apps/haxpower_v1 python -m haxpower_v1.migration.demo dev/v1/data/v116b/demo
PYTHONPATH=apps/haxpower_v1 python -m haxpower_v1.migration.demo dev/v1/data/v116b/anomalies --anomalies
```

正常来源有 13 类、23 属性、260 个规格、548 个 SKU、2080 条属性行，另有增量第二版。请选择其中一种格式在独立演示站使用。预先配置原生 Item Group 及 PC／SET 单位，显式运行现有 `catalog.initial_configuration.initialize()`，向合成站点员工授予表中能力后，打开 `bench --site <合成站点> console`。文件路径须对该 Bench 可读。以下调用展示控制台操作，先翻页检查实际差异，再输入审核理由执行。

```python
import frappe
from haxpower_v1.migration import service
frappe.set_user("synthetic-migration@example.invalid")
run = service.upload_paths("haxpower-master-1", "synthetic-v116b", ["/tmp/demo/master.xlsx"])
view = service.preview(run["task"], run["version"], limit=100)
rows = service.source_rows(run["task"], run["version"], limit=20)
print(view["total"], run["blocked"])
# 翻页完成审核后，再为这一版调用 approve。
service.approve(run["task"], run["version"], run["digest"], "已核对合成数据和目标差异")
service.execute(run["task"], run["version"], max_groups=25)
```

重复执行到任务状态为 `Completed`。第二版保持相同格式及数据集标识，初次导入完成后替换文件并重新审阅。通过具 `PRODUCT_READ` 的目录搜索查询新编码、旧标识、品牌或技术属性，可核实 SKU 与原生 Item 稳定关联。旧格式中空品牌或缺失技术值须先用 `revise` 提交明确决定，再批准返回的新版本。
