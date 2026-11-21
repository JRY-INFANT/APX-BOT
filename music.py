import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import yt_dlp
import os
import shutil
import tempfile
import aiohttp
import re
from datetime import datetime
from gtts import gTTS

# Setup logging / add_log bridge
try:
    from dashboard import add_log
except Exception:
    def add_log(event, details, status="success"):
        print(f"[{event}] {details}")

# ----------------------------------------------------
# 1. FFmpeg & yt-dlp Configuration
# ----------------------------------------------------
def get_ffmpeg_executable():
    """Detect imageio_ffmpeg or system ffmpeg binary."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    which_ffmpeg = shutil.which("ffmpeg")
    if which_ffmpeg:
        return which_ffmpeg
    return "ffmpeg"

YTDL_OPTIONS = {
    'format': 'bestaudio/best',
    'extractaudio': True,
    'audioformat': 'mp3',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)

# ----------------------------------------------------
# 2. Spotify Track & Playlist Resolver
# ----------------------------------------------------
class SpotifyResolver:
    """Extracts track names and artists from Spotify URLs without requiring API credentials."""

    @staticmethod
    def is_spotify_url(query: str) -> bool:
        return bool(re.search(r"open\.spotify\.com/(track|album|playlist)/", query))

    @staticmethod
    async def resolve_spotify(url: str):
        tracks_to_search = []
        track_match = re.search(r"open\.spotify\.com/track/([a-zA-Z0-9]+)", url)
        playlist_match = re.search(r"open\.spotify\.com/(playlist|album)/([a-zA-Z0-9]+)", url)

        async with aiohttp.ClientSession() as session:
            if track_match:
                # 1. Public Spotify oEmbed
                try:
                    oembed_url = f"https://open.spotify.com/oembed?url={url}"
                    async with session.get(oembed_url, timeout=7) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            title = data.get("title", "")
                            # Try to extract artist from page HTML
                            artist = ""
                            try:
                                async with session.get(url, timeout=7) as html_resp:
                                    if html_resp.status == 200:
                                        html = await html_resp.text()
                                        desc_m = re.search(r'<meta property="og:description" content="([^"·]+)', html)
                                        if desc_m:
                                            artist = desc_m.group(1).strip()
                            except Exception:
                                pass
                            search_query = f"{title} {artist}".strip() if artist else title
                            return [{"query": search_query, "title": title, "artist": artist, "thumb": data.get("thumbnail_url")}]
                except Exception as e:
                    print(f"Spotify oEmbed error: {e}")

            elif playlist_match:
                # 2. Extract Playlist / Album Track List from Spotify Embed
                try:
                    embed_url = f"https://open.spotify.com/embed/{playlist_match.group(1)}/{playlist_match.group(2)}"
                    async with session.get(embed_url, timeout=10) as resp:
                        if resp.status == 200:
                            html = await resp.text()
                            # Find all track names in embed JSON or HTML
                            matches = re.findall(r'"title":\s*"([^"]+)",\s*"artists":\s*\[{"name":\s*"([^"]+)"', html)
                            if matches:
                                for t_title, t_artist in matches[:25]:  # limit to first 25 tracks
                                    tracks_to_search.append({
                                        "query": f"{t_title} {t_artist}",
                                        "title": t_title,
                                        "artist": t_artist,
                                        "thumb": None
                                    })
                            else:
                                # Fallback og:title
                                og_m = re.search(r'<meta property="og:title" content="([^"]+)"', html)
                                if og_m:
                                    tracks_to_search.append({"query": og_m.group(1), "title": og_m.group(1), "artist": "", "thumb": None})
                except Exception as e:
                    print(f"Spotify playlist error: {e}")

        return tracks_to_search

# ----------------------------------------------------
# 3. Audio Source Extractor
# ----------------------------------------------------
class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.8):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url')
        self.webpage_url = data.get('webpage_url', '')
        self.thumbnail = data.get('thumbnail')
        self.duration = data.get('duration', 0)
        self.uploader = data.get('uploader', 'Unknown')

    @classmethod
    async def from_query(cls, query, *, loop=None, volume=0.8):
        loop = loop or asyncio.get_event_loop()
        ffmpeg_bin = get_ffmpeg_executable()

        def _extract():
            return ytdl.extract_info(query, download=False)

        data = await loop.run_in_executor(None, _extract)

        if 'entries' in data:
            data = data['entries'][0]

        source = discord.FFmpegPCMAudio(data['url'], executable=ffmpeg_bin, **FFMPEG_OPTIONS)
        return cls(source, data=data, volume=volume)

# ----------------------------------------------------
# 4. Per-Guild Music Player & Queue
# ----------------------------------------------------
class GuildMusicPlayer:
    def __init__(self, bot, guild_id):
        self.bot = bot
        self.guild_id = guild_id
        self.queue = []
        self.current = None
        self.voice_client = None
        self.volume = 0.8
        self.loop = False
        self.channel = None
        self.is_playing = False
        self.paused_for_tts = False

    def get_voice_client(self):
        guild = self.bot.get_guild(self.guild_id)
        if guild:
            return guild.voice_client
        return None

    async def play_next(self):
        vc = self.get_voice_client()
        if not vc or not vc.is_connected():
            self.is_playing = False
            self.current = None
            return

        if self.loop and self.current:
            # Replay current song
            song = self.current
        elif len(self.queue) > 0:
            song = self.queue.pop(0)
            self.current = song
        else:
            self.is_playing = False
            self.current = None
            if self.channel:
                embed = discord.Embed(
                    description="⏹️ Queue concluded. Add more songs with `/play`!",
                    color=discord.Color.from_rgb(88, 101, 242)
                )
                await self.channel.send(embed=embed)
            return

        self.is_playing = True
        try:
            player = await YTDLSource.from_query(song['query'], loop=self.bot.loop, volume=self.volume)
            song['title'] = player.title
            song['duration'] = player.duration
            song['thumbnail'] = player.thumbnail
            song['webpage_url'] = player.webpage_url
            song['uploader'] = player.uploader

            def after_playing(error):
                if error:
                    print(f"Player error in guild {self.guild_id}: {error}")
                fut = asyncio.run_coroutine_threadsafe(self.play_next(), self.bot.loop)
                try:
                    fut.result()
                except Exception:
                    pass

            vc.play(player, after=after_playing)

            if self.channel:
                mins, secs = divmod(player.duration, 60)
                embed = discord.Embed(
                    title="🎶 Now Playing",
                    description=f"**[{player.title}]({player.webpage_url})**",
                    color=discord.Color.from_rgb(87, 242, 135)
                )
                if player.thumbnail:
                    embed.set_thumbnail(url=player.thumbnail)
                embed.add_field(name="Duration", value=f"{mins}:{secs:02d}", inline=True)
                embed.add_field(name="Channel / Artist", value=player.uploader, inline=True)
                embed.add_field(name="Requested By", value=song.get('requester', 'User'), inline=True)
                embed.set_footer(text=f"Alpha-X Music Player • Volume: {int(self.volume * 100)}%")
                await self.channel.send(embed=embed)

            add_log("Music Playing", f"Playing '{player.title}' in guild {self.guild_id}")

        except Exception as e:
            print(f"Error playing song {song.get('query')}: {e}")
            if self.channel:
                await self.channel.send(f"⚠️ Error streaming `{song.get('title') or song.get('query')}`: {str(e)[:100]}")
            await self.play_next()

    def stop(self):
        self.queue.clear()
        self.current = None
        self.is_playing = False
        vc = self.get_voice_client()
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()

# ----------------------------------------------------
# 5. Music & TTS Cog (Slash + Prefix Commands)
# ----------------------------------------------------
class MusicCog(commands.Cog, name="Music & Voice"):
    """Full-featured Music Player & Text-To-Speech Voice System."""

    def __init__(self, bot):
        self.bot = bot
        self.players = {}

    def get_player(self, guild_id: int) -> GuildMusicPlayer:
        if guild_id not in self.players:
            self.players[guild_id] = GuildMusicPlayer(self.bot, guild_id)
        return self.players[guild_id]

    async def ensure_voice(self, interaction_or_ctx):
        """Ensures the bot joins the user's voice channel."""
        author = getattr(interaction_or_ctx, 'author', None) or interaction_or_ctx.user
        guild = interaction_or_ctx.guild

        if not author.voice or not author.voice.channel:
            return None, "❌ You must be connected to a Voice Channel first!"

        voice_channel = author.voice.channel
        vc = guild.voice_client

        if vc:
            if vc.channel.id != voice_channel.id:
                await vc.move_to(voice_channel)
        else:
            try:
                vc = await voice_channel.connect(timeout=15.0, reconnect=True)
            except Exception as e:
                return None, f"❌ Failed to join voice channel: {e}"

        return vc, None

    # ====================================================
    # SLASH COMMAND: /play
    # ====================================================
    @app_commands.command(name="play", description="Play audio from YouTube, Spotify, or search query")
    @app_commands.describe(query="Song title, artist, YouTube URL, or Spotify track/album/playlist link")
    async def slash_play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()

        vc, err = await self.ensure_voice(interaction)
        if err:
            await interaction.followup.send(err)
            return

        player = self.get_player(interaction.guild_id)
        player.channel = interaction.channel

        # Handle Spotify Links
        if SpotifyResolver.is_spotify_url(query):
            spotify_tracks = await SpotifyResolver.resolve_spotify(query)
            if not spotify_tracks:
                await interaction.followup.send("❌ Unable to extract track info from that Spotify link.")
                return

            if len(spotify_tracks) == 1:
                t = spotify_tracks[0]
                song = {
                    'query': t['query'],
                    'title': t['title'],
                    'requester': interaction.user.mention,
                    'is_spotify': True,
                    'thumb': t.get('thumb')
                }
                player.queue.append(song)
                if not player.is_playing:
                    await interaction.followup.send(f"🟢 **Resolved Spotify track:** `{t['title']}`. Starting playback...")
                    await player.play_next()
                else:
                    embed = discord.Embed(
                        title="🎵 Enqueued Spotify Track",
                        description=f"**{t['title']}**\nAdded to position #{len(player.queue)} in queue",
                        color=discord.Color.from_rgb(88, 101, 242)
                    )
                    if t.get('thumb'):
                        embed.set_thumbnail(url=t['thumb'])
                    await interaction.followup.send(embed=embed)
            else:
                for t in spotify_tracks:
                    player.queue.append({
                        'query': t['query'],
                        'title': t['title'],
                        'requester': interaction.user.mention,
                        'is_spotify': True
                    })
                await interaction.followup.send(f"🟢 **Loaded Spotify Playlist/Album:** Added {len(spotify_tracks)} tracks to queue!")
                if not player.is_playing:
                    await player.play_next()
            return

        # Handle YouTube Link or Search Query
        song = {
            'query': query,
            'title': query,
            'requester': interaction.user.mention
        }
        player.queue.append(song)

        if not player.is_playing:
            await interaction.followup.send(f"🔍 Searching & loading **{query}**...")
            await player.play_next()
        else:
            embed = discord.Embed(
                title="🎵 Track Enqueued",
                description=f"**{query}**\nPosition: #{len(player.queue)} in queue",
                color=discord.Color.from_rgb(88, 101, 242)
            )
            embed.set_footer(text=f"Requested by {interaction.user.display_name}")
            await interaction.followup.send(embed=embed)

    # Prefix command !play
    @commands.command(name="play", aliases=["p"])
    async def cmd_play(self, ctx, *, query: str):
        vc, err = await self.ensure_voice(ctx)
        if err:
            await ctx.send(err)
            return

        player = self.get_player(ctx.guild.id)
        player.channel = ctx.channel

        if SpotifyResolver.is_spotify_url(query):
            spotify_tracks = await SpotifyResolver.resolve_spotify(query)
            if not spotify_tracks:
                await ctx.send("❌ Unable to extract track info from that Spotify link.")
                return

            for t in spotify_tracks:
                player.queue.append({
                    'query': t['query'],
                    'title': t['title'],
                    'requester': ctx.author.mention
                })
            await ctx.send(f"🟢 Loaded **{len(spotify_tracks)}** track(s) from Spotify into queue!")
            if not player.is_playing:
                await player.play_next()
            return

        player.queue.append({'query': query, 'title': query, 'requester': ctx.author.mention})
        if not player.is_playing:
            await ctx.send(f"🔍 Loading **{query}**...")
            await player.play_next()
        else:
            await ctx.send(f"🎵 Enqueued **{query}** (Position #{len(player.queue)})")

    # ====================================================
    # SLASH COMMAND: /talk (Text-To-Speech)
    # ====================================================
    @app_commands.command(name="talk", description="Speak a message in your voice channel using Text-To-Speech")
    @app_commands.describe(
        message="The text message you want the bot to say out loud",
        language="Accent/language code (e.g. en, es, fr, de, hi, ja) - default 'en'"
    )
    async def slash_talk(self, interaction: discord.Interaction, message: str, language: str = "en"):
        await interaction.response.defer()

        vc, err = await self.ensure_voice(interaction)
        if err:
            await interaction.followup.send(err)
            return

        player = self.get_player(interaction.guild_id)
        clean_text = message.strip()[:350]  # limit to 350 chars for clean voice output

        # Generate TTS audio file in background executor
        def _generate():
            tts = gTTS(text=clean_text, lang=language, slow=False)
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
            tts.save(tmp.name)
            tmp.close()
            return tmp.name

        try:
            tts_file = await self.bot.loop.run_in_executor(None, _generate)
        except Exception as e:
            await interaction.followup.send(f"❌ Failed to generate speech: {e}")
            return

        ffmpeg_bin = get_ffmpeg_executable()
        tts_source = discord.FFmpegPCMAudio(tts_file, executable=ffmpeg_bin)

        # If music is playing, pause it for TTS, then resume
        was_music_playing = vc.is_playing()
        if was_music_playing:
            vc.pause()
            player.paused_for_tts = True

        def _after_tts(err):
            try:
                if os.path.exists(tts_file):
                    os.remove(tts_file)
            except Exception:
                pass

            if was_music_playing and player.paused_for_tts:
                player.paused_for_tts = False
                try:
                    vc.resume()
                except Exception:
                    pass

        vc.play(tts_source, after=_after_tts)

        embed = discord.Embed(
            title="🗣️ Alpha-X Voice Talk",
            description=f"Spoke in **{interaction.user.voice.channel.name}**:\n> *\"{clean_text}\"*",
            color=discord.Color.from_rgb(87, 242, 135)
        )
        embed.set_footer(text=f"Requested by {interaction.user.display_name} • Lang: {language}")
        await interaction.followup.send(embed=embed)
        add_log("TTS Voice", f"@{interaction.user.name} spoke: '{clean_text[:40]}'")

    # Prefix command !talk / !tts
    @commands.command(name="talk", aliases=["tts", "speak"])
    async def cmd_talk(self, ctx, *, message: str):
        vc, err = await self.ensure_voice(ctx)
        if err:
            await ctx.send(err)
            return

        clean_text = message.strip()[:350]
        def _generate():
            tts = gTTS(text=clean_text, lang="en", slow=False)
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
            tts.save(tmp.name)
            tmp.close()
            return tmp.name

        try:
            tts_file = await self.bot.loop.run_in_executor(None, _generate)
        except Exception as e:
            await ctx.send(f"❌ TTS Error: {e}")
            return

        ffmpeg_bin = get_ffmpeg_executable()
        tts_source = discord.FFmpegPCMAudio(tts_file, executable=ffmpeg_bin)
        player = self.get_player(ctx.guild.id)

        was_music_playing = vc.is_playing()
        if was_music_playing:
            vc.pause()
            player.paused_for_tts = True

        def _after_tts(err):
            try:
                if os.path.exists(tts_file):
                    os.remove(tts_file)
            except Exception:
                pass
            if was_music_playing and player.paused_for_tts:
                player.paused_for_tts = False
                try:
                    vc.resume()
                except Exception:
                    pass

        vc.play(tts_source, after=_after_tts)
        await ctx.send(f"🗣️ Speaking: *\"{clean_text}\"*")

    # ====================================================
    # CONTROLS: /skip, /pause, /resume, /stop, /queue
    # ====================================================
    @app_commands.command(name="skip", description="Skip to the next song in the queue")
    async def slash_skip(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_playing():
            await interaction.response.send_message("❌ No song is currently playing to skip.", ephemeral=True)
            return
        vc.stop()
        await interaction.response.send_message("⏭️ Skipped current track!")
        add_log("Music Skip", f"Song skipped in {interaction.guild.name} by @{interaction.user.name}")

    @commands.command(name="skip", aliases=["s"])
    async def cmd_skip(self, ctx):
        vc = ctx.guild.voice_client
        if vc and vc.is_playing():
            vc.stop()
            await ctx.send("⏭️ Skipped current track!")
        else:
            await ctx.send("❌ Nothing is playing to skip.")

    @app_commands.command(name="pause", description="Pause music playback")
    async def slash_pause(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            await interaction.response.send_message("⏸️ Music paused.")
        else:
            await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)

    @commands.command(name="pause")
    async def cmd_pause(self, ctx):
        vc = ctx.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            await ctx.send("⏸️ Music paused.")

    @app_commands.command(name="resume", description="Resume paused music")
    async def slash_resume(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            await interaction.response.send_message("▶️ Music resumed.")
        else:
            await interaction.response.send_message("❌ Music is not paused.", ephemeral=True)

    @commands.command(name="resume")
    async def cmd_resume(self, ctx):
        vc = ctx.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            await ctx.send("▶️ Music resumed.")

    @app_commands.command(name="stop", description="Stop music, clear queue, and leave voice channel")
    async def slash_stop(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        player.stop()
        vc = interaction.guild.voice_client
        if vc:
            await vc.disconnect()
        await interaction.response.send_message("⏹️ Playback stopped and queue cleared.")
        add_log("Music Stop", f"Stopped in {interaction.guild.name}")

    @commands.command(name="stop")
    async def cmd_stop(self, ctx):
        player = self.get_player(ctx.guild.id)
        player.stop()
        vc = ctx.guild.voice_client
        if vc:
            await vc.disconnect()
        await ctx.send("⏹️ Playback stopped and queue cleared.")

    @app_commands.command(name="queue", description="Show the current music queue")
    async def slash_queue(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        if not player.current and len(player.queue) == 0:
            await interaction.response.send_message("📭 The queue is currently empty. Use `/play` to add tracks!")
            return

        embed = discord.Embed(
            title="📜 Alpha-X Music Queue",
            color=discord.Color.from_rgb(88, 101, 242)
        )
        if player.current:
            embed.add_field(
                name="▶️ Currently Playing",
                value=f"**{player.current.get('title', 'Unknown')}** (Requested by {player.current.get('requester')})",
                inline=False
            )

        if len(player.queue) > 0:
            queue_str = "\n".join(
                f"`{i+1}.` {s.get('title', s.get('query'))[:45]} — {s.get('requester')}"
                for i, s in enumerate(player.queue[:10])
            )
            if len(player.queue) > 10:
                queue_str += f"\n*...and {len(player.queue) - 10} more*"
            embed.add_field(name="Up Next", value=queue_str, inline=False)
        else:
            embed.add_field(name="Up Next", value="No upcoming songs in queue.", inline=False)

        await interaction.response.send_message(embed=embed)

    @commands.command(name="queue", aliases=["q"])
    async def cmd_queue(self, ctx):
        player = self.get_player(ctx.guild.id)
        if not player.current and len(player.queue) == 0:
            await ctx.send("📭 Queue is empty.")
            return
        msg = f"▶️ **Now Playing:** {player.current.get('title', 'Unknown') if player.current else 'None'}\n"
        if player.queue:
            msg += "**Up Next:**\n" + "\n".join(f"`{i+1}.` {s.get('title', s.get('query'))[:45]}" for i, s in enumerate(player.queue[:8]))
        await ctx.send(msg)

    @app_commands.command(name="volume", description="Set playback volume (1-100%)")
    @app_commands.describe(level="Volume level from 1 to 100")
    async def slash_volume(self, interaction: discord.Interaction, level: int):
        level = max(1, min(100, level))
        player = self.get_player(interaction.guild_id)
        player.volume = level / 100.0
        vc = interaction.guild.voice_client
        if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = player.volume
        await interaction.response.send_message(f"🔊 Volume adjusted to **{level}%**")

    @commands.command(name="volume", aliases=["vol"])
    async def cmd_volume(self, ctx, level: int):
        level = max(1, min(100, level))
        player = self.get_player(ctx.guild.id)
        player.volume = level / 100.0
        vc = ctx.guild.voice_client
        if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = player.volume
        await ctx.send(f"🔊 Volume set to **{level}%**")

    @app_commands.command(name="nowplaying", description="Show details of the song currently playing")
    async def slash_nowplaying(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        if not player.current or not player.is_playing:
            await interaction.response.send_message("❌ Nothing is currently playing.", ephemeral=True)
            return

        c = player.current
        embed = discord.Embed(
            title="🎶 Now Playing",
            description=f"**[{c.get('title')}]({c.get('webpage_url', '')})**",
            color=discord.Color.from_rgb(87, 242, 135)
        )
        if c.get('thumbnail'):
            embed.set_thumbnail(url=c.get('thumbnail'))
        if c.get('duration'):
            m, s = divmod(c['duration'], 60)
            embed.add_field(name="Duration", value=f"{m}:{s:02d}", inline=True)
        embed.add_field(name="Requested By", value=c.get('requester', 'User'), inline=True)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="join", description="Connect the bot to your voice channel")
    async def slash_join(self, interaction: discord.Interaction):
        vc, err = await self.ensure_voice(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
        else:
            await interaction.response.send_message(f"🔊 Connected to **{vc.channel.name}**!")

    @commands.command(name="join", aliases=["j"])
    async def cmd_join(self, ctx):
        vc, err = await self.ensure_voice(ctx)
        if err:
            await ctx.send(err)
        else:
            await ctx.send(f"🔊 Connected to **{vc.channel.name}**!")

    @app_commands.command(name="leave", description="Disconnect the bot from voice")
    async def slash_leave(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc:
            player = self.get_player(interaction.guild_id)
            player.stop()
            await vc.disconnect()
            await interaction.response.send_message("👋 Disconnected from voice channel.")
        else:
            await interaction.response.send_message("❌ I am not connected to any voice channel.", ephemeral=True)

    @commands.command(name="leave", aliases=["dc", "disconnect"])
    async def cmd_leave(self, ctx):
        vc = ctx.guild.voice_client
        if vc:
            player = self.get_player(ctx.guild.id)
            player.stop()
            await vc.disconnect()
            await ctx.send("👋 Disconnected from voice channel.")
        else:
            await ctx.send("❌ I am not connected to any voice channel.")

async def setup(bot):
    await bot.add_cog(MusicCog(bot))
