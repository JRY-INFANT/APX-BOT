const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

// 1. Load Configuration
function loadEnv() {
  const envPath = path.join(process.cwd(), '.env');
  const config = {
    PORT: process.env.PORT || 5000,
    DASHBOARD_PASSWORD: process.env.DASHBOARD_PASSWORD || '0000',
    DISCORD_TOKEN: process.env.DISCORD_TOKEN || ''
  };

  if (fs.existsSync(envPath)) {
    try {
      const lines = fs.readFileSync(envPath, 'utf8').split('\n');
      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed || trimmed.startsWith('#')) continue;
        const idx = trimmed.indexOf('=');
        if (idx !== -1) {
          const key = trimmed.slice(0, idx).trim();
          const val = trimmed.slice(idx + 1).trim();
          config[key] = val;
        }
      }
    } catch (e) {}
  }
  return config;
}

const env = loadEnv();
const activeSessions = new Set();
const activityLogs = [];

function addLog(type, details, status = 'success') {
  const time = new Date().toLocaleTimeString();
  activityLogs.unshift({ time, type, details, status });
  if (activityLogs.length > 60) activityLogs.pop();
}

// 2. Authentication Helper
function isAuth(req) {
  const token = req.headers['x-session-token'] || '';
  return activeSessions.has(token);
}

// 3. Discord REST API Helper
async function discordRequest(endpoint, options = {}) {
  const token = process.env.DISCORD_TOKEN || env.DISCORD_TOKEN;
  if (!token) throw new Error('DISCORD_TOKEN is missing in environment variables');

  const url = `https://discord.com/api/v10${endpoint}`;
  const headers = {
    Authorization: `Bot ${token}`,
    'Content-Type': 'application/json',
    ...(options.headers || {})
  };

  const response = await fetch(url, { ...options, headers });
  const data = await response.json().catch(() => null);

  if (!response.ok) {
    const errorMsg = data?.message || `Discord API error (${response.status})`;
    const err = new Error(errorMsg);
    err.status = response.status;
    err.discordData = data;
    throw err;
  }
  return data;
}

// 4. JSON Response Helper
function sendJson(res, statusCode, data) {
  res.writeHead(statusCode, {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'Content-Type, X-Session-Token',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS'
  });
  res.end(JSON.stringify(data));
}

// 5. Read Request Body Helper
function readBody(req) {
  return new Promise((resolve, reject) => {
    let body = '';
    req.on('data', chunk => { body += chunk; });
    req.on('end', () => {
      try {
        resolve(body ? JSON.parse(body) : {});
      } catch (e) {
        reject(new Error('Invalid JSON format'));
      }
    });
    req.on('error', reject);
  });
}

