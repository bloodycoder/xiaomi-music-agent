const WebSocket = require('ws');

async function main() {
  const resp = await fetch('http://127.0.0.1:9222/json');
  const pages = await resp.json();

  // Find NetEase main page (not error page)
  const page = pages.find(p => p.url.includes('orpheus://') && !p.url.includes('data:'));
  if (!page) {
    console.log(JSON.stringify({success: false, error: 'no orpheus page', pages: pages.map(p => p.url)}));
    return;
  }

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise(r => ws.on('open', r));
  let msgId = 0;

  function evalJS(expr, timeout = 5000) {
    return new Promise((res, rej) => {
      const id = ++msgId;
      const t = setTimeout(() => rej(new Error('timeout')), timeout);
      const handler = (d) => {
        const m = JSON.parse(d);
        if (m.id === id) {
          clearTimeout(t);
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

  try {
    // Find and click the play-all button
    const result = await evalJS(`(function(){
      // Method 1: search all elements for "播放全部" text
      const all = document.querySelectorAll('button, a, span, div, [class*=btn]');
      for (const el of all) {
        const text = (el.textContent || '').trim();
        if (text === '播放全部') {
          el.click();
          return 'clicked: ' + el.tagName + '.' + (el.className || '').substring(0, 30);
        }
      }
      // Method 2: find closest clickable parent of "播放全部" text node
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      while (walker.nextNode()) {
        if (walker.currentNode.textContent.trim() === '播放全部') {
          const parent = walker.currentNode.parentElement;
          if (parent) {
            parent.click();
            return 'clicked parent: ' + parent.tagName + '.' + (parent.className || '').substring(0, 30);
          }
        }
      }
      // Method 3: dump page title and look for play-related elements
      const title = document.title;
      const bodyClass = document.body.className;
      return 'not_found. title=' + title + ' bodyClass=' + bodyClass;
    })()`);
    console.log(JSON.stringify({success: true, result}));
  } catch (e) {
    console.log(JSON.stringify({success: false, error: e.message}));
  }
  ws.close();
}
main();
