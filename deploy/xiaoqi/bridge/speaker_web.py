#!/usr/bin/env python3
"""小七 · 声纹管理（登记 + 测试）。

单独一个页面，不动原来的对话记录页。
  GET  /                 管理页
  GET  /api/state        人员 + 片段 + 质量
  POST /api/people       新增人员  {name}
  POST /api/people/del   删除人员  {id}
  POST /api/enroll       登记一段  {person, sentence, wav(base64)}
  POST /api/test         测试一段  {wav(base64)}
  POST /api/clip/del     删除片段  {person, clip}
"""
from __future__ import annotations

import base64
import io
import json
import os
import ssl
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

from speaker_lib import (
    SENTENCES,
    DATA_DIR,
    add_clip,
    embed_pcm,
    intra_similarity,
    load_clips,
    load_people,
    save_people,
)

PORT = int(os.environ.get("PORT", "8097"))
TLS_PORT = int(os.environ.get("TLS_PORT", "8444"))
TLS_DIR = os.path.join(os.environ.get("SPEAKER_DIR", "/data/speakers"), "tls")


def parse_wav(data: bytes) -> np.ndarray | None:
    """浏览器已经重采样成 16k 单声道，这里只做最朴素的解析。"""
    try:
        with wave.open(io.BytesIO(data)) as w:
            if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
                return None
            raw = w.readframes(w.getnframes())
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    except Exception:
        return None


def state() -> dict:
    people = load_people()
    out = []
    for pid, info in people.items():
        clips = load_clips(pid)
        out.append(
            {
                "id": pid,
                "name": info.get("name", pid),
                "ts": info.get("ts"),
                "clips": [
                    {"id": c["id"], "sentence": c.get("sentence", -1), "src": c.get("src", "")}
                    for c in clips
                ],
                "quality": intra_similarity(clips),
            }
        )
    pend = {}
    ppath = os.path.join(DATA_DIR, "pending.json")
    if os.path.exists(ppath):
        try:
            with open(ppath, encoding="utf-8") as f:
                pend = json.load(f) or {}
        except Exception:
            pend = {}
        if float(pend.get("expire", 0)) < __import__("time").time():
            pend = {}
    return {"people": out, "sentences": SENTENCES, "pending": pend}


