import discord
from discord.ext import commands
import logging
from dotenv import load_dotenv
import os
import sys
from dashboard import start_dashboard, add_log

load_dotenv()
token = os.getenv('DISCORD_TOKEN')

# Configure file logging and console logging
file_handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')
console_handler = logging.StreamHandler(sys.stdout)
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(name)s: %(message)s',
    handlers=[file_handler, console_handler]
)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

class AlphaBot(commands.Bot):
    async def setup_hook(self):
        # Starts the private web dashboard server on the bot's event loop
        await start_dashboard(self)

bot = AlphaBot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f"We are ready to go in, {bot.user.name}")
    add_log("Bot Online", f"Ready as {bot.user.name} ({len(bot.guilds)} servers connected)", "success")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if 'hello' in message.content.lower():
        await message.channel.send(f'Hello {message.author.mention}!')
        channel_name = getattr(message.channel, 'name', 'DM')
        add_log("Auto Reply", f"Replied 'hello' to @{message.author.name} in #{channel_name}")

    await bot.process_commands(message)

# ----------------------------------------------------
# Private Commands (Admin / Owner control inside Discord)
# ----------------------------------------------------

@bot.command()
@commands.has_permissions(administrator=True)
async def say(ctx, channel: discord.TextChannel, *, message: str):
    """Admin-only: Dispatch a message to any channel and remove trigger message."""
    try:
        await ctx.message.delete()
    except Exception:
        pass
    await channel.send(message)
    add_log("In-Discord Say", f"Sent to #{channel.name} by @{ctx.author.name}")

@bot.command()
@commands.has_permissions(administrator=True)
async def dm(ctx, user: discord.User, *, msg: str):
    """Admin-only: Send a private direct message to a user."""
    try:
        await ctx.message.delete()
    except Exception:
        pass
    await user.send(msg)
    add_log("In-Discord DM", f"Sent DM to @{user.name} by @{ctx.author.name}")

@bot.command()
async def reply(ctx):
    await ctx.reply("This is a reply to your message!")
    add_log("Command !reply", f"Replied to @{ctx.author.name}")

if __name__ == '__main__':
    if not token:
        print("ERROR: DISCORD_TOKEN is missing in your .env file!")
        sys.exit(1)
    bot.run(token)
