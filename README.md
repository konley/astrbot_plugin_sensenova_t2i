# SenseNova 文生图

AstrBot 插件 — 调用商汤日日新 SenseNova U1 Fast 模型生成图片。

## 功能

- **文生图** — 输入文字描述，AI 生成图片
- **Prompt 增强** — 调用 AstrBot 主 Provider 对简短描述自动扩写优化
- **11 种尺寸** — 支持 1:1、16:9、9:16 等比例简写或完整像素值
- **多图生成** — 一次生成 1-4 张图片
- **信息图模式** — 自动追加信息图结构化模板，适合海报/图表场景
- **自定义触发词** — WebUI 可配置触发关键词
- **忽略 / 前缀** — 可配置是否忽略指令前的 `/` 或 `#`

## 安装

在 AstrBot WebUI 插件管理中输入仓库地址安装：

```
https://github.com/konley/astrbot_plugin_sensenova_t2i
```

## 配置

安装后在 WebUI 插件配置页面填写以下必要项：

| 配置项 | 必填 | 说明 |
|--------|------|------|
| `api_key` | 是 | SenseNova API Key，在 [platform.sensenova.cn](https://platform.sensenova.cn) 控制台创建 |
| 其他 | 否 | 均有默认值，开箱即用 |

### 全部配置项

#### 基础配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `api_key` | string | `""` | SenseNova API Key（必填） |
| `ignore_slash` | bool | `true` | 忽略指令前的 `/` 或 `#` 前缀 |
| `command_keywords` | list | `["画"]` | 触发关键词列表 |
| `cooldown` | int | `10` | 用户冷却时间（秒） |

#### 图片配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `default_size` | string | `2752x1536` | 默认尺寸（16:9 横版海报） |
| `default_n` | int | `1` | 默认生成数量（1-4） |
| `timeout` | int | `180` | API 超时时间（秒） |

#### Prompt 增强

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enable_prompt_enhance` | bool | `true` | 启用主 Provider Prompt 增强 |
| `enhance_system_prompt` | text | `""` | 增强用系统提示词（留空使用内置默认） |
| `enhance_max_tokens` | int | `2048` | 增强模型最大 token |

#### 默认参数模板

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `default_style_suffix` | text | `""` | 自动追加到 prompt 末尾的风格后缀 |
| `infographic_mode` | bool | `false` | 信息图模式：自动追加结构化模板 |

#### 运维配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `cleanup_delay` | int | `10` | 发送后清理图片延迟（秒） |
| `max_retry` | int | `2` | 失败重试次数 |
| `rate_limit_cooldown` | int | `5` | 429 限流冷却时间（秒） |

## 使用

### 基本用法

```
画 一只可爱的猫咪坐在彩虹上
```

### 指定尺寸

```
画 --size 1:1 卡通头像
画 --size 9:16 手机壁纸：星空下的城市
画 --size 2048x2048 正方形插画
```

### 多图生成

```
画 --n 2 赛博朋克城市夜景
```

### 信息图

```
画 信息图：2026年AI发展趋势，三列布局，深蓝科技风
```

### 帮助

```
画帮助
```

### 支持的尺寸

| 比例简写 | 像素值 | 说明 |
|----------|--------|------|
| `1:1` | `2048x2048` | 正方形，插画/头像 |
| `16:9` | `2752x1536` | 横版海报（默认） |
| `9:16` | `1536x2752` | 竖版海报/手机壁纸 |
| `3:2` | `2496x1664` | 横版经典 |
| `2:3` | `1664x2496` | 竖版经典 |
| `4:3` | `2368x1760` | 横版传统 |
| `3:4` | `1760x2368` | 竖版传统 |
| `5:4` | `2272x1824` | 接近正方 |
| `4:5` | `1824x2272` | 社交媒体 |
| `21:9` | `3072x1376` | 超宽横幅 |
| `9:21` | `1344x3136` | 超长竖图 |

## Prompt 增强说明

开启 Prompt 增强后，用户输入的简短描述会先经过 AstrBot 主 Provider 扩写优化，再传给 U1 生成图片。

```
用户输入: "画 一只猫"
         ↓
主 Provider 扩写: "一只毛茸茸的橘猫慵懒地趴在窗台上，午后阳光透过纱帘洒落，
                  金色光线勾勒出猫的轮廓，背景是模糊的绿植和书架，温馨治愈风格"
         ↓
U1 生成图片
```

- 需要在 AstrBot 中配置至少一个大模型 Provider
- 可在 WebUI 中自定义增强用系统提示词
- 关闭后直接使用用户原始描述

## 关于 SenseNova U1

- **模型**: `sensenova-u1-fast` — 商汤日日新 U1 快速推理版本
- **能力**: 文生图、信息图生成、交错图文生成
- **费用**: 公测期间免费（每 5 小时 1500 次调用额度）
- **平台**: [platform.sensenova.cn](https://platform.sensenova.cn)
- **开源仓库**: [github.com/OpenSenseNova/SenseNova-U1](https://github.com/OpenSenseNova/SenseNova-U1)

## 技术细节

- 异步 HTTP 调用（aiohttp），不阻塞事件循环
- 临时文件存放在 `data/plugin_data/sensenova_t2i/`，发送后自动清理
- API Key 从配置读取，不硬编码
- 完整错误处理：限流重试、超时处理、网络异常友好提示
- 启动时自动清理残留临时文件

## 适配器支持

aiocqhttp / qq_official / telegram / wecom / lark / discord

## License

MIT
