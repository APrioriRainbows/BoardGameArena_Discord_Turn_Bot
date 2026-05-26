# BGA Discord Turn Bot

A Discord bot that pings players when it is their turn in a Board Game Arena game.

The bot runs on a server or virtual machine you control. It connects to BGA in spectator mode (no login required) and posts turn notifications in a Discord channel or thread with @mentions, emojis, and a final score announcement when the game ends.

## How it works

1. You run the bot on a server (Linux/macOS/Windows, needs to stay online)
2. In BGA, set your table to public so spectators can view it
3. In Discord, run `/bga configure` from the channel where you want notifications, paste the table URL, and pick your notification settings
4. Run `/bga link-all` to match each BGA player to their Discord account
5. The bot @mentions the right person when it is their turn, and posts final scores when the game ends

That is the whole setup. Everything after step 4 is automatic.

## What the bot posts

**When it is someone's turn:**
```
⏳ @Alice, you're up in Scythe!
✅ Bob just completed their turn.
Table 854745799: https://boardgamearena.com/6/scythe?table=854745799
[Show players]
```

The **Show players** button is a private expand that lists all players and the table link. Only the person who clicks it can see the response.

**When the game ends:**
```
🎉 Game over! Scythe (Table 854745799) has ended!

🏆 Final scores:
🥇 Alice - 142
🥈 Bob - 118
🥉 Charlie - 97

🦄 ⭐ 🎉
```

Score extraction is best-effort. If scores are not available for a particular game, the announcement still posts with the player list.

## Quick start

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
# edit .env with your token and server ID
python -m bga_turn >> bga-bot.log 2>&1 &
```

### Windows PowerShell

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
Copy-Item .env.example .env
# edit .env with your token and server ID
python -m bga_turn
```

## Deployment

### Requirements

- Python 3.10 or newer
- A Discord bot created in the Discord developer portal
- The bot invited to your server with `Send Messages`, `Embed Links`, `Read Message History`, and `View Channels` permissions
- One or more BGA tables publicly accessible in spectator mode

### Project structure

- `src/bga_turn/app.py`: application entry point
- `src/bga_turn/commands_bga.py`: `/bga` slash commands
- `src/bga_turn/bga_client.py`: public BGA networking, HTML parsing, websocket handling
- `src/bga_turn/monitor.py`: watch loop, Discord publishing, and game-over announcements
- `src/bga_turn/database.py`: SQLite persistence
- `src/bga_turn/models.py`: domain dataclasses
- `src/bga_turn/schema.sql`: packaged SQLite schema
- `pyproject.toml`: package metadata and console entry point
- `.env.example`: configuration template

### Local installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Then edit `.env`:

```env
DISCORD_TOKEN=paste_your_bot_token_here
DISCORD_GUILD_ID=paste_your_server_id_here
DISCORD_CLEAR_GLOBAL_COMMANDS=0
BGA_POLL_SECONDS=15
BGA_DB_PATH=bga_bot.db
BGA_WS_URL=wss://ws-x1.boardgamearena.com/connection/websocket
BGA_ENABLE_TABLEINFOS_FALLBACK=0
LOG_LEVEL=INFO
```

### `.env` variables

- `DISCORD_TOKEN`: required; copy from the Discord Developer Portal under Bot
- `DISCORD_GUILD_ID`: strongly recommended during setup; paste your server ID for instant slash command sync (see below). If omitted, commands sync globally and may take up to an hour to appear.
- `DISCORD_CLEAR_GLOBAL_COMMANDS`: set to `1` once to delete stale global commands, then back to `0`
- `BGA_POLL_SECONDS`: how often the monitor checks for table changes (default 15)
- `BGA_DB_PATH`: SQLite file path
- `BGA_WS_URL`: public BGA websocket endpoint
- `BGA_ENABLE_TABLEINFOS_FALLBACK`: set to `1` to re-enable the legacy HTTP tableinfos fallback for end-of-game detection (disabled by default)
- `LOG_LEVEL`: `INFO` for normal use, `DEBUG` to capture raw BGA event data

### Discord setup

#### 1. Create the bot

1. Go to `https://discord.com/developers/applications`
2. Click `New Application`, give it a name
3. Go to the `Bot` tab
4. Click `Reset Token` and copy the value into `DISCORD_TOKEN` in your `.env`
5. Keep this token secret. If it is ever exposed, regenerate it immediately.
6. No privileged intents are required.

#### 2. Invite the bot to your server

1. In the Developer Portal, open the `Installation` page
2. Under `Guild Install`, make sure the install link includes `bot` and `applications.commands`
3. Bot permissions needed: `View Channels`, `Send Messages`, `Embed Links`, `Read Message History`
4. Copy the install link, open it in a browser, and choose your server

If the portal shows `OAuth2 > URL Generator` instead of `Installation`, use that to generate the URL with the same scopes and permissions.

#### 3. Get your server ID (`DISCORD_GUILD_ID`)

1. Open Discord settings (click your avatar, bottom left)
2. Go to `Advanced` in the left sidebar
3. Enable `Developer Mode`
4. Right-click your server name and click `Copy Server ID`
5. Paste into `DISCORD_GUILD_ID` in your `.env`

### Running the bot

```bash
python -m bga_turn
```

To run in the background and log to a file (recommended for persistent hosting):

```bash
python -m bga_turn >> bga-bot.log 2>&1 &
```

## Discord commands

