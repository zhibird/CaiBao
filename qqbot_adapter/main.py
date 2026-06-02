"""CaiBao QQ Bot 适配器入口。

启动所有配置的 QQ 通道，连接 CaiBao Agent API，
在 QQ 与 CaiBao 之间双向翻译消息。

用法：
    python main.py                          # 前台运行
    python main.py --config config.toml     # 指定配置文件
    python main.py --log-level DEBUG        # 调试日志

架构：
    QQ消息 → NapCat WS → NapCatChannel → MessageBus.inbound
        → AgentBridge → CaiBao API (SSE)
        → MessageBus.outbound → NapCatChannel → QQ回复

依赖：
    pip install -r requirements.txt

前置条件：
    1. CaiBao 已启动（http://127.0.0.1:8000）
    2. NapCat 已启动并配置好正向 WebSocket
    3. 已在 CaiBao 中注册 bot_user_id 账号
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import tomllib
from pathlib import Path

# 确保 qqbot_adapter 作为包可被导入（python main.py 直接运行时需要）
_sys_path_root = Path(__file__).resolve().parents[1]
if str(_sys_path_root) not in sys.path:
    sys.path.insert(0, str(_sys_path_root))

from core.bus import MessageBus
from core.bridge import AgentBridge
from channels.napcat_channel import NapCatChannel
from channels.qqbot_channel import QQBotChannel


def setup_logging(level: str = "INFO") -> None:
    """配置日志格式和级别。"""
    fmt = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
    datefmt = "%H:%M:%S"

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=fmt,
        datefmt=datefmt,
    )

    # 降低第三方库日志噪音
    for noisy in ("httpx", "httpcore", "websockets", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def load_config(path: str) -> dict:
    """加载 TOML 配置文件。"""
    config_path = Path(path)
    if not config_path.exists():
        print(f"错误: 配置文件不存在: {config_path}")
        sys.exit(1)

    try:
        with open(config_path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        print(f"错误: 配置文件格式无效 ({config_path}): {exc}")
        sys.exit(1)


async def main(config_path: str) -> None:
    """主入口：初始化 MessageBus → 启动通道 → 启动 AgentBridge。"""
    config = load_config(config_path)

    # 1. MessageBus
    bus = MessageBus()
    await bus.start()

    channels = []
    bridge = None

    try:
        # 2. NapCat 通道
        napcat_cfg = config.get("channels", {}).get("napcat", {})
        if napcat_cfg.get("enabled", False):
            napcat = NapCatChannel(
                bus=bus,
                ws_url=napcat_cfg.get("ws_url", "ws://127.0.0.1:3001"),
                access_token=napcat_cfg.get("access_token") or None,
                allow_from=napcat_cfg.get("allow_from", []),
                allow_all=napcat_cfg.get("allow_all", False),
                groups=napcat_cfg.get("groups", []),
                reconnect=napcat_cfg.get("reconnect", True),
            )
            await napcat.start()
            channels.append(napcat)
            _logger.info("NapCat channel started")

        # 3. QQBot 官方通道
        qqbot_cfg = config.get("channels", {}).get("qqbot", {})
        if qqbot_cfg.get("enabled", False):
            qqbot = QQBotChannel(
                bus=bus,
                app_id=qqbot_cfg.get("app_id", ""),
                client_secret=qqbot_cfg.get("client_secret", ""),
                allow_from=qqbot_cfg.get("allow_from", []),
                allow_all=qqbot_cfg.get("allow_all", False),
                groups=qqbot_cfg.get("groups", []),
                reconnect=qqbot_cfg.get("reconnect", True),
            )
            await qqbot.start()
            channels.append(qqbot)
            _logger.info("QQBot channel started")

        if not channels:
            _logger.error(
                "No channels configured! "
                "Set channels.napcat.enabled = true in config.toml"
            )
            return

        # 4. AgentBridge
        caibao_cfg = config.get("caibao", {})
        message_cfg = config.get("message", {})
        bridge = AgentBridge(
            bus=bus,
            caibao_base_url=caibao_cfg.get("base_url", "http://127.0.0.1:8000"),
            bot_user_id=caibao_cfg.get("bot_user_id", "qqbot"),
            bot_password=caibao_cfg.get("bot_password", ""),
            http_timeout=caibao_cfg.get("http_timeout", 120.0),
            system_prompt=caibao_cfg.get("system_prompt") or None,
            sync_processing_notice_enabled=message_cfg.get("show_sync_processing_notice", True),
            sync_processing_notice_delay_seconds=message_cfg.get("processing_notice_delay_seconds", 6.0),
            sync_processing_notice=message_cfg.get(
                "sync_processing_notice",
                "⏳ 稍等一下，我在认真整理思路中 (ง •̀_•́)ง",
            ),
        )

        _logger.info(
            "CaiBao QQ Bot Adapter started\n"
            "  CaiBao: %s\n"
            "  Bot User: %s\n"
            "  Channels: %s",
            caibao_cfg.get("base_url"),
            caibao_cfg.get("bot_user_id"),
            [ch.channel_type for ch in channels],
        )

        # 5. 主循环（阻塞直到被中断）
        await bridge.run()

    except asyncio.CancelledError:
        _logger.info("Shutting down...")
    except KeyboardInterrupt:
        _logger.info("Interrupted by user")
    finally:
        # 清理
        if bridge is not None:
            await bridge.close()
        for ch in channels:
            try:
                await ch.stop()
            except Exception:
                _logger.exception("Error stopping channel %s", ch.channel_type)
        await bus.stop()
        _logger.info("CaiBao QQ Bot Adapter stopped")


# 模块级 logger（在 setup_logging 之后才有 handler）
_logger = logging.getLogger("qqbot_adapter")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="CaiBao QQ Bot Adapter",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py
  python main.py --config config.toml
  python main.py --log-level DEBUG
        """,
    )
    parser.add_argument(
        "--config", "-c",
        default="config.toml",
        help="配置文件路径 (默认: config.toml)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别 (默认: INFO)",
    )
    args = parser.parse_args()

    setup_logging(args.log_level)
    # 重新获取 logger（setup_logging 之后才会有 handler）
    _logger = logging.getLogger("qqbot_adapter")

    try:
        asyncio.run(main(args.config))
    except KeyboardInterrupt:
        pass
