"""Antarmuka web Hermes: daftar alur (recipe) otonom.

Halaman utama adalah daftar recipe beserta jadwal dan statusnya.
Input hanya muncul di dalam kartu recipe yang mendeklarasikan `inputs`;
recipe tanpa input cukup dijalankan dengan satu tombol.

Pakai http.server bawaan Python — tanpa dependency tambahan.
Jalankan: python -m hermes web   → buka http://localhost:10000
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from . import cron, db, recipes, tasks

if TYPE_CHECKING:
    from .config import Config

log = logging.getLogger("hermes.webui")

# Eksekusi recipe berjalan di thread latar agar UI tidak menggantung.
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Halaman (single-page, 2 tab: Dashboard · Log)
# ---------------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hermes Agent — Alur Otomatis</title>
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --panel2:#1c2129; --line:#2a313b;
    --fg:#e6edf3; --dim:#8b949e; --dimmer:#6e7681;
    --accent:#4f9cf9; --ok:#3fb950; --warn:#d29922; --err:#f85149;
    --radius:10px;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
    font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    -webkit-font-smoothing:antialiased}
  a{color:var(--accent);text-decoration:none}
  code{background:var(--panel2);padding:1px 5px;border-radius:5px;font-size:12.5px}

  /* ---------- shell ---------- */
  .app{display:flex;flex-direction:column;height:100vh}
  .top{display:flex;align-items:center;gap:12px;padding:12px 20px;
    border-bottom:1px solid var(--line);background:var(--panel);flex:none}
  .brand{display:flex;align-items:center;gap:9px;font-weight:600;font-size:15px}
  .dot{width:9px;height:9px;border-radius:50%;background:var(--ok);box-shadow:0 0 0 3px #3fb95022}
  .dot.off{background:var(--dimmer);box-shadow:none}
  nav{display:flex;gap:4px;margin-left:22px}
  nav button{background:none;border:1px solid transparent;color:var(--dim);
    padding:7px 14px;border-radius:8px;cursor:pointer;font:inherit;font-weight:500}
  nav button:hover{color:var(--fg);background:var(--panel2)}
  nav button.active{color:var(--fg);background:var(--panel2);border-color:var(--line)}
  .top .meta{margin-left:auto;display:flex;gap:8px;align-items:center}
  .chip{font-size:12px;color:var(--dim);border:1px solid var(--line);
    padding:3px 10px;border-radius:99px;background:var(--bg)}
  .chip b{color:var(--fg);font-weight:600}
  main{flex:1;overflow:hidden}
  .view{display:none;height:100%;overflow-y:auto}
  .view.active{display:block}
  .pad{padding:20px;max-width:1180px;margin:0 auto}

  /* ---------- statistik ---------- */
  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:14px 16px}
  .card .lbl{color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
  .card .val{font-size:26px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums}
  .card .sub{color:var(--dimmer);font-size:12px;margin-top:2px}
  .card.ok .val{color:var(--ok)} .card.err .val{color:var(--err)} .card.warn .val{color:var(--warn)}

  .grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
    padding:16px;margin-bottom:16px}
  .panel h3{margin:0 0 12px;font-size:13px;font-weight:600;color:var(--dim);
    text-transform:uppercase;letter-spacing:.04em}

  /* ---------- kartu recipe ---------- */
  .rec{border:1px solid var(--line);border-radius:9px;padding:14px 16px;margin-bottom:12px;
    background:var(--panel2)}
  .rec:last-child{margin-bottom:0}
  .rec.running{border-color:#4f9cf966}
  .rec .head{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  .rec .name{font-weight:600;font-size:15px}
  .rec .desc{color:var(--dim);font-size:12.5px;margin-top:5px}
  .rec .steps{color:var(--dimmer);font-size:11.5px;margin-top:9px;line-height:1.7}
  .rec .steps span{display:inline-block}
  .rec .sched{font-size:11.5px;color:var(--dim);border:1px solid var(--line);
    padding:2px 9px;border-radius:99px;background:var(--bg);font-variant-numeric:tabular-nums}
  .rec .body{margin-top:12px}
  .rec .foot{display:flex;gap:14px;flex-wrap:wrap;margin-top:12px;font-size:11.5px;
    color:var(--dim);font-variant-numeric:tabular-nums;align-items:center}
  .rec .foot b{color:var(--fg);font-weight:600}

  /* form input inline (hanya bila recipe butuh) */
  .inputs{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end;padding:12px;
    background:var(--bg);border:1px solid var(--line);border-radius:8px}
  .field{display:flex;flex-direction:column;gap:5px;min-width:150px}
  .field label{font-size:11.5px;color:var(--dim);font-weight:500}
  .field input{padding:8px 10px;border-radius:7px;border:1px solid var(--line);
    background:var(--panel);color:var(--fg);font:inherit;font-size:13px}
  .field input:focus{outline:none;border-color:var(--accent)}
  .field .hint{font-size:10.5px;color:var(--dimmer);max-width:210px}

  button.run{background:none;border:1px solid var(--line);color:var(--accent);
    padding:5px 14px;border-radius:6px;cursor:pointer;font:inherit;font-size:12.5px}
  button.run:hover{border-color:var(--accent);background:#4f9cf911}
  button.run:disabled{opacity:.5;cursor:not-allowed}
  .rec .head .run{margin-left:auto}

  /* hasil yang tampil di kartu (pengganti chat) */
  .result{margin-top:10px;padding:10px 12px;border-radius:8px;border:1px solid var(--line);
    background:var(--bg);font-size:12.5px;white-space:pre-wrap;word-break:break-word;
    font-family:ui-monospace,SFMono-Regular,monospace;display:none}
  .result.show{display:block}
  .result.ok{border-color:#3fb95055}
  .result.err{border-color:#f8514955}
  .result.warn{border-color:#d2992255}

  /* bar chart */
  .bars{display:flex;align-items:flex-end;gap:8px;height:120px;padding-top:6px}
  .bar{flex:1;display:flex;flex-direction:column;align-items:center;gap:6px;height:100%;
    justify-content:flex-end}
  .bar .fill{width:100%;background:var(--accent);border-radius:4px 4px 0 0;min-height:2px;
    transition:height .3s ease}
  .bar .n{font-size:11px;color:var(--dim);font-variant-numeric:tabular-nums}
  .bar .d{font-size:10.5px;color:var(--dimmer);white-space:nowrap}

  /* tables */
  table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
  th{text-align:left;color:var(--dim);font-size:12px;font-weight:500;
    padding:8px 10px;border-bottom:1px solid var(--line)}
  td{padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
  tr:last-child td{border-bottom:none}

  .pill{display:inline-block;font-size:11px;font-weight:600;padding:2px 9px;border-radius:99px}
  .pill.ok{background:#3fb95022;color:var(--ok)}
  .pill.err{background:#f8514922;color:var(--err)}
  .pill.partial{background:#d2992222;color:var(--warn)}
  .pill.pending{background:#4f9cf922;color:var(--accent)}

  /* ---------- notifikasi Telegram ---------- */
  .ntf{border:1px solid var(--line);border-left:3px solid var(--accent);
    border-radius:8px;padding:11px 14px;margin-bottom:10px;background:var(--panel2)}
  .ntf:last-child{margin-bottom:0}
  .ntf.err{border-left-color:var(--err)}
  .ntfhead{display:flex;align-items:center;gap:10px;margin-bottom:7px}
  .ntfhead .name{font-weight:600;font-size:13px}
  .ntfhead .ts{margin-left:auto;color:var(--dimmer);font-size:11px;
    font-variant-numeric:tabular-nums}
  .ntftext{white-space:pre-wrap;word-break:break-word;font-size:12.5px;
    font-family:ui-monospace,SFMono-Regular,monospace;color:var(--fg);line-height:1.6}
  .empty{color:var(--dimmer);text-align:center;padding:32px 0;font-size:13px}
  .act{background:none;border:1px solid var(--line);color:var(--dim);padding:3px 9px;
    border-radius:6px;cursor:pointer;font:inherit;font-size:12px}
  .act:hover{color:var(--fg);border-color:var(--dim)}

  /* ---------- log ---------- */
  .logbar{display:flex;gap:6px;margin-bottom:12px;flex-wrap:wrap}
  .lvl{background:none;border:1px solid var(--line);color:var(--dim);padding:4px 11px;
    border-radius:99px;cursor:pointer;font:inherit;font-size:12px}
  .lvl.active{background:var(--panel2);color:var(--fg);border-color:var(--dim)}
  .lvl .c{opacity:.7;margin-left:4px}
  #loglist{font-family:ui-monospace,monospace;font-size:12.5px}
  .lg{display:grid;grid-template-columns:76px 1fr;gap:10px;padding:6px 8px;
    border-left:3px solid var(--line);border-bottom:1px solid var(--line)}
  .lg:hover{background:var(--panel2)}
  .lg .lv{font-weight:600;font-size:11px;text-transform:uppercase}
  .lg.info .lv{color:var(--dim)} .lg.info{border-left-color:var(--dimmer)}
  .lg.success .lv{color:var(--ok)} .lg.success{border-left-color:var(--ok)}
  .lg.warn .lv{color:var(--warn)} .lg.warn{border-left-color:var(--warn)}
  .lg.error .lv{color:var(--err)} .lg.error{border-left-color:var(--err)}
  .lg .t{color:var(--fg);word-break:break-word}
  .lg .ts{color:var(--dimmer);font-size:11px;grid-column:2;margin-top:-2px}

  @media(max-width:820px){
    .grid{grid-template-columns:1fr}
    nav{margin-left:6px} nav button{padding:6px 10px;font-size:13px}
    .top{padding:10px 14px} .pad{padding:14px}
    .rec .head .run{margin-left:0;width:100%}
  }
</style>
</head>
<body>
<div class="app">
  <div class="top">
    <div class="brand"><span class="dot" id="dot"></span>Hermes Agent</div>
    <nav>
      <button data-v="dash" class="active">Dashboard</button>
      <button data-v="rec">Alur</button>
      <button data-v="notif">Notifikasi</button>
      <button data-v="log">Log</button>
    </nav>
    <div class="meta">
      <span class="chip">mode <b id="mode">…</b></span>
      <span class="chip">jadwal aktif <b id="schedcount">0</b></span>
    </div>
  </div>

  <main>
    <!-- ================= DASHBOARD ================= -->
    <section class="view active" id="v-dash">
      <div class="pad">
        <div class="stats" id="stats"></div>
        <div class="panel">
          <h3>Notifikasi terakhir</h3>
          <div id="dashnotif"></div>
        </div>
        <div class="grid">
          <div class="panel">
            <h3>Aktivitas 7 hari</h3>
            <div class="bars" id="bars"></div>
          </div>
          <div class="panel">
            <h3>Kesehatan</h3>
            <table><tbody id="health"></tbody></table>
          </div>
        </div>
      </div>
    </section>

    <!-- ================= ALUR (daftar recipe) ================= -->
    <section class="view" id="v-rec">
      <div class="pad">
        <div class="panel">
          <h3>Alur otomatis</h3>
          <div id="recipes"></div>
        </div>
      </div>
    </section>

    <!-- ================= NOTIFIKASI ================= -->
    <section class="view" id="v-notif">
      <div class="pad">
        <div class="panel">
          <h3>Pesan yang dikirim ke Telegram</h3>
          <div id="notiflist"></div>
        </div>
      </div>
    </section>

    <!-- ================= LOG ================= -->
    <section class="view" id="v-log">
      <div class="pad">
        <div class="logbar" id="lvlbar"></div>
        <div style="margin-bottom:10px"><a class="act" href="/api/logs?format=txt" style="padding:4px 11px;border:1px solid var(--line);border-radius:6px">⬇️ Export log (.txt)</a></div>
        <div id="loglist"></div>
      </div>
    </section>
  </main>
</div>

<script>
/* ---------- helpers ---------- */
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmtTime = t => t ? new Date(t.replace(' ','T')).toLocaleTimeString('id-ID',{hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '—';
const fmtDate = t => t ? new Date(t.replace(' ','T')).toLocaleDateString('id-ID',{day:'2-digit',month:'short'}) : '—';
const fmtFull = t => t ? new Date(t.replace(' ','T')).toLocaleString('id-ID',{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'}) : '—';

/* ---------- tabs ---------- */
document.querySelectorAll('nav button').forEach(b => b.onclick = () => {
  document.querySelectorAll('nav button').forEach(x => x.classList.remove('active'));
  document.querySelectorAll('.view').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  $('#v-'+b.dataset.v).classList.add('active');
  if (b.dataset.v === 'dash') loadDash();
  if (b.dataset.v === 'rec') loadDash();
  if (b.dataset.v === 'notif') loadNotif();
  if (b.dataset.v === 'log') loadLogs();
});

function statCard(lbl, val, sub, cls=''){
  return `<div class="card ${cls}"><div class="lbl">${lbl}</div>
    <div class="val">${val}</div>${sub?`<div class="sub">${sub}</div>`:''}</div>`;
}

/* ---------- dashboard ---------- */
let DASH = {recipes: []};
let busy = {};          // recipe key -> true saat tombol ditekan

async function loadDash(){
  const d = await fetch('/api/dashboard').then(r=>r.json());
  DASH = d;

  $('#mode').textContent = d.autonomous ? 'otonom' : 'manual';
  $('#schedcount').textContent = d.stats.scheduled;
  $('#dot').classList.toggle('off', !d.autonomous);

  $('#stats').innerHTML =
    statCard('Alur', d.stats.recipes, `${d.stats.scheduled} terjadwal`) +
    statCard('Jalan hari ini', d.stats.today, 'eksekusi') +
    statCard('Sukses', d.stats.ok, `${d.stats.success_rate}% dari total`, 'ok') +
    statCard('Gagal', d.stats.failed, 'perlu dicek', d.stats.failed?'err':'') +
    statCard('Total eksekusi', d.stats.runs, 'sejak dipasang');

  // bar chart
  const days = [];
  for (let i=6;i>=0;i--){
    const dt = new Date(); dt.setDate(dt.getDate()-i);
    const k = dt.getFullYear()+'-'+String(dt.getMonth()+1).padStart(2,'0')+'-'+String(dt.getDate()).padStart(2,'0');
    days.push({k, n: d.by_day[k]||0, lbl: dt.toLocaleDateString('id-ID',{weekday:'short'})});
  }
  const max = Math.max(1, ...days.map(x=>x.n));
  $('#bars').innerHTML = days.map(x =>
    `<div class="bar"><span class="n">${x.n||''}</span>
     <div class="fill" style="height:${(x.n/max*100)}%"></div>
     <span class="d">${x.lbl}</span></div>`).join('');

  // health
  $('#health').innerHTML = [
    ['Mode', d.autonomous ? 'otonom (jadwal aktif)' : 'manual'],
    ['Alur terjadwal', d.stats.scheduled],
    ['API kepegawaian', d.kepeg_api ? '✅ terpasang' : '— belum ada'],
    ['Log total', d.logs.total],
    ['Error log', d.logs.by_level.error||0],
  ].map(([k,v]) => `<tr><td style="color:var(--dim)">${k}</td>
    <td style="text-align:right"><b>${esc(v)}</b></td></tr>`).join('');

  renderRecipes();
  renderNotif('#dashnotif', (d.notifications||[]).slice(0,3), 3);
}

/* ---------- notifikasi (pesan Telegram) ---------- */
function notifCard(n){
  const r = (DASH.recipes||[]).find(x=>x.key===n.recipe);
  const judul = r ? r.title : (n.recipe||'notifikasi');
  return `<div class="ntf ${n.ok===false?'err':''}">
    <div class="ntfhead"><span class="name">${esc(judul)}</span>
      <span class="ts">${fmtFull(n.created_at)}</span></div>
    <div class="ntftext">${esc(n.text)}</div></div>`;
}

function renderNotif(sel, list, maks){
  const wrap = $(sel);
  if (!wrap) return;
  if (!list.length){ wrap.innerHTML = '<div class="empty">Belum ada notifikasi terkirim.</div>'; return; }
  wrap.innerHTML = list.slice(0, maks||200).map(notifCard).join('');
}

async function loadNotif(){
  const d = await fetch('/api/notifications').then(r=>r.json());
  renderNotif('#notiflist', d.notifications||[], 200);
}

function statusPill(st){
  if (st==='ok') return '<span class="pill ok">ok</span>';
  if (st==='failed') return '<span class="pill err">gagal</span>';
  return '<span style="color:var(--dimmer);font-size:12px">belum pernah</span>';
}

function inputField(inp){
  const type = inp.type === 'number' ? 'number' : (inp.type === 'date' ? 'date' : 'text');
  const val = inp.default ? esc(inp.default) : '';
  return `<div class="field">
    <label for="in-${esc(inp.name)}">${esc(inp.label || inp.name)}${inp.required?' *':''}</label>
    <input id="in-${esc(inp.name)}" data-input="${esc(inp.name)}" type="${type}"
      value="${val}" placeholder="${inp.required?'wajib':'opsional'}">
    ${inp.help?`<span class="hint">${esc(inp.help)}</span>`:''}
  </div>`;
}

function renderRecipes(){
  const list = DASH.recipes || [];
  const wrap = $('#recipes');
  if (!list.length){
    wrap.innerHTML = '<div class="empty">Belum ada alur terdaftar.</div>';
    return;
  }
  wrap.innerHTML = list.map(r => {
    const running = r.running || busy[r.key];
    const dur = r.last_duration_ms!=null ? (r.last_duration_ms/1000).toFixed(1)+'s' : '—';
    const sched = r.next_run
      ? `<span class="sched" title="jadwal ${esc(r.schedule)}">berikutnya ${fmtFull(r.next_run)}</span>`
      : (r.schedule ? `<span class="sched">${esc(r.schedule)}</span>`
                    : `<span class="sched">manual</span>`);
    const inputs = (r.inputs||[]).length
      ? `<div class="body"><div class="inputs">${r.inputs.map(inputField).join('')}
           <button class="run" onclick="runRecipe('${r.key}', this)" ${running?'disabled':''}>
             ${running?'Menjalankan…':'▶ Jalankan'}</button>
         </div></div>`
      : '';
    return `<div class="rec ${running?'running':''}" id="rec-${r.key}">
      <div class="head">
        <span class="name">${esc(r.title)}</span>
        ${statusPill(r.last_status)}
        ${sched}
        ${(r.inputs||[]).length ? '' :
          `<button class="run" onclick="runRecipe('${r.key}', this)" ${running?'disabled':''}>
             ${running?'Menjalankan…':'▶ Jalankan'}</button>`}
      </div>
      <div class="desc">${esc(r.description)}</div>
      <div class="steps">${(r.steps||[]).map((s,i)=>`<span>${i+1}. ${esc(s)}</span>`).join(' · ')}</div>
      ${inputs}
      <div class="result ${r.last_reply?'show '+(r.last_status==='ok'?'ok':'err'):''}"
        id="res-${r.key}">${esc(r.last_reply||'')}</div>
      <div class="foot">
        <span>Terakhir: <b>${r.last_run?fmtFull(r.last_run):'—'}</b></span>
        <span>Durasi: <b>${dur}</b></span>
        <span>Total: <b>${r.runs||0}×</b></span>
        <a href="#" onclick="showRecipeLogs('${r.key}');return false" style="margin-left:auto">lihat log</a>
      </div>
    </div>`;
  }).join('');
}

function showRecipeLogs(key){
  document.querySelector('nav button[data-v="log"]').click();
  curLevel=''; recipeFilter = key;
  loadLogs();
}

async function runRecipe(key, btn){
  const card = $('#rec-'+key);
  const payload = {};
  if (card) card.querySelectorAll('[data-input]').forEach(el => {
    const v = el.value.trim();
    if (v) payload[el.dataset.input] = v;
  });

  btn.disabled = true; const old = btn.textContent; btn.textContent = 'Menjalankan…';
  busy[key] = true;
  const box = $('#res-'+key);
  if (box){ box.className='result show'; box.textContent='⏳ Menjalankan alur…'; }

  try{
    const r = await fetch('/api/recipes/'+key+'/run', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({inputs: payload})
    });
    const d = await r.json();
    if (!d.ok){
      if (box){ box.className='result show err'; box.textContent = d.reply || '⚠️ Gagal.'; }
      return;
    }
    // Tunggu thread latar selesai, lalu ambil ringkasan yang tersimpan.
    await waitJob(key, box);
  }catch(e){
    if (box){ box.className='result show err'; box.textContent='⚠️ Gagal: '+e; }
  }finally{
    busy[key] = false;
    btn.disabled = false; btn.textContent = old;
    loadDash();
  }
}

async function waitJob(key, box){
  for (let i=0; i<200; i++){        // maks ~10 menit
    await new Promise(r => setTimeout(r, 3000));
    const d = await fetch('/api/dashboard').then(r=>r.json());
    const rec = (d.recipes||[]).find(x => x.key === key);
    if (!rec) return;
    if (rec.running){ continue; }
    if (box && rec.last_reply){
      box.className = 'result show ' + (rec.last_status==='ok' ? 'ok' : 'err');
      box.textContent = rec.last_reply;
    }
    return;
  }
}

/* ---------- log ---------- */
let curLevel = '';
let recipeFilter = null;
async function loadLogs(){
  const d = await fetch('/api/logs' + (curLevel?('?level='+curLevel):'')).then(r=>r.json());
  const counts = d.counts || {};
  let bar = ['', 'success','info','warn','error'].map(l =>
    `<button class="lvl ${curLevel===l?'active':''}" data-l="${l}">${l||'semua'}
     <span class="c">${l?counts[l]||0:''}</span></button>`).join('');
  if (recipeFilter){
    bar = `<button class="lvl active" onclick="recipeFilter=null;loadLogs()">alur: ${esc(recipeFilter)} ✕</button>` + bar;
  }
  $('#lvlbar').innerHTML = bar;
  document.querySelectorAll('.lvl[data-l]').forEach(b => b.onclick = () => {
    curLevel = b.dataset.l; loadLogs();
  });
  let logs = d.logs || [];
  if (recipeFilter) logs = logs.filter(l => (l.meta && l.meta.recipe) === recipeFilter);
  if (!logs.length){
    $('#loglist').innerHTML = '<div class="empty">Belum ada log.</div>';
    return;
  }
  $('#loglist').innerHTML = logs.map(l =>
    `<div class="lg ${l.level}"><div class="lv">${l.level}</div>
     <div class="t">${esc(l.message)}</div>
     <div class="ts">${esc(l.created_at)} · ${esc(l.event)}${l.meta?' · '+esc(JSON.stringify(l.meta)):''}</div></div>`
  ).join('');
}

/* ---------- boot ---------- */
loadDash();
setInterval(() => {
  if ($('#v-dash').classList.contains('active')) loadDash();
}, 15000);
</script>
</body>
</html>"""


