# Stage 0 — Frappe/ERPNext 安装与测试站点进展

日期：2026-11-17（Asia/Shanghai）。任务类型：实验环境安装、基础设施验证工具改进。

**当前状态：Bench 与 ERPNext 应用安装通过；测试站点等待用户安全交互建站，尚未完成站点验收。** 本报告明确区分“应用已进入 Bench”和“应用已安装到站点”，不把前者写成后者。用户完成交互后，须补充站点、网页、API 与 worker 的实测结果。

## 1. Git 基线与执行边界

- PR #2 已合并到 main，时间为2026-11-17 16:06:38 +08:00。
- 合并提交：`4f92a57b336a7f7b1e8d00165113d2f9b543b5a8`。
- 确认原工作区干净后 fetch `origin/main`，创建 `chore/erpnext-site-setup`；不在 main 修改文件。
- 实验目录保持 `/home/h-goal/labs/haxpower-erpnext-v16/`，Compose Project保持 `haxpower-erpnext-v16-lab`，沿用锁定的镜像与数据卷。
- 本次未修改 Frappe/ERPNext 核心源码、镜像版本、系统网络或认证配置；未连接阿里云、删除数据卷、创建 HaxpowerPlatform 模块或执行库存业务测试。

## 2. 实际安装版本与 Commit

| 组件 | 实测版本/Commit | 状态 |
| --- | --- | --- |
| Python（镜像和 Bench venv） | 3.14.10 | 通过 |
| Node / Yarn / npm | 24.22.3 / 1.22.22 / 11.19.1 | 通过 |
| Bench CLI | 5.31.1 | 通过 |
| Frappe | 16.55.0 / `90c8b61afd60ee49f982ca1a9751008e4ae1e24c` | 已进入虚拟环境，资源已构建，核心仓库干净 |
| ERPNext | 16.55.0 / `8e10a348daaa93853de8c36dc55ec789a440c219` | get-app、Python安装、资源构建完成，站点安装待验证 |
| MariaDB | 11.8.9-MariaDB-ubu2404 | 基础连接通过 |
| Redis cache / queue | 7.4.12 / 7.4.12 | 连接通过 |
| pip（镜像） | 26.2.2 | 保持原镜像版本 |
| pip（Bench venv） | 26.3 | 由官方 Bench 使用uv创建虚拟环境时自动seed，未升级镜像内pip |

安装前从开发容器重新查询两个官方tag，均与 [versions.lock.json](../dev/erpnext-lab/versions.lock.json) 一致。Frappe约束Python `>=3.14,<3.15`；ERPNext要求Python `>=3.14`和Frappe `>=16.55.0,<17.0.0`。安装后再次核对实际Commit、导入版本和依赖文件SHA-256，并检查两个仓库 `git status --porcelain` 为空。

