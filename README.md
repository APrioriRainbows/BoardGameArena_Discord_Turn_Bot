# BGA Discord Turn Bot

A Discord bot that @mentions players when it's their turn in a Board Game Arena game, and posts final scores when the game ends.

Runs on a server or VM you control. Connects to BGA in spectator mode (no BGA login required).

## Requirements

- Python 3.10 or newer
- A Discord bot token: create one at [discord.com/developers/applications](https://discord.com/developers/applications).
  1. Click **New Application**, give it a name, then go to the **Bot** tab.
  2. Click **Reset Token** and copy the value into `DISCORD_TOKEN` in your `.env`.
  3. To invite the bot to your server, go to **Installation** (or **OAuth2 > URL Generator**), enable the `bot` and `applications.commands` scopes, and grant these permissions: **View Channels**, **Send Messages**, **Embed Links**, **Read Message History**. No privileged intents are required.
  4. Copy the generated URL, open it in a browser, and select your server.
- A server or VM that stays online (e.g. Google Cloud e2-micro free tier, Oracle Cloud free tier, Railway)

## Setup

```bash
git clone https://github.com/APrioriRainbows/BoardGameArena_Discord_Turn_Bot
cd BoardGameArena_Discord_Turn_Bot
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -e .
cp .env.example .env
# fill in DISCORD_TOKEN and DISCORD_GUILD_ID in .env
python -m bga_turn >> bga-bot.log 2>&1 &
```

### `.env` variables

| Variable | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | Yes | Bot token from the Discord Developer Portal |
| `DISCORD_GUILD_ID` | Recommended | Your server ID. Required for instant slash command sync. To find it: in Discord open **Settings > Advanced** and turn on **Developer Mode**, then right-click your server name and click **Copy Server ID**. |
| `DISCORD_CLEAR_GLOBAL_COMMANDS` | No | Set to `1` once to clear stale global commands, then back to `0` |
| `BGA_POLL_SECONDS` | No | How often to check for table changes (default: 15) |
| `BGA_DB_PATH` | No | SQLite file path (default: `bga_bot.db`) |
| `BGA_WS_URL` | No | BGA websocket endpoint |
| `BGA_ENABLE_TABLEINFOS_FALLBACK` | No | Set to `1` to enable the legacy HTTP fallback for end-of-game detection |
| `LOG_LEVEL` | No | `INFO` (default) or `DEBUG` for raw BGA event logging |

### Discord bot permissions

When inviting the bot to your server, it needs: `View Channels`, `Send Messages`, `Embed Links`, `Read Message History`.

## Commands

All commands are under `/bga`. Run `/bga info` in Discord for full usage details.

| Command | What it does |
|---|---|
| `/bga configure` | Add a game to watch, or edit settings for an existing one |
| `/bga link-all` | Bulk-link BGA players to Discord accounts using dropdowns |
| `/bga link-member` | Manually link one BGA player to a Discord account |
| `/bga unlink-member` | Remove a player link |
| `/bga linked` | List all current player links |
| `/bga unwatch` | Stop watching a table |
| `/bga unwatch-all` | Stop watching all tables in the server |
| `/bga watchlist` | List all tables currently being watched |
| `/bga turn` | Manually repost the current turn status |
| `/bga status` | Show the last known turn state (only visible to you) |
| `/bga logs` | Dump recent raw BGA events (for debugging) |
| `/bga info` | Full command reference and usage details |

## Notes

- BGA tables must be set to **public** (spectator mode on) for the bot to see them
- The bot must keep running to keep watching tables
- Final score extraction is best-effort; use `/bga logs` if scores are missing after a game ends
