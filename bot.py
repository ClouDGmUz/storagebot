import asyncio
import html
import logging
import os
import random
import re
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, Filter
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    Document,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultCachedAudio,
    InlineQueryResultCachedDocument,
    InlineQueryResultCachedGif,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedSticker,
    InlineQueryResultCachedVideo,
    InlineQueryResultCachedVoice,
    InputTextMessageContent,
    Message,
)
from dotenv import load_dotenv

from db import DB

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}
DB_PATH = os.getenv("DB_PATH", "storage.db")
ALBUM_WAIT = 1.2  # seconds to collect album parts
MAX_RESTORE_SIZE = 20 * 1024 * 1024  # Bot API getFile limit
MAX_TOPIC_BUTTONS = 90

TOPIC_COLORS = [7322096, 16766590, 13338331, 9367192, 16749490, 16478047]
HASHTAG_RE = re.compile(r"(?<![\w#])#([\w\-]{1,64})")
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)

db = DB(DB_PATH)
router = Router()
log = logging.getLogger("storagebot")

_albums: dict[str, list[Message]] = defaultdict(list)
_topic_lock = asyncio.Lock()
_awaiting_restore: set[int] = set()
_awaiting_topic_name: dict[int, tuple[int, int, int]] = {}  # user -> (batch, report chat, report msg)


# ---------- access ----------

def owners() -> set[int]:
    if ALLOWED:
        return ALLOWED
    owner = db.get("owner_id")
    return {int(owner)} if owner else set()


class IsOwner(Filter):
    """Works for messages, callback queries and inline queries."""

    async def __call__(self, event) -> bool:
        return event.from_user is not None and event.from_user.id in owners()


class InStorageGroup(Filter):
    async def __call__(self, m: Message) -> bool:
        return group_id() is not None and m.chat.id == group_id()


# ---------- callback data ----------

class ItemCB(CallbackData, prefix="it"):
    action: str  # move | new | back | del | delok
    batch: int


class MoveCB(CallbackData, prefix="mv"):
    batch: int
    thread: int


class TopicCB(CallbackData, prefix="tp"):
    action: str  # delok | cancel
    thread: int


def item_kb(batch: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📂 Move", callback_data=ItemCB(action="move", batch=batch).pack()),
        InlineKeyboardButton(text="🗑 Delete", callback_data=ItemCB(action="del", batch=batch).pack()),
    ]])


