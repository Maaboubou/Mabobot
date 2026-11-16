# AI 引擎配置与迁移

适用版本：Mabobot v3.12.0。

## 首次配置与路由

1. 在 AI 运行中心创建或选择 Codex / OpenCode Profile，分别登录。两引擎不会互相复制 token；同一真实账号可填写相同 `account_key`，用于本地调度。
2. 验证该 Profile、模型及推理档位的直接生成和工具能力。验证只使用指定连接，修改参数后应重新测试。
3. 配置默认引擎与聊天覆盖；旧 Codex 模型卡继续固定 Codex，普通 API 卡不自动转换为共享订阅卡。
4. 如需共享订阅模型卡，先查看转换预览，再配置主备候选和逐任务处理方式。新路由默认关闭，应先在测试聊天显式启用。
5. 图片入口保留原账号和渠道；图片编辑、Weekly 分别配置自己的图片策略，Weekly 的页计划还需设置模型卡与直接生成路由。

模型完成、图片生成和微信送达分别记录。已派发任务遇到断线或超时后保留未知结果；取消不等于上游已经停止。任务详情中的图片操作可由管理员在核对上游与登记文件后填写结果和依据，不能据此自动重发。Weekly 恢复会复用已完成页面，未知交付应先核对。

## 部署组合

在应用 `.env` 中设置 `MABOBOT_ENABLED_ENGINES`，重启应用生效。它控制部署依赖；聊天、模型卡及主备路由仍在AI 运行中心配置。

| 安装组合 | 值 | 行为 |
| --- | --- | --- |
| Codex-only | `codex` | 沿用 Codex Profile、更新和 Skills；不会启动或探测 OpenCode |
| 混合 | `codex,opencode` | 两引擎分别登录、分别配置 Profile；共用宿主权限和账号预算 |
| OpenCode-only | `opencode` | 不启动 Codex 运行时、Codex 更新计时器或 Codex Profile 终端桥；Codex 专属图片渠道需另行启用 Codex 才能使用 |

默认值为 `codex,opencode`，兼容已有阶段 1～4 配置。引擎缺失或模型离线不阻止控制台启动；未验证的能力不因此变为可用。普通 API 模型卡保持自己的渠道，已配置的候选按原快照检查，不凭空加入备用模型。

Windows 启动器现已通过专用 WSL stdio 桥接支持 OpenCode：Web 和微信服务继续运行在 Windows，模型、认证及受限执行进程运行在 WSL；Linux 后端仍可直接运行。两个宿主之间使用私有进程管道传递控制与 MCP 回调，不新增网络监听入口。微信自动化沿用既有 `wx_bot.py` 服务和接入边界。安装 Python 3.11/3.12 和项目依赖后，控制台可先独立启动；OpenCode、浏览器、媒体、文件工具均为按需安装的组件。

```sh
# 在项目根目录，使用部署该项目的 Python 环境
python -m pip install -r requirements.txt
python scripts/manage_engines.py doctor
python start.py
```

`doctor` 只检查启用引擎的二进制版本与部署状态，不发起模型请求。控制台的引擎状态、运行工具状态和启动向导均显示部署边界。`/api/engines/deployment` 提供版本化诊断；没有账号目录验证时仍显示未验证，不把安装成功当作登录成功。

## Windows 启动器：可选的一键安装

更新代码后重启 Web 服务；重新打开启动器以加载新的配置向导，浏览器刷新控制台。

1. 启动器的“配置向导 → 可选组件”提供 **OpenCode** 安装按钮；控制台“工具与更新”也提供 **安装 OpenCode**。未安装时显示“未安装（可选）”，不会因打开页面自动下载或启动 OpenCode 的 WSL worker。
2. 点击按钮立即开始后台安装，并显示安装中 / 失败重试状态。原生安装窗口负责 WSL / Ubuntu、构建依赖及固定版本。Windows 可能请求管理员授权，Ubuntu 可能要求初始化用户或输入 sudo 密码；若系统要求重启，重启后再次点击按钮即可继续。不会自动重启电脑。
3. 默认复用适合安装的现有 WSL 发行版，必要时准备 Ubuntu / WSL 2。选择保存在项目的机器本地设置中；不改变 Codex 使用的默认发行版。发行版选择通过固定 argv 传入，浏览器不能传入宿主命令、路径或下载 URL。
4. 安装器固定上游提交、Bun 版本和补丁，使用锁文件构建，校验后才切换安装记录；已校验安装可重复运行。失败不标记成功，保留原账号及发布文件。
5. 显示“已安装并接入”后，到AI 运行中心登记 OpenCode Profile、完成独立登录，再验证模型能力和选择测试聊天。**安装不自动更换聊天引擎、模型卡或图片账号，也不复制 Codex token。**

