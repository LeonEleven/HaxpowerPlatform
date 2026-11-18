# Stage 0 前端静态资源排查与修复报告

日期：2026-11-18（Asia/Shanghai）。任务属于原型环境故障排查与测试；站点为隔离实验中的 `haxpower.localhost`，Windows 地址为 `http://localhost:8000`。

## 1. 结论与边界

用户报告登录页和 Setup Wizard 无样式、内容靠左、图标巨大。排查时，在执行任何清理命令之前，服务器资源、文件链接及无缓存浏览器登录页均已正常。用户随后明确反馈：**“禁用缓存刷新后已恢复”**。

本次最小处理为 Chrome 禁用缓存刷新，以及使用受支持的 Bench 命令清理该实验站点缓存。证据支持陈旧浏览器缓存或页面状态这一判断；没有取得原始失败请求，且同时执行过服务器缓存清理，不能进一步断言具体坏缓存条目或唯一根因。没有发现需要重建资源的服务器文件问题。

没有重建站点、删除数据库/数据卷、重新安装、构建资源、升级版本、修改核心源码或全局配置，也未开始库存测试。现有 Bench 和 watch 保持运行。

## 2. 修复前检查

- 只检查现有 Compose Project `haxpower-erpnext-v16-lab`。四个基础容器均 running / healthy，无 Docker 主机端口发布，Windows 通过 VSCode 转发访问。
- 同一组 Honcho 主进程 PID 7140 和 esbuild `--watch --live-reload` PID 7271 在排查前后均存在；Honcho 的子进程不计为第二组 Bench。未再次执行 `bench start`。
- `sites/assets/frappe`、`sites/assets/erpnext` 均为正确符号链接，分别解析到容器内 `apps/frappe/frappe/public`、`apps/erpnext/erpnext/public`。被引用文件均存在。链接是容器绝对路径，不能依据宿主直接解析失败误判文件丢失。
- `assets.json` 的映射与实际登录页引用一致；修复前后 SHA-256 均为 `35438995450ef5365949c261ab3c0ff43d234d254d0f8d12ee917f1afb1f4157`。构建产物可用，无须再次构建。
- 容器内直接访问 `127.0.0.1:8000`，分别指定 Host `localhost`、`haxpower.localhost`，登录 HTML、资源路径和内容一致。`/app` 与 `/app/setup-wizard` 的访客请求最终跳转到 `/login`，没有把这个 200 当作已登录向导验收。
- 使用镜像已提供的 Chromium 无缓存访问登录页，无网络失败；三个外部样式表分别加载 4559、37、198 条规则。1280px 视口中登录卡片宽 400px、左边距 440px、padding 24px，实际可见图标为 16×16px，截图布局正常。字体请求返回 200 / `font/woff2`，SVG sprites 返回 200 / `image/svg+xml`。

登录 HTML 实际引用如下（两个 Host 结果相同）：

| URL | 状态 | Content-Type | 字节数 |
| --- | --- | --- | --- |
| `/assets/frappe/dist/css/website.bundle.CCN4GMVQ.css` | 200 | text/css; charset=utf-8 | 696764 |
| `/assets/erpnext/dist/css/erpnext-web.bundle.AHY3JPZX.css` | 200 | text/css; charset=utf-8 | 3538 |
| `/assets/frappe/dist/css/login.bundle.BH2VMAEO.css` | 200 | text/css; charset=utf-8 | 86448 |
| `/assets/frappe/dist/js/frappe-web.bundle.MONQFRZH.js` | 200 | text/javascript; charset=utf-8 | 1367187 |

这些响应完整内容的 SHA-256 与磁盘对应文件一致。CSS 起始内容为 `@charset "UTF-8";` 或构建注释及样式规则，JS 起始内容为 `(() => {`；不是错误页或登录 HTML。详细证据保存长度、摘要及前 100 字符，不保存完整 HTML、登录会话或密码。

## 3. 最小修复与内存评估

2026-11-18 10:03:06 +08:00，在现有 frappe 容器的 Bench 目录执行：