def move_kb(batch: int, current_thread: int | None) -> InlineKeyboardMarkup:
    topics = [t for t in db.topics() if t["thread_id"] != current_thread][:MAX_TOPIC_BUTTONS]
    buttons = [
        InlineKeyboardButton(text=t["name"][:40], callback_data=MoveCB(batch=batch, thread=t["thread_id"]).pack())
        for t in topics
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([
        InlineKeyboardButton(text="➕ New topic", callback_data=ItemCB(action="new", batch=batch).pack()),
        InlineKeyboardButton(text="« Back", callback_data=ItemCB(action="back", batch=batch).pack()),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_delete_kb(batch: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Yes, delete", callback_data=ItemCB(action="delok", batch=batch).pack()),
        InlineKeyboardButton(text="« Back", callback_data=ItemCB(action="back", batch=batch).pack()),
    ]])


# ---------- helpers ----------

def group_id() -> int | None:
    gid = db.get("group_id")
    return int(gid) if gid else None


def link(thread_id: int | None, message_id: int | None = None) -> str:
    """t.me/c link for private supergroup message/topic."""
    gid = str(group_id())
    internal = gid[4:] if gid.startswith("-100") else gid.lstrip("-")
    if message_id is None:
        return f"https://t.me/c/{internal}/{thread_id or 1}"
    if thread_id:
        return f"https://t.me/c/{internal}/{thread_id}/{message_id}"
    return f"https://t.me/c/{internal}/{message_id}"


def kind_of(m: Message) -> str:
    if m.photo:
        return "photo"
    if m.video or m.animation:
        return "video"
    if m.video_note or m.voice:
        return "voice"
    if m.audio:
        return "audio"
    if m.document:
        return "file"
    if m.sticker:
        return "sticker"
    if m.text:
        return "link" if URL_RE.search(m.text) or _has_url_entity(m) else "text"
    return "other"


def media_of(m: Message) -> tuple[str | None, str | None]:
    """(media type, file_id) — used for inline search results."""
    if m.photo:
        return "photo", m.photo[-1].file_id
    if m.animation:  # check before document: GIFs also carry .document
        return "animation", m.animation.file_id
    for attr in ("video", "video_note", "voice", "audio", "document", "sticker"):
        obj = getattr(m, attr)
        if obj:
            return attr, obj.file_id
    return None, None


def _has_url_entity(m: Message) -> bool:
    return any(e.type in ("url", "text_link") for e in (m.entities or []))


DEFAULT_TOPICS = {
    "photo": "Photos",
    "video": "Videos",
    "voice": "Voice",
    "audio": "Music",
    "file": "Files",
    "sticker": "Stickers",
    "link": "Links",
    "text": "Notes",
    "other": "Other",
}


def searchable_text(m: Message) -> str | None:
    parts = [m.text or m.caption or ""]
    if m.document and m.document.file_name:
        parts.append(m.document.file_name)
    if m.audio:
        parts.append(" ".join(filter(None, [m.audio.performer, m.audio.title, m.audio.file_name])))
    if m.forward_origin:
        chat = getattr(m.forward_origin, "chat", None) or getattr(m.forward_origin, "sender_user", None)
        if chat:
            parts.append(getattr(chat, "title", None) or getattr(chat, "full_name", "") or "")
    text = " ".join(p for p in parts if p).strip()
    return text or None


def item_row(m: Message, new_message_id: int, src: bool = True) -> dict:
    media, file_id = media_of(m)
    return {
        "message_id": new_message_id,
        "kind": kind_of(m),
        "text": searchable_text(m),
        "src_chat_id": m.chat.id if src else None,
        "src_message_id": m.message_id if src else None,
        "forwarded": m.forward_origin is not None,
        "media": media,
        "file_id": file_id,
    }


def pick_topic(m: Message) -> str:
    """#hashtag > sticky /use topic > auto by content type."""
    tag = HASHTAG_RE.search(m.text or m.caption or "")
    if tag:
        return tag.group(1).replace("_", " ")
    sticky = db.get("sticky")
    if sticky:
        return sticky
    return DEFAULT_TOPICS[kind_of(m)]


def find_topic(name: str):
    """Exact (case-insensitive) match, else unique prefix match. Row has name, thread_id, n."""
    rows = db.topics()
    exact = [t for t in rows if t["name"].lower() == name.lower()]
    if exact:
        return exact[0]
    matches = [t for t in rows if t["name"].lower().startswith(name.lower())]
    return matches[0] if len(matches) == 1 else None


async def ensure_topic(bot: Bot, name: str, force_new: bool = False) -> int:
    async with _topic_lock:
        row = db.topic(name)
        if row and not force_new:
            return row["thread_id"]
        topic = await bot.create_forum_topic(
            chat_id=group_id(), name=name[:128], icon_color=random.choice(TOPIC_COLORS)
        )
        db.save_topic(name, topic.message_thread_id)
        return topic.message_thread_id


def _thread_gone(e: TelegramBadRequest) -> bool:
    msg = e.message.lower()
    return "thread not found" in msg or "topic_deleted" in msg or "topic was deleted" in msg


async def transfer(bot: Bot, topic: str, from_chat: int, ids: list[int], forwarded: bool) -> tuple[int, list[int]]:
    """Copy (or forward, to keep source header) messages into topic. Recreates deleted topics."""
    for attempt in range(2):
        thread_id = await ensure_topic(bot, topic, force_new=attempt > 0)
        try:
            send = bot.forward_messages if forwarded else bot.copy_messages
            res = await send(group_id(), from_chat, ids, message_thread_id=thread_id)
            return thread_id, [r.message_id for r in res]
        except TelegramBadRequest as e:
            if attempt == 0 and _thread_gone(e):
                log.info("topic %r was deleted, recreating", topic)
                continue
            raise
    raise RuntimeError("unreachable")


async def store(bot: Bot, messages: list[Message]) -> tuple[str, int, list[int], int]:
    """Save private messages into the right topic. Returns (topic, thread_id, new_ids, batch)."""
    messages = sorted(messages, key=lambda x: x.message_id)
    first = next((m for m in messages if m.caption or m.text), messages[0])
    topic = pick_topic(first)
    thread_id, new_ids = await transfer(
        bot, topic, messages[0].chat.id, [m.message_id for m in messages], messages[0].forward_origin is not None
    )
    batch = db.add_items(topic, thread_id, [item_row(m, nid) for m, nid in zip(messages, new_ids)])
    return topic, thread_id, new_ids, batch


async def delete_in_group(bot: Bot, ids: list[int]) -> bool:
    try:
        await bot.delete_messages(group_id(), ids)
        return True
    except TelegramBadRequest as e:
        log.warning("delete failed: %s", e.message)
        return False


async def move_batch(bot: Bot, batch: int, topic: str) -> tuple[int, list[int], bool]:
    """Move saved batch to topic. Returns (thread_id, new_ids, old_deleted)."""
    items = db.batch_items(batch)
    old_ids = [i["message_id"] for i in items]
    thread_id, new_ids = await transfer(bot, topic, group_id(), old_ids, bool(items[0]["forwarded"]))
    db.move_batch(batch, topic, thread_id, new_ids)
    deleted = await delete_in_group(bot, old_ids)
    return thread_id, new_ids, deleted


def saved_text(topic: str, thread_id: int, new_ids: list[int], verb: str = "Saved") -> str:
    what = f"{verb} {len(new_ids)} items" if len(new_ids) > 1 else verb
    return f"✅ {what} → <a href=\"{link(thread_id, new_ids[0])}\">{html.escape(topic)}</a>"


async def report(m: Message, topic: str, thread_id: int, new_ids: list[int], batch: int) -> None:
    r = await m.reply(saved_text(topic, thread_id, new_ids), reply_markup=item_kb(batch),
                      disable_web_page_preview=True)
    db.set_report(batch, r.message_id)


def target_batch(m: Message) -> int | None:
    """Batch referenced by a reply (to original msg or bot's report), else the last saved one."""
    if m.reply_to_message:
        return db.batch_by_src(m.chat.id, m.reply_to_message.message_id)
    return db.last_batch(m.chat.id)


def _item_lines(rows) -> str:
    out = []
    for r in rows:
        snippet = (r["text"] or r["kind"]).replace("\n", " ")
        if len(snippet) > 60:
            snippet = snippet[:57] + "…"
        out.append(
            f"• <a href=\"{link(r['thread_id'], r['message_id'])}\">{html.escape(snippet)}</a>"
            f" <i>({html.escape(r['topic'])})</i>"
        )
    return "\n".join(out)


def need_group(m: Message) -> bool:
    if group_id():
        return False
    asyncio.create_task(m.answer("No storage group yet. See /help"))
    return True


# ---------- group: bind + sync ----------

@router.message(Command("bind"), F.chat.type == ChatType.SUPERGROUP)
async def cmd_bind(m: Message, bot: Bot):
    if m.from_user.id not in owners():
        return
    if not m.chat.is_forum:
        await m.reply("Enable <b>Topics</b> in group settings first, then /bind again.")
        return
    me = await bot.get_chat_member(m.chat.id, bot.id)
    if me.status != "administrator" or not getattr(me, "can_manage_topics", False):
        await m.reply("Make me an <b>admin</b> with <b>Manage topics</b> permission, then /bind again.")
        return
    if group_id() != m.chat.id:
        db.clear_topics()  # thread ids belong to the old group
    db.set("group_id", str(m.chat.id))
    text = "📦 This group is now the storage. Send me stuff in private chat."
    if not getattr(me, "can_delete_messages", False):
        text += "\n⚠️ Give me <b>Delete messages</b> permission too, or Move/Delete will leave old copies."
    await m.reply(text)


@router.message(InStorageGroup(), F.forum_topic_created)
async def on_topic_created(m: Message):
    """Topics created by hand in the group become available to the bot."""
    db.save_topic(m.forum_topic_created.name, m.message_thread_id)


@router.message(InStorageGroup(), F.forum_topic_edited.name)
async def on_topic_edited(m: Message):
    old = db.topic_by_thread(m.message_thread_id)
    db.rename_topic(m.message_thread_id, m.forum_topic_edited.name)
    if old and (db.get("sticky") or "").lower() == old["name"].lower():
        db.set("sticky", m.forum_topic_edited.name)


CONTENT_TYPES = {"text", "photo", "video", "animation", "document", "audio", "voice", "video_note",
                 "sticker", "location", "venue", "contact", "poll"}


@router.message(InStorageGroup(), F.is_topic_message, F.from_user.is_bot.is_(False),
                F.content_type.in_(CONTENT_TYPES))
async def on_group_post(m: Message):
    """Index things posted directly into a topic, so /find and inline search see them."""
    if (m.text or "").startswith("/"):
        return
    topic = db.topic_by_thread(m.message_thread_id)
    if topic:
        db.add_items(topic["name"], m.message_thread_id, [item_row(m, m.message_id, src=False)])


# ---------- private: info ----------

@router.message(Command("start"), F.chat.type == ChatType.PRIVATE)
async def cmd_start(m: Message):
    if not ALLOWED and not db.get("owner_id"):
        db.set("owner_id", str(m.from_user.id))
        await m.answer("👑 You are now the owner of this bot.")
    if m.from_user.id not in owners():
        await m.answer("⛔ Private bot.")
        return
    await cmd_help(m)


@router.message(Command("help"), F.chat.type == ChatType.PRIVATE, IsOwner())
async def cmd_help(m: Message):
    status = "✅ storage group bound" if group_id() else "⚠️ no storage group yet"
    await m.answer(
        "<b>Storage bot</b>\n"
        f"{status}\n\n"
        "<b>Setup</b>\n"
        "1. Create a group, enable <b>Topics</b>\n"
        "2. Add me as admin with <b>Manage topics</b> + <b>Delete messages</b>\n"
        "3. Send /bind in that group\n\n"
        "<b>Saving</b> — send me anything:\n"
        "• auto-sorted: Photos, Videos, Files, Links, Notes, Voice, Music…\n"
        "• <code>#tag</code> in text/caption → topic <i>tag</i>\n"
        "• buttons under each save: 📂 Move, 🗑 Delete\n\n"
        "<b>Items</b> (reply to a saved message, or acts on the last one)\n"
        "/move &lt;topic&gt; — move to topic\n"
        "/delete — delete from storage\n\n"
        "<b>Topics</b>\n"
        "/topics — list topics\n"
        "/new &lt;name&gt; — create topic\n"
        "/rename &lt;old&gt; &gt; &lt;new&gt; — rename topic\n"
        "/deltopic &lt;name&gt; — delete topic with everything in it\n"
        "/use &lt;name&gt; — send everything to this topic · /auto — back to auto\n"
        "/forget &lt;name&gt; — unlink topic from bot\n\n"
        "<b>Finding</b>\n"
        "/find &lt;text&gt; — search\n"
        "/recent [topic] — last saved\n"
        "/random [topic] — get a random item back\n"
        "/stats — numbers\n"
        "Inline: type <code>@botname query</code> in any chat to share saved items\n\n"
        "<b>Data</b>\n"
        "/backup — get .db file · /restore — load .db backup\n"
        "/cancel — abort pending action"
    )


@router.message(Command("topics"), F.chat.type == ChatType.PRIVATE, IsOwner())
async def cmd_topics(m: Message):
    if need_group(m):
        return
    rows = db.topics()
    if not rows:
        return await m.answer("No topics yet. Send me something!")
    sticky = (db.get("sticky") or "").lower()
    lines = [
        f"{'📌' if r['name'].lower() == sticky else '•'} "
        f"<a href=\"{link(r['thread_id'])}\">{html.escape(r['name'])}</a> — {r['n']}"
        for r in rows
    ]
    await m.answer(f"<b>Topics</b> ({db.count()} items)\n" + "\n".join(lines), disable_web_page_preview=True)


@router.message(Command("stats"), F.chat.type == ChatType.PRIVATE, IsOwner())
async def cmd_stats(m: Message):
    size = Path(DB_PATH).stat().st_size if Path(DB_PATH).exists() else 0
    kinds = "\n".join(f"• {r['kind']}: {r['n']}" for r in db.stats()) or "• nothing yet"
    top = sorted(db.topics(), key=lambda t: -t["n"])[:5]
    tops = "\n".join(f"• {html.escape(t['name'])}: {t['n']}" for t in top) or "• none"
    await m.answer(
        f"📊 <b>{db.count()}</b> items in <b>{len(db.topics())}</b> topics\n\n"
        f"<b>By type</b>\n{kinds}\n\n<b>Biggest topics</b>\n{tops}\n\n"
        f"DB size: {size / 1024:.1f} KB"
    )


# ---------- private: topics ----------

@router.message(Command("new"), F.chat.type == ChatType.PRIVATE, IsOwner())
async def cmd_new(m: Message, command: CommandObject, bot: Bot):
    if need_group(m):
        return
    name = (command.args or "").strip()
    if not name:
        return await m.answer("Usage: /new &lt;name&gt;")
    if db.topic(name):
        return await m.answer("Topic already exists.")
    thread_id = await ensure_topic(bot, name)
    await m.answer(f"🆕 <a href=\"{link(thread_id)}\">{html.escape(name)}</a>", disable_web_page_preview=True)


@router.message(Command("rename"), F.chat.type == ChatType.PRIVATE, IsOwner())
async def cmd_rename(m: Message, command: CommandObject, bot: Bot):
    if need_group(m):
        return
    parts = re.split(r"\s*(?:>|\|)\s*", (command.args or "").strip(), maxsplit=1)
    if len(parts) != 2 or not all(parts):
        return await m.answer("Usage: /rename Old name &gt; New name")
    old, new = parts
    t = find_topic(old)
    if not t:
        return await m.answer(f"No topic <b>{html.escape(old)}</b>. See /topics")
    if db.topic(new) and db.topic(new)["thread_id"] != t["thread_id"]:
        return await m.answer("A topic with that name already exists.")
    try:
        await bot.edit_forum_topic(group_id(), t["thread_id"], name=new[:128])
    except TelegramBadRequest as e:
        if "not modified" not in e.message.lower():
            return await m.answer(f"❌ {html.escape(e.message)}")
    db.rename_topic(t["thread_id"], new)
    if (db.get("sticky") or "").lower() == t["name"].lower():
        db.set("sticky", new)
    await m.answer(f"✏️ {html.escape(t['name'])} → <b>{html.escape(new)}</b>")


@router.message(Command("deltopic"), F.chat.type == ChatType.PRIVATE, IsOwner())
async def cmd_deltopic(m: Message, command: CommandObject):
    if need_group(m):
        return
    name = (command.args or "").strip()
    t = find_topic(name) if name else None
    if not t:
        return await m.answer("Usage: /deltopic &lt;existing topic&gt;")
    await m.answer(
        f"⚠️ Delete topic <b>{html.escape(t['name'])}</b> and everything in it "
        f"({t['n']} indexed items)? This cannot be undone.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Delete topic",
                                 callback_data=TopicCB(action="delok", thread=t["thread_id"]).pack()),
            InlineKeyboardButton(text="Cancel", callback_data=TopicCB(action="cancel", thread=t["thread_id"]).pack()),
        ]]),
    )