class _State:
    cfg: "Config"


def _get_conn(state: _State) -> sqlite3.Connection:
    """Koneksi SQLite baru per-request — aman untuk server multithread."""
    return tasks.init(state.cfg.db_path)


def _running(key: str) -> bool:
    with _JOBS_LOCK:
        return bool(_JOBS.get(key, {}).get("running"))


def _start_recipe(cfg: "Config", key: str, inputs: dict) -> dict:
    """Jalankan recipe di thread latar. Return ringkasan awal."""
    if _running(key):
        return {"ok": False, "pending": True, "reply": "Alur ini sedang berjalan."}

    meta = recipes.get_recipe(key)
    if meta is None:
        return {"ok": False, "reply": f"Alur '{key}' tidak dikenal."}

    clean, errors = recipes.validate_inputs(key, inputs)
    if errors:
        return {"ok": False, "reply": "⚠️ " + " ".join(errors)}

    def _worker() -> None:
        conn = tasks.init(cfg.db_path)
        try:
            result = recipes.run_recipe(cfg, conn, key, clean)
            reply = recipes.summarize(key, result)
            ok = bool(result.get("ok"))
        except Exception as e:  # noqa: BLE001
            log.exception("Alur %s gagal", key)
            reply, ok = f"⚠️ {type(e).__name__}: {e}", False
        finally:
            conn.close()
        with _JOBS_LOCK:
            _JOBS[key] = {"running": False, "reply": reply, "ok": ok}

    with _JOBS_LOCK:
        _JOBS[key] = {"running": True, "reply": "", "ok": None}
    threading.Thread(target=_worker, name=f"recipe-{key}", daemon=True).start()
    return {"ok": True, "pending": True, "reply": f"▶️ {meta.get('title', key)} dimulai…"}


