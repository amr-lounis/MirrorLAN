/* MirrorLAN Single-room sharer logic (extracted from Sharer.html; loaded after shared.js). */
const preview = $('preview');
let screenStream = null, pcs = {}, pollTimer = null, starting = false, netFails = 0;

function updateViewers(){
  const n = Object.keys(pcs).length;
  $('count').textContent = n + '/' + MAXV;
  log('viewers: ' + n + '/' + MAXV);
}

function paintSharerBtns(){
  // Play shows only when idle; X shows while sharing or picking a source.
  const active = !!screenStream || starting;
  $('share').style.display = active ? 'none' : '';
  $('stop').style.display = active ? '' : 'none';
}

function audioTracks(){
  try{ return screenStream && screenStream.getAudioTracks ? screenStream.getAudioTracks().length : 0; }
  catch(e){ return 0; }
}

function paintAud(){
  // Green dot = an audio track is really being captured (not just requested).
  // Gray = video only: the picked source/browser gave no audio track.
  const n = audioTracks();
  const el = $('aud');
  el.dataset.audio = n ? '1' : '0';
  el.style.background = n ? 'rgba(34,197,94,.9)' : 'rgba(255,255,255,.25)';
  el.title = n ? 'sharing with audio' : 'video only - no audio track';
}

const MAXV = (() => {
  const n = parseInt(new URLSearchParams(location.search).get('max'), 10);
  return Number.isFinite(n) ? Math.min(99, Math.max(1, n)) : 1;
})();
log('room: ' + (ROOM || '(default)') + ' max: ' + MAXV);

function dropGone(gone){
  // Viewers whose leave reached the server drop at once (next poll is ≤1s).
  // Unknown ids are ignored.
  if(!Array.isArray(gone)) return;
  for(const id of gone){
    const pc = pcs[id];
    if(!pc) continue;
    log('viewer ' + id + ' left - dropping at once');
    try{ pc.onconnectionstatechange = null; pc.close(); }catch(e){}
    delete pcs[id];
  }
}

async function pollOffers(){
  roomPing(false);
  const full = Object.keys(pcs).length >= MAXV;
  try{
    const r = await fetch('api/claim', { method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ room: ROOM, known: Object.keys(pcs), accept: !full }) });
    let o = null;
    try{ o = await r.json(); }catch(e){}
    if(o) dropGone(o.gone); // departures apply even when full (queue untouched)
    if(r.ok && o && !o.waiting && o.id && o.sdp){
      if(pcs[o.id]){
        // Same viewer re-offered (reconnect/retry): its old peer is
        // definitionally gone — it closed it before re-offering — so
        // replacing can never kill a live link. Skipping instead would
        // eat the offer and strand the viewer on black forever.
        log('viewer ' + o.id + ' re-offered - replacing stale link');
        try{ pcs[o.id].onconnectionstatechange = null; pcs[o.id].close(); }catch(e){}
        delete pcs[o.id];
      }
      await serveViewer(o.id, o.sdp);
    }
    if(netFails){ netFails = 0; hideToast(); } // back online
  }catch(e){ if(++netFails === 3) showToast('Connection lost — retrying…'); }
  updateViewers();
}

