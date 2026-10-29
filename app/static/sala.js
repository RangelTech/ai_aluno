(function () {
  // Sala ao vivo: WebRTC em malha (ate 5 pessoas), sinalizacao pelo WebSocket do proprio servidor.
  var app = document.getElementById('app');
  if (!app) return;
  var mid = app.dataset.mid, ICE = JSON.parse(app.dataset.ice || '[]');
  var tiles = document.getElementById('tiles'), statusEl = document.getElementById('status');
  var log = document.getElementById('log'), me = null, myName = '';
  var peers = {}, local = null, camStream = null, screenStream = null, ws = null, closed = false;
  var mic = true, cam = true, sharing = false;

  function setStatus(t) { statusEl.textContent = t; }
  function layout() {
    var n = tiles.children.length;
    tiles.className = 'tiles n' + Math.min(5, Math.max(1, n));
  }
  function initials(n) { return (n || '?').trim().charAt(0).toUpperCase(); }
  function tile(id, name, stream, isMe) {
    var t = document.getElementById('t-' + id);
    if (!t) {
      t = document.createElement('div');
      t.className = 'tile'; t.id = 't-' + id;
      t.innerHTML = '<video autoplay playsinline></video><div class="off"><span class="avatar"></span></div><div class="nm"></div>';
      tiles.appendChild(t);
    }
    t.querySelector('.avatar').textContent = initials(name);
    t.querySelector('.nm').textContent = name + (isMe ? ' (você)' : '');
    var v = t.querySelector('video');
    if (isMe) v.muted = true;
    if (stream && v.srcObject !== stream) v.srcObject = stream;
    layout();
    return t;
  }
  function drop(id) {
    var t = document.getElementById('t-' + id); if (t) t.remove();
    if (peers[id]) { try { peers[id].pc.close(); } catch (e) {} delete peers[id]; }
    layout();
  }
  function chat(name, text, mine) {
    var d = document.createElement('div');
    var b = document.createElement('b'); b.textContent = mine ? 'Você' : name;
    d.appendChild(b); d.appendChild(document.createTextNode(': ' + text));
    log.appendChild(d); log.scrollTop = log.scrollHeight;
  }
  function send(o) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }
  function broadcastState() { send({ type: 'state', mic: mic, cam: cam, screen: sharing }); }

  function makePeer(id, name, initiator) {
    if (peers[id]) { try { peers[id].pc.close(); } catch (e) {} }
    var pc = new RTCPeerConnection({ iceServers: ICE });
    var p = peers[id] = { pc: pc, name: name, pending: [], remote: false };
    var remoteStream = new MediaStream();
    tile(id, name, remoteStream, false);
    (local ? local.getTracks() : []).forEach(function (tr) { pc.addTrack(tr, local); });
    pc.ontrack = function (e) {
      e.streams[0] && e.streams[0].getTracks().forEach(function (tr) { if (!remoteStream.getTracks().includes(tr)) remoteStream.addTrack(tr); });
      tile(id, name, remoteStream, false);
    };
    pc.onicecandidate = function (e) { if (e.candidate) send({ type: 'signal', to: id, data: { candidate: e.candidate } }); };
    pc.onconnectionstatechange = function () {
      if (pc.connectionState === 'connected') setStatus('Conectado · ' + (Object.keys(peers).length + 1) + ' na sala');
      if (pc.connectionState === 'failed') { setStatus('Conexão com ' + name + ' falhou. Tentando de novo…'); pc.restartIce && pc.restartIce(); }
    };
    if (initiator) {
      pc.onnegotiationneeded = function () {
        pc.createOffer().then(function (o) { return pc.setLocalDescription(o); })
          .then(function () { send({ type: 'signal', to: id, data: { sdp: pc.localDescription } }); }).catch(console.error);
      };
    }
    return p;
  }

  function onSignal(from, data) {
    var p = peers[from] || makePeer(from, (peers[from] && peers[from].name) || 'Participante', false);
    var pc = p.pc;
    if (data.sdp) {
      pc.setRemoteDescription(data.sdp).then(function () {
        p.remote = true;
        p.pending.splice(0).forEach(function (c) { pc.addIceCandidate(c).catch(function () {}); });
        if (data.sdp.type === 'offer') {
          return pc.createAnswer().then(function (a) { return pc.setLocalDescription(a); })
            .then(function () { send({ type: 'signal', to: from, data: { sdp: pc.localDescription } }); });
        }
      }).catch(console.error);
    } else if (data.candidate) {
      if (p.remote) pc.addIceCandidate(data.candidate).catch(function () {}); else p.pending.push(data.candidate);
    }
  }

  function connect() {
    var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    ws = new WebSocket(proto + location.host + '/ws/sala/' + mid);
    ws.onmessage = function (e) {
      var m = JSON.parse(e.data);
      if (m.type === 'denied') {
        closed = true;
        var why = { session_not_open: 'A sala não está aberta neste horário.', room_full: 'A sala está cheia (máximo de 5 pessoas).', consent_required: 'É preciso aceitar o aviso de gravação antes de entrar.', not_participant: 'Você não está na lista desta mentoria.' }[m.reason] || ('Entrada negada (' + m.reason + ').');
        setStatus(why); return;
      }
      if (m.type === 'replaced') { closed = true; setStatus('Você entrou em outra aba ou aparelho. Esta sessão foi encerrada.'); local && local.getTracks().forEach(function (t) { t.stop(); }); return; }
      if (m.type === 'welcome') {
        me = m.you; myName = m.name; tile(me, myName, local, true);
        m.peers.forEach(function (p) { makePeer(p.id, p.name, true); });
        setStatus(m.peers.length ? 'Conectando…' : 'Você é o primeiro na sala. Aguardando os outros participantes.');
        broadcastState();
      } else if (m.type === 'peer-joined') {
        makePeer(m.id, m.name, false); setStatus(m.name + ' entrou');
        broadcastState();
      } else if (m.type === 'peer-left') { var nm = peers[m.id] && peers[m.id].name; drop(m.id); setStatus((nm || 'Participante') + ' saiu'); }
      else if (m.type === 'signal') onSignal(m.from, m.data);
      else if (m.type === 'chat') chat(m.name, m.text, m.from === me);
      else if (m.type === 'state') {
        var t = document.getElementById('t-' + m.id);
        if (t) { t.classList.toggle('camoff', !m.cam && !m.screen); t.classList.toggle('screen', !!m.screen); }
      }
    };
    ws.onclose = function () {
      if (closed) return;
      setStatus('Conexão com a sala perdida. Reconectando…');
      Object.keys(peers).forEach(drop);
      setTimeout(connect, 2000);
    };
  }

  function mediaFallback(err) {
    // sem camera/microfone liberados: entra assim mesmo (so assiste e usa o chat)
    setStatus('Não foi possível acessar câmera/microfone (' + (err && err.name || 'erro') + '). Você entra só para ver e ouvir.');
    local = new MediaStream();
    mic = false; cam = false;
  }

  navigator.mediaDevices && navigator.mediaDevices.getUserMedia
    ? navigator.mediaDevices.getUserMedia({ video: { width: 1280, height: 720 }, audio: { echoCancellation: true, noiseSuppression: true } })
        .then(function (s) { local = camStream = s; }, mediaFallback).then(connect)
    : (mediaFallback({ name: 'sem suporte' }), connect());

  function toggleBtn(id, on) { document.getElementById(id).classList.toggle('off', !on); }
  document.getElementById('bMic').onclick = function () {
    if (!camStream) return; mic = !mic; camStream.getAudioTracks().forEach(function (t) { t.enabled = mic; }); toggleBtn('bMic', mic); broadcastState();
  };
  document.getElementById('bCam').onclick = function () {
    if (!camStream) return; cam = !cam; camStream.getVideoTracks().forEach(function (t) { t.enabled = cam; }); toggleBtn('bCam', cam);
    var t = document.getElementById('t-' + me); if (t) t.classList.toggle('camoff', !cam); broadcastState();
  };
  document.getElementById('bScr').onclick = function () {
    function swap(track) {
      Object.keys(peers).forEach(function (id) {
        var s = peers[id].pc.getSenders().find(function (x) { return x.track && x.track.kind === 'video'; });
        if (s && track) s.replaceTrack(track);
      });
    }
    if (!sharing) {
      navigator.mediaDevices.getDisplayMedia({ video: true }).then(function (s) {
        screenStream = s; sharing = true; var tr = s.getVideoTracks()[0]; swap(tr);
        tile(me, myName, s, true); toggleBtn('bScr', false); broadcastState();
        tr.onended = function () { stopShare(); };
      }).catch(function () {});
    } else stopShare();
    function stopShare() {
      if (!sharing) return; sharing = false; screenStream && screenStream.getTracks().forEach(function (t) { t.stop(); });
      var cv = camStream && camStream.getVideoTracks()[0]; if (cv) swap(cv);
      tile(me, myName, camStream || local, true); toggleBtn('bScr', true); broadcastState();
    }
  };
  document.getElementById('chatForm').onsubmit = function (e) {
    e.preventDefault(); var i = document.getElementById('chatIn'); var t = i.value.trim();
    if (t) send({ type: 'chat', text: t }); i.value = '';
  };
  window.addEventListener('beforeunload', function () { closed = true; try { ws.close(); } catch (e) {} });
})();
