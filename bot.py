"""
Simple Telegram GBAN Bot (single file)
- Library : python-telegram-bot v21+
- Storage : SQLite (set DB_PATH to a Railway Volume path, e.g. /data/gban.db, to persist)
- Logs    : every action is sent to LOG_GROUP_ID

Environment variables:
    BOT_TOKEN      - token from @BotFather                (required)
    OWNER_ID       - your Telegram user ID                (required)
    LOG_GROUP_ID   - log group/channel ID, e.g. -100123.. (required)
    SUDO_USERS     - comma separated user IDs             (optional)
    DB_PATH        - sqlite file path (default gban.db)   (optional)
"""

import asyncio
import html
import logging
import os
import sqlite3
import time

from telegram import Update
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.ext import (
    Application,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO
)
log = logging.getLogger("gbanbot")

# ----------------------------- CONFIG ---------------------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
LOG_GROUP_ID = int(os.environ["LOG_GROUP_ID"])
SUDO_USERS = {
    int(x) for x in os.getenv("SUDO_USERS", "").replace(" ", "").split(",") if x.strip()
}
SUDO_USERS.add(OWNER_ID)
DB_PATH = os.getenv("DB_PATH", "gban.db")

# ----------------------------- DATABASE -------------------------------------
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute(
    """CREATE TABLE IF NOT EXISTS gbans(
        user_id INTEGER PRIMARY KEY,
        reason  TEXT,
        by_id   INTEGER,
        ts      INTEGER
    )"""
)
db.execute("CREATE TABLE IF NOT EXISTS chats(chat_id INTEGER PRIMARY KEY, title TEXT)")
db.commit()


def db_get_gban(user_id: int):
    return db.execute(
        "SELECT user_id, reason, by_id, ts FROM gbans WHERE user_id=?", (user_id,)
    ).fetchone()


def db_add_gban(user_id: int, reason: str, by_id: int):
    db.execute(
        "INSERT OR REPLACE INTO gbans VALUES(?,?,?,?)",
        (user_id, reason, by_id, int(time.time())),
    )
    db.commit()


def db_del_gban(user_id: int):
    db.execute("DELETE FROM gbans WHERE user_id=?", (user_id,))
    db.commit()


def db_all_gbans():
    return db.execute("SELECT user_id, reason FROM gbans ORDER BY ts DESC").fetchall()


def db_add_chat(chat_id: int, title: str):
    db.execute("INSERT OR REPLACE INTO chats VALUES(?,?)", (chat_id, title))
    db.commit()


def db_del_chat(chat_id: int):
    db.execute("DELETE FROM chats WHERE chat_id=?", (chat_id,))
    db.commit()


def db_all_chats():
    return [r[0] for r in db.execute("SELECT chat_id FROM chats").fetchall()]


# ----------------------------- HELPERS --------------------------------------
def is_sudo(user_id: int) -> bool:
    return user_id in SUDO_USERS


async def send_log(context: ContextTypes.DEFAULT_TYPE, text: str):
    try:
        await context.bot.send_message(
            LOG_GROUP_ID, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )
    except Exception as e:
        log.warning("Could not send to log group: %s", e)