@router.callback_query(TopicCB.filter(), IsOwner())
async def cb_topic(c: CallbackQuery, callback_data: TopicCB, bot: Bot):
    if callback_data.action == "cancel":
        await c.message.edit_text("Cancelled.")
        return await c.answer()
    t = db.topic_by_thread(callback_data.thread)
    try:
        await bot.delete_forum_topic(group_id(), callback_data.thread)
    except TelegramBadRequest as e:
        if not _thread_gone(e):
            return await c.answer(f"❌ {e.message}", show_alert=True)
    db.delete_topic_with_items(callback_data.thread)
    if t and (db.get("sticky") or "").lower() == t["name"].lower():
        db.set("sticky", None)
    await c.message.edit_text(f"🗑 Topic <b>{html.escape(t['name'] if t else '?')}</b> deleted.")
    await c.answer()


@router.message(Command("use"), F.chat.type == ChatType.PRIVATE, IsOwner())
async def cmd_use(m: Message, command: CommandObject):
    name = (command.args or "").strip()
    if not name:
        return await m.answer("Usage: /use &lt;name&gt;  (or /auto to reset)")
    t = find_topic(name)
    name = t["name"] if t else name
    db.set("sticky", name)
    await m.answer(f"📌 Everything goes to <b>{html.escape(name)}</b> now. /auto to reset.")


