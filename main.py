"""SenseNova U1 文生图插件

调用商汤日日新 sensenova-u1-fast 模型生成图片。
支持双模式（通用日常 / 信息图）、Prompt 增强、11 种尺寸、多图生成。
模式可通过 WebUI 配置或指令切换。
"""

import asyncio
import os
import re
import time
import uuid

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

# ── 常量 ──────────────────────────────────────────────────

API_BASE = "https://token.sensenova.cn/v1"
IMAGE_ENDPOINT = f"{API_BASE}/images/generations"

# U1 Fast 支持的 11 种固定分辨率
SIZE_MAP = {
    "1:1": "2048x2048",
    "16:9": "2752x1536",
    "9:16": "1536x2752",
    "3:2": "2496x1664",
    "2:3": "1664x2496",
    "4:3": "2368x1760",
    "3:4": "1760x2368",
    "5:4": "2272x1824",
    "4:5": "1824x2272",
    "21:9": "3072x1376",
    "9:21": "1344x3136",
}

ALL_SIZES = sorted(set(SIZE_MAP.values()))

# ── 双模式定义 ────────────────────────────────────────────

MODE_DAILY = "daily"
MODE_INFOGRAPHIC = "infographic"
VALID_MODES = {MODE_DAILY, MODE_INFOGRAPHIC}

MODE_NAMES = {
    MODE_DAILY: "通用日常",
    MODE_INFOGRAPHIC: "信息图",
}

# 通用日常模式 — 内置 Prompt 增强系统提示词（万能大师）
DEFAULT_DAILY_ENHANCE_PROMPT = (
    "你是一个顶级 AI 绘画提示词工程师。将用户的简短描述扩写为高质量图像生成提示词。\n"
    "规则：\n"
    "1. 分析用户内容，自动判断最适合的风格（写实/插画/信息图/概念艺术等）\n"
    "2. 保留用户原始意图\n"
    "3. 补充：主体细节、构图布局、光影配色、画面氛围、艺术风格\n"
    "4. 信息图/海报场景：补充布局结构、分区、配色逻辑\n"
    "5. 写实场景：补充镜头、光线、材质、景深\n"
    "6. 插画场景：补充画风、配色、氛围\n"
    "7. 用中文输出，不要解释，直接输出提示词\n"
    "8. 不要使用 Markdown 格式\n"
    "9. 200-300字"
)

DEFAULT_DAILY_STYLE_SUFFIX = "高质量，精细细节，专业构图，画面锐利清晰"

# 信息图模式 — 内置 Prompt 增强系统提示词
DEFAULT_INFOGRAPHIC_ENHANCE_PROMPT = (
    "你是一个专业的信息图设计师和 AI 绘画提示词工程师。"
    "将用户的简短描述扩写为信息图/海报风格的图像生成提示词。\n"
    "扩写要求：\n"
    "1. 分析用户内容，自动设计布局结构（三列网格/时间线/对比布局/流程图等）\n"
    "2. 明确描述每个区域的内容、位置和视觉层级\n"
    "3. 补充配色方案：主色、辅色、强调色\n"
    "4. 补充视觉元素：图标、装饰线、背景纹理\n"
    "5. 补充字体风格描述：标题用粗体无衬线，正文用等宽字体等\n"
    "6. 描述整体视觉风格：科技感/扁平化/商务风/活泼风\n"
    "7. 用中文输出，不要解释，直接输出提示词\n"
    "8. 不要使用 Markdown 格式\n"
    "9. 控制在 300 字以内，确保布局描述清晰可执行"
)

DEFAULT_INFOGRAPHIC_STYLE_SUFFIX = "排版精准对齐，文字清晰可读，视觉层次分明"

INFOGRAPHIC_PREFIX = (
    "请以信息图（Infographic）的风格生成以下内容，"
    "要求排版清晰、层次分明、文字精准可读：\n"
)

