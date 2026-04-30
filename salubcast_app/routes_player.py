from .shared import *


def weather_for_screen(screen: sqlite3.Row) -> dict[str, str] | None:
    city = str(screen["weather_city"] or "").strip()
    if not city:
        return None
    return get_weather_summary(city)


@app.route("/health")
def health() -> Response:
    if HEALTH_STATUS_TOKEN:
        sent = request.headers.get("X-Health-Token", "").strip()
        if not sent or not hmac.compare_digest(sent, HEALTH_STATUS_TOKEN):
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
    try:
        db_ok = fetch_one("SELECT 1 AS ok")
        media_dir_ok = UPLOAD_DIR.exists()
        payload = {
            "ok": bool(db_ok),
            "app": BRAND["name"],
            "database": "ok" if db_ok else "error",
            "media_dir": "ok" if media_dir_ok else "missing",
            "time": now_iso(),
        }
        return jsonify(payload), 200 if db_ok and media_dir_ok else 503
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503

@app.route("/api/feed-ticker")
def feed_ticker_api():
    screen_id = request.headers.get('X-Screen-ID') or request.args.get('screen_id', '').strip()
    screen_name = request.args.get("screen", "").strip()
    screen_token = request.headers.get('X-Screen-Token') or request.args.get('token', '').strip()
    screen = resolve_screen_by_auth(screen_name=screen_name, screen_id=screen_id, screen_token=screen_token)
    if not screen:
        return jsonify({"error": "Unauthorized player"}), 401
    text = get_ticker_text_for_screen(screen)
    return jsonify({"text": text})

def resolve_screen_by_auth(screen_name: str | None = None, screen_id: str | None = None, screen_token: str | None = None) -> sqlite3.Row | None:
    screen = None
    if screen_id:
        screen = fetch_one("SELECT * FROM screens WHERE id = ? LIMIT 1", (screen_id,))
    elif screen_name:
        candidates = fetch_all("SELECT * FROM screens WHERE name = ?", (screen_name,))
        if screen_token:
            screen = next((row for row in candidates if verify_screen_token(screen_token, row["token"])), None)
        elif len(candidates) == 1:
            screen = candidates[0]
    if not screen:
        return None
    if verify_screen_token(screen_token or "", screen['token']):
        return screen
    return None

