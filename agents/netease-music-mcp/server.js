#!/usr/bin/env node
const { McpServer } = require('@modelcontextprotocol/sdk/server/mcp.js');
const { StdioServerTransport } = require('@modelcontextprotocol/sdk/server/stdio.js');
const { z } = require('zod');
const { playSong, togglePlay, nextTrack, prevTrack, getStatus } = require('./cdp');

const server = new McpServer({
  name: 'netease-music-cdp',
  version: '1.0.0',
});

function ok(data) {
  return { content: [{ type: 'text', text: typeof data === 'string' ? data : JSON.stringify(data) }] };
}

function err(e) {
  return { content: [{ type: 'text', text: `Error: ${e.message}` }], isError: true };
}

server.tool('music_play', 'Search and play a song on NeteaseMusic', { query: z.string().describe('Song name, optionally with artist (e.g. "富士山下 陈奕迅")') }, async ({ query }) => {
  try {
    const result = await playSong(query);
    return result.success ? ok(`Now playing: ${result.played}`) : ok(`Failed: ${result.error}`);
  } catch (e) { return err(e); }
});

server.tool('music_pause', 'Toggle play/pause on NeteaseMusic', {}, async () => {
  try { return ok(await togglePlay()); } catch (e) { return err(e); }
});

server.tool('music_next', 'Skip to next track on NeteaseMusic', {}, async () => {
  try { return ok(await nextTrack()); } catch (e) { return err(e); }
});

server.tool('music_prev', 'Go to previous track on NeteaseMusic', {}, async () => {
  try { return ok(await prevTrack()); } catch (e) { return err(e); }
});

server.tool('music_status', 'Get current playback status (title, artist, playing state, cover art)', {}, async () => {
  try { return ok(await getStatus()); } catch (e) { return err(e); }
});

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch(console.error);
