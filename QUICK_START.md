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

> 💡 白名单用户不受配额限制

## 常用命令速查

| 命令 | 说明 |
| --- | --- |
| `/生图 <提示词>` | 基础生图 |
| `/生图 <提示词> 16:9` | 指定比例 |
| `/生图 <提示词> 4K` | 指定分辨率（Vertex） |
| `/生图 预设名` | 使用预设 |
| `/生图 预设名 额外内容` | 预设+附加词 |
| `[引用图片] /生图 <提示词>` | 图生图 |
| `/生图 <提示词> @用户` | 使用头像 |

## 支持的比例

`1:1`, `2:3`, `3:2`, `3:4`, `4:3`, `4:5`, `5:4`, `9:16`, `16:9`, `21:9`

或使用关键词：`横屏`/`横版`/`landscape` → 16:9，`竖屏`/`竖版`/`portrait` → 9:16

## 支持的分辨率（仅 Vertex）

`1K`, `2K`, `4K`（白名单用户可用全部，普通用户限制 1K）