@router.message(Command("auto"), F.chat.type == ChatType.PRIVATE, IsOwner())
async def cmd_auto(m: Message):
    db.set("sticky", None)
    await m.answer("🔀 Auto-sorting by type.")


@router.message(Command("forget"), F.chat.type == ChatType.PRIVATE, IsOwner())
async def cmd_forget(m: Message, command: CommandObject):
    name = (command.args or "").strip()
    if not name or not db.topic(name):
        return await m.answer("Usage: /forget &lt;existing topic name&gt;")
    db.delete_topic(name)
    await m.answer(f"Unlinked <b>{html.escape(name)}</b>. Next save creates a fresh topic.")


# ---------- private: items ----------

@router.message(Command("move"), F.chat.type == ChatType.PRIVATE, IsOwner())
async def cmd_move(m: Message, command: CommandObject, bot: Bot):
    if need_group(m):
        return
    batch = target_batch(m)
    if not batch or not db.batch_items(batch):
        return await m.answer("Nothing to move. Reply to a saved message.")
    name = (command.args or "").strip()
    if not name:  # no topic given → show picker
        items = db.batch_items(batch)
        return await m.answer("📂 Move to:", reply_markup=move_kb(batch, items[0]["thread_id"]))
    t = find_topic(name)
    topic = t["name"] if t else name
    if t and t["thread_id"] == db.batch_items(batch)[0]["thread_id"]:
        return await m.answer("Already in that topic.")
    try:
        thread_id, new_ids, deleted = await move_batch(bot, batch, topic)
    except TelegramBadRequest as e:
        return await m.answer(f"❌ {html.escape(e.message)}")
    text = saved_text(topic, thread_id, new_ids, "Moved")
    if not deleted:
        text += "\n⚠️ Couldn't delete the old copy (need <b>Delete messages</b> right)."
    await m.answer(text, disable_web_page_preview=True)


