/* The recorded demo: the two screens with no server and no model.
   Loaded only in the static build (app/build_demo.py adds the script tag). Every call the pages make to /api/...
   is answered here: questions from data.json, which app/record_demo.py recorded from a running copy of the app;
   cases, grades, saved rules, outcomes and flags from this browser's own storage. Nothing is sent anywhere. */
(() => {
  const real = window.fetch.bind(window);
  const DATA = real('data.json').then(r => r.json());
  const KEY = 'pinpoint-recorded-demo-1';
  const norm = s => String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
  const now = () => { const d = new Date(); return new Date(d - d.getTimezoneOffset() * 60000).toISOString().slice(0, 19); };   // local time, as the server writes it
  const OFF = 'Switched off in the recorded demo.';

  let state = null;
  const keep = () => { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {} };
  function open(data){
    if (state) return state;
    try { state = JSON.parse(localStorage.getItem(KEY)); } catch (e) {}
    if (!state){                      // first visit: one case already worked, so the screens are not empty
      state = {cases: [], flags: [], nextCase: 1, nextAsk: 1, nextFlag: 1};
      const c = {id: state.nextCase++, label: 'Disenrolled from MLTC, home care', facts: {...data.facts}, updated: now(), outcome: null, outcome_note: null, asks: [], saved: []};
      for (const q of data.seed || []){
        const i = data.asks.findIndex(a => a.question === q);
        if (i >= 0) c.asks.push({ask_id: state.nextAsk++, i, created: now(), facts: {...data.facts}, grade: c.asks.length ? null : 'good', grade_note: null});
      }
      state.cases.push(c); keep();
    }
    return state;
  }

  const find = (st, id) => st.cases.find(c => c.id === +id);
  const findAsk = (st, aid) => { for (const c of st.cases){ const a = c.asks.find(x => x.ask_id === +aid); if (a) return [c, a]; } return []; };
  const reply = (data, a) => ({...data.asks[a.i].reply, ask_id: a.ask_id, grade: a.grade, grade_note: a.grade_note, searched: data.sources});
  function passage(data, id){        // a line the recorded replies name, by its id
    let hit = null;
    const walk = x => {
      if (hit || !x || typeof x !== 'object') return;
      if (!Array.isArray(x) && x.id === +id && x.cite && x.text) { hit = x; return; }
      Object.values(x).forEach(walk);
    };
    walk(data.asks);
    return hit && {id: hit.id, cite: hit.cite, heading: hit.heading || '', text: hit.text, url: hit.url};
  }
  const later = (ms, value) => new Promise(done => setTimeout(() => done(value), ms));

  async function act(path, query, body){
    const data = await DATA, st = open(data);
    switch (path){
      case '/api/sources': return data.sources;
      case '/api/knowledge': return {...data.knowledge, flags: [...st.flags].reverse()};
      case '/api/section': return data.sections[query.get('id')] || {error: 'No such passage.'};
      case '/api/find': {
        const q = norm(query.get('q')), hit = Object.keys(data.find).find(k => norm(k) === q);
        return hit ? data.find[hit] : {words: [], count: 0, rows: [], note: 'Not a recorded search'};
      }
      case '/api/cases':
        return [...st.cases].sort((a, b) => a.updated < b.updated ? 1 : -1).map(c => ({id: c.id, label: c.label || `Case ${c.id}`, updated: c.updated, asks: c.asks.length, outcome: c.outcome}));
      case '/api/record': {
        const rows = [];
        for (const c of st.cases) for (const a of c.asks){
          const rec = data.asks[a.i], r = rec.reply;
          rows.push({id: a.ask_id, case: c.label || `Case ${c.id}`, created: a.created, question: r.sent, facts: a.facts, kind: r.kind,
            cites: (r.cards || []).flatMap(x => x.lines.map(l => l.cite)), short: (r.summary || []).map(s => s.text).join(' ') || null,
            short_by: r.by || null, seconds: rec.seconds, grade: a.grade, note: a.grade_note, outcome: c.outcome, outcome_note: c.outcome_note});
        }
        rows.sort((a, b) => b.id - a.id);
        const count = {all: rows.length, none: rows.filter(r => !r.grade).length};
        for (const g of ['good', 'bad', 'missing']) count[g] = rows.filter(r => r.grade === g).length;
        return {asks: rows, count};
      }
      case '/api/case': {
        if (!body){
          const c = find(st, query.get('id'));
          return c ? {id: c.id, label: c.label, facts: c.facts, asks: c.asks.map(a => reply(data, a)), saved: c.saved, outcome: c.outcome, outcome_note: c.outcome_note} : {error: 'No such case.'};
        }
        let c = find(st, body.id);
        if (!c){ c = {id: st.nextCase++, asks: [], saved: [], outcome: null, outcome_note: null}; st.cases.push(c); }
        Object.assign(c, {label: String(body.label || '').trim().slice(0, 80), facts: body.facts || {}, updated: now()});
        keep(); return {id: c.id};
      }
      case '/api/case/outcome': {
        const c = find(st, body.id);
        if (c){ c.outcome = body.outcome || null; c.outcome_note = String(body.note || '').trim().slice(0, 500) || null; c.updated = now(); keep(); }
        return {ok: true};
      }
      case '/api/case/delete': st.cases = st.cases.filter(c => c.id !== +body.id); keep(); return {ok: true};
      case '/api/ask': {
        const q = norm(body.question), i = data.asks.findIndex(a => norm(a.question) === q || norm(a.reply.sent) === q);
        if (i < 0) return later(400, {kind: 'recorded', sent: body.question, can: data.asks.map(a => a.reply.kind === 'answer' && a.question.length < 90 ? a.question : null).filter(Boolean)});
        const c = find(st, body.case_id), a = {ask_id: st.nextAsk++, i, created: now(), facts: body.facts || {}, grade: null, grade_note: null};
        if (c){ c.asks.push(a); c.facts = body.facts || c.facts; c.updated = now(); keep(); }
        const r = reply(data, a);
        return later(900, {...r, need: Object.keys(data.facts).filter(k => !(body.facts || {})[k]), writing: !!r.summary});
      }
      case '/api/answer': return later(1400, {});          // the summary came with the recorded reply
      case '/api/spanish': {
        const hit = data.asks.find(a => a.spanish && a.reply.summary && a.reply.summary[0].text === (body.sentences || [])[0]);
        return later(600, hit ? hit.spanish : {error: OFF});
      }
      case '/api/grade': {
        const [, a] = findAsk(st, body.ask_id);
        if (a){ a.grade = body.grade || null; a.grade_note = body.grade && body.note ? String(body.note).trim().slice(0, 500) || null : null; keep(); }
        return {ok: true};
      }
      case '/api/save': {
        const c = find(st, body.case_id), p = passage(data, body.passage_id);
        if (c){ c.saved = c.saved.filter(x => x.id !== +body.passage_id); if (body.on && p) c.saved.push(p); c.updated = now(); keep(); }
        return {ok: true};
      }
      case '/api/flag':
        st.flags.push({id: st.nextFlag++, kind: body.kind || 'gap', question: String(body.question || '').slice(0, 2000), plan: body.plan || null,
          cite: body.cite || null, url: body.url || null, created: now().slice(0, 10), status: 'open', note: String(body.note || '').slice(0, 500) || null});
        keep(); return {ok: true};
      case '/api/flag/status': { const f = st.flags.find(x => x.id === +body.id); if (f){ f.status = body.status; keep(); } return {ok: true}; }
      default: return {error: OFF};                         // adding, checking, approving and retiring a source need the server
    }
  }

  window.fetch = (input, init) => {
    const u = new URL(typeof input === 'string' ? input : input.url, location.href), at = u.pathname.indexOf('/api/');
    if (at < 0) return real(input, init);
    let body = null; try { body = init && init.body ? JSON.parse(init.body) : null; } catch (e) {}
    return act(u.pathname.slice(at), u.searchParams, body).then(x => new Response(JSON.stringify(x), {headers: {'Content-Type': 'application/json'}}));
  };

  /* what the page itself needs to say and hide */
  document.addEventListener('DOMContentLoaded', async () => {
    const style = document.createElement('style');
    style.textContent = '.recorded{font-size:.8rem;opacity:.85;margin-left:auto;padding:0 14px;white-space:nowrap}' +
      '#load-all,#approve-all,#check-all,[data-act="check"]{display:none}' +
      '.recorded-finds{display:flex;gap:6px 12px;flex-wrap:wrap;margin:8px 0 0;font-size:.85rem}';
    document.head.append(style);
    const label = document.createElement('span'); label.className = 'recorded'; label.textContent = 'Recorded demo. No model runs here.';
    const bar = document.querySelector('header.bar'), nav = bar && bar.querySelector('nav');
    if (nav) bar.insertBefore(label, nav);
    const hint = document.querySelector('.compose .hint');
    if (hint) hint.textContent = 'The answers here were recorded. Pick one of the suggested questions, or type one of them.';
    const cat = document.getElementById('cat');             // the organisation's own list of links is not part of the demo
    if (cat) cat.closest('section').hidden = true;
    const box = document.getElementById('find');
    if (box){
      const data = await DATA, row = document.createElement('p'); row.className = 'recorded-finds';
      row.append('Recorded searches:');
      for (const words of Object.keys(data.find)){
        const b = document.createElement('button'); b.type = 'button'; b.className = 'link'; b.textContent = words;
        b.onclick = () => { box.value = words; box.dispatchEvent(new Event('input')); };
        row.append(b);
      }
      box.after(row);
    }
  });
})();
