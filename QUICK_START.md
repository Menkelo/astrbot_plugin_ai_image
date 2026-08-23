# 快速配置指南

## 方式一：使用系统提供商（推荐）

1. 在 AstrBot 配置页面添加提供商
2. 在插件配置中选择已添加的提供商 ID
3. 配置完成，发送 `/生图 测试图片` 试试

## 方式二：Vertex AI 手动配置

适合需要使用 Vertex AI 或需要更精细控制的高级用户。

### 步骤：

1. 启用 `vertex_manual_config.enabled`
2. 填写 `keys` 列表，格式：`API_KEY|PROJECT_ID`
3. 配置两个模型槽位的命令和模型名
4. 发送 `/vertex图 测试图片` 试试

### 示例配置：

```json
{
  "vertex_manual_config": {
    "enabled": true,
    "base_url": "https://aiplatform.googleapis.com",
    "api_version": "v1beta1",
    "location": "global",
    "keys": [
      "your-api-key-1|your-project-id-1",
      "your-api-key-2|your-project-id-2"
    ],
    "vertex_1": {
      "command": "vertex图",
      "model": "gemini-3-pro-image-preview",
      "default_resolution": "2K"
    },
    "vertex_2": {
      "command": "vertex图2",
      "model": "gemini-2.5-flash-image-preview",
      "default_resolution": "1K"
    }
  }
}
```

## 方式三：Gemini 手动配置（官方/中转）

适合直接使用 Gemini `generateContent` 接口（官方 API 或 Bearer 鉴权中转站，如 `https://meinianda.top/v1beta`）。

### 步骤：

1. 启用 `gemini_manual_config.enabled`
2. `base_url` 填官方地址或中转地址（以 `/v1beta` 结尾）
3. 填写 `keys` 列表，格式：`API_KEY`（多 Key 自动轮询）
4. 模型可留空（`auto`）：自动从接口获取生图模型，`gemini图` 优先 flash 系、`gemini图2` 优先 pro 系；插件加载后配置页的模型项也会自动变为下拉列表，可直接选择
5. 发送 `/gemini图 测试图片` 试试

### 示例配置：

```json
{
  "gemini_manual_config": {
    "enabled": true,
    "base_url": "https://meinianda.top/v1beta",
    "keys": [
      "your-api-key-1",
      "your-api-key-2"
    ],
    "gemini_1": {
      "command": "gemini图",
      "model": "auto",
      "default_resolution": "1K"
    },
    "gemini_2": {
      "command": "gemini图2",
      "model": "auto",
      "default_resolution": "2K"
    }
  }
}
```

## 权限配置示例

### 白名单模式（仅特定用户可用）

```json
{
  "permission_config": {
    "mode": "whitelist",
    "users": ["123456789", "987654321"],
    "groups": ["111111111"]
  }
}
```

### 黑名单模式（禁止特定用户）

```json
{
  "permission_config": {
    "mode": "blacklist",
    "users": ["999999999"],
    "no_permission_reply": "❌ 你已被禁止使用生图功能"
  }
}
```

## 每日配额配置

```json
{
  "quota_config": {
    "enable_daily_quota": true,
    "daily_free_count": 5,
    "quota_exceeded_reply": "今日次数已用完，明天再来吧～"
  }
}
```

> 💡 白名单用户与机器人管理员不受配额限制；生成失败不会扣除次数。

## 常用命令速查

| 命令 | 说明 |
| --- | --- |
| `/生图 <提示词>` | 基础生图 |
| `/生图 <提示词> 16:9` | 指定比例 |
| `/生图 <提示词> 4K` | 指定分辨率 |
| `/生图 预设名` | 使用预设 |
| `/生图 预设名 额外内容` | 预设+附加词 |
| `[引用图片] /生图 <提示词>` | 图生图 |
| `/生图 <提示词> @用户` | 使用头像 |
| `/vertex图 <提示词>` | Vertex 手动模型1（需启用） |
| `/vertex图2 <提示词>` | Vertex 手动模型2（需启用） |
| `/gemini图 <提示词>` | Gemini 手动模型1（需启用，模型可自动获取） |
| `/gemini图2 <提示词>` | Gemini 手动模型2（需启用，模型可自动获取） |

## 支持的比例

`1:1`, `2:3`, `3:2`, `3:4`, `4:3`, `4:5`, `5:4`, `9:16`, `16:9`, `21:9`

或使用关键词：`横屏`/`横版`/`landscape` → 16:9，`竖屏`/`竖版`/`portrait` → 9:16

> 可在插件配置面板为提供商槽位（provider_1）设置 `default_aspect_ratio` 默认比例；指令内嵌比例（如 `/生图 风景 16:9`）优先于默认比例。

## 支持的分辨率

`1K`, `2K`, `4K`

> Vertex 渠道：白名单用户可用全部档位，普通用户限制 1K。Gemini/OpenAI 渠道所有用户均可指定。