PAGE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>小七 · 声纹管理</title>
<style>
  :root{--bg:#faf9f7;--card:#fff;--line:#e7e5e4;--ink:#292524;--sub:#78716c;
        --amber:#b45309;--amber-soft:#fef3c7;--amber-line:#f59e0b;--ok:#15803d;--warn:#b91c1c}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:15px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
  .wrap{max-width:860px;margin:0 auto;padding:28px 20px 64px}
  h1{font-size:20px;margin:0 0 4px;font-weight:600}
  .sub{color:var(--sub);font-size:13px;margin-bottom:18px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;
        padding:16px 18px;margin-bottom:14px}
  .hint{background:var(--amber-soft);border:1px solid var(--amber-line);border-radius:14px;
        padding:14px 18px;margin-bottom:18px;font-size:13px;color:#7c2d12}
  .hint b{color:var(--amber)}
  .row{display:flex;justify-content:space-between;align-items:center;gap:12px}
  .name{font-weight:600;font-size:16px}
  .meta{color:var(--sub);font-size:12px;display:flex;gap:14px;flex-wrap:wrap;margin-top:2px}
  .pill{background:#f5f5f4;border:1px solid var(--line);border-radius:999px;
        padding:1px 9px;font-size:11px;color:var(--sub)}
  .pill.ok{background:#f0fdf4;border-color:#bbf7d0;color:var(--ok)}
  .pill.bad{background:#fef2f2;border-color:#fecaca;color:var(--warn)}
  input[type=text]{border:1px solid var(--line);border-radius:10px;padding:9px 12px;
                   font:inherit;width:180px;background:#fff}
  button{border:1px solid var(--amber-line);background:#fff;color:var(--amber);
         border-radius:10px;padding:7px 13px;font:inherit;cursor:pointer}
  button:hover{background:var(--amber-soft)}
  button.ghost{border-color:var(--line);color:var(--sub)}
  button.rec{background:var(--amber-soft)}
  .sent{display:flex;justify-content:space-between;align-items:center;gap:10px;
        padding:7px 0;border-top:1px dashed var(--line);font-size:14px}
  .sent:first-of-type{border-top:none}
  .done{color:var(--ok);font-size:12px}
  .todo{color:var(--sub);font-size:12px}
  .empty{color:var(--sub);text-align:center;padding:36px 0}
  #msg{position:fixed;left:50%;bottom:26px;transform:translateX(-50%);
       background:#292524;color:#fff;padding:9px 16px;border-radius:10px;font-size:13px;
       opacity:0;transition:opacity .2s;pointer-events:none}
  #msg.show{opacity:.94}
  .score{font-variant-numeric:tabular-nums}
  a{color:var(--amber)}
</style></head>
<body><div class="wrap">
  <h1>小七 · 声纹管理</h1>
  <div class="sub">给每位家人登记几句话，之后用这些数据判断"能不能认出人"。<a href="/">← 对话记录</a></div>

  <div class="hint" id="secur" style="display:none;background:#fef2f2;border-color:#fecaca;color:#991b1b">
    <b>当前是 http 内网地址，浏览器禁止网页录音。</b>
    要现场录，请改用安全地址：<a id="tls" href="#" style="font-weight:600">切换到 HTTPS</a>（首次会提示证书不受信任，选“继续访问”即可）。
    也可以直接用「上传」——手机录音机录好再选文件。
  </div>

  <div class="hint">
    <b>录音要用音箱旁边的真实距离。</b>手机贴嘴录的"干净声音"和音箱听到的远场声音差得很远，
    拿它登记会让模型在你家失效。最理想是站在平时说话的位置，对着小七说这几句话。
  </div>

  <div class="card">
    <div class="row" style="margin-bottom:12px">
      <div class="name">新增人员</div>
      <div style="display:flex;gap:8px">
        <input type="text" id="newname" placeholder="比如 爸爸 / 妈妈 / 哥哥">
        <button onclick="addPerson()">添加</button>
      </div>
    </div>
    <div id="list"></div>
  </div>

  <div class="card">
    <div class="row" style="margin-bottom:10px">
      <div class="name">测试：这段是谁说的？</div>
      <div style="display:flex;gap:8px">
        <button class="rec" onclick="pickFile(true)">选择文件</button>
        <button class="rec" onclick="record(true)">现场录</button>
      </div>
    </div>
    <div id="testout" class="meta">还没有测试。</div>
  </div>
</div>
<input type="file" id="file" accept="audio/*" style="display:none">
<div id="msg"></div>

<script>
let ST = {people: [], sentences: []};
let pending = null;   // {kind:'enroll', person, sentence} | {kind:'test'}

function toast(t){const m=document.getElementById('msg');m.textContent=t;m.classList.add('show');
  clearTimeout(window._tt);window._tt=setTimeout(()=>m.classList.remove('show'),2200);}

async function refresh(){
  const r = await fetch('/api/state'); ST = await r.json(); render();
}

function render(){
  if(ST.pending && ST.pending.person){
    const nm = (ST.people.find(x=>x.id===ST.pending.person)||{}).name || '';
    toast('等待收录：' + nm + ' 第 ' + (ST.pending.sentence+1) + ' 句 —— 请喊「你好小七」再说');
  } else if(window._capturing){
    window._capturing = false; toast('已收录，看看上面那句是不是变「已录」了');
  }
  const L = document.getElementById('list');
  if(!ST.people.length){ L.innerHTML = '<div class="empty">还没有人员，先添加一个。</div>'; return; }
  L.innerHTML = ST.people.map(p=>{
    const bySent = {};
    p.clips.forEach(c=>{ (bySent[c.sentence] = bySent[c.sentence] || []).push(c.src === 'device' ? '设备' : '手机'); });
    const label = i => {
      const srcs = bySent[i];
      if(!srcs) return '';
      const u = [...new Set(srcs)];
      return u.length > 1 ? '已录·两种' : '已录·' + u[0];
    };
    const q = p.quality===null ? '<span class="pill">样本不足</span>' :
      `<span class="pill ${p.quality>=0.5?'ok':'bad'}">一致性 ${p.quality.toFixed(2)}</span>`;
    const sents = ST.sentences.map((s,i)=>`
      <div class="sent">
        <span>${i+1}. ${s}</span>
        <span style="display:flex;gap:6px;align-items:center">
        ${bySent[i]?`<span class="done">${label(i)}</span>`:``}
        <button class="ghost" onclick="record(false,'${p.id}',${i})">录音</button>
        <button class="ghost" onclick="pickFile(false,'${p.id}',${i})">上传</button>
        <button onclick="deviceCapture('${p.id}',${i})">对小七说</button>
        </span>
      </div>`).join('');
    return `<div style="border-top:1px solid var(--line);padding-top:12px;margin-top:12px">
      <div class="row">
        <div>
          <div class="name">${p.name}</div>
          <div class="meta">${q}<span class="pill">${p.clips.length} 段</span></div>
        </div>
        <button class="ghost" onclick="delPerson('${p.id}','${p.name}')">删除</button>
      </div>
      <div style="margin-top:8px">${sents}</div>
    </div>`;
  }).join('');
}

async function addPerson(){
  const v = document.getElementById('newname').value.trim();
  if(!v) return toast('先填个名字');
  const r = await fetch('/api/people',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:v})});
  const j = await r.json();
  if(!r.ok) return toast(j.error||'失败');
  document.getElementById('newname').value=''; refresh(); toast('已添加 '+v);
}
async function delPerson(id,name){
  if(!confirm('删除 '+name+' 及其全部录音？')) return;
  await fetch('/api/people/del',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id})});
  refresh();
}
async function deviceCapture(pid, i){
  const r = await fetch('/api/capture',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({person:pid, sentence:i})});
  const j = await r.json();
  if(!r.ok) return toast(j.error||'失败');
  window._capturing = true;
  toast('现在喊「你好小七」，再把这句话念一遍（120 秒内有效）');
  refresh();
}
function pickFile(isTest, person, sentence){
  pending = isTest ? {kind:'test'} : {kind:'enroll', person, sentence};
  const f = document.getElementById('file'); f.value=''; f.click();
}
document.getElementById('file').onchange = e => {
  const f = e.target.files[0]; if(f) handleBlob(f);
};

