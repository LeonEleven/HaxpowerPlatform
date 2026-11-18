# Stage 0 — Frappe/ERPNext 安装与测试站点验证报告

日期：2026-11-17（Asia/Shanghai）。任务类型：实验环境安装、基础设施验证工具改进。

**当前状态：`haxpower.localhost` 已创建并安装 Frappe/ERPNext 16.55.0，完整站点验证19项通过。** 用户在安全终端完成密码交互后，本助手核对站点应用、数据库和Redis，启动Bench并验证登录页、API、CSS/JS资源及在线worker。VSCode图形界面连接与浏览器人工登录未自动执行，操作步骤见第7节。

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
| Frappe | 16.55.0 / `90c8b61afd60ee49f982ca1a9751008e4ae1e24c` | 已安装到测试站点，资源可访问，核心仓库干净 |
| ERPNext | 16.55.0 / `8e10a348daaa93853de8c36dc55ec789a440c219` | get-app、资源构建及测试站点安装完成，核心仓库干净 |
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

## 4. 测试站点创建与密码交接

目标为 **`haxpower.localhost`**，只允许在当前实验MariaDB中创建。

按用户要求，建站曾暂停等待安全密码交互。用户已在自己的WSL终端完成创建并回复“已完成”；本助手没有代答密码提示、获取Administrator密码或记录交互终端内容，随后用实际站点连接和应用列表核对结果。

以下为已完成的单次建站命令，仅供新环境重建参考；**当前站点已经存在，不要重复执行**：

```bash
docker compose --project-directory "$HOME/labs/haxpower-erpnext-v16" -f "$HOME/labs/haxpower-erpnext-v16/compose.yaml" exec --workdir /workspace/development/frappe-bench frappe bench new-site --db-type mariadb --db-host mariadb --mariadb-user-host-login-scope=% --install-app erpnext --set-default haxpower.localhost
```

隐藏提示中输入现有数据库root密码，为Administrator设置并保存强密码；不要将任何密码回传聊天，不开启录屏/终端录制、不使用tee或密码命令参数。已有站点或失败时停止，保留现场，不force、删库、删卷或重复建站。

验收结果如下；没有仅凭目录存在就判定成功：

| 验收项 | 实测结果 |
| --- | --- |
| `bench --site haxpower.localhost list-apps` | frappe 16.55.0、erpnext 16.55.0 |
| 站点应用列表 | 仅frappe、erpnext，无haxpower_platform |
| 站点专用数据库用户 | Frappe连接成功，`SELECT 1`通过；未输出数据库密码或用户名 |
| Administrator | 用户记录存在，密码由用户交互设置并保管；未自动进行密码登录测试 |
| Redis cache / queue | 按站点实际配置连接，两者PING通过 |
| `/login`、`/api/method/ping` | HTTP200，API返回pong |
| 登录页CSS、frappe-web JS | 版本化公共资源均HTTP200 |
| worker | `bench doctor`显示Workers online: 1 |
| scheduler | 开发schedule进程已启动，但站点scheduler仍disabled/inactive；未自动启用定时任务 |

Bench于2026-11-17 17:04:36 +08:00在既有frappe容器内启动，web、socketio、watch、schedule、worker均已启动；17:05:06日志显示Watching for changes。使用私有日志包装程序遮盖已知站点秘密和Debugger PIN，不改变官方Procfile或核心代码。日志快照 `install-logs/bench-start-acceptance-20261117T170832.log` 的SHA-256为 `cc45c1252ac788bbe0613fab49878e03f7d7b20e6238edc2b43577402820d777`；该验收快照没有Traceback、端口冲突或非零退出等阻塞记录。

实时脱敏启动日志仍位于Bench的 `logs/lab-start-20261117T090436Z.log`；运行包装程序和PID元数据仅留在实验Bench日志目录，不作为生产服务管理方案。基础Compose仍执行sleep infinity，重启容器后需重新启动Bench。

## 5. Docker、数据库和Redis复核

四个基础容器均running/healthy，实际镜像ID、Project标签、网络成员和专用卷归属与锁定配置一致。无主机端口发布，无SSH密钥或Docker socket挂载；只有frappe接入egress，MariaDB及Redis仅在内部backend网络。

