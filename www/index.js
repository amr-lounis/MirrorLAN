/* MirrorLAN Rooms page logic (extracted from index.html; loaded after shared.js). */
const WATCH_ICON = '<svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>';
async function refresh(){
  try{
    const r = await fetch('api/rooms');
    if(!r.ok) throw new Error(r.status);
    $('err').style.display = 'none';
    const data = await r.json();
    const box = $('list');
    box.querySelectorAll('.row').forEach(e => e.remove());
    const rooms = (data.rooms || []).filter(x => x.live);
    $('empty').style.display = rooms.length ? 'none' : 'block';
    $('livecount').textContent = rooms.length;
    for(const x of rooms){
      const row = document.createElement('div');
      row.className = 'row';
      const dot = document.createElement('span');
      dot.className = 'dot';
      const name = document.createElement('span');
      name.className = 'name';
      name.textContent = x.room || '(default)';
      const btn = document.createElement('button');
      btn.innerHTML = WATCH_ICON + 'Watch';
      const room = x.room;
      btn.onclick = () => { location.href = 'Viewer.html' + (room ? '?room=' + encodeURIComponent(room) : ''); };
      row.append(dot, name, btn);
      box.append(row);
    }
  }catch(e){ $('err').style.display = 'block'; }
}

refresh();
setInterval(refresh, 3000);
