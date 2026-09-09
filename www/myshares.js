/* MirrorLAN Multi-room sharer grid logic (extracted from myshares.html; loaded after shared.js). */
// Multi-room sharer: every card is a full live sharer (own capture stream,
// own heartbeat + claim loop + peer connections), same protocol as Sharer.html.
const grid = $('grid'), empty = $('empty');
const overlay = $('overlay'), mname = $('mname'), mmax = $('mmax'), merr = $('merr');
const cards = new Map(); // room name -> card {room,maxv,stream,pcs,pollTimer,netFails,starting,els}

function paintEmpty(){
  empty.style.display = cards.size ? 'none' : 'block';
}
function openModal(){
  merr.textContent = '';
  mname.value = '';
  mmax.value = '1';
  overlay.classList.add('open');
  setTimeout(() => { try{ mname.focus(); }catch(e){} }, 0);
}
function closeModal(){
  overlay.classList.remove('open');
}
function setStat(card, t){
  const el = card.els.stat;
  if(!t){ el.style.display = 'none'; return; }
  el.textContent = t;
  el.style.display = 'block';
}

function updateViewers(card){
  const n = Object.keys(card.pcs).length;
  card.els.count.textContent = n + '/' + card.maxv;
}
function audioTracks(card){
  try{ return card.stream && card.stream.getAudioTracks ? card.stream.getAudioTracks().length : 0; }
  catch(e){ return 0; }
}
function paintAud(card){
  const n = audioTracks(card);
  card.els.aud.style.background = n ? 'rgba(34,197,94,.9)' : 'rgba(255,255,255,.25)';
  card.els.aud.title = n ? 'sharing with audio' : 'video only - no audio track';
}
function dropGone(card, gone){
  if(!Array.isArray(gone)) return;
  for(const id of gone){
    const pc = card.pcs[id];
    if(!pc) continue;
    log('[' + card.room + '] viewer ' + id + ' left - dropping at once');
    try{ pc.onconnectionstatechange = null; pc.close(); }catch(e){}
    delete card.pcs[id];
  }
}
function roomPing(card, leave){
  try{
    const body = JSON.stringify({ room: card.room });
    if(leave && navigator.sendBeacon) navigator.sendBeacon('api/sharer/leave', new Blob([body], {type:'application/json'}));
    else fetch(leave ? 'api/sharer/leave' : 'api/sharer/heartbeat', { method:'POST', headers:{'Content-Type':'application/json'}, body, keepalive:true });
  }catch(e){}
}

async function pollOffers(card){
  roomPing(card, false);
  const full = Object.keys(card.pcs).length >= card.maxv;
  try{
    const r = await fetch('api/claim', { method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ room: card.room, known: Object.keys(card.pcs), accept: !full }) });
    let o = null;
    try{ o = await r.json(); }catch(e){}
    if(o) dropGone(card, o.gone);
    if(r.ok && o && !o.waiting && o.id && o.sdp){
      if(card.pcs[o.id]){
        log('[' + card.room + '] viewer ' + o.id + ' re-offered - replacing stale link');
        try{ card.pcs[o.id].onconnectionstatechange = null; card.pcs[o.id].close(); }catch(e){}
        delete card.pcs[o.id];
      }
      await serveViewer(card, o.id, o.sdp);
    }
    if(card.netFails){ card.netFails = 0; setStat(card, null); }
  }catch(e){ if(++card.netFails === 3) setStat(card, 'Connection lost — retrying…'); }
  updateViewers(card);
}

