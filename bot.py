import os
import asyncio
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import RPCError
from pyrogram.raw import functions, types

# Configuration from Environment Variables (Railway)
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", 0))  # Your Telegram User ID

app = Client(
    "MultiControlBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# In-memory storage for GBanned users & active VC lock states
GBANNED_USERS = set()
VC_LOCKED_CHATS = set()

# Helper: Check if user is Admin or Owner
async def is_admin(client: Client, chat_id: int, user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    try:
        member = await client.get_chat_member(chat_id, user_id)
        return member.status in ["administrator", "creator"]
    except Exception:
        return False

# ==================== 1. GLOBAL BAN (GBAN) MODULE ====================

@app.on_message(filters.command("gban") & filters.private)
async def gban_user(client: Client, message: Message):
    if message.from_user.id != OWNER_ID:
        return await message.reply_text("❌ You are not authorized to use Global Ban.")
    
    if len(message.command) < 2:
        return await message.reply_text("Usage: `/gban <user_id> [reason]`", parse_mode="markdown")
    
    try:
        target_id = int(message.command[1])
    except ValueError:
        return await message.reply_text("❌ Invalid User ID.")
    
    reason = " ".join(message.command[2:]) or "No reason provided."
    GBANNED_USERS.add(target_id)
    
    status_msg = await message.reply_text(f"🔄 Initiating Global Ban for `{target_id}` across all groups...")
    
    success_count = 0
    fail_count = 0
    
    # Iterate through all dialogs/groups where the bot is present
    async for dialog in client.get_dialogs():
        if dialog.chat.type in ["supergroup", "group"]:
            chat_id = dialog.chat.id
            try:
                # Check if bot has admin rights to ban members in that group
                bot_member = await client.get_chat_member(chat_id, "me")
                if bot_member.privileges and bot_member.privileges.can_restrict_members:
                    await client.ban_chat_member(chat_id, target_id)
                    success_count += 1
                else:
                    fail_count += 1
            except Exception:
                fail_count += 1
                
    await status_msg.edit_text(
        f"✅ **Global Ban Executed Successfully!**\n\n"
        f"👤 **User ID:** `{target_id}`\n"
        f"📝 **Reason:** {reason}\n"
        f"🌐 **Banned in Groups:** {success_count}\n"
        f"⚠️ **Failed/No Rights:** {fail_count}"
    )

@app.on_message(filters.new_chat_members)
async def auto_gban_check(client: Client, message: Message):
    """Automatically ban incoming users if they are in the GBAN list"""
    chat_id = message.chat.id
    for member in message.new_chat_members:
        if member.id in GBANNED_USERS:
            try:
                await client.ban_chat_member(chat_id, member.id)
                await message.reply_text(f"🚨 Globally Banned User `{member.id}` detected and automatically removed!")
            except Exception:
                pass


# ==================== 2. VOICE CHAT / GROUP CALL CONTROL ====================

@app.on_message(filters.command(["vcon", "startvc"]) & filters.group)
async def turn_on_vc(client: Client, message: Message):
    if not await is_admin(client, message.chat.id, message.from_user.id):
        return await message.reply_text("❌ Only group admins can control voice chats.")
    
    try:
        # Using Telegram Raw API to start a group call / video chat
        await client.invoke(
            functions.phone.CreateGroupCall(
                peer=await client.resolve_peer(message.chat.id),
                random_id=int(asyncio.get_event_loop().time()) % 1000000
            )
        )
        await message.reply_text("🎙️ **Voice Chat** has been successfully started!")
    except RPCError as e:
        await message.reply_text(f"❌ Failed to start Voice Chat: `{e}`")

@app.on_message(filters.command(["vcoff", "endvc"]) & filters.group)
async def turn_off_vc(client: Client, message: Message):
    if not await is_admin(client, message.chat.id, message.from_user.id):
        return await message.reply_text("❌ Only group admins can control voice chats.")
    
    try:
        # Fetch full chat info to get the group call reference
        chat_full = await client.invoke(
            functions.channels.GetFullChannel(
                channel=await client.resolve_peer(message.chat.id)
            )
        )
        call = chat_full.full_chat.call
        if call:
            await client.invoke(
                functions.phone.DiscardGroupCall(call=call)
            )
            await message.reply_text("🛑 **Voice Chat** has been ended.")
        else:
            await message.reply_text("⚠️ No active Voice Chat found in this group.")
    except RPCError as e:
        await message.reply_text(f"❌ Failed to end Voice Chat: `{e}`")


# ==================== 3. VOICE CHAT SPEAKER MUTE CONTROLS ====================

@app.on_message(filters.command("vcmute") & filters.group)
async def mute_vc_participants(client: Client, message: Message):
    """Mutes a specific user in the voice chat (Requires admin rights)"""
    if not await is_admin(client, message.chat.id, message.from_user.id):
        return await message.reply_text("❌ Admin command only.")
        
    if not message.reply_to_message:
        return await message.reply_text("⚠️ Please reply to the user you want to mute in the voice chat.")
        
    target_user = message.reply_to_message.from_user
    if not target_user:
        return await message.reply_text("❌ Could not identify target user.")
        
    try:
        chat_full = await client.invoke(
            functions.channels.GetFullChannel(channel=await client.resolve_peer(message.chat.id))
        )
        call = chat_full.full_chat.call
        if call:
            await client.invoke(
                functions.phone.EditGroupCallParticipant(
                    call=call,
                    participant=await client.resolve_peer(target_user.id),
                    muted=True
                )
            )
            await message.reply_text(f"🔇 Muted {target_user.mention} in the Voice Chat.")
        else:
            await message.reply_text("⚠️ No active Voice Chat found.")
    except Exception as e:
        await message.reply_text(f"❌ Error muting user: `{e}`")


@app.on_message(filters.command("vcunmute") & filters.group)
async def unmute_vc_participants(client: Client, message: Message):
    """Unmutes a specific user in the voice chat"""
    if not await is_admin(client, message.chat.id, message.from_user.id):
        return await message.reply_text("❌ Admin command only.")
        
    if not message.reply_to_message:
        return await message.reply_text("⚠️ Please reply to the user you want to unmute.")
        
    target_user = message.reply_to_message.from_user
    if not target_user:
        return await message.reply_text("❌ Could not identify target user.")
        
    try:
        chat_full = await client.invoke(
            functions.channels.GetFullChannel(channel=await client.resolve_peer(message.chat.id))
        )
        call = chat_full.full_chat.call
        if call:
            await client.invoke(
                functions.phone.EditGroupCallParticipant(
                    call=call,
                    participant=await client.resolve_peer(target_user.id),
                    muted=False
                )
            )
            await message.reply_text(f"🔊 Unmuted {target_user.mention} in the Voice Chat.")
        else:
            await message.reply_text("⚠️ No active Voice Chat found.")
    except Exception as e:
        await message.reply_text(f"❌ Error unmuting user: `{e}`")

@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    await message.reply_text(
        "👋 **Multi-Control Bot Online!**\n\n"
        "• **Global Ban Sync:** Managed via private chat by the owner.\n"
        "• **Voice Chat Control:** Use `/vcon` and `/vcoff` in groups.\n"
        "• **VC Member Management:** Use `/vcmute` and `/vcunmute` by replying to users."
    )

if __name__ == "__main__":
    print("🤖 Starting Bot...")
    app.run()