def resolve_active_playlist_for_screen(screen: sqlite3.Row) -> dict[str, Any] | None:
    company = fetch_one("SELECT * FROM companies WHERE id = ? LIMIT 1", (screen["company_id"],))
    if not company or company["is_active"] != 1:
        return {'screen': {
            'name': screen['name'],
            'location': screen['location'],
            'orientation': screen['orientation'] or 'landscape',
            'badge_visible': screen['badge_visible'] if 'badge_visible' in screen.keys() else 1,
            'badge_position': screen['badge_position'] if 'badge_position' in screen.keys() else 'top-right',
            'image_fit': screen['image_fit'] if 'image_fit' in screen.keys() else 'contain',
            'portrait_image_fit': screen['portrait_image_fit'] if 'portrait_image_fit' in screen.keys() else 'contain',
            'feed_layout': screen['feed_layout'] if 'feed_layout' in screen.keys() else 'cards',
        }, 'blocked': True, 'items': []}
    weekday = datetime.now().weekday()
    now_str = datetime.now().strftime("%H:%M")
    schedules = fetch_all("SELECT * FROM schedules WHERE screen_id = ? AND company_id = ? AND active = 1 ORDER BY priority ASC, start_time ASC", (screen['id'], screen['company_id']))
    chosen = None
    for sch in schedules:
        if is_schedule_live(sch, weekday, now_str):
            chosen = sch
            break
    if not chosen:
        return None
    media_rows = fetch_all("SELECT m.title, m.filename, m.mimetype, m.duration_seconds, pi.sort_order FROM playlist_items pi JOIN media m ON m.id = pi.media_id WHERE pi.playlist_id = ? ORDER BY pi.sort_order ASC", (chosen['playlist_id'],))
    items = [{'title': i['title'], 'url': url_for('uploaded_file', filename=i['filename']), 'mimetype': i['mimetype'], 'duration_seconds': i['duration_seconds'], 'sort_order': i['sort_order']} for i in media_rows]

    if screen['insert_feed_pages'] == 1:
        orientation = screen['orientation'] or 'landscape'
        feed_limit = 9 if orientation == 'portrait' else 8
        per_page = 3 if orientation == 'portrait' else 4
        feed_name, feed_entries = get_feed_page_entries(screen['company_id'], feed_limit)
        if feed_entries:
            feed_pages = chunk_feed_entries(feed_entries, per_page, orientation)
            augmented = []
            every = max(1, int(screen['feed_page_every'] or 3))
            duration = max(8, int(screen['feed_page_duration'] or 12))
            inserted_feed_page = False
            for idx, item in enumerate(items, 1):
                augmented.append(item)
                if idx % every == 0:
                    for page_idx, page_entries in enumerate(feed_pages, 1):
                        augmented.append({
                            'title': f'Nieuws | {feed_name or "Updates"}',
                            'url': '',
                            'mimetype': 'application/x-feed-page',
                            'duration_seconds': duration,
                            'sort_order': 100000 + idx + (page_idx / 100),
                            'feed_name': feed_name or 'Nieuws',
                            'feed_entries': page_entries,
                            'feed_page_index': page_idx,
                            'feed_page_count': len(feed_pages),
                            'weather': weather_for_screen(screen),
                        })
                    inserted_feed_page = True
            if items and not inserted_feed_page:
                for page_idx, page_entries in enumerate(feed_pages, 1):
                    augmented.append({
                        'title': f'Nieuws | {feed_name or "Updates"}',
                        'url': '',
                        'mimetype': 'application/x-feed-page',
                        'duration_seconds': duration,
                        'sort_order': 199999 + (page_idx / 100),
                        'feed_name': feed_name or 'Nieuws',
                        'feed_entries': page_entries,
                        'feed_page_index': page_idx,
                        'feed_page_count': len(feed_pages),
                        'weather': weather_for_screen(screen),
                    })
            if not items:
                for page_idx, page_entries in enumerate(feed_pages, 1):
                    augmented.append({
                        'title': f'Nieuws | {feed_name or "Updates"}',
                        'url': '',
                        'mimetype': 'application/x-feed-page',
                        'duration_seconds': duration,
                        'sort_order': 100000 + (page_idx / 100),
                        'feed_name': feed_name or 'Nieuws',
                        'feed_entries': page_entries,
                        'feed_page_index': page_idx,
                        'feed_page_count': len(feed_pages),
                        'weather': weather_for_screen(screen),
                    })
            items = augmented

    return {
        'screen': {
            'name': screen['name'],
            'location': screen['location'],
            'id': screen['id'],
            'orientation': screen['orientation'] or 'landscape',
            'insert_feed_pages': screen['insert_feed_pages'],
            'feed_page_every': screen['feed_page_every'],
            'feed_page_duration': screen['feed_page_duration'],
            'badge_visible': screen['badge_visible'],
            'badge_position': screen['badge_position'],
            'image_fit': screen['image_fit'],
            'portrait_image_fit': screen['portrait_image_fit'],
            'feed_layout': screen['feed_layout'],
        },
        'playlist_id': chosen['playlist_id'],
        'schedule_priority': chosen['priority'],
        'items': items
    }

@app.route("/api/player/heartbeat", methods=["POST"])
def heartbeat() -> Response:
    payload = request.get_json(silent=True) or {}
    screen_id = request.headers.get('X-Screen-ID') or payload.get('screen_id')
    screen_token = request.headers.get('X-Screen-Token') or payload.get('token')
    screen = resolve_screen_by_auth(screen_id=screen_id, screen_token=screen_token)
    if not screen:
        return jsonify({'ok': False, 'error': 'Unauthorized player'}), 401
    execute("UPDATE screens SET last_seen = ?, device_last_ip = ? WHERE id = ?", (now_iso(), request.remote_addr, screen['id']))
    return jsonify({'ok': True, 'timestamp': now_iso()})

@app.route("/api/player/playlist")
def player_playlist() -> Response:
    screen_id = request.headers.get('X-Screen-ID') or request.args.get('screen_id', '')
    screen_name = request.args.get('screen', '')
    screen_token = request.headers.get('X-Screen-Token') or request.args.get('token', '')
    screen = resolve_screen_by_auth(screen_name=screen_name, screen_id=screen_id, screen_token=screen_token)
    if not screen:
        return jsonify({'error': 'Unauthorized player'}), 401
    result = resolve_active_playlist_for_screen(screen)
    if not result:
        return jsonify({'screen': screen['name'], 'items': []})
    return jsonify(result)

