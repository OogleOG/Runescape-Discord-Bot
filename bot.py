import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiohttp
import aiosqlite
import asyncio
from datetime import datetime
from typing import Optional
import os

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

DATABASE = "clan_bot.db"
RUNEMETRICS_URL = "https://apps.runescape.com/runemetrics/profile/profile?user={}&activities=20"
HISCORES_URL = "https://secure.runescape.com/m=hiscore/index_lite.ws?player={}"

# Skill names in order from hiscores
SKILLS = [
    "Overall", "Attack", "Defence", "Strength", "Constitution", "Ranged",
    "Prayer", "Magic", "Cooking", "Woodcutting", "Fletching", "Fishing",
    "Firemaking", "Crafting", "Smithing", "Mining", "Herblore", "Agility",
    "Thieving", "Slayer", "Farming", "Runecrafting", "Hunter", "Construction",
    "Summoning", "Dungeoneering", "Divination", "Invention", "Archaeology", "Necromancy"
]


async def init_db():
    """Initialize the SQLite database."""
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS linked_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id INTEGER NOT NULL,
                rsn TEXT NOT NULL,
                linked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(discord_id, rsn)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS drop_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id INTEGER NOT NULL,
                rsn TEXT NOT NULL,
                item_name TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS achievement_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id INTEGER NOT NULL,
                rsn TEXT NOT NULL,
                achievement_type TEXT NOT NULL,
                achievement_detail TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS player_cache (
                rsn TEXT PRIMARY KEY,
                total_xp INTEGER,
                last_activity TEXT,
                last_updated TIMESTAMP
            )
        """)
        await db.commit()


async def fetch_runemetrics(rsn: str) -> Optional[dict]:
    """Fetch player data from RuneMetrics API."""
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(RUNEMETRICS_URL.format(rsn.replace(" ", "%20")), timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if "error" not in data:
                        return data
        except Exception as e:
            print(f"RuneMetrics error for {rsn}: {e}")
    return None


async def fetch_hiscores(rsn: str) -> Optional[dict]:
    """Fetch player data from Hiscores API."""
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(HISCORES_URL.format(rsn.replace(" ", "%20")), timeout=10) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    lines = text.strip().split("\n")
                    skills = {}
                    for i, line in enumerate(lines[:len(SKILLS)]):
                        parts = line.split(",")
                        if len(parts) >= 3:
                            skills[SKILLS[i]] = {
                                "rank": int(parts[0]),
                                "level": int(parts[1]),
                                "xp": int(parts[2])
                            }
                    return skills
        except Exception as e:
            print(f"Hiscores error for {rsn}: {e}")
    return None


def format_number(num: int) -> str:
    """Format large numbers with K/M/B suffixes."""
    if num >= 1_000_000_000:
        return f"{num / 1_000_000_000:.2f}B"
    elif num >= 1_000_000:
        return f"{num / 1_000_000:.2f}M"
    elif num >= 1_000:
        return f"{num / 1_000:.1f}K"
    return str(num)


@bot.event
async def on_ready():
    """Called when the bot is ready."""
    await init_db()
    print(f"Logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")


# ==================== ACCOUNT LINKING ====================

@bot.tree.command(name="link", description="Link a RuneScape account to your Discord")
@app_commands.describe(rsn="Your RuneScape username")
async def link_account(interaction: discord.Interaction, rsn: str):
    """Link an RSN to the user's Discord account."""
    await interaction.response.defer()
    
    # Verify the account exists
    data = await fetch_runemetrics(rsn)
    if not data:
        await interaction.followup.send(
            f"❌ Could not find player **{rsn}**. Make sure the name is correct and the profile is public.",
            ephemeral=True
        )
        return
    
    actual_name = data.get("name", rsn)
    
    async with aiosqlite.connect(DATABASE) as db:
        try:
            await db.execute(
                "INSERT INTO linked_accounts (discord_id, rsn) VALUES (?, ?)",
                (interaction.user.id, actual_name)
            )
            await db.commit()
            await interaction.followup.send(
                f"✅ Successfully linked **{actual_name}** to your Discord account!"
            )
        except aiosqlite.IntegrityError:
            await interaction.followup.send(
                f"⚠️ **{actual_name}** is already linked to your account.",
                ephemeral=True
            )


@bot.tree.command(name="unlink", description="Unlink a RuneScape account from your Discord")
@app_commands.describe(rsn="The RuneScape username to unlink")
async def unlink_account(interaction: discord.Interaction, rsn: str):
    """Unlink an RSN from the user's Discord account."""
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute(
            "DELETE FROM linked_accounts WHERE discord_id = ? AND LOWER(rsn) = LOWER(?)",
            (interaction.user.id, rsn)
        )
        await db.commit()
        
        if cursor.rowcount > 0:
            await interaction.response.send_message(f"✅ Unlinked **{rsn}** from your account.")
        else:
            await interaction.response.send_message(
                f"❌ **{rsn}** is not linked to your account.",
                ephemeral=True
            )