官方流程与参数依据：[Frappe Bench命令](https://docs.frappe.io/framework/user/en/bench/frappe-commands)、[锁定版本的new-site参数](https://github.com/frappe/frappe/blob/90c8b61afd60ee49f982ca1a9751008e4ae1e24c/frappe/commands/site.py)。不使用develop分支或未审查版本绕过问题。

## 3. Bench 初始化和 ERPNext 应用安装

容器用户为frappe（UID1000），在 `/workspace/development` 执行官方命令：

```bash
bench init --frappe-path https://github.com/frappe/frappe.git --frappe-branch v16.55.0 --python /home/frappe/.pyenv/versions/3.14.10/bin/python --skip-redis-config-generation --no-backups frappe-bench
```

退出码0，`SUCCESS: Bench frappe-bench initialized`。创建venv、安装Frappe、安装Node依赖并构建资源成功；没有创建站点。`--no-backups`避免本轮初始化配置容器内定时备份，`--skip-redis-config-generation`避免启动重复Redis进程。

通过 `bench set-config -g` 设置：

| 设置 | 值 |
| --- | --- |
| db_host | mariadb |
| redis_cache | redis://redis-cache:6379 |
| redis_queue | redis://redis-queue:6379 |
| redis_socketio | redis://redis-queue:6379 |
| chromium_path | /usr/lib/chromium/chromium-headless-shell |

随后在Bench目录执行：

```bash
bench get-app --branch v16.55.0 https://github.com/frappe/erpnext.git
```

退出码0。ERPNext Python包、主资源及banking资源构建完成。未使用overwrite、删除目录或删卷重试。Bench、apps、sites、env目录归属及可写性检查通过，生成的Procfile仅包含web、socketio、watch、schedule、worker，使用外部Redis，不包含额外Redis服务。

### 3.1 本地脱敏安装证据

证据位于实验目录的 `install-logs/`，目录0700、日志0600，不提交完整日志或上游源码到项目仓库。日志写入前遮盖已知实验root密码、常见GitHub Token和带用户名/密码的URL；安装命令本身没有密码参数。

| 文件 | SHA-256 |
| --- | --- |
| 20261117T161036-bench.log | `42de1ef726f70a7fde38b88119a9ead3b4afbb871e513762790e339bd49df5a9` |
| 20261117T161608-erpnext.log | `93d93f27ca02e6cc678f9d04c193b895570b0adb068e9e2563529ebe489bb01d` |
| python-freeze-postinstall.txt | `c41a3faf90a1dfefa5aced294ef08ba928433841d42622210bfdaaa66badaf24` |

另存 `service-configuration.log`。Python依赖快照为165行；它记录实际解析结果，不宣称完整应用安装仅凭镜像Digest即可永久复现。两个应用中的Node锁文件保持上游原样。

## 4. 测试站点和密码交互停止点

目标为 **`haxpower.localhost`**，只允许在当前实验MariaDB中创建。

按用户要求：“如果需要用户交互输入密码，请明确提示并停止该操作，等待用户在安全的交互式终端完成。” 本助手已向用户提供安全终端步骤，未自动调用new-site、传入管理员密码、代答密码提示或把密码放在聊天/日志中。

用户需在自己的Ubuntu WSL终端安全查看既有实验root密码文件，随后运行一次：

```bash
docker compose --project-directory "$HOME/labs/haxpower-erpnext-v16" -f "$HOME/labs/haxpower-erpnext-v16/compose.yaml" exec --workdir /workspace/development/frappe-bench frappe bench new-site --db-type mariadb --db-host mariadb --mariadb-user-host-login-scope=% --install-app erpnext --set-default haxpower.localhost
```

隐藏提示中输入现有数据库root密码，为Administrator设置并保存强密码；不要将任何密码回传聊天，不开启录屏/终端录制、不使用tee或密码命令参数。已有站点或失败时停止，保留现场，不force、删库、删卷或重复建站。

**待补验证**：new-site结果、站点安装的应用列表、站点数据库账号连接、Bench启动、网页/API、公共资源和后台worker。当前不把这些项目标为通过，也不根据目录是否存在就判定建站成功。

## 5. Docker、数据库和Redis复核

四个基础容器均running/healthy，实际镜像ID、Project标签、网络成员和专用卷归属与锁定配置一致。无主机端口发布，无SSH密钥或Docker socket挂载；只有frappe接入egress，MariaDB及Redis仅在内部backend网络。

| 检查 | 结果 |
| --- | --- |
| MariaDB从frappe发起的认证TCP连接 | 通过，11.8.9、utf8mb4/utf8mb4_unicode_ci、snapshot isolation=0 |
| 数据库TLS | TLS_AES_256_GCM_SHA384 |
| 错误/空数据库root密码 | 均拒绝，1045 |
| Redis cache / queue | PONG / PONG |
| GitHub HTTPS与Git、PyPI/pip、npm/Yarn | 均通过 |
| 站点专用数据库用户连接 | 等待站点创建后验证，不能以root连接代替 |

启动前Docker可见12 CPU、约9.65GiB内存；WSL available约7.6GiB；C盘约104GiB可用。应用安装后C盘约99GiB可用，WSL项目文件系统约950GiB可用。空闲安装后样本中frappe约1.181GiB/4GiB、MariaDB约93MiB/2GiB，Redis各约4–6MiB；不是站点运行或业务负载测试结果。

## 6. 验证工具改进与结果

采用统一入口加独立应用检查模块，避免重复维护基础安全规则：

- `verify.py --phase base`：基础配置、资源、连接和网络；允许Bench存在并在结果记录其存在状态。
- `--phase bench`：增加venv、目录权限、两个应用版本/Commit/依赖哈希、核心源码干净状态及服务设置检查。
- `--phase site`：增加站点应用列表、专用数据库和Redis连接、Administrator存在、匿名登录页/API ping、CSS/JS公共资源与在线worker检查。
- `site_checks.py`保存独立的容器内只读检查，不输出site_config.json、数据库密码或加密密钥。
- 关键安全校验及内嵌Python检查已移除assert，改为明确条件与错误处理；失败返回非零。`--config-only`结果明确标记，不能充当站点验收。
- 输出路径防符号链接/目录穿越，使用独占创建拒绝覆盖现有证据；保留历史 `bootstrap-checks.json`。
- 错误处理不输出已知站点数据库密码/加密密钥；不通过降低验证阶段将失败站点标为通过。

安全回归测试7项：公开数据库端口、未锁定镜像、非内部网络必须拒绝；历史文件不能覆盖；输出不能目录穿越或经过符号链接；基础检查允许已存在Bench。普通和 `python -O` 模式各7项通过。

### 6.1 已完成的运行检查

| 本地证据 | 检查数/结果 | SHA-256 |
| --- | --- | --- |
| bootstrap-checks.json（历史） | 15项通过，内容保留 | `5bf819e5a07a7ffcbde398214373b750ce5f79abb327d2766686d546396e45e8` |
| site-setup-preflight.json | 15项通过 | `171dd2b9a7f4af8253939f02c79daa6b9744500b84f58445e5c4c1aee9275baa` |
| bench-installed-checks.json | 16项通过，2026-11-17 16:18:48 +08:00开始 | `58d8e308b2f2fd78a8c5601d27ab7374d0e6581dcb7b086ccb867834a06825db` |
| base-after-bench-optimized.json | `python -O`下15项通过，Bench已存在仍正常通过 | `657fde4a44ef7ef36122fa85c36059b682888d415d04871bc178b26ffee3c9c8` |

站点阶段检查尚待用户完成交互后执行，不能用本表替代站点通过证据。

## 7. 本地地址与VSCode Dev Container

站点和Bench启动成功后，浏览器预期地址为 `http://haxpower.localhost:8000`，默认站点亦可经 `http://localhost:8000` 访问；API冒烟地址为 `/api/method/ping`。当前尚未实测站点HTTP，不宣称浏览器已经可用。

Dev Container配置可解析，remoteUser与挂载目录UID1000一致，无自动安装/初始化命令或不必要秘密挂载。无法自动操作Windows VSCode图形界面，用户步骤：

1. 在WSL中运行 `code "$HOME/labs/haxpower-erpnext-v16"`。
2. 确认Windows VSCode已安装WSL及Dev Containers扩展，选择 `Dev Containers: Reopen in Container`。
3. 在容器终端进入 `/workspace/development/frappe-bench`，确认站点创建完成，运行 `bench --site haxpower.localhost list-apps`，再启动 `bench start`。
4. 在“端口”面板确认8000/9000/6787转发至localhost，不转发3306/6379。关闭该Dev Container窗口采用stopCompose，会停止实验服务。
5. 访问登录页，使用用户自己设置的Administrator密码；不要输入真实公司资料或开始库存测试。

未为VSCode修改Docker全局配置、WSL网络模式、DNS或代理。GUI连接及浏览器登录仍待实际确认。

## 8. 实际问题与处理

- uv缓存与工作区位于不同文件系统，hardlink失败后自动copy；安装成功，不修改全局挂载或缓存配置。
- Bench venv由uv自动seed pip26.3；这是官方初始化输出，不是为绕过失败而更换Frappe/ERPNext版本。
- Frappe初始化构建早于服务地址配置，提示redis_cache无法更新assets_json；之后配置指向实验Redis，ERPNext资源构建与Redis检查通过。未改核心源码，站点实际资源加载仍需验收。
- Yarn出现less/stylus/vue-template-compiler peer dependency及workspaces警告，Browserslist提示数据过期，banking Vite提示未来configLoader兼容性；两次构建均退出0，未执行自动升级或改写上游锁文件。
- banking构建提示InterVariable两项字体引用不能解析，当前对应源码字体文件也未找到；作为该上游模块的待验证资源问题记录，不通过修改核心源码修复，不能据主bundle构建成功声称所有模块资源都可用。
- MariaDB沿用已记录的io_uring回退和memory.pressure警告，连接仍通过；负例密码测试产生预期Access denied日志。未通过删卷、清空Docker或修改sysctl处理。
- 新站点密码必须由用户交互输入，已在该操作前停止；这属于用户要求的安全交接，尚不是站点安装失败。

## 9. 下一阶段库存验证的准备条件

先完成安全交互建站，核对站点已安装Frappe/ERPNext均为16.55.0；启动Bench并通过完整 `--phase site`、网页/资源/API与worker检查，确认只有实验数据、日志已脱敏、核心仓库保持干净。

上述条件满足并完成本任务审查后，才能另行授权创建独立 `haxpower_platform` 原型App，按 [ERPNext验证计划](ERPNext-VALIDATION-PLAN.md) 执行空批次、估值、部分接收、权限和库存完整性测试。本轮不自动进入该阶段。