@app.route("/player")
def player_page() -> str:
    screen_name = request.args.get('screen', 'Receptie TV')
    screen_id = request.args.get('screen_id', '')
    query_screen_token = request.args.get('token', '')
    cookie_screen_id = request.cookies.get('salubcast_screen_id', '')
    cookie_screen_token = request.cookies.get('salubcast_screen_token', '') if cookie_screen_id == screen_id else ''
    bootstrap_screen_token = query_screen_token or cookie_screen_token
    html = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{BRAND['name']} Player - {screen_name}</title>
  <style>html, body {{ margin:0; width:100%; height:100%; overflow:hidden; background:#02060c; color:white; font-family:"Segoe UI Variable Display","Segoe UI","Trebuchet MS",sans-serif; }} #stage {{ position:fixed; inset:0; background:#000; overflow:hidden; }} .layer {{ position:absolute; inset:0; opacity:0; transition: opacity 900ms ease-in-out; display:flex; align-items:center; justify-content:center; background:#000; }} .layer.active {{ opacity:1; }} .layer img, .layer video, .layer iframe {{ width:100%; height:100%; object-fit:contain; border:0; background:#000; }} #empty {{ color:#94a3b8; font-size:40px; text-align:center; max-width:20ch; line-height:1.2; }} #badge {{ position:fixed; top:18px; right:18px; background:linear-gradient(180deg, rgba(8,18,30,.84), rgba(8,18,30,.62)); border:1px solid rgba(255,255,255,.12); padding:10px 16px; border-radius:999px; font-size:13px; letter-spacing:.08em; text-transform:uppercase; font-weight:800; z-index:10; backdrop-filter: blur(14px); }} body.badge-top-left #badge {{ top:18px; left:18px; right:auto; bottom:auto; }} body.badge-bottom-right #badge {{ bottom:18px; right:18px; top:auto; left:auto; }} body.badge-bottom-left #badge {{ bottom:18px; left:18px; top:auto; right:auto; }} body.badge-hidden #badge {{ display:none; }} body.feed-active #badge {{ display:none; }} #newsTicker {{ position:fixed; left:18px; right:18px; bottom:8px; z-index:20; background:linear-gradient(90deg, rgba(6,12,22,.96), rgba(10,23,36,.88)); border:1px solid rgba(255,255,255,.1); color:white; padding:16px 22px; font-size:24px; border-radius:22px; white-space:nowrap; overflow:hidden; display:none; box-shadow:0 22px 46px rgba(0,0,0,.24); backdrop-filter: blur(16px); }} #newsTickerInner {{ display:inline-block; padding-left:100%; animation:tickerMove 60s linear infinite; font-weight:800; letter-spacing:.01em; }} .feed-page {{ width:100%; height:100%; display:grid; grid-template-rows:auto auto minmax(0, 1fr); gap:24px; padding:48px; align-content:start; position:relative; overflow:hidden; background:
linear-gradient(135deg, rgba(34,197,94,.18), transparent 30%),
radial-gradient(circle at top right, rgba(56,189,248,.2), transparent 26%),
linear-gradient(180deg, #060c14, #0a1623 44%, #0e1d30 100%); }}
body.feed-active .feed-page {{ padding-top:48px; }}
.feed-page.theme-sunrise {{ background:
linear-gradient(135deg, rgba(251,191,36,.18), transparent 30%),
radial-gradient(circle at top right, rgba(249,115,22,.18), transparent 26%),
linear-gradient(180deg, #180d14, #231428 44%, #1d2940 100%); }}
.feed-page.theme-aqua {{ background:
linear-gradient(135deg, rgba(45,212,191,.18), transparent 30%),
radial-gradient(circle at top right, rgba(56,189,248,.2), transparent 26%),
linear-gradient(180deg, #041019, #092233 44%, #0e3a45 100%); }}
.feed-page.theme-coral {{ background:
linear-gradient(135deg, rgba(251,113,133,.18), transparent 30%),
radial-gradient(circle at top right, rgba(244,114,182,.18), transparent 26%),
linear-gradient(180deg, #170d16, #28152a 44%, #252f49 100%); }}
.feed-page::before {{ content:''; position:absolute; inset:0; pointer-events:none; background:
linear-gradient(180deg, rgba(255,255,255,.05), transparent 20%, transparent 80%, rgba(255,255,255,.03)),
radial-gradient(circle at 18% 10%, rgba(255,255,255,.08), transparent 24%); }}
.feed-page::after {{ content:''; position:absolute; inset:auto -12% -24% auto; width:46vw; height:46vw; border-radius:50%; background:radial-gradient(circle, rgba(255,255,255,.08), transparent 60%); filter:blur(34px); opacity:.28; pointer-events:none; }}
.feed-page .feed-hero {{ display:grid; grid-template-columns:minmax(0, 1.4fr) auto; align-items:end; gap:26px; padding-bottom:18px; border-bottom:1px solid rgba(255,255,255,.1); }}
.feed-page .feed-kicker {{ font-size:15px; letter-spacing:.28em; text-transform:uppercase; color:#fde68a; opacity:.95; font-weight:900; }}
.feed-page h1 {{ margin:0; font-size:76px; line-height:.94; letter-spacing:-.06em; max-width:11ch; }}
.feed-page .feed-sub {{ color:#dbeafe; font-size:24px; line-height:1.35; max-width:38ch; }}
.feed-page .feed-header {{ display:grid; grid-template-columns:minmax(0,1fr) 360px; gap:20px; align-items:stretch; }}
.feed-page .feed-title-block {{ display:grid; gap:10px; align-content:start; }}
.feed-page .feed-pager {{ display:flex; align-items:center; gap:14px; flex-wrap:wrap; margin-top:14px; }}
.feed-page .feed-page-label {{ color:#fef3c7; font-size:14px; letter-spacing:.18em; text-transform:uppercase; font-weight:900; }}
.feed-page .feed-dots {{ display:flex; gap:10px; }}
.feed-page .feed-dot {{ width:12px; height:12px; border-radius:999px; background:rgba(255,255,255,.16); border:1px solid rgba(255,255,255,.12); }}
.feed-page .feed-dot.active {{ background:#facc15; border-color:rgba(250,204,21,.6); box-shadow:0 0 0 5px rgba(250,204,21,.12); }}
.feed-page .feed-grid {{ display:grid; grid-template-columns: 1.16fr .84fr; gap:20px; align-content:start; min-height:0; }}
.feed-page.layout-headline-list .feed-grid {{ grid-template-columns: 1fr; }}
.feed-page.layout-headline-list .feed-card {{ padding:22px 24px; min-height:unset; }}
.feed-page.layout-compact .feed-grid {{ grid-template-columns: 1fr 1fr; gap:14px; }}
.feed-page.layout-compact .feed-card {{ padding:18px 20px; border-radius:20px; min-height:200px; }}
.feed-page.layout-compact .feed-card h3 {{ font-size:28px; }}
.feed-card {{ background:linear-gradient(180deg, rgba(255,255,255,.12), rgba(255,255,255,.035)); border:1px solid rgba(255,255,255,.14); border-radius:24px; padding:22px; box-shadow:0 18px 38px rgba(0,0,0,.24); overflow:hidden; min-height:220px; display:grid; gap:14px; align-content:start; position:relative; backdrop-filter: blur(18px); }}
.feed-card::before {{ content:''; position:absolute; inset:0; pointer-events:none; background:linear-gradient(180deg, rgba(255,255,255,.05), transparent 22%, transparent 78%, rgba(255,255,255,.03)); }}
.feed-card.lead {{ grid-row: span 2; background:linear-gradient(180deg, rgba(34,197,94,.18), rgba(255,255,255,.05)); border-color:rgba(34,197,94,.28); min-height:456px; }}
.feed-page.layout-headline-list .feed-card.lead,
.feed-page.layout-compact .feed-card.lead {{ grid-row:auto; min-height:unset; }}
.feed-card h3 {{ margin:0; font-size:31px; line-height:1.04; letter-spacing:-.04em; position:relative; z-index:1; }}
.feed-card.lead h3 {{ font-size:44px; }}
.feed-card p {{ margin:0; color:#cbd5e1; font-size:15px; opacity:.9; position:relative; z-index:1; }}
.feed-card .feed-index {{ display:inline-flex; align-items:center; gap:8px; color:#fde68a; font-size:13px; letter-spacing:.16em; text-transform:uppercase; font-weight:900; position:relative; z-index:1; }}
.feed-card .feed-story {{ color:#eff6ff; font-size:21px; line-height:1.34; opacity:.98; display:-webkit-box; -webkit-line-clamp:5; -webkit-box-orient:vertical; overflow:hidden; position:relative; z-index:1; }}
.feed-card.lead .feed-story {{ font-size:25px; -webkit-line-clamp:7; }}
.feed-card .feed-link {{ margin-top:auto; color:#86efac; font-size:13px; text-transform:uppercase; letter-spacing:.14em; font-weight:800; position:relative; z-index:1; }}
.feed-weather {{ display:grid; align-content:space-between; gap:14px; padding:22px; border-radius:26px; background:linear-gradient(180deg, rgba(8,26,44,.76), rgba(14,42,63,.56)); border:1px solid rgba(125,211,252,.18); box-shadow:0 18px 38px rgba(0,0,0,.18); min-height:208px; backdrop-filter: blur(18px); overflow:hidden; position:relative; margin-top:22px; }}
.feed-weather::before {{ content:''; position:absolute; inset:auto -12% -28% auto; width:210px; height:210px; border-radius:50%; background:radial-gradient(circle, rgba(125,211,252,.14), transparent 62%); pointer-events:none; }}
.feed-weather .weather-top {{ display:grid; grid-template-columns:1fr auto; gap:16px; align-items:start; position:relative; z-index:1; }}
.feed-weather .weather-meta {{ display:grid; gap:8px; }}
.feed-weather .weather-icon {{ width:84px; height:84px; border-radius:22px; background:linear-gradient(180deg, rgba(255,255,255,.12), rgba(255,255,255,.04)); border:1px solid rgba(255,255,255,.12); display:grid; place-items:center; position:relative; box-shadow:inset 0 1px 0 rgba(255,255,255,.06); }}
.feed-weather .weather-icon.sun::before {{ content:''; width:34px; height:34px; border-radius:50%; background:radial-gradient(circle, #fde68a, #f59e0b 72%); box-shadow:0 0 0 10px rgba(250,204,21,.12), 0 0 24px rgba(250,204,21,.32); }}
.feed-weather .weather-icon.cloud::before {{ content:''; position:absolute; width:42px; height:22px; border-radius:999px; background:#dbeafe; bottom:26px; box-shadow:-16px -8px 0 2px #dbeafe, 14px -10px 0 0 #dbeafe; }}
.feed-weather .weather-icon.rain::before {{ content:''; position:absolute; width:42px; height:22px; border-radius:999px; background:#dbeafe; top:22px; box-shadow:-16px -8px 0 2px #dbeafe, 14px -10px 0 0 #dbeafe; }}
.feed-weather .weather-icon.rain::after {{ content:''; position:absolute; width:34px; height:28px; bottom:14px; left:25px; background:
linear-gradient(180deg, rgba(56,189,248,.9), rgba(56,189,248,0) 75%);
clip-path: polygon(8% 0, 22% 0, 16% 100%, 0 100%, 8% 0, 40% 0, 54% 0, 48% 100%, 32% 100%, 40% 0, 72% 0, 86% 0, 80% 100%, 64% 100%, 72% 0); }}
.feed-weather .weather-icon.partly::before {{ content:''; position:absolute; width:30px; height:30px; border-radius:50%; background:radial-gradient(circle, #fde68a, #f59e0b 72%); top:18px; left:16px; box-shadow:0 0 0 8px rgba(250,204,21,.12); }}
.feed-weather .weather-icon.partly::after {{ content:''; position:absolute; width:40px; height:20px; border-radius:999px; background:#e2e8f0; bottom:24px; right:16px; box-shadow:-16px -8px 0 2px #e2e8f0, 12px -10px 0 0 #e2e8f0; }}
.feed-weather .temp {{ font-size:70px; font-weight:900; line-height:.9; letter-spacing:-.06em; }}
.feed-weather .city {{ font-size:28px; font-weight:900; letter-spacing:-.03em; }}
.feed-weather .cond, .feed-weather .details {{ color:#dbeafe; font-size:16px; line-height:1.35; }}
.feed-weather .weather-footer {{ display:flex; align-items:flex-end; justify-content:space-between; gap:16px; position:relative; z-index:1; }}
.feed-weather .time {{ font-size:24px; font-weight:900; color:#f8fafc; }}
.feed-weather .weather-label {{ font-size:12px; letter-spacing:.18em; text-transform:uppercase; font-weight:800; color:#bfdbfe; }}
body.portrait #badge {{ font-size:18px; top:16px; right:16px; }}
body.portrait #newsTicker {{ font-size:28px; padding:18px 24px; left:14px; right:14px; bottom:2px; }}
body.portrait .feed-page {{ padding:28px 22px 18px; gap:16px; grid-template-rows:auto auto minmax(0, 1fr); }}
body.feed-active.portrait .feed-page {{ padding-top:28px; }}
body.portrait .feed-weather {{ margin-top:16px; }}
body.portrait .feed-page .feed-hero {{ grid-template-columns:1fr; gap:18px; }}
body.portrait .feed-page h1 {{ font-size:58px; max-width:none; }}
body.portrait .feed-page .feed-sub {{ font-size:25px; max-width:none; }}
body.portrait .feed-page .feed-header {{ grid-template-columns:1fr; gap:16px; }}
body.portrait .feed-page .feed-grid {{ grid-template-columns: 1fr; gap:16px; }}
body.portrait .feed-card {{ min-height:unset; }}
body.portrait .feed-card.lead {{ min-height:unset; grid-row:auto; }}
body.portrait .feed-card h3 {{ font-size:34px; }}
body.portrait .feed-card.lead h3 {{ font-size:40px; }}
body.portrait .feed-card .feed-story {{ font-size:22px; -webkit-line-clamp:6; }}
body.portrait .feed-card.lead .feed-story {{ font-size:24px; -webkit-line-clamp:7; }}
body.portrait .feed-weather .temp {{ font-size:78px; }}
body.portrait .feed-weather .city {{ font-size:32px; }}
body.portrait .feed-weather .cond, body.portrait .feed-weather .details {{ font-size:18px; }}
body.portrait .feed-weather .time {{ font-size:28px; }}
body.portrait .layer img, body.portrait .layer video, body.portrait .layer iframe {{ object-fit: contain; }}
body.portrait-simulated #stage {{ transform: rotate(90deg) scale(calc(100vh / 100vw)); transform-origin:center center; }}
body.portrait-simulated .layer img, body.portrait-simulated .layer video, body.portrait-simulated .layer iframe {{ object-fit: contain; }}
@keyframes tickerMove {{ 0% {{ transform: translateX(0); }} 100% {{ transform: translateX(-100%); }} }}</style>
</head>
<body>
  <div id="stage"><div id="layerA" class="layer active"></div><div id="layerB" class="layer"></div><div id="badge">{BRAND['name']} | {screen_name}</div><div id="newsTicker"><div id="newsTickerInner"></div></div></div>
  <script>
    const screenName = {json.dumps(screen_name)}; const screenId = {json.dumps(screen_id)}; const bootstrapScreenToken = {json.dumps(bootstrap_screen_token)}; if (bootstrapScreenToken) {{ localStorage.setItem('salubcast_screen_token', bootstrapScreenToken); const cleanUrl = new URL(window.location.href); cleanUrl.searchParams.delete('token'); window.history.replaceState({{}}, '', cleanUrl.toString()); }} const screenToken = localStorage.getItem('salubcast_screen_token') || ''; let items = []; let currentIndex = 0; let screenOrientation = 'landscape'; let timeoutHandle = null; let currentSignature = ''; let activeLayer = 'A'; let companyBlocked = false; let badgeVisible = true; let badgePosition = 'top-right'; let imageFit = 'contain'; let portraitImageFit = 'contain'; let feedLayout = 'cards'; const layerA = document.getElementById('layerA'); const layerB = document.getElementById('layerB'); const feedThemes = ['theme-sunrise', 'theme-aqua', 'theme-coral']; function currentLayer() {{ return activeLayer === 'A' ? layerA : layerB; }} function nextLayer() {{ return activeLayer === 'A' ? layerB : layerA; }} function currentFit() {{ return screenOrientation === 'portrait' ? portraitImageFit : imageFit; }} function weatherIconClass(condition) {{ const text = String(condition || '').toLowerCase(); if (text.includes('regen') || text.includes('bui') || text.includes('rain') || text.includes('drizzle')) return 'rain'; if (text.includes('bewolkt') || text.includes('cloud') || text.includes('mist') || text.includes('fog')) return 'cloud'; if (text.includes('half') || text.includes('partly') || text.includes('wisselend')) return 'partly'; if (text.includes('zon') || text.includes('helder') || text.includes('clear') || text.includes('sun')) return 'sun'; return 'partly'; }} function applyScreenChrome() {{ document.body.classList.remove('badge-top-right','badge-top-left','badge-bottom-right','badge-bottom-left','badge-hidden'); document.body.classList.add(`badge-${{badgePosition}}`); if (!badgeVisible) document.body.classList.add('badge-hidden'); }} function clearTimers() {{ if (timeoutHandle) {{ clearTimeout(timeoutHandle); timeoutHandle = null; }} }} async function heartbeat() {{ try {{ await fetch('/api/player/heartbeat', {{ method:'POST', headers:{{'Content-Type':'application/json','X-Screen-ID':screenId,'X-Screen-Token':screenToken}}, body: JSON.stringify({{screen:screenName, screen_id:screenId, token:screenToken}}) }}); }} catch (e) {{}} }} function playlistSignature(nextItems) {{ return JSON.stringify((nextItems || []).map(i => [i.title, i.url, i.mimetype, i.duration_seconds, i.sort_order, i.feed_entries ? i.feed_entries.length : 0, i.feed_page_index || 1, i.feed_page_count || 1, i.weather ? (i.weather.city || '') : '', i.weather ? (i.weather.temperature || '') : '', i.weather ? (i.weather.feels_like || '') : '', i.weather ? (i.weather.wind || '') : '', i.weather ? (i.weather.condition || '') : ''])); }} async function loadPlaylist() {{ try {{ const res = await fetch(`/api/player/playlist?screen=${{encodeURIComponent(screenName)}}&screen_id=${{encodeURIComponent(screenId)}}&_=${{Date.now()}}`, {{ headers: {{ 'X-Screen-ID': screenId, 'X-Screen-Token': screenToken }} }}); if (res.status === 401) {{ renderUnauthorized(); return; }} const data = await res.json(); companyBlocked = !!data.blocked; screenOrientation = (data.screen && data.screen.orientation) || screenOrientation || 'landscape'; badgeVisible = ((data.screen && data.screen.badge_visible) ?? 1) == 1; badgePosition = (data.screen && data.screen.badge_position) || 'top-right'; imageFit = (data.screen && data.screen.image_fit) || 'contain'; portraitImageFit = (data.screen && data.screen.portrait_image_fit) || 'contain'; feedLayout = (data.screen && data.screen.feed_layout) || 'cards'; document.body.classList.toggle('portrait', screenOrientation === 'portrait'); document.body.classList.toggle('portrait-simulated', screenOrientation === 'portrait' && window.innerWidth > window.innerHeight); applyScreenChrome(); const nextItems = data.items || []; const nextSignature = JSON.stringify([companyBlocked, playlistSignature(nextItems)]); if (nextSignature !== currentSignature) {{ currentSignature = nextSignature; items = nextItems; currentIndex = 0; playCurrent(); }} }} catch (e) {{ console.error(e); }} }} function scheduleNext(delayMs = 1000) {{ clearTimers(); timeoutHandle = setTimeout(() => {{ if (!items.length) {{ playCurrent(); return; }} currentIndex = (currentIndex + 1) % items.length; playCurrent(); }}, Math.max(1000, delayMs)); }} function swapLayers() {{ const fromLayer = currentLayer(); const toLayer = nextLayer(); toLayer.classList.add('active'); fromLayer.classList.remove('active'); setTimeout(() => {{ if (!fromLayer.classList.contains('active')) {{ fromLayer.innerHTML = ''; }} }}, 1000); activeLayer = activeLayer === 'A' ? 'B' : 'A'; }} function renderEmpty() {{ document.body.classList.remove('feed-active'); const message = companyBlocked ? 'Scherm geblokkeerd' : 'Geen actieve content'; nextLayer().innerHTML = '<div id="empty">' + message + '</div>'; swapLayers(); scheduleNext(8000); }} function renderUnauthorized() {{ document.body.classList.remove('feed-active'); nextLayer().innerHTML = '<div id="empty">Player niet geautoriseerd. Genereer opnieuw een player installer of activeer opnieuw.</div>'; swapLayers(); scheduleNext(8000); }} function renderFeedPage(item, target) {{ document.body.classList.add('feed-active'); const wrapper = document.createElement('div'); const pageIndex = item.feed_page_index || 1; const pageCount = item.feed_page_count || 1; const entries = item.feed_entries || []; wrapper.className = 'feed-page layout-' + (feedLayout || 'cards') + ' ' + feedThemes[(pageIndex - 1) % feedThemes.length]; const hero = document.createElement('div'); hero.className = 'feed-hero'; const titleWrap = document.createElement('div'); titleWrap.className = 'feed-title-block'; const pager = document.createElement('div'); pager.className = 'feed-pager'; const pagerLabel = document.createElement('div'); pagerLabel.className = 'feed-page-label'; pagerLabel.textContent = 'Pagina ' + pageIndex + ' van ' + pageCount; const dots = document.createElement('div'); dots.className = 'feed-dots'; for (let i = 1; i <= pageCount; i += 1) {{ const dot = document.createElement('div'); dot.className = 'feed-dot' + (i === pageIndex ? ' active' : ''); dots.appendChild(dot); }} pager.appendChild(pagerLabel); pager.appendChild(dots); titleWrap.appendChild(pager); hero.appendChild(titleWrap); const weather = item.weather || null; const weatherBox = document.createElement('div'); weatherBox.className = 'feed-weather'; const weatherTop = document.createElement('div'); weatherTop.className = 'weather-top'; const weatherMeta = document.createElement('div'); weatherMeta.className = 'weather-meta'; const weatherIcon = document.createElement('div'); weatherIcon.className = 'weather-icon'; const timeValue = document.createElement('div'); timeValue.className = 'time'; timeValue.textContent = new Date().toLocaleTimeString([], {{hour:'2-digit', minute:'2-digit'}}); const footer = document.createElement('div'); footer.className = 'weather-footer'; const label = document.createElement('div'); label.className = 'weather-label'; label.textContent = 'Actueel weer'; if (weather) {{ weatherIcon.classList.add(weatherIconClass(weather.condition)); const temp = document.createElement('div'); temp.className = 'temp'; temp.textContent = weather.temperature || '--'; const city = document.createElement('div'); city.className = 'city'; city.textContent = weather.city || 'Weer'; const cond = document.createElement('div'); cond.className = 'cond'; cond.textContent = weather.condition || ''; const details = document.createElement('div'); details.className = 'details'; details.textContent = 'Gevoel ' + (weather.feels_like || '--') + ' | Wind ' + (weather.wind || '--'); weatherMeta.appendChild(temp); weatherMeta.appendChild(city); if (cond.textContent) weatherMeta.appendChild(cond); weatherMeta.appendChild(details); }} else {{ weatherIcon.classList.add('partly'); const city = document.createElement('div'); city.className = 'city'; city.textContent = 'Nieuwsupdate'; const details = document.createElement('div'); details.className = 'details'; details.textContent = 'Laatste verhalen in beeld'; weatherMeta.appendChild(city); weatherMeta.appendChild(details); label.textContent = 'Live update'; }} weatherTop.appendChild(weatherMeta); weatherTop.appendChild(weatherIcon); footer.appendChild(label); footer.appendChild(timeValue); weatherBox.appendChild(weatherTop); weatherBox.appendChild(footer); hero.appendChild(weatherBox); wrapper.appendChild(hero); const grid = document.createElement('div'); grid.className = 'feed-grid'; entries.forEach((entry, index) => {{ const card = document.createElement('div'); card.className = 'feed-card' + (index === 0 ? ' lead' : ''); const label = document.createElement('div'); label.className = 'feed-index'; label.textContent = index === 0 ? 'Hoofdverhaal' : ('Verhaal ' + (index + 1)); const h = document.createElement('h3'); h.textContent = entry.title || ('Nieuwsbericht ' + (index + 1)); const story = document.createElement('div'); story.className = 'feed-story'; story.textContent = entry.summary || 'Geen samenvatting beschikbaar.'; const meta = document.createElement('p'); meta.textContent = entry.published_at || ''; const link = document.createElement('div'); link.className = 'feed-link'; link.textContent = entry.link ? 'Bron beschikbaar' : ''; card.appendChild(label); card.appendChild(h); card.appendChild(story); if (meta.textContent) card.appendChild(meta); if (link.textContent) card.appendChild(link); grid.appendChild(card); }}); wrapper.appendChild(grid); target.appendChild(wrapper); swapLayers(); scheduleNext(Math.max((item.duration_seconds || 12) * 1000, 15000)); }}

function playCurrent() {{ if (!items.length) {{ renderEmpty(); return; }} const item = items[currentIndex]; const target = nextLayer(); target.innerHTML = ''; if ((item.mimetype || '') === 'application/x-feed-page') {{ renderFeedPage(item, target); return; }} document.body.classList.remove('feed-active'); if ((item.mimetype || '').startsWith('image/')) {{ const img = document.createElement('img'); img.style.objectFit = currentFit(); img.src = item.url + (item.url.includes('?') ? '&' : '?') + 'v=' + Date.now(); img.onload = () => {{ swapLayers(); scheduleNext((item.duration_seconds || 10) * 1000); }}; img.onerror = () => scheduleNext(4000); target.appendChild(img); return; }} if ((item.mimetype || '').startsWith('video/')) {{ const video = document.createElement('video'); video.style.objectFit = currentFit(); video.src = item.url + (item.url.includes('?') ? '&' : '?') + 'v=' + Date.now(); video.autoplay = true; video.muted = true; video.playsInline = true; video.oncanplay = () => swapLayers(); video.onended = () => scheduleNext(50); video.onerror = () => scheduleNext(4000); target.appendChild(video); return; }} if ((item.mimetype || '') === 'application/pdf') {{ const frame = document.createElement('iframe'); frame.style.objectFit = currentFit(); frame.src = item.url + (item.url.includes('?') ? '&' : '?') + 'v=' + Date.now(); target.appendChild(frame); swapLayers(); scheduleNext((item.duration_seconds || 12) * 1000); return; }} target.innerHTML = '<div id="empty">Onbekend bestandstype</div>'; swapLayers(); scheduleNext(5000); }} async function loadTicker() {{ try {{ const res = await fetch('/api/feed-ticker?screen=' + encodeURIComponent(screenName) + '&screen_id=' + encodeURIComponent(screenId), {{ headers: {{ 'X-Screen-ID': screenId, 'X-Screen-Token': screenToken }} }}); const data = await res.json(); const text = data.text || ''; const ticker = document.getElementById('newsTicker'); const inner = document.getElementById('newsTickerInner'); if (!text) {{ ticker.style.display = 'none'; return; }} ticker.style.display = 'block'; inner.textContent = text; }} catch (e) {{ document.getElementById('newsTicker').style.display = 'none'; }} }} loadPlaylist().then(() => playCurrent()); loadTicker(); heartbeat(); setInterval(heartbeat, 30000); setInterval(loadPlaylist, 30000); setInterval(loadTicker, 300000);
  </script>
</body>
</html>
    """
    return html

@app.route('/uploads/<path:filename>')
def uploaded_file(filename: str) -> Response:
    return send_from_directory(UPLOAD_DIR, filename)
