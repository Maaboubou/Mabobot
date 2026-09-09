# mabowx 微信升级兼容性检查

目标：升级前留下证据，升级后找出定位和界面差异，再进行有针对性的开发。工具不根据相似控件自动修改配置，也不因定位通过就宣布完整功能兼容。

## 当前覆盖

运行时已接入 YAML：导航栏、会话容器及列表、搜索框、聊天页面、消息列表、输入框、发送按钮，包括控件失效后的重新查找。初始定位条件保持与原 Python 实现一致。

尚未迁入：主窗口启动发现、导航页名称、菜单与群设置、消息类型解析、媒体预览与下载流程等。YAML 中其他条目目前仍是参考资料，并不意味着所有业务代码已经使用它们。控件树会保留已打开页面的证据，但当前自动定位检查只覆盖上述九种选择器。

版本使用微信进程可执行文件的 Windows **FileVersion**，不是安装目录名，也不是界面中的产品版本字符串。当前实际采集值为 `4.1.12.26`。

选择规则：完全匹配优先，否则数字分段比较，选择不高于当前版本的最近配置。只有更新配置时明确报错，不自动向前套用。配置元数据与文件名必须一致。版本读取失败也明确报错。每个窗口实例缓存所选配置；调整配置后需重启 wx_bot。

`validation: pending` 表示完整功能尚未完成验收；即使定位全通过也不自动修改这个值。

## 升级前采集

在 Windows 项目根目录 PowerShell 中运行。保持微信已登录、主窗口处于聊天页面、打开相同的测试聊天。不要在采集时切换页面。首次可直接参考本次已采集的基线，长期应使用固定测试会话降低噪声。

```powershell
.\.venv\Scripts\python.exe -m mabowx.tools.compatibility capture --scenario main-chat --out scratch\compat-before --screenshot
```

输出目录必须不存在，防止覆盖旧基线。生成：

- `report.md`：可读检查报告。
- `report.json`：版本、配置选择、窗口信息、控件树、定位数量、失败信息、功能验收状态。
- `profile.yaml`：本次使用的配置快照；JSON 记录配置哈希。
- 可选 PNG：窗口矩形对应的屏幕截图，可能包含遮挡内容，不会自动激活窗口。

采集只读，不实例化会改变窗口布局的 WeChat 客户端，不发送消息、不切换聊天、不执行下载、不修改微信设置。用 UI 事务锁避免与 mabowx 的 UI 操作交叉。节点、深度或软时间预算用尽时标记 `incomplete`，不会把部分结果标为通过；单个底层 COM 调用仍可能阻塞，无法保证硬超时。

**JSON 和截图可能包含真实聊天文字、姓名和图像，仅保存在本机。不要直接提交到 Git 或对外上传。**

## 升级后检查与比较

先打开与基线一致的聊天页面和独立窗口，再采集到新目录。未知新版本自动沿用最近的旧配置。重启微信后 HWND 会变化，比较使用窗口角色及标题摘要配对，不使用 HWND 配对。

```powershell
.\.venv\Scripts\python.exe -m mabowx.tools.compatibility capture --scenario main-chat --out scratch\compat-after --screenshot
.\.venv\Scripts\python.exe -m mabowx.tools.compatibility compare scratch\compat-before\report.json scratch\compat-after\report.json --out scratch\compat-diff.json
```

生成 `compat-diff.md` 和 JSON，区分定位结果变化、结构变化、新增/缺失窗口、当前失败和功能验收状态。结构摘要忽略普通聊天文字、运行时 ID、坐标和重复结构；动态消息类型、按钮文字、页面状态仍可能制造差异，需结合截图判断。原始树保留文字、矩形及层级路径，供深入排查。

若新配置已建立，但要复查旧规则表现，可在 capture 加 `--profile-version 4.1.12.26`。报告会同时记录真实微信版本和请求的配置版本；配置哈希不同会明确提示，避免把配置修改误认为纯微信变化。

`pass` 仅表示该窗口下唯一定位成功。其他状态：`missing` 未匹配、`ambiguous` 匹配多项、`not_observed` 没有观察到相应窗口、`incomplete` 采集不完整。发送按钮因输入框为空而禁用不等于不兼容，其状态保存在原始树中。缺少聊天页面也会导致未匹配，不能直接认定是微信升级改了控件。

## 功能验收

报告有独立清单：会话切换、文本发送、文件发送、图片下载、引用图片下载、历史消息、群信息、更新弹窗。初始都是 `not_run`，**当前工具不会自动执行这些有副作用的操作**。

在指定测试会话中通过现有 mabowx 功能或人工辅助复现流程，检查实际结果，记录日志/截图等证据。再用以下命令登记（命令本身只登记，不执行测试）：

```powershell
.\.venv\Scripts\python.exe -m mabowx.tools.compatibility record scratch\compat-after\report.json --function download_quote_image --status fail --evidence "引用图片预览未打开，详见本机日志的请求 ID …"
```

支持 `pass`、`fail`、`blocked`；结果注明人工登记时间和证据，同时更新 Markdown。不能把纯手动操作微信成功当作 mabowx 自动化流程已经通过。

## 建立新版配置

发现新版本后复制上一配置到草稿，不改内置配置、不覆盖现有文件：

```powershell
.\.venv\Scripts\python.exe -m mabowx.tools.compatibility draft --version 4.1.13.30 --from-version 4.1.12.26 --out scratch\wechat_4.1.13.30_cn.yaml
```

开发时根据报告修改定位规则或 Python 流程，复测后把独立 YAML 放入 `mabowx/selectors/`，使用 `wechat_版本_cn.yaml` 命名。没有配置继承，旧版不随新版修改而改变。draft 不会自行安装草稿或声明验证通过。

## 桌面启动器交互与自动创建 YAML

启动器“设置”页提供“微信兼容性”卡片，概览的环境检查仅显示微信版本，定期读取运行中的微信文件版本（不访问 UIA）。发现版本变化后显示需要检查的提示。无对应 YAML 时自动复制最近的较旧配置，记录 `copied_from` 并设为 `validation: pending`；通过原子创建避免覆盖已有配置。没有可用旧配置或写入失败会显示错误，不猜测规则。此自动创建属于桌面启动器功能，单独运行 wx_bot 仍只选择已有配置。

- **运行兼容性检查**：后台执行只读 capture，并自动与已保存基线 compare；期间禁用重复启动，失败原因显示在卡片中。
- **查看报告 / 查看升级差异**：使用系统默认程序打开本机 Markdown 文件。
- **设为对比基线**：仅当前版本、定位检查全部通过的报告可选；需要明确点击，不自动替换旧基线，也不标记完整功能兼容。所有历史报告仍保留。

报告、截图和基线指针保存在 `data/compatibility/`；关闭再打开启动器后仍可查看。

启动器和 wx_bot 的配置都有进程内缓存：修改桌面启动器代码需完整退出并重新打开启动器；卡片显示“已准备配置”指磁盘上可用的配置，已运行 wx_bot 会在下次重启加载它。只点击服务“重启”不会重新加载桌面启动器本身。
