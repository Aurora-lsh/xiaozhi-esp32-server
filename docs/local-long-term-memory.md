# 本地长期记忆 1.0

Windows 笔记本日常开机时，可使用项目根目录的 `Start-Xiaozhi.ps1` 启动现有完整后端；步骤见 [Windows 开机到后端就绪流程](windows-startup.md)。

本功能默认开启，更新代码后重启 Python 的 `xiaozhi-server` 即可。使用包含旧代码的 Docker 镜像时需重新构建镜像再启动，并映射 HTTP 端口。单服务和连接智控台两种部署都支持，无需部署额外的数据库、向量服务或嵌入模型。

## 当前项目的 Docker 全模块启动

现有的 `docker-compose_all.yml` 使用官方预构建镜像。要运行本地新增的代码，请同时使用 `docker-compose.memory.yml`，在 `main/xiaozhi-server` 目录执行：

```powershell
docker compose -f docker-compose_all.yml -f docker-compose.memory.yml up -d --build --no-deps xiaozhi-esp32-server
docker compose -f docker-compose_all.yml -f docker-compose.memory.yml logs -f --tail 100 xiaozhi-esp32-server
```

该命令构建本地代码，只更新 Python 后端，沿用原有 `data/` 与模型挂载。智控台、MySQL、Redis 应已正常运行。以后更新此后端继续使用这两个 Compose 文件。此覆盖配置固定了原有基础镜像的摘要；本机没有该镜像时才需下载。

Python 镜像有独立的构建排除规则，只发送服务源码；MySQL 数据、聊天数据、临时文件、socket 以及已挂载的 SenseVoice 模型权重不会进入构建上下文。遇到 `invalid file request ... mysql.sock` 时请更新仓库根目录的 `.dockerignore` 和 `Dockerfile-server.dockerignore`，然后重新执行上述构建命令，不要删除数据库目录或 socket。

Docker 的宿主机请求会通过容器网络进入服务，需要先在 `data/.config.yaml` 的 `local_memory` 中配置自己的 `management_token`，启动后在记忆页面“访问设置”输入相同口令。不要因为浏览器地址是 localhost 而省略此步骤。

## 使用

在部署服务的电脑上打开 `http://localhost:8003/memory/`。如果配置了其他 `server.http_port`，请使用对应端口。页面会显示聊过天的设备，可以按设备搜索记忆、查看来源、编辑、删除和浏览聊天原文。各设备相互独立，即使绑定在同一个智能体上也不共享新记忆。

正常聊天会自动提取个人信息、偏好、关系、经历、计划和约定。可以直接说：

- “记住，我的猫叫奶茶。”
- “你记得我什么？”或“查看你关于我的考试的记忆。”
- “你记错了，我的猫叫布丁，把这条记忆改过来。”
- “忘掉关于我的猫叫什么的记忆。”

海绵宝宝的比奇堡原创日常使用独立的“角色故事”页签，可自动保存、续讲与手动补充，不作为用户的现实经历。详见 [角色故事使用说明](character-stories.md)。

操作对象不明确时，小智会询问具体是哪件事。语音管理不要求模型支持 function calling，但需要所配置的模型能输出有效 JSON。自然的事实变化（“我以前喜欢咖啡，现在戒了”）会保留过去与现在两条记忆；明确纠正错误则记录修改痕迹，旧内容不再作为有效事实参与回忆。

值得后续关心的事情会记录适合随访的时间。你下次聊天时，若时间合适，小智会得到一条关心建议，并优先回答你当前的问题。每次会话最多提供一条建议，同一事项默认间隔72小时；得到明确结果后停止跟进。它不会在没有人聊天时主动唤醒设备。具体措辞和是否适合当时询问由聊天模型决定。

## 本地配置

在 `main/xiaozhi-server/data/.config.yaml` 中配置以下字段。连接智控台时这些字段同样生效；它们是部署电脑的设置，优先于智控台中的 Memory 模型。其他聊天和模型配置仍按原有方式使用。

```yaml
local_memory:
  enabled: true
  database_path: data/long_term_memory.sqlite3
  llm: ""                         # 留空使用当前聊天模型，也可填写已配置的 LLM 名称
  migrate_legacy: true
  follow_up_cooldown_hours: 72
  query_rewrite: true              # 追问往事时，调用现有模型扩展检索词
  query_rewrite_on_miss: true      # 本地已有明确匹配时，省去扩展词的模型调用
  extraction_idle_seconds: 10     # 回复后等待短暂空闲再提取；新一轮聊天会取消等待中的任务
  extraction_batch_chars: 1800     # 每批提取的原文长度；超长原文分段处理
  extraction_max_tokens: 1200      # 每次提取的输出上限
  recall_max_chars: 3600           # 每轮注入的记忆与原话节选长度上限
  management_token: ""            # 留空只允许本机直接访问管理 API
```