@router.message(Command("delete"), F.chat.type == ChatType.PRIVATE, IsOwner())
async def cmd_delete(m: Message):
    batch = target_batch(m)
    items = db.batch_items(batch) if batch else []
    if not items:
        return await m.answer("Nothing to delete. Reply to a saved message.")
    snippet = html.escape((items[0]["text"] or items[0]["kind"])[:60])
    await m.answer(f"🗑 Delete <i>{snippet}</i> from <b>{html.escape(items[0]['topic'])}</b>?",
                   reply_markup=confirm_delete_kb(batch))


@router.callback_query(ItemCB.filter(), IsOwner())
async def cb_item(c: CallbackQuery, callback_data: ItemCB, bot: Bot):
    batch, action = callback_data.batch, callback_data.action
    items = db.batch_items(batch)
    if not items:
        await c.answer("Item no longer exists.", show_alert=True)
        return await c.message.edit_reply_markup(reply_markup=None)

    if action == "move":
        await c.message.edit_reply_markup(reply_markup=move_kb(batch, items[0]["thread_id"]))
    elif action == "back":
        await c.message.edit_reply_markup(reply_markup=item_kb(batch))
    elif action == "del":
        await c.message.edit_reply_markup(reply_markup=confirm_delete_kb(batch))
    elif action == "delok":
        deleted = await delete_in_group(bot, [i["message_id"] for i in items])
        db.delete_batch(batch)
        text = "🗑 Deleted." if deleted else "🗑 Removed from index (couldn't delete in group — check permissions)."
        await c.message.edit_text(text, reply_markup=None)
    elif action == "new":
        _awaiting_topic_name[c.from_user.id] = (batch, c.message.chat.id, c.message.message_id)
        await c.message.answer("✍️ Send the name for the new topic. /cancel to abort.")
    await c.answer()


