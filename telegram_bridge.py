#!/usr/bin/env python3
"""
AgentChattr <-> Telegram bridge (bidirectional).

Read path:  Polls AgentChattr for new messages and forwards to Telegram.
Write path: Polls Telegram for operator messages and forwards to AgentChattr.

Usage:
    python3 telegram_bridge.py [--config agentchattr/config.toml]

Requires:
    pip install requests tomli  (tomli only needed for Python < 3.11)
"""

import argparse
import atexit
import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_AGENTCHATTR_URL = "http://127.0.0.1:8300"
DEFAULT_POLL_INTERVAL = 2  # seconds
DEFAULT_CURSOR_FILE = "telegram_bridge_cursor.json"
TELEGRAM_API = "https://api.telegram.org"

logger = logging.getLogger("telegram_bridge")


def load_config(config_path: str | None = None) -> dict:
    """Load config: TOML defaults -> env var overrides. Env vars always win."""
    # Start with hardcoded defaults
    config = {
        "telegram_bot_token": "",
        "telegram_chat_id": "",
        "agentchattr_url": DEFAULT_AGENTCHATTR_URL,
        "poll_interval": DEFAULT_POLL_INTERVAL,
        "bridge_sender": "telegram-bridge",
        "cursor_file": DEFAULT_CURSOR_FILE,
    }

    # Layer 1: config.toml [telegram] section
    toml_path = Path(config_path) if config_path else Path(__file__).parent / "config.toml"
    if toml_path.exists():
        try:
            if sys.version_info >= (3, 11):
                import tomllib
            else:
                import tomli as tomllib  # type: ignore[no-redef]
            with open(toml_path, "rb") as f:
                toml_data = tomllib.load(f)
            tg = toml_data.get("telegram", {})
            if tg.get("bot_token"):
                config["telegram_bot_token"] = tg["bot_token"]
            if tg.get("chat_id"):
                config["telegram_chat_id"] = str(tg["chat_id"])
            if tg.get("agentchattr_url"):
                config["agentchattr_url"] = tg["agentchattr_url"]
            if tg.get("poll_interval"):
                config["poll_interval"] = int(tg["poll_interval"])
            if tg.get("bridge_sender"):
                config["bridge_sender"] = tg["bridge_sender"]
        except Exception as e:
            logger.warning("Could not parse config.toml [telegram] section: %s", e)

    # Layer 2: environment variables override everything
    env_map = {
        "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
        "TELEGRAM_CHAT_ID": "telegram_chat_id",
        "AGENTCHATTR_URL": "agentchattr_url",
        "POLL_INTERVAL": "poll_interval",
        "BRIDGE_SENDER": "bridge_sender",
        "CURSOR_FILE": "cursor_file",
    }
    for env_key, config_key in env_map.items():
        val = os.environ.get(env_key)
        if val:
            config[config_key] = int(val) if config_key == "poll_interval" else val

    # Resolve cursor file path relative to the config file directory (not cwd)
    cursor = Path(config["cursor_file"])
    if not cursor.is_absolute():
        config["cursor_file"] = str(toml_path.parent / cursor)

    return config


def validate_config(config: dict) -> None:
    """Validate required config values are present."""
    if not config["telegram_bot_token"]:
        sys.exit("Error: TELEGRAM_BOT_TOKEN not set (env var or config.toml [telegram] bot_token)")
    if not config["telegram_chat_id"]:
        sys.exit("Error: TELEGRAM_CHAT_ID not set (env var or config.toml [telegram] chat_id)")


# ---------------------------------------------------------------------------
# Cursor persistence
# ---------------------------------------------------------------------------

def load_cursor(cursor_file: str) -> tuple[int, int]:
    """Load cursors from file. Returns (last_seen_id, telegram_update_offset)."""
    path = Path(cursor_file)
    if path.exists():
        try:
            data = json.loads(path.read_text())
            seen = int(data.get("last_seen_id", 0))
            offset = int(data.get("telegram_update_offset", 0))
            logger.info("Loaded cursor: last_seen_id=%d, telegram_offset=%d", seen, offset)
            return seen, offset
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            logger.warning("Could not parse cursor file %s: %s", cursor_file, e)
    return 0, 0


