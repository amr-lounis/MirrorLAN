/* MirrorLAN shared helpers: stage pages (Viewer/My Shares) + room-name validation.
 * Freshness is enforced by the server (Cache-Control: no-cache on
 * html/js/css), so no ?v= cache-busters are needed. */
const $ = id => document.getElementById(id);

// Ring buffer of recent logs + optional on-screen panel (?debug=1).
// Phones with a black screen can't open devtools, so ?debug=1 shows the
// same lines on the page itself (e.g. Viewer.html?room=n&debug=1).
window.__mlLogs = window.__mlLogs || [];
const DEBUG = (() => { try{ return new URLSearchParams(location.search).has('debug'); }catch(e){ return false; } })();
function _paintDbg(t){
  try{
    let el = document.getElementById('dbg');
    if(!el && DEBUG){
      el = document.createElement('div');
      el.id = 'dbg';
      el.style.cssText = 'position:fixed;left:8px;right:8px;bottom:76px;max-height:38vh;overflow:auto;'
        + 'background:rgba(0,0,0,.82);color:#9fe8b8;font:11px/1.5 Consolas,monospace;'
        + 'padding:8px 10px;border-radius:10px;z-index:50;white-space:pre-wrap;word-break:break-word';
      document.body.appendChild(el);
    }
    if(el){ el.textContent += t + '\n'; el.scrollTop = el.scrollHeight; }
  }catch(e){}
}
function log(t){
  try{
    const line = new Date().toISOString().slice(11, 19) + ' ' + t;
    window.__mlLogs.push(line);
    if(window.__mlLogs.length > 200) window.__mlLogs.shift();
    console.log(line);
    _paintDbg(line);
  }catch(e){ try{ console.log(t); }catch(_){} }
}

// Current room from ?room= (lowercased, charset-checked, "" = default room).
const ROOM = ((new URLSearchParams(location.search).get('room') || '').toLowerCase().match(/^[a-z0-9\-_]{0,32}$/) || [''])[0];

// Room-name validation for the "new room" form.
const ROOM_RE = /^[a-z0-9\-_]{1,32}$/;
function cleanRoom(v){ return (v || '').toLowerCase().trim(); }

// ICE candidate lines of an SDP blob.
function candLines(sdp){
  try{ return String(sdp || '').split('\n').filter(l => l.indexOf('a=candidate:') === 0); }
  catch(e){ return []; }
}
// Number of ICE candidates in an SDP blob. Zero means this device cannot
// gather any network path (UDP blocked?) — the link can never form.
function countCands(sdp){ return candLines(sdp).length; }
// True when every candidate is an mDNS hostname (*.local): the browser hides
// literal LAN IPs, so the link needs working multicast DNS (UDP 5353) on the
// LAN. If EITHER side fails to resolve .local, ICE never leaves "new" and
// the screen stays black (signaling still works — it goes over TCP/HTTPS).
function mdnsOnly(sdp){
  const c = candLines(sdp);
  return c.length > 0 && c.every(l => l.indexOf('.local') >= 0);
}
// Relay candidates gathered through the built-in TURN server (literal server
// IP — no mDNS involved). Any number > 0 means the black-screen-proof path
// is available even when multicast DNS is blocked on the LAN.
function relayCount(sdp){
  return candLines(sdp).filter(l => l.indexOf('typ relay') >= 0).length;
}
// TURN relay credentials from the server, or [] when the relay is down.
// Called before creating the peer connection; failure always falls back to
// plain host candidates so nothing breaks when TURN is disabled/offline.
async function getIceServers(){
  try{
    const r = await fetch('api/turn');
    if(r.ok){
      const t = await r.json();
      if(t && t.urls && t.username && t.credential){
        log('turn: ' + t.urls + ' (relay fallback ready)');
        return [{urls: t.urls, username: t.username, credential: t.credential}];
      }
    }
  }catch(e){}
  log('turn unavailable - host candidates only');
  return [];
}

// Small status pill (#toast). No-op on pages without one.
function showToast(t){ const el = document.getElementById('toast'); if(!el) return; el.textContent = t; el.style.display = 'block'; }
function hideToast(){ const el = document.getElementById('toast'); if(el) el.style.display = 'none'; }

// Play a received stream, working around mobile autoplay-with-sound blocks:
// try with sound first, fall back to muted so the picture always shows.
function playWithSound(video){
  video.muted = false;
  try{
    const p = video.play();
    if(p && p.then) p.then(() => {
      try{ log('video playing ' + video.videoWidth + 'x' + video.videoHeight + (video.muted ? ' (muted)' : ' (sound)')); }catch(e){}
    }).catch(() => {
      video.muted = true;
      log('autoplay with sound blocked - muted fallback');
      video.play().then(() => {
        try{ log('video playing ' + video.videoWidth + 'x' + video.videoHeight + ' (muted)'); }catch(e){}
      }).catch(()=>{ log('video play failed (muted too)'); });
    });
  }catch(e){ log('video play threw: ' + (e && e.message)); }
}

// Fullscreen toggle button with iOS fallback (CSS-class based, no Fullscreen API).
const FS_ICON_EXPAND = '<svg viewBox="0 0 24 24" width="22" height="22" stroke="currentColor" stroke-width="2.5" fill="none" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg>';
const FS_ICON_MIN = '<svg viewBox="0 0 24 24" width="22" height="22" stroke="currentColor" stroke-width="2.5" fill="none" stroke-linecap="round" stroke-linejoin="round"><path d="M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5"/></svg>';
function setupFullscreen(stageId, buttonId){
  const stage = $(stageId), btn = $(buttonId);
  const ios = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const on = () => stage.classList.contains('fs') || !!document.fullscreenElement;
  const paint = () => { btn.innerHTML = on() ? FS_ICON_MIN : FS_ICON_EXPAND; };
  paint();
  btn.onclick = async () => {
    try{
      if(on()){ stage.classList.remove('fs'); if(document.fullscreenElement) await document.exitFullscreen(); }
      else if(stage.requestFullscreen && !ios) await stage.requestFullscreen();
      else stage.classList.add('fs');
    }catch(e){ stage.classList.add('fs'); }
    paint();
  };
  document.addEventListener('fullscreenchange', () => {
    if(!document.fullscreenElement) stage.classList.remove('fs');
    paint();
  });
}

// Auto-hide the control bar after 5s without interaction.
function keepBarAwake(barId){
  const bar = $(barId);
  let idleT = null;
  const poke = () => {
    bar.classList.remove('hidden');
    if(idleT) clearTimeout(idleT);
    idleT = setTimeout(() => bar.classList.add('hidden'), 5000);
  };
  ['touchstart','mousemove','mousedown','keydown','wheel','click'].forEach(ev => document.addEventListener(ev, poke, {passive:true}));
  poke();
}