@router.callback_query(MoveCB.filter(), IsOwner())
async def cb_move(c: CallbackQuery, callback_data: MoveCB, bot: Bot):
    t = db.topic_by_thread(callback_data.thread)
    if not t or not db.batch_items(callback_data.batch):
        return await c.answer("Topic or item no longer exists.", show_alert=True)
    try:
        thread_id, new_ids, deleted = await move_batch(bot, callback_data.batch, t["name"])
    except TelegramBadRequest as e:
        return await c.answer(f"❌ {e.message}", show_alert=True)
    text = saved_text(t["name"], thread_id, new_ids, "Moved")
    if not deleted:
        text += "\n⚠️ Old copy not deleted (need <b>Delete messages</b> right)."
    await c.message.edit_text(text, reply_markup=item_kb(callback_data.batch), disable_web_page_preview=True)
    await c.answer("Moved")


@router.message(F.chat.type == ChatType.PRIVATE, F.text, ~F.text.startswith("/"), IsOwner(),
                F.from_user.id.func(lambda uid: uid in _awaiting_topic_name))
async def on_new_topic_name(m: Message, bot: Bot):
    batch, chat_id, report_id = _awaiting_topic_name.pop(m.from_user.id)
    name = m.text.strip()[:128]
    if not db.batch_items(batch):
        return await m.answer("Item no longer exists.")
    try:
        thread_id, new_ids, deleted = await move_batch(bot, batch, name)
    except TelegramBadRequest as e:
        return await m.answer(f"❌ {html.escape(e.message)}")
    text = saved_text(name, thread_id, new_ids, "Moved")
    if not deleted:
        text += "\n⚠️ Old copy not deleted (need <b>Delete messages</b> right)."
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=report_id,
                                    reply_markup=item_kb(batch), disable_web_page_preview=True)
        await m.answer(f"🆕 Created <b>{html.escape(name)}</b> and moved.")
    except TelegramBadRequest:
        await m.answer(text, reply_markup=item_kb(batch), disable_web_page_preview=True)


# ---------- private: finding ----------

