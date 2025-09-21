import os, re, json, io, asyncio, datetime, aiosqlite, discord, traceback, logging
from discord.ext import commands
from discord import app_commands

logging.basicConfig(level=logging.INFO)

print("BOOT: starting")

# ------------------ Config ------------------
COMMAND_PREFIX = "!"
MAX_READ_BYTES = 1_000_000
MAX_DB_TEXT = 500_000
DB_PATH = "bot.db"
INVITE_LINK = "https://discord.com/oauth2/authorize?client_id=1419230856626704437&permissions=1275259905&integration_type=0&scope=bot"

# Single (default) removal list (case-insensitive containment)
BAN_CONTAINS = {
    "humanoid",
    "timestep",
    "runningcontroller2",
    "debounce",
    "replicator",
    "decomp",
}

# ------------------ Bot Setup ------------------
intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True  # MUST be enabled in Dev Portal too
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)

db: aiosqlite.Connection | None = None

# ------------------ DB ------------------
async def init_db():
    global db
    db = await aiosqlite.connect(DB_PATH)
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS guilds (
            guild_id INTEGER PRIMARY KEY,
            banned   INTEGER NOT NULL DEFAULT 0,
            broadcast_channel_id INTEGER
        );
        """
    )
    await db.commit()

async def upsert_guild(guild: discord.Guild):
    await db.execute(
        "INSERT OR IGNORE INTO guilds (guild_id, banned, broadcast_channel_id) VALUES (?, 0, NULL)",
        (guild.id,),
    )
    await db.commit()

async def is_guild_banned(guild_id: int) -> bool:
    cur = await db.execute("SELECT banned FROM guilds WHERE guild_id=?", (guild_id,))
    row = await cur.fetchone()
    return bool(row and row[0])

async def set_broadcast_channel(guild_id: int, channel_id: int | None):
    await db.execute("UPDATE guilds SET broadcast_channel_id=? WHERE guild_id=?", (channel_id, guild_id))
    await db.commit()

async def get_broadcast_channels() -> list[tuple[int, int]]:
    cur = await db.execute("SELECT guild_id, broadcast_channel_id FROM guilds WHERE broadcast_channel_id IS NOT NULL")
    return await cur.fetchall()

# ------------------ Helpers ------------------
def to_json(obj: dict) -> str:
    s = json.dumps(obj, ensure_ascii=False, indent=2)
    if len(s) > MAX_DB_TEXT:
        s = s[:MAX_DB_TEXT] + "\n…(truncated)"
    return s

def parse_flags_from_text(text: str) -> dict:
    """
    Accepts either a raw JSON object or a loose key:value list.
    Tries JSON first; falls back to simple parser like:
      key: value
      "Key Name"="Value"
    """
    text = (text or "").strip()
    # try JSON
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass

    flags: dict[str, str] = {}
    for line in text.splitlines():
        if len(line) > MAX_READ_BYTES:
            continue
        # key: value OR "key"="value"
        m = re.match(r'\s*"?([^"=:]+)"?\s*[:=]\s*"?([^"]+)"?\s*$', line.strip())
        if m:
            k, v = m.group(1).strip(), m.group(2).strip()
            flags[k] = v
    return flags

async def parse_flags_from_message(msg: discord.Message) -> dict:
    """Try text, then attachments (.json/.txt)."""
    # First try text content
    flags = parse_flags_from_text(msg.content)
    if flags:
        return flags

    # Then try attachments
    for att in msg.attachments:
        name = (att.filename or "").lower()
        if not (name.endswith(".json") or name.endswith(".txt")):
            continue
        if att.size and att.size > MAX_READ_BYTES:
            continue
        try:
            data = await att.read()
            text = data.decode("utf-8", errors="ignore")
            flags = parse_flags_from_text(text)
            if flags:
                return flags
        except Exception:
            continue
    return {}

def filter_flags(ff: dict):
    kept, removed = {}, {}
    for k, v in ff.items():
        low = k.lower()
        if any(s in low for s in BAN_CONTAINS):
            removed[k] = v
        else:
            kept[k] = v
    return kept, removed

async def safe_reply(ctx, content=None, **kwargs):
    try:
        return await ctx.reply(content, mention_author=False, **kwargs)
    except discord.Forbidden:
        return await ctx.send(content, **kwargs)

def is_owner_check():
    async def pred(ctx: commands.Context):
        return await ctx.bot.is_owner(ctx.author)
    return commands.check(pred)

def admin_only_check():
    async def pred(ctx: commands.Context):
        perms = getattr(ctx.author, "guild_permissions", None)
        return bool(perms and (perms.manage_guild or perms.administrator))
    return commands.check(pred)

# ------------------ Events ------------------
@bot.event
async def on_ready():
    print(f"READY: {bot.user} ({bot.user.id})")
    await init_db()
    for g in bot.guilds:
        await upsert_guild(g)
    # Sync slash commands so /ping works even if prefix fails
    try:
        await bot.tree.sync()
        print("App commands synced.")
    except Exception as e:
        print("App command sync failed:", e)

@bot.event
async def on_guild_join(guild: discord.Guild):
    await upsert_guild(guild)

# ------------------ Commands ------------------
@bot.command(name="link")
async def link(ctx: commands.Context):
    """Show the bot's invite link."""
    await safe_reply(ctx, f"Invite me with: {INVITE_LINK}")

