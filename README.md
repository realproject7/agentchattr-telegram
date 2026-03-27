# AgentChattr Telegram Bridge

A bidirectional bridge between [AgentChattr](https://github.com/bcurts/agentchattr) and Telegram. Monitor and control your AI coding agents from your phone.

![Web UI](screenshots/web-ui.png)
![Telegram](screenshots/telegram.png)

## What it does

- **Read path**: Relays AgentChattr messages to your Telegram chat in real time
- **Write path**: Send messages from Telegram back to AgentChattr (with @mentions and channel routing)
- **Channel routing**: Prefix messages with `#channel-name` or use `/channel <name>` to switch channels
- **Operator commands**: `/status`, `/channels`, `/channel <name>`, `/help`
- **State persistence**: Cursor tracking so no messages are lost on restart

## Architecture

Single Python script (~600 lines), no dependencies beyond `requests`. Registers with AgentChattr as a regular agent via the REST API — no modifications to AgentChattr needed.

```
Telegram Bot API  <-->  telegram_bridge.py  <-->  AgentChattr REST API
 (getUpdates)            (polling both)          (/api/messages, /api/send)
 (sendMessage)                                   (/api/register, /api/heartbeat)
```

## Setup

### 1. Create a Telegram bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow the prompts
3. Copy the bot token (e.g., `123456:ABC-DEF...`)

### 2. Get your chat ID

1. Send any message to your new bot
2. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser
3. Find `"chat":{"id":123456789}` — that's your chat ID

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure

**Option A: Environment variables**

```bash
export TELEGRAM_BOT_TOKEN="your-bot-token"
export TELEGRAM_CHAT_ID="your-chat-id"
# Optional:
export AGENTCHATTR_URL="http://127.0.0.1:8300"
export POLL_INTERVAL="2"
```

**Option B: AgentChattr config.toml**

Add a `[telegram]` section to your existing `config.toml`:

```toml
[telegram]
bot_token = "your-bot-token"
chat_id = "your-chat-id"
agentchattr_url = "http://127.0.0.1:8300"
poll_interval = 2
```

Environment variables always override config.toml values.

### 5. Run

```bash
# If using config.toml:
python3 telegram_bridge.py --config /path/to/config.toml

# If using env vars:
python3 telegram_bridge.py

# With debug logging:
python3 telegram_bridge.py -v
```

## Usage

### Sending messages

Messages you type in Telegram are forwarded to AgentChattr's default channel (`general`).

To send to a specific channel, prefix your message:

```
#discussion Hey team, what's the status?
```

To change the sticky default channel:

```
/channel ops-growth
```

All subsequent messages without a `#prefix` will go to `ops-growth`.

### @mentions

`@mentions` in your Telegram messages are preserved and trigger AgentChattr's agent routing. For example:

```
@t3 please fix the login bug
```

### Commands

| Command | Description |
|---------|-------------|
| `/status` | Show connected agents and their state |
| `/channels` | List available channels |
| `/channel <name>` | Set default channel |
| `/help` | Show help message |

## How it works

1. **Registers** with AgentChattr as a regular agent (gets a Bearer token)
2. **Polls** AgentChattr `/api/messages` for new messages (configurable interval, default 2s)
3. **Polls** Telegram `getUpdates` for operator messages
4. **Formats** and relays messages between both platforms
5. **Persists** cursors to a JSON file so no messages are lost on restart
6. **Sends heartbeats** every 5s to keep the bridge's presence alive in AgentChattr
7. **Deregisters** on shutdown (SIGINT/SIGTERM) to free the agent slot

## Requirements

- Python 3.10+
- AgentChattr running locally (or accessible via network)
- A Telegram bot token and chat ID

## License

MIT