```bash
bench --site haxpower.localhost clear-cache
bench --site haxpower.localhost clear-website-cache
```

两个命令均退出 0。Frappe 的资源映射可能经过缓存，但本轮清理前映射已经正确，不能据此声称服务器映射失效是已证实根因。用户在 Windows Chrome 的 F12 → Network 勾选 Disable cache、保持开发者工具打开并刷新后，确认样式恢复。

frappe 容器上限 4 GiB，排查开始约使用 2.445 GiB，完整复验时约 2.562 GiB / 4 GiB；已有 watch 且 VSCode 已连接。资源已正确，因此不额外执行 `bench build`，避免不必要的并行构建与内存压力。未停止 watch、重启容器或改变 developer_mode。

## 4. 修复后资源与 Setup Wizard 验证

加强后的检查对每个 Host 分别验证 16 个 CSS/JS URL，共 32 次资源请求：全部 200、MIME 正确、无跳转、完整响应与磁盘文件相同。上述四个登录资源之外，Desk 模板 hooks 解析出的共享资源如下：

| 应用 / 目录（前缀 `/assets/`） | 文件 | 状态 / MIME |
| --- | --- | --- |
| frappe/dist/css | desk.bundle.AKDJIRRD.css | 200 / text/css |
| frappe/dist/css | report.bundle.MKQQAUMJ.css | 200 / text/css |
| erpnext/dist/css | erpnext.bundle.XEMCHUDP.css | 200 / text/css |
| frappe/dist/js | libs.bundle.7EJQO4AW.js | 200 / text/javascript |
| frappe/dist/js | billing.bundle.DBDNFU32.js | 200 / text/javascript |
| frappe/dist/js | desk.bundle.4SWXOLO5.js | 200 / text/javascript |
| frappe/dist/js | list.bundle.ABSGJ5XO.js | 200 / text/javascript |
| frappe/dist/js | form.bundle.J7NT7MSX.js | 200 / text/javascript |
| frappe/dist/js | controls.bundle.XPNXKXHB.js | 200 / text/javascript |
| frappe/dist/js | report.bundle.CDZX2ELP.js | 200 / text/javascript |
| frappe/dist/js | telemetry.bundle.KKJKIQIU.js | 200 / text/javascript |
| erpnext/dist/js | erpnext.bundle.4G5DWSQN.js | 200 / text/javascript |

Setup Wizard 是 Desk 内的 Page，共用上述 Desk 样式与脚本。检查通过本地 Frappe 的受支持 Page loader 只读取得 `setup-wizard` 页面资源，没有执行向导安装或创建数据。页面脚本 27523 字节，SHA-256 为 `4e3db647c8ab3a8d7de64f36d6e5217588c02333876ceeadd133ae079c4230a8`，`node --check` 通过；该 Page 本身没有额外 CSS。

没有取得用户密码或 Cookie，没有自动化登录后的 Windows Chrome/Setup Wizard DOM 验收；共享静态资源检查和脚本语法检查不能代替该验收。用户反馈当前异常已恢复，逐页 Network 与取消禁用缓存后的复查步骤见第 8 节。

## 5. 版本、核心与服务复验

| 项目 | 实测结果 |
| --- | --- |
| Frappe | 16.55.0，Commit `90c8b61afd60ee49f982ca1a9751008e4ae1e24c`，核心工作区干净 |
| ERPNext | 16.55.0，Commit `8e10a348daaa93853de8c36dc55ec789a440c219`，核心工作区干净 |
| 工具 | Python 3.14.10、Node 24.22.3、Yarn 1.22.22、Bench 5.31.1 |
| MariaDB | 11.8.9，root 检查及站点账号连接通过，错误/空密码被拒绝 |
| Redis | Cache / Queue 均 7.4.12，连接与 PING 通过 |
| Bench | 单组开发进程、1 个在线 worker；scheduler 仍 disabled / inactive，未自动启用 |
| HTTP/API | 两个 Host 的 `/login` 200、`/api/method/ping` 返回 pong |
| 容器 | frappe、mariadb、redis-cache、redis-queue 均健康；Digest 锁定、网络及专属卷隔离检查通过 |
| 日志 | Bench 日志最近片段无 traceback/fatal/OOM；MariaDB 保留已知 io_uring / memory.pressure 提示及认证负例 Access denied，连接正常 |

