const WebSocket = require('ws');

const CDP_URL = 'http://127.0.0.1:9222/json';

async function getWSUrl() {
  const resp = await fetch(CDP_URL);
  const pages = await resp.json();
  const page = pages.find(p => p.url.includes('orpheus://'));
  if (!page) throw new Error('NeteaseMusic not running with CDP. Start it with: open -a NeteaseMusic --args --remote-debugging-port=9222');
  return page.webSocketDebuggerUrl;
}

function connectCDP(wsUrl) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    let msgId = 0;

    function evalJS(expr, timeout = 8000) {
      return new Promise((res, rej) => {
        const id = ++msgId;
        const timer = setTimeout(() => rej(new Error('CDP eval timeout')), timeout);
        const handler = (d) => {
          const m = JSON.parse(d);
          if (m.id === id) {
            clearTimeout(timer);
            ws.off('message', handler);
            if (m.result?.exceptionDetails) {
              rej(new Error(m.result.exceptionDetails.text || 'JS error'));
            } else {
              res(m.result?.result?.value);
            }
          }
        };
        ws.on('message', handler);
        ws.send(JSON.stringify({
          id,
          method: 'Runtime.evaluate',
          params: { expression: expr, awaitPromise: true }
        }));
      });
    }

    ws.on('open', () => resolve({ ws, evalJS }));
    ws.on('error', reject);
  });
}

async function withCDP(fn) {
  const wsUrl = await getWSUrl();
  const { ws, evalJS } = await connectCDP(wsUrl);
  try {
    return await fn(evalJS);
  } finally {
    ws.close();
  }
}

async function playSong(query) {
  return withCDP(async (evalJS) => {
    await evalJS(`
      (async () => {
        const input = document.querySelector('input.cmd-input');
        if (!input) throw new Error('Search input not found');
        input.focus();
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
        setter.call(input, ${JSON.stringify(query)});
        input.dispatchEvent(new Event('input', { bubbles: true }));
        return 'typed';
      })()
    `);

    await new Promise(r => setTimeout(r, 500));
    await evalJS(`
      (async () => {
        const input = document.querySelector('input.cmd-input');
        input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', keyCode: 13, bubbles: true}));
        input.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', keyCode: 13, bubbles: true}));
        return 'searched';
      })()
    `);

    await new Promise(r => setTimeout(r, 2500));

    const result = await evalJS(`
      (async () => {
        const cards = document.querySelectorAll('[class*=TrackCard], [class*=trackCard], [class*=song-item], [class*=SongItem]');
        if (cards.length > 0) {
          cards[0].dispatchEvent(new MouseEvent('dblclick', {bubbles: true, cancelable: true, view: window}));
          return JSON.stringify({success: true, played: cards[0].textContent?.trim()?.substring(0, 80)});
        }
        const allEls = document.querySelectorAll('[class*=Item], [class*=item], [class*=Wrapper]');
        const trackEl = Array.from(allEls).find(el => {
          const cls = el.className || '';
          return (cls.includes('Track') || cls.includes('track') || cls.includes('Song') || cls.includes('song')) &&
                 el.textContent?.length < 200;
        });
        if (trackEl) {
          trackEl.dispatchEvent(new MouseEvent('dblclick', {bubbles: true, cancelable: true, view: window}));
          return JSON.stringify({success: true, played: trackEl.textContent?.trim()?.substring(0, 80)});
        }
        return JSON.stringify({success: false, error: 'No track found in search results'});
      })()
    `);
    return JSON.parse(result);
  });
}

async function setShuffleMode() {
  return withCDP(async (evalJS) => {
    const result = await evalJS(`
      (async () => {
        function currentModeEl() {
          return document.querySelector('footer .cmd-icon-shuffle, footer .cmd-icon-random, footer [aria-label=shuffle], footer [title*=随机], footer .cmd-icon-order, footer .cmd-icon-loop, footer .cmd-icon-singleloop, footer .cmd-icon-one');
        }
        function modeText(el) {
          return el ? String(el.title || el.getAttribute('aria-label') || el.className || '') : '';
        }
        for (let i = 0; i < 5; i++) {
          const el = currentModeEl();
          const before = modeText(el);
          if (/随机|shuffle/i.test(before)) {
            return JSON.stringify({success:true, mode:before, clicks:i});
          }
          if (!el) return JSON.stringify({success:false, error:'mode button not found'});
          const btn = el.closest('button') || el;
          btn.click();
          await new Promise(r => setTimeout(r, 250));
        }
        const el = currentModeEl();
        const after = modeText(el);
        return JSON.stringify({success:/随机|shuffle/i.test(after), mode:after, clicks:5});
      })()
    `, 8000);
    return JSON.parse(result);
  });
}

