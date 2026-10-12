(function () {
  // 리스트 필터
  const chips = document.querySelectorAll('.aw-chip');
  const items = document.querySelectorAll('.aw-news-item');
  chips.forEach((c) => c.addEventListener('click', () => {
    chips.forEach((x) => x.classList.remove('is-active')); c.classList.add('is-active');
    const f = c.dataset.filter;
    items.forEach((li) => {
      const show = f === 'all' || (f === 'hot' && li.dataset.hot === 'y') || (f === 'analyzed' && li.dataset.analyzed === 'y') || (f === 'pending' && li.dataset.analyzed === 'n') || (f === 'telegram' && li.dataset.source === 'telegram') || (f === 'rss' && li.dataset.source === 'rss');
      li.hidden = !show;
    });
  }));

  const status = document.getElementById('analyze-status');
  function setStatus(msg, err) { if (!status) return; status.hidden = false; status.textContent = msg; status.classList.toggle('is-error', !!err); }
  function poll(id, onDone) {
    const t = setInterval(async () => {
      try {
        const r = await fetch('/api/jobs/' + id); const j = await r.json();
        if (j.status === 'done') { clearInterval(t); onDone(); }
        else if (j.status === 'error') { clearInterval(t); setStatus('분석 실패: ' + (j.error || ''), true); }
      } catch (e) { /* keep polling */ }
    }, 2500);
  }

  // 링크 분석 폼
  const form = document.getElementById('analyze-form');
  if (form) form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const url = document.getElementById('analyze-url').value.trim();
    const segment = document.getElementById('analyze-segment').value.trim() || null;
    const btn = form.querySelector('button'); btn.disabled = true;
    setStatus('기사 본문을 읽고 앤트위키 KB와 대조하는 중…');
    try {
      const r = await fetch('/api/analyze', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url, segment }) });
      const j = await r.json();
      if (!r.ok) { setStatus(j.detail || '요청 실패', true); btn.disabled = false; return; }
      if (j.status === 'done') { location.href = '/news/' + j.id; return; }
      setStatus('LLM이 수혜주를 분석하고 있습니다 (30초~1분). 완료되면 자동으로 이동합니다.');
      poll(j.id, () => { location.href = '/news/' + j.id; });
    } catch (err) { setStatus('네트워크 오류: ' + err, true); btn.disabled = false; }
  });

  // 상세: 지금 분석 / 다시 분석
  const re = document.getElementById('reanalyze');
  if (re) re.addEventListener('click', async () => {
    re.disabled = true; setStatus('LLM이 분석하고 있습니다. 완료되면 자동으로 새로고침됩니다.');
    const r = await fetch('/api/analyze/' + re.dataset.id, { method: 'POST' });
    if (!r.ok) { const j = await r.json().catch(() => ({})); setStatus(j.detail || '요청 실패', true); re.disabled = false; return; }
    poll(re.dataset.id, () => location.reload());
  });
  // 1M / 1D 스파크라인 자동 전환 (4초, 호버 시 일시정지, 클릭 시 즉시 전환)
  document.querySelectorAll('.aw-spark-toggle').forEach((box) => {
    const panes = Array.from(box.querySelectorAll('.aw-spark-pane'));
    const dots = Array.from(box.querySelectorAll('.aw-spark-dots i'));
    if (panes.length < 2) return;
    let idx = panes.findIndex((p) => p.classList.contains('is-active')); if (idx < 0) idx = 0;
    const show = (i) => { idx = (i + panes.length) % panes.length; panes.forEach((p, k) => p.classList.toggle('is-active', k === idx)); dots.forEach((d, k) => d.classList.toggle('is-active', k === idx)); };
    let timer = setInterval(() => show(idx + 1), 4000);
    box.addEventListener('mouseenter', () => clearInterval(timer));
    box.addEventListener('mouseleave', () => { clearInterval(timer); timer = setInterval(() => show(idx + 1), 4000); });
    box.addEventListener('click', () => show(idx + 1));
  });
  const cp = document.getElementById('copy-post');
  if (cp) cp.addEventListener('click', async () => { try { await navigator.clipboard.writeText(cp.dataset.text); cp.textContent = '복사됨 ✓'; setTimeout(() => cp.textContent = '본문 복사', 1500); } catch (e) { cp.textContent = '복사 실패'; } });
  if (status && status.dataset.poll) poll(status.dataset.poll, () => location.reload());
})();
