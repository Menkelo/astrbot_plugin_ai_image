# GPT Image 2.5 适配

官方当前图像生成指南以 GPT Image 2.5 为主要示例，提供两个模型。中转站是否提供这些模型、别名映射到哪个后端，需要以该站模型列表和说明为准。

| 官方模型 ID | 官方定位 |
| --- | --- |
| `gpt-image-2.5-sunburst` | 更看重编辑精度的图生图和修图工作 |
| `gpt-image-2.5-flare` | 快速、日常的高质量图片生成 |

在 AstrBot 系统提供商中选择服务实际提供的模型，然后让插件的主/备用槽位使用该提供商。插件保持配置的模型 ID 原样发送；例如中转站使用 `gpt-image-2.5` 作为别名时，也会透传该名称，其是否可用取决于该站。

## 本次适配

| 参数 | GPT Image 2.5 的处理 |
| --- | --- |
| `size` | 根据比例与 1K/2K/4K 档位选择合法 `WIDTHxHEIGHT`，包括任意参考图比例；识别 Sunburst / Flare 和 2.5 家族别名 |
| `quality` | 支持 `auto`、`low`、`medium`、`high`、`xhigh`、`max`；默认 auto 时省略字段，使用模型默认值 |
| `background` | 支持 `auto`、`opaque`、`transparent`；默认 auto 时省略字段 |
| `output_format` | 固定请求 `png`，与插件的无损 PNG 输出流程一致，并支持透明背景 |
| `response_format` | 2.5 请求中省略；该接口原生返回 base64 图片数据，文件编码由 `output_format` 控制 |
| `input_fidelity` | 2.5 请求中省略，沿用当前原生图像接口参数约定 |
| `output_compression` | PNG 流程无需发送该参数；它用于 JPEG/WebP 的输出压缩 |

2.5 的自定义尺寸约束与 GPT Image 2 一致：两边为 16 的倍数，单边最多 3840，长短边比最多 3:1，总像素为 655,360–8,294,400。官方将超过 2560×1440 的分辨率标记为实验性能力。

例如 2K 16:9 请求 `2048x1152`，4K 16:9 请求 `3840x2160`。接口请求尺寸与本地目标比例继续分开保存；上游返回比例不符时，沿用[保留完整内容并补边](IMAGE_RATIO.md)的策略。实际透明图的补边保留透明度。

## 配置

在插件配置的「生图参数」中设置：

| 配置键 | 默认 | 说明 |
| --- | --- | --- |
| `generate_config.gpt_image_quality` | `auto` | GPT Image 2.5 质量档位；更高档位通常需要更多时间和费用 |
| `generate_config.gpt_image_background` | `auto` | GPT Image 2.5 背景类型 |

两项配置仅应用于 GPT Image 2.5 的原生图像接口，同时覆盖文生图与图生图。其他模型，包括备用的 GPT Image 2、GPT Image 1 或 Gemini/Vertex，维持原有参数。更改配置后重载插件。

日常使用可保持 `auto`；需要比较精细效果时可尝试 `high`、`xhigh`，再根据效果和耗时考虑 `max`。这些档位控制模型的渲染质量，1K/2K/4K 则控制请求尺寸，两者可以分别选择。

透明素材的配置示例：

```json
{
  "generate_config": {
    "gpt_image_quality": "xhigh",
    "gpt_image_background": "transparent"
  }
}
```

## 中转兼容与验证范围

明确的 `size` 不兼容错误仍允许降级一次，只移除尺寸字段，保留已经选择的质量、背景和 PNG 格式。服务明确拒绝质量、背景或输出格式时，插件会提示对应选项不兼容，供你调整设置；不会仅为这些参数错误反复请求。

默认 auto 的质量、背景字段不额外发送，避免无意固定到高档位。中转站是否完整支持官方参数，需要通过该站实际能力确认。2.5 模型名称相似并不能证明不同服务的参数透传完全一致。

专项回归使用官方文档约束和离线 HTTP 录制替身，检查真实 JSON/multipart 编码、模型 ID 透传、原生尺寸、质量档位、透明度、参数拒绝与备用模型隔离：

```bash
python -m unittest tests.test_gpt_image_25 tests.test_generation_gallery -v
```

验证不调用付费生图接口，不代表对任何特定中转站进行过线上质量或计费评测。

## 官方依据

- [图像生成指南：尺寸、质量及透明背景](https://developers.openai.com/api/docs/guides/image-generation#size-and-quality-options)
- [GPT Image 2.5 Sunburst](https://developers.openai.com/api/docs/models/gpt-image-2.5-sunburst)
- [GPT Image 2.5 Flare](https://developers.openai.com/api/docs/models/gpt-image-2.5-flare)
- [图像编辑 API 参数](https://developers.openai.com/api/docs/api-reference/images/createEdit)