HELP_TEXT = (
    "━━━ SenseNova 文生图 ━━━\n"
    "用法: 画 <描述> [选项]\n\n"
    "选项:\n"
    "  --size <尺寸>   指定图片尺寸（默认 16:9）\n"
    "                  比例简写: 1:1 16:9 9:16 3:2 2:3 4:3 3:4 5:4 4:5 21:9 9:21\n"
    "                  或直接像素: 2048x2048 2752x1536 等\n"
    "  --n <数量>      生成数量（1-4，默认 1）\n"
    "  --mode <模式>   本次生成模式：daily(日常) / info(信息图)\n"
    "                  不指定则使用当前默认模式\n\n"
    "模式切换:\n"
    "  画 模式              查看当前模式\n"
    "  画 模式 日常          切换到通用日常模式\n"
    "  画 模式 信息图        切换到信息图模式\n\n"
    "示例:\n"
    "  画 一只可爱的猫咪坐在彩虹上\n"
    "  画 --size 1:1 卡通头像\n"
    "  画 --mode info --size 16:9 2026年AI发展趋势\n"
    "  画 --size 9:16 --n 2 赛博朋克城市夜景\n\n"
    "提示:\n"
    "  - 日常模式：自动适配风格，适合画猫画狗画风景\n"
    "  - 信息图模式：优化排版布局，适合海报/图表/知识图\n"
    "  - WebUI 可配置双模式的增强提示词和风格后缀"
)


def _parse_keywords(value, default: list[str]) -> list[str]:
    """将配置值解析为关键词列表，兼容 list / str 两种格式。"""
    if value is None:
        return list(default)
    if isinstance(value, list):
        return [str(k).strip() for k in value if str(k).strip()]
    if isinstance(value, str):
        parts = [k.strip() for k in value.split(",") if k.strip()]
        return parts if parts else list(default)
    return list(default)


def _resolve_mode_alias(val: str) -> str | None:
    """解析模式别名，返回标准模式名或 None。"""
    val = val.strip().lower()
    aliases = {
        "daily": MODE_DAILY,
        "日常": MODE_DAILY,
        "通用": MODE_DAILY,
        "通用日常": MODE_DAILY,
        "infographic": MODE_INFOGRAPHIC,
        "info": MODE_INFOGRAPHIC,
        "信息图": MODE_INFOGRAPHIC,
        "海报": MODE_INFOGRAPHIC,
    }
    return aliases.get(val)


