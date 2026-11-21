from http.server import BaseHTTPRequestHandler
import json
import os
import secrets
from urllib.parse import urlparse, parse_qs
import urllib.request
import urllib.error
from datetime import datetime

active_sessions = set()
activity_logs = []

def add_log(event_type, details, status="success"):
    now = datetime.now().strftime("%H:%M:%S")
    activity_logs.insert(0, {"time": now, "type": event_type, "details": details, "status": status})
    if len(activity_logs) > 60:
        activity_logs.pop()

def discord_api(endpoint, method="GET", payload=None):
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        raise ValueError("DISCORD_TOKEN is missing in Environment Variables")
    url = f"https://discord.com/api/v10{endpoint}"
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": "AlphaX-Dashboard/1.0"
    }
    data = json.dumps(payload).encode("utf-8") if payload else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8")
        try:
            err_json = json.loads(err_body)
            msg = err_json.get("message", err_body)
        except Exception:
            msg = err_body
        raise RuntimeError(f"Discord error ({e.code}): {msg}")

class handler(BaseHTTPRequestHandler):
    def send_json(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Session-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Session-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def is_auth(self):
        token = self.headers.get("X-Session-Token", "").strip()
        return bool(token and token in active_sessions)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # Serve Dashboard HTML
        if path in ("/", "/index.html", "/api", "/api/index"):
            candidates = [
                os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "index.html"),
                os.path.join(os.getcwd(), "templates", "index.html"),
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "index.html")
            ]
            template_path = next((p for p in candidates if os.path.exists(p)), None)
            if template_path:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                with open(template_path, "rb") as f:
                    self.wfile.write(f.read())
                return
            return self.send_json(404, {"error": "templates/index.html not found"})

        # Verify Session
        if path == "/api/verify":
            valid = self.is_auth()
            return self.send_json(200 if valid else 401, {"valid": valid})

        # Protected endpoints
        if not self.is_auth():
            return self.send_json(401, {"error": "Unauthorized: Session invalid or expired"})

        # Status & Guilds
        if path == "/api/status":
            try:
                bot_user = discord_api("/users/@me")
                guilds = discord_api("/users/@me/guilds")
                guilds_list = [{
                    "id": g["id"],
                    "name": g["name"],
                    "icon": f"https://cdn.discordapp.com/icons/{g['id']}/{g['icon']}.png" if g.get("icon") else None,
                    "member_count": g.get("approximate_member_count", "Guild")
                } for g in (guilds or [])]

                avatar_url = f"https://cdn.discordapp.com/avatars/{bot_user['id']}/{bot_user['avatar']}.png" if bot_user.get("avatar") else "https://cdn.discordapp.com/embed/avatars/0.png"
                return self.send_json(200, {
                    "online": True,
                    "bot_name": bot_user.get("username", "ALPHA - X"),
                    "bot_id": bot_user.get("id", ""),
                    "avatar": avatar_url,
                    "ping": 42,
                    "guild_count": len(guilds_list),
                    "guilds": guilds_list
                })
            except Exception as e:
                return self.send_json(500, {"online": False, "error": str(e)})

        # Channels
        if path == "/api/channels":
            guild_id = query.get("guild_id", [""])[0]
            if not guild_id:
                return self.send_json(400, {"error": "Missing guild_id parameter"})
            try:
                channels = discord_api(f"/guilds/{guild_id}/channels")
                text_channels = [{
                    "id": c["id"],
                    "name": c["name"],
                    "category": "Text Channels"
                } for c in (channels or []) if c.get("type") in (0, 5)]
                return self.send_json(200, {"channels": text_channels})
            except Exception as e:
                return self.send_json(500, {"error": str(e)})

        # Logs
        if path == "/api/logs":
            return self.send_json(200, {"logs": activity_logs})

        self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # Read JSON body
        content_len = int(self.headers.get("Content-Length", 0))
        body = {}
        if content_len > 0:
            try:
                body = json.loads(self.rfile.read(content_len).decode("utf-8"))
            except Exception:
                return self.send_json(400, {"error": "Invalid JSON format"})

        # Login
        if path == "/api/login":
            password = (body.get("password") or "").strip()
            target_pw = os.environ.get("DASHBOARD_PASSWORD", "0000").strip()
            if password == target_pw:
                token = secrets.token_hex(24)
                active_sessions.add(token)
                add_log("Auth", "Operator unlocked dashboard", "success")
                return self.send_json(200, {"success": True, "token": token})
            add_log("Auth Failed", "Invalid password attempted", "error")
            return self.send_json(401, {"error": "Access Denied: Incorrect operator password"})

        # Logout
        if path == "/api/logout":
            token = self.headers.get("X-Session-Token", "").strip()
            if token in active_sessions:
                active_sessions.remove(token)
            return self.send_json(200, {"success": True})

        # Protected: Send Message
        if not self.is_auth():
            return self.send_json(401, {"error": "Unauthorized: Session invalid or expired"})

        if path == "/api/send":
            mode = body.get("mode", "channel")
            message = (body.get("message") or "").strip()
            is_embed = bool(body.get("is_embed"))
            embed_title = (body.get("embed_title") or "").strip()
            embed_color = body.get("embed_color", "#5865f2").replace("#", "")

            if not message and not embed_title:
                return self.send_json(400, {"error": "Message or Embed Title is required"})

            payload = {}
            if message and not is_embed:
                payload["content"] = message

            if is_embed:
                try:
                    color_int = int(embed_color, 16)
                except Exception:
                    color_int = 0x5865F2
                payload["embeds"] = [{
                    "title": embed_title or None,
                    "description": message or None,
                    "color": color_int,
                    "footer": {"text": "Dispatched via Alpha-X Console"}
                }]

            try:
                if mode == "channel":
                    channel_id = body.get("channel_id")
                    if not channel_id:
                        return self.send_json(400, {"error": "channel_id is required"})
                    sent = discord_api(f"/channels/{channel_id}/messages", method="POST", payload=payload)
                    add_log("Message Sent", f"Dispatched to channel ID {channel_id}", "success")
                    return self.send_json(200, {"success": True, "message_id": sent.get("id"), "channel": channel_id})
                elif mode == "dm":
                    user_id = body.get("user_id")
                    if not user_id:
                        return self.send_json(400, {"error": "user_id is required"})
                    dm_channel = discord_api("/users/@me/channels", method="POST", payload={"recipient_id": user_id})
                    sent = discord_api(f"/channels/{dm_channel['id']}/messages", method="POST", payload=payload)
                    add_log("DM Sent", f"Dispatched DM to user ID {user_id}", "success")
                    return self.send_json(200, {"success": True, "message_id": sent.get("id"), "user": user_id})
            except Exception as e:
                return self.send_json(500, {"error": str(e)})

        self.send_json(404, {"error": "Not found"})
