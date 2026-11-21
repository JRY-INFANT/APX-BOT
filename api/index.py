from http.server import BaseHTTPRequestHandler
import json
import os
import hmac
import hashlib
import time
from urllib.parse import urlparse, parse_qs
import urllib.request
import urllib.error
from datetime import datetime

activity_logs = []

def add_log(event_type, details, status="success"):
    now = datetime.now().strftime("%H:%M:%S")
    activity_logs.insert(0, {"time": now, "type": event_type, "details": details, "status": status})
    if len(activity_logs) > 60:
        activity_logs.pop()

def get_secret():
    pw = os.environ.get("DASHBOARD_PASSWORD", "0000").strip() or "0000"
    tok = os.environ.get("DISCORD_TOKEN", "").strip()[:12]
    return f"alphax_{pw}_{tok}".encode("utf-8")

def generate_token():
    ts = str(int(time.time()))
    sig = hmac.new(get_secret(), ts.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"

def verify_token(token):
    if not token or "." not in token:
        return False
    try:
        ts, sig = token.split(".", 1)
        if time.time() - int(ts) > 1209600:
            return False
        expected_sig = hmac.new(get_secret(), ts.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected_sig)
    except Exception:
        return False

def discord_api(endpoint, method="GET", payload=None):
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        raise ValueError("DISCORD_TOKEN is missing in Vercel Environment Variables")
    url = f"https://discord.com/api/v10{endpoint}"
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": "AlphaX-Dashboard/2.0"
    }
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as response:
            if response.status == 204:
                return {}
            resp_body = response.read().decode("utf-8")
            return json.loads(resp_body) if resp_body else {}
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
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Session-Token, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, DELETE, PUT")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Session-Token, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, DELETE, PUT")
        self.end_headers()

    def is_auth(self):
        token = self.headers.get("X-Session-Token", "").strip()
        if not token:
            auth_header = self.headers.get("Authorization", "").strip()
            if auth_header.startswith("Bearer "):
                token = auth_header[7:].strip()
        return verify_token(token)

    def get_resolved_path(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if "endpoint" in query:
            ep = query["endpoint"][0].strip("/")
            return f"/api/{ep}"

        matched = self.headers.get("x-matched-path", "").strip()
        if matched:
            return urlparse(matched).path.rstrip("/")

        forwarded = self.headers.get("x-forwarded-uri", "").strip()
        if forwarded:
            return urlparse(forwarded).path.rstrip("/")

        return parsed.path.rstrip("/")

    def do_GET(self):
        resolved_path = self.get_resolved_path()
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        # 1. Verify Session
        if resolved_path == "/api/verify":
            valid = self.is_auth()
            return self.send_json(200 if valid else 401, {"valid": valid})

        # 2. Status & Guilds
        if resolved_path == "/api/status":
            if not self.is_auth():
                return self.send_json(401, {"error": "Unauthorized: Session invalid or expired"})
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
                    "ping": 35,
                    "guild_count": len(guilds_list),
                    "guilds": guilds_list
                })
            except Exception as e:
                return self.send_json(500, {"online": False, "error": str(e)})

        # 3. Channels in a Guild
        if resolved_path == "/api/channels":
            if not self.is_auth():
                return self.send_json(401, {"error": "Unauthorized: Session invalid or expired"})
            guild_id = query.get("guild_id", [""])[0]
            if not guild_id:
                return self.send_json(400, {"error": "Missing guild_id parameter"})
            try:
                channels = discord_api(f"/guilds/{guild_id}/channels")
                text_channels = [{
                    "id": c["id"],
                    "name": c["name"],
                    "topic": c.get("topic", ""),
                    "category": "Text Channels"
                } for c in (channels or []) if c.get("type") in (0, 5)]
                return self.send_json(200, {"channels": text_channels})
            except Exception as e:
                return self.send_json(500, {"error": str(e)})

        # 4. Read Live Channel Messages (Chat Viewer)
        if resolved_path == "/api/messages":
            if not self.is_auth():
                return self.send_json(401, {"error": "Unauthorized: Session invalid or expired"})
            channel_id = query.get("channel_id", [""])[0]
            if not channel_id:
                return self.send_json(400, {"error": "Missing channel_id parameter"})
            try:
                msgs = discord_api(f"/channels/{channel_id}/messages?limit=25")
                formatted = []
                for m in (msgs or []):
                    author = m.get("author", {})
                    avatar_id = author.get("avatar")
                    user_id = author.get("id")
                    avatar_url = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_id}.png" if avatar_id else "https://cdn.discordapp.com/embed/avatars/0.png"
                    formatted.append({
                        "id": m.get("id"),
                        "content": m.get("content", ""),
                        "author": {
                            "id": user_id,
                            "name": author.get("username", "Unknown"),
                            "avatar": avatar_url,
                            "is_bot": bool(author.get("bot"))
                        },
                        "timestamp": m.get("timestamp", ""),
                        "embeds": m.get("embeds", [])
                    })
                return self.send_json(200, {"messages": formatted})
            except Exception as e:
                return self.send_json(500, {"error": str(e)})

        # 5. Activity Logs
        if resolved_path == "/api/logs":
            if not self.is_auth():
                return self.send_json(401, {"error": "Unauthorized: Session invalid or expired"})
            return self.send_json(200, {"logs": activity_logs})

        # Default: Serve Modern HTML Dashboard
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

        self.send_json(404, {"error": "templates/index.html not found"})

    def do_POST(self):
        resolved_path = self.get_resolved_path()

        content_len = int(self.headers.get("Content-Length", 0))
        body = {}
        if content_len > 0:
            try:
                body = json.loads(self.rfile.read(content_len).decode("utf-8"))
            except Exception:
                return self.send_json(400, {"error": "Invalid JSON format"})

        # Login
        if resolved_path == "/api/login":
            password = (body.get("password") or "").strip()
            target_pw = os.environ.get("DASHBOARD_PASSWORD", "0000").strip() or "0000"
            if password == target_pw:
                token = generate_token()
                add_log("Auth", "Operator unlocked console", "success")
                return self.send_json(200, {"success": True, "token": token})
            add_log("Auth Failed", "Invalid password attempted", "error")
            return self.send_json(401, {"error": "Access Denied: Incorrect operator password"})

        # Logout
        if resolved_path == "/api/logout":
            return self.send_json(200, {"success": True})

        # Protected operations
        if not self.is_auth():
            return self.send_json(401, {"error": "Unauthorized: Session invalid or expired"})

        # Dispatch Message / Embed
        if resolved_path == "/api/send":
            mode = body.get("mode", "channel")
            message = (body.get("message") or "").strip()
            reply_to = body.get("reply_to_id")
            
            payload = {}
            if message:
                payload["content"] = message

            # Advanced Embed Handling
            if body.get("is_embed"):
                embed_obj = {}
                if body.get("embed_title"):
                    embed_obj["title"] = body["embed_title"].strip()
                if body.get("embed_description"):
                    embed_obj["description"] = body["embed_description"].strip()
                if body.get("embed_url"):
                    embed_obj["url"] = body["embed_url"].strip()

                color_hex = (body.get("embed_color") or "#5865f2").replace("#", "")
                try:
                    embed_obj["color"] = int(color_hex, 16)
                except Exception:
                    embed_obj["color"] = 0x5865F2

                if body.get("embed_author"):
                    author_data = {"name": body["embed_author"].strip()}
                    if body.get("embed_author_icon"):
                        author_data["icon_url"] = body["embed_author_icon"].strip()
                    embed_obj["author"] = author_data

                if body.get("embed_footer"):
                    footer_data = {"text": body["embed_footer"].strip()}
                    if body.get("embed_footer_icon"):
                        footer_data["icon_url"] = body["embed_footer_icon"].strip()
                    embed_obj["footer"] = footer_data
                else:
                    embed_obj["footer"] = {"text": "Dispatched via Alpha-X Console"}

                if body.get("embed_thumbnail"):
                    embed_obj["thumbnail"] = {"url": body["embed_thumbnail"].strip()}

                if body.get("embed_image"):
                    embed_obj["image"] = {"url": body["embed_image"].strip()}

                # Custom Fields
                fields = body.get("embed_fields", [])
                if isinstance(fields, list) and fields:
                    valid_fields = []
                    for f in fields:
                        name = (f.get("name") or "").strip()
                        val = (f.get("value") or "").strip()
                        if name and val:
                            valid_fields.append({"name": name, "value": val, "inline": bool(f.get("inline"))})
                    if valid_fields:
                        embed_obj["fields"] = valid_fields

                if body.get("embed_timestamp"):
                    embed_obj["timestamp"] = datetime.utcnow().isoformat()

                payload["embeds"] = [embed_obj]

            # Message reply reference
            if reply_to:
                payload["message_reference"] = {"message_id": str(reply_to).strip()}

            if not payload.get("content") and not payload.get("embeds"):
                return self.send_json(400, {"error": "Message content or Embed is required"})

            try:
                if mode == "channel":
                    channel_id = body.get("channel_id")
                    if not channel_id:
                        return self.send_json(400, {"error": "channel_id is required"})
                    sent = discord_api(f"/channels/{channel_id}/messages", method="POST", payload=payload)
                    preview = (body.get("embed_title") or message)[:40]
                    add_log("Message Dispatched", f"To channel ID {channel_id}: {preview}", "success")
                    return self.send_json(200, {"success": True, "message_id": sent.get("id"), "channel": channel_id})
                elif mode == "dm":
                    user_id = body.get("user_id")
                    if not user_id:
                        return self.send_json(400, {"error": "user_id is required"})
                    dm_channel = discord_api("/users/@me/channels", method="POST", payload={"recipient_id": user_id})
                    sent = discord_api(f"/channels/{dm_channel['id']}/messages", method="POST", payload=payload)
                    preview = (body.get("embed_title") or message)[:40]
                    add_log("DM Dispatched", f"To User ID {user_id}: {preview}", "success")
                    return self.send_json(200, {"success": True, "message_id": sent.get("id"), "user": user_id})
            except Exception as e:
                return self.send_json(500, {"error": str(e)})

        # Delete message
        if resolved_path == "/api/delete-message":
            channel_id = body.get("channel_id")
            message_id = body.get("message_id")
            if not channel_id or not message_id:
                return self.send_json(400, {"error": "channel_id and message_id are required"})
            try:
                discord_api(f"/channels/{channel_id}/messages/{message_id}", method="DELETE")
                add_log("Message Deleted", f"Deleted message ID {message_id}", "info")
                return self.send_json(200, {"success": True})
            except Exception as e:
                return self.send_json(500, {"error": str(e)})

        self.send_json(404, {"error": f"Endpoint not found: {resolved_path}"})
