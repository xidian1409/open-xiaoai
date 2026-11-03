#!/usr/bin/env python3
"""小七 · 对话记录（只读页面）。

读 DIALOG_LOG（JSONL）里桥接写下的记录：
  {"ts":..., "role":"user",      "text":...}
  {"ts":..., "role":"assistant", "text":...}
  {"ts":..., "role":"audio",     "file":"....wav", "ms":...}
把 user/assistant 按时间顺序配成一轮，audio 记录作为该轮的原始录音。
"""
from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

LOG = os.environ.get("DIALOG_LOG", "/data/logs/dialog.jsonl")
AUDIO_DIR = os.environ.get("UTTERANCE_DIR", "/data/logs/utterances")
PORT = int(os.environ.get("PORT", "8099"))
MAX_ITEMS = 300

PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>小七 · 对话记录</title>
<style>
  :root{
    --bg:#faf9f7; --card:#fff; --line:#e7e5e4; --ink:#292524; --sub:#78716c;
    --amber:#b45309; --amber-soft:#fef3c7; --amber-line:#f59e0b;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:15px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
  .wrap{max-width:860px;margin:0 auto;padding:28px 20px 64px}
  h1{font-size:20px;margin:0 0 4px;font-weight:600}
  .sub{color:var(--sub);font-size:13px;margin-bottom:20px}
  .stats{background:var(--card);border:1px solid var(--line);border-radius:14px;
         padding:14px 18px;display:flex;justify-content:space-between;
         font-size:13px;color:var(--sub);margin-bottom:22px}
  .stats b{color:var(--ink);font-weight:600}
  .turn{background:var(--card);border:1px solid var(--line);border-radius:14px;
        padding:14px 18px;margin-bottom:12px}
  .turn.barge{border-color:var(--amber-line);background:var(--amber-soft)}
  .row{display:flex;justify-content:space-between;align-items:baseline;gap:12px}
  .who{font-size:12px;color:var(--sub);letter-spacing:.04em}
  .u{margin:6px 0 10px;font-size:16px;font-weight:600}
  .a{color:var(--ink);font-size:15px}
  .a:before{content:"小七：";color:var(--sub);font-weight:400}
  .meta{font-size:12px;color:var(--sub);display:flex;gap:14px;flex-wrap:wrap}
  .tag{background:var(--amber-soft);border:1px solid var(--amber-line);
       color:var(--amber);border-radius:999px;padding:1px 8px;font-size:11px}
  audio{height:32px;margin-top:10px;width:100%}
  .empty{color:var(--sub);text-align:center;padding:60px 0}
  a{color:var(--amber)}
</style></head>
<body><div class="wrap">
  <h1>小七 · 对话记录</h1>
  <div class="sub">客厅 · LX06 · 只读 · <a id="spk" href="#">声纹管理 →</a></div>
  <script>
    // 声纹管理在另一个端口（同一台 NAS），按当前主机名推出来
    document.getElementById('spk').href =
      location.protocol + '//' + location.hostname + ':8097/';
  </script>
  <div class="stats" id="stats"></div>
  <div id="list"></div>
</div>
<script>
const fmt = t => new Date(t*1000).toLocaleString('zh-CN',{hour12:false});
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
async function load(){
  const r = await fetch('/api/dialogs'); const d = await r.json();
  const list = document.getElementById('list');
  if(!d.turns.length){ list.innerHTML = '<div class="empty">还没有对话记录</div>'; return; }
  document.getElementById('stats').innerHTML =
    `<span>共 <b>${d.turns.length}</b> 轮</span>` +
    `<span>今天 <b>${d.today}</b> 轮</span>` +
    `<span>空识别 <b>${d.empty}</b> 条</span>` +
    `<span>最近 <b>${fmt(d.turns[0].ts)}</b></span>`;
  list.innerHTML = d.turns.map(t => `
    <div class="turn ${t.empty?'barge':''}">
      <div class="row"><span class="who">${t.spk && t.spk.name
          ? `<span class="tag">${esc(t.spk.name)} ${t.spk.score?t.spk.score.toFixed(2):''}</span>`
          : '<span class="tag" style="background:#f5f5f4;border-color:#e7e5e4;color:#78716c">未认人</span>'} 你说</span>
        <span class="meta">${t.empty?'<span class="tag">空识别</span>':''}
          ${t.ms?`<span>${(t.ms/1000).toFixed(1)}s</span>`:''}
          <span>${fmt(t.ts)}</span></span></div>
      <div class="u">${esc(t.user) || '（没识别到内容）'}</div>
      <div class="a">${esc(t.assistant) || '（无回复）'}</div>
      ${t.file?`<audio controls preload="none" src="/audio/${encodeURIComponent(t.file)}"></audio>`:''}
    </div>`).join('');
}
load(); setInterval(load, 15000);
</script></body></html>"""


def read_records() -> list[dict]:
    if not os.path.exists(LOG):
        return []
    out = []
    with open(LOG, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def build_turns(records: list[dict]) -> tuple[list[dict], int]:
    """按时间顺序把 user/assistant/audio 拼成轮次。"""
    turns: list[dict] = []
    cur: dict | None = None
    pending_audio: dict | None = None
    empty = 0
    # 声纹结果单独一行写进来，按文件名挂回去
    speaker_of: dict[str, dict] = {}
    for r in records:
        if r.get("role") == "speaker" and r.get("file"):
            speaker_of[r["file"]] = {"name": r.get("name") or "", "score": r.get("score")}
    for r in records:
        role = r.get("role")
        if role == "speaker":
            continue
        if role == "audio":
            # 录音先落盘、识别文本随后才到：先挂起，等下一轮用户话认领
            pending_audio = r
            continue
        elif role == "user":
            cur = {"ts": r.get("ts", 0), "user": r.get("text", ""), "assistant": "",
                   "file": None, "ms": None}
            if pending_audio is not None and abs(cur["ts"] - pending_audio.get("ts", 0)) < 8:
                cur["file"] = pending_audio.get("file")
                cur["ms"] = pending_audio.get("ms")
                cur["spk"] = speaker_of.get(cur["file"])
            pending_audio = None
            turns.append(cur)
        elif role == "assistant":
            if cur is not None:
                cur["assistant"] = (cur["assistant"] + " " + r.get("text", "")).strip()
            else:
                cur = {"ts": r.get("ts", 0), "user": "", "assistant": r.get("text", ""),
                       "file": None, "ms": None}
                turns.append(cur)
    for t in turns:
        text = (t.get("user") or "").strip()
        t["empty"] = not any(ch.isalnum() for ch in text)
        if t["empty"]:
            empty += 1
    turns.reverse()
    return turns[:MAX_ITEMS], empty


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = unquote(self.path.split("?")[0])
        if path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/dialogs":
            turns, empty = build_turns(read_records())
            today_start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
            today = sum(1 for t in turns if t["ts"] >= today_start)
            self._send(200, json.dumps({"turns": turns, "today": today, "empty": empty},
                                       ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        if path.startswith("/audio/"):
            name = os.path.basename(path[len("/audio/"):])
            fp = os.path.join(AUDIO_DIR, name)
            if os.path.isfile(fp):
                with open(fp, "rb") as f:
                    self._send(200, f.read(), "audio/wav")
                return
        self._send(404, b"not found", "text/plain")


if __name__ == "__main__":
    os.makedirs(AUDIO_DIR, exist_ok=True)
    print(f"对话记录页: http://0.0.0.0:{PORT}/  (日志 {LOG})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