| 检查 | 结果 |
| --- | --- |
| MariaDB从frappe发起的认证TCP连接 | 通过，11.8.9、utf8mb4/utf8mb4_unicode_ci、snapshot isolation=0 |
| 数据库TLS | TLS_AES_256_GCM_SHA384 |
| 错误/空数据库root密码 | 均拒绝，1045 |
| Redis cache / queue | PONG / PONG |
| GitHub HTTPS与Git、PyPI/pip、npm/Yarn | 均通过 |
| 站点专用数据库用户连接 | 通过，直接使用Frappe站点配置；未用root连接代替 |

启动前Docker可见12 CPU、约9.65GiB内存；WSL available约7.6GiB；C盘约104GiB可用。应用安装后C盘约99GiB可用，WSL项目文件系统约950GiB可用。空闲安装后样本中frappe约1.181GiB/4GiB、MariaDB约93MiB/2GiB，Redis各约4–6MiB；不是站点运行或业务负载测试结果。

站点启动验证期间样本：frappe约3.13GiB/4GiB（93个PID）、MariaDB约221.6MiB/2GiB、两个Redis约5.85/4.79MiB；WSL available约6.0GiB。开发watch与构建会增加资源占用，下一阶段关注4GiB容器限制的余量，不据本次样本宣称业务性能或压力测试通过，也未为此修改全局资源配置。

## 6. 验证工具改进与结果

采用统一入口加独立应用检查模块，避免重复维护基础安全规则：

- `verify.py --phase base`：基础配置、资源、连接和网络；允许Bench存在并在结果记录其存在状态。
- `--phase bench`：增加venv、目录权限、两个应用版本/Commit/依赖哈希、核心源码干净状态及服务设置检查。
- `--phase site`：增加站点应用列表、专用数据库和Redis连接、Administrator存在、匿名登录页/API ping、CSS/JS公共资源与在线worker检查。
- `site_checks.py`保存独立的容器内只读检查，不输出site_config.json、数据库密码或加密密钥。
- 关键安全校验及内嵌Python检查已移除assert，改为明确条件与错误处理；失败返回非零。`--config-only`结果明确标记，不能充当站点验收。
- 输出路径防符号链接/目录穿越，使用独占创建拒绝覆盖现有证据；保留历史 `bootstrap-checks.json`。
- 错误处理不输出已知站点数据库密码/加密密钥；不通过降低验证阶段将失败站点标为通过。

安全与兼容性回归测试8项：公开数据库端口、未锁定镜像、非内部网络必须拒绝；历史文件不能覆盖；输出不能目录穿越或经过符号链接；基础检查允许已存在Bench；站点初始化必须先切换到sites目录。普通和 `python -O` 模式各8项通过。

### 6.1 已完成的运行检查

| 本地证据 | 检查数/结果 | SHA-256 |
| --- | --- | --- |
| bootstrap-checks.json（历史） | 15项通过，内容保留 | `5bf819e5a07a7ffcbde398214373b750ce5f79abb327d2766686d546396e45e8` |
| site-setup-preflight.json | 15项通过 | `171dd2b9a7f4af8253939f02c79daa6b9744500b84f58445e5c4c1aee9275baa` |
| bench-installed-checks.json | 16项通过，2026-11-17 16:18:48 +08:00开始 | `58d8e308b2f2fd78a8c5601d27ab7374d0e6581dcb7b086ccb867834a06825db` |
| base-after-bench-optimized.json | `python -O`下15项通过，Bench已存在仍正常通过 | `657fde4a44ef7ef36122fa85c36059b682888d415d04871bc178b26ffee3c9c8` |
| site-installed-checks.json | 完整site阶段19项通过，2026-11-17 17:05:21 +08:00开始 | `8514c1857200cca1e3f11a5fe7786fff3363d5388c857d852881e48954168e0d` |

完整site阶段结果为 `passed=true`、`failed_checks=[]`、`configuration_only=false`。登录页、API和公共资源均从开发容器实际请求；当前未把Windows浏览器或图形界面人工登录列为自动验收通过项。

## 7. 本地地址与VSCode Dev Container

