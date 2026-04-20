import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Awaitable

from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

logger = logging.getLogger(__name__)


class _SlackShutdownNoiseFilter(logging.Filter):
    """Suppress slack-sdk's event-loop-closed errors emitted during shutdown.

    When the service is stopping, slack-sdk's background tasks keep trying
    to use a queue bound to the old event loop and log a blizzard of
    RuntimeError tracebacks. Those errors are harmless but flood the
    journal and can cause TimeoutStopSec to fire because log flushing
    takes so long. This filter drops only those specific errors.
    """
    _SIGNATURES = (
        "is bound to a different event loop",
        "Event loop is closed",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if any(sig in msg for sig in self._SIGNATURES):
            return False
        # Also suppress the follow-up exc_info spam that mentions these errors
        if record.exc_info:
            exc_msg = str(record.exc_info[1]) if record.exc_info[1] else ""
            if any(sig in exc_msg for sig in self._SIGNATURES):
                return False
        return True


# Install the filter once at import time on the noisy slack-sdk logger.
logging.getLogger("slack_sdk.socket_mode.aiohttp").addFilter(_SlackShutdownNoiseFilter())
logging.getLogger("slack_sdk.socket_mode.async_client").addFilter(_SlackShutdownNoiseFilter())

# Type for callback that processes batched messages
TriageCallback = Callable[[str], Awaitable[None]]  # (triage_prompt) -> None


@dataclass
class SlackMessage:
    channel: str
    user: str
    text: str
    timestamp: float


@dataclass
class SlackConfig:
    bot_token: str
    app_token: str
    channels: dict[str, dict] = field(default_factory=dict)
    history_limit: int = 50
    triage_interval: int = 900  # seconds between triage runs (default 15 min)
    enabled: bool = True


class SlackMonitor:
    def __init__(self, config: SlackConfig) -> None:
        self.config = config
        self.web_client = AsyncWebClient(token=config.bot_token)
        self.socket_client: SocketModeClient | None = None
        self._buffer: list[SlackMessage] = []
        self._buffer_lock = asyncio.Lock()
        self._channel_id_map: dict[str, str] = {}  # channel_id -> channel_name
        self._triage_callback: TriageCallback | None = None
        self._triage_task: asyncio.Task | None = None
        self._running = False

    def set_triage_callback(self, callback: TriageCallback) -> None:
        self._triage_callback = callback

    async def start(self) -> None:
        if not self.config.enabled:
            logger.info("Slack monitor disabled")
            return

        # Resolve channel names to IDs
        await self._resolve_channels()

        # Start socket mode connection
        self.socket_client = SocketModeClient(
            app_token=self.config.app_token,
            web_client=self.web_client,
        )
        self.socket_client.socket_mode_request_listeners.append(self._handle_event)

        self._running = True
        await self.socket_client.connect()
        logger.info(
            "Slack monitor connected, watching %d channels: %s",
            len(self._channel_id_map),
            ", ".join(self._channel_id_map.values()),
        )

        # Start periodic triage loop
        self._triage_task = asyncio.create_task(self._triage_loop())

    async def stop(self) -> None:
        self._running = False
        if self._triage_task:
            self._triage_task.cancel()
            try:
                await asyncio.wait_for(self._triage_task, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        if self.socket_client:
            # Try close() first if available (cleaner, stops background tasks).
            # Fall back to disconnect(). Both wrapped in a short timeout to
            # ensure the service doesn't hit TimeoutStopSec.
            closer = getattr(self.socket_client, "close", None) or self.socket_client.disconnect
            try:
                await asyncio.wait_for(closer(), timeout=2.0)
            except (asyncio.TimeoutError, Exception) as e:
                logger.debug("Slack shutdown error (expected): %s", type(e).__name__)
        # Flush remaining buffer
        try:
            await self._flush_buffer()
        except Exception:
            pass

    async def _resolve_channels(self) -> None:
        """Map configured channel names to Slack channel IDs."""
        configured_names = {
            name.lstrip("#"): name for name in self.config.channels
        }

        cursor = None
        while True:
            result = await self.web_client.conversations_list(
                types="public_channel,private_channel",
                cursor=cursor,
                limit=200,
            )
            for channel in result["channels"]:
                ch_name = channel["name"]
                if ch_name in configured_names:
                    self._channel_id_map[channel["id"]] = f"#{ch_name}"
                    logger.info("Resolved Slack channel: #%s -> %s", ch_name, channel["id"])

            cursor = result.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        # Warn about unresolved channels
        resolved_names = set(self._channel_id_map.values())
        for name in self.config.channels:
            if name not in resolved_names:
                logger.warning("Could not resolve Slack channel: %s", name)

    async def _handle_event(self, client: SocketModeClient, req: SocketModeRequest) -> None:
        """Handle incoming socket mode events."""
        # Always acknowledge
        response = SocketModeResponse(envelope_id=req.envelope_id)
        await client.send_socket_mode_response(response)

        if req.type != "events_api":
            return

        event = req.payload.get("event", {})
        event_type = event.get("type")

        # Only handle regular messages (not bot messages, edits, etc.)
        if event_type != "message" or event.get("subtype"):
            return

        channel_id = event.get("channel", "")
        if channel_id not in self._channel_id_map:
            return

        # Resolve user name
        user_id = event.get("user", "unknown")
        user_name = await self._resolve_user(user_id)

        msg = SlackMessage(
            channel=self._channel_id_map[channel_id],
            user=user_name,
            text=event.get("text", ""),
            timestamp=float(event.get("ts", time.time())),
        )

        async with self._buffer_lock:
            self._buffer.append(msg)
            logger.debug("Buffered Slack message from %s in %s", msg.user, msg.channel)

    async def _resolve_user(self, user_id: str) -> str:
        """Resolve a Slack user ID to a display name."""
        try:
            result = await self.web_client.users_info(user=user_id)
            profile = result["user"].get("profile", {})
            return profile.get("display_name") or profile.get("real_name") or user_id
        except Exception:
            return user_id

    async def _triage_loop(self) -> None:
        """Periodically flush buffer and send to Claude for triage."""
        try:
            while self._running:
                await asyncio.sleep(self.config.triage_interval)
                await self._flush_buffer()
        except asyncio.CancelledError:
            pass

    async def _flush_buffer(self) -> None:
        """Send buffered messages to Claude for triage."""
        async with self._buffer_lock:
            if not self._buffer:
                return
            messages = self._buffer.copy()
            self._buffer.clear()

        if not self._triage_callback:
            logger.warning("No triage callback set, discarding %d messages", len(messages))
            return

        # Format messages for Claude
        prompt = self._format_triage_prompt(messages)
        logger.info("Triaging %d Slack messages across channels", len(messages))

        try:
            await self._triage_callback(prompt)
        except Exception:
            logger.exception("Slack triage failed")

    def _format_triage_prompt(self, messages: list[SlackMessage]) -> str:
        # Group by channel
        by_channel: dict[str, list[SlackMessage]] = {}
        for msg in messages:
            by_channel.setdefault(msg.channel, []).append(msg)

        lines = [
            "Review these recent Slack messages and notify me via your response "
            "ONLY if there is something that needs my attention or action. "
            "Be concise. If nothing is noteworthy, respond with just: "
            '"Nothing notable in Slack right now."\n'
        ]

        for channel, msgs in by_channel.items():
            lines.append(f"\n**{channel}** ({len(msgs)} messages):")
            for msg in msgs:
                lines.append(f"  [{msg.user}]: {msg.text}")

        return "\n".join(lines)
