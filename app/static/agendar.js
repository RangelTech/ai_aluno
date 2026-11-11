// Agenda publica: escolhe o dia (fichas) e o horario (grade). Sem bibliotecas. O backend decide tudo (409 se o horario foi tomado).
(function () {
  var box = document.getElementById('dias'), conf = document.getElementById('conf'), btn = document.getElementById('btn');
  var token = box.dataset.token, escolha = null, dados = null, diaSel = 0;
  var DIAS = { seg: 'segunda', ter: 'terça', qua: 'quarta', qui: 'quinta', sex: 'sexta', 'sáb': 'sábado', dom: 'domingo' };
  var extra = location.search.indexOf('agora=') > -1 ? location.search.slice(1) : '';
  function render() {
    box.innerHTML = '';
    var fichas = document.createElement('div'); fichas.className = 'fichas';
    dados.dias.forEach(function (dia, i) {
      var f = document.createElement('button'); f.type = 'button'; f.className = 'ficha' + (i === diaSel ? ' on' : '') + (dia.horarios.length ? '' : ' off');
      f.innerHTML = '<span>' + dia.dia + '</span><b>' + dia.rotulo + '</b><i>' + (dia.horarios.length ? dia.horarios.length + ' livres' : 'cheio') + '</i>';
      f.disabled = !dia.horarios.length;
      f.onclick = function () { diaSel = i; escolha = null; conf.classList.remove('on'); render(); };
      fichas.appendChild(f);
    });
    box.appendChild(fichas);
    var dia = dados.dias[diaSel];
    var tit = document.createElement('p'); tit.className = 'dia-tit'; tit.textContent = DIAS[dia.dia] + ', ' + dia.rotulo + ' · horário de Brasília';
    box.appendChild(tit);
    var grade = document.createElement('div'); grade.className = 'grade';
    dia.horarios.forEach(function (h) {
      var b = document.createElement('button'); b.type = 'button'; b.className = 'hora' + (escolha === dia.data + 'T' + h ? ' sel' : ''); b.textContent = h;
      b.onclick = function () {
        escolha = dia.data + 'T' + h;
        Array.prototype.forEach.call(grade.querySelectorAll('.hora.sel'), function (x) { x.classList.remove('sel'); });
        b.classList.add('sel');
        document.getElementById('conf-quando').textContent = DIAS[dia.dia] + ', ' + dia.rotulo + ' às ' + h;
        conf.classList.add('on'); document.getElementById('erro').textContent = '';
        conf.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      };
      grade.appendChild(b);
    });
    box.appendChild(grade);
  }
  function carregar() {
    fetch('/api/agenda/horarios?t=' + encodeURIComponent(token) + (extra ? '&' + extra : ''), { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.dias) { box.innerHTML = '<div class="vazio">Não deu para carregar. Recarregue a página.</div>'; return; }
        dados = d;
        diaSel = 0;
        for (var i = 0; i < d.dias.length; i++) { if (d.dias[i].horarios.length) { diaSel = i; break; } }
        render();
      })
      .catch(function () { box.innerHTML = '<div class="vazio">Não deu para carregar. Recarregue a página.</div>'; });
  }
  btn.onclick = function () {
    if (!escolha) return;
    btn.disabled = true;
    fetch('/api/agenda/reservar' + (extra ? '?' + extra : ''), { method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin', body: JSON.stringify({ t: token, inicio: escolha }) })
      .then(function (r) { return r.json().then(function (d) { return { s: r.status, d: d }; }); })
      .then(function (x) {
        btn.disabled = false;
        if (x.s === 201) {
          conf.classList.remove('on'); box.style.display = 'none';
          var f = document.getElementById('feito');
          f.style.display = 'block';
          f.innerHTML = '<h2>Marcado: ' + x.d.quando + '.</h2><p class="sub">Você vai receber um e-mail com a confirmação e, antes da hora, o link da chamada. Se precisar mudar, use o link que está no e-mail.</p>';
          f.scrollIntoView({ behavior: 'smooth', block: 'start' });
        } else if (x.s === 409) {
          document.getElementById('erro').textContent = 'Esse horário acabou de ser tomado. Escolha outro.';
          escolha = null; conf.classList.remove('on'); carregar();
        } else {
          document.getElementById('erro').textContent = 'Não deu para marcar agora. Tente de novo em instantes.';
        }
      })
      .catch(function () { btn.disabled = false; document.getElementById('erro').textContent = 'Sem conexão. Tente de novo.'; });
  };
  carregar();
})();