// 6. Request Handler (Works for Node HTTP and Vercel Serverless)
async function handleRequest(req, res) {
  const urlObj = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  const pathname = urlObj.pathname;
  const method = req.method;

  // Handle CORS Preflight
  if (method === 'OPTIONS') {
    res.writeHead(204, {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Headers': 'Content-Type, X-Session-Token',
      'Access-Control-Allow-Methods': 'GET, POST, OPTIONS'
    });
    return res.end();
  }

  try {
    // Serve Static HTML Dashboard
    if ((pathname === '/' || pathname === '/index.html') && method === 'GET') {
      const candidates = [
        path.join(__dirname, 'templates', 'index.html'),
        path.join(process.cwd(), 'templates', 'index.html'),
        path.join(__dirname, '..', 'templates', 'index.html')
      ];
      let templatePath = candidates.find(p => fs.existsSync(p));
      if (templatePath) {
        res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
        return fs.createReadStream(templatePath).pipe(res);
      }
      return sendJson(res, 404, { error: 'templates/index.html not found' });
    }

    // Auth: Login
    if (pathname === '/api/login' && method === 'POST') {
      const body = await readBody(req);
      const password = (body.password || '').trim();
      const targetPassword = process.env.DASHBOARD_PASSWORD || env.DASHBOARD_PASSWORD || '0000';

      if (password === targetPassword) {
        const token = crypto.randomBytes(24).toString('hex');
        activeSessions.add(token);
        addLog('Auth', 'Operator unlocked private dashboard', 'success');
        return sendJson(res, 200, { success: true, token });
      }
      addLog('Auth Failed', 'Invalid password attempted on dashboard', 'error');
      return sendJson(res, 401, { error: 'Access Denied: Incorrect operator password' });
    }

    // Auth: Verify Session
    if (pathname === '/api/verify' && method === 'GET') {
      return sendJson(res, isAuth(req) ? 200 : 401, { valid: isAuth(req) });
    }

    // Auth: Logout
    if (pathname === '/api/logout' && method === 'POST') {
      const token = req.headers['x-session-token'];
      if (token) activeSessions.delete(token);
      return sendJson(res, 200, { success: true });
    }

    // Protected API Endpoints
    if (!isAuth(req)) {
      return sendJson(res, 401, { error: 'Unauthorized: Session invalid or expired' });
    }

    // API: Bot Status & Guilds
    if (pathname === '/api/status' && method === 'GET') {
      try {
        const start = Date.now();
        const botUser = await discordRequest('/users/@me');
        const ping = Date.now() - start;
        const guilds = await discordRequest('/users/@me/guilds');

        const guildsList = (guilds || []).map(g => ({
          id: g.id,
          name: g.name,
          icon: g.icon ? `https://cdn.discordapp.com/icons/${g.id}/${g.icon}.png` : null,
          member_count: g.approximate_member_count || 'Guild'
        }));

        const avatarUrl = botUser.avatar
          ? `https://cdn.discordapp.com/avatars/${botUser.id}/${botUser.avatar}.png`
          : 'https://cdn.discordapp.com/embed/avatars/0.png';

        return sendJson(res, 200, {
          online: true,
          bot_name: botUser.username,
          bot_id: botUser.id,
          avatar: avatarUrl,
          ping: ping,
          guild_count: guildsList.length,
          guilds: guildsList
        });
      } catch (err) {
        return sendJson(res, 500, {
          online: false,
          error: err.message,
          hint: 'Verify DISCORD_TOKEN is added in Environment Variables'
        });
      }
    }

    // API: Channels in a Guild
    if (pathname === '/api/channels' && method === 'GET') {
      const guildId = urlObj.searchParams.get('guild_id');
      if (!guildId) return sendJson(res, 400, { error: 'Missing guild_id parameter' });

      const channels = await discordRequest(`/guilds/${guildId}/channels`);
      const textChannels = (channels || [])
        .filter(c => c.type === 0 || c.type === 5)
        .map(c => ({
          id: c.id,
          name: c.name,
          category: 'Text Channels'
        }));

      return sendJson(res, 200, { channels: textChannels });
    }

    // API: Send Message / Embed
    if (pathname === '/api/send' && method === 'POST') {
      const body = await readBody(req);
      const mode = body.mode || 'channel';
      const message = (body.message || '').trim();
      const isEmbed = !!body.is_embed;
      const embedTitle = (body.embed_title || '').trim();
      const embedColorHex = (body.embed_color || '#5865f2').replace('#', '');
      const embedColor = parseInt(embedColorHex, 16) || 0x5865f2;

      if (!message && !embedTitle) {
        return sendJson(res, 400, { error: 'Message content or embed title is required' });
      }

      const payload = {};
      if (message && !isEmbed) {
        payload.content = message;
      }

      if (isEmbed) {
        payload.embeds = [{
          title: embedTitle || undefined,
          description: message || undefined,
          color: embedColor,
          timestamp: new Date().toISOString(),
          footer: { text: 'Dispatched via Alpha-X Console' }
        }];
      }

      // Mode: Guild Channel
      if (mode === 'channel') {
        const channelId = body.channel_id;
        if (!channelId) return sendJson(res, 400, { error: 'channel_id is required' });

        const sent = await discordRequest(`/channels/${channelId}/messages`, {
          method: 'POST',
          body: JSON.stringify(payload)
        });

        addLog('Message Sent', `Dispatched to channel ID ${channelId}: ${(embedTitle || message).slice(0, 40)}`, 'success');
        return sendJson(res, 200, { success: true, message_id: sent.id, channel: channelId });
      }

      // Mode: User DM
      if (mode === 'dm') {
        const userId = body.user_id;
        if (!userId) return sendJson(res, 400, { error: 'user_id is required' });

        const dmChannel = await discordRequest('/users/@me/channels', {
          method: 'POST',
          body: JSON.stringify({ recipient_id: userId })
        });

        const sent = await discordRequest(`/channels/${dmChannel.id}/messages`, {
          method: 'POST',
          body: JSON.stringify(payload)
        });

        addLog('DM Sent', `Dispatched DM to User ID ${userId}: ${(embedTitle || message).slice(0, 40)}`, 'success');
        return sendJson(res, 200, { success: true, message_id: sent.id, user: userId });
      }

      return sendJson(res, 400, { error: 'Invalid mode' });
    }

    // API: Activity Logs
    if (pathname === '/api/logs' && method === 'GET') {
      return sendJson(res, 200, { logs: activityLogs });
    }

    sendJson(res, 404, { error: 'Endpoint not found' });
  } catch (err) {
    console.error('[Dashboard Error]', err);
    sendJson(res, err.status || 500, { error: err.message });
  }
}

// 7. If executed directly via `node server.js`
if (require.main === module) {
  const PORT = parseInt(process.env.PORT || env.PORT, 10) || 5000;
  const server = http.createServer(handleRequest);
  server.listen(PORT, '0.0.0.0', () => {
    console.log('=======================================================');
    console.log(`🚀 ALPHA - X CUSTOM DASHBOARD IS LIVE!`);
    console.log(`   Local URL:    http://localhost:${PORT}`);
    console.log(`   Operator Key: ${process.env.DASHBOARD_PASSWORD || env.DASHBOARD_PASSWORD || '0000'}`);
    console.log('=======================================================');
  });
}

// 8. Export for Vercel Serverless
module.exports = handleRequest;