@bot.tree.command(name="accounts", description="View all your linked RuneScape accounts")
async def view_accounts(interaction: discord.Interaction):
    """Show all RSNs linked to the user's Discord account."""
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute(
            "SELECT rsn, linked_at FROM linked_accounts WHERE discord_id = ?",
            (interaction.user.id,)
        )
        rows = await cursor.fetchall()
    
    if not rows:
        await interaction.response.send_message(
            "You don't have any linked accounts. Use `/link <rsn>` to add one!",
            ephemeral=True
        )
        return
    
    embed = discord.Embed(
        title="🔗 Your Linked Accounts",
        color=discord.Color.blue()
    )
    
    for rsn, linked_at in rows:
        embed.add_field(name=rsn, value=f"Linked: {linked_at[:10]}", inline=True)
    
    await interaction.response.send_message(embed=embed)


# ==================== STATS LOOKUP ====================

@bot.tree.command(name="stats", description="Look up a player's stats")
@app_commands.describe(rsn="RuneScape username (leave blank to use your linked account)")
async def stats_lookup(interaction: discord.Interaction, rsn: Optional[str] = None):
    """Look up stats for a player."""
    await interaction.response.defer()
    
    # If no RSN provided, try to use linked account
    if not rsn:
        async with aiosqlite.connect(DATABASE) as db:
            cursor = await db.execute(
                "SELECT rsn FROM linked_accounts WHERE discord_id = ? LIMIT 1",
                (interaction.user.id,)
            )
            row = await cursor.fetchone()
            if row:
                rsn = row[0]
            else:
                await interaction.followup.send(
                    "Please provide an RSN or link an account with `/link`.",
                    ephemeral=True
                )
                return
    
    # Fetch data from both APIs
    runemetrics = await fetch_runemetrics(rsn)
    hiscores = await fetch_hiscores(rsn)
    
    if not runemetrics and not hiscores:
        await interaction.followup.send(
            f"❌ Could not find player **{rsn}**.",
            ephemeral=True
        )
        return
    
    embed = discord.Embed(
        title=f"📊 Stats for {runemetrics.get('name', rsn) if runemetrics else rsn}",
        color=discord.Color.gold(),
        timestamp=datetime.utcnow()
    )
    
    if runemetrics:
        embed.add_field(
            name="Total XP",
            value=format_number(runemetrics.get("totalxp", 0)),
            inline=True
        )
        embed.add_field(
            name="Total Level",
            value=str(runemetrics.get("totalskill", 0)),
            inline=True
        )
        embed.add_field(
            name="Combat Level",
            value=str(runemetrics.get("combatlevel", 0)),
            inline=True
        )
        
        if runemetrics.get("questscomplete"):
            embed.add_field(
                name="Quests",
                value=f"{runemetrics.get('questscomplete', 0)}/{runemetrics.get('questsstarted', 0) + runemetrics.get('questscomplete', 0) + runemetrics.get('questsnotstarted', 0)}",
                inline=True
            )
        
        # Recent activity
        activities = runemetrics.get("activities", [])
        if activities:
            recent = activities[0]
            embed.add_field(
                name="Recent Activity",
                value=recent.get("text", "Unknown")[:100],
                inline=False
            )
    
    if hiscores:
        # Show combat stats
        combat_stats = ["Attack", "Strength", "Defence", "Constitution", "Ranged", "Prayer", "Magic", "Summoning", "Necromancy"]
        combat_text = ""
        for skill in combat_stats:
            if skill in hiscores:
                combat_text += f"**{skill}:** {hiscores[skill]['level']} | "
        if combat_text:
            embed.add_field(name="Combat Stats", value=combat_text.rstrip(" | "), inline=False)
    
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="compare", description="Compare two players' stats")
@app_commands.describe(rsn1="First player's RSN", rsn2="Second player's RSN")
async def compare_stats(interaction: discord.Interaction, rsn1: str, rsn2: str):
    """Compare stats between two players."""
    await interaction.response.defer()
    
    data1 = await fetch_runemetrics(rsn1)
    data2 = await fetch_runemetrics(rsn2)
    
    if not data1 or not data2:
        missing = []
        if not data1:
            missing.append(rsn1)
        if not data2:
            missing.append(rsn2)
        await interaction.followup.send(
            f"❌ Could not find player(s): {', '.join(missing)}",
            ephemeral=True
        )
        return
    
    embed = discord.Embed(
        title=f"⚔️ {data1['name']} vs {data2['name']}",
        color=discord.Color.purple()
    )
    
    # Compare key stats
    comparisons = [
        ("Total XP", "totalxp"),
        ("Total Level", "totalskill"),
        ("Combat Level", "combatlevel"),
        ("Quests Complete", "questscomplete"),
    ]
    
    for label, key in comparisons:
        val1 = data1.get(key, 0)
        val2 = data2.get(key, 0)
        
        if key == "totalxp":
            display1 = format_number(val1)
            display2 = format_number(val2)
        else:
            display1 = str(val1)
            display2 = str(val2)
        
        winner = "🏆" if val1 > val2 else ("🏆" if val2 > val1 else "🤝")
        
        embed.add_field(
            name=label,
            value=f"{display1} {'🏆' if val1 > val2 else ''} vs {display2} {'🏆' if val2 > val1 else ''}",
            inline=True
        )
    
    await interaction.followup.send(embed=embed)