let rec=null, chunks=[];
async function record(isTest, person, sentence){
  if(!navigator.mediaDevices){
    return toast('这个地址禁止录音（浏览器只允许 HTTPS 或 localhost）。请用「上传」，或点上面的安全地址。');
  }
  pending = isTest ? {kind:'test'} : {kind:'enroll', person, sentence};
  const stream = await navigator.mediaDevices.getUserMedia({audio:{
    channelCount:1, echoCancellation:false, noiseSuppression:false, autoGainControl:false}});
  rec = new MediaRecorder(stream); chunks=[];
  rec.ondataavailable = ev => chunks.push(ev.data);
  rec.onstop = async () => {
    stream.getTracks().forEach(t=>t.stop());
    toast('处理中…');
    handleBlob(new Blob(chunks,{type:chunks[0].type}));
  };
  rec.start(); toast('开始录音，说那句话…');
  setTimeout(()=>{ if(rec && rec.state==='recording'){ rec.stop(); } }, 6000);
}

async function handleBlob(blob){
  try{
    const buf = await blob.arrayBuffer();
    const ctx = new (window.AudioContext||window.webkitAudioContext)();
    const audio = await ctx.decodeAudioData(buf);
    const target = 16000, len = Math.max(1, Math.round(audio.duration*target));
    const off = new OfflineAudioContext(1, len, target);
    const src = off.createBufferSource(); src.buffer = audio; src.connect(off.destination); src.start();
    const rendered = await off.startRendering();
    const pcm = rendered.getChannelData(0);
    // 太短直接拒绝
    if(pcm.length/target < 0.6) { toast('太短了（不足 0.6 秒）'); return; }
    const wav = encodeWav(pcm, target);
    await send(b64(wav));
  }catch(e){ toast('解码失败：'+e.message); }
}

function encodeWav(f32, rate){
  const n = f32.length, buf = new ArrayBuffer(44 + n*2), v = new DataView(buf);
  const ws = (o,s)=>{ for(let i=0;i<s.length;i++) v.setUint8(o+i, s.charCodeAt(i)); };
  ws(0,'RIFF'); v.setUint32(4, 36+n*2, true); ws(8,'WAVE'); ws(12,'fmt ');
  v.setUint32(16,16,true); v.setUint16(20,1,true); v.setUint16(22,1,true);
  v.setUint32(24,rate,true); v.setUint32(28,rate*2,true); v.setUint16(32,2,true);
  v.setUint16(34,16,true); ws(36,'data'); v.setUint32(40, n*2, true);
  let o=44; for(let i=0;i<n;i++){ const s=Math.max(-1,Math.min(1,f32[i]));
    v.setInt16(o, s<0? s*0x8000 : s*0x7fff, true); o+=2; }
  return new Uint8Array(buf);
}
function b64(u8){ let s=''; for(let i=0;i<u8.length;i+=0x8000)
  s+=String.fromCharCode.apply(null, u8.subarray(i,i+0x8000)); return btoa(s); }

async function send(wav64){
  if(!pending) return;
  if(pending.kind==='enroll'){
    const r = await fetch('/api/enroll',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({person:pending.person, sentence:pending.sentence, wav:wav64})});
    const j = await r.json();
    if(!r.ok) return toast(j.error||'登记失败');
    refresh(); toast('已登记');
  }else{
    const r = await fetch('/api/test',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({wav:wav64})});
    const j = await r.json();
    const out = document.getElementById('testout');
    if(!r.ok){ out.textContent = j.error||'测试失败'; return; }
    if(!j.ranking.length){ out.textContent='还没有登记任何人'; return; }
    out.innerHTML = j.ranking.map(x=>{
      const cls = x.score>=0.5?'ok':'';
      return `<span class="pill ${cls} score">${x.name} ${x.score.toFixed(3)}</span>`;
    }).join(' ');
  }
  pending = null;
}

