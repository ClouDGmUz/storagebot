# storagebot

Telegram bot that turns a forum group (group with Topics) into personal storage.
Send anything to the bot in private → it lands in a topic of your group.

## Setup

1. Create bot via @BotFather → copy token.
2. `pip install -r requirements.txt`
3. `cp .env.example .env`, put `BOT_TOKEN` (optionally `ALLOWED_USER_IDS`).
4. `python bot.py`
5. DM bot `/start` (first user becomes owner if `ALLOWED_USER_IDS` empty).
6. Create a group → Settings → enable **Topics**.
7. Add bot as **admin** with **Manage topics** and **Delete messages** permissions.
8. Send `/bind` in the group.
9. Optional: in @BotFather run `/setinline` to enable inline search (`@yourbot query` in any chat).

### Docker

```bash
cp .env.example .env   # fill BOT_TOKEN
docker compose up -d --build
docker compose logs -f
```

The database is kept in the `storagebot-data` volume (`/data/storage.db`), so it survives rebuilds.

### Railway

1. New Project → Deploy from GitHub repo → pick this repo (it builds from the `Dockerfile`).
2. **Variables**: add `BOT_TOKEN` (and optionally `ALLOWED_USER_IDS`).
3. **Add a Volume** to the service with mount path `/data`. Without it the database is wiped on every redeploy.
4. Run only **one** replica: Telegram polling allows one bot instance at a time.

## Usage

Send to the bot in private:

| You send | Goes to topic |
|---|---|
| text with `#tag` | `tag` (`#work_stuff` → "work stuff") |
| photo | Photos |
| video / gif | Videos |
| document | Files |
| audio | Music |
| voice / video note | Voice |
| text with link | Links |
| plain text | Notes |
| sticker | Stickers |

Topics are created automatically. Albums stay grouped. Forwarded messages keep the "Forwarded from" header.
Deleted topic? Recreated on next save.

Every save reply has **📂 Move** and **🗑 Delete** buttons.

Things you post straight into a topic of the group are indexed too, so `/find` and inline search see them. Topics you create or rename by hand in the group are picked up automatically.

### Commands (private chat)

| Command | What it does |
|---|---|
| `/move <topic>` | Move an item. Reply to the saved message (or the bot's ✅ reply); without a reply it moves the last save. With no topic you get a picker. |
| `/delete` | Delete an item (reply, or the last save), with a confirmation |
| `/topics` | List topics with item counts |
| `/new <name>` | Create topic |
| `/rename <old> > <new>` | Rename topic (in Telegram too) |
| `/deltopic <name>` | Delete topic and everything in it, with a confirmation |
| `/use <name>` / `/auto` | Send everything to one topic / go back to auto-sorting |
| `/forget <name>` | Unlink a topic from the bot (the next save creates a fresh one) |
| `/find <text>` | Search text, captions, file names and topic names |
| `/recent [topic]` | Last 10 saves |
| `/random [topic]` | Bot sends you back a random saved item |
| `/stats` | Counts by type, biggest topics, DB size |
| `/backup` / `/restore` | See below |
| `/cancel` | Abort a pending restore or new-topic prompt |

Topic names can be shortened: `/recent pho` matches "Photos" if that's the only match.

Inline mode: type `@yourbot cat` in any chat to share a saved photo, file, video, voice, sticker or note. Only owners get results.

### Backup / restore

- `/backup` — bot sends you a snapshot of the `.db` file.
- `/restore` — then send the `.db` file (or send the file with caption `/restore`, or reply `/restore` to it). `/cancel` aborts.
  The file is checked first (SQLite integrity check plus the required tables). The current DB is saved as `storage.before-restore-<time>.db` next to it, so you can undo a restore.

Data: `storage.db` (SQLite) keeps topic ↔ thread mapping and an index of saved items for `/find`. The actual content lives in Telegram.

## License

[MIT](LICENSE)
