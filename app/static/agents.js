// The live agent layer on every page: polls /agents/live, draws the rail, and takes a call dropped anywhere.
(function () {
  const $ = id => document.getElementById(id);
  const MAX_AUDIO = 10000000;
  const AUDIO_NAME = /\.(wav|mp3|mp4|m4a|ogg|opus|flac|aac|webm|amr)$/i;
  const STEP_FOR_AGENT = { Collector: 'collect', Onboarding: 'collect', Triage: 'investigate', Investigator: 'investigate', 'Risk check': 'investigate',
    'Rule checker': 'investigate', Drafter: 'draft', Verifier: 'verify', Advisor: 'advise', 'Seller Protection check': 'advise' };
  const STEP_FOR_CALL = { queued: 'collect', transcribing: 'collect', labelling: 'collect', selecting: 'collect', reading: 'collect' };
  const NEED_ICON = { approve: '✓', check: '?', case: '↗', failed: '!' };
  let state = null, previous = null, timer = null, pendingFiles = null;

  function el(tag, attrs, ...kids) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => { if (k === 'class') node.className = v; else if (v !== undefined && v !== null) node.setAttribute(k, v); });
    kids.flat().forEach(k => { if (k !== null && k !== undefined) node.append(k); });
    return node;
  }

  function toast(message, href, linkText, isError) {
    const box = el('div', { class: 'toast' + (isError ? ' err' : ''), role: 'status' }, message);
    if (href) box.append(' ', el('a', { href }, linkText));
    $('toasts').append(box);
    setTimeout(() => box.remove(), isError ? 8000 : 6000);
  }

  // ---- drawing ----
  function fill(id, nodes, empty) {
    const box = $(id);
    if (box) box.replaceChildren(...(nodes.length ? nodes : (empty ? [el('div', { class: 'rail-empty' }, empty)] : [])));
  }

  function workItems(s) {
    return s.calls.filter(c => c.status !== 'reading').map(c => el('div', { class: 'work-item' }, el('b', null, c.filename), c.label))
      .concat(s.running.map(r => el('div', { class: 'work-item' }, el('b', null, r.dispute_id), r.agent)));
  }

  function drawRail(s) {
    const working = s.calls.length + s.running.length;
    fill('rail-work', workItems(s), 'No agent is mid-task right now.');
    fill('rail-needs', s.needs.map(n => el('a', { class: 'need-item ' + n.kind, href: n.href }, el('b', null, n.title), el('span', null, n.detail || ''))), 'Nothing needs you.');
    fill('rail-agents', s.agents.map(a => el('div', { class: 'agent-row' }, el('span', { class: 'pulse ' + a.state }), el('div', null, el('b', null, a.name), el('div', null, a.detail)))));
    fill('rail-feed', s.feed.map(f => el('div', { class: 'feed-row' }, el('time', null, f.ts), el('div', null, el('b', null, f.what), f.dispute_id ? ' · ' + f.dispute_id : ''))), 'Nothing yet.');
    if ($('needs-count')) $('needs-count').textContent = s.needs.length || '';
    const label = working ? `${working} ${working === 1 ? 'agent' : 'agents'} working` : s.needs.length ? `${s.needs.length} waiting on you` : 'Agents on standby';
    [$('side-status-text'), $('rail-state')].forEach(n => { if (n) n.textContent = label; });
    [$('side-status-dot'), $('rail-dot')].forEach(n => { if (n) n.className = 'pulse ' + (working ? 'working' : s.demo ? 'idle' : 'watching'); });
  }

  function drawCommand(s) {
    if ($('command-needs')) {
      const rows = s.needs.map(n => el('a', { class: 'need-row ' + n.kind, href: n.href }, el('span', { class: 'ico', 'aria-hidden': 'true' }, NEED_ICON[n.kind]),
        el('div', null, el('b', null, n.title), el('span', { class: 't' }, n.detail || '')), el('span', { class: 'go', 'aria-hidden': 'true' }, '→')));
      $('command-needs').replaceChildren(...(rows.length ? rows : [el('div', { class: 'all-clear' }, 'All clear. The agents will call you when a decision is yours.')]));
    }
    const active = new Set(s.calls.map(c => STEP_FOR_CALL[c.status]).concat(s.running.map(r => STEP_FOR_AGENT[r.agent] || 'advise')));
    document.querySelectorAll('.pipeline [data-step]').forEach(li => li.classList.toggle('on', active.has(li.dataset.step)));
    if ($('live-line')) {
      const call = s.calls.find(c => c.status !== 'reading'), run = s.running[0];
      const text = run ? [el('b', null, run.agent), ' is on ', el('span', { class: 'mono' }, run.dispute_id), `. ${run.what}`] :
        call ? [el('b', null, call.label), ': ', el('span', { class: 'mono' }, call.filename)] :
          [s.needs.length ? 'The agents have finished. Your decisions are below.' : s.demo ? 'Standing by. Open a case and run the team to watch them work.' : 'Watching PayPal for new cases.'];
      $('live-line').replaceChildren(el('span', { class: 'pulse ' + (run || call ? 'working' : s.demo ? 'idle' : 'watching') }), el('span', null, ...text));
    }
    if ($('page-live')) {
      const items = s.calls.map(c => el('div', { class: 'work-item' }, el('b', null, c.filename), c.label));
      $('page-live').replaceChildren(...items);
      $('page-live').hidden = !items.length;
    }
    const strip = $('case-live');
    if (strip) {
      const run = s.running.find(r => r.dispute_id === strip.dataset.case);
      const call = s.calls.find(c => c.dispute_id === strip.dataset.case && !run);
      strip.classList.toggle('working', !!(run || call));
      const line = $('case-live-line');
      if (run) line.replaceChildren(el('span', { class: 'pulse working' }), el('span', null, el('b', null, run.agent), ` ${run.what}`));
      else if (call) line.replaceChildren(el('span', { class: 'pulse working' }), el('span', null, el('b', null, call.label)));
    }
  }

  function sellerIsTyping() {
    return [...document.querySelectorAll('main textarea, main input')].some(f =>
      f.type === 'checkbox' ? f.checked !== f.defaultChecked : f.type !== 'hidden' && f.value !== f.defaultValue);
  }

  // Pages show saved results, so reload once the work behind them finishes. Never over a reply being edited.
  function reactToFinished(s) {
    if (!previous) return;
    const nowCalls = new Set(s.calls.map(c => c.id)), nowRuns = new Set(s.running.map(r => r.dispute_id));
    const doneCalls = previous.calls.filter(c => !nowCalls.has(c.id)), doneRuns = previous.running.filter(r => !nowRuns.has(r.dispute_id));
    doneCalls.forEach(c => {
      const need = s.needs.find(n => n.title.includes(c.filename));
      if (need) toast(`${c.filename}: ${need.title.includes('Check') ? 'read and ready to check.' : need.detail || 'needs you.'}`, need.href, 'Open');
    });
    if (!doneCalls.length && !doneRuns.length) return;
    const strip = $('case-live');
    const here = strip ? strip.dataset.case : null;
    const affectsPage = here ? (doneRuns.some(r => r.dispute_id === here) || doneCalls.some(c => c.dispute_id === here)) :
      document.body.dataset.path === '/' || document.body.dataset.path === '/recordings';
    if (!affectsPage) return;
    if (sellerIsTyping()) toast('The agents have new results.', location.pathname, 'Reload');
    else location.reload();
  }

  async function poll() {
    clearTimeout(timer);
    let wait = 6000;
    if (!document.hidden) {
      try {
        const r = await fetch('/agents/live', { headers: { accept: 'application/json' } });
        if (r.ok) {
          state = await r.json();
          drawRail(state);
          drawCommand(state);
          reactToFinished(state);
          previous = state;
          if (state.calls.length || state.running.length) wait = 1500;
        }
      } catch (e) { /* the next poll tries again */ }
    }
    timer = setTimeout(poll, wait);
  }

  // ---- adding a call ----
  function chooseFile() { $('call-file').click(); }

  async function upload(file, consent) {
    const form = new FormData();
    form.append('audio', file);
    if (consent) form.append('consent', 'yes');
    const r = await fetch('/recordings/upload', { method: 'POST', body: form });
    const out = await r.json().catch(() => ({ error: 'The upload failed. Try again.' }));
    if (!r.ok) toast(out.error, null, null, true);
    else toast(`Got ${file.name}. The agents will take it from here.`);
  }

  async function startFiles(files, consent) {
    for (const file of files) await upload(file, consent);
    poll();
  }

  function takeFiles(list) {
    const files = [...list].filter(f => f.type.startsWith('audio/') || f.type.startsWith('video/') || AUDIO_NAME.test(f.name));
    if (!files.length) return toast('That doesn’t look like an audio file.', null, null, true);
    if (files.some(f => f.size > MAX_AUDIO)) return toast('Each recording must be under 10 MB.', null, null, true);
    if (state && !state.upload) {
      if (state.demo) return sampleOffer();
      return toast('Audio upload needs GNANI_API_KEY in the app’s settings.', null, null, true);
    }
    if (state && !state.consent) {
      pendingFiles = files;
      return $('consent-dialog').showModal();
    }
    startFiles(files, false);
  }

  function sampleOffer() {
    toast('Real audio is off in this demo, so here’s a sample call instead.');
    runSample();
  }

  async function runSample() {
    const r = await fetch('/recordings/sample', { method: 'POST' });
    const out = await r.json().catch(() => ({}));
    if (!r.ok) toast(out.error || 'That didn’t work.', null, null, true);
    poll();
  }

  // ---- wiring ----
  function wire() {
    const overlay = $('drop-overlay');
    let depth = 0;
    const hasFiles = e => e.dataTransfer && [...e.dataTransfer.types].includes('Files');
    window.addEventListener('dragenter', e => { if (hasFiles(e)) { depth++; overlay.classList.add('on'); e.preventDefault(); } });
    window.addEventListener('dragover', e => { if (hasFiles(e)) e.preventDefault(); });
    window.addEventListener('dragleave', e => { if (hasFiles(e) && --depth <= 0) { depth = 0; overlay.classList.remove('on'); } });
    window.addEventListener('drop', e => {
      if (!hasFiles(e)) return;
      e.preventDefault(); depth = 0; overlay.classList.remove('on');
      takeFiles(e.dataTransfer.files);
    });
    $('call-file').addEventListener('change', e => { if (e.target.files.length) takeFiles(e.target.files); e.target.value = ''; });
    document.addEventListener('click', e => {
      if (e.target.closest('[data-add-call]')) chooseFile();
      if (e.target.closest('[data-sample-call]')) runSample();
      if (e.target.closest('[data-rail-toggle]')) {
        const closed = document.body.classList.toggle('rail-closed');
        try { localStorage.setItem('dd-rail', closed ? 'closed' : 'open'); } catch (err) { /* only a convenience */ }
      }
    });
    $('consent-yes').addEventListener('click', () => {
      if (!$('consent-box').checked) return toast('Tick the box to confirm.', null, null, true);
      $('consent-dialog').close();
      const files = pendingFiles; pendingFiles = null; $('consent-box').checked = false;
      startFiles(files, true);
    });
    $('consent-no').addEventListener('click', () => { pendingFiles = null; $('consent-dialog').close(); });
    document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
  }

  // The carrier fields only matter for a carrier record
  document.querySelectorAll('[data-doc-type]').forEach(select => {
    const fields = select.form.querySelector('[data-carrier-fields]');
    const show = () => { fields.hidden = select.value !== 'carrier_record'; };
    select.addEventListener('change', show);
    show();
  });

  wire();
  poll();
})();
