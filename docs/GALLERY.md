# 图片管理 · 映像

图片管理作为 AstrBot 的插件 Pages（Canvas Page）提供。安装插件即可使用已构建好的 HTML/CSS/JavaScript 页面，不需要 Node.js、前端编译或额外启动 Web 服务。

## 打开图库

1. 使用支持插件 Pages 和 `astrbot.api.web` 的 AstrBot。开发与联调使用 **AstrBot v4.28.0** 的官方 Web API helper 和 Bridge SDK。
2. 更新插件后重载插件，打开 **插件 → AI 绘图聚合 → 图片管理**。
3. 插件配置中「图片管理」的「启用图片管理」「自动保存生成结果」默认开启。
4. 发送一次生图命令，成功结果就会归档；页面中点击「刷新」可看到新作品，也可以导入本地图片。

不支持 Pages API 的旧 AstrBot 会在日志中提示升级。生图和自动归档仍然可用，升级框架后可以看到此前已归档的图片。

旧插件只写临时目录，没有完整的历史索引。本版本从启用后开始归档；手动导入旧图可以保存文件，但不能还原已经丢失的生成提示词和模型记录。

## 日常使用

| 功能 | 使用方式 |
| --- | --- |
| 查找图片 | 顶部搜索支持名称、提示词、标签、备注、模型、提供商、创作者及用户/群组 ID |
| 筛选 | 来源页签区分 AI 生成与本地导入；「筛选」中选择模型、标签、方向或日期 |
| 日期 | 按当前浏览器时区理解日期，包含所选结束日期的全天 |
| 排序 | 支持最新、最早、文件大小和名称排序，每页 48 张 |
| 预览 | 点击图片查看详情；方向键切换本页图片，Esc 关闭；放大按钮查看更大的预览 |
| 收藏 | 图片右上角爱心；详情页也可按 F 切换收藏 |
| 相册 | 左侧新建相册；批量加入相册，或在详情中勾选多个所属相册 |
| 标签与备注 | 在详情页编辑后点「保存修改」；批量添加标签保留原有标签 |
| 批量选择 | 点击工具栏的选择按钮，勾选图片或全选本页，支持在同一视图跨页选择 |
| 移出相册 | 在某个相册中选择图片，使用「移出相册」，原图保留在图库 |
| 删除 | 移入回收站后可恢复；浮动提示也提供短时间的「撤销」按钮 |
| 永久删除 | 仅对回收站内选中的图片生效，需要再次确认；不会自动清空回收站 |
| 下载 | 详情页下载原图；批量下载生成 ZIP，同时保留提示词等元数据 |
| 导入 | 点击「导入图片」或拖放文件，在相册内导入会加入当前相册 |

复制生成命令不会自动发送消息或调用模型。重新生成会沿用原指令与请求参数；图生图还需重新附上参考图片。参考图片本身不会额外归档。浏览器限制剪贴板访问时，页面提供可选中文本供手动复制。

## 配置

配置项位于 `gallery_config`：

| 项目 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 启用归档存储与管理接口；关闭不会清除已有图片 |
| `auto_archive` | `true` | 自动收录成功生成的图片；关闭后仍可浏览和手动导入 |
| `max_storage_mb` | `0` | 图库容量上限，0 为不限；统计原图、预览、缩略图及回收站 |
| `max_upload_mb` | `25` | 单张导入大小限制，可设 1–100 MB |

达到容量上限后不再新增归档或导入，不会自动删除图片。生图结果仍照常发送，归档失败会写入 AstrBot 日志。需要释放空间时，先在回收站中永久删除不再需要的图片，或增加容量配置。

容量显示统计受管理的图片文件，不含 SQLite 数据库本身。配置更改后重载插件生效。

## 文件与元数据

图库位于 AstrBot 为插件分配的持久化数据目录：

```text
data/plugin_data/astrbot_plugin_ai_image/gallery/
├── library.sqlite3       # 索引、相册、标签及生成信息
├── originals/            # 原始图片字节
├── thumbnails/           # 最长边 480px 的 WebP 缩略图
└── previews/             # 最长边 1800px 的 WebP 预览
```

