# AI 绘图聚合插件

### 命令
- `/生图 <提示词或预设名称> [额外提示词]` (可配置为其他命令)
  - 生成图片。示例: `/生图 一只可爱的小猫`
  - 使用预设。示例: `/生图 手办化`
  - 使用预设并附加额外提示词。示例: `/生图 手办化 蓝色头发`
  - 如果消息中包含图片、引用包含图片的消息，或@用户，将自动作为参考图进入图生图模式。
  - @用户时会获取其头像作为参考图。示例: `/生图 手办化 @用户A`
  - 支持内嵌比例指定，如：`/生图 风景画 16:9` 或 `/生图 人物肖像 竖屏`
  - 支持分辨率指定（Vertex 渠道），如：`/生图 高清壁纸 4K`

- `/动漫图`、`/海报图` (可自定义命令名称)
  - 3 个提供商槽位，可分别绑定不同提供商和模型，使用不同的命令触发。
  - 用法与 `/生图` 相同。

- `/vertex图`、`/vertex图2` (需手动启用)
  - Vertex AI 手动配置模式，独立于系统提供商。
  - 支持双指令双模型配置，适合高级用户。

### 功能特性

- ✅ **多提供商支持**：支持 Gemini/OpenAI/Vertex AI 三种 API 类型，自动识别。
- ✅ **3 槽位命令绑定**：3 个独立提供商槽位，可分别配置不同模型和命令（如 `/生图`、`/动漫图`、`/海报图`）。
- ✅ **Vertex AI 双模型**：可选手动配置 Vertex AI，支持双指令双模型独立运行（`/vertex图`、`/vertex图2`）。
- ✅ **文生图**：根据文字描述生成图片。
- ✅ **图生图**：基于参考图片（支持多张）生成新图片。
- ✅ **智能参考图**：自动识别消息、引用消息中的图片，以及通过@用户获取其头像作为参考图。
- ✅ **比例与分辨率控制**：支持 11 种宽高比（如 1:1、16:9、竖屏等），Vertex 渠道支持 1K/2K/4K 分辨率。
- ✅ **智能比例识别**：图生图时自动推断参考图比例，无需手动指定。
- ✅ **LLM 工具集成**：可在对话中由 LLM 自动调用图像生成功能（支持图生图和头像获取）。
- ✅ **全局预设系统**：使用 AstrBot 全局预设，支持快速调用，可动态管理（增/删）。
- ✅ **多 API Key 轮询**（Vertex）：Vertex 渠道支持配置多组凭证，失败或限流时自动切换。
- ✅ **失败重试**：生成失败时自动重试 3 次，提高成功率。
- ✅ **每日免费次数**：可配置每用户每日免费生图次数，00:00 自动重置，白名单用户不受限。
- ✅ **权限控制**：支持黑名单/白名单模式，可限制使用生图功能的用户或群组。
- ✅ **自定义回复**：可自定义无权限或次数用尽时的回复语，或选择静默模式。

### 配置项

#### api_config（提供商配置）

| 子配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| **provider_1** | object | - | 提供商槽位 1（默认命令 `/生图`） |
| `provider_1.id` | string | `””` | 系统提供商 ID 选择 |
| `provider_1.command` | string | `”生图”` | 触发命令名称 |
| `provider_1.default_resolution` | string | `”1K”` | 默认分辨率（仅 Vertex 渠道生效）<br>可选：`1K`、`2K`、`4K` |
| **provider_2** | object | - | 提供商槽位 2（默认命令 `/动漫图`） |
| `provider_2.id` | string | `””` | 系统提供商 ID 选择 |
| `provider_2.command` | string | `”动漫图”` | 触发命令名称 |
| `provider_2.default_resolution` | string | `”1K”` | 默认分辨率（仅 Vertex 渠道生效） |
| **provider_3** | object | - | 提供商槽位 3（默认命令 `/海报图`） |
| `provider_3.id` | string | `””` | 系统提供商 ID 选择 |
| `provider_3.command` | string | `”海报图”` | 触发命令名称 |
| `provider_3.default_resolution` | string | `”1K”` | 默认分辨率（仅 Vertex 渠道生效） |

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

#### generate_config（生图参数）

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `timeout` | int | `180` | 生图超时时间（秒），超时将返回错误 |
| `max_image_size_mb` | int | `10` | 用户上传的参考图片最大允许大小（MB） |

#### permission_config（权限配置）

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `mode` | string | `”disable”` | 权限模式<br>`disable`：不启用<br>`blacklist`：黑名单模式<br>`whitelist`：白名单模式 |
| `users` | list | `[]` | 用户黑/白名单列表（填入用户 ID，如 QQ 号） |
| `groups` | list | `[]` | 群组黑/白名单列表（填入群组 ID） |
| `no_permission_reply` | string | `”❌ 您没有权限使用此功能”` | 无权限时的回复内容 |
| `silent_on_no_permission` | bool | `false` | 无权限时是否静默（不回复） |

#### quota_config（免费次数配置）

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enable_daily_quota` | bool | `true` | 启用每日免费次数限制 |
| `daily_free_count` | int | `3` | 每人每日免费次数（00:00 自动重置） |
| `quota_exceeded_reply` | string | `”❌ 今日免费生图次数已用完，请明天再试。”` | 次数用尽时的回复内容 |

> **注意事项：**
> - 白名单用户（`permission_config.users`）不受每日次数限制。
> - Vertex 渠道的分辨率控制：白名单用户可使用 1K/2K/4K，非白名单用户强制 1K。
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

#### 指定分辨率（Vertex 渠道）
```
/vertex图 高清壁纸 4K
/vertex图 城市夜景 16:9 2K
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
[引用一张图片]
/生图 动漫风格
```

#### 使用@用户头像
```
/生图 像素艺术风格 @用户A @用户B
```

#### 多槽位命令
```
/动漫图 少女，粉色头发       # 使用 provider_2
/海报图 科幻电影海报 16:9     # 使用 provider_3
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
> 支持的分辨率（仅 Vertex 渠道）：`1K`、`2K`、`4K`

**💡 预设联动**：可对接全局预设 [astrbot_plugin_preset_hub](https://github.com/Menkelo/astrbot_plugin_preset_hub)，统一管理预设库。