# ==================== DROP LOGGING ====================

@bot.tree.command(name="drop", description="Log a rare drop")
@app_commands.describe(
    item="The item you received",
    rsn="Which account got the drop (optional if you only have one linked)"
)
async def log_drop(interaction: discord.Interaction, item: str, rsn: Optional[str] = None):
    """Manually log a rare drop."""
    async with aiosqlite.connect(DATABASE) as db:
        # Get user's linked accounts
        cursor = await db.execute(
            "SELECT rsn FROM linked_accounts WHERE discord_id = ?",
            (interaction.user.id,)
        )
        accounts = await cursor.fetchall()
        
        if not accounts:
            await interaction.response.send_message(
                "You need to link an account first with `/link`.",
                ephemeral=True
            )
            return
        
        if rsn:
            # Verify the RSN is linked to this user
            if not any(a[0].lower() == rsn.lower() for a in accounts):
                await interaction.response.send_message(
                    f"**{rsn}** is not linked to your account.",
                    ephemeral=True
                )
                return
            selected_rsn = rsn
        else:
            selected_rsn = accounts[0][0]
        
        await db.execute(
            "INSERT INTO drop_log (discord_id, rsn, item_name) VALUES (?, ?, ?)",
            (interaction.user.id, selected_rsn, item)
        )
        await db.commit()
    
    # Announce in the channel
    embed = discord.Embed(
        title="🎉 Rare Drop!",
        description=f"**{selected_rsn}** received **{item}**!",
        color=discord.Color.green(),
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"Logged by {interaction.user.display_name}")
    
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="drops", description="View drop history")
@app_commands.describe(rsn="Filter by RSN (optional)")
async def view_drops(interaction: discord.Interaction, rsn: Optional[str] = None):
    """View logged drops."""
    async with aiosqlite.connect(DATABASE) as db:
        if rsn:
            cursor = await db.execute(
                "SELECT rsn, item_name, timestamp FROM drop_log WHERE LOWER(rsn) = LOWER(?) ORDER BY timestamp DESC LIMIT 10",
                (rsn,)
            )
        else:
            cursor = await db.execute(
                "SELECT rsn, item_name, timestamp FROM drop_log ORDER BY timestamp DESC LIMIT 10"
            )
        drops = await cursor.fetchall()
    
    if not drops:
        await interaction.response.send_message("No drops logged yet!", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="📜 Recent Drops",
        color=discord.Color.gold()
    )
    
    for drop_rsn, item, timestamp in drops:
        embed.add_field(
            name=item,
            value=f"**{drop_rsn}** - {timestamp[:10]}",
            inline=True
        )
    
    await interaction.response.send_message(embed=embed)


# ==================== CLAN STATS ====================

@bot.tree.command(name="leaderboard", description="View clan XP leaderboard")
async def leaderboard(interaction: discord.Interaction):
    """Show XP leaderboard for all linked accounts."""
    await interaction.response.defer()
    
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("SELECT DISTINCT rsn FROM linked_accounts")
        accounts = await cursor.fetchall()
    
    if not accounts:
        await interaction.followup.send("No accounts linked yet!")
        return
    
    # Fetch XP for all accounts
    player_data = []
    for (rsn,) in accounts:
        data = await fetch_runemetrics(rsn)
        if data:
            player_data.append({
                "name": data.get("name", rsn),
                "xp": data.get("totalxp", 0),
                "level": data.get("totalskill", 0)
            })
    
    # Sort by XP
    player_data.sort(key=lambda x: x["xp"], reverse=True)
    
    embed = discord.Embed(
        title="🏆 Clan Leaderboard",
        color=discord.Color.gold()
    )
    
    medals = ["🥇", "🥈", "🥉"]
    leaderboard_text = ""
    
    for i, player in enumerate(player_data[:10]):
        medal = medals[i] if i < 3 else f"**{i+1}.**"
        leaderboard_text += f"{medal} **{player['name']}** - {format_number(player['xp'])} XP (Level {player['level']})\n"
    
    embed.description = leaderboard_text or "No data available"
    await interaction.followup.send(embed=embed)


# ==================== RUN BOT ====================

def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("Error: DISCORD_TOKEN environment variable not set")
        print("Set it with: export DISCORD_TOKEN='your-bot-token-here'")
        return
    
    bot.run(token)


if __name__ == "__main__":
    main()