def parse_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Return (user_id, reason) from a reply or from '/cmd <id> [reason]'."""
    msg = update.effective_message
    args = context.args or []
    if msg.reply_to_message and msg.reply_to_message.from_user:
        return msg.reply_to_message.from_user.id, " ".join(args)
    if args and args[0].lstrip("-").isdigit():
        return int(args[0]), " ".join(args[1:])
    return None, ""


async def ban_everywhere(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> int:
    ok = 0
    for chat_id in db_all_chats():
        try:
            await context.bot.ban_chat_member(chat_id, user_id)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.1)  # stay under flood limits
    return ok


# ----------------------------- COMMANDS -------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "👋 I'm a global ban bot.\n\n"
        "Sudo commands:\n"
        "/gban <id|reply> [reason]\n"
        "/ungban <id|reply>\n"
        "/gbanlist\n"
        "/chatid"
    )


async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(f"<code>{update.effective_chat.id}</code>", parse_mode=ParseMode.HTML)


async def gban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if not is_sudo(admin.id):
        return
    user_id, reason = parse_target(update, context)
    if not user_id:
        return await update.effective_message.reply_text("Usage: /gban <user_id> [reason] (or reply to a user)")
    if user_id in SUDO_USERS or user_id == context.bot.id:
        return await update.effective_message.reply_text("I can't gban that user.")
    reason = reason or "No reason given"

    already = db_get_gban(user_id) is not None
    db_add_gban(user_id, reason, admin.id)
    status = await update.effective_message.reply_text("⏳ Banning everywhere...")
    count = await ban_everywhere(context, user_id)

    await status.edit_text(
        f"🔨 {'Updated gban' if already else 'Globally banned'} <code>{user_id}</code>\n"
        f"Reason: {html.escape(reason)}\nBanned in {count} chats.",
        parse_mode=ParseMode.HTML,
    )
    await send_log(
        context,
        f"🚫 <b>#GBAN</b>\n<b>User:</b> <code>{user_id}</code>\n"
        f"<b>By:</b> {html.escape(admin.full_name)} (<code>{admin.id}</code>)\n"
        f"<b>Reason:</b> {html.escape(reason)}\n<b>Chats:</b> {count}",
    )


async def ungban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if not is_sudo(admin.id):
        return
    user_id, _ = parse_target(update, context)
    if not user_id:
        return await update.effective_message.reply_text("Usage: /ungban <user_id> (or reply to a user)")
    if not db_get_gban(user_id):
        return await update.effective_message.reply_text("That user isn't gbanned.")
    db_del_gban(user_id)

    count = 0
    for chat_id in db_all_chats():
        try:
            await context.bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
            count += 1
        except Exception:
            pass
        await asyncio.sleep(0.1)

    await update.effective_message.reply_text(f"✅ Ungbanned <code>{user_id}</code> ({count} chats).", parse_mode=ParseMode.HTML)
    await send_log(
        context,
        f"✅ <b>#UNGBAN</b>\n<b>User:</b> <code>{user_id}</code>\n"
        f"<b>By:</b> {html.escape(admin.full_name)} (<code>{admin.id}</code>)",
    )


async def gbanlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_sudo(update.effective_user.id):
        return
    rows = db_all_gbans()
    if not rows:
        return await update.effective_message.reply_text("No gbanned users.")
    text = "\n".join(f"{uid} — {r}" for uid, r in rows)
    if len(text) > 3500:
        text = text[:3500] + "\n..."
    await update.effective_message.reply_text(f"Gbanned users ({len(rows)}):\n{text}")


# ----------------------------- ENFORCEMENT ----------------------------------
async def enforce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ban gbanned users when they speak or join."""
    chat = update.effective_chat
    msg = update.effective_message
    if not chat or chat.type == "private" or not msg:
        return

    users = []
    if msg.new_chat_members:
        users = list(msg.new_chat_members)
    elif update.effective_user:
        users = [update.effective_user]

    for u in users:
        row = db_get_gban(u.id)
        if not row:
            continue
        try:
            await context.bot.ban_chat_member(chat.id, u.id)
            if not msg.new_chat_members:
                await msg.delete()
            await context.bot.send_message(
                chat.id,
                f"🚫 Globally banned user removed.\n<b>ID:</b> <code>{u.id}</code>\n"
                f"<b>Reason:</b> {html.escape(row[1] or 'N/A')}",
                parse_mode=ParseMode.HTML,
            )
            await send_log(
                context,
                f"🛡 <b>#ENFORCED</b>\n<b>User:</b> <code>{u.id}</code>\n"
                f"<b>Chat:</b> {html.escape(chat.title or str(chat.id))} (<code>{chat.id}</code>)",
            )
        except Exception as e:
            log.warning("Enforce failed in %s: %s", chat.id, e)


async def track_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remember groups where the bot is admin so gban can reach them."""
    ev = update.my_chat_member
    chat = ev.chat
    if chat.type == "private":
        return
    new = ev.new_chat_member
    if new.status == ChatMemberStatus.ADMINISTRATOR:
        db_add_chat(chat.id, chat.title or "")
        await send_log(context, f"➕ Added to <b>{html.escape(chat.title or '')}</b> (<code>{chat.id}</code>)")
    elif new.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED, ChatMemberStatus.MEMBER):
        db_del_chat(chat.id)


async def post_init(app: Application):
    await app.bot.send_message(LOG_GROUP_ID, "✅ GBAN bot started.")


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chatid", chatid))
    app.add_handler(CommandHandler("gban", gban))
    app.add_handler(CommandHandler("ungban", ungban))
    app.add_handler(CommandHandler("gbanlist", gbanlist))
    app.add_handler(ChatMemberHandler(track_chats, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, enforce), group=1)

    log.info("Bot running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