async function playPlaylist(playlistId, opts = {}) {
  const shuffle = opts.shuffle !== false;
  const { execFileSync } = require('child_process');
  const pid = String(playlistId);
  // URL open is only a fallback. In the desktop app, the reliable route for the
  // user's own visible playlists is clicking the left-side playlist item by its
  // data-log s_cid, then clicking the page-level “播放全部” button.
  try { execFileSync('open', [`orpheus://playlist/${pid}`]); } catch (e) {}

  const playResult = await withCDP(async (evalJS) => {
    const result = await evalJS(`
      (async () => {
        const pid = ${JSON.stringify(pid)};
        function visible(el) {
          if (!el) return false;
          const r = el.getBoundingClientRect();
          const st = getComputedStyle(el);
          return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
        }
        function logParams(el) {
          try { return JSON.parse(el.dataset.log || '{}').params || {}; } catch { return {}; }
        }
        const sidebarItem = Array.from(document.querySelectorAll('[data-log]')).find(el => {
          const p = logParams(el);
          return String(p.s_cid) === pid && p.s_ctype === 'list';
        });
        if (sidebarItem) {
          sidebarItem.click();
          sidebarItem.dispatchEvent(new MouseEvent('dblclick', {bubbles:true, cancelable:true, view:window}));
          await new Promise(r => setTimeout(r, 1800));
        } else {
          await new Promise(r => setTimeout(r, 2500));
        }

        const before = (document.body.innerText || '').slice(0, 1400);
        const candidates = Array.from(document.querySelectorAll('main button, main [role=button], section button, section [role=button], article button, article [role=button], button, [role=button], [class*=PlayAll]'))
          .filter(el => visible(el) && !el.closest('footer'))
          .map((el, idx) => ({el, idx, text:(el.innerText || el.textContent || el.title || el.getAttribute('aria-label') || '').trim(), cls:String(el.className || '')}));
        const play = candidates.find(x => /播放全部|播放/.test(x.text) && !/暂停/.test(x.text))
          || candidates.find(x => /PlayAll|btn-play|icon-play|play/i.test(x.cls) && !/pause|footer/i.test(x.cls));
        if (play) {
          play.el.click();
          return JSON.stringify({success:true, nativeQueue:true, clicked:{text:play.text, cls:play.cls.slice(0,120)}, pageText:before.slice(0,500)});
        }
        return JSON.stringify({success:false, error:'page play-all button not found', sidebarItemFound:!!sidebarItem, candidates:candidates.slice(0,30).map(x=>({text:x.text, cls:x.cls.slice(0,80)})), pageText:before.slice(0,800)});
      })()
    `, 15000);
    return JSON.parse(result);
  });

  if (playResult.success && shuffle) {
    await new Promise(r => setTimeout(r, 800));
    playResult.shuffle = await setShuffleMode().catch(e => ({success:false, error:e.message}));
  }
  return playResult;
}

async function clickFooterButton(iconClass) {
  return withCDP(async (evalJS) => {
    const result = await evalJS(`
      (function() {
        var btn = document.querySelector('footer .cmd-icon-${iconClass}');
        if (!btn) return JSON.stringify({success: false, error: '${iconClass} button not found'});
        var button = btn.closest('button') || btn;
        button.click();
        return JSON.stringify({success: true, action: '${iconClass}'});
      })()
    `);
    return JSON.parse(result);
  });
}

async function togglePlay() { return clickFooterButton('play').catch(() => clickFooterButton('pause')); }
async function nextTrack() { return clickFooterButton('next'); }
async function prevTrack() { return clickFooterButton('pre'); }

async function getStatus() {
  return withCDP(async (evalJS) => {
    const result = await evalJS(`
      (function() {
        var footer = document.querySelector('footer');
        if (!footer) return JSON.stringify({playing: false});
        var img = footer.querySelector('img');
        var cover = img ? img.src : null;
        var side = footer.querySelector('.side');
        var title = '', artist = '';
        if (side) {
          var titleDiv = side.querySelector('.title');
          if (titleDiv) {
            var firstSpan = titleDiv.querySelector('span');
            if (firstSpan) title = firstSpan.textContent.trim();
          }
          var authorEl = side.querySelector('.author a');
          if (authorEl) artist = authorEl.textContent.trim();
        }
        var playBtn = footer.querySelector('.cmd-icon-play');
        var pauseBtn = footer.querySelector('.cmd-icon-pause');
        var isPlaying = !!pauseBtn;
        var modeEl = footer.querySelector('[class*=cmd-icon-order], [class*=cmd-icon-shuffle], [class*=cmd-icon-random], [class*=cmd-icon-loop], [class*=cmd-icon-singleloop], [class*=cmd-icon-one]');
        var mode = modeEl ? (modeEl.title || '') : '';
        return JSON.stringify({playing: isPlaying, title: title, artist: artist, cover: cover, mode: mode});
      })()
    `);
    return JSON.parse(result);
  });
}

module.exports = { playSong, playPlaylist, setShuffleMode, togglePlay, nextTrack, prevTrack, getStatus };