WSL 账号保存在 Linux 用户的私有目录，Windows 不读取原生认证数据库。普通聊天 MCP 仍在宿主核对 Session、任务和权限；WSL 工具调用通过私有管道返回宿主授权执行。在途取消走独立控制通道，断线仍标记未知，不自动重放。终端记录保存其发行版归属，不把另一发行版中的进程误判为本地进程。

`data/opencode-installation.json` 和 `data/opencode-wsl-target.json` 是本机安装观察及发行版选择，不属于可移植账号备份；换机器恢复后重新安装并登录。安装按钮不会把未验证的 OpenCode 原生 Skills 或公网 shell 打开。

实际验证包括从锁定源码自动下载 / 构建，以及 **Windows Python 3.11 → wsl.exe → 新构建 OpenCode** 的严格生成、去重、反向 MCP、流式输出、在途取消与原生进程清理；只使用合成模型，不调用真实账号或发送微信。该验证不代表所有真实账号或微信客户端兼容性均已通过。

## 安装与更新 OpenCode

固定基线是 `2.3.0+mabobot.p0.1`，适配器 `1.0.0`。安装器位于 `scripts/launcher/install_opencode.py`，生产补丁位于 `scripts/launcher/patches/retain-transient-generate.patch`。不自动安装上游 latest，也不复制两个引擎的 token。

以下步骤供 Linux 部署或手动维护使用。Windows 启动器的安装按钮会自动完成准备，无需手工设置 `OPENCODE_BIN`。

1. 在宿主取得受信任的已验证二进制与独立核对的 SHA256；手动安装将 `OPENCODE_BIN` 指向该文件。
2. 分阶段安装，命令会检查摘要、精确版本，复制二进制及适配器到内容寻址目录。失败不会改变 Profile。

   ```sh
   python scripts/manage_engines.py stage-opencode \
     --binary /path/to/pinned/opencode --sha256 VERIFIED_SHA256
   ```

3. 在AI 运行中心登记 Profile、独立登录。已有 Profile 更新时，使用命令输出中的发布目录名和AI 运行中心当前 revision：

   ```sh
   python scripts/manage_engines.py activate-opencode \
     --release RELEASE_DIRECTORY_NAME --expected-revision REVISION
   ```

4. 新配置产生新的 Profile generation，重新进行该账号、模型、推理档位的单连接能力验证，再启用新任务。探测与业务调用均使用对应代际的发布文件。账号认证引用保持原绑定，不静默换号。

已接受任务保留原快照，已启动进程保留原租约。不要覆盖旧 `OPENCODE_BIN` 文件，不要在其仍被旧快照引用时清理旧发布目录。新发布文件被修改或丢失时明确失败；不寻找“另一个可用二进制”。服务更新若变更适配器协议，需要先做出站、MCP 和生命周期回归，再调整允许版本。

Codex 保留已有升级与回退入口；停用 Codex 时不检查或执行其升级。普通运行时滚动更新的旧池等待活跃调用释放，任务自身超时与显式关闭仍按原规则处理。

## 终端、Skills 与旧接口

- 终端页面按引擎列出 Profile。旧 `/api/terminal/sessions` 请求省略 `engine` 时仍选择 Codex；OpenCode 请求显式携带 `engine: "opencode"`、`full_access: false`。不接受浏览器传入命令、包装脚本路径或上游地址。
- OpenCode 终端使用独立控制目录与宿主生成的包装脚本，清除宿主 API key 环境变量，禁用项目配置自动加载和自动更新。它是管理员的交互终端，不属于普通群工具权限。Codex 的“完整访问”选项不套用到 OpenCode。
- 原回环 / 同源 / 已验证 Tailscale Serve 边界、私有 ttyd 凭据和进程身份校验保留。
- Codex Skills 继续使用原 Profile-local 存储和旧接口。受管 OpenCode 的原生 Skills 自动加载仍关闭；通用 `/api/engines/profiles/{id}/skills` 返回明确的 unsupported 状态，不去读取 Codex Skills。任务规则与授权工具继续由宿主注入。
- 原 `/api/codex/*`、旧固定 Codex 卡和字符串插件包装不随默认聊天引擎改变。旧 `mabowx-v2` 备份和 Codex 受管 Profile 恢复仍可读。

## 三类备份的含义

| 类型 | 内容 | 恢复规则 |
| --- | --- | --- |
| 应用状态 / 完整迁移备份 | 原有配置、模型卡、角色、权限、宿主档案、资产、SQLite 任务与交付账本；迁移包另含代码 | SQLite 使用在线 backup，包含已提交 WAL；恢复后自动派发暂停 |
| 引擎配置迁移包 | 引擎、Profile 的非敏感认证引用、预算标识及路由设置 | 不含模型卡文件、账号凭据、能力通过记录和原生会话；账号及模型卡需另外配置或由应用备份恢复 |
| 原有 Codex 受管账号备份 | 保留原格式允许的配置、认证文件、API key 与 Skills | 按原敏感备份机制保护，换机器重建包装脚本并核对账号 |