@register(
    "astrbot_plugin_sensenova_t2i",
    "konley",
    "SenseNova U1 文生图 — 双模式（通用日常/信息图）、Prompt 增强、11 种尺寸、多图生成",
    "0.2.0",
)
class SenseNovaT2IPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        config = config or {}

        # 基础配置
        self.api_key: str = str(config.get("api_key", "")).strip()
        self.ignore_slash: bool = bool(config.get("ignore_slash", True))
        self.command_keywords: list[str] = _parse_keywords(
            config.get("command_keywords"), ["画"]
        )
        self.cooldown: int = int(config.get("cooldown", 10))

        # 图片配置
        self.default_size: str = str(config.get("default_size", "2752x1536")).strip()
        self.default_n: int = max(1, min(4, int(config.get("default_n", 1))))
        self.timeout: int = int(config.get("timeout", 180))

        # 双模式配置
        self.default_mode: str = str(config.get("default_mode", MODE_DAILY)).strip().lower()
        if self.default_mode not in VALID_MODES:
            self.default_mode = MODE_DAILY
        # 运行时默认模式（可被指令修改）
        self._runtime_mode: str = self.default_mode

        # 日常模式 Prompt 增强
        self.enable_prompt_enhance: bool = bool(
            config.get("enable_prompt_enhance", True)
        )
        self.daily_enhance_prompt: str = (
            config.get("daily_enhance_prompt") or DEFAULT_DAILY_ENHANCE_PROMPT
        )
        self.daily_style_suffix: str = str(
            config.get("daily_style_suffix") or DEFAULT_DAILY_STYLE_SUFFIX
        ).strip()

        # 信息图模式 Prompt 增强
        self.infographic_enhance_prompt: str = (
            config.get("infographic_enhance_prompt") or DEFAULT_INFOGRAPHIC_ENHANCE_PROMPT
        )
        self.infographic_style_suffix: str = str(
            config.get("infographic_style_suffix") or DEFAULT_INFOGRAPHIC_STYLE_SUFFIX
        ).strip()

        self.enhance_max_tokens: int = int(config.get("enhance_max_tokens", 2048))

        # 运维配置
        self.cleanup_delay: int = int(config.get("cleanup_delay", 10))
        self.max_retry: int = int(config.get("max_retry", 2))
        self.rate_limit_cooldown: int = int(config.get("rate_limit_cooldown", 5))

        # 临时目录
        self.tmp_dir = os.path.join(
            get_astrbot_data_path(), "plugin_data", "sensenova_t2i"
        )
        os.makedirs(self.tmp_dir, exist_ok=True)

        # 用户冷却记录 {user_id: last_ts}
        self._cooldowns: dict[str, float] = {}

        # 启动时清理残留临时文件
        self._cleanup_stale_tempfiles()

    # ── 模式工具方法 ──────────────────────────────────

    def _get_mode_prompt(self, mode: str) -> str:
        """获取指定模式的增强系统提示词。"""
        if mode == MODE_INFOGRAPHIC:
            return self.infographic_enhance_prompt
        return self.daily_enhance_prompt

    def _get_mode_suffix(self, mode: str) -> str:
        """获取指定模式的风格后缀。"""
        if mode == MODE_INFOGRAPHIC:
            return self.infographic_style_suffix
        return self.daily_style_suffix

    # ── 主入口 ────────────────────────────────────────

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_message(self, event: AstrMessageEvent):
        raw = event.message_str.strip()
        if not raw:
            return

        # 去前缀
        match_text = raw
        if self.ignore_slash and (
            match_text.startswith("/") or match_text.startswith("#")
        ):
            match_text = match_text[1:].strip()

        # 匹配触发关键词
        matched_kw = None
        for kw in self.command_keywords:
            if match_text.startswith(kw):
                matched_kw = kw
                break

        if not matched_kw:
            return

        rest = match_text[len(matched_kw):].strip()

        # 帮助指令
        if rest in ("帮助", "help", "说明"):
            event.stop_event()
            yield event.plain_result(HELP_TEXT)
            return

        # 空指令
        if not rest:
            event.stop_event()
            yield event.plain_result(HELP_TEXT)
            return

        # 模式管理指令：画 模式 / 画 模式 信息图 / 画 模式 日常
        if rest == "模式" or rest.startswith("模式 "):
            event.stop_event()
            yield from self._handle_mode_command(event, rest)
            return

        # 冷却检查
        user_id = event.get_sender_id()
        now = time.time()
        last = self._cooldowns.get(user_id, 0)
        if now - last < self.cooldown:
            remaining = int(self.cooldown - (now - last))
            event.stop_event()
            yield event.plain_result(
                f"冷却中，请 {remaining} 秒后再试"
            )
            return
        self._cooldowns[user_id] = now

        event.stop_event()

        # 解析 --size, --n, --mode 参数
        size, n, mode, prompt = self._parse_args(rest)

        if not prompt:
            yield event.plain_result("请输入图片描述\n用法: 画 <描述>\n输入 画帮助 查看完整用法")
            return

        # 校验 API Key
        if not self.api_key:
            yield event.plain_result(
                "未配置 SenseNova API Key，请在 WebUI 插件配置中填写"
            )
            return

        # 所有耗时操作在此完成后，只 yield 最终结果
        # （不能 yield 进度消息，否则其他插件的 after_message_sent 钩子
        #   会终止事件传播，导致生成器不再被迭代，后续代码不执行）

        # Prompt 增强（带超时保护）
        final_prompt = await self._enhance_prompt(prompt, mode)
        if final_prompt is None:
            final_prompt = prompt

        # 应用模式模板
        final_prompt = self._apply_template(final_prompt, mode)

        logger.info(
            f"[sensenova_t2i] 生成图片: mode={mode}, size={size}, n={n}, "
            f"prompt={final_prompt[:100]}..."
        )

        # 调用 API 生成图片
        image_urls = await self._generate_images(final_prompt, size, n)

        if not image_urls:
            yield event.plain_result("图片生成失败，请稍后重试")
            return

        # 下载并发送图片
        async for r in self._download_and_send(event, image_urls):
            yield r

    # ── 模式管理 ──────────────────────────────────────

    def _handle_mode_command(self, event: AstrMessageEvent, rest: str):
        """处理 '画 模式' 和 '画 模式 <模式名>' 指令。"""
        parts = rest.split(maxsplit=1)
        # parts[0] == "模式"
        if len(parts) == 1:
            # 画 模式 → 显示当前模式
            mode_name = MODE_NAMES.get(self._runtime_mode, self._runtime_mode)
            yield event.plain_result(
                f"当前模式：{mode_name}（{self._runtime_mode}）\n"
                f"可用模式：通用日常(daily) / 信息图(infographic)\n"
                f"切换：画 模式 日常  或  画 模式 信息图"
            )
            return

        target = _resolve_mode_alias(parts[1])
        if target is None:
            yield event.plain_result(
                f"未知模式：{parts[1]}\n"
                f"可用模式：日常(daily) / 信息图(infographic)"
            )
            return

        self._runtime_mode = target
        mode_name = MODE_NAMES.get(target, target)
        yield event.plain_result(f"已切换到 {mode_name} 模式（{target}）")

    # ── 参数解析 ──────────────────────────────────────

    def _parse_args(self, text: str) -> tuple[str, int, str, str]:
        """解析 --size, --n, --mode 参数，返回 (size, n, mode, prompt)。"""
        size = self.default_size
        n = self.default_n
        mode = self._runtime_mode

        # 匹配 --size 参数（可出现在任意位置）
        size_match = re.search(r"--size\s+(\S+)", text, re.IGNORECASE)
        if size_match:
            size = self._resolve_size(size_match.group(1))
            text = text[: size_match.start()] + text[size_match.end():]

        # 匹配 -s 短参数
        s_match = re.search(r"-s\s+(\S+)", text, re.IGNORECASE)
        if s_match:
            size = self._resolve_size(s_match.group(1))
            text = text[: s_match.start()] + text[s_match.end():]

        # 匹配 --n 参数
        n_match = re.search(r"--n\s+(\d+)", text, re.IGNORECASE)
        if n_match:
            n = max(1, min(4, int(n_match.group(1))))
            text = text[: n_match.start()] + text[n_match.end():]

        # 匹配 --mode 参数
        mode_match = re.search(r"--mode\s+(\S+)", text, re.IGNORECASE)
        if mode_match:
            resolved = _resolve_mode_alias(mode_match.group(1))
            if resolved:
                mode = resolved
            text = text[: mode_match.start()] + text[mode_match.end():]

        prompt = text.strip()
        return size, n, mode, prompt

    @staticmethod
    def _resolve_size(size_val: str) -> str:
        """将尺寸值解析为合法的像素尺寸。支持比例简写和完整像素。"""
        size_val = size_val.strip()

        # 比例简写
        if size_val in SIZE_MAP:
            return SIZE_MAP[size_val]

        # 完整像素值
        if size_val in ALL_SIZES:
            return size_val

        # 尝试匹配 WxH 格式
        m = re.match(r"^(\d+)x(\d+)$", size_val, re.IGNORECASE)
        if m:
            candidate = f"{m.group(1)}x{m.group(2)}"
            if candidate in ALL_SIZES:
                return candidate

        # 无法识别，返回默认
        return "2752x1536"

    # ── Prompt 增强 ────────────────────────────────────

    async def _enhance_prompt(self, prompt: str, mode: str) -> str | None:
        """调用 AstrBot 主 Provider 对 prompt 进行扩写增强。"""
        if not self.enable_prompt_enhance:
            return None

        provider = self.context.get_using_provider()
        if provider is None:
            logger.warning("[sensenova_t2i] 未配置大模型提供商，跳过 Prompt 增强")
            return None

        system_prompt = self._get_mode_prompt(mode)

        try:
            llm_resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    max_tokens=self.enhance_max_tokens,
                ),
                timeout=60,
            )
            enhanced = (llm_resp.completion_text or "").strip()
            if enhanced:
                logger.info(
                    f"[sensenova_t2i] Prompt 增强({mode}): {prompt[:50]} -> {enhanced[:50]}..."
                )
                return enhanced
        except Exception as e:
            logger.error(f"[sensenova_t2i] Prompt 增强失败: {e}")

        return None

    def _apply_template(self, prompt: str, mode: str) -> str:
        """应用模式模板（信息图前缀 + 风格后缀）。"""
        result = prompt

        if mode == MODE_INFOGRAPHIC:
            result = INFOGRAPHIC_PREFIX + result

        suffix = self._get_mode_suffix(mode)
        if suffix:
            result = result + " " + suffix

        return result

    # ── API 调用 ──────────────────────────────────────

    async def _generate_images(
        self, prompt: str, size: str, n: int
    ) -> list[str]:
        """调用 SenseNova U1 Fast API 生成图片，返回图片 URL 列表。"""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "sensenova-u1-fast",
            "prompt": prompt,
            "size": size,
            "n": n,
        }

        last_error = None
        total_attempts = self.max_retry + 1

        for attempt in range(total_attempts):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        IMAGE_ENDPOINT,
                        headers=headers,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=self.timeout),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            urls = [
                                item.get("url")
                                for item in data.get("data", [])
                                if item.get("url")
                            ]
                            if urls:
                                return urls
                            logger.error(
                                f"[sensenova_t2i] API 返回无图片 URL: {data}"
                            )
                            return []

                        elif resp.status == 429:
                            last_error = "API 限流"
                            logger.warning(
                                f"[sensenova_t2i] 429 限流, "
                                f"等待 {self.rate_limit_cooldown}s 后重试"
                            )
                            if attempt < total_attempts - 1:
                                await asyncio.sleep(self.rate_limit_cooldown)
                            continue

                        elif resp.status == 401:
                            error_text = await resp.text()
                            logger.error(
                                f"[sensenova_t2i] 401 认证失败: {error_text}"
                            )
                            return []

                        else:
                            error_text = await resp.text()
                            last_error = f"HTTP {resp.status}: {error_text}"
                            logger.error(
                                f"[sensenova_t2i] API 错误: {last_error}"
                            )
                            if attempt < total_attempts - 1:
                                await asyncio.sleep(2)
                            continue

            except asyncio.TimeoutError:
                last_error = "API 超时"
                logger.error(f"[sensenova_t2i] {last_error}")
            except Exception as e:
                last_error = str(e)
                logger.error(f"[sensenova_t2i] API 调用异常: {e}")

        logger.error(f"[sensenova_t2i] 所有重试失败: {last_error}")
        return []

    # ── 图片下载与发送 ──────────────────────────────────

    async def _download_and_send(
        self, event: AstrMessageEvent, image_urls: list[str]
    ):
        """下载图片到本地并发送，发送后延迟清理。"""
        local_paths = []

        for i, url in enumerate(image_urls):
            uid = uuid.uuid4().hex[:8]
            local_path = os.path.join(
                self.tmp_dir, f"t2i_{os.getpid()}_{uid}_{i}.png"
            )
            ok = await self._download_image(url, local_path)
            if ok:
                local_paths.append(local_path)

        if not local_paths:
            yield event.plain_result("图片下载失败，请稍后重试")
            return

        # 合并所有图片到一条消息发送
        chain = []
        for path in local_paths:
            chain.append(Comp.Image(file=str(path)))
        yield event.chain_result(chain)

        # 延迟清理
        paths_to_clean = list(local_paths)

        async def _cleanup():
            await asyncio.sleep(self.cleanup_delay)
            for p in paths_to_clean:
                try:
                    os.remove(p)
                    logger.debug(
                        f"[sensenova_t2i] 清理临时文件: {os.path.basename(p)}"
                    )
                except OSError:
                    pass

        asyncio.ensure_future(_cleanup())

    @staticmethod
    async def _download_image(url: str, path: str) -> bool:
        """异步下载图片到本地。

        SenseNova 返回的是 S3 presigned URL，aiohttp 默认会对 URL 重新编码
        导致签名不匹配（SignatureDoesNotMatch 403）。
        使用 yarl.URL(encoded=True) 跳过重新编码。
        """
        try:
            from yarl import URL
            encoded_url = URL(url, encoded=True)
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    encoded_url, timeout=aiohttp.ClientTimeout(total=60)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        with open(path, "wb") as f:
                            f.write(data)
                        return True
                    logger.error(
                        f"[sensenova_t2i] 下载失败 HTTP {resp.status}"
                    )
                    return False
        except Exception as e:
            logger.error(f"[sensenova_t2i] 下载异常: {e}")
            return False

    # ── 临时文件清理 ────────────────────────────────────

    def _cleanup_stale_tempfiles(self):
        """清除进程崩溃后残留的旧临时文件（超过 1 小时的）。"""
        now = time.time()
        cutoff = now - 3600
        try:
            for fname in os.listdir(self.tmp_dir):
                if fname.startswith("t2i_"):
                    fpath = os.path.join(self.tmp_dir, fname)
                    try:
                        if os.path.getmtime(fpath) < cutoff:
                            os.remove(fpath)
                            logger.info(
                                f"[sensenova_t2i] 清理残留: {fname}"
                            )
                    except OSError:
                        pass
        except OSError:
            pass

    async def terminate(self):
        pass