@router.message(Command("find"), F.chat.type == ChatType.PRIVATE, IsOwner())
async def cmd_find(m: Message, command: CommandObject):
    q = (command.args or "").strip()
    if not q:
        return await m.answer("Usage: /find &lt;text&gt;")
    rows = db.search(q)
    if not rows:
        return await m.answer("Nothing found.")
    await m.answer(f"🔎 <b>{len(rows)}</b> result(s):\n" + _item_lines(rows), disable_web_page_preview=True)


@router.message(Command("recent"), F.chat.type == ChatType.PRIVATE, IsOwner())
async def cmd_recent(m: Message, command: CommandObject):
    name = (command.args or "").strip()
    t = find_topic(name) if name else None
    if name and not t:
        return await m.answer(f"No topic <b>{html.escape(name)}</b>.")
    rows = db.recent(10, t["name"] if t else None)
    if not rows:
        return await m.answer("Nothing saved yet.")
    title = f"🕘 <b>Recent in {html.escape(t['name'])}</b>" if t else "🕘 <b>Recent</b>"
    await m.answer(title + "\n" + _item_lines(rows), disable_web_page_preview=True)


@router.message(Command("random"), F.chat.type == ChatType.PRIVATE, IsOwner())
async def cmd_random(m: Message, command: CommandObject, bot: Bot):
    if need_group(m):
        return
    name = (command.args or "").strip()
    t = find_topic(name) if name else None
    if name and not t:
        return await m.answer(f"No topic <b>{html.escape(name)}</b>.")
    item = db.random_item(t["name"] if t else None)
    if not item:
        return await m.answer("Nothing saved yet.")
    ids = [i["message_id"] for i in db.batch_items(item["batch"])]
    try:
        await bot.copy_messages(m.chat.id, group_id(), ids)
    except TelegramBadRequest as e:
        return await m.answer(f"❌ {html.escape(e.message)} (deleted in group?)")
    await m.answer(f"🎲 from <a href=\"{link(item['thread_id'], item['message_id'])}\">"
                   f"{html.escape(item['topic'])}</a>", disable_web_page_preview=True)


# ---------- inline search ----------

def inline_result(r):
    rid, fid = str(r["id"]), r["file_id"]
    title = (r["text"] or r["kind"])[:64]
    media = r["media"]
    if media == "photo":
        return InlineQueryResultCachedPhoto(id=rid, photo_file_id=fid)
    if media == "animation":
        return InlineQueryResultCachedGif(id=rid, gif_file_id=fid)
    if media == "video":
        return InlineQueryResultCachedVideo(id=rid, video_file_id=fid, title=title)
    if media == "document":
        return InlineQueryResultCachedDocument(id=rid, document_file_id=fid, title=title, description=r["topic"])
    if media == "audio":
        return InlineQueryResultCachedAudio(id=rid, audio_file_id=fid)
    if media == "voice":
        return InlineQueryResultCachedVoice(id=rid, voice_file_id=fid, title=title)
    if media == "sticker":
        return InlineQueryResultCachedSticker(id=rid, sticker_file_id=fid)
    if r["kind"] in ("text", "link") and r["text"]:
        return InlineQueryResultArticle(
            id=rid, title=title, description=r["topic"],
            input_message_content=InputTextMessageContent(message_text=r["text"][:4096], parse_mode=None),
        )
    return None  # video notes etc. have no inline type


@router.inline_query(IsOwner())
async def on_inline(q: InlineQuery):
    offset = int(q.offset or 0)
    rows = db.search(q.query.strip(), limit=50, offset=offset, with_file=True)
    results = [r for r in (inline_result(row) for row in rows) if r]
    await q.answer(results, cache_time=0, is_personal=True,
                   next_offset=str(offset + 50) if len(rows) == 50 else "")


@router.inline_query()
async def on_inline_stranger(q: InlineQuery):
    await q.answer([], cache_time=300, is_personal=True)


# ---------- backup / restore ----------

@router.message(Command("backup"), F.chat.type == ChatType.PRIVATE, IsOwner())
async def cmd_backup(m: Message):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"storagebot-{stamp}.db"
        db.backup_to(path)
        await m.answer_document(
            FSInputFile(path),
            caption=f"💾 Backup: {db.count()} items, {len(db.topics())} topics.\n"
                    "Restore: send this file with caption /restore",
        )


