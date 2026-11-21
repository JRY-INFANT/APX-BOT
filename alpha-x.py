import discord
from discord.ext import commands
from discord import app_commands
import logging
from dotenv import load_dotenv
import os
import sys
import subprocess

# Auto-check and self-heal PyNaCl voice encryption dependency
try:
    import nacl
except ImportError:
    print("[Alpha-X] PyNaCl is missing for Discord voice. Installing PyNaCl, yt-dlp, gTTS...")
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "PyNaCl", "yt-dlp", "gTTS", "imageio-ffmpeg"], check=False)
        import nacl
        print("[Alpha-X] PyNaCl installed successfully.")
    except Exception as e:
        print(f"[Alpha-X] PyNaCl auto-install note: {e}")

from dashboard import start_dashboard, add_log

try:
    from music import MusicCog, get_ffmpeg_executable
    MUSIC_AVAILABLE = True
except Exception as e:
    print(f"Warning: could not import MusicCog: {e}")
    MusicCog = None
    MUSIC_AVAILABLE = False

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
        # 1. Register Music Player & Text-To-Speech Cog
        if MUSIC_AVAILABLE and MusicCog:
            try:
                await self.add_cog(MusicCog(self))
                print("Music & Voice TTS Cog loaded successfully.")
            except Exception as e:
                print(f"Failed to load Music Cog: {e}")

        # 2. Start the private web dashboard server on the bot's event loop
        try:
            await start_dashboard(self)
        except Exception as e:
            print(f"Failed to start dashboard server: {e}")

        # 3. Synchronize Slash Commands globally with Discord
        try:
            synced = await self.tree.sync()
            print(f"Synced {len(synced)} slash command(s) globally with Discord.")
            add_log("Slash Commands", f"Synced {len(synced)} slash commands globally", "success")
        except Exception as e:
            print(f"Failed to sync slash commands: {e}")

bot = AlphaBot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f"We are ready to go in, {bot.user.name}")
    add_log("Bot Online", f"Ready as {bot.user.name} ({len(bot.guilds)} servers connected)", "success")
    try:
        ff = get_ffmpeg_executable()
        print(f"[Alpha-X] Active Audio Engine (FFmpeg): {ff}")
    except Exception as e:
        print(f"[Alpha-X] Audio engine check: {e}")

    try:
        activity = discord.Activity(type=discord.ActivityType.listening, name="/play & /talk")
        await bot.change_presence(status=discord.Status.online, activity=activity)
    except Exception:
        pass

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    content = message.content.strip()

    # Intelligent fallback: If user types slash command as regular text message (e.g. /play query: Song or /join)
    if content.startswith("/"):
        parts = content[1:].split(" ", 1)
        cmd_name = parts[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""

        # Strip "query:" or "message:" if typed literally
        if cmd_name == "play" and args.lower().startswith("query:"):
            args = args[6:].strip()
        elif cmd_name == "talk" and args.lower().startswith("message:"):
            args = args[8:].strip()

        command = bot.get_command(cmd_name)
        if command:
            ctx = await bot.get_context(message)
            try:
                if args:
                    await ctx.invoke(command, query=args if "query" in command.clean_params else args)
                else:
                    await ctx.invoke(command)
                return
            except Exception as e:
                print(f"Command execution error: {e}")

    if 'hello' in message.content.lower():
        await message.channel.send(f'Hello {message.author.mention}!')
        channel_name = getattr(message.channel, 'name', 'DM')
        add_log("Auto Reply", f"Replied 'hello' to @{message.author.name} in #{channel_name}")

    await bot.process_commands(message)

# ----------------------------------------------------
# Private Admin Commands (In Discord)
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