async function serveViewer(id, offerSdp){
  try{
    const offCands = countCands(offerSdp);
    log('offer from ' + id + ' (' + offCands + ' remote, ' + relayCount(offerSdp) + ' relay cands)');
    const pc = new RTCPeerConnection({ iceServers: await getIceServers() }); // LAN only, relay fallback
    pcs[id] = pc;
    screenStream.getTracks().forEach(t => pc.addTrack(t, screenStream));
    pc.oniceconnectionstatechange = () => {
      if(pcs[id] !== pc) return;
      log('viewer ' + id + ' ice: ' + pc.iceConnectionState);
    };
    pc.onconnectionstatechange = () => {
      updateViewers();
      const s = pc.connectionState;
      log('viewer ' + id + ' connection: ' + s + ' (ice=' + pc.iceConnectionState + ')');
      if(s === 'failed') log('viewer ' + id + ' failed - check same network');
      if(s === 'closed' || s === 'disconnected' || s === 'failed'){
        // 'closed' = deliberate leave -> drop at once so the slot frees immediately.
        // 'disconnected'/'failed' may be a passing blip -> 5s grace to recover.
        const wait = (s === 'closed') ? 0 : 5000;
        setTimeout(() => {
          if(pcs[id] !== pc) return;
          const cur = pc.connectionState;
          if(cur !== 'closed' && cur !== 'disconnected' && cur !== 'failed') return; // recovered meanwhile
          try{pc.close();}catch(e){}
          delete pcs[id];
          updateViewers();
        }, wait);
      }
    };
    // server is sendonly, no incoming media expected
    await pc.setRemoteDescription({ type: 'offer', sdp: offerSdp });
    const answer = await pc.createAnswer();
    await pc.setLocalDescription(answer);
    // wait for ICE gather to complete for non-trickle answer
    await new Promise(res => {
      if(pc.iceGatheringState === 'complete') return res();
      const f = () => { if(pc.iceGatheringState === 'complete'){ pc.removeEventListener('icegatheringstatechange', f); res(); } };
      pc.addEventListener('icegatheringstatechange', f);
      setTimeout(res, 3000);
    });
    const ansSdp = pc.localDescription.sdp;
    const ansCands = countCands(ansSdp);
    await fetch('api/answer', { method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ id, sdp: ansSdp, room: ROOM, cands: ansCands }) });
    log('serving viewer ' + id + ' (' + ansCands + ' local, ' + relayCount(ansSdp) + ' relay candidates)');
    if(mdnsOnly(offerSdp) && mdnsOnly(ansSdp))
      log('viewer ' + id + ' mDNS-only both ends - needs UDP 5353 multicast');
    else if(mdnsOnly(ansSdp))
      log('viewer ' + id + ': sharer hides IP (.local) - viewer must resolve via UDP 5353, or flag off HERE');
    else if(mdnsOnly(offerSdp))
      log('viewer ' + id + ' hides its IP (.local) - sharer must resolve via UDP 5353, or flag off on VIEWER');
  }catch(e){
    console.error(e);
    delete pcs[id];
    log('serve failed for ' + id + ': ' + e.message);
  }
}

$('share').onclick = async () => {
  starting = true; paintSharerBtns();
  try{
    screenStream = await navigator.mediaDevices.getDisplayMedia({ video: { frameRate: { ideal: 15, max: 30 } }, audio: true });
  }catch(e){
    try{ screenStream = await navigator.mediaDevices.getDisplayMedia({ video: true }); }
    catch(e2){ starting = false; paintSharerBtns(); alert('Screen capture failed: ' + e2.name); log('capture failed: ' + e2.message); return; }
  }
  starting = false;
  preview.srcObject = screenStream;
  $('share').disabled = true;
  $('stop').disabled = false;
  paintSharerBtns();
  paintAud();
  log('sharing live: ' + screenStream.getVideoTracks().length + ' video + ' + audioTracks() + ' audio track(s)');
  screenStream.getVideoTracks()[0].addEventListener('ended', stopAll);
  pollTimer = setInterval(pollOffers, 1000);
  pollOffers();
};

function roomPing(leave){
  try{
    const body = JSON.stringify({ room: ROOM });
    if(leave && navigator.sendBeacon) navigator.sendBeacon('api/sharer/leave', new Blob([body], {type:'application/json'}));
    else fetch(leave ? 'api/sharer/leave' : 'api/sharer/heartbeat', { method:'POST', headers:{'Content-Type':'application/json'}, body, keepalive:true });
  }catch(e){}
}

function stopAll(){
  roomPing(true);
  if(pollTimer){ clearInterval(pollTimer); pollTimer = null; }
  for(const id of Object.keys(pcs)){ try{pcs[id].close();}catch(e){} }
  pcs = {};
  if(screenStream){ try{screenStream.getTracks().forEach(t=>t.stop());}catch(e){} screenStream = null; }
  preview.srcObject = null;
  $('share').disabled = false;
  $('stop').disabled = true;
  starting = false;
  paintSharerBtns();
  paintAud();
  updateViewers();
  log('stopped');
}
$('stop').onclick = stopAll;

setupFullscreen('stage', 'full');
keepBarAwake('bar');
paintSharerBtns();
paintAud();
window.addEventListener('beforeunload', () => { try{stopAll();}catch(e){} });
