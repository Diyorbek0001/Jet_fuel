# Fuel Monitoring Telegram Bot

Production-ready Python 3.11+ Telegram bot for monitoring Samsara vehicle fuel levels.

## What it does

- Fetches Samsara vehicle fuel percentages from `GET https://api.samsara.com/fleet/vehicles/stats/feed`
- Requests the Samsara `fuelPercents` stat type
- Persists Samsara `endCursor` and reuses it as `after` on future checks
- Shows all trucks at or below the configured threshold
- Lets dispatchers create, update, and clear notes
- Alerts only when fuel is low and there is no active note
- Avoids repeated alert spam by waiting `REPEAT_ALERT_MINUTES` before re-alerting the same unresolved unit
- Auto-clears a note after fuel increases enough or reaches the configured full level
- Stores everything in SQLite

The Samsara API token must include **Read Vehicle Statistics** permission.

## Setup

```bash
cd /Users/kirahopkins/Desktop/Fuel_DMR
python3.11 -m venv .venv
. .venv/bin/activate
pip install -r fuel_bot/requirements.txt
cp fuel_bot/.env.example .env
```

Fill in `.env`:

```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
SAMSARA_API_TOKEN_1=your_first_samsara_token
SAMSARA_API_TOKEN_2=your_second_samsara_token
SAMSARA_API_TOKEN_3=your_third_samsara_token
ALERT_CHAT_ID=
CHECK_INTERVAL_MINUTES=15
REPEAT_ALERT_MINUTES=29
FUEL_THRESHOLD=60
AUTO_CLEAR_INCREASE=30
AUTO_CLEAR_FULL_LEVEL=85
```

When multiple tokens are configured, the bot tries them in order and moves to
the next token if one is unauthorized.

Run the bot:

```bash
python -m fuel_bot.main
```

In the Telegram group where alerts should appear, run:

```text
/set_alert_chat
```

The bot saves that chat ID in SQLite. You can also copy the printed ID into `.env` as `ALERT_CHAT_ID`.

## Commands

Run `/start` to open the button menu. The main workflow is available through
Telegram buttons:

- `⛽ Low Fuel`
- `🚨 Needs Notes`
- `📝 Active Notes`
- `🔄 Check Fuel`
- `➕ Add Note`
- `✅ Clear Note`

Slash commands are still available as a backup:

- `/start` - Show help
- `/fuel` - Refresh from Samsara and list trucks at or below 60%, lowest first
- `/fuel_attention` - Show only low-fuel trucks without active notes
- `/note UNIT NOTE_TEXT` - Add or update a dispatcher note
- Reply to a fuel alert with `/note NOTE_TEXT` - Save a note for that alert's unit
- `/clear_note UNIT` - Manually clear a note
- Reply to a fuel alert or saved-note confirmation with `/clear_note` - Clear that unit's note
- `/notes` - Show active notes
- `/checkfuel` - Manually run the fuel check and alert logic
- `/testfuel UNIT FUEL_PERCENT` - Simulate a fuel reading without Samsara
- `/set_alert_chat` - Save the current Telegram chat as the alert destination

When multiple Samsara tokens are configured, `/checkfuel` reports readings per
token plus the merged unit count so you can confirm all configured APIs are
being queried.

## Test flow before Samsara

```text
/set_alert_chat
/testfuel 1002 45
/note 1002 Sent to station
/testfuel 1002 70
/testfuel 1002 45
/testfuel 1002 75
```

Expected behavior:

- First test is below threshold with no note, so it alerts.
- After the note is added, the same low fuel should not alert.
- With `AUTO_CLEAR_INCREASE=30`, 70 from a note created at 45 is +25, so it does not clear yet.
- When fuel reaches 75 from a note created at 45, the +30% increase clears the note automatically.

## Database

SQLite is auto-created at `fuel_bot.sqlite3` in the project root unless `DATABASE_PATH` is set.

Tables:

- `unit_notes`
- `fuel_states`
- `app_config`
- `alert_events`

## Deployment notes

Use a process manager such as systemd, Supervisor, Docker, or a hosted worker platform. Keep `.env` outside source control and rotate tokens if they are exposed.
