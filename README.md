# AI 绘图聚合插件

多模型 AI 图像生成插件，支持 Gemini/OpenAI/Vertex AI 三种 API 类型，提供灵活的槽位命令绑定，支持文生图、图生图及智能参考图识别。

[更新日志](CHANGELOG.md) · [比例处理说明与版本复盘](docs/IMAGE_RATIO.md) · [GPT Image 2.5 适配](docs/GPT_IMAGE_25.md)

## 快速开始

1. 在 AstrBot 插件市场搜索「AI 绘图聚合」并安装
2. 配置页面选择提供商（或手动配置 API）
3. 发送 `/生图 一只可爱的猫咪` 开始使用
4. 使用支持插件 Pages 的 AstrBot（已按 **v4.28.0** 验证），在插件详情页打开 **「图片管理」**，浏览和整理生成结果。

## 图片管理（Canvas Page）

v1.8.0 新增「映像」图库，在 AstrBot WebUI 内直接使用，自动跟随亮暗主题，也支持手机布局。

- **自动归档**：保存启用后的生成原图、提示词、实际出图模型、创作者、尺寸与生成耗时；主接口切换备用接口时记录实际成功的提供商。
- **浏览与查找**：瀑布流、紧凑布局、大图预览；搜索提示词、名称、标签、备注和创作者，按模型、来源、日期及画面方向筛选。
- **整理作品**：收藏、多个相册、标签、作品名称和备注；批量收藏、添加标签、加入或移出相册。
- **导入与导出**：选择或拖入 PNG/JPEG/WebP/GIF，重复导入自动识别；下载原图或批量导出 ZIP（包含原图和 `metadata.json`）。
- **回收站**：普通删除可以恢复，只有手动永久删除才清理文件；删除相册会保留其中的图片。
- **复用提示词**：复制提示词或生成命令；图生图会提示需要重新附上参考图。

入口：**AstrBot WebUI → 插件 → AI 绘图聚合 → 图片管理**。更新插件后重载一次，让 AstrBot 扫描新的 `pages/gallery/index.html`。

图库默认开启，数据保存在 `data/plugin_data/astrbot_plugin_ai_image/gallery/`。旧版本只保存过临时图片，已有历史不会自动补入；仍保留在本地的旧图可以手动导入。图库仅供已登录的 WebUI 管理，图片不会生成公开访问链接。

