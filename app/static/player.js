(function () {
  var v = document.getElementById('v');
  if (!v) return;
  var src = v.dataset.src, lesson = +v.dataset.lesson, csrf = v.dataset.csrf, start = +v.dataset.start || 0;
  var last = 0, sent = 0;

  function save(done) {
    if (!isFinite(v.currentTime)) return;
    fetch('/api/progress', {
      method: 'POST', credentials: 'same-origin', keepalive: true,
      headers: { 'Content-Type': 'application/json', 'X-CSRF': csrf },
      body: JSON.stringify({ lesson_id: lesson, seconds: v.currentTime, completed: !!done })
    }).catch(function () {});
    sent = Date.now();
  }

  function ready() {
    if (start > 5 && start < (v.duration || 1e9) - 5) v.currentTime = start;
  }

  if (window.Hls && Hls.isSupported()) {
    var hls = new Hls({ enableWorker: true });
    hls.loadSource(src);
    hls.attachMedia(v);
    hls.on(Hls.Events.MANIFEST_PARSED, ready);
    hls.on(Hls.Events.ERROR, function (_, d) {
      if (d.fatal && d.type === Hls.ErrorTypes.NETWORK_ERROR) setTimeout(function () { hls.startLoad(); }, 1500);
      else if (d.fatal && d.type === Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError();
    });
  } else {
    v.src = src;  // Safari reproduz HLS nativamente
    v.addEventListener('loadedmetadata', ready, { once: true });
  }

  v.addEventListener('timeupdate', function () { if (Date.now() - sent > 10000 && !v.paused) save(false); });
  v.addEventListener('pause', function () { save(false); });
  v.addEventListener('ended', function () {
    save(true);
    var n = document.getElementById('next');
    if (n) { n.textContent = 'Próxima aula em 5s… (clique para ir agora)'; setTimeout(function () { if (document.getElementById('v')) location.href = n.href; }, 5000); }
  });
  window.addEventListener('pagehide', function () { save(false); });
})();
