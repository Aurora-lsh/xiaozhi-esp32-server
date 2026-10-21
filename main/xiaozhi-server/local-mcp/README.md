# 小智本地 MCP 服务

本服务使用 MCP Python SDK，通过 Streamable HTTP 接入现有小智服务端。
提供 `calculator`（算术计算）、`get_beijing_time`（北京时间）和
`local_mcp_status`（服务状态）三个工具，均在本地执行，不需要 API Key。

## 启动与检查

先启动原有小智 Docker 部署，再在本目录执行：

```powershell
docker compose up -d
docker compose ps
docker compose logs --tail 30
docker compose exec xiaozhi-local-mcp python /app/verify.py
```

复用本机已有的小智镜像及其中的 MCP 1.22.0，无需额外下载依赖。
Compose 连接现有的 `xiaozhi-server_default` 网络；若换了部署名称，
请相应修改 Compose 中的外部网络名。服务设置了自动重启。

- 本机健康检查：<http://127.0.0.1:8004/health>
- 本机 MCP 地址：`http://127.0.0.1:8004/mcp`
- 小智容器内 MCP 地址：`http://xiaozhi-local-mcp:8004/mcp`

MCP 地址使用协议客户端访问，普通浏览器请打开健康检查地址。
宿主机端口只绑定回环地址；原小智容器通过 Docker 网络访问。

## 接入小智

在相邻的 `data/.mcp_server_settings.json` 中合并以下内容，保留已有服务：

```json
{
  "mcpServers": {
    "xiaozhi-local-mcp": {
      "url": "http://xiaozhi-local-mcp:8004/mcp",
      "transport": "streamable-http"
    }
  }
}
```

该文件已经挂载到运行中的小智容器。机器人重新连接后，小智会读取配置并发现
这三个工具；智能体的意图识别需使用函数调用模式。
这是服务端直接连接 MCP 的方式，不需要在智控台填写 WebSocket MCP 接入点。

对机器人说：“请使用计算器工具，计算一百二十八加二百五十六，再乘以三。”
预期结果为 1152。也可以说：“检查一下本地 MCP 服务状态。”
通过 `docker logs --tail 100 xiaozhi-esp32-server` 确认出现工具注册和
`执行服务端MCP工具 calculator` 日志，才能确认此次回答实际调用了 MCP。

## 验证范围

`verify.py` 检查真实 HTTP MCP 初始化、工具发现、算术结果、北京时间与状态查询，
以及非法表达式和代码执行尝试的拒绝。还可从小智容器运行它的 `--xiaozhi`
模式，验证实际服务端配置加载、工具注册与调用。
机器人语音输入和扬声器播放仍需单独实测。

从本目录在 PowerShell 执行下列命令，可验证小智容器的实际 MCP 管理器，
以及 LM Studio 中的 Qwen 3.5 9B 发出工具调用、取得结果并生成中文回答：

```powershell
Get-Content -Raw verify.py | docker exec -i xiaozhi-esp32-server python - --xiaozhi
Get-Content -Raw verify_qwen.py | docker exec -i xiaozhi-esp32-server python -
```

第二项要求 LM Studio 的 `1234` 端口能从 Docker 访问，模型名称为
`qwen/qwen3.5-9b`；可用 `--base-url` 和 `--model` 覆盖。

计算器支持数字、括号及 `+ - * / // % **`，限制表达式长度、复杂度、幂指数和
结果大小；使用 Python 常规整数/浮点运算，不执行用户代码。

## 停止

在本目录执行 `docker compose down` 仅停止此 MCP 容器。
若不再使用，还应删除 `data/.mcp_server_settings.json` 内对应服务条目。

参考：[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.22.0)。
