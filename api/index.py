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
active_operators = {}

def add_log(event_type, details, status="success"):
    now = datetime.now().strftime("%H:%M:%S")
    activity_logs.insert(0, {"time": now, "type": event_type, "details": details, "status": status})
    if len(activity_logs) > 100:
        activity_logs.pop()

def get_secret():
    pw = os.environ.get("DASHBOARD_PASSWORD", "0000").strip() or "0000"
    tok = os.environ.get("DISCORD_TOKEN", "").strip()[:12]
    return f"alphax_{pw}_{tok}".encode("utf-8")

def generate_token(operator_name="Operator"):
    ts = str(int(time.time()))
    safe_name = "".join(c for c in operator_name if c.isalnum() or c in ("-", "_", " "))[:24].strip() or "Operator"
    raw = f"{ts}.{safe_name}"
    sig = hmac.new(get_secret(), raw.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"

def verify_token(token):
    if not token:
        return ("", False)
    parts = token.split(".")
    if len(parts) == 3:
        ts, name, sig = parts
        try:
            if time.time() - int(ts) > 1209600:  # 14 days validity
                return ("", False)
            raw = f"{ts}.{name}"
            expected_sig = hmac.new(get_secret(), raw.encode("utf-8"), hashlib.sha256).hexdigest()
            if hmac.compare_digest(sig, expected_sig):
                return (name, True)
        except Exception:
            return ("", False)
    elif len(parts) == 2:
        ts, sig = parts
        try:
            if time.time() - int(ts) > 1209600:
                return ("", False)
            expected_sig = hmac.new(get_secret(), ts.encode("utf-8"), hashlib.sha256).hexdigest()
            if hmac.compare_digest(sig, expected_sig):
                return ("Operator", True)
        except Exception:
            return ("", False)
    return ("", False)

def parse_device(ua_str):
    if not ua_str:
        return "Unknown Device"
    ua = ua_str.lower()
    os_name = "Windows" if "windows" in ua else "Mac" if "mac" in ua else "iPhone" if "iphone" in ua else "Android" if "android" in ua else "Linux" if "linux" in ua else "Device"
    browser = "Chrome" if "chrome" in ua and "edg" not in ua else "Edge" if "edg" in ua else "Firefox" if "firefox" in ua else "Safari" if "safari" in ua and "chrome" not in ua else "Browser"
    return f"{os_name} • {browser}"

def parse_location(headers):
    city = headers.get("x-vercel-ip-city", "")
    country = headers.get("x-vercel-ip-country", "")
    ip = headers.get("x-forwarded-for", "").split(",")[0].strip() or headers.get("x-real-ip", "")
    parts = []
    if city:
        parts.append(city)
    if country:
        parts.append(country)
    if parts:
        return ", ".join(parts)
    if ip:
        return f"IP: {ip}"
    return "Web Client"

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

    def get_auth_info(self):
        token = self.headers.get("X-Session-Token", "").strip()
        if not token:
            auth_header = self.headers.get("Authorization", "").strip()
            if auth_header.startswith("Bearer "):
                token = auth_header[7:].strip()
        name, valid = verify_token(token)
        if valid:
            now_ts = int(time.time())
            op_key = f"{name}_{token[-8:]}"
            existing = active_operators.get(op_key, {})
            login_time = existing.get("login_time", datetime.now().strftime("%H:%M:%S"))
            active_operators[op_key] = {
                "name": name,
                "device": parse_device(self.headers.get("User-Agent", "")),
                "location": parse_location(self.headers),
                "login_time": login_time,
                "last_seen": now_ts,
                "last_seen_str": datetime.now().strftime("%H:%M:%S")
            }
        return (name, valid)

    def is_auth(self):
        _, valid = self.get_auth_info()
        return valid

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
            name, valid = self.get_auth_info()
            return self.send_json(200 if valid else 401, {"valid": valid, "operator": name})

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
                    "category": "Text Channels"
                } for c in (channels or []) if c.get("type") in (0, 5)]
                return self.send_json(200, {"channels": text_channels})
            except Exception as e:
                return self.send_json(500, {"error": str(e)})

        # 4. Live Channel Chat Messages
        if resolved_path == "/api/messages":
            if not self.is_auth():
                return self.send_json(401, {"error": "Unauthorized: Session invalid or expired"})
            channel_id = query.get("channel_id", [""])[0]
            if not channel_id:
                return self.send_json(400, {"error": "Missing channel_id parameter"})
            try:
                msgs = discord_api(f"/channels/{channel_id}/messages?limit=30")
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

        # 5. Active Operators Tracker
        if resolved_path == "/api/operators":
            if not self.is_auth():
                return self.send_json(401, {"error": "Unauthorized: Session invalid or expired"})
            now = int(time.time())
            active_list = []
            for k, op in list(active_operators.items()):
                # Keep if accessed in last 15 minutes
                if now - op.get("last_seen", 0) < 900:
                    is_online = (now - op.get("last_seen", 0)) < 75
                    active_list.append({
                        "name": op.get("name", "Operator"),
                        "device": op.get("device", "Unknown"),
                        "location": op.get("location", "Web Client"),
                        "login_time": op.get("login_time", "--:--"),
                        "last_seen_str": op.get("last_seen_str", "--:--"),
                        "is_online": is_online
                    })
            active_list.sort(key=lambda x: (x["is_online"], x.get("last_seen_str", "")), reverse=True)
            return self.send_json(200, {
                "operators": active_list,
                "count": len([o for o in active_list if o["is_online"]])
            })

        # 6. Activity Logs
        if resolved_path == "/api/logs":
            if not self.is_auth():
                return self.send_json(401, {"error": "Unauthorized: Session invalid or expired"})
            return self.send_json(200, {"logs": activity_logs})

        # Default: Serve HTML
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
            operator_name = (body.get("operator_name") or "Operator").strip() or "Operator"
            target_pw = os.environ.get("DASHBOARD_PASSWORD", "0000").strip() or "0000"
            if password == target_pw:
                token = generate_token(operator_name)
                device = parse_device(self.headers.get("User-Agent", ""))
                loc = parse_location(self.headers)
                now_str = datetime.now().strftime("%H:%M:%S")
                op_key = f"{operator_name}_{token[-8:]}"
                active_operators[op_key] = {
                    "name": operator_name,
                    "device": device,
                    "location": loc,
                    "login_time": now_str,
                    "last_seen": int(time.time()),
                    "last_seen_str": now_str
                }
                add_log("Operator Login", f"@{operator_name} logged in from {loc} ({device})", "success")
                return self.send_json(200, {"success": True, "token": token, "operator": operator_name})
            add_log("Auth Failed", f"Failed login attempt for '{operator_name}'", "error")
            return self.send_json(401, {"error": "Access Denied: Incorrect operator password"})

        # Heartbeat
        if resolved_path == "/api/heartbeat":
            name, valid = self.get_auth_info()
            return self.send_json(200 if valid else 401, {"alive": valid, "operator": name})

        # Logout
        if resolved_path == "/api/logout":
            name, _ = self.get_auth_info()
            token = self.headers.get("X-Session-Token", "").strip() or self.headers.get("Authorization", "").replace("Bearer ", "").strip()
            if token and name:
                op_key = f"{name}_{token[-8:]}"
                active_operators.pop(op_key, None)
            if name:
                add_log("Operator Logout", f"@{name} logged out", "info")
            return self.send_json(200, {"success": True})

        # Protected: Send Message
        operator_name, is_valid = self.get_auth_info()
        if not is_valid:
            return self.send_json(401, {"error": "Unauthorized: Session invalid or expired"})

        if resolved_path == "/api/send":
            mode = body.get("mode", "channel")
            message = (body.get("message") or "").strip()
            reply_to = body.get("reply_to_id")

            payload = {}
            if message:
                payload["content"] = message

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

                footer_text = body.get("embed_footer", "").strip() or f"Dispatched by @{operator_name}"
                embed_obj["footer"] = {"text": footer_text}

                if body.get("embed_thumbnail"):
                    embed_obj["thumbnail"] = {"url": body["embed_thumbnail"].strip()}
                if body.get("embed_image"):
                    embed_obj["image"] = {"url": body["embed_image"].strip()}

                fields = body.get("embed_fields", [])
                if isinstance(fields, list) and fields:
                    valid_fields = []
                    for f in fields:
                        n = (f.get("name") or "").strip()
                        v = (f.get("value") or "").strip()
                        if n and v:
                            valid_fields.append({"name": n, "value": v, "inline": bool(f.get("inline"))})
                    if valid_fields:
                        embed_obj["fields"] = valid_fields

                if body.get("embed_timestamp"):
                    embed_obj["timestamp"] = datetime.utcnow().isoformat()

                payload["embeds"] = [embed_obj]

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
                    add_log("Message Sent", f"@{operator_name} -> Channel ID {channel_id}: {preview}", "success")
                    return self.send_json(200, {"success": True, "message_id": sent.get("id"), "channel": channel_id})
                elif mode == "dm":
                    user_id = body.get("user_id")
                    if not user_id:
                        return self.send_json(400, {"error": "user_id is required"})
                    dm_channel = discord_api("/users/@me/channels", method="POST", payload={"recipient_id": user_id})
                    sent = discord_api(f"/channels/{dm_channel['id']}/messages", method="POST", payload=payload)
                    preview = (body.get("embed_title") or message)[:40]
                    add_log("DM Sent", f"@{operator_name} -> User ID {user_id}: {preview}", "success")
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
                add_log("Message Deleted", f"@{operator_name} deleted msg {message_id}", "info")
                return self.send_json(200, {"success": True})
            except Exception as e:
                return self.send_json(500, {"error": str(e)})

        self.send_json(404, {"error": f"Endpoint not found: {resolved_path}"})
