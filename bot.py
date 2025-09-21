import os, re, json, io, asyncio, datetime, aiosqlite, discord, traceback, logging
from discord.ext import commands

logging.basicConfig(level=logging.INFO)


logging.basicConfig(level=logging.INFO)


print("BOOT: starting")

# ------------------ Config ------------------
COMMAND_PREFIX = "!"
MAX_READ_BYTES = 1_000_000
MAX_DB_TEXT = 500_000
DB_PATH = "bot.db"
INVITE_LINK = "https://discord.com/oauth2/authorize?client_id=1419230856626704437&permissions=1275259905&integration_type=0&scope=bot"

# Keep your original main-removed list EXACTLY the same
BAN_CONTAINS = {"debounce", "decomp", "humanoid"}  # case-insensitive containment

# Second stricter list you requested
STRICT_CONTAINS = {"humanoid", "timestep", "runningcontroller2", "debounce", "replicator", "decomp"}  # case-insensitive

# ------------------ Bot Setup ------------------
intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True
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
    # hard limit to avoid Discord payload explosions
    s = json.dumps(obj, ensure_ascii=False, indent=2)
    if len(s) > MAX_DB_TEXT:
        s = s[:MAX_DB_TEXT] + "\n…(truncated)"
    return s

def parse_flags_from_message(text: str) -> dict:
    """
    Accepts either a raw JSON object or a loose key:value list.
    Tries JSON first; falls back to simple parser like:
      key: value
      "Key Name"="Value"
    """
    text = text.strip()
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

def filter_flags(ff: dict):
    kept, removed = {}, {}
    for k, v in ff.items():
        low = k.lower()
        if any(s in low for s in BAN_CONTAINS):
            removed[k] = v
        else:
            kept[k] = v
    return kept, removed

def split_strict(kept: dict):
    final_kept, strict_removed = {}, {}
    for k, v in kept.items():
        low = k.lower()
        if any(s in low for s in STRICT_CONTAINS):
            strict_removed[k] = v
        else:
            final_kept[k] = v
    return final_kept, strict_removed

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

@bot.event
async def on_guild_join(guild: discord.Guild):
    await upsert_guild(guild)

# ------------------ Commands ------------------
@bot.command(name="link")
async def link(ctx: commands.Context):
    """Show the bot's invite link."""
    await ctx.reply(f"Invite me with: {INVITE_LINK}")

@bot.command(name="scan")
async def scan(ctx: commands.Context):
    """
    Paste your flags JSON (or key:value lines) after the command:
      !scan { ...json... }
    or send the flags in the previous message and do: !scan
    """
    if ctx.guild and await is_guild_banned(ctx.guild.id):
        raise commands.CheckFailure("This server is banned.")

    # Grab text after command; if empty, try previous message content
    content = ctx.message.content
    m = re.search(r"scan\s+(.*)$", content, re.DOTALL | re.IGNORECASE)
    text = (m.group(1).strip() if m else "").strip()

    if not text and ctx.message.reference:
        try:
            ref_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
            text = ref_msg.content
        except Exception:
            pass

    if not text:
        return await ctx.reply("Send flags after the command or reply to a message that has them.")

    ff = parse_flags_from_message(text)
    if not ff:
        return await ctx.reply("I couldn't parse any flags. Make sure it's JSON or key:value pairs.")

    kept, removed = filter_flags(ff)
    final_kept, strict_removed = split_strict(kept)

    kept_json = to_json(final_kept)
    removed_json = to_json(removed)
    strict_json = to_json(strict_removed)

    files = [
        discord.File(io.BytesIO(kept_json.encode("utf-8")), filename="cleared_list.json"),
        discord.File(io.BytesIO(strict_json.encode("utf-8")), filename="strict_list.json"),
    ]

    title = "Illegal Flags Found!" if (removed or strict_removed) else "No Illegal Flags Found"
    desc = (
        f"Removed (main) **{len(removed)}** • "
        f"Removed (strict) **{len(strict_removed)}** • "
        f"Kept **{len(final_kept)}**."
    )

    # short preview of removed (main)
    if removed:
        preview = "\n".join([f'"{k}": "{v}"' for k, v in list(removed.items())[:25]])
        if len(preview) > 1500:
            preview = preview[:1500] + "\n… (truncated)"
        desc += "\n\n```json\n" + preview + "\n```"

    await ctx.reply(
        embed=discord.Embed(
            title=title,
            description=desc,
            color=discord.Color.red() if (removed or strict_removed) else discord.Color.green(),
        ),
        files=files,
    )

