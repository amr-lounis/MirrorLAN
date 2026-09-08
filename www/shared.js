/* MirrorLAN shared helpers: stage pages (Sharer/Viewer) + room-name validation. */
const $ = id => document.getElementById(id);

function log(t){ console.log(t); }

// Current room from ?room= (lowercased, charset-checked, "" = default room).
const ROOM = ((new URLSearchParams(location.search).get('room') || '').toLowerCase().match(/^[a-z0-9\-_]{0,32}$/) || [''])[0];

// Room-name validation for the "new room" form.
const ROOM_RE = /^[a-z0-9\-_]{1,32}$/;
function cleanRoom(v){ return (v || '').toLowerCase().trim(); }

// Small status pill (#toast). No-op on pages without one.
function showToast(t){ const el = document.getElementById('toast'); if(!el) return; el.textContent = t; el.style.display = 'block'; }
function hideToast(){ const el = document.getElementById('toast'); if(el) el.style.display = 'none'; }

// Play a received stream, working around mobile autoplay-with-sound blocks:
// try with sound first, fall back to muted so the picture always shows.
function playWithSound(video){
  video.muted = false;
  video.play().catch(() => { video.muted = true; video.play().catch(()=>{}); });
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