async function serveViewer(card, id, offerSdp){
  try{
    const offCands = countCands(offerSdp);
    log('[' + card.room + '] offer from ' + id + ' (' + offCands + ' remote, ' + relayCount(offerSdp) + ' relay cands)');
    const pc = new RTCPeerConnection({ iceServers: await getIceServers() });
    card.pcs[id] = pc;
    card.stream.getTracks().forEach(t => pc.addTrack(t, card.stream));
    pc.oniceconnectionstatechange = () => {
      if(card.pcs[id] !== pc) return;
      log('[' + card.room + '] viewer ' + id + ' ice: ' + pc.iceConnectionState);
    };
    pc.onconnectionstatechange = () => {
      updateViewers(card);
      const s = pc.connectionState;
      log('[' + card.room + '] viewer ' + id + ' connection: ' + s + ' (ice=' + pc.iceConnectionState + ')');
      if(s === 'failed') log('[' + card.room + '] viewer ' + id + ' failed - check same network');
      if(s === 'closed' || s === 'disconnected' || s === 'failed'){
        const wait = (s === 'closed') ? 0 : 5000;
        setTimeout(() => {
          if(card.pcs[id] !== pc) return;
          const cur = pc.connectionState;
          if(cur !== 'closed' && cur !== 'disconnected' && cur !== 'failed') return;
          try{pc.close();}catch(e){}
          delete card.pcs[id];
          updateViewers(card);
        }, wait);
      }
    };
    await pc.setRemoteDescription({ type: 'offer', sdp: offerSdp });
    const answer = await pc.createAnswer();
    await pc.setLocalDescription(answer);
    await new Promise(res => {
      if(pc.iceGatheringState === 'complete') return res();
      const f = () => { if(pc.iceGatheringState === 'complete'){ pc.removeEventListener('icegatheringstatechange', f); res(); } };
      pc.addEventListener('icegatheringstatechange', f);
      setTimeout(res, 3000);
    });
    const ansSdp = pc.localDescription.sdp;
    const ansCands = countCands(ansSdp);
    await fetch('api/answer', { method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ id, sdp: ansSdp, room: card.room, cands: ansCands }) });
    log('[' + card.room + '] serving viewer ' + id + ' (' + ansCands + ' local, ' + relayCount(ansSdp) + ' relay candidates)');
  }catch(e){
    console.error(e);
    delete card.pcs[id];
    log('[' + card.room + '] serve failed for ' + id + ': ' + e.message);
  }
}

let cardSeq = 0;

function paintCardBtns(card){
  // Same toggle as Sharer.html: Share shows only when idle, Stop while live.
  const active = !!card.stream || card.starting;
  card.els.share.style.display = active ? 'none' : '';
  card.els.stop.style.display = active ? '' : 'none';
}

function stopLive(card){
  // Same as Sharer.html stopAll but per card: the card stays so the same
  // room can be re-shared with its Share button.
  roomPing(card, true);
  if(card.pollTimer){ clearInterval(card.pollTimer); card.pollTimer = null; }
  for(const id of Object.keys(card.pcs)){ try{card.pcs[id].close();}catch(e){} }
  card.pcs = {};
  if(card.stream){ try{card.stream.getTracks().forEach(t=>t.stop());}catch(e){} card.stream = null; }
  try{ card.els.video.srcObject = null; }catch(e){}
  card.els.share.disabled = false;
  card.els.stop.disabled = true;
  card.starting = false;
  paintCardBtns(card);
  paintAud(card);
  updateViewers(card);
  setStat(card, null);
  log('stopped: ' + (card.room || '(default)'));
  maybeDisarm();
}

function stopCard(name){
  const card = cards.get(name);
  if(!card) return;
  stopLive(card);
  cards.delete(name);
  try{ card.els.root.remove(); }catch(e){}
  paintEmpty();
}

// Same auto-hide as Sharer.html keepBarAwake, but scoped per card: the bar
// slides down after 5s without interaction on that card, and wakes on any.
function keepCardBarAwake(root, bar){
  let idleT = null;
  const poke = () => {
    bar.classList.remove('hidden');
    if(idleT) clearTimeout(idleT);
    idleT = setTimeout(() => bar.classList.add('hidden'), 5000);
  };
  ['touchstart','mousemove','mousedown','keydown','wheel','click'].forEach(ev => root.addEventListener(ev, poke, {passive:true}));
  poke();
}