def _dashboard(state: _State) -> dict:
    conn = _get_conn(state)
    try:
        cfg = state.cfg
        now = datetime.now()
        items = []
        for r in recipes.stats(conn):
            next_run = None
            if r["schedule"]:
                try:
                    # Selalu hitung dari "sekarang" agar tidak pernah menampilkan
                    # jadwal yang sudah lewat (last_run bisa dari zona waktu lain).
                    next_run = cron.next_run(r["schedule"], now).isoformat(timespec="seconds")
                except Exception:  # noqa: BLE001
                    next_run = None
            items.append({**r, "next_run": next_run, "running": _running(r["key"])})

        recipe_stats = db.recipe_stats(conn)
        today = conn.execute(
            "SELECT COUNT(*) FROM logs WHERE event='recipe_done' "
            "AND DATE(created_at,'localtime')=DATE('now','localtime')"
        ).fetchone()[0]
        total_runs = max(recipe_stats["runs"], 0)
        ok = recipe_stats["ok"]
        rate = round(ok / total_runs * 100, 1) if total_runs else 0.0

        return {
            "stats": {
                "recipes": len(items),
                "scheduled": sum(1 for r in items if r["schedule"]),
                "today": today,
                "runs": total_runs,
                "ok": ok,
                "failed": recipe_stats["failed"],
                "success_rate": rate,
            },
            "recipes": items,
            "by_day": db.recipe_runs_by_day(conn, 7),
            "logs": db.log_stats(conn),
            "notifications": _notifications(conn, limit=30),
            "autonomous": any(r["schedule"] for r in items),
            "kepeg_api": _kepeg_api_ready(cfg),
        }
    finally:
        conn.close()


