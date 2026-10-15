import os
import asyncio
import logging
import random

import aiosqlite
from pyrogram import Client, filters, idle, enums
from pyrogram.types import Message, ChatMemberUpdated
from pyrogram.errors import (
    FloodWait, RPCError, ChatAdminRequired, UserAdminInvalid,
    PeerIdInvalid, ChannelPrivate,
)
from pyrogram.raw import functions, types as raw_types

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("MultiControlBot")

# ==================== CONFIG ====================
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", 0))
DB_PATH = os.environ.get("DB_PATH", "/data/bot.db")  # mount a Railway volume at /data

if not all([API_ID, API_HASH, BOT_TOKEN, OWNER_ID]):
    raise SystemExit("Missing one of: API_ID, API_HASH, BOT_TOKEN, OWNER_ID")

app = Client(
    "MultiControlBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,  # Railway disk is ephemeral; avoids stale session files
)

# In-memory caches (backed by SQLite)
GBANNED: set[int] = set()
KNOWN_CHATS: set[int] = set()
BOT_ID: int = 0

ADMIN_STATUSES = (enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER)
GROUP_TYPES = (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP)


# ==================== DATABASE ====================
async def db_init():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS chats (chat_id INTEGER PRIMARY KEY)")
        await db.execute(
            "CREATE TABLE IF NOT EXISTS gbans ("
            "user_id INTEGER PRIMARY KEY, reason TEXT, "
            "banned_at TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        await db.commit()
        async with db.execute("SELECT chat_id FROM chats") as cur:
            KNOWN_CHATS.update(r[0] for r in await cur.fetchall())
        async with db.execute("SELECT user_id FROM gbans") as cur:
            GBANNED.update(r[0] for r in await cur.fetchall())
    log.info("DB ready: %d chats, %d gbanned users", len(KNOWN_CHATS), len(GBANNED))


async def db_exec(query: str, params: tuple = ()):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(query, params)
        await db.commit()


async def remember_chat(chat_id: int):
    if chat_id not in KNOWN_CHATS:
        KNOWN_CHATS.add(chat_id)
        await db_exec("INSERT OR IGNORE INTO chats (chat_id) VALUES (?)", (chat_id,))


async def forget_chat(chat_id: int):
    KNOWN_CHATS.discard(chat_id)
    await db_exec("DELETE FROM chats WHERE chat_id = ?", (chat_id,))


# ==================== HELPERS ====================
async def is_admin(client: Client, message: Message) -> bool:
    """True for owner, group admins, and anonymous admins."""
    if message.sender_chat and message.sender_chat.id == message.chat.id:
        return True  # anonymous admin
    user = message.from_user
    if not user:
        return False
    if user.id == OWNER_ID:
        return True
    try:
        member = await client.get_chat_member(message.chat.id, user.id)
        return member.status in ADMIN_STATUSES
    except RPCError:
        return False


async def bot_can_manage_vc(client: Client, chat_id: int) -> bool:
    try:
        me = await client.get_chat_member(chat_id, BOT_ID)
    except RPCError:
        return False
    if me.status == enums.ChatMemberStatus.OWNER:
        return True
    return bool(me.privileges and me.privileges.can_manage_video_chats)


async def get_group_call(client: Client, chat_id: int):
    """Return the InputGroupCall of the active VC, or None."""
    peer = await client.resolve_peer(chat_id)
    if isinstance(peer, raw_types.InputPeerChannel):
        full = await client.invoke(functions.channels.GetFullChannel(channel=peer))
    else:  # basic group
        full = await client.invoke(functions.messages.GetFullChat(chat_id=-chat_id))
    return getattr(full.full_chat, "call", None)


async def safe_ban(client: Client, chat_id: int, user_id: int) -> str:
    """Returns 'ok' or an error code string."""
    for _ in range(3):
        try:
            await client.ban_chat_member(chat_id, user_id)
            return "ok"
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
        except (ChatAdminRequired, UserAdminInvalid):
            return "no_rights_or_target_is_admin"
        except PeerIdInvalid:
            return "user_not_seen_by_bot"
        except ChannelPrivate:
            await forget_chat(chat_id)
            return "bot_removed"
        except RPCError as e:
            return type(e).__name__
    return "flood"


async def safe_unban(client: Client, chat_id: int, user_id: int) -> str:
    for _ in range(3):
        try:
            await client.unban_chat_member(chat_id, user_id)
            return "ok"
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
        except ChannelPrivate:
            await forget_chat(chat_id)
            return "bot_removed"
        except RPCError as e:
            return type(e).__name__
    return "flood"


async def parse_target(client: Client, message: Message):
    """Target from reply, numeric ID, or @username. Returns (user_id, reason)."""
    args = message.command[1:]
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id, " ".join(args)
    if not args:
        return None, ""
    token, reason = args[0], " ".join(args[1:])
    try:
        return int(token), reason
    except ValueError:
        pass
    try:
        user = await client.get_users(token)
        return user.id, reason
    except RPCError:
        return None, ""


# ==================== 1. GLOBAL BAN ====================
owner_only = filters.user(OWNER_ID)


@app.on_message(filters.command("gban") & owner_only)
async def gban_cmd(client: Client, message: Message):
    target_id, reason = await parse_target(client, message)
    if not target_id:
        return await message.reply_text(
            "Usage: `/gban <user_id|@username> [reason]` or reply to a user with `/gban [reason]`"
        )
    if target_id in (OWNER_ID, BOT_ID):
        return await message.reply_text("❌ I won't ban the owner or myself.")

    reason = reason or "No reason provided."
    GBANNED.add(target_id)
    await db_exec(
        "INSERT OR REPLACE INTO gbans (user_id, reason) VALUES (?, ?)", (target_id, reason)
    )

    status = await message.reply_text(
        f"🔄 Banning `{target_id}` in {len(KNOWN_CHATS)} groups..."
    )

    ok, failures = 0, {}
    for chat_id in list(KNOWN_CHATS):
        result = await safe_ban(client, chat_id, target_id)
        if result == "ok":
            ok += 1
        else:
            failures[result] = failures.get(result, 0) + 1
        await asyncio.sleep(0.3)  # gentle rate limiting

    fail_text = "\n".join(f"   • {k}: {v}" for k, v in failures.items()) or "   • none"
    await status.edit_text(
        f"✅ **Global Ban Executed**\n\n"
        f"👤 **User ID:** `{target_id}`\n"
        f"📝 **Reason:** {reason}\n"
        f"🌐 **Banned in:** {ok} groups\n"
        f"⚠️ **Failed:**\n{fail_text}\n\n"
        f"ℹ️ The user is saved and will be auto-banned on join or on any message."
    )


@app.on_message(filters.command("ungban") & owner_only)
async def ungban_cmd(client: Client, message: Message):
    target_id, _ = await parse_target(client, message)
    if not target_id:
        return await message.reply_text("Usage: `/ungban <user_id|@username>` or reply to a user.")

    GBANNED.discard(target_id)
    await db_exec("DELETE FROM gbans WHERE user_id = ?", (target_id,))

    status = await message.reply_text(f"🔄 Unbanning `{target_id}`...")
    ok = 0
    for chat_id in list(KNOWN_CHATS):
        if await safe_unban(client, chat_id, target_id) == "ok":
            ok += 1
        await asyncio.sleep(0.3)
    await status.edit_text(f"✅ `{target_id}` removed from GBan list. Unbanned in {ok} groups.")


@app.on_message(filters.command("gbanlist") & owner_only)
async def gbanlist_cmd(client: Client, message: Message):
    if not GBANNED:
        return await message.reply_text("The GBan list is empty.")
    lines = [f"• `{uid}`" for uid in sorted(GBANNED)]
    await message.reply_text(f"🚫 **Globally banned users ({len(lines)}):**\n" + "\n".join(lines[:100]))


@app.on_message(filters.group, group=-1)
async def track_and_enforce(client: Client, message: Message):
    """Runs on every group message: remember the chat and enforce GBans."""
    await remember_chat(message.chat.id)

    offenders = []
    if message.new_chat_members:
        offenders += [u.id for u in message.new_chat_members if u.id in GBANNED]
    if message.from_user and message.from_user.id in GBANNED:
        offenders.append(message.from_user.id)

    for uid in set(offenders):
        result = await safe_ban(client, message.chat.id, uid)
        if result == "ok":
            log.info("Auto-banned gbanned user %s in %s", uid, message.chat.id)
            try:
                await message.delete()
            except RPCError:
                pass
            try:
                await client.send_message(
                    message.chat.id, f"🚨 Globally banned user `{uid}` was removed."
                )
            except RPCError:
                pass
        else:
            log.warning("Could not ban %s in %s: %s", uid, message.chat.id, result)


@app.on_chat_member_updated(filters.group)
async def bot_membership_tracker(client: Client, update: ChatMemberUpdated):
    """Track groups when the bot is added/removed."""
    new = update.new_chat_member
    if not new or not new.user.is_self:
        return
    if new.status in (enums.ChatMemberStatus.MEMBER, *ADMIN_STATUSES):
        await remember_chat(update.chat.id)
        log.info("Added to chat %s", update.chat.id)
    else:
        await forget_chat(update.chat.id)
        log.info("Removed from chat %s", update.chat.id)


# ==================== 2. VOICE CHAT START / END ====================
@app.on_message(filters.command(["vcon", "startvc"]) & filters.group)
async def vc_on(client: Client, message: Message):
    if not await is_admin(client, message):
        return await message.reply_text("❌ Only group admins can control voice chats.")
    if not await bot_can_manage_vc(client, message.chat.id):
        return await message.reply_text(
            "❌ I need admin rights with **Manage Video Chats** enabled."
        )
    try:
        if await get_group_call(client, message.chat.id):
            return await message.reply_text("⚠️ A voice chat is already active.")
        await client.invoke(
            functions.phone.CreateGroupCall(
                peer=await client.resolve_peer(message.chat.id),
                random_id=random.randint(1, 2**31 - 1),
            )
        )
        await message.reply_text("🎙️ **Voice Chat** started!")
    except RPCError as e:
        log.exception("vcon failed")
        await message.reply_text(f"❌ Failed to start Voice Chat: `{e}`")


@app.on_message(filters.command(["vcoff", "endvc"]) & filters.group)
async def vc_off(client: Client, message: Message):
    if not await is_admin(client, message):
        return await message.reply_text("❌ Only group admins can control voice chats.")
    if not await bot_can_manage_vc(client, message.chat.id):
        return await message.reply_text(
            "❌ I need admin rights with **Manage Video Chats** enabled."
        )
    try:
        call = await get_group_call(client, message.chat.id)
        if not call:
            return await message.reply_text("⚠️ No active Voice Chat in this group.")
        await client.invoke(functions.phone.DiscardGroupCall(call=call))
        await message.reply_text("🛑 **Voice Chat** ended.")
    except RPCError as e:
        log.exception("vcoff failed")
        await message.reply_text(f"❌ Failed to end Voice Chat: `{e}`")


# ==================== 3. VC MUTE / UNMUTE ====================
async def _vc_set_mute(client: Client, message: Message, mute: bool):
    if not await is_admin(client, message):
        return await message.reply_text("❌ Admin command only.")
    if not message.reply_to_message or not message.reply_to_message.from_user:
        return await message.reply_text("⚠️ Reply to the user's message.")
    if not await bot_can_manage_vc(client, message.chat.id):
        return await message.reply_text(
            "❌ I need admin rights with **Manage Video Chats** enabled."
        )

    target = message.reply_to_message.from_user
    try:
        call = await get_group_call(client, message.chat.id)
        if not call:
            return await message.reply_text("⚠️ No active Voice Chat.")
        await client.invoke(
            functions.phone.EditGroupCallParticipant(
                call=call,
                participant=await client.resolve_peer(target.id),
                muted=mute,
            )
        )
        if mute:
            await message.reply_text(f"🔇 Muted {target.mention} in the Voice Chat.")
        else:
            await message.reply_text(
                f"🔊 {target.mention} is allowed to speak again (they must unmute themselves)."
            )
    except RPCError as e:
        log.exception("vc mute/unmute failed")
        await message.reply_text(
            f"❌ Error: `{e}`\n(The user must be currently joined in the voice chat.)"
        )


@app.on_message(filters.command("vcmute") & filters.group)
async def vc_mute(client: Client, message: Message):
    await _vc_set_mute(client, message, True)


@app.on_message(filters.command("vcunmute") & filters.group)
async def vc_unmute(client: Client, message: Message):
    await _vc_set_mute(client, message, False)


# ==================== START ====================
@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    await message.reply_text(
        "👋 **Multi-Control Bot Online!**\n\n"
        "**Owner:** `/gban`, `/ungban`, `/gbanlist`\n"
        "**Admins (groups):** `/vcon`, `/vcoff`, `/vcmute`, `/vcunmute`"
    )


async def main():
    global BOT_ID
    await db_init()
    await app.start()
    BOT_ID = (await app.get_me()).id
    log.info("Bot started as %s", BOT_ID)
    await idle()
    await app.stop()


if __name__ == "__main__":
    app.run(main())
