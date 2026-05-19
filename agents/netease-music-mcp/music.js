#!/usr/bin/env node
const { playSong, playPlaylist, clearQueue, togglePlay, nextTrack, prevTrack, getStatus } = require('./cdp');

const [,, action, ...args] = process.argv;
const run = (fn) => fn().then(r => console.log(JSON.stringify(r))).catch(e => { console.error(`Error: ${e.message}`); process.exit(1); });

switch (action) {
  case 'play':
    if (args.length > 0) {
      playSong(args.join(' '))
        .then(r => { if (r.success) console.log(`Now playing: ${r.played}`); else { console.error(`Failed: ${r.error}`); process.exit(1); } })
        .catch(e => { console.error(`Error: ${e.message}`); process.exit(1); });
    } else {
      run(togglePlay);
    }
    break;
  case 'playlist':
    if (args.length > 0) {
      playPlaylist(args[0])
        .then(r => { if (r.success) console.log(`Playlist play: ${JSON.stringify(r)}`); else { console.error(`Failed: ${JSON.stringify(r)}`); process.exit(1); } })
        .catch(e => { console.error(`Error: ${e.message}`); process.exit(1); });
    } else {
      console.error('Usage: node music.js playlist <playlist_id>'); process.exit(1);
    }
    break;
  case 'clear': run(clearQueue); break;
  case 'pause': run(togglePlay); break;
  case 'next': run(nextTrack); break;
  case 'prev': run(prevTrack); break;
  case 'status': run(getStatus); break;
  default:
    console.log('Usage: node music.js <play [query]|playlist <id>|clear|pause|next|prev|status>');
}
