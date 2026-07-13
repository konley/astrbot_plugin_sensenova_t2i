"""SenseNova U1 文生图插件

调用商汤日日新 sensenova-u1-fast 模型生成图片。
支持 Prompt 增强（调用 AstrBot 主 Provider）、11 种固定尺寸、多图生成、信息图模式。
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

DEFAULT_ENHANCE_PROMPT = (
    "你是一个专业的图像提示词工程师。用户会给你一段简短的图片描述，"
    "请将其扩写为结构化的高质量图像生成提示词。\n"
    "要求：\n"
    "1. 保留用户原始意图，不要改变核心内容\n"
    "2. 补充构图布局、光影配色、风格细节、画面氛围\n"
    "3. 用中文输出，不要解释，直接输出增强后的提示词\n"
    "4. 不要使用任何 Markdown 格式\n"
    "5. 如果用户描述的是信息图/海报/图表场景，请补充布局结构描述"
)

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
    "  --n <数量>      生成数量（1-4，默认 1）\n\n"
    "示例:\n"
    "  画 一只可爱的猫咪坐在彩虹上\n"
    "  画 --size 1:1 卡通头像\n"
    "  画 --size 9:16 --n 2 赛博朋克城市夜景\n"
    "  画 信息图：2026年AI发展趋势，三列布局，深蓝科技风\n\n"
    "提示:\n"
    "  - 详细描述效果更好：主题+构图+配色+风格\n"
    "  - 信息图场景建议描述布局结构\n"
    "  - WebUI 可配置 Prompt 增强、默认尺寸等"
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


@register(
    "astrbot_plugin_sensenova_t2i",
    "konley",
    "SenseNova U1 文生图 — 调用商汤日日新 sensenova-u1-fast 模型，支持 Prompt 增强、11 种尺寸、多图生成、信息图模式",
    "0.1.0",
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

        # Prompt 增强
        self.enable_prompt_enhance: bool = bool(
            config.get("enable_prompt_enhance", True)
        )
        self.enhance_system_prompt: str = (
            config.get("enhance_system_prompt") or DEFAULT_ENHANCE_PROMPT
        )
        self.enhance_max_tokens: int = int(config.get("enhance_max_tokens", 2048))

        # 默认参数模板
        self.default_style_suffix: str = str(
            config.get("default_style_suffix", "")
        ).strip()
        self.infographic_mode: bool = bool(config.get("infographic_mode", False))

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

        # 解析 --size 和 --n 参数
        size, n, prompt = self._parse_args(rest)

        if not prompt:
            yield event.plain_result("请输入图片描述\n用法: 画 <描述>\n输入 画帮助 查看完整用法")
            return

        # 校验 API Key
        if not self.api_key:
            yield event.plain_result(
                "未配置 SenseNova API Key，请在 WebUI 插件配置中填写"
            )
            return

        # 提示生成中
        yield event.plain_result("正在生成图片，请稍候...")

        # Prompt 增强
        final_prompt = await self._enhance_prompt(prompt)
        if final_prompt is None:
            final_prompt = prompt

        # 应用默认参数模板
        final_prompt = self._apply_template(final_prompt)

        logger.info(
            f"[sensenova_t2i] 生成图片: size={size}, n={n}, "
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

    # ── 参数解析 ──────────────────────────────────────

    def _parse_args(self, text: str) -> tuple[str, int, str]:
        """解析 --size 和 --n 参数，返回 (size, n, prompt)。"""
        size = self.default_size
        n = self.default_n
        remaining_parts = []

        # 匹配 --size 参数
        size_match = re.match(
            r"--size\s+(\S+)", text, re.IGNORECASE
        )
        if size_match:
            size_val = size_match.group(1)
            size = self._resolve_size(size_val)
            text = text[: size_match.start()] + text[size_match.end():]

        # 匹配 -s 短参数
        s_match = re.match(r"-s\s+(\S+)", text, re.IGNORECASE)
        if s_match:
            size_val = s_match.group(1)
            size = self._resolve_size(size_val)
            text = text[: s_match.start()] + text[s_match.end():]

        # 匹配 --n 参数
        n_match = re.match(r"--n\s+(\d+)", text, re.IGNORECASE)
        if n_match:
            n = max(1, min(4, int(n_match.group(1))))
            text = text[: n_match.start()] + text[n_match.end():]

        prompt = text.strip()
        return size, n, prompt

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

    async def _enhance_prompt(self, prompt: str) -> str | None:
        """调用 AstrBot 主 Provider 对 prompt 进行扩写增强。"""
        if not self.enable_prompt_enhance:
            return None

        provider = self.context.get_using_provider()
        if provider is None:
            logger.warning("[sensenova_t2i] 未配置大模型提供商，跳过 Prompt 增强")
            return None

        try:
            llm_resp = await provider.text_chat(
                prompt=prompt,
                system_prompt=self.enhance_system_prompt,
                max_tokens=self.enhance_max_tokens,
            )
            enhanced = (llm_resp.completion_text or "").strip()
            if enhanced:
                logger.info(
                    f"[sensenova_t2i] Prompt 增强: {prompt[:50]} -> {enhanced[:50]}..."
                )
                return enhanced
        except Exception as e:
            logger.error(f"[sensenova_t2i] Prompt 增强失败: {e}")

        return None

    def _apply_template(self, prompt: str) -> str:
        """应用默认参数模板（风格后缀 + 信息图模式）。"""
        result = prompt

        if self.infographic_mode:
            result = INFOGRAPHIC_PREFIX + result

        if self.default_style_suffix:
            result = result + " " + self.default_style_suffix

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

        # 逐张发送
        for path in local_paths:
            yield event.chain_result([Comp.Image(file=str(path))])

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
        """异步下载图片到本地。"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=60)
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