已实测容器内 `/login` 对Host `haxpower.localhost`及`localhost`均返回200；API冒烟路径为 `/api/method/ping`。浏览器在配置VSCode localhost转发后访问 `http://haxpower.localhost:8000` 或 `http://localhost:8000`。Docker仍无主机端口发布，因此容器内HTTP通过不等于Windows端转发已建立。

Dev Container配置可解析，remoteUser与挂载目录UID1000一致，无自动安装/初始化命令或不必要秘密挂载。无法自动操作Windows VSCode图形界面，用户步骤：

1. 在WSL中运行 `code "$HOME/labs/haxpower-erpnext-v16"`。
2. 确认Windows VSCode已安装WSL及Dev Containers扩展，选择 `Dev Containers: Reopen in Container`。
3. 在容器终端进入 `/workspace/development/frappe-bench`，可运行 `bench --site haxpower.localhost list-apps`核对版本。当前已有一组Bench进程运行，不要重复启动；容器重启或进程停止后才重新运行 `bench start`。
4. 在“端口”面板确认8000/9000/6787转发至localhost，不转发3306/6379。关闭该Dev Container窗口采用stopCompose，会停止实验服务。
5. 访问登录页，使用用户自己设置的Administrator密码；不要输入真实公司资料或开始库存测试。人工登录、Setup Wizard及各业务模块界面不属于本次自动验证结果。

未为VSCode修改Docker全局配置、WSL网络模式、DNS或代理。GUI连接及浏览器登录仍待实际确认。

## 8. 实际问题与处理

- uv缓存与工作区位于不同文件系统，hardlink失败后自动copy；安装成功，不修改全局挂载或缓存配置。
- Bench venv由uv自动seed pip26.3；这是官方初始化输出，不是为绕过失败而更换Frappe/ERPNext版本。
- Frappe初始化构建早于服务地址配置，提示redis_cache无法更新assets_json；之后配置指向实验Redis，ERPNext资源构建与Redis检查通过。未改核心源码，站点实际资源加载仍需验收。
- Yarn出现less/stylus/vue-template-compiler peer dependency及workspaces警告，Browserslist提示数据过期，banking Vite提示未来configLoader兼容性；两次构建均退出0，未执行自动升级或改写上游锁文件。
- banking构建提示InterVariable两项字体引用不能解析，当前对应源码字体文件也未找到；作为该上游模块的待验证资源问题记录，不通过修改核心源码修复，不能据主bundle构建成功声称所有模块资源都可用。
- MariaDB沿用已记录的io_uring回退和memory.pressure警告，连接仍通过；负例密码测试产生预期Access denied日志。未通过删卷、清空Docker或修改sysctl处理。
- 新站点密码按要求由用户交互输入，用户确认完成后才继续验收；未接收或保存Administrator密码。交互时段数据库还保留历史Access denied记录，未据此猜测原因；当前正确root认证与站点账号连接均已验证成功，未重建或改写认证配置。
- 首次站点检查使用Bench根目录配合绝对sites_path，触发Frappe日志相对路径错误：找不到 `/workspace/development/logs/database.log`。核对锁定版本logger源码后，仅修正自有检查脚本，先进入Bench的sites目录再初始化；已增加对应回归测试，实际站点检查通过，没有修改Frappe核心或创建错误路径目录来掩盖问题。
- Bench启动日志含开发服务器和Browserslist提示，没有阻塞错误；Debugger PIN在保存前已遮盖。`bench doctor`报告新站点scheduler disabled/inactive，这是明确保留的运行限制，未通过自动启用后台定时任务使输出看起来通过。

## 9. 下一阶段库存验证的准备条件

本任务的技术前置条件已满足：测试站点已安装Frappe/ERPNext均为16.55.0，Bench已启动，完整 `--phase site`、网页/资源/API与worker检查通过，日志脱敏，核心仓库保持干净。Windows VSCode连接、人工登录、Setup Wizard、站点定时调度和完整模块字体/UI仍有明确待确认项。

完成本任务审查后，才能另行授权创建独立 `haxpower_platform` 原型App，按 [ERPNext验证计划](ERPNext-VALIDATION-PLAN.md) 执行空批次、估值、部分接收、权限和库存完整性测试。本轮不自动进入该阶段，未创建业务模块或自定义库存代码。
