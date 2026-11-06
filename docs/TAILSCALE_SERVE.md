# Tailscale Serve 多机远程管理

Mabobot 默认只监听 `127.0.0.1:8888`。每台运行 Mabobot 的机器各自启用一次 Tailscale Serve，即可从同一 Tailnet 的其他设备管理它，同时不在普通局域网或公网直接开放 8888 端口。

## 每台机器执行一次

1. 安装 Tailscale，并让该机器登录你的 Tailnet。
2. 启动 Mabobot，确认本机能打开 `http://127.0.0.1:8888/health`。
3. 在 PowerShell 中进入项目目录并运行：

   ```powershell
   .\scripts\setup_tailscale_serve.ps1
   ```

   如果 Tailscale 提示权限不足，改用管理员 PowerShell。脚本会把 `http://127.0.0.1:8888` 配置为持久化 Serve 目标，并同时提供：

   - HTTPS 地址：`https://<机器名>.<tailnet>.ts.net/`
   - 短 MagicDNS 地址：`http://<机器名>:8888/`

   两个入口都只在 Tailnet 内可见；短地址需要访问设备启用 Tailscale 的 MagicDNS。

   Tailnet 第一次使用 Serve 时，Tailscale 会打印一个 `login.tailscale.com/f/serve` 确认链接。使用 Tailnet 管理员账号打开并启用 Serve，然后在该机器重新运行脚本；这是整个 Tailnet 一次性的功能开关，后续节点通常无需重复确认。

4. 在另一台已登录同一 Tailnet 的设备上，打开任一地址，例如：

   ```text
   https://desktop-a.example-tailnet.ts.net
   http://desktop-a:8888
   ```

多台机器重复以上步骤即可。每个节点都有自己的 MagicDNS/HTTPS 地址，因此不需要在 Mabobot 中维护机器清单或路由表。

## 远程使用终端

现有 Serve 入口同时支持 [Codex 终端](TERMINAL.md)，无需另开终端端口。在远程设备打开控制台后，选择侧栏用量统计下方的“终端”；也可以直接打开 `https://<机器名>.<tailnet>.ts.net/terminal` 或 `http://<机器名>:8888/terminal`。

终端自动核对本节点实际生效、指向当前 Web 端口的 Serve 配置，HTTP 与 HTTPS 入口均可使用，WebSocket 连接沿用同一入口。终端的 Profile、工作目录和命令实际运行在该节点上；关闭远程浏览器不会结束会话，重新打开页面可继续连接。

## 日常检查

查看当前节点的转发状态：

```powershell
tailscale serve status
```

应用端口不是 8888 时（短地址的端口也会随之变化）：

```powershell
.\scripts\setup_tailscale_serve.ps1 -Port 9000
```

如需停用当前节点的全部 Serve 配置：

```powershell
tailscale serve reset
```

## 安全边界

- 本方案只使用 Tailscale Serve，不启用 Funnel，因此不会主动发布公网入口。
- HTTPS 地址由 Tailscale 终止 TLS；短 HTTP 地址的链路仍运行在加密的 Tailnet 内，但浏览器会把它视为普通 HTTP。需要浏览器安全上下文能力时优先使用 HTTPS 地址。
- 当前按你的部署选择，不额外添加应用层或 Tailscale 黑白名单；能够访问该控制台的范围由 Tailnet 成员关系与现有 Tailnet 策略决定。
- 控制台目前没有独立用户登录。不要把 8888 端口映射到路由器公网，也不要把 `WEB_HOST` 改回 `0.0.0.0` 来替代 Serve。
- 管理页面、API 与微信桥接仍在各自机器上运行；Serve 只转发 Web 控制台端口，不转发微信桥接的 5555 端口。

命令语法与平台说明可参考 [Tailscale Serve 官方文档](https://tailscale.com/docs/reference/tailscale-cli/serve)。