## 6. 此前验收通过的原因与工具改进

2026-11-17 的 `site-installed-checks.json` 19 项通过结果保留不变。旧 HTTP 检查只验证登录/API 与 manifest 中 `login.bundle.css`、`frappe-web.bundle.js` 的可访问性；资源只判断 200、读取前 1024 字节，没有校验 MIME、完整内容、实际 HTML 引用、网站/ERPNext 样式或 Desk 共享资源，也没有模拟 Windows 浏览器的缓存与视觉布局。

因此，旧结果只能说明当时这些 URL 可达，不能证明每个浏览器后来仍渲染正常。本轮也未观察到服务器返回错误内容，不能把上述检查缺口当作已发生的服务器故障证据。

新增 `asset_checks.py` 并接入既有 `--phase site`：解析真实登录 HTML，解析 Desk hooks，检查两个 Host、MIME、完整内容摘要、磁盘文件、manifest 映射及符号链接范围，拒绝返回 HTML 的伪成功。新增 9 个资源回归测试；与原有 8 个测试合计 17 项，普通模式和 `python -O` 均通过。安全校验使用显式异常，不依赖 assert。工具不安装应用、不启动 Bench、不登录、不保存 Setup Wizard 源码或站点配置。

## 7. 证据与检查命令

本地详细证据位于实验目录，未纳入 Git；所有摘要为 SHA-256：

| 相对 `~/labs/haxpower-erpnext-v16/` 的路径 | 摘要 |
| --- | --- |
| asset-diagnostics/20261118T095425-before.json | `d910ad05ecc7fc5ab40715e5aa9f83ffdb7539c940c72d0607288409353422cb` |
| asset-diagnostics/browser-before.json | `6713ca5904126675a717408e6d0ff2e08423294e0bf0806d6be46be3b36d262e` |
| asset-diagnostics/login-before.png | `3885cdd2992e83354ab53dc39af1633bd2cce53d353f1b1f5a12f01165b6d369` |
| asset-diagnostics/cache-refresh.json | `9e0ba0ecace9e94472e17c6901aca3983b3e61b686612549254150f60d444de3` |
| asset-diagnostics/20261118T100539-after.json | `215bf53699a711a8b660a09695d2d6c769a3e9f237fb5732feea0ed6ea407d61` |
| site-assets-recheck-20261118.json | `c062fb9587a41f4cc0b5d23eb5687876ddfd60964981ebdb043613c9600576c1` |

完整 site 复验于 2026-11-18 10:09:03 +08:00 开始，19 项全部通过，`configuration_only=false`、`failed_checks=[]`。历史基础证据摘要仍为 `5bf819e5a07a7ffcbde398214373b750ce5f79abb327d2766686d546396e45e8`，历史安装证据摘要仍为 `8514c1857200cca1e3f11a5fe7786fff3363d5388c857d852881e48954168e0d`。

```bash
python3 dev/erpnext-lab/verify.py --phase site --output "$HOME/labs/haxpower-erpnext-v16/site-assets-recheck-20261118.json"
python3 -m unittest discover -s dev/erpnext-lab -p 'test_*.py' -v
python3 -O -m unittest discover -s dev/erpnext-lab -p 'test_*.py' -v
git diff --check
```

输出文件已存在，未来复测必须换新文件名，不覆盖本轮或历史证据。提交前检查变更与敏感信息，仅提交自有验证代码、测试、说明和本报告。

## 8. Windows Chrome 人工复验与剩余限制