@bot.command(name="ping")
async def ping(ctx: commands.Context):
    await safe_reply(ctx, "pong")

@bot.tree.command(name="ping", description="Slash ping")
async def slash_ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong (slash)")

@bot.command(name="diag")
async def diag(ctx: commands.Context):
    """Show required perms in this channel."""
    me = ctx.guild.me if ctx.guild else None
    if not me:
        return await safe_reply(ctx, "Not in a guild.")
    p = ctx.channel.permissions_for(me)
    needed = {
        "view_channel": p.view_channel,
        "send_messages": p.send_messages,
        "read_message_history": p.read_message_history,
        "embed_links": p.embed_links,
        "attach_files": p.attach_files,
        "mention_everyone": p.mention_everyone,
    }
    missing = [k for k, ok in needed.items() if not ok]
    msg = "All good ✅" if not missing else "Missing ❌: " + ", ".join(missing)
    await safe_reply(ctx, msg)

@bot.command(name="scan")
async def scan(ctx: commands.Context):
    """
    Ways to use:
      1) !scan { ...json... }
      2) Paste flags in a message, then reply to it with !scan
      3) Attach a .json or .txt file with the flags and run !scan (in the same message or by replying to it)
    """
    if ctx.guild and await is_guild_banned(ctx.guild.id):
        raise commands.CheckFailure("This server is banned.")

    # 1) try to grab inline tail after command
    content = ctx.message.content
    m = re.search(r"scan\s+(.*)$", content, re.DOTALL | re.IGNORECASE)
    inline_text = (m.group(1).strip() if m else "").strip()

    # Try inline first
    ff = parse_flags_from_text(inline_text) if inline_text else {}

    # 2) then try attachments on the same message
    if not ff:
        ff = await parse_flags_from_message(ctx.message)

    # 3) then try the referenced message (content or its attachments)
    if not ff and ctx.message.reference:
        try:
            ref_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
            ff = await parse_flags_from_message(ref_msg)
        except Exception:
            pass

    if not ff:
        return await safe_reply(ctx, "Send flags after the command, attach a .json/.txt, or reply to a message/file that has them.")

    kept, removed = filter_flags(ff)
    kept_json = to_json(kept)
    removed_json = to_json(removed)

    files = [discord.File(io.BytesIO(kept_json.encode("utf-8")), filename="cleared_list.json")]

    title = "Illegal Flags Found!" if removed else "No Illegal Flags Found"
    desc = f"Removed **{len(removed)}** • Kept **{len(kept)}**."

    if removed:
        preview = "\n".join([f'"{k}": "{v}"' for k, v in list(removed.items())[:25]])
        if len(preview) > 1500:
            preview = preview[:1500] + "\n… (truncated)"
        desc += "\n\n```json\n" + preview + "\n```"

    await safe_reply(
        ctx,
        embed=discord.Embed(
            title=title,
            description=desc,
            color=discord.Color.red() if removed else discord.Color.green(),
        ),
        files=files,
    )

# ------- Announce safely (owner-only, current channel) -------
@bot.command(name="announcehere")
@is_owner_check()
async def announce_here(ctx: commands.Context, *, message: str):
    """Owner-only. Announces to @everyone in the CURRENT channel (requires Mention Everyone permission)."""
    if not ctx.guild:
        return await safe_reply(ctx, "Use this in a server channel.")
    if not ctx.guild.me.guild_permissions.mention_everyone:
        return await safe_reply(ctx, "I don't have **Mention Everyone** permission here.")
    await ctx.send(
        f"@everyone {message}",
        allowed_mentions=discord.AllowedMentions(everyone=True, users=False, roles=False),
    )