更多配置、备份方法与测试方式见 [图片管理说明](docs/GALLERY.md)。交互参考了 [Immich](https://github.com/immich-app/immich)、[Lychee](https://github.com/LycheeOrg/Lychee) 和 [gptGrok2api](https://github.com/Menkelo/gptGrok2api)，无需部署这些服务。

## 命令
- `/生图 <提示词或预设名称> [额外提示词]` (可配置为其他命令)
  - 生成图片。示例: `/生图 一只可爱的小猫`
  - 使用预设。示例: `/生图 手办化`
  - 使用预设并附加额外提示词。示例: `/生图 手办化 蓝色头发`
  - 如果消息中包含图片、群文件图片、引用包含图片的消息，或@用户，将自动作为参考图进入图生图模式。
  - @用户时会获取其头像作为参考图。示例: `/生图 手办化 @用户A`
  - 支持内嵌比例指定，如：`/生图 风景画 16:9` 或 `/生图 人物肖像 竖屏`
  - 支持分辨率指定（Vertex 渠道），如：`/生图 高清壁纸 4K`

- `/vertex图`、`/vertex图2` (需手动启用)
  - Vertex AI 手动配置模式，独立于系统提供商。
  - 支持双指令双模型配置，适合高级用户。

- `/gemini图`、`/gemini图2` (需手动启用)
  - Gemini 手动配置模式，独立于系统提供商，支持官方接口与 `Authorization: Bearer` 鉴权的 Gemini 兼容中转（如 `https://meinianda.top/v1beta`）。
  - 支持双指令双模型配置与多 Key 自动轮询。
  - **模型自动获取**：模型留空或填 `auto` 时自动从中转接口拉取模型列表并选型（`gemini图` 优先 flash 系、`gemini图2` 优先 pro 系），无需手动填写模型名。
  - 插件加载时会拉取接口返回的生图模型并注入配置页，模型配置项自动变为下拉列表（拉取失败保持手填，不影响使用）。

### 功能特性

- ✅ **多提供商支持**：支持 Gemini/OpenAI/Vertex AI 三种 API 类型，自动识别。
- ✅ **槽位命令绑定**：提供商槽位可自定义触发命令（默认 `/生图`）。
- ✅ **模型自动获取**（Gemini 手动渠道）：模型留空或 `auto` 时自动调用 ListModels 接口拉取生图模型并按槽位选型（flash/pro），获取失败回退默认模型；插件加载时自动将模型列表注入配置页下拉选项。
- ✅ **Vertex AI 双模型**：可选手动配置 Vertex AI，支持双指令双模型独立运行（`/vertex图`、`/vertex图2`）。
- ✅ **Gemini 手动双模型**：可选手动配置 Gemini `generateContent` 接口（官方或 Bearer 鉴权中转），支持双指令双模型（`/gemini图`、`/gemini图2`）与多 Key 轮询；中转站不识别可选 `generationConfig` 时自动降级重试，结果保留接口返回画幅，分辨率通过等比放大兜底。
- ✅ **文生图**：根据文字描述生成图片。
- ✅ **图生图**：基于参考图片（支持多张）生成新图片。
- ✅ **智能参考图**：自动识别消息、引用消息中的图片、群文件图片，以及通过@用户获取其头像作为参考图。
- ✅ **比例与分辨率控制**：支持 11 种指令比例与 1K/2K/4K 档位。GPT Image 2 / 2.5 按尺寸约束选择合法请求（如 2K 16:9=`2048x1152`），较早 GPT Image 模型使用其支持的固定尺寸；只有明确的尺寸参数错误才降级重试。
- ✅ **GPT Image 2.5**：支持 Sunburst / Flare 的原生尺寸、`xhigh/max` 质量档位及透明 PNG 背景，默认质量与背景均为 `auto`；通过系统提供商选择实际模型。
- ✅ **保留完整画面**：优先采用指令/预设比例，否则以第一张参考图应用 EXIF 方向后的实际比例为目标，其他参考图只作为辅助。输入保持原画幅；Gemini/Vertex 保留接口返回画幅，避免因原生尺寸与标称比例略有偏差而补出白边；OpenAI/GPT 返回比例不符时等比适配并补边。各渠道均不主动裁剪内容，备用接口按实际成功渠道处理。
- ✅ **全局预设系统**：使用 AstrBot 全局预设，支持快速调用，可动态管理（增/删）。
- ✅ **多 API Key 轮询**（Vertex）：Vertex 渠道支持配置多组凭证，失败或限流时自动切换。
- ✅ **备用提供商**：主提供商失败一次后立即请求备用提供商；`400`/`413`/`415`/`422` 及内容拦截直接返回，不切换、不重试。`401`/`403`/`404` 会立刻切备用（适合两个不同中转站）。
- ✅ **每日免费次数**：可配置每用户每日免费生图次数，00:00 自动重置；供应商渠道白名单用户/群组及管理员不受限，手动渠道（Gemini/Vertex）仅白名单人员及管理员不受限，生成失败不计入次数。
- ✅ **权限控制**：支持黑名单/白名单模式，可限制使用生图功能的用户或群组。`whitelist` 模式下手动渠道（Gemini/Vertex）仅白名单人员或白名单群组成员可用。
- ✅ **自定义回复**：可自定义无权限或次数用尽时的回复语，或选择静默模式。

### 配置项

#### api_config（提供商配置，主接口 + 备用接口）

| 子配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| **provider_1** | object | - | 主提供商槽位（默认命令 `/生图`） |
| `provider_1.id` | string | `””` | 系统提供商 ID 选择 |
| `provider_1.command` | string | `”生图”` | 触发命令名称 |
| `provider_1.default_resolution` | string | `”1K”` | 默认分辨率<br>可选：`1K`、`2K`、`4K` |
| **provider_backup** | object | - | 备用提供商（同一 `/生图` 命令） |
| `provider_backup.id` | string | `””` | 主提供商失败一次后立即请求此接口<br>`400`/`413`/`415`/`422` 及内容拦截不切换；`401`/`403`/`404` 会立刻切备用 |

#### vertex_manual_config（Vertex AI 手动配置）

| 子配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | 启用 Vertex AI 手动配置（不走系统提供商） |
| `base_url` | string | `”https://aiplatform.googleapis.com”` | Vertex API Base URL |
| `api_version` | string | `”v1beta1”` | API 版本（建议 v1beta1 或 v1） |
| `location` | string | `”global”` | 区域（如 global、us-central1） |
| `keys` | list | `[]` | Vertex Keys 列表<br>格式：`[“API_KEY\|PROJECT_ID”, ...]` |
| **vertex_1** | object | - | Vertex 模型槽位 1 |
| `vertex_1.command` | string | `”vertex图”` | 触发命令名称 |
| `vertex_1.model` | string | `”gemini-3-pro-image-preview”` | 模型 ID |
| `vertex_1.default_resolution` | string | `”1K”` | 默认分辨率（1K/2K/4K） |
| **vertex_2** | object | - | Vertex 模型槽位 2 |
| `vertex_2.command` | string | `”vertex图2”` | 触发命令名称 |
| `vertex_2.model` | string | `”gemini-2.5-flash-image-preview”` | 模型 ID |
| `vertex_2.default_resolution` | string | `”1K”` | 默认分辨率（1K/2K/4K） |

#### gemini_manual_config（Gemini 手动配置）

支持 Gemini `generateContent` 原生接口（官方地址或 Bearer 鉴权中转均可，如 `https://meinianda.top/v1beta`）。

| 子配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | 启用 Gemini 手动配置（不走系统提供商） |
| `base_url` | string | `”https://generativelanguage.googleapis.com”` | Gemini API Base URL<br>中转地址以 `/v1beta` 结尾，如 `”https://meinianda.top/v1beta”` |
| `keys` | list | `[]` | Gemini Keys 列表<br>格式：`[“API_KEY”, ...]`（Bearer 鉴权，多 Key 自动轮询） |
| **gemini_1** | object | - | Gemini 模型槽位 1 |
| `gemini_1.command` | string | `”gemini图”` | 触发命令名称 |
| `gemini_1.model` | string | `”auto”` | 模型 ID<br>留空或 `”auto”`：自动从接口获取，优先选择 flash 系生图模型 |
| `gemini_1.default_resolution` | string | `”1K”` | 默认分辨率（1K/2K/4K） |
| **gemini_2** | object | - | Gemini 模型槽位 2 |
| `gemini_2.command` | string | `”gemini图2”` | 触发命令名称 |
| `gemini_2.model` | string | `”auto”` | 模型 ID<br>留空或 `”auto”`：自动从接口获取，优先选择 pro 系生图模型 |
| `gemini_2.default_resolution` | string | `”1K”` | 默认分辨率（1K/2K/4K） |

> 请求走 `POST {base_url}/models/{model}:generateContent`，携带 `Authorization: Bearer {API Key}`（官方域名使用 `x-goog-api-key`）；`contents[].parts[]` 传提示词与参考图，响应从 `candidates[].content.parts[].inlineData` 提取 Base64 图片。

#### generate_config（生图参数）

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `timeout` | int | `180` | 生图超时时间（秒），超时将返回错误 |
| `max_image_size_mb` | int | `10` | 用户上传的参考图片最大允许大小（MB） |
| `gpt_image_quality` | string | `auto` | GPT Image 2.5 的质量，可选 `auto`、`low`、`medium`、`high`、`xhigh`、`max`；较高档位通常增加生成时间与费用 |
| `gpt_image_background` | string | `auto` | GPT Image 2.5 的背景，可选 `auto`、`opaque`、`transparent`，输出使用 PNG |

以上两个 GPT Image 选项仅用于 2.5 图像接口；主/备用提供商使用其他模型时维持该模型原有参数。配置更改后重载插件。模型 ID、选择建议与中转兼容说明见 [GPT Image 2.5 适配](docs/GPT_IMAGE_25.md)。

#### permission_config（权限配置）

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `mode` | string | `”disable”` | 权限模式<br>`disable`：不启用<br>`blacklist`：黑名单模式<br>`whitelist`：白名单模式 |
| `users` | list | `[]` | 用户黑/白名单列表（填入用户 ID，如 QQ 号）<br>白名单模式下，机器人管理员自动视为白名单，无需手动填入 |
| `groups` | list | `[]` | 群组黑/白名单列表（填入群组 ID） |
| `no_permission_reply` | string | `”❌您没有权限使用此功能”` | 无权限时的回复内容 |
| `silent_on_no_permission` | bool | `false` | 无权限时是否静默（不回复） |

#### quota_config（免费次数配置）

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enable_daily_quota` | bool | `true` | 启用每日免费次数限制 |
| `daily_free_count` | int | `3` | 每人每日免费次数（00:00 自动重置） |
| `quota_exceeded_reply` | string | `”❌今日免费生图次数已用完，请明天再试”` | 次数用尽时的回复内容 |

> **注意事项：**
> - 机器人管理员（AstrBot 全局配置中的管理员）自动等同白名单用户，无需手动将其 QQ 添加到 `permission_config.users`。
> - 每日次数豁免规则按渠道区分：供应商渠道下白名单用户（`permission_config.users`）及白名单群组成员均不受限；手动渠道（Gemini/Vertex，`/gemini图`、`/vertex图` 等）仅白名单人员不受限，位于白名单群但不在白名单人员列表中的用户仍受每日次数限制。
> - 手动渠道使用门槛：`whitelist` 模式下，手动渠道（Gemini/Vertex）仅白名单人员或白名单群组成员可使用，其余用户会被拒绝；`disable`/`blacklist` 模式无此限制。
> - 分辨率控制（1K/2K/4K）：Gemini/OpenAI 渠道全部用户均可指定；Vertex 渠道仅在 `whitelist` 模式下限制非白名单用户强制 1K，`disable`/`blacklist` 模式对全部用户开放 1K/2K/4K。
> - 插件会自动识别提供商的 API 类型（Gemini/OpenAI/Vertex），无需手动指定。

### 使用示例

#### 基础用法
```
/生图 一只穿着宇航服的猫在月球上
```

#### 指定比例
```
/生图 风景画 16:9
/生图 人物肖像 竖屏
/生图 壁纸 21:9
```

#### 指定分辨率
```
/生图 高清壁纸 4K
/生图 城市夜景 16:9 2K
```

#### 使用预设
```
/生图 手办化
```

#### 使用预设并附加内容
```
/生图 手办化 蓝色头发，微笑
```

#### 图生图（引用消息）
```
[引用一张图片或群文件图片]
/生图 动漫风格
```

#### 使用@用户头像
```
/生图 像素艺术风格 @用户A @用户B
```

#### 手动渠道命令
```
/gemini图 少女，粉色头发       # Gemini 手动模型1（自动选型，优先 flash 系）
/gemini图2 科幻电影海报 16:9   # Gemini 手动模型2（自动选型，优先 pro 系）
/vertex图 高清壁纸 4K          # Vertex 手动模型1
```

#### 预设管理（使用 AstrBot 全局预设系统）

参考 AstrBot 文档配置全局预设，支持基础格式和 JSON 格式：

**基础格式**
```
预设名:提示词
```

**JSON 格式**（支持指定比例和分辨率）
```json
预设名:{"prompt": "提示词", "aspect_ratio": "16:9", "resolution": "2K"}
```

> 支持的比例：`1:1`、`2:3`、`3:2`、`3:4`、`4:3`、`4:5`、`5:4`、`9:16`、`16:9`、`21:9`  
> 支持的分辨率：`1K`、`2K`、`4K`

**💡 预设联动**：可对接全局预设 [astrbot_plugin_preset_hub](https://github.com/Menkelo/astrbot_plugin_preset_hub)，统一管理预设库。

## 常见问题

### Q: 支持哪些 AI 模型？
A: 支持所有兼容 Gemini/OpenAI/Vertex AI 接口的模型，插件会自动识别 API 类型。

### Q: 如何配置多个模型？
A: 启用 Gemini 或 Vertex 手动配置即可使用双指令双模型；Gemini 渠道模型可留空（`auto`）自动获取，无需手动填写。也可在提供商槽位绑定系统提供商使用其模型。

### Q: 生成失败怎么办？
A: 主提供商失败一次后会立即请求备用提供商（需配置 `provider_backup`）。`400`/`413`/`415`/`422` 及内容拦截会直接返回，不切换、不重试。`401`/`403`/`404` 会立刻切备用。失败后会显示分类后的错误原因及脱敏详情（如「模型不存在」「API Key 无效」「配额或余额不足」等，URL/Key/Token 会被隐藏），完整原始错误会记录到插件日志。检查 API Key 是否有效、配额是否充足、模型名是否正确。

### Q: 白名单用户有什么特权？
A: 白名单用户不受每日免费次数限制。在 `whitelist` 权限模式下：供应商渠道的白名单群组成员同样免次数，但手动渠道（Gemini/Vertex）仅白名单人员免次数，位于白名单群但非白名单人员的用户使用 `/gemini图`、`/vertex图` 等手动命令时仍受每日次数限制；且手动渠道仅白名单人员或白名单群组成员可用，白名单范围以外的用户会被拒绝。分辨率方面：Vertex 渠道且权限模式为 `whitelist` 时白名单用户可使用 2K/4K（非白名单限制 1K），`disable`/`blacklist` 模式下 Vertex 渠道所有用户均可指定 1K/2K/4K；Gemini/OpenAI 渠道所有用户均可指定 1K/2K/4K。机器人管理员（AstrBot 全局管理员）自动享有上述特权，无需手动添加 QQ 到白名单。

### Q: 如何获取用户头像作为参考图？
A: 在命令中 @ 用户即可，如：`/生图 手办化 @用户A`

### Q: 为什么新图片可能出现补边？
A: OpenAI/GPT 返回比例与目标不一致时，插件保留完整画面，通过补边调整比例；原图有实际透明区域时补透明边，否则补白边。Gemini/Vertex 会向接口传递指定比例，但返回像素可能只是近似该比例，因此保留接口返回画幅，不再本地补边，也不裁剪模型本身生成的边缘。分辨率档位仍控制请求分辨率与最小输出长边。“跟随参考图比例”不代表强制输出与参考图完全相同的像素数，Gemini/Vertex 的最终比例以接口返回为准。详见 [比例处理说明](docs/IMAGE_RATIO.md)。

## 贡献与支持

- 项目地址：[GitHub](https://github.com/Menkelo/astrbot_plugin_ai_image)
- 问题反馈：[Issues](https://github.com/Menkelo/astrbot_plugin_ai_image/issues)
- 许可协议：AGPL-3.0

## 致谢

感谢 AstrBot 框架提供的强大插件系统支持。