def save_cursor(cursor_file: str, last_seen_id: int, telegram_update_offset: int = 0) -> None:
    """Persist cursors to file."""
    try:
        Path(cursor_file).write_text(json.dumps({
            "last_seen_id": last_seen_id,
            "telegram_update_offset": telegram_update_offset,
        }))
        logger.debug("Saved cursor: last_seen_id=%d, telegram_offset=%d", last_seen_id, telegram_update_offset)
    except OSError as e:
        logger.error("Failed to save cursor: %s", e)


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def telegram_get_me(token: str) -> dict:
    """Verify bot token with Telegram getMe API."""
    resp = requests.get(f"{TELEGRAM_API}/bot{token}/getMe", timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram getMe failed: {data}")
    return data["result"]


def telegram_get_updates(token: str, offset: int, timeout: int = 1) -> list[dict]:
    """Poll Telegram for new updates (long polling with short timeout)."""
    resp = requests.get(
        f"{TELEGRAM_API}/bot{token}/getUpdates",
        params={"offset": offset, "timeout": timeout, "allowed_updates": '["message"]'},
        timeout=timeout + 10,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        return []
    return data.get("result", [])


def telegram_send_message(token: str, chat_id: str, text: str, _retries: int = 3) -> None:
    """Send a message to Telegram. Truncates at 4096 chars on a line boundary."""
    suffix = "\n\u2026(truncated)"
    max_len = 4096 - len(suffix)
    if len(text) > 4096:
        # Cut at the last newline before max_len to avoid splitting HTML tags
        cut = text.rfind("\n", 0, max_len)
        if cut <= 0:
            cut = max_len
        text = text[:cut] + suffix
    resp = requests.post(
        f"{TELEGRAM_API}/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        timeout=10,
    )
    if resp.status_code == 429 and _retries > 0:
        retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
        logger.warning("Telegram rate limited, sleeping %ds (%d retries left)", retry_after, _retries)
        time.sleep(retry_after)
        telegram_send_message(token, chat_id, text, _retries=_retries - 1)
        return
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# AgentChattr helpers
# ---------------------------------------------------------------------------

def agentchattr_register(url: str, base: str = "telegram-bridge", label: str = "Telegram Bridge") -> dict:
    """Register the bridge as an API agent. Returns {"name": ..., "token": ...}."""
    resp = requests.post(
        f"{url}/api/register",
        json={"base": base, "label": label},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def agentchattr_send(url: str, token: str, text: str, channel: str = "general") -> int:
    """Send a message to AgentChattr as the bridge agent.

    Returns HTTP status code (200 = success, 403 = stale token, 0 = connection error).
    """
    try:
        resp = requests.post(
            f"{url}/api/send",
            json={"text": text, "channel": channel},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        return resp.status_code
    except requests.RequestException as e:
        logger.warning("Failed to send to AgentChattr: %s", e)
        return 0


def agentchattr_status(url: str) -> dict:
    """Get agent status from AgentChattr (public endpoint, no auth required)."""
    try:
        resp = requests.get(f"{url}/api/status", timeout=5)
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException:
        pass
    return {}


def agentchattr_channels(url: str) -> list[str]:
    """Get list of channels from AgentChattr settings (public endpoint, no auth required)."""
    try:
        resp = requests.get(f"{url}/api/settings", timeout=5)
        if resp.status_code == 200:
            return resp.json().get("channels", [])
    except requests.RequestException:
        pass
    return []


def agentchattr_poll(url: str, token: str, since_id: int, limit: int = 50) -> list[dict]:
    """Poll AgentChattr for messages since the given ID."""
    resp = requests.get(
        f"{url}/api/messages",
        params={"since_id": since_id, "limit": limit},
        headers={"Authorization": f"Bearer {token}"} if token else {},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def _html_escape(text: str) -> str:
    """Escape HTML special chars for Telegram HTML parse mode."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_message(msg: dict) -> str:
    """Format an AgentChattr message for Telegram display.

    Format: [#channel] **sender** (HH:MM):\\nmessage text
    Attachments are appended as links.
    """
    sender = msg.get("sender", "unknown")
    text = msg.get("text", "")
    channel = msg.get("channel", "general")
    msg_type = msg.get("type", "chat")
    time_str = msg.get("time", "")
    attachments = msg.get("attachments", [])

    sender = _html_escape(sender)
    text = _html_escape(text)

    if msg_type == "join":
        return f"<i>{sender} joined</i>"
    if msg_type == "leave":
        return f"<i>{sender} left</i>"

    # Channel prefix (omit for general)
    prefix = f"<b>[#{_html_escape(channel)}]</b> " if channel != "general" else ""
    time_tag = f" <i>({_html_escape(time_str)})</i>" if time_str else ""

    parts = [f"{prefix}<b>{sender}</b>{time_tag}:\n{text}"]

    # Append attachments as links
    for att in attachments:
        name = _html_escape(att.get("name", "attachment"))
        url = att.get("url", "")
        if url:
            # Percent-encode attribute-dangerous chars, preserve & for query strings
            safe_url = url.replace('"', "%22").replace("<", "%3C").replace(">", "%3E")
            parts.append(f'<a href="{safe_url}">{name}</a>')

    return "\n".join(parts)


def parse_channel_prefix(text: str) -> tuple[str | None, str]:
    """Parse optional #channel prefix from operator message.

    '#ops hello' -> ('ops', 'hello')
    'hello' -> (None, 'hello')
    """
    if text.startswith("#"):
        parts = text.split(None, 1)
        if len(parts) >= 1:
            channel = parts[0][1:]  # strip #
            if channel:
                body = parts[1] if len(parts) > 1 else ""
                return channel, body
    return None, text


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def handle_telegram_command(tg_token: str, chat_id: str, url: str, ac_token: str,
                           command: str, sticky_channel: dict) -> None:
    """Handle bot commands from Telegram operator.

    sticky_channel is a mutable dict {"name": "general"} so commands can update it.
    """
    parts = command.strip().split(None, 1)
    cmd = parts[0].lower().split("@")[0]  # strip @botname suffix
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/status":
        status = agentchattr_status(url)
        if not status:
            telegram_send_message(tg_token, chat_id, "<i>No agents connected (or AgentChattr unreachable)</i>")
            return
        lines = ["<b>Agent status:</b>"]
        for name, info in status.items():
            if name == "paused" or not isinstance(info, dict):
                continue
            avail = "online" if info.get("available") else "offline"
            active = " (active)" if info.get("active") else ""
            role = f" [{info['role']}]" if info.get("role") else ""
            lines.append(f"  {name}: {avail}{active}{role}")
        telegram_send_message(tg_token, chat_id, "\n".join(lines))

    elif cmd == "/channels":
        channels = agentchattr_channels(url)
        if not channels:
            telegram_send_message(tg_token, chat_id, "<i>No channels (or AgentChattr unreachable)</i>")
            return
        current = sticky_channel["name"]
        lines = [f"<b>Channels</b> (current: #{_html_escape(current)}):"]
        for c in channels:
            marker = " ←" if c == current else ""
            lines.append(f"  #{_html_escape(c)}{marker}")
        telegram_send_message(tg_token, chat_id, "\n".join(lines))

    elif cmd == "/channel":
        if not arg:
            telegram_send_message(tg_token, chat_id,
                f"Current channel: <b>#{_html_escape(sticky_channel['name'])}</b>\nUsage: /channel &lt;name&gt;")
            return
        sticky_channel["name"] = arg
        telegram_send_message(tg_token, chat_id, f"Default channel set to <b>#{_html_escape(arg)}</b>")

    elif cmd == "/help":
        telegram_send_message(tg_token, chat_id, (
            "<b>Bridge commands:</b>\n"
            "/status — Show connected agents\n"
            "/channels — List channels\n"
            "/channel &lt;name&gt; — Set default channel\n"
            "/help — This message\n\n"
            "<b>Channel routing:</b>\n"
            "<code>#channel-name message</code> — Send to specific channel\n"
            "Plain message — Send to current default channel\n"
            "@mentions are preserved and trigger agent routing."
        ))

    else:
        telegram_send_message(tg_token, chat_id, f"Unknown command: {cmd}. Try /help")


def run(config: dict) -> None:
    """Main polling loop: bidirectional AgentChattr <-> Telegram."""
    token = config["telegram_bot_token"]
    chat_id = config["telegram_chat_id"]
    url = config["agentchattr_url"]
    interval = config["poll_interval"]
    bridge_sender = config["bridge_sender"]
    cursor_file = config["cursor_file"]

    # Verify Telegram bot
    bot_info = telegram_get_me(token)
    bot_id = bot_info.get("id")
    logger.info("Telegram bot: @%s (%s)", bot_info.get("username"), bot_info.get("first_name"))

    # Register with AgentChattr to get Bearer token.
    # Registration endpoint is auth-exempt on loopback, so we register first
    # and then use the Bearer token for all subsequent API calls.
    # Use a mutable dict so the heartbeat thread sees re-registration updates.
    ac = {"token": "", "name": ""}
    try:
        reg = agentchattr_register(url, base=bridge_sender, label="Telegram Bridge")
        ac["token"] = reg.get("token", "")
        ac["name"] = reg.get("name", bridge_sender)
        logger.info("Registered with AgentChattr as '%s' at %s", ac["name"], url)
    except Exception as e:
        logger.warning("Failed to register with AgentChattr: %s — will retry in poll loop", e)

    # Start heartbeat thread to keep presence alive
    def _heartbeat_loop():
        while True:
            if ac["name"] and ac["token"]:
                try:
                    requests.post(
                        f"{url}/api/heartbeat/{ac['name']}",
                        headers={"Authorization": f"Bearer {ac['token']}"},
                        timeout=5,
                    )
                except Exception:
                    pass
            time.sleep(5)

    threading.Thread(target=_heartbeat_loop, daemon=True).start()
    logger.info("Heartbeat thread started (interval=5s)")

    # Load cursors
    last_seen_id, tg_update_offset = load_cursor(cursor_file)

    # Register shutdown handler (with guard to prevent double execution)
    _shutdown_done = [False]

    def shutdown():
        if _shutdown_done[0]:
            return
        _shutdown_done[0] = True
        save_cursor(cursor_file, last_seen_id, tg_update_offset)
        # Deregister from AgentChattr to free the slot
        if ac["name"] and ac["token"]:
            try:
                requests.post(
                    f"{url}/api/deregister/{ac['name']}",
                    headers={"Authorization": f"Bearer {ac['token']}"},
                    timeout=5,
                )
                logger.info("Deregistered '%s' from AgentChattr", ac["name"])
            except Exception:
                logger.debug("Deregistration failed (server may be down)")
        logger.info("Cursors saved on shutdown")

    atexit.register(shutdown)

    # Handle signals for graceful shutdown
    def signal_handler(signum, frame):
        logger.info("Received signal %d, shutting down...", signum)
        shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Sticky default channel for operator messages (mutable dict for closure access)
    sticky_channel = {"name": "general"}

    logger.info("Starting poll loop (interval=%ds, ac_cursor=%d, tg_offset=%d)", interval, last_seen_id, tg_update_offset)

    while True:
        # --- Read path: AgentChattr -> Telegram ---
        try:
            messages = agentchattr_poll(url, ac["token"], last_seen_id)

            for msg in messages:
                msg_id = msg.get("id", 0)
                sender = msg.get("sender", "")

                # Skip messages from the bridge itself to prevent echo loops
                if sender in (bridge_sender, ac["name"]):
                    last_seen_id = max(last_seen_id, msg_id)
                    continue

                # Skip system routing messages
                if sender == "system" and msg.get("type") == "chat":
                    text = msg.get("text", "")
                    if "auto-recovered" in text or "interrupted" in text:
                        last_seen_id = max(last_seen_id, msg_id)
                        continue

                formatted = format_message(msg)
                telegram_send_message(token, chat_id, formatted)

                last_seen_id = max(last_seen_id, msg_id)

        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (401, 403):
                logger.warning("AgentChattr token rejected (HTTP %d) — re-registering", e.response.status_code)
                try:
                    reg = agentchattr_register(url, base=bridge_sender, label="Telegram Bridge")
                    ac["token"] = reg.get("token", "")
                    ac["name"] = reg.get("name", bridge_sender)
                    logger.info("Re-registered with AgentChattr as '%s'", ac["name"])
                except Exception:
                    logger.warning("Re-registration failed — will retry next cycle")
            else:
                logger.warning("AgentChattr poll error (will retry): %s", e)
        except requests.RequestException as e:
            logger.warning("AgentChattr poll error (will retry): %s", e)
        except Exception:
            logger.exception("Unexpected error in read path")

        # --- Write path: Telegram -> AgentChattr ---
        try:
            updates = telegram_get_updates(token, tg_update_offset)

            for update in updates:
                update_id = update.get("update_id", 0)
                tg_update_offset = max(tg_update_offset, update_id + 1)

                msg = update.get("message")
                if not msg:
                    continue

                # Security: only accept messages from the configured chat
                msg_chat_id = str(msg.get("chat", {}).get("id", ""))
                if msg_chat_id != str(chat_id):
                    logger.debug("Ignoring message from unauthorized chat %s", msg_chat_id)
                    continue

                # Ignore messages from the bot itself
                from_id = msg.get("from", {}).get("id")
                if from_id == bot_id:
                    continue

                text = msg.get("text", "")
                if not text:
                    continue

                # Handle bot commands
                if text.startswith("/"):
                    handle_telegram_command(token, chat_id, url, ac["token"], text, sticky_channel)
                    continue

                # Forward to AgentChattr as the bridge agent
                if not ac["token"]:
                    # Try to register if we don't have a token yet
                    try:
                        reg = agentchattr_register(url, base=bridge_sender, label="Telegram Bridge")
                        ac["token"] = reg.get("token", "")
                        ac["name"] = reg.get("name", bridge_sender)
                        logger.info("Registered with AgentChattr as '%s'", ac["name"])
                    except Exception:
                        telegram_send_message(token, chat_id, "<i>AgentChattr not available — message not sent</i>")
                        continue

                # Parse optional #channel prefix, fall back to sticky default
                channel_override, body = parse_channel_prefix(text)
                target_channel = channel_override or sticky_channel["name"]

                if not body:
                    continue  # empty message after stripping prefix

                status = agentchattr_send(url, ac["token"], body, channel=target_channel)
                if status == 200:
                    pass  # success
                elif status in (401, 403):
                    # Token expired/invalid — clear and re-register on next message
                    logger.warning("AgentChattr rejected token (HTTP %d) — will re-register", status)
                    ac["token"] = ""
                    ac["name"] = ""
                    telegram_send_message(token, chat_id, "<i>Session expired — re-registering. Please resend.</i>")
                else:
                    telegram_send_message(token, chat_id, "<i>Failed to send to AgentChattr (is it running?)</i>")

        except requests.RequestException as e:
            logger.warning("Telegram poll error (will retry): %s", e)
        except Exception:
            logger.exception("Unexpected error in write path")

        # Persist cursors
        save_cursor(cursor_file, last_seen_id, tg_update_offset)

        time.sleep(interval)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="AgentChattr -> Telegram bridge")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    parser.add_argument("--config", "-c", type=str, default=None, help="Path to config.toml")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    config = load_config(args.config)
    validate_config(config)
    run(config)


if __name__ == "__main__":
    main()