All commands are under `/bga`. Run `/bga info` in Discord to see the full list at any time.

### Setup

#### `/bga configure`

Add a new game to watch, or edit settings for an existing one. This is the main setup command.

When no games are being watched, it opens a modal to paste a BGA table URL. After submitting:

- Pick a channel or thread for notifications (defaults to the channel you ran the command from)
- Choose which notification types to enable (all on by default)
- Click **Start watching**

If games are already being watched, it shows a list with an **Edit settings** button and an **Add new game** button.

**Notification types:**
- **Turn notifications** (⏳): ping the active player when it is their turn
- **Turn recap** (✅): show who just completed their turn
- **Final results** (🎉): post scores when the game ends

### Watching games

#### `/bga unwatch <table_id_or_url>`

Stop watching a table.

#### `/bga unwatch-all`

Stop watching all tables in the server. Requires `Manage Server` or `Administrator`.

#### `/bga watchlist`

List all tables currently being watched.

#### `/bga turn`

Manually post the current turn status to the channel. Useful if the automatic message was dismissed or missed.

#### `/bga status`

Show the last known turn state for all watched tables. Only visible to you.

### Linking players

Linking maps a BGA username to a Discord account so the bot can @mention the right person.

#### `/bga link-all`

Bulk-link all BGA players from your watched games to Discord members in one step.

The bot shows a searchable Discord member dropdown for each unlinked BGA player. Select the right Discord account for each player and the link is saved immediately. Supports up to 4 players per page with Previous/Next navigation for larger groups.

#### `/bga link-member <discord_user> <bga_name> [bga_id]`

Manually link one player.

```
/bga link-member @Alice alice_bga
```

The link can be partial: name only, ID only, or both. The bot fills in missing fields automatically when it sees the player on a watched table.

#### `/bga unlink-member <discord_user>`

Remove a link.

#### `/bga linked`

List all current Discord/BGA links.

### Other commands

#### `/bga info`

Show a full command reference. Only visible to you.

#### `/bga logs`

Show the last 30 raw BGA websocket events. Useful for debugging move data or verifying what the bot sees during a game. Requires `LOG_LEVEL=DEBUG` in `.env` and the bot started with output redirected to `bga-bot.log`.

## Technical overview

### BGA network flow

The bot does not use cookies, browser sessions, or BGA login.

#### 1. Load the public table page

The bot downloads the public table URL and extracts:
- Anonymous spectator identity (`user_id`, `credentials`) for the websocket handshake
- Known player names from the HTML bootstrap
- Initial game state when available (`gamestate.active_player`)

#### 2. Open the public websocket

The bot connects to the public BGA websocket and replays the Centrifugo handshake:
- `connect`
- `subscribe bgamsg`
- `subscribe /general/emergency`
- `subscribe /player/p<visitor_id>`
- `subscribe /table/t<TABLE_ID>`
- `presence /table/t<TABLE_ID>`

#### 3. Interpret events

The bot reconstructs `waiting_ids` in this priority order:

1. `gameStateMultipleActiveUpdate`
2. `gameStateChange.active_player` for single-active-player games
3. `yourturnack` as a fallback
4. Limited public heuristics (`beginTurn`, `endPrivateAction`, etc.)

End-of-game detection uses:

1. `tableInfosChanged` with `status = finished` or `reload_reason = tableDestroy`
2. End-of-game events in the public stream (`simpleNote`, `simpleNode` with "end of game")
3. Optionally: `tableinfos.html` HTTP fallback (disabled by default, enable with `BGA_ENABLE_TABLEINFOS_FALLBACK=1`)

When a game ends, the bot fetches the public `tableinfos.html` result payload to extract final scores and posts a game-over announcement before removing the watch.

#### 4. Single-active vs multi-active games

- For `activeplayer` states, the bot can initialize from the HTML bootstrap immediately
- For `multipleactiveplayer` states, it waits for `gameStateMultipleActiveUpdate` websocket events

### Discord message behavior

For each watched table:
- A message is created when an active turn starts
- It is edited while the waiting list shrinks (multi-active games)
- It is deleted and a new one is created when the next player's turn begins
- When the game ends, the active message is deleted, a results announcement is posted, and the watch is removed automatically

On startup, the bot scans the notification channel history for an existing turn message for each watched table and tracks it rather than deleting and reposting. This prevents duplicate notifications when the bot restarts mid-game.

### SQLite schema

Three tables:

**`users`**: Discord-to-BGA player mapping
- `discord_user_id`, `bga_player_id`, `bga_player_name`

**`watch_subscriptions`**: watched tables per guild/channel
- `subscription_id`, `table_id`, `table_url`, `guild_id`, `channel_id`, `created_by_discord_user_id`, `notification_flags`

**`watch_states`**: last known state per watch
- `subscription_id`, `last_packet_id`, `last_waiting_ids`, `last_player_names`, `is_initialized`, `game_name`

## Notes and limitations

- Tables must be set to public (spectator mode on) in BGA for the bot to see them
- The bot is self-hosted: it must keep running to keep watching tables
- Game names come from the BGA URL slug or public bootstrap and are not always perfectly formatted
- Final score extraction is best-effort and may not work for all games; use `/bga logs` after a game ends to see the raw result payload if scores are missing
- Discord voice warnings (`PyNaCl`, `davey`) are not relevant for this project
- The `message content intent` warning is not blocking; the bot relies on slash commands only
