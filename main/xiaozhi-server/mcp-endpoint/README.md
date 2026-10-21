# 本机 Docker MCP 接入点

此目录部署教程所使用的 WebSocket MCP 接入点，以及绑定到当前智能体的计算器。

## 已部署的连接关系

```text
ESP32 → 小智后端 → MCP 接入点（8005）→ mcp_pipe → endpoint_calculator
                ↑
             智控台配置
```

服务容器：`xiaozhi-mcp-endpoint`、`xiaozhi-mcp-calculator`。
它们加入现有 `xiaozhi-server_default` 网络，均配置 `restart: unless-stopped`。
原有 `xiaozhi-local-mcp`（8004，Streamable HTTP）继续使用原配置。

## 启动、状态和日志

在本目录执行：

```powershell
docker compose up -d
docker compose ps
docker compose logs -f calculator
```

从项目根目录启动也可以：

```powershell
docker compose -f main/xiaozhi-server/mcp-endpoint/docker-compose.yml up -d
```

这两个容器不在原 `docker-compose_all.yml` 中；原文件仍负责后端、智控台、MySQL、Redis。
Docker Desktop 启动后，这两个容器会按重启策略自动恢复。

## 在智控台查看

打开 http://127.0.0.1:8002，刷新页面，进入当前智能体的“配置角色 → 意图识别 → 编辑功能 → MCP 接入点”。
刷新工具列表，应出现 `endpoint_calculator`。
“参数管理”中的 `server.mcp_endpoint` 已设置管理健康地址；“系统功能配置”中的 MCP 接入点已启用。
此地址带有管理密钥；接入点页面显示的 `ws://.../mcp/?token=...` 是给外部工具桥接器使用的地址。

机器人重新连接后，说：**“请使用 MCP 接入点计算器，计算一百二十八加二百五十六，再乘以三。”**
预期工具执行结果为 `1152`。工具层验证不代表已验证麦克风、语音识别或模型选择工具的行为。

## 验证

通过 WebSocket 中转进行发现与实际调用：

```powershell
docker exec xiaozhi-mcp-calculator python /app/verify.py
```

使用小智后端自带的 MCP 接入点客户端进行实际调用：

```powershell
.\Verify-Backend.ps1
```

两个验证都应返回 `1152`。容器健康检查只检查工具发现，不执行计算。

## 本机 IP 改变时

采用本机 WLAN IPv4 地址的 `8005` 端口，实际地址记录在 `private/deployment.json`，Windows 和 Docker 均可访问。
换网络后，先用 `ipconfig` 找出新的 WLAN IPv4 地址，然后执行：

```powershell
.\Configure-Endpoint.ps1 -EndpointHost '新的本机IPv4'
docker compose up -d --force-recreate calculator
docker restart xiaozhi-esp32-server
.\Verify-Backend.ps1
```

脚本复用智控台现有管理员登录，通过管理 API 更新参数和缓存。
若登录过期，先在智控台重新登录。首次修改前的参数备份在 `private/setup-backup.json`。

## 添加下一项外部能力

从智控台当前智能体的 MCP 接入点页面复制工具端 WebSocket 地址，把它作为新桥接器的 `MCP_ENDPOINT`。
为新的 MCP 工具另建 Docker 服务，使用同一 Docker 网络，并保持桥接器进程运行。
stdio 工具可以使用本目录的官方 `vendor/mcp_pipe.py`；HTTP/SSE MCP 服务需配合适用的 MCP 代理。
工具连入后，在智控台刷新列表；已连接的机器人需重新连接才能加载新增工具。

## 摄像头显示连接但没有调用（2026-10-20）

本机摄像头注册 `look_at_camera`（单张抓拍）和 `watch_camera`（短时间串行快照）。已连接表示桥接器连到了接入点；实际对话还需要获取最新工具列表，并执行对应工具。排查中曾发现模型只说“拍好啦”但没有工具调用，也曾发现会话列表中只有计算器。

