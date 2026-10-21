# 海绵宝宝的角色故事

小智以海绵宝宝的身份聊天，可以用第一人称讲述符合比奇堡设定的原创日常。已有故事会保存到本机，下次连接同一设备时可以检索和续讲。仍使用现有 Qwen 与 GPT-SoVITS 音色，没有新增模型或联网检索服务。

## 怎样使用

直接和小智说：

- “你和派大星在水母田发生过什么有趣的事？”
- “讲一个你在蟹堡王的新日常。”
- “继续上次那个纸风车的故事。”
- “那条蓝丝带后来放在哪里？”

明确说“新故事”“换一个”时创建另一篇；在故事后接着问“后来呢”，或下次明确说“继续上次的……故事”，会向同一篇追加续篇。中间聊过其他事情，再只说“后来呢”时，不会自动接到旧故事上。
问“还记得上次的故事吗”时回顾已有记录，不新增情节或重复保存成另一篇；明确说“继续上次的故事”时才创作续篇。

初始提供三篇本机原创日常：《水母田的蓝丝带》《给章鱼哥留一段安静时间》《小蜗和纸水母》。这些不是动画集数或官方剧情。问起出处时，角色须如实说明原创身份；不编造集数，也不编造与用户在线下共同经历过的事。

## 查看和补充

打开 [本地记忆管理页](http://localhost:8003/memory/)，输入原有管理口令，选择设备，再点击“角色故事”。

- “添加故事”：自己补充标题与内容，按比奇堡设定编写。
- “查看来源”：查看当时的角色讲述原文及修改记录。
- “编辑”：纠正标题或情节，之后的回忆使用新版本。
- “忘掉”：停止召回这个故事；初始故事也不会在重连后重新出现。原始聊天文字仍保留在“聊天原文”。

角色故事与关于用户现实生活的记忆分别存放。比如“我明天考试，讲个比奇堡故事鼓励我”，考试计划仍由原有记忆功能处理，助手讲的虚构经历不作为用户的现实经历。不同设备分别积累故事和续篇。

## 保存与检索

故事使用现有 `data/long_term_memory.sqlite3` 数据库的独立表保存。原始讲述仍进入原有聊天档案，故事内容和续篇同步写入，查询不需要额外调用模型。

每次只召回相关故事的必要节选，默认最多 1200 字，计入原有的 3600 字总记忆预算；较长故事保留开头和最近的续篇。完整故事不因节选而删除。Qwen 请求继续使用已有的 16K 上下文预算与串行保护。

这台电脑的本地 Qwen 已验证并开启 `local_memory.extraction_json_schema: true`，通过 JSON Schema 约束记忆提取的返回格式，避免角色话语干扰纯 JSON 输出。正常聊天仍使用原来的自由文本或工具格式。此配置默认关闭，只在接口兼容时开启；[LM Studio 官方说明](https://lmstudio.ai/docs/developer/openai-compat/structured-output)介绍了对应接口格式。

故事越长，检索送给模型的节选越可能省略中段细节。问到遗漏的细节时可给出具体人物、物品或故事名；也可以在管理页查看全文。生成措辞与情节衔接由当前模型完成。

编辑或忘掉故事后，旧的助手讲述保留为档案，并退出自动回忆；下一轮清理会话中的旧副本。用户自己的原话及真实记忆保留。自动截取的故事标题随内容纠正同步更新，避免标题继续带入旧情节。

## 配置与更新

默认配置在 `main/xiaozhi-server/config.yaml`。需要覆盖时，在 `main/xiaozhi-server/data/.config.yaml` 添加：

```yaml
local_memory:
  character:
    enabled: true
    profile: characters/spongebob.json
    recall_max_chars: 1200
```

角色基础身份、人物关系和初始原创日常保存在 `main/xiaozhi-server/characters/spongebob.json`。运行中新增故事建议通过网页添加。修改资料文件或程序后，从仓库根目录执行：

```powershell
.\Start-Xiaozhi.ps1 -Build
```

该命令更新 Python 后端，沿用现有本地数据与模型服务。`character.enabled: false` 关闭角色身份附加和故事功能，已保存的数据仍留在本地。

## 基础设定资料

角色核心关系按 Nickelodeon 官方人物资料整理；本机初始故事为另行创作，不是这些页面中的剧情：

- [海绵宝宝](https://www.nick.com/info-page/xe31fl/spongebob-squarepants)
- [派大星](https://www.nick.com/info-page/8hz5p5/patrick-star)
- [章鱼哥](https://www.nick.com/info-page/c2e5s0/squidward-j-q-tentacles)
- [蟹老板](https://www.nick.com/info-page/0vq4xx/mr-krabs)

## 回归检查

在 `main/xiaozhi-server` 中运行：

```sh
python -m pytest tests/core/test_character_stories.py tests/core/test_local_memory.py tests/core/test_llm_context.py tests/core/test_memory_api.py tests/core/test_memory_connection.py tests/core/utils/test_dialogue.py -q
```

覆盖故事持久化、跨会话续讲、短续篇、设备隔离、手动编辑与删除、档案保留、用户真实记忆隔离、管理接口认证、角色身份注入及原有上下文保护。

2026-10-19 本机完成 89 项回归检查。真实 Qwen + GPT-SoVITS 测试中，新故事保存后断开重连，再讲出 65 字的新续篇，并追加到同一篇档案；保留纸风车与已有图案，未重复开场。该次文字提交至首包音频为 8.854 秒，完整播报 26.441 秒；这是单次观测，不含录音、VAD 与 ASR。原有饮品偏好问答也通过实测。最终检查待提取原文为 0，170 条聊天原文保留，运行日志未出现上下文、模型流或记忆提取错误。