function makeCard(name, maxv){
  cardSeq++;
  const root = document.createElement('div');
  root.className = 'card';
  root.id = 'card-' + cardSeq;
  const dot = document.createElement('span');
  dot.className = 'live';
  const aud = document.createElement('span');
  aud.className = 'aud';
  aud.title = 'audio track';
  const video = document.createElement('video');
  video.autoplay = true;
  video.muted = true;
  video.playsInline = true;
  const count = document.createElement('div');
  count.className = 'count';
  count.textContent = '0/' + maxv;
  const stat = document.createElement('div');
  stat.className = 'stat';
  // Same control bar as Sharer.html: Share / Stop / Fullscreen.
  const bar = document.createElement('div');
  bar.className = 'bar';
  const share = document.createElement('button');
  share.title = 'Share';
  share.setAttribute('aria-label', 'Share ' + (name || 'default'));
  share.innerHTML = '<svg viewBox="0 0 24 24" width="22" height="22" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M9 20h6M12 16v4"/></svg>';
  const stop = document.createElement('button');
  stop.className = 'danger';
  stop.title = 'Stop';
  stop.setAttribute('aria-label', 'Stop ' + (name || 'default'));
  stop.innerHTML = '<svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor"><rect x="7" y="7" width="10" height="10" rx="1"/></svg>';
  stop.disabled = true;
  const full = document.createElement('button');
  full.className = 'blue';
  full.title = 'Fullscreen';
  full.setAttribute('aria-label', 'Fullscreen ' + (name || 'default'));
  full.id = 'full-' + cardSeq;
  bar.append(share, stop, full);
  keepCardBarAwake(root, bar);
  const label = document.createElement('div');
  label.className = 'name';
  label.textContent = name || '(default)';
  const x = document.createElement('button');
  x.className = 'close';
  x.title = 'Remove card';
  x.setAttribute('aria-label', 'Remove card ' + (name || 'default'));
  x.textContent = '×';
  x.onclick = () => stopCard(name);
  root.append(dot, aud, video, count, stat, bar, label, x);
  grid.append(root);
  setupFullscreen(root.id, full.id);
  return { root, video, count, aud, stat, share, stop, full };
}

async function startCapture(card){
  card.starting = true;
  paintCardBtns(card);
  let stream = null;
  try{
    stream = await navigator.mediaDevices.getDisplayMedia({ video: { frameRate: { ideal: 15, max: 30 } }, audio: true });
  }catch(e){
    try{ stream = await navigator.mediaDevices.getDisplayMedia({ video: true }); }
    catch(e2){ card.starting = false; paintCardBtns(card); alert('Screen capture failed: ' + (e2 && e2.name || 'error')); log('capture failed: ' + (e2 && e2.message || e2)); return; }
  }
  card.starting = false;
  card.stream = stream;
  card.els.video.srcObject = stream;
  try{ const p = card.els.video.play(); if(p && p.catch) p.catch(() => {}); }catch(e){}
  card.els.share.disabled = true;
  card.els.stop.disabled = false;
  paintCardBtns(card);
  paintAud(card);
  updateViewers(card);
  log('sharing live: ' + card.room + ' max: ' + card.maxv);
  try{
    stream.getVideoTracks()[0].addEventListener('ended', () => stopLive(card));
  }catch(e){}
  card.pollTimer = setInterval(() => pollOffers(card), 1000);
  pollOffers(card);
  armGuard();
}

function addShare(name, maxv){
  const card = { room: name, maxv, stream: null, pcs: {}, pollTimer: null, netFails: 0, starting: false, els: null };
  card.els = makeCard(name, maxv);
  card.els.share.onclick = () => { if(!card.stream && !card.starting) startCapture(card); };
  card.els.stop.onclick = () => stopLive(card);
  cards.set(name, card);
  paintCardBtns(card);
  paintAud(card);
  updateViewers(card);
  paintEmpty();
  startCapture(card);
}

$('add').onclick = openModal;
$('mcancel').onclick = closeModal;
overlay.addEventListener('click', e => { if(e.target === overlay) closeModal(); });
document.addEventListener('keydown', e => {
  if(e.key !== 'Escape') return;
  if(leaveov.classList.contains('open')) closeLeaveModal();
  else if(roomsov.classList.contains('open')) closeRooms();
  else if(overlay.classList.contains('open')) closeModal();
});
mname.addEventListener('keydown', e => { if(e.key === 'Enter') $('mok').click(); });
$('mok').onclick = () => {
  const v = cleanRoom(mname.value);
  if(!ROOM_RE.test(v)){ merr.textContent = 'Use a-z 0-9 - _ (1-32 chars).'; mname.focus(); return; }
  if(cards.has(v)){ merr.textContent = 'This room is already sharing here.'; mname.focus(); return; }
  let n = parseInt(mmax.value, 10);
  if(!Number.isFinite(n)) n = 1;
  n = Math.min(99, Math.max(1, n));
  closeModal();
  addShare(v, n);
};

