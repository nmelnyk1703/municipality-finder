#!/usr/bin/env python3
"""
Local web UI for the Municipality Finder.

Run:
    python3 web_app.py
Then open http://localhost:8000 in your browser.

No third-party packages required (uses Python's built-in http.server) and it
reuses the logic in municipality_finder.py.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from municipality_finder import find_municipality, CensusError

# Render (and most hosts) inject the port to listen on via $PORT. Bind to
# 0.0.0.0 so the platform can route traffic to us; default to 8000 locally.
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Municipality Finder</title>
<style>
  :root { --accent:#2563eb; --line:#e5e7eb; --bg:#f8fafc; --ok:#16a34a; }
  * { box-sizing:border-box; }
  body { font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
         margin:0; background:var(--bg); color:#0f172a; }
  .wrap { max-width:640px; margin:0 auto; padding:32px 20px 64px; }
  h1 { font-size:24px; margin:0 0 4px; }
  p.sub { color:#64748b; margin:0 0 24px; }
  .card { background:#fff; border:1px solid var(--line); border-radius:12px;
          padding:20px; box-shadow:0 1px 2px rgba(0,0,0,.04); }
  label { display:block; font-size:13px; font-weight:600; margin:12px 0 4px; }
  input { width:100%; padding:10px 12px; border:1px solid var(--line);
          border-radius:8px; font-size:15px; }
  .row { display:flex; gap:12px; }
  .row > div { flex:1; }
  button { margin-top:18px; width:100%; padding:12px; background:var(--accent);
           color:#fff; border:0; border-radius:8px; font-size:15px;
           font-weight:600; cursor:pointer; }
  button:disabled { opacity:.6; cursor:default; }
  .hint { color:#94a3b8; font-size:12px; margin-top:6px; }
  #out { margin-top:24px; }
  .result { background:#fff; border:1px solid var(--line); border-radius:12px;
            padding:20px; }
  .primary { font-size:20px; font-weight:700; }
  .badge { display:inline-block; background:#eff6ff; color:var(--accent);
           border-radius:999px; padding:2px 10px; font-size:13px;
           font-weight:600; margin-left:6px; }
  .meta { color:#64748b; font-size:13px; margin-top:4px; }
  .neigh { margin-top:16px; padding-top:12px; border-top:1px solid var(--line); }
  .neigh h3 { font-size:13px; text-transform:uppercase; letter-spacing:.04em;
              color:#64748b; margin:0 0 8px; }
  .neigh li { list-style:none; display:flex; justify-content:space-between;
              padding:6px 0; }
  .bar { height:8px; background:#e2e8f0; border-radius:999px; overflow:hidden;
         margin-top:4px; }
  .bar > span { display:block; height:100%; background:var(--accent); }
  .reason { margin-top:14px; font-size:14px; color:#334155;
            background:#f8fafc; border-radius:8px; padding:10px 12px; }
  .insp { margin-top:8px; font-size:14px; color:#0f172a;
          background:#ecfdf5; border:1px solid #a7f3d0; border-radius:8px;
          padding:8px 12px; }
  .insp-label { display:inline-block; font-size:11px; font-weight:700;
                text-transform:uppercase; letter-spacing:.04em; color:var(--ok);
                margin-right:6px; }
  .insp-none { background:#f8fafc; border-color:var(--line); color:#94a3b8; }
  .insp-notes { color:#475569; font-size:13px; margin-top:3px; }
  .err { color:#b91c1c; background:#fef2f2; border:1px solid #fecaca;
         border-radius:8px; padding:12px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Municipality Finder</h1>
  <p class="sub">Enter an address, coordinates, or both. Finds the U.S. municipality
    (city / village / town) and any neighbor within 3 miles.</p>
  <div class="card">
    <label>Address</label>
    <input id="address" placeholder="800 Algoma Blvd, Oshkosh, WI 54901">
    <label>Geolocation (latitude, longitude)</label>
    <input id="coords" placeholder="43.0722, -89.5089" inputmode="decimal">
    <div class="hint">Fill in the address OR the geolocation (or both). Paste the
      coordinates as one value — comma or space separated.</div>
    <button id="go" onclick="run()">Find municipality</button>
  </div>
  <div id="out"></div>
</div>
<script>
async function run() {
  const btn = document.getElementById('go');
  const out = document.getElementById('out');
  const address = document.getElementById('address').value.trim();
  const coords = document.getElementById('coords').value.trim();
  if (!address && !coords) {
    out.innerHTML = '<div class="result err">Enter an address or a geolocation.</div>';
    return;
  }
  btn.disabled = true; btn.textContent = 'Searching…';
  out.innerHTML = '';
  try {
    const params = new URLSearchParams();
    if (address) params.set('address', address);
    if (coords) params.set('coords', coords);
    const resp = await fetch('/api?' + params.toString());
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || 'Request failed');
    out.innerHTML = render(data);
  } catch (e) {
    out.innerHTML = '<div class="result err">' + e.message + '</div>';
  } finally {
    btn.disabled = false; btn.textContent = 'Find municipality';
  }
}
function render(d) {
  const p = d.primary, c = d.confidence;
  const loc = [p.county, p.state].filter(Boolean).join(', ');
  let html = '<div class="result">';
  if (d.note) html += '<div class="reason" style="background:#fffbeb;color:#92400e">⚠️ ' + esc(d.note) + '</div>';
  html += '<div class="primary">' + esc(p.name) + ' ' + esc(p.type) +
          '<span class="badge">' + Math.round(c.primary) + '% confidence</span></div>';
  if (loc) html += '<div class="meta">' + esc(loc) + '</div>';
  html += '<div class="meta">' +
          (d.matched_address ? esc(d.matched_address) + ' · ' : '') +
          d.coordinates.lat.toFixed(5) + ', ' + d.coordinates.lon.toFixed(5) + '</div>';
  html += inspectorHtml(p.inspectors);
  const ns = d.neighbors_within_3mi || [];
  if (ns.length) {
    html += '<div class="neigh"><h3>Other municipalities within 3 miles</h3><ul>';
    for (const n of ns) {
      const nc = c.neighbors && c.neighbors[n.full_name];
      html += '<li><span>' + esc(n.name) + ' ' + esc(n.type) +
              ' <span style="color:#94a3b8">(~' + n.approx_miles + ' mi)</span></span>' +
              '<span>' + (nc != null ? Math.round(nc) + '%' : '') + '</span></li>';
      if (nc != null) html += '<div class="bar"><span style="width:' + nc + '%"></span></div>';
      html += inspectorHtml(n.inspectors);
    }
    html += '</ul></div>';
  }
  if (c.reasoning) html += '<div class="reason">' + esc(c.reasoning) + '</div>';
  html += '</div>';
  return html;
}
function inspectorHtml(list) {
  if (!list || !list.length) {
    return '<div class="insp insp-none">Inspector: none on file</div>';
  }
  let out = '';
  for (const ins of list) {
    const head = [ins.inspector, (ins.phones || []).join(' / ')]
                   .filter(Boolean).map(esc).join('  ·  ');
    out += '<div class="insp"><span class="insp-label">Inspector</span> ' +
           (head || '(see notes)');
    if (ins.notes) out += '<div class="insp-notes">' + esc(ins.notes) + '</div>';
    out += '</div>';
  }
  return out;
}
function esc(s){return String(s).replace(/[&<>"]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m]));}
['address','coords'].forEach(id=>document.getElementById(id)
  .addEventListener('keydown',e=>{if(e.key==='Enter')run();}));
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api":
            q = parse_qs(parsed.query)
            address = q.get("address", [None])[0]
            lat = q.get("lat", [None])[0]
            lon = q.get("lon", [None])[0]
            coords = q.get("coords", [None])[0]
            try:
                latf = float(lat) if lat else None
                lonf = float(lon) if lon else None
                if coords:  # single "lat, lon" (or space-separated) string
                    parts = [p for p in coords.replace(",", " ").split() if p]
                    if len(parts) != 2:
                        raise ValueError(
                            'Geolocation must be "latitude, longitude" '
                            '(two numbers), e.g. "43.0722, -89.5089".')
                    latf, lonf = float(parts[0]), float(parts[1])
                result = find_municipality(address, latf, lonf)
                self._send(200, json.dumps(result).encode("utf-8"),
                           "application/json")
            except (CensusError, ValueError) as exc:
                self._send(400, json.dumps({"error": str(exc)}).encode("utf-8"),
                           "application/json")
            except Exception as exc:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(exc)}).encode("utf-8"),
                           "application/json")
            return
        self._send(404, b"Not found", "text/plain")

    def log_message(self, *args) -> None:  # quiet console
        return


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Municipality Finder web UI running at:  http://localhost:{PORT}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