原图与插件源码分开保存，更新插件源码不会覆盖图库。备份或迁移时，停止插件并复制整个 `gallery` 目录；恢复时放回相同的插件数据位置。批量 ZIP 适合导出选中的原图与元数据；本版导入入口接收图片文件，不直接恢复 ZIP 中的相册和提示词。完整恢复请使用整个数据目录的备份。

索引记录提示词、实际成功的提供商和模型、原指令、请求比例和分辨率、创作者、用户/群组 ID、参考图数量、耗时及实际图片尺寸。图片归档和页面 API 不读取或保存提供商 API Key。

文件导入会按 SHA-256 查重，重复文件保留已有信息；若重复文件已在回收站，会提示它的位置而不会擅自恢复。不同生成请求即使返回完全相同的字节，也分别保留各次生成记录。

原图下载保持原始字节不变。GIF、动态 WebP、APNG 的预览展示第一帧，动图原文件仍可下载。图片最多支持 4000 万像素，拒绝损坏文件以及 SVG 等非支持格式。

单次批量最多 100 张；ZIP 最多包含 256 MB 原图，超过时请分批下载。标签每张最多 20 个，每个最多 40 字；每张图片最多加入 30 个相册。

## 接入方式

页面位于 `pages/gallery/`，名称和描述由 `.astrbot-plugin/i18n/` 提供。后端路由由 `context.register_web_api()` 注册在 `/astrbot_plugin_ai_image/gallery/` 下。

前端使用 `window.AstrBotPluginPage.ready()`、`apiGet()`、`apiPost()`、`upload()` 和 `download()`，不访问父页面 DOM、LocalStorage 或 Dashboard Cookie。缩略图和预览通过鉴权接口返回受限大小的 Data URL；原图和 ZIP 经 Bridge 下载。管理接口要求 Dashboard 身份，不开放匿名图片目录。

数据层使用 Python 标准库 SQLite，图片处理复用已有 Pillow 依赖；文件处理在线程中执行，列表分页，缩略图按可见区域分批读取。插件卸载时清理自己注册的 API，热重载时不会误删新实例的路由。

## 测试与本地预览

数据层和生图流程回归测试：

```bash
python -m unittest discover -s tests -v
```

可选的 API 与浏览器验证依赖：

```bash
python -m pip install -r tests/requirements-web.txt
python -m playwright install chromium
```

当本机安装的 AstrBot 包含 `astrbot.api.web` 时，API 测试会使用该文件。也可将 `ASTRBOT_WEB_API` 环境变量设为 AstrBot 源码中的 `astrbot/api/web.py` 绝对路径，在独立 Python 环境中验证；缺少此模块时只跳过 Web API 测试，数据层和生图测试仍会执行。

运行带示例图片的本地预览：

```bash
python tools/preview_gallery.py --seed --bridge-sdk /path/to/AstrBot/astrbot/dashboard/plugin_page_bridge.js
```

在另一个终端执行浏览器验收：

```bash
python tools/check_gallery_browser.py --url http://127.0.0.1:8765
```

预览仅监听 `127.0.0.1`，使用独立临时图库和程序绘制的示例图片，不读写实际 AstrBot 图库，也不调用生图服务。关闭预览进程即清理示例数据。浏览器验收要求预览服务返回隔离环境标识，避免误对真实图库操作。截图与验证导出包写入 `artifacts/gallery/`。

## 设计参考

- [Immich](https://github.com/immich-app/immich)：参考相册、收藏、元数据浏览、筛选以及图片整理流程。
- [Lychee](https://github.com/LycheeOrg/Lychee)：参考轻量相册的浏览与导入体验。
- [gptGrok2api 图库](https://github.com/Menkelo/gptGrok2api/blob/main/web-vue/src/views/Gallery.vue)：参考标签、日期过滤和批量下载等 AI 图片管理需求。
- [AstrBot 插件 Pages 文档](https://github.com/AstrBotDevs/AstrBot/blob/v4.28.0/docs/zh/dev/star/guides/plugin-pages.md)：页面目录、Bridge、主题与 Web API 接入规范。

以上项目仅作为交互与功能设计参考。本插件独立实现，运行时不需要部署这些服务或加载外部前端资源。