async def _do_restore(m: Message, bot: Bot, doc: Document):
    _awaiting_restore.discard(m.from_user.id)
    if doc.file_size and doc.file_size > MAX_RESTORE_SIZE:
        return await m.reply("❌ File too big (max 20 MB).")

    with tempfile.TemporaryDirectory() as tmp:
        incoming = Path(tmp) / "incoming.db"
        await bot.download(doc, destination=incoming)
        try:
            n = DB.validate(incoming)
        except ValueError as e:
            return await m.reply(f"❌ Invalid backup: {html.escape(str(e))}")

        # safety copy of current DB next to it
        safety = Path(DB_PATH).with_name(f"{Path(DB_PATH).stem}.before-restore-{datetime.now():%Y%m%d-%H%M%S}.db")
        db.backup_to(safety)

        owner = db.get("owner_id")
        db.restore_from(incoming)
        if not ALLOWED and owner:  # don't lock the current owner out
            db.set("owner_id", owner)

    _albums.clear()
    _awaiting_topic_name.clear()
    await m.reply(
        f"♻️ Restored: {n} items, {len(db.topics())} topics.\n"
        f"Previous DB saved as <code>{html.escape(safety.name)}</code>."
    )


@router.message(Command("restore"), F.chat.type == ChatType.PRIVATE, IsOwner())
async def cmd_restore(m: Message, bot: Bot):
    # /restore as caption of the file, or as reply to the file
    doc = m.document or (m.reply_to_message.document if m.reply_to_message else None)
    if doc:
        return await _do_restore(m, bot, doc)
    _awaiting_restore.add(m.from_user.id)
    await m.answer("📥 Send the .db backup file now. /cancel to abort.\n"
                   "⚠️ Current data will be replaced (a safety copy is kept).")


@router.message(Command("cancel"), F.chat.type == ChatType.PRIVATE, IsOwner())
async def cmd_cancel(m: Message):
    _awaiting_restore.discard(m.from_user.id)
    _awaiting_topic_name.pop(m.from_user.id, None)
    await m.answer("Cancelled.")


@router.message(F.chat.type == ChatType.PRIVATE, F.document, IsOwner(),
                F.from_user.id.func(lambda uid: uid in _awaiting_restore))
async def on_restore_file(m: Message, bot: Bot):
    await _do_restore(m, bot, m.document)


# ---------- saving ----------

async def _flush_album(key: str, bot: Bot):
    await asyncio.sleep(ALBUM_WAIT)
    messages = _albums.pop(key, [])
    if not messages:
        return
    try:
        topic, thread_id, new_ids, batch = await store(bot, messages)
        await report(messages[0], topic, thread_id, new_ids, batch)
    except Exception as e:  # noqa: BLE001
        log.exception("album save failed")
        await messages[0].reply(f"❌ Failed: {html.escape(str(e))}")


@router.message(F.chat.type == ChatType.PRIVATE, IsOwner())
async def on_message(m: Message, bot: Bot):
    if m.text and m.text.startswith("/"):
        return await m.answer("Unknown command. /help")
    if need_group(m):
        return

    if m.media_group_id:
        key = f"{m.chat.id}:{m.media_group_id}"
        if not _albums[key]:
            asyncio.create_task(_flush_album(key, bot))
        _albums[key].append(m)
        return

    try:
        topic, thread_id, new_ids, batch = await store(bot, [m])
        await report(m, topic, thread_id, new_ids, batch)
    except TelegramBadRequest as e:
        log.exception("save failed")
        await m.reply(f"❌ Failed: {html.escape(e.message)}")


@router.message(F.chat.type == ChatType.PRIVATE)
async def on_stranger(m: Message):
    await m.answer("⛔ Private bot.")


# ---------- main ----------

async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.set_my_commands([
        BotCommand(command="topics", description="List topics"),
        BotCommand(command="find", description="Search saved items"),
        BotCommand(command="recent", description="Last saved items [topic]"),
        BotCommand(command="move", description="Move item (reply) to topic"),
        BotCommand(command="delete", description="Delete item (reply)"),
        BotCommand(command="new", description="Create topic"),
        BotCommand(command="rename", description="Rename topic: old > new"),
        BotCommand(command="deltopic", description="Delete topic with contents"),
        BotCommand(command="use", description="Send everything to one topic"),
        BotCommand(command="auto", description="Auto-sort by type"),
        BotCommand(command="random", description="Random saved item [topic]"),
        BotCommand(command="stats", description="Statistics"),
        BotCommand(command="backup", description="Download DB backup"),
        BotCommand(command="restore", description="Load DB backup"),
        BotCommand(command="help", description="Help"),
    ])
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
