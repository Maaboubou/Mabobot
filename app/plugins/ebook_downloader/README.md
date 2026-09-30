# 电子书下载插件

命令示例：`找书 傲慢与偏见英文版`、`电子书 9787544722727`。

处理顺序：本地规则预检 → Mapping 模型结构化与内容分类 → 本地规则复检 → 共享 Chrome 搜索 → 高置信度自动下载或返回最多 5 个作品 → 下载前最终复检 → 微信发送文件。成功时只发文件。大陆政治传播高风险、明确色情/淫秽作品、涉及未成年人的色情内容及其他明确违法传播内容会在搜索前拒绝。

默认语言是中文（简体优先、繁体其次）。没有中文结果时不会静默发送外文，而是要求用户确认。普通文字书的默认格式顺序是 EPUB、PDF、MOBI、AZW3；扫描书、漫画和图集是 PDF、EPUB、MOBI、AZW3。

出现候选列表后，发起请求的用户可在 60 秒内直接回复序号、回复 `@机器人名 序号`，或引用该候选列表回复序号；三种方式都会进入同一个下载会话。其他用户、其他引用内容和无关文字不会被消费。

## 模型配置

在 Web 管理端的 Mapping 页面为 `ebook_downloader.parse_request` 选择主模型和备用模型。模型未映射、拒答、超时或返回无效 JSON 时流程关闭，不会进入浏览器。

## 共享浏览器

搜索和下载使用 `app.services.shared_chrome_runtime` 公共浏览器服务，与摘要、地块查询等共享 Chrome。调试端口不可用时会自动启动受管 Chrome，连接异常时会尝试重启恢复；无需先使用摘要插件。为兼容已有配置，浏览器路径、调试端口、用户数据目录、Profile 和 WebDriver 命令超时仍从 `summary_plus` 配置读取。

电子书网站账号仍需在受管 Chrome 中手动登录，普通 Chrome 的登录状态不会自动同步。任务结束只关闭电子书工作标签页并停止临时 ChromeDriver 服务，保留共享浏览器与用户数据目录。自动恢复失败时会提示检查摘要插件的浏览器配置和浏览器日志。

## 本地策略文件

内置 `download_policy_rules.json` 初始化了 **Banned Books — Open Censorship Core v2026-07-07** 中 `CN + active + banned/restricted` 的 38 个作品，并补充了部分简繁中文别名。原始数据采用 CC-BY-4.0，固定版本 DOI 为 `10.5281/zenodo.21235503`。它仍不宣称是完整或官方目录；生产部署应通过 `local_policy_path` 叠加管理员维护的 JSON，并定期独立复核。

规则字段包括 `id`、`title`、`aliases`、`authors`、`isbn`、`doi`、`source_tier`、`source_url`、`status`、`last_verified`。A/B 命中后硬拒绝，C 命中后不自动下载并要求人工复核。

规则示例：

```json
{"id":"rule-id","title":"书名","aliases":["别名"],"isbn":[],"doi":[],"source_tier":"B","source_url":"https://...","status":"active","last_verified":"2026-09-05"}
```

标题规则使用 Unicode 归一化后的完整别名匹配；原始命令预检只对至少 4 个归一化字符的标题做包含匹配，避免短标题误杀。ISBN 和 DOI 使用标准化精确匹配。