1. 保持现有 Bench 运行和 VSCode 8000/9000/6787 localhost 转发；不要再启动 Bench。
2. 在 `http://localhost:8000` 按 F12，Network 勾选 Disable cache 后刷新。用户已确认样式恢复；登录页可在无痕窗口查看，当前登录会话无需退出。
3. 用自己的现有会话进入 Setup Wizard，确认布局、图标正常；筛选 CSS/JS，请求应为 200，CSS 的 Content-Type 为 text/css，JS 为 text/javascript 或 application/javascript，Response 不应为登录 HTML。不要提交向导或录入真实公司信息。
4. 取消 Disable cache，普通刷新再验证。若复发，仅提供失败 URL、状态码、MIME 与脱敏 Console 错误，不分享密码、Cookie、Authorization 或完整 HAR。

当前没有原始失败请求可供回放，不能精确定位当时缓存异常的层次。已登录向导的自动化 UI 检查、所有业务模块和历史 banking 字体构建警告仍未被本次登录页验收覆盖。

本任务分支 `fix/erpnext-static-assets` 最初基于站点安装提交 `db228edb78477115ddc355cd1b2990838b3a86d7`。首次排查交付时安装 PR #3 尚未合并，PR #4 当时以 Draft 提交并注明依赖。PR #3 后续已完成 Squash 合并，PR #4 的分支同步与复验见第 9 节；不自动合并 PR，不进入下一阶段库存验证。

## 9. PR #3 合并后的冲突修复与复验

2026-11-18，确认 PR #3 已于 10:23:55 +08:00 Squash 合并到 main，提交为 `21b22ec019ffc928885fc84fc0fd77fcc7816289`。开始时本地工作区干净，PR #4 远端分支存在且与本地一致。使用 merge 将 `origin/main` 合入 `fix/erpnext-static-assets`，不 rebase、不强推。

Squash 提交的文件树与原安装提交一致，但提交历史不同，产生 README 内容冲突及 site_checks.py 的 add/add 冲突。逐项处理如下：

- README：保留 main 已验证的安装、交互建站、服务隔离和安全说明；保留 PR #4 的静态资源报告链接、检查工具说明、两个测试文件的通配运行命令及缓存/浏览器验收步骤。更新已过时的“VSCode 尚未连接”“基础阶段无 Web”描述，将首次启动日志明确为历史日志，不重复启动 Bench。相对 Markdown 链接检查通过。
- site_checks.py：工具版本、数据库正/负例、Redis、应用 Commit/核心干净状态、站点账号及 worker 检查与 main 完全一致。HTTP 检查保留 asset_checks.py 集成，其中包含登录页与 API ping，并加强 MIME、内容、文件与两个 Host 校验。没有同时保留旧的仅状态码检查，避免重复或弱化验收。base/bench/site 的阶段路由和安全配置检查保持原样。

原有验证工具 8 项、新增资源检查 9 项均保留，普通模式和 `python -O` 各 17 项通过。因实验环境正在运行，还依次执行三阶段验证，全部通过；证据使用全新文件名，未覆盖历史记录：

| 实验根目录中的新证据 | 检查通过数量 | SHA-256 |
| --- | --- | --- |
| merge-pr4-20261118T103240-base.json | 15 | `77d4590b3c1ba2c037d1237d6299dbc914815b7394b99389542c641f0f7fadd2` |
| merge-pr4-20261118T103240-bench.json | 16 | `0b32d3f035e11636178d5b64760c01a34c5325a4b6da1a7b3b1cb2f7bee637bd` |
| merge-pr4-20261118T103240-site.json | 19 | `aa1b7ef7e95f12742d4c2cf656490b0d453481f6533a65075dee02b43da3d7a3` |

本次未重新清理缓存、构建资源或改变实验服务；只执行检查并保存本地证据。Frappe/ERPNext 锁定版本和核心源码干净状态复验通过。已知的 scheduler disabled/inactive 和未自动化登录向导验收限制不变，没有因合并引入新的环境异常。PR #4 相对最新 main 的文件范围仅为原有五个静态资源验证/测试及文档文件，不再包含 PR #3 的安装变更。