**OpenCode 首版采用配置恢复加重新登录。** 应用备份排除配置的 `OPENCODE_PROFILE_ROOT`、默认 `data/opencode`、原生运行时 / 缓存、终端会话凭据与发布二进制。没有新增明文 OpenCode 账号导出，也不宣称恢复其原生数据库或私有会话。现有应用备份仍未加密，可能包含 `.env`、普通 API key 和 Codex 凭据，清单与界面继续如实标记。

宿主账本必须位于项目 `data/` 内；显式配置到外部且实际存在的引擎 / 调度账本会拒绝备份，避免产生不完整的迁移包。建议全部使用相对路径：

```dotenv
ENGINE_EXECUTION_DB=data/codex_runtime.db
CODEX_RUNTIME_STATE_DB=data/codex_runtime.db
SCHEDULED_TASKS_DB=data/scheduled_tasks.sqlite3
WORK_COORDINATION_DB=data/shared_work.sqlite3
OPENCODE_PROFILE_ROOT=data/opencode
```

包含引擎表的备份使用 `mabowx-v2-engines-v1` 代际；当前版本同时接受原 `mabowx-v2`。旧代码不认识新代际，会在恢复校验时拒绝它，避免旧恢复逻辑重放新任务。备份压缩格式版本仍为 2，原 v2 Codex 备份兼容不变。

仅迁移引擎配置：

```sh
python scripts/manage_engines.py export-config --output /secure/engine-config.json
# 目标目录应为空部署；不得覆盖已有任务和配置
python scripts/manage_engines.py --project-root /new/project \
  import-config --input /secure/engine-config.json
```

## 换机器恢复演练与正式操作

1. 计划迁移时先停止源机器接单并让在途任务收尾，再生成最终备份；保留源机器的账本直到核对完成。停止目标项目的 Web、微信 Bot 和自动任务，准备空目标目录及匹配的软件依赖。将备份放到受控目录，保留原始备份。
2. 使用当前版本的恢复工具验证后恢复。已有任务与交付账本的目标目录会被拒绝覆盖；应选空目录，不删除账本绕过检查。

   ```sh
   python scripts/manage_engines.py --project-root /new/project \
     restore --archive /secure/selected.mabobot-backup.zip
   ```

3. 恢复重建相对账本路径，将 OpenCode 账号目录重置为目标项目专属 `data/opencode`；重新安装固定二进制并登录。恢复生成、产物和发送事实；未派发任务取消，已派发任务保持未知，不自动重跑。未决图片 / 发送需核对。旧原生会话头与能力通过记录清除，之后从宿主档案重建。
4. 恢复暂停期间控制台仍可查看配置。插件自动加载、主助手、日程和微信接收暂停；独立 `wx_bot.py` 也拒绝启动。核对账号、来源 / 接收聊天权限、未决操作、发送及日程；不要将“生成完成”视为“已送达”。
5. 记录核对依据并解除暂停，然后重启服务。日程和新引擎路由仍保持停用，逐项检查后手动启用；已经恢复为取消 / 未知的 run 不因解除暂停而重新派发。

   ```sh
   python scripts/manage_engines.py --project-root /new/project \
     review-recovery --evidence '已核对账号与群权限；未决图片及发送逐项记录于运维工单……'
   ```

配置迁移同样生成恢复暂停记录。此命令保存核对凭据，不伪造工具或微信回执；无法确认的操作继续保留未知状态。

## 软件版本回退

先停止新接单，等待已接受任务收尾；仍有在途任务、未知操作、待交付结果或未决日程发送时拒绝回退。选择升级前的完整迁移备份：

```sh
python scripts/manage_engines.py restore \
  --archive /secure/pre-upgrade.mabobot-backup.zip --code-only
```

只替换代码与依赖清单，保留当前 `.env`、助手 / 插件配置、模型卡、角色、档案、资产、任务 / 操作 / 交付账本及账号。日程派发保持关闭。使用与旧软件兼容的路由配置（例如对后续任务关闭新路由，选择已就绪的 Codex），按旧依赖清单准备运行环境。不得将旧数据库整体恢复到新账本上。

旧程序可能不认识恢复暂停文件，因此回退前必须完成上述收尾校验并保持服务停止；不要依赖旧程序识别新标记。回退到旧版本后，不重新启用它无法理解的新引擎任务或恢复派发。