# ------- Announce safely (owner-only, current channel) -------
@bot.command(name="announcehere")
@is_owner_check()
async def announce_here(ctx: commands.Context, *, message: str):
    """Owner-only. Announces to @everyone in the CURRENT channel (requires Mention Everyone permission)."""
    if not ctx.guild:
        return await ctx.reply("Use this in a server channel.")
    if not ctx.guild.me.guild_permissions.mention_everyone:
        return await ctx.reply("I don't have **Mention Everyone** permission here.")
    await ctx.send(
        f"@everyone {message}",
        allowed_mentions=discord.AllowedMentions(everyone=True, users=False, roles=False),
    )

# ------- Opt-in broadcast system -------
@bot.command(name="optin_broadcast")
@admin_only_check()
async def optin_broadcast(ctx: commands.Context, channel: discord.TextChannel):
    """Admins can opt in a channel to receive owner broadcasts."""
    await set_broadcast_channel(ctx.guild.id, channel.id)
    await ctx.reply(f"Opted in for broadcasts → {channel.mention}")

@bot.command(name="optout_broadcast")
@admin_only_check()
async def optout_broadcast(ctx: commands.Context):
    """Admins can opt out from broadcasts."""
    await set_broadcast_channel(ctx.guild.id, None)
    await ctx.reply("Opted out of broadcasts.")

@bot.command(name="broadcast")
@is_owner_check()
async def broadcast(ctx: commands.Context, *, message: str):
    """Owner-only. Sends an @everyone message to all opted-in channels where the bot can mention everyone."""
    rows = await get_broadcast_channels()
    if not rows:
        return await ctx.reply("No servers have opted in.")

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

    await ctx.reply(f"Broadcast complete. Success: {ok}, Failed: {fail}.")

# ------------------ Basic moderation: ban/unban servers ------------------
@bot.command(name="serverban")
@is_owner_check()
async def server_ban(ctx: commands.Context, guild_id: int):
    await db.execute("UPDATE guilds SET banned=1 WHERE guild_id=?", (guild_id,))
    await db.commit()
    await ctx.reply(f"Banned server `{guild_id}`.")

@bot.command(name="serverunban")
@is_owner_check()
async def server_unban(ctx: commands.Context, guild_id: int):
    await db.execute("UPDATE guilds SET banned=0 WHERE guild_id=?", (guild_id,))
    await db.commit()
    await ctx.reply(f"Unbanned server `{guild_id}`.")

# Simple test command
@bot.command(name="ping")
async def ping(ctx):
    await ctx.reply("pong")

# Global error handler to see what’s breaking
@bot.event
async def on_command_error(ctx, error):
    traceback.print_exception(type(error), error, error.__traceback__)
    try:
        await ctx.reply(f"Error: {error.__class__.__name__}: {error}", mention_author=False)
    except Exception:
        pass


# ------------------ Global checks ------------------
@bot.check
async def block_banned(ctx: commands.Context):
    if ctx.guild is None:
        return True
    return not await is_guild_banned(ctx.guild.id)

# ------------------ Startup ------------------
def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("No DISCORD_TOKEN set. Put your bot token in the DISCORD_TOKEN env var.")
    bot.run(token)

if __name__ == "__main__":
    main()