if(!window.isSecureContext){
  document.getElementById('secur').style.display='block';
  document.getElementById('tls').href = 'https://' + location.hostname + ':8444/';
}
setInterval(()=>{ if(window._capturing) refresh(); }, 2500);
refresh();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode())

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/api/state"):
            return self._json(state())
        if self.path.startswith("/api/clips"):
            return self._json({"ok": True})
        if self.path == "/" or self.path.startswith("/index"):
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json({"error": "bad request"}, 400)

        if self.path.startswith("/api/people/del"):
            people = load_people()
            pid = req.get("id", "")
            people.pop(pid, None)
            save_people(people)
            d = os.path.join(DATA_DIR, pid)
            if pid and os.path.isdir(d):
                for f in os.listdir(d):
                    try:
                        os.remove(os.path.join(d, f))
                    except OSError:
                        pass
                try:
                    os.rmdir(d)
                except OSError:
                    pass
            return self._json({"ok": True})

        if self.path.startswith("/api/people"):
            name = (req.get("name") or "").strip()
            if not name:
                return self._json({"error": "名字不能为空"}, 400)
            people = load_people()
            exist = [p for p in people.values() if p.get("name") == name]
            if exist:
                return self._json({"error": "已经有同名的人了"}, 400)
            pid = "p_" + os.urandom(4).hex()
            people[pid] = {"name": name, "ts": int(__import__("time").time())}
            save_people(people)
            return self._json({"ok": True, "id": pid})

        if self.path.startswith("/api/enroll"):
            pid = req.get("person", "")
            if pid not in load_people():
                return self._json({"error": "人员不存在"}, 400)
            try:
                wav = base64.b64decode(req.get("wav", ""))
            except Exception:
                return self._json({"error": "音频解码失败"}, 400)
            pcm = parse_wav(wav)
            if pcm is None:
                return self._json({"error": "音频格式不对（需要 16k 单声道）"}, 400)
            emb = embed_pcm(pcm)
            if emb is None:
                return self._json({"error": "这段太短或模型没加载"}, 400)
            add_clip(pid, wav, emb, int(req.get("sentence", -1)), req.get("src", "upload"))
            return self._json({"ok": True})

        if self.path.startswith("/api/capture"):
            pid = req.get("person", "")
            if pid not in load_people():
                return self._json({"error": "人员不存在"}, 400)
            t = __import__("time").time()
            with open(os.path.join(DATA_DIR, "pending.json"), "w", encoding="utf-8") as f:
                json.dump({"person": pid, "sentence": int(req.get("sentence", -1)),
                           "ts": t, "expire": t + 120}, f)
            return self._json({"ok": True})

        if self.path.startswith("/api/test"):
            try:
                wav = base64.b64decode(req.get("wav", ""))
            except Exception:
                return self._json({"error": "音频解码失败"}, 400)
            pcm = parse_wav(wav)
            if pcm is None:
                return self._json({"error": "音频格式不对"}, 400)
            emb = embed_pcm(pcm)
            if emb is None:
                return self._json({"error": "这段太短"}, 400)
            people = load_people()
            ranking = []
            for pid, info in people.items():
                clips = load_clips(pid)
                if not clips:
                    continue
                V = np.asarray([c["emb"] for c in clips], dtype=np.float32)
                V /= np.linalg.norm(V, axis=1, keepdims=True) + 1e-9
                # 逐片段取最高分更稳（质心会被人数少的样本拖偏）
                ranking.append(
                    {"name": info.get("name", pid), "score": float((V @ emb).max())}
                )
            ranking.sort(key=lambda x: -x["score"])
            return self._json({"ok": True, "ranking": ranking})

        self._send(404, b"not found", "text/plain")


def serve_http() -> None:
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


def serve_https() -> None:
    cert = os.path.join(TLS_DIR, "cert.pem")
    key = os.path.join(TLS_DIR, "key.pem")
    if not (os.path.exists(cert) and os.path.exists(key)):
        print("⚠️ 没有 TLS 证书，跳过 HTTPS（网页录音会不可用）", flush=True)
        return
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    srv = ThreadingHTTPServer(("0.0.0.0", TLS_PORT), Handler)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    print(f"🔒 安全地址: https://0.0.0.0:{TLS_PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)
    print(f"🎙️ 声纹管理页: http://0.0.0.0:{PORT}", flush=True)
    threading.Thread(target=serve_https, daemon=True).start()
    serve_http()
