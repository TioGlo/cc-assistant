"""Discord transport for the assistant.

Runs alongside the Telegram bot in the same asyncio loop. Listens on
allowlisted guilds/channels (with per-channel requireMention semantics),
routes inbound messages through the same Claude bridge, and sends
replies back to the originating channel.

Outbound delivery for cron-triggered jobs is exposed via `send_to_channel`,
which the scheduler callback uses when a job has `delivery.transport ==
"discord"`.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import discord

from .config import DiscordConfig
from .formatter import split_message

if TYPE_CHECKING:
    from .bot import AssistantBot

logger = logging.getLogger(__name__)

DISCORD_MAX_LEN = 2000

# Pattern to strip a leading @-mention of the bot from message content.
# Discord mention forms: <@123456789> and <@!123456789>
_MENTION_RE = re.compile(r"<@!?(\d+)>")


class DiscordBot:
    """Thin Discord client that delegates inbound messages to AssistantBot."""

    def __init__(self, config: DiscordConfig, assistant_bot: AssistantBot) -> None:
        self.config = config
        self.assistant_bot = assistant_bot
        self.client: discord.Client | None = None
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()

    # -- Lifecycle --

    async def start(self) -> None:
        if not self.config.enabled:
            logger.info("Discord disabled by config")
            return
        if not self.config.bot_token:
            logger.warning("Discord enabled but bot_token is empty — skipping")
            return

        intents = discord.Intents.default()
        intents.message_content = True  # privileged — must be enabled in dev portal
        intents.guilds = True
        intents.guild_messages = True

        self.client = discord.Client(intents=intents)

        @self.client.event
        async def on_ready() -> None:
            logger.info("Discord connected as %s (id=%s)", self.client.user, self.client.user.id)
            allowed = sum(len(g.channels) for g in self.config.guilds.values())
            logger.info("Discord allowlist: %d guild(s), %d channel(s)",
                        len(self.config.guilds), allowed)
            self._ready.set()

        @self.client.event
        async def on_message(message: discord.Message) -> None:
            try:
                await self._on_message(message)
            except Exception:
                logger.exception("Discord on_message failed")

        # Run the client as a background task — we don't want client.start() to
        # block main.py's Telegram polling loop.
        self._task = asyncio.create_task(self._run_client(), name="discord-client")
        logger.info("Discord client task started")

    async def _run_client(self) -> None:
        try:
            await self.client.start(self.config.bot_token)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Discord client crashed")

    async def stop(self) -> None:
        if self.client and not self.client.is_closed():
            await self.client.close()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    # -- Inbound --

    async def _on_message(self, message: discord.Message) -> None:
        # Skip our own messages and other bots
        if message.author == self.client.user or message.author.bot:
            return

        # Skip DMs for now (allowlist is per-guild)
        if message.guild is None:
            return

        # Allowlist check
        require_mention = self.config.channel_requires_mention(
            message.guild.id, message.channel.id,
        )
        if require_mention is None:
            # Channel not allowlisted — silently ignore
            return

        # Mention requirement
        if require_mention and self.client.user not in message.mentions:
            return

        # Extract the prompt — strip mentions of the bot
        text = self._clean_message_text(message)
        if not text:
            return

        session_key = f"discord:{message.channel.id}"
        logger.info(
            "Discord inbound (guild=%s channel=%s author=%s): %s",
            message.guild.id, message.channel.id, message.author.name, text[:80],
        )

        send_text, send_typing = self._make_callbacks(message.channel)
        await self.assistant_bot.process_text_input(
            text=text,
            session_key=session_key,
            send_text=send_text,
            send_typing=send_typing,
        )

    def _clean_message_text(self, message: discord.Message) -> str:
        """Strip mentions of the bot and return the user's intended prompt."""
        text = message.content or ""
        bot_id = self.client.user.id if self.client.user else None
        if bot_id is None:
            return text.strip()

        def _replace(m: re.Match[str]) -> str:
            return "" if m.group(1) == str(bot_id) else m.group(0)

        return _MENTION_RE.sub(_replace, text).strip()

    def _make_callbacks(
        self, channel: discord.abc.Messageable,
    ) -> tuple[Callable[[str], Awaitable[None]], Callable[[], Awaitable[None]] | None]:
        async def send_text(chunk: str) -> None:
            for piece in split_message(chunk, max_len=DISCORD_MAX_LEN):
                await channel.send(piece)

        async def send_typing() -> None:
            # Discord typing pulses last ~10s; keep it alive while we wait.
            try:
                while True:
                    async with channel.typing():
                        await asyncio.sleep(8)
            except asyncio.CancelledError:
                return

        return send_text, send_typing

    # -- Outbound (used by scheduler callback for Discord-targeted jobs) --

    async def send_to_channel(self, channel_id: int | str, text: str) -> None:
        """Send a message to a Discord channel by ID. Used by cron delivery."""
        if self.client is None or not self._ready.is_set():
            logger.warning(
                "Discord not ready; dropping send_to_channel(%s): %s",
                channel_id, text[:80],
            )
            return
        try:
            channel = self.client.get_channel(int(channel_id))
            if channel is None:
                channel = await self.client.fetch_channel(int(channel_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logger.exception("Discord channel %s unreachable", channel_id)
            return
        for chunk in split_message(text, max_len=DISCORD_MAX_LEN):
            try:
                await channel.send(chunk)
            except discord.HTTPException:
                logger.exception("Discord send failed (channel=%s)", channel_id)
                return
