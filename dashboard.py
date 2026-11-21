import os
import json
import secrets
import logging
import asyncio
from datetime import datetime
from aiohttp import web
import discord
from discord.ext import commands

logger = logging.getLogger('dashboard')

bot_instance: commands.Bot = None
activity_logs = []
active_sessions = set()

def add_log(event_type: str, details: str, status: str = "success"):
    """Record an action or bot event in the in-memory activity feed."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    entry = {
        "time": timestamp,
        "type": event_type,
        "details": details,
        "status": status
    }
    activity_logs.insert(0, entry)
    if len(activity_logs) > 60:
        activity_logs.pop()

def get_configured_password() -> str:
    """Retrieve password from environment or use default."""
    return os.getenv('DASHBOARD_PASSWORD', 'alphax_admin2026').strip()

def is_authenticated(request: web.Request) -> bool:
    """Verify if the request carries a valid active session token."""
    token = request.headers.get('X-Session-Token', '').strip()
    if not token:
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header[7:].strip()
    return bool(token and token in active_sessions)

async def handle_index(request: web.Request):
    """Serve the private dashboard interface."""
    template_path = os.path.join(os.path.dirname(__file__), 'templates', 'index.html')
    if os.path.exists(template_path):
        return web.FileResponse(template_path)
    return web.Response(text="Dashboard template file not found.", status=404)

async def handle_login(request: web.Request):
    """Authenticate with the private password and issue a session token."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    password = (data.get("password") or "").strip()
    configured = get_configured_password()

    if password == configured:
        session_token = secrets.token_hex(32)
        active_sessions.add(session_token)
        add_log("Auth", f"Private dashboard accessed via Web UI", "success")
        return web.json_response({
            "success": True,
            "token": session_token,
            "message": "Authenticated successfully"
        })
    else:
        add_log("Auth Failed", "Failed login attempt on private dashboard", "error")
        return web.json_response({"error": "Incorrect password. Access denied."}, status=401)

async def handle_verify(request: web.Request):
    """Verify if client's current session token is still active."""
    if is_authenticated(request):
        return web.json_response({"valid": True})
    return web.json_response({"valid": False}, status=401)

async def handle_logout(request: web.Request):
    """Invalidate current session token."""
    token = request.headers.get('X-Session-Token', '').strip()
    if token in active_sessions:
        active_sessions.remove(token)
    add_log("Auth", "Operator logged out from dashboard", "info")
    return web.json_response({"success": True, "message": "Logged out"})

async def handle_status(request: web.Request):
    """Return bot status, servers, and ping (Strictly Private)."""
    if not is_authenticated(request):
        return web.json_response({"error": "Unauthorized access"}, status=401)

    global bot_instance
    if not bot_instance:
        return web.json_response({"online": False, "message": "Bot instance initializing"}, status=503)

    is_ready = bot_instance.is_ready()
    guilds_data = []

    if is_ready:
        for guild in bot_instance.guilds:
            guilds_data.append({
                "id": str(guild.id),
                "name": guild.name,
                "icon": guild.icon.url if guild.icon else None,
                "member_count": guild.member_count or 0
            })

    bot_name = bot_instance.user.name if is_ready and bot_instance.user else "ALPHA - X"
    bot_id = str(bot_instance.user.id) if is_ready and bot_instance.user else ""
    avatar_url = bot_instance.user.display_avatar.url if is_ready and bot_instance.user else "https://cdn.discordapp.com/embed/avatars/0.png"
    ping_ms = round(bot_instance.latency * 1000) if is_ready and bot_instance.latency is not None else 0

    return web.json_response({
        "online": is_ready,
        "bot_name": bot_name,
        "bot_id": bot_id,
        "avatar": avatar_url,
        "ping": ping_ms,
        "guild_count": len(guilds_data),
        "guilds": guilds_data
    })

async def handle_channels(request: web.Request):
    """Return text channels for the selected guild (Strictly Private)."""
    if not is_authenticated(request):
        return web.json_response({"error": "Unauthorized access"}, status=401)

    guild_id = request.query.get('guild_id')
    if not guild_id:
        return web.json_response({"error": "guild_id is required"}, status=400)

    try:
        guild = bot_instance.get_guild(int(guild_id))
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid guild ID format"}, status=400)

    if not guild:
        try:
            guild = await bot_instance.fetch_guild(int(guild_id))
        except Exception:
            return web.json_response({"error": "Guild not found"}, status=404)

    channels = []
    for ch in guild.channels:
        if isinstance(ch, (discord.TextChannel, discord.VoiceChannel, discord.Thread)):
            perms = ch.permissions_for(guild.me)
            if perms.send_messages and perms.view_channel:
                category_name = ch.category.name if getattr(ch, 'category', None) else "Channels"
                channels.append({
                    "id": str(ch.id),
                    "name": ch.name,
                    "category": category_name
                })

    return web.json_response({"channels": channels})