def _notifications(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    """Pesan yang benar-benar dikirim ke Telegram (dicatat saat kirim).

    Isi pesan disimpan di meta.text supaya dashboard menampilkan hal yang
    sama dengan yang diterima di HP — bukan ringkasan yang dihitung ulang.
    """
    hasil = []
    for row in db.list_logs(conn, limit=limit, event="telegram_sent"):
        meta = row.get("meta") or {}
        hasil.append({
            "recipe": meta.get("recipe", ""),
            "text": meta.get("text", ""),
            "created_at": row["created_at"],
            "ok": True,
        })
    return hasil


def _kepeg_api_ready(cfg: "Config") -> bool:
    """Impor ke kepegawaian kini lewat API — cukup ada kunci API."""
    return bool(cfg.kepeg_api_key)


class Handler(BaseHTTPRequestHandler):
    state: _State

    def log_message(self, *args):  # senyapkan log akses default
        pass

    # ---- HTTP Basic Auth (password saja) ----
    def _authorized(self) -> bool:
        cfg = self.state.cfg
        # Tanpa password terkonfigurasi -> akses bebas (mode lokal).
        if not cfg.web_pass:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        import base64
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
        except Exception:  # noqa: BLE001
            return False
        # Terima "<password>" saja atau ":<password>" (username kosong).
        if ":" in decoded:
            _, _, pwd = decoded.partition(":")
        else:
            pwd = decoded
        import hmac
        return hmac.compare_digest(pwd, cfg.web_pass)

    def _deny(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Hermes Agent"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        body = b"Login diperlukan."
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):  # noqa: N802
        if not self._authorized():
            self._deny()
            return
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/dashboard":
            try:
                self._json(200, _dashboard(self.state))
            except Exception as e:  # noqa: BLE001
                log.exception("Gagal memuat dashboard")
                self._json(500, {"error": f"{type(e).__name__}: {e}"})
            return
        if path == "/api/notifications":
            conn = _get_conn(self.state)
            try:
                self._json(200, {"notifications": _notifications(conn, limit=200)})
            finally:
                conn.close()
            return
        if path == "/api/logs":
            conn = _get_conn(self.state)
            q = parse_qs(urlparse(self.path).query)
            level = (q.get("level") or [None])[0]
            if "format" in q and q["format"][0] == "txt":
                rows = db.list_logs(conn, limit=500)
                lines = [
                    f"{r['created_at']} [{r['level']}] {r['event']}: {r['message']}"
                    + (f" | {json.dumps(r['meta'], ensure_ascii=False)}" if r.get("meta") else "")
                    for r in rows
                ]
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Disposition", "attachment; filename=hermes-log.txt")
                body = ("\n".join(lines) + "\n").encode("utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self._json(200, {
                "logs": db.list_logs(conn, limit=150, level=level),
                "counts": db.log_stats(conn).get("by_level", {}),
            })
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if not self._authorized():
            self._deny()
            return
        path = urlparse(self.path).path
        # Jalankan alur dari dashboard: POST /api/recipes/<key>/run
        if path.startswith("/api/recipes/") and path.endswith("/run"):
            key = path.strip("/").split("/")[2]
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            inputs = {}
            if raw:
                try:
                    payload = json.loads(raw.decode("utf-8"))
                    if isinstance(payload, dict):
                        inputs = payload.get("inputs") or {}
                except json.JSONDecodeError:
                    inputs = {}
            try:
                self._json(200, _start_recipe(self.state.cfg, key, inputs))
            except Exception as e:  # noqa: BLE001
                log.exception("Gagal menjalankan alur %s", key)
                self._json(500, {"reply": f"⚠️ {type(e).__name__}: {e}"})
            return
        self._json(404, {"error": "not found"})


def serve(cfg: "Config", host: str | None = None, port: int | None = None) -> None:
    host = host or cfg.web_host
    port = port or cfg.web_port

    state = _State()
    state.cfg = cfg
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    tasks.init(cfg.db_path)  # pastikan skema ada

    Handler.state = state
    server = ThreadingHTTPServer((host, port), Handler)
    log.info("Hermes web UI aktif di http://%s:%d", host, port)
    print(f"🌐 Hermes web UI: http://{host}:{port}  (Ctrl+C untuk berhenti)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nWeb UI dihentikan.")
    finally:
        server.server_close()