// ---- In-page Rooms panel: browse live rooms without leaving this page
// (leaving would kill every capture). Watch links open in a new tab.
const roomsov = $('roomsov'), roomslist = $('roomslist'), roomsempty = $('roomsempty');
let roomsTimer = null;
async function refreshRooms(){
  try{
    const r = await fetch('api/rooms');
    if(!r.ok) throw new Error(r.status);
    const data = await r.json();
    const rooms = (data.rooms || []).filter(x => x.live);
    roomslist.querySelectorAll('.rrow').forEach(e => e.remove());
    roomsempty.style.display = rooms.length ? 'none' : 'block';
    roomsempty.textContent = 'No live rooms.';
    for(const x of rooms){
      const row = document.createElement('div');
      row.className = 'rrow';
      const dot = document.createElement('span');
      dot.className = 'rdot';
      const nm = document.createElement('span');
      nm.className = 'rname';
      nm.textContent = x.room || '(default)';
      const btn = document.createElement('button');
      btn.className = 'watch';
      btn.textContent = 'Watch';
      const room = x.room;
      btn.onclick = () => { window.open('Viewer.html' + (room ? '?room=' + encodeURIComponent(room) : ''), '_blank', 'noopener'); };
      row.append(dot, nm, btn);
      roomslist.append(row);
    }
  }catch(e){
    roomsempty.style.display = 'block';
    roomsempty.textContent = 'Cannot reach server.';
  }
}
function openRooms(){
  roomsov.classList.add('open');
  refreshRooms();
  if(roomsTimer) clearInterval(roomsTimer);
  roomsTimer = setInterval(refreshRooms, 3000);
}
function closeRooms(){
  roomsov.classList.remove('open');
  if(roomsTimer){ clearInterval(roomsTimer); roomsTimer = null; }
}
$('roomsbtn').onclick = openRooms;
$('roomsclose').onclick = closeRooms;
roomsov.addEventListener('click', e => { if(e.target === roomsov) closeRooms(); });

// ---- Back-button guard: leaving this page kills every capture + peer
// connection, so the browser Back button is trapped while anything is live
// and a strong confirmation decides instead. Tab close / refresh / typed
// URLs can't be trapped the same way — beforeunload below is their fallback.
const leaveov = $('leaveov'), leavemsg = $('leavemsg');
let guardActive = false, suppressGuardOnce = false;
function liveCount(){
  try{ return Array.from(cards.values()).filter(c => !!c.stream).length; }
  catch(e){ return 0; }
}
function armGuard(){
  if(guardActive) return;
  guardActive = true;
  try{ history.pushState({ mlGuard: 1 }, ''); }catch(e){}
}
function maybeDisarm(){
  // Last live share ended: silently drop the guard entry (if still on top).
  if(!guardActive || liveCount() > 0) return;
  guardActive = false;
  suppressGuardOnce = true;
  try{ history.back(); }catch(e){}
}
function openLeaveModal(n){
  leavemsg.innerHTML = 'Going back will <b>STOP all ' + n + ' live share(s)</b> at once. Viewers will be cut off.';
  leaveov.classList.add('open');
}
function closeLeaveModal(){
  leaveov.classList.remove('open');
}
window.addEventListener('popstate', () => {
  guardActive = false;
  if(suppressGuardOnce){ suppressGuardOnce = false; return; }
  const n = liveCount();
  if(n > 0){ openLeaveModal(n); armGuard(); }
});
function stopAllCards(){
  for(const name of Array.from(cards.keys())) stopCard(name);
}
$('leavestay').onclick = closeLeaveModal;
$('leavego').onclick = () => {
  closeLeaveModal();
  stopAllCards(); // drops the guard on the way out
  try{ history.back(); }catch(e){} // exit to the previous page
};
leaveov.addEventListener('click', e => { if(e.target === leaveov) closeLeaveModal(); });

paintEmpty();
window.addEventListener('beforeunload', (e) => {
  // Fallback for tab close / refresh / typed URLs (History can't trap those).
  // Browsers show their own generic text — setting returnValue only triggers it.
  // Warn only when something is actually live; idle shells cost nothing.
  try{
    if(liveCount() > 0){ e.preventDefault(); e.returnValue = ''; }
  }catch(err){}
  for(const name of Array.from(cards.keys())) stopCard(name);
});