async def handle_send(request: web.Request):
    """Send message or rich embed into Discord (Strictly Private)."""
    if not is_authenticated(request):
        return web.json_response({"error": "Unauthorized access"}, status=401)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Malformed JSON payload"}, status=400)

    mode = data.get("mode", "channel") # "channel" or "dm"
    message_content = (data.get("message") or "").strip()
    is_embed = data.get("is_embed", False)
    embed_title = (data.get("embed_title") or "").strip()
    embed_color = data.get("embed_color", "#5865f2")

    if not message_content and not embed_title:
        return web.json_response({"error": "Message or Embed Title cannot be empty"}, status=400)

    # Build optional embed
    embed = None
    if is_embed:
        color_val = int(embed_color.replace('#', ''), 16) if embed_color.startswith('#') else 0x5865f2
        embed = discord.Embed(
            title=embed_title if embed_title else None,
            description=message_content if message_content else None,
            color=color_val,
            timestamp=datetime.utcnow()
        )
        embed.set_footer(text=f"Sent via Alpha-X Console")

    # Mode 1: Send to Guild Channel
    if mode == "channel":
        channel_id = data.get("channel_id")
        if not channel_id:
            return web.json_response({"error": "channel_id is required"}, status=400)

        try:
            channel = bot_instance.get_channel(int(channel_id))
            if not channel:
                channel = await bot_instance.fetch_channel(int(channel_id))
        except Exception as e:
            add_log("Send Failed", f"Channel {channel_id} inaccessible: {e}", "error")
            return web.json_response({"error": f"Channel error: {e}"}, status=404)

        try:
            if embed:
                sent = await channel.send(embed=embed)
            else:
                sent = await channel.send(message_content)

            channel_name = getattr(channel, 'name', str(channel_id))
            guild_name = channel.guild.name if hasattr(channel, 'guild') else "Server"
            preview = (embed_title or message_content)[:50]
            add_log("Message Dispatched", f"To #{channel_name} ({guild_name}): {preview}", "success")
            return web.json_response({
                "success": True,
                "message_id": str(sent.id),
                "channel": channel_name
            })
        except discord.Forbidden:
            add_log("Send Failed", f"Missing permissions in #{getattr(channel, 'name', channel_id)}", "error")
            return web.json_response({"error": "Bot lacks permission to send messages in that channel."}, status=403)
        except Exception as e:
            add_log("Send Failed", f"Error in #{getattr(channel, 'name', channel_id)}: {e}", "error")
            return web.json_response({"error": f"Failed to send: {e}"}, status=500)

    # Mode 2: Send Direct Message
    elif mode == "dm":
        user_id = data.get("user_id")
        if not user_id:
            return web.json_response({"error": "user_id is required"}, status=400)

        try:
            user = bot_instance.get_user(int(user_id))
            if not user:
                user = await bot_instance.fetch_user(int(user_id))
        except Exception as e:
            add_log("DM Failed", f"User {user_id} not found: {e}", "error")
            return web.json_response({"error": f"User {user_id} not found: {e}"}, status=404)

        try:
            if embed:
                sent = await user.send(embed=embed)
            else:
                sent = await user.send(message_content)

            preview = (embed_title or message_content)[:50]
            add_log("DM Dispatched", f"To @{user.name}: {preview}", "success")
            return web.json_response({
                "success": True,
                "message_id": str(sent.id),
                "user": user.name
            })
        except discord.Forbidden:
            add_log("DM Failed", f"DMs disabled for @{user.name}", "error")
            return web.json_response({"error": f"Cannot DM @{user.name} (DMs closed or bot blocked)"}, status=403)
        except Exception as e:
            add_log("DM Failed", f"Error sending DM: {e}", "error")
            return web.json_response({"error": f"Failed to send DM: {e}"}, status=500)

    return web.json_response({"error": f"Unknown mode: {mode}"}, status=400)

async def handle_logs(request: web.Request):
    """Return activity logs (Strictly Private)."""
    if not is_authenticated(request):
        return web.json_response({"error": "Unauthorized access"}, status=401)
    return web.json_response({"logs": activity_logs})

async def start_dashboard(bot: commands.Bot, host: str = "0.0.0.0", port: int = None):
    """Start the private aiohttp web server on the bot's asyncio event loop."""
    global bot_instance
    bot_instance = bot

    if port is None:
        port = int(os.getenv("PORT", 5000))

    app = web.Application()
    # Public route to serve the HTML interface
    app.router.add_get('/', handle_index)
    
    # Auth endpoints
    app.router.add_post('/api/login', handle_login)
    app.router.add_get('/api/verify', handle_verify)
    app.router.add_post('/api/logout', handle_logout)

    # Strictly Private authenticated API endpoints
    app.router.add_get('/api/status', handle_status)
    app.router.add_get('/api/channels', handle_channels)
    app.router.add_post('/api/send', handle_send)
    app.router.add_get('/api/logs', handle_logs)

    try:
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        add_log("System", f"Private dashboard listening on port {port}", "success")
        print(f"\n=======================================================")
        print(f"🔒 ALPHA - X PRIVATE DASHBOARD ONLINE")
        print(f"   URL:   http://localhost:{port}")
        print(f"   Key:   {get_configured_password()}")
        print(f"=======================================================\n")
    except Exception as e:
        print(f"[Dashboard Error] Could not bind to port {port}: {e}")