数据库路径以 `main/xiaozhi-server` 为基准，也可以填写绝对路径。聊天原文和记忆写入同一个 SQLite 数据库，采用 WAL 和事务；原文在消息加入对话时就保存，模型提取在后台进行。提取失败不丢原文，下次聊天或重新连接会重试。后续追问先检索记忆与相关原文，再让模型组织回答；没有依据时要求模型明确说明，不编造。

为减少等待，检索先检查本地档案；有明确关键词匹配时直接把结果交给聊天模型，缺少匹配时仍调用模型扩展同义词。自动提取默认等回复后空闲 10 秒再运行，同一设备新一轮聊天会取消尚未开始的提取任务，原文始终即时保存；断开连接后也会继续安排提取。将 `extraction_idle_seconds` 设为 `0` 可恢复立即提取，将 `query_rewrite_on_miss` 设为 `false` 可恢复每次符合条件都扩展检索词。

原文和记忆不会按时间自动清理。`enabled: false` 可切回原有 Memory 模型行为，已保存的本地数据库仍保留。自动迁移只导入旧 `data/.memory.yaml` 中同设备的摘要，并明确标记缺少原文来源；智控台的智能体摘要可能由多台设备共享，因此不会自动导入新设备记忆。功能启用前已有的历史聊天不会自动回填。

## 本地模型的上下文限制

OpenAI 兼容提供器会识别本机 1234 端口的 LM Studio，默认按 8192 上下文安排请求，并让同一模型的聊天、记忆提取和检索词扩展排队执行，避免并发生成耗尽共享 KV 缓存。其他服务默认沿用原有并发方式。模型配置可显式设置 `context_window`（应与模型实际加载值一致）和 `serialize_requests: true`。

当前笔记本的设备工具说明较长，已将智控台模型配置和 LM Studio 实际加载值同时设为 16384；统一启动脚本会维持此值。不能只修改请求中的预算而不重新加载模型。工具名称、参数及说明保持完整，较早的聊天历史可按预算裁剪。

本机 LM Studio 的 Qwen 默认使用 `reasoning_effort: none` 关闭内部思考，避免短 JSON 请求在思考阶段耗尽输出预算而返回空内容。此参数已在当前 `qwen/qwen3.5-9b` 上验证；模型配置可以显式覆盖 `reasoning_effort`。

聊天请求会为输出预留空间，按完整轮次移除较早的历史，保留当前问题及工具调用与结果的对应关系。若服务仍报告上下文超限，仅在尚未输出文字或工具调用时缩小预算重试一次，避免重复播报或执行工具。调整仅作用于发送给模型的副本，本地原始档案不变。

记忆提取使用较小批次和已有记忆的必要字段。超长原文分段提取，全部片段成功后才标记处理完成；中途失败仍保留待处理状态。召回只注入相关记忆及原话节选，完整档案始终可以通过管理页查看和检索。

## 修改、删除与原始档案

网页编辑与语音修改共用同一个数据库。明确删除后会保留删除标记，相关旧原文及其助手回复不再参与自动回忆或提取；原文仍能在“聊天原文”中查看和搜索，并标注不参与回忆。修改记错的事实也会排除其错误来源，保留可查的纠错记录。下一轮对话会清理当前会话中可能携带旧信息的上下文。以后你明确说“重新记住……”可以重新保存一条新记忆。

这里的“忘掉”表示停止将内容作为记忆使用，不等于擦除原始聊天档案。原文长期保留，符合1.0的档案要求。

## 管理访问与备份

默认请直接在服务电脑上访问 localhost；管理 API 不信任转发头，也不开放跨域调用。若服务在 Docker 中，或需要局域网管理，应设置独立的 `management_token`，并在页面“访问设置”中输入；每次 API 请求都会验证口令。页面不把口令写入浏览器的本地存储。

数据保存在自己的电脑，记忆提取、检索词扩展和回答会按当前大模型配置发送所需文字到模型接口。没有新增独立的记忆云服务。使用容器时，应将 `data/` 挂载为持久化目录。

备份时停止 Python 服务后复制 `data/long_term_memory.sqlite3`；在线备份请使用 SQLite 的 backup API，避免只复制正在使用的主文件而遗漏 WAL 中的数据。

## 验证

在 `main/xiaozhi-server` 目录运行：

```sh
python -m pytest tests/core/test_llm_context.py tests/core/test_local_memory.py tests/core/test_memory_api.py tests/core/test_memory_connection.py tests/core/utils/test_dialogue.py -q
```

测试使用模拟模型与临时数据库，不调用真实模型接口，覆盖持久化、设备隔离、历史变化、纠错删除、原文来源、提取失败重试、主动关心时机和管理接口，同时检查本地模型请求排队、上下文裁剪、超限重试及长原文分段处理。
