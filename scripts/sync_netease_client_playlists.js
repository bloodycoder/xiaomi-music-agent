#!/usr/bin/env node
// Merge playlist IDs visible in the logged-in NeteaseMusic desktop client into runtime/playlists.json.
// This is useful when pyncm is logged into a different account than the desktop client.
const fs = require('fs');
const path = require('path');
const WebSocket = require(`${ROOT}/agents/netease-music-mcp/node_modules/ws`);

const ROOT = process.env.XIAOMI_MUSIC_ROOT || `${process.env.HOME}/xiaomi-music`;
const OUT = path.join(ROOT, 'runtime/playlists.json');

async function getWSUrl() {
  const resp = await fetch('http://127.0.0.1:9222/json');
  const pages = await resp.json();
  const page = pages.find(p => (p.url || '').includes('orpheus://'));
  if (!page) throw new Error('NeteaseMusic CDP target not found');
  return page.webSocketDebuggerUrl;
}

function connectCDP(wsUrl) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    let msgId = 0;
    function evalJS(expression, timeout = 12000) {
      return new Promise((res, rej) => {
        const id = ++msgId;
        const timer = setTimeout(() => rej(new Error('CDP eval timeout')), timeout);
        const handler = (d) => {
          const m = JSON.parse(d);
          if (m.id === id) {
            clearTimeout(timer);
            ws.off('message', handler);
            if (m.result?.exceptionDetails) rej(new Error((m.result.exceptionDetails.exception && m.result.exceptionDetails.exception.description) || m.result.exceptionDetails.text || 'JS error'));
            else res(m.result?.result?.value);
          }
        };
        ws.on('message', handler);
        ws.send(JSON.stringify({ id, method: 'Runtime.evaluate', params: { expression, awaitPromise: true } }));
      });
    }
    ws.on('open', () => resolve({ ws, evalJS }));
    ws.on('error', reject);
  });
}

async function scrapeClientPlaylists() {
  const { ws, evalJS } = await connectCDP(await getWSUrl());
  try {
    const raw = await evalJS(`
      (() => {
        function parseLog(el) {
          try { return JSON.parse(el.dataset.log || '{}'); } catch { return {}; }
        }
        const bodyText = document.body.innerText || '';
        const currentCreator = (Array.from(document.querySelectorAll('a.user'))[0]?.innerText || '').trim();
        const nodes = Array.from(document.querySelectorAll('[class*=PlayListItemContent], [data-log]'));
        const playlists = [];
        const seen = new Set();
        for (const el of nodes) {
          const text = (el.innerText || el.textContent || '').trim();
          const log = parseLog(el);
          const params = log.params || {};
          const id = params.s_cid || params.resourceId || params.id;
          const type = params.s_ctype || params.type;
          if (!id || !text || type !== 'list') continue;
          if (seen.has(String(id))) continue;
          seen.add(String(id));
          playlists.push({
            id: String(id),
            name: text.split('\\n')[0].trim(),
            count: 0,
            creator: currentCreator || 'NeteaseMusicDesktop',
            is_mine: true,
            source: 'netease_desktop_cdp_visible',
          });
        }
        return JSON.stringify({
          title: document.title,
          currentCreator,
          bodySnippet: bodyText.slice(0, 800),
          playlists,
        });
      })()
    `);
    return JSON.parse(raw);
  } finally {
    ws.close();
  }
}

(async () => {
  const scraped = await scrapeClientPlaylists();
  const existing = fs.existsSync(OUT) ? JSON.parse(fs.readFileSync(OUT, 'utf8')) : { success: true, playlists: [] };
  const byId = new Map();
  for (const pl of existing.playlists || []) byId.set(String(pl.id), { ...pl, id: String(pl.id) });
  for (const pl of scraped.playlists || []) {
    const old = byId.get(String(pl.id)) || {};
    byId.set(String(pl.id), { ...old, ...pl, id: String(pl.id), is_mine: true });
  }
  const merged = {
    ...existing,
    success: true,
    updated_at: Date.now() / 1000,
    desktop_account_hint: scraped.currentCreator || existing.desktop_account_hint || '',
    playlists: Array.from(byId.values()),
  };
  fs.writeFileSync(OUT, JSON.stringify(merged, null, 2) + '\n');
  console.log(JSON.stringify({
    ok: true,
    scraped: scraped.playlists.length,
    merged: merged.playlists.length,
    currentCreator: scraped.currentCreator,
    names: scraped.playlists.map(p => `${p.name}:${p.id}`),
  }, null, 2));
})().catch(e => { console.error(e.stack || e.message); process.exit(1); });
