# Otto 无摄像头屏幕改进

源码来自用户提供的 `https://github.com/txp666/xiaozhi-esp32.git`，固定在 2.2.2 提交 `b6c61fe390dcc66d73f61d797c7ca499e02106d1`。本地工作副本位于 `.runtime/otto-firmware-source`；改动另存为此目录的 `otto-2.2.2-readable.patch`。

新增构建版本 `otto-robot-nocam-readable`，沿用原有 `NON_CAMERA_VERSION_CONFIG` 引脚和音频配置，跳过板载摄像头探测。独立 MCP 摄像头运行在原后端。新版本使用 20 号中文字体、两行字幕分页，以及“正在听／正在想／正在说”文字状态。原有表情 GIF 从现有 assets 分区加载并缩放，给字幕留出空间。

状态提示通过 hello 中的 `features.response_status` 协商，WebSocket 和 MQTT/UDP 使用同一消息解析逻辑。它只改变显示标签，音频和状态机仍走原来的 TTS 协议。旧服务器没有这个提示时仍能收听和播放。

## 构建

该旧版源码要求 ESP-IDF >= 5.5.2。本机在 `.runtime/esp-idf-5.5.2` 准备独立 SDK，复用已安装的 Python 环境，补充依赖放在 `.runtime/idf-python-extra`。运行此目录的 `Build.ps1` 构建指定版本。默认单任务；内存充足时可传 `-Jobs 4`。脚本通过 `CMAKE_BUILD_PARALLEL_LEVEL` 和 Ninja 编译/链接任务池控制并发，并复用已配置的同版本构建。结果保存在 `.runtime/voice-streaming-20261020/otto-nocam-readable-app.bin`；脚本只构建，不烧录。

重新准备源码时，先检出上述提交，再执行 `git apply /path/to/otto-2.2.2-readable.patch`。补丁新增的版本定义在 `main/boards/otto-robot/config.json` 中，构建入口是该版本原有的 `scripts/release.py otto-robot --name otto-robot-nocam-readable`。

脚本同时启用 `CMAKE_NINJA_FORCE_RESPONSE_FILE`，解决 Windows 在最终应用目标编译时的命令行长度限制，采用 [Espressif 官方处理方法](https://docs.espressif.com/projects/esp-faq/zh_CN/latest/development-environment/environment-setup.html)。

## 核验与写入范围

`test_display.cpp` 直接调用实际字幕分页函数和 JSON 状态解析函数，覆盖中文、英文、混合文字、换行和无效状态。构建通过仍需检查实机字体、表情位置、唤醒、收听、播放、打断和 MCP 摄像头。

烧录前核对芯片身份、分区表及当前启动分区，并保存当前应用与 otadata 的备份。只写应用分区，保留 NVS、分区表、海绵宝宝唤醒词和 assets。编译出的合并镜像用于打包核验，不可直接整片覆盖现有设备。

屏幕构建和实机验证的结果见本机 `.runtime/voice-streaming-20261020/` 记录；没有构建成功记录或实机检查记录时，不能视为已刷机完成。

## 本机执行记录（2026-10-20）

已完成指定无摄像头版本构建并写入 COM37 对应的原机器人。应用大小为 3,987,408 字节，写入地址为 `0x20000`。写入后独立校验通过，启动日志中的 ELF 指纹与本机构建一致，设备正常进入 idle，未发现重启循环；MultiNet7 和“海绵宝宝”自定义唤醒指令加载成功。

完整 16MB 备份保存在 `.runtime/voice-streaming-20261020/device-backup-20261020-185533/flash-original-16m.bin`。写入前后的启动和 NVS 区域逐字节一致，完整 8MB assets 分区的 SHA256 一致。运行记录为同目录上级的 `display-flash-result.json`，屏幕截图为 `display-before.jpg` 和 `display-after.jpg`（通过设备原有截图工具取得）。

Qwen 已按原配置恢复，全部后端服务就绪。实际 WebSocket 链路验证了 thinking → speaking 提示、打断后停止发送旧音频，以及同一连接中的下一轮回复；此次测试没有上下文溢出或记忆异常，待提取聊天已处理完毕。上述自动音频测试使用测试客户端。用户随后已确认：实机能通过“海绵宝宝”唤醒并正常回复，字幕与“正在听／正在想／正在说”状态显示正常。独立 MCP 摄像头拍照未在本次更新中专项测试。