# ------- Announce to all servers (owner-only) -------
@bot.command(name="announceall")
@is_owner_check()
async def announce_all(ctx: commands.Context, *, message: str):
    """Owner-only. Announces to @everyone in all servers the bot is in."""
    # Confirm the command is being executed
    confirmation = await ctx.send("Starting to send announcements to all servers...")
    
    success_count = 0
    fail_count = 0
    
    for guild in bot.guilds:
        # Skip banned guilds
        if await is_guild_banned(guild.id):
            print(f"Skipping banned guild: {guild.name} ({guild.id})")
            fail_count += 1
            continue
            
        # Find a suitable channel to send the message in each guild
        target_channel = None
        
        # Check system channel first (usually the default welcome channel)
        if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
            target_channel = guild.system_channel
        else:
            # If no system channel or no permissions, find the first text channel with permissions
            for channel in guild.text_channels:
                if (channel.permissions_for(guild.me).send_messages and 
                    channel.permissions_for(guild.me).mention_everyone):
                    target_channel = channel
                    break
        
        if target_channel:
            try:
                allowed_mentions = discord.AllowedMentions(everyone=True)
                await target_channel.send(f"@everyone {message}", allowed_mentions=allowed_mentions)
                print(f"Announcement sent to {guild.name} in channel #{target_channel.name}")
                success_count += 1
                await asyncio.sleep(1)  # Rate limiting to avoid being flagged
            except discord.Forbidden:
                print(f"Missing permissions in {guild.name}")
                fail_count += 1
            except discord.HTTPException as e:
                print(f"Failed to send message in {guild.name}: {e}")
                fail_count += 1
        else:
            print(f"Could not find a suitable channel in {guild.name}")
            fail_count += 1
    
    # Update the confirmation message with results
    await confirmation.edit(content=f"✅ Announcements completed! Sent to {success_count} servers, failed in {fail_count} servers.")

# ------- Opt-in broadcast system -------
@bot.command(name="optin_broadcast")
@admin_only_check()
async def optin_broadcast(ctx: commands.Context, channel: discord.TextChannel):
    """Admins can opt in a channel to receive owner broadcasts."""
    await set_broadcast_channel(ctx.guild.id, channel.id)
    await safe_reply(ctx, f"Opted in for broadcasts → {channel.mention}")

@bot.command(name="optout_broadcast")
@admin_only_check()
async def optout_broadcast(ctx: commands.Context):
    """Admins can opt out from broadcasts."""
    await set_broadcast_channel(ctx.guild.id, None)
    await safe_reply(ctx, "Opted out of broadcasts.")

@bot.command(name="broadcast")
@is_owner_check()
async def broadcast(ctx: commands.Context, *, message: str):
    """Owner-only. Sends an @everyone message to all opted-in channels where the bot can mention everyone."""
    rows = await get_broadcast_channels()
    if not rows:
        return await safe_reply(ctx, "No servers have opted in.")

    ok, fail = 0, 0
    for guild_id, chan_id in rows:
        guild = bot.get_guild(guild_id)
        channel = guild.get_channel(chan_id) if guild else None
        if not channel:
            fail += 1
            continue
        try:
            if not guild.me.guild_permissions.mention_everyone:
                fail += 1
                continue
            await channel.send(
                f"@everyone {message}",
                allowed_mentions=discord.AllowedMentions(everyone=True, users=False, roles=False),
            )
            ok += 1
            await asyncio.sleep(1.5)  # avoid hammering rate limits
        except Exception:
            fail += 1

    await safe_reply(ctx, f"Broadcast complete. Success: {ok}, Failed: {fail}.")

# ------------------ Basic moderation: ban/unban servers ------------------
@bot.command(name="serverban")
@is_owner_check()
async def server_ban(ctx: commands.Context, guild_id: int):
    await db.execute("UPDATE guilds SET banned=1 WHERE guild_id=?", (guild_id,))
    await db.commit()
    await safe_reply(ctx, f"Banned server `{guild_id}`.")

@bot.command(name="serverunban")
@is_owner_check()
async def server_unban(ctx: commands.Context, guild_id: int):
    await db.execute("UPDATE guilds SET banned=0 WHERE guild_id=?", (guild_id,))
    await db.commit()
    await safe_reply(ctx, f"Unbanned server `{guild_id}`.")

# ------------------ Global checks & error handler ------------------
@bot.check
async def block_banned(ctx: commands.Context):
    if ctx.guild is None:
        return True
    return not await is_guild_banned(ctx.guild.id)

@bot.event
async def on_command_error(ctx, error):
    traceback.print_exception(type(error), error, error.__traceback__)
    try:
        await safe_reply(ctx, f"Error: {error.__class__.__name__}: {error}")
    except Exception:
        pass

# ------------------ Startup ------------------
def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("No DISCORD_TOKEN set. Put your bot token in the DISCORD_TOKEN env var.")
    bot.run(token)

if __name__ == "__main__":
    main()