后端现会在“拍张照看看”“你再拍一张看看前面有什么”等明确请求时刷新工具列表，再通过统一工具执行器调用 `look_at_camera`。工具退出后会移出缓存；收到 [MCP 工具列表变更通知](https://modelcontextprotocol.io/specification/2025-11-25/server/tools) 时也会刷新。连接中断后，下一次明确摄像头请求会重连。询问“你有拍照功能吗”只检查能力，不会抓拍；故事、假装拍照和禁止拍照不走此直接路由。摄像头返回失败或超时时会说明未取得画面。

真实链路已用“拍张照看看”验证：记录到一次 `look_at_camera` 调用、一次成功结果，并生成语音。该次视觉结果为“图片较模糊，无法看清具体内容”，不能据此声称已识别到具体物体。记录在项目根目录 `.runtime/camera-mcp-20261020/live-verification.json`。摄像头桥接器源码位于 `F:/CQU/Project/xiaozhi/pr/xiaozhi-esp32/CameraWebServer/mcp_server/`，本次恢复为后台运行，进程和日志记录也在上述 `.runtime` 目录。

普通 HTTP MCP 的 `/mcp` URL 和管理参数 `server.mcp_endpoint` 使用不同协议，不能直接互换。
当前部署可供这台电脑和能访问它的局域网机器使用；远程云端工具需要可达的公网或隧道 WebSocket 地址。

## 摄像头已拍照但描述模糊（2026-10-20）

实际原图为横置的 QVGA 320×240 房间照片。使用用户授权的识别对比后，640×480 原图能得到柜门、背包和悬挂物等更具体的描述。摄像头 MCP 桥接器现在会在每次抓拍前设置 VGA 640×480、JPEG quality=10；摄像头断电后，下次 MCP 调用会重新应用。该设置不会开启定时识别。

“拍张照看看”等通用动作指令会转换为描述主要物体及位置，具体视觉问题则交给视觉模型；提示词要求依据可见事实回答，并保留不确定性。当前摄像头固件先将 URL 编码后的 prompt 复制到 256 字节缓冲区，再解码中文。桥接器已按编码后的 255 个字符限制长度，避免中文截断使视觉接口报 `invalid unicode`；长问题会截短，宜简短提问。此修改位于独立摄像头的 `CameraWebServer/mcp_server/camera_mcp.py` 与 `esp32_client.py`，不需要重新烧录 Otto 固件。

40 项摄像头桥接回归测试已通过，包括画质重应用、通用指令转换、编码长度边界及失败不触发拍照。原始对比、源码备份与部署记录位于项目根目录 `.runtime/camera-mcp-20261020/`。分辨率提升不能纠正光学失焦、逆光或横置取景，应同时在摄像头网页核对原图。

## 抓拍失败且没有错误语音（2026-10-20）

一次故障同时包含两个问题：摄像头记录地址 `10.68.63.95` 的 HTTP 请求连接超时，且 Windows 无法取得该地址的局域网邻居信息；GPT-SoVITS 则因缺失 NLTK 英文发音数据，在英文文本处理报错后出现 `t2s_model is None`，导致流式连接中断、普通合成返回 400。MCP 返回结果中的 `success:false` 才是抓拍失败状态，外层 RPC 调用成功不能表示拿到了照片。

本机已补齐 `cmudict`、`averaged_perceptron_tagger`、`averaged_perceptron_tagger_eng` 到 `.runtime/gpt-sovits/runtime/nltk_data/`，并在语音启动脚本中设置 `NLTK_DATA`。数据按 [NLTK 官方手动安装方法](https://www.nltk.org/data.html) 下载，核对官方索引校验和后保存。原有海绵宝宝权重已重新加载，实际设备配置下的中文普通合成和中英混合流式合成都生成了非静音音频。接口文档能打开仅说明 HTTP 服务在线，不能代替真实合成检查。

当时有两个摄像头桥接实例，已停止旧实例、保留拥有注册监听的实例。摄像头的 `start_mcp.ps1` 现在会检查同一路径的已有桥接并直接复用；客户端失败提示会保留接口路径与错误类别，避免将 HTTP 错误一律解释为设备离线。新增测试后共 42 项通过。当前局域网拍照连接仍需摄像头重新联网核验；本次修复没有自动抓拍或上传照片。

用户随后给独立摄像头重新上电，同一地址恢复访问。实际摄像头客户端已成功设置 VGA/quality=10，并连续三次读取状态成功，耗时约 0.08–0.12 秒；识别计数保持为 0。后端原生 MCP 客户端也发现了 `look_at_camera`、`watch_camera` 和计算器工具。这里只验证了本地连接、画质设置和工具发现，新的实际拍照与识别由用户自行体验，未自动上传照片。恢复记录为 `.runtime/camera-mcp-20261020/connection-and-tts-repair.json`。

## 文件说明

- `docker-compose.yml`：固定镜像摘要的两个容器。
- `vendor/mcp_pipe.py`：官方桥接器；版本见 `vendor/SOURCE.md`。
- `calculator.py`：复用相邻 `local-mcp/server.py` 的算术解析器，以独立工具名注册到接入点。
- `data/.mcp-endpoint-server.cfg`：持久化中转配置和密钥。
- `private/calculator.env`：计算器使用的接入凭据。
- `Configure-Endpoint.ps1`：同步智控台参数及计算器接入地址。

`data/` 和 `private/` 已在本目录忽略，保留它们即可保留接入凭据。接入地址中的 token 和管理 key 不应分享。

参考：[小智服务端 MCP 接入点](https://github.com/xinnan-tech/mcp-endpoint-server)、[官方计算器桥接示例](https://github.com/78/mcp-calculator)。
