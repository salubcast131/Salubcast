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


def player_screen_payload(screen: sqlite3.Row) -> dict[str, Any]:
    return {
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
    }


def player_change_token(screen: sqlite3.Row) -> str:
    company_id = screen['company_id']
    schedules = fetch_all(
        "SELECT id, playlist_id, start_time, end_time, days_json, priority, active FROM schedules WHERE screen_id = ? AND company_id = ? ORDER BY id",
        (screen['id'], company_id),
    )
    playlist_ids = sorted({row['playlist_id'] for row in schedules})
    playlist_items: list[tuple[Any, ...]] = []
    if playlist_ids:
        placeholders = ','.join('?' for _ in playlist_ids)
        playlist_items = [
            tuple(row)
            for row in fetch_all(
                f"SELECT pi.playlist_id, pi.media_id, pi.sort_order, m.title, m.filename, m.mimetype, m.duration_seconds, m.uploaded_at FROM playlist_items pi JOIN media m ON m.id = pi.media_id WHERE pi.playlist_id IN ({placeholders}) ORDER BY pi.playlist_id, pi.sort_order, pi.id",
                tuple(playlist_ids),
            )
        ]
    feed_state = fetch_all(
        "SELECT f.id, f.name, f.url, f.max_items, f.refresh_seconds, f.is_active, f.is_ticker, f.last_fetched_at, COUNT(fi.id) AS item_count, MAX(fi.created_at) AS newest_item FROM feeds f LEFT JOIN feed_items fi ON fi.feed_id = f.id WHERE f.company_id = ? GROUP BY f.id ORDER BY f.id",
        (company_id,),
    )
    company = fetch_one("SELECT id, is_active, logo_filename, plan_name, billing_status FROM companies WHERE id = ? LIMIT 1", (company_id,))
    payload = {
        'company': dict(company) if company else None,
        'screen': player_screen_payload(screen),
        'schedules': [tuple(row) for row in schedules],
        'playlist_items': playlist_items,
        'feeds': [tuple(row) for row in feed_state],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode('utf-8')).hexdigest()


@app.route("/api/player/status")
def player_status() -> Response:
    screen_id = request.headers.get('X-Screen-ID') or request.args.get('screen_id', '')
    screen_name = request.args.get('screen', '')
    screen_token = request.headers.get('X-Screen-Token') or request.args.get('token', '')
    screen = resolve_screen_by_auth(screen_name=screen_name, screen_id=screen_id, screen_token=screen_token)
    if not screen:
        return jsonify({'error': 'Unauthorized player'}), 401
    return jsonify({'revision': player_change_token(screen), 'timestamp': now_iso()})

def resolve_active_playlist_for_screen(screen: sqlite3.Row) -> dict[str, Any] | None:
    company = fetch_one("SELECT * FROM companies WHERE id = ? LIMIT 1", (screen["company_id"],))
    if not company or company["is_active"] != 1:
        return {'screen': player_screen_payload(screen), 'blocked': True, 'items': []}
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
        weather_summary = weather_for_screen(screen)
        if feed_entries:
            feed_pages = chunk_feed_entries(feed_entries, per_page, orientation)
            duration = max(8, int(screen['feed_page_duration'] or 12))

            def build_pages(base_sort_order: float) -> list[dict[str, Any]]:
                pages = [
                    {
                        'title': f'Nieuws | {feed_name or "Updates"}',
                        'url': '',
                        'mimetype': 'application/x-feed-page',
                        'duration_seconds': duration,
                        'sort_order': base_sort_order + (page_idx / 100),
                        'feed_name': feed_name or 'Nieuws',
                        'feed_entries': page_entries,
                        'feed_page_index': page_idx,
                        'feed_page_count': len(feed_pages),
                    }
                    for page_idx, page_entries in enumerate(feed_pages, 1)
                ]
                if weather_summary:
                    pages.append({
                        'title': 'Weer',
                        'url': '',
                        'mimetype': 'application/x-weather-page',
                        'duration_seconds': duration,
                        'sort_order': base_sort_order + ((len(feed_pages) + 1) / 100),
                        'weather': weather_summary,
                    })
                return pages

            augmented = []
            every = max(1, int(screen['feed_page_every'] or 3))
            inserted_feed_page = False
            for idx, item in enumerate(items, 1):
                augmented.append(item)
                if idx % every == 0:
                    augmented.extend(build_pages(100000 + idx))
                    inserted_feed_page = True
            if items and not inserted_feed_page:
                augmented.extend(build_pages(199999))
            if not items:
                augmented.extend(build_pages(100000))
            items = augmented

    return {
        'screen': player_screen_payload(screen),
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
        result = {'screen': player_screen_payload(screen), 'items': []}
    result['revision'] = player_change_token(screen)
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
  <style>html, body {{ margin:0; width:100%; height:100%; overflow:hidden; background:#02060c; color:white; font-family:"Segoe UI Variable Display","Segoe UI","Trebuchet MS",sans-serif; }} #stage {{ position:fixed; inset:0; background:#000; overflow:hidden; }} .layer {{ position:absolute; inset:0; opacity:0; transition: opacity 900ms ease-in-out; display:flex; align-items:center; justify-content:center; background:#000; }} .layer.active {{ opacity:1; }} .layer img, .layer video, .layer iframe {{ width:100%; height:100%; object-fit:contain; border:0; background:#000; }} #empty {{ color:#94a3b8; font-size:40px; text-align:center; max-width:20ch; line-height:1.2; }} #badge {{ position:fixed; top:18px; right:18px; background:linear-gradient(180deg, rgba(8,18,30,.48), rgba(8,18,30,.3)); border:1px solid rgba(255,255,255,.22); padding:10px 16px; border-radius:999px; font-size:13px; letter-spacing:.08em; text-transform:uppercase; font-weight:800; z-index:10; backdrop-filter: blur(22px) saturate(160%); -webkit-backdrop-filter: blur(22px) saturate(160%); }} body.badge-top-left #badge {{ top:18px; left:18px; right:auto; bottom:auto; }} body.badge-bottom-right #badge {{ bottom:18px; right:18px; top:auto; left:auto; }} body.badge-bottom-left #badge {{ bottom:18px; left:18px; top:auto; right:auto; }} body.badge-hidden #badge {{ display:none; }} body.feed-active #badge {{ display:none; }} body.feed-active #newsTicker {{ display:none !important; }} body.weather-active #badge {{ display:none; }} #newsTicker {{ position:fixed; left:24px; right:24px; bottom:18px; z-index:20; background:linear-gradient(90deg, rgba(8,18,30,.42), rgba(8,18,30,.26)); border:1px solid rgba(255,255,255,.24); color:white; padding:14px 22px; font-size:23px; border-radius:999px; white-space:nowrap; overflow:hidden; display:none; box-shadow:0 18px 48px rgba(0,0,0,.18); backdrop-filter: blur(26px) saturate(170%); -webkit-backdrop-filter: blur(26px) saturate(170%); }} #newsTickerInner {{ display:inline-block; padding-left:100%; animation:tickerMove 60s linear infinite; font-weight:750; letter-spacing:0; text-shadow:0 1px 12px rgba(0,0,0,.34); }} .feed-page {{ width:100%; height:100%; display:grid; grid-template-rows:auto minmax(0, auto); gap:22px; padding:clamp(28px,4vw,56px); align-content:center; justify-content:center; position:relative; overflow:hidden; background:
linear-gradient(135deg, rgba(34,197,94,.18), transparent 30%),
radial-gradient(circle at top right, rgba(56,189,248,.2), transparent 26%),
linear-gradient(180deg, #060c14, #0a1623 44%, #0e1d30 100%); }}
body.feed-active .feed-page {{ padding-top:clamp(28px,4vw,56px); }}
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
.feed-page .feed-hero {{ display:grid; grid-template-columns:minmax(0, 1.4fr) auto; align-items:center; gap:26px; padding-bottom:18px; border-bottom:1px solid rgba(255,255,255,.1); width:min(100%, 1500px); justify-self:center; }}
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
.feed-page .feed-grid {{ display:grid; grid-template-columns: 1.16fr .84fr; gap:20px; align-content:center; min-height:0; width:min(100%, 1500px); justify-self:center; }}
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
.feed-card .feed-story {{ color:#eff6ff; font-size:19px; line-height:1.32; opacity:.98; display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden; position:relative; z-index:1; min-height:0; }}
.feed-card.lead .feed-story {{ font-size:25px; -webkit-line-clamp:7; }}
.feed-card:not(.lead) h3 {{ display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }}
.feed-card-media {{ width:100%; height:112px; border-radius:18px; overflow:hidden; position:relative; z-index:1; background:rgba(255,255,255,.06); }}
.feed-card-media img {{ width:100%; height:100%; object-fit:cover; display:block; }}
.feed-card.lead .feed-card-media {{ height:280px; }}
.feed-page.layout-compact .feed-card-media {{ height:130px; }}
.feed-page.layout-headline-list .feed-card-media {{ height:150px; }}
.weather-page {{ width:100%; height:100%; position:relative; overflow:hidden; padding:clamp(28px,4vw,64px); display:grid; align-items:center; background:
linear-gradient(135deg, rgba(56,189,248,.18), transparent 34%),
radial-gradient(circle at top right, rgba(125,211,252,.18), transparent 32%),
linear-gradient(180deg, #041019, #092233 48%, #0e3a45 100%); }}
.weather-page.cond-sun {{ background:
linear-gradient(135deg, rgba(253,224,71,.24), transparent 36%),
radial-gradient(circle at top right, rgba(249,115,22,.2), transparent 32%),
linear-gradient(180deg, #0b1a2b, #163049 48%, #1d3b52 100%); }}
.weather-page.cond-rain {{ background:
linear-gradient(135deg, rgba(56,189,248,.16), transparent 34%),
radial-gradient(circle at top right, rgba(30,64,175,.24), transparent 32%),
linear-gradient(180deg, #02070d, #050f1c 48%, #081627 100%); }}
.weather-page::before {{ content:''; position:absolute; inset:0; pointer-events:none; background:radial-gradient(circle at 18% 10%, rgba(255,255,255,.08), transparent 26%); z-index:1; }}
.weather-scene {{ position:absolute; inset:0; overflow:hidden; pointer-events:none; z-index:0; }}
.weather-sun-glow {{ position:absolute; top:6%; right:8%; width:min(34vw, 340px); height:min(34vw, 340px); border-radius:50%; background:radial-gradient(circle, rgba(253,224,71,.85), rgba(245,158,11,.35) 46%, transparent 72%); filter:blur(4px); animation: weatherSunPulse 5s ease-in-out infinite; }}
@keyframes weatherSunPulse {{ 0%, 100% {{ transform:scale(1); opacity:.85; }} 50% {{ transform:scale(1.08); opacity:1; }} }}
.weather-cloud {{ position:absolute; opacity:.6; filter:blur(.5px); background:
radial-gradient(circle at 26% 66%, rgba(255,255,255,.95) 42%, transparent 44%),
radial-gradient(circle at 46% 72%, rgba(255,255,255,.95) 46%, transparent 48%),
radial-gradient(circle at 60% 30%, rgba(255,255,255,.95) 50%, transparent 52%),
radial-gradient(circle at 82% 64%, rgba(255,255,255,.95) 36%, transparent 38%); animation-name: weatherDrift; animation-timing-function:linear; animation-iteration-count:infinite; }}
@keyframes weatherDrift {{ from {{ transform:translateX(-26vw); }} to {{ transform:translateX(126vw); }} }}
.weather-rain-drop {{ position:absolute; top:-8vh; width:2px; height:64px; background:linear-gradient(180deg, transparent, rgba(191,219,254,.75)); animation-name: weatherRainFall; animation-timing-function:linear; animation-iteration-count:infinite; }}
@keyframes weatherRainFall {{ 0% {{ transform:translateY(0); opacity:0; }} 10% {{ opacity:.9; }} 100% {{ transform:translateY(118vh); opacity:.2; }} }}
.weather-content {{ position:relative; z-index:2; width:min(100%, 1440px); margin:0 auto; display:grid; grid-template-columns:1.05fr .95fr; gap:clamp(24px,4vw,64px); align-items:center; }}
.weather-primary {{ display:grid; gap:16px; animation: weatherFadeUp .7s ease both; }}
.weather-page-kicker {{ font-size:16px; letter-spacing:.34em; text-transform:uppercase; color:#bae6fd; font-weight:900; }}
.weather-icon-big {{ width:180px; height:180px; border-radius:44px; background:linear-gradient(180deg, rgba(255,255,255,.16), rgba(255,255,255,.05)); border:1px solid rgba(255,255,255,.18); display:grid; place-items:center; position:relative; box-shadow:0 30px 70px rgba(0,0,0,.34), inset 0 1px 0 rgba(255,255,255,.1); animation: weatherIconFloat 5s ease-in-out infinite; }}
@keyframes weatherIconFloat {{ 0%, 100% {{ transform:translateY(0); }} 50% {{ transform:translateY(-14px); }} }}
.weather-icon-big.sun::before {{ content:''; width:76px; height:76px; border-radius:50%; background:radial-gradient(circle, #fde68a, #f59e0b 72%); box-shadow:0 0 0 22px rgba(250,204,21,.12), 0 0 60px rgba(250,204,21,.35); animation: weatherSunPulse 4s ease-in-out infinite; }}
.weather-icon-big.cloud::before {{ content:''; position:absolute; width:96px; height:50px; border-radius:999px; background:#dbeafe; bottom:56px; box-shadow:-36px -18px 0 4px #dbeafe, 32px -22px 0 0 #dbeafe; }}
.weather-icon-big.rain::before {{ content:''; position:absolute; width:96px; height:50px; border-radius:999px; background:#dbeafe; top:44px; box-shadow:-36px -18px 0 4px #dbeafe, 32px -22px 0 0 #dbeafe; }}
.weather-icon-big.rain::after {{ content:''; position:absolute; width:78px; height:64px; bottom:28px; left:56px; background:
linear-gradient(180deg, rgba(56,189,248,.9), rgba(56,189,248,0) 75%);
clip-path: polygon(8% 0, 22% 0, 16% 100%, 0 100%, 8% 0, 40% 0, 54% 0, 48% 100%, 32% 100%, 40% 0, 72% 0, 86% 0, 80% 100%, 64% 100%, 72% 0); }}
.weather-icon-big.partly::before {{ content:''; position:absolute; width:68px; height:68px; border-radius:50%; background:radial-gradient(circle, #fde68a, #f59e0b 72%); top:40px; left:34px; box-shadow:0 0 0 18px rgba(250,204,21,.12); }}
.weather-icon-big.partly::after {{ content:''; position:absolute; width:90px; height:44px; border-radius:999px; background:#e2e8f0; bottom:50px; right:32px; box-shadow:-34px -18px 0 4px #e2e8f0, 26px -20px 0 0 #e2e8f0; }}
.weather-temp-row {{ display:flex; align-items:flex-start; gap:6px; }}
.weather-temp-big {{ font-size:clamp(110px, 13vw, 200px); font-weight:900; line-height:.84; letter-spacing:-.06em; font-variant-numeric:tabular-nums; }}
.weather-city-big {{ font-size:clamp(30px, 3.2vw, 46px); font-weight:900; letter-spacing:-.03em; }}
.weather-cond-big {{ font-size:clamp(19px, 1.8vw, 26px); color:#dbeafe; }}
.weather-stats-grid {{ position:relative; z-index:2; display:grid; grid-template-columns:repeat(3, 1fr); gap:16px; animation: weatherFadeUp .9s .15s ease both; }}
.weather-stat-card {{ background:rgba(255,255,255,.07); border:1px solid rgba(255,255,255,.14); border-radius:20px; padding:18px; backdrop-filter: blur(16px); display:grid; gap:6px; box-shadow:0 14px 30px rgba(0,0,0,.16); }}
.weather-stat-card .weather-stat-label {{ font-size:11.5px; letter-spacing:.14em; text-transform:uppercase; color:#93c5fd; font-weight:800; }}
.weather-stat-card .weather-stat-value {{ font-size:24px; font-weight:900; }}
.weather-clock {{ position:absolute; top:clamp(24px,3vw,44px); right:clamp(24px,3vw,52px); text-align:right; z-index:2; animation: weatherFadeUp .6s ease both; }}
.weather-clock-time {{ font-size:clamp(28px,2.6vw,40px); font-weight:900; font-variant-numeric:tabular-nums; }}
.weather-clock-date {{ font-size:14px; color:#bfdbfe; text-transform:capitalize; }}
@keyframes weatherFadeUp {{ from {{ opacity:0; transform:translateY(18px); }} to {{ opacity:1; transform:none; }} }}
body.portrait .weather-content {{ grid-template-columns:1fr; text-align:center; justify-items:center; gap:28px; }}
body.portrait .weather-primary {{ justify-items:center; }}
body.portrait .weather-stats-grid {{ grid-template-columns:repeat(2, 1fr); width:100%; }}
body.portrait .weather-clock {{ position:static; text-align:center; margin-bottom:8px; }}
body.portrait .weather-temp-big {{ font-size:clamp(90px, 20vw, 150px); }}
body.portrait #badge {{ font-size:18px; top:16px; right:16px; }}
body.portrait #newsTicker {{ font-size:27px; padding:16px 24px; left:16px; right:16px; bottom:12px; }}
body.portrait .feed-page {{ padding:26px 22px; gap:16px; grid-template-rows:auto minmax(0, auto); align-content:center; }}
body.feed-active.portrait .feed-page {{ padding-top:26px; }}
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
body.portrait .layer img, body.portrait .layer video, body.portrait .layer iframe {{ object-fit: contain; }}
body.portrait-simulated #stage {{ transform: rotate(90deg) scale(calc(100vh / 100vw)); transform-origin:center center; }}
body.portrait-simulated .layer img, body.portrait-simulated .layer video, body.portrait-simulated .layer iframe {{ object-fit: contain; }}
@keyframes tickerMove {{ 0% {{ transform: translateX(0); }} 100% {{ transform: translateX(-100%); }} }}</style>
</head>
<body>
  <div id="stage"><div id="layerA" class="layer active"></div><div id="layerB" class="layer"></div><div id="badge">{BRAND['name']} | {screen_name}</div><div id="newsTicker"><div id="newsTickerInner"></div></div></div>
  <script>
    const screenName = {json.dumps(screen_name)}; const screenId = {json.dumps(screen_id)}; const bootstrapScreenToken = {json.dumps(bootstrap_screen_token)}; if (bootstrapScreenToken) {{ localStorage.setItem('salubcast_screen_token', bootstrapScreenToken); const cleanUrl = new URL(window.location.href); cleanUrl.searchParams.delete('token'); window.history.replaceState({{}}, '', cleanUrl.toString()); }} const screenToken = localStorage.getItem('salubcast_screen_token') || ''; let items = []; let currentIndex = 0; let screenOrientation = 'landscape'; let timeoutHandle = null; let currentSignature = ''; let playerRevision = ''; let refreshInFlight = false; let activeLayer = 'A'; let companyBlocked = false; let badgeVisible = true; let badgePosition = 'top-right'; let imageFit = 'contain'; let portraitImageFit = 'contain'; let feedLayout = 'cards'; let tickerText = ''; let weatherClockTimer = null; const layerA = document.getElementById('layerA'); const layerB = document.getElementById('layerB'); const feedThemes = ['theme-sunrise', 'theme-aqua', 'theme-coral']; function currentLayer() {{ return activeLayer === 'A' ? layerA : layerB; }} function nextLayer() {{ return activeLayer === 'A' ? layerB : layerA; }} function currentFit() {{ return screenOrientation === 'portrait' ? portraitImageFit : imageFit; }} function weatherIconClass(condition) {{ const text = String(condition || '').toLowerCase(); if (text.includes('regen') || text.includes('bui') || text.includes('rain') || text.includes('drizzle')) return 'rain'; if (text.includes('bewolkt') || text.includes('cloud') || text.includes('mist') || text.includes('fog')) return 'cloud'; if (text.includes('half') || text.includes('partly') || text.includes('wisselend')) return 'partly'; if (text.includes('zon') || text.includes('helder') || text.includes('clear') || text.includes('sun')) return 'sun'; return 'partly'; }} function applyScreenChrome() {{ document.body.classList.remove('badge-top-right','badge-top-left','badge-bottom-right','badge-bottom-left','badge-hidden'); document.body.classList.add(`badge-${{badgePosition}}`); if (!badgeVisible) document.body.classList.add('badge-hidden'); }} function applyTickerVisibility() {{ const ticker = document.getElementById('newsTicker'); if (!ticker) return; ticker.style.display = tickerText && !document.body.classList.contains('feed-active') && !document.body.classList.contains('weather-active') ? 'block' : 'none'; }} function clearTimers() {{ if (timeoutHandle) {{ clearTimeout(timeoutHandle); timeoutHandle = null; }} }} async function heartbeat() {{ try {{ await fetch('/api/player/heartbeat', {{ method:'POST', headers:{{'Content-Type':'application/json','X-Screen-ID':screenId,'X-Screen-Token':screenToken}}, body: JSON.stringify({{screen:screenName, screen_id:screenId, token:screenToken}}) }}); }} catch (e) {{}} }} function playlistSignature(nextItems) {{ return JSON.stringify((nextItems || []).map(i => [i.title, i.url, i.mimetype, i.duration_seconds, i.sort_order, i.feed_entries ? i.feed_entries.length : 0, i.feed_page_index || 1, i.feed_page_count || 1, i.weather ? (i.weather.city || '') : '', i.weather ? (i.weather.temperature || '') : '', i.weather ? (i.weather.feels_like || '') : '', i.weather ? (i.weather.wind || '') : '', i.weather ? (i.weather.condition || '') : ''])); }} async function loadPlaylist() {{ try {{ const res = await fetch(`/api/player/playlist?screen=${{encodeURIComponent(screenName)}}&screen_id=${{encodeURIComponent(screenId)}}&_=${{Date.now()}}`, {{ headers: {{ 'X-Screen-ID': screenId, 'X-Screen-Token': screenToken }} }}); if (res.status === 401) {{ renderUnauthorized(); return; }} const data = await res.json(); companyBlocked = !!data.blocked; screenOrientation = (data.screen && data.screen.orientation) || screenOrientation || 'landscape'; badgeVisible = ((data.screen && data.screen.badge_visible) ?? 1) == 1; badgePosition = (data.screen && data.screen.badge_position) || 'top-right'; imageFit = (data.screen && data.screen.image_fit) || 'contain'; portraitImageFit = (data.screen && data.screen.portrait_image_fit) || 'contain'; feedLayout = (data.screen && data.screen.feed_layout) || 'cards'; document.body.classList.toggle('portrait', screenOrientation === 'portrait'); document.body.classList.toggle('portrait-simulated', screenOrientation === 'portrait' && window.innerWidth > window.innerHeight); applyScreenChrome(); const nextItems = data.items || []; if (data.revision) playerRevision = data.revision; const nextSignature = JSON.stringify([companyBlocked, data.revision || '', playlistSignature(nextItems)]); if (nextSignature !== currentSignature) {{ currentSignature = nextSignature; items = nextItems; currentIndex = 0; playCurrent(); }} }} catch (e) {{ console.error(e); }} }} function scheduleNext(delayMs = 1000) {{ clearTimers(); timeoutHandle = setTimeout(() => {{ if (!items.length) {{ playCurrent(); return; }} currentIndex = (currentIndex + 1) % items.length; playCurrent(); }}, Math.max(1000, delayMs)); }} function swapLayers() {{ const fromLayer = currentLayer(); const toLayer = nextLayer(); toLayer.classList.add('active'); fromLayer.classList.remove('active'); setTimeout(() => {{ if (!fromLayer.classList.contains('active')) {{ fromLayer.innerHTML = ''; }} }}, 1000); activeLayer = activeLayer === 'A' ? 'B' : 'A'; }} function renderEmpty() {{ document.body.classList.remove('feed-active'); applyTickerVisibility(); const message = companyBlocked ? 'Scherm geblokkeerd' : 'Geen actieve content'; nextLayer().innerHTML = '<div id="empty">' + message + '</div>'; swapLayers(); scheduleNext(8000); }} function renderUnauthorized() {{ document.body.classList.remove('feed-active'); applyTickerVisibility(); nextLayer().innerHTML = '<div id="empty">Player niet geautoriseerd. Genereer opnieuw een player installer of activeer opnieuw.</div>'; swapLayers(); scheduleNext(8000); }} function renderFeedPage(item, target) {{ document.body.classList.add('feed-active'); applyTickerVisibility(); const wrapper = document.createElement('div'); const pageIndex = item.feed_page_index || 1; const pageCount = item.feed_page_count || 1; const entries = item.feed_entries || []; wrapper.className = 'feed-page layout-' + (feedLayout || 'cards') + ' ' + feedThemes[(pageIndex - 1) % feedThemes.length]; const hero = document.createElement('div'); hero.className = 'feed-hero'; const titleWrap = document.createElement('div'); titleWrap.className = 'feed-title-block'; const kicker = document.createElement('div'); kicker.className = 'feed-kicker'; kicker.textContent = 'Nieuws'; const heading = document.createElement('h1'); heading.textContent = item.feed_name || 'Updates'; titleWrap.appendChild(kicker); titleWrap.appendChild(heading); hero.appendChild(titleWrap); const pagerBlock = document.createElement('div'); pagerBlock.className = 'feed-pager'; const pagerLabel = document.createElement('div'); pagerLabel.className = 'feed-page-label'; pagerLabel.textContent = 'Pagina ' + pageIndex + ' van ' + pageCount; const dots = document.createElement('div'); dots.className = 'feed-dots'; for (let i = 1; i <= pageCount; i += 1) {{ const dot = document.createElement('div'); dot.className = 'feed-dot' + (i === pageIndex ? ' active' : ''); dots.appendChild(dot); }} pagerBlock.appendChild(pagerLabel); pagerBlock.appendChild(dots); hero.appendChild(pagerBlock); wrapper.appendChild(hero); const grid = document.createElement('div'); grid.className = 'feed-grid'; entries.forEach((entry, index) => {{ const card = document.createElement('div'); card.className = 'feed-card' + (index === 0 ? ' lead' : ''); if (entry.image_url) {{ const media = document.createElement('div'); media.className = 'feed-card-media'; const img = document.createElement('img'); img.loading = 'lazy'; img.referrerPolicy = 'no-referrer'; img.onerror = () => media.remove(); img.src = entry.image_url; media.appendChild(img); card.appendChild(media); }} const label = document.createElement('div'); label.className = 'feed-index'; label.textContent = index === 0 ? 'Hoofdverhaal' : ('Verhaal ' + (index + 1)); const h = document.createElement('h3'); h.textContent = entry.title || ('Nieuwsbericht ' + (index + 1)); const story = document.createElement('div'); story.className = 'feed-story'; story.textContent = entry.summary || ''; const meta = document.createElement('p'); meta.textContent = entry.published_at || ''; card.appendChild(label); card.appendChild(h); if (story.textContent) card.appendChild(story); if (meta.textContent) card.appendChild(meta); grid.appendChild(card); }}); wrapper.appendChild(grid); target.appendChild(wrapper); swapLayers(); scheduleNext(Math.max((item.duration_seconds || 12) * 1000, 15000)); }}

function renderWeatherPage(item, target) {{
  document.body.classList.remove('feed-active');
  document.body.classList.add('weather-active');
  applyTickerVisibility();
  const weather = item.weather || {{}};
  const condClass = weatherIconClass(weather.condition);
  const wrapper = document.createElement('div');
  wrapper.className = 'weather-page cond-' + condClass;

  const scene = document.createElement('div');
  scene.className = 'weather-scene';
  if (condClass === 'sun' || condClass === 'partly') {{
    const glow = document.createElement('div');
    glow.className = 'weather-sun-glow';
    scene.appendChild(glow);
  }}
  if (condClass === 'cloud' || condClass === 'partly' || condClass === 'rain') {{
    const cloudCount = condClass === 'rain' ? 2 : 3;
    for (let i = 0; i < cloudCount; i += 1) {{
      const cloud = document.createElement('div');
      cloud.className = 'weather-cloud';
      const size = 60 + Math.random() * 70;
      cloud.style.width = size + 'px';
      cloud.style.height = (size * 0.42) + 'px';
      cloud.style.top = (8 + Math.random() * 55) + '%';
      cloud.style.animationDuration = (34 + Math.random() * 26) + 's';
      cloud.style.animationDelay = (-Math.random() * 30) + 's';
      scene.appendChild(cloud);
    }}
  }}
  if (condClass === 'rain') {{
    for (let i = 0; i < 40; i += 1) {{
      const drop = document.createElement('div');
      drop.className = 'weather-rain-drop';
      drop.style.left = (Math.random() * 100) + '%';
      drop.style.animationDuration = (0.7 + Math.random() * 0.6) + 's';
      drop.style.animationDelay = (-Math.random() * 2) + 's';
      scene.appendChild(drop);
    }}
  }}
  wrapper.appendChild(scene);

  const clock = document.createElement('div');
  clock.className = 'weather-clock';
  const clockTime = document.createElement('div');
  clockTime.className = 'weather-clock-time';
  const clockDate = document.createElement('div');
  clockDate.className = 'weather-clock-date';
  clockDate.textContent = new Date().toLocaleDateString('nl-NL', {{weekday:'long', day:'numeric', month:'long'}});
  function tickClock() {{
    clockTime.textContent = new Date().toLocaleTimeString('nl-NL', {{hour:'2-digit', minute:'2-digit'}});
  }}
  tickClock();
  clock.appendChild(clockTime);
  clock.appendChild(clockDate);
  wrapper.appendChild(clock);
  weatherClockTimer = setInterval(tickClock, 1000);

  const content = document.createElement('div');
  content.className = 'weather-content';

  const primary = document.createElement('div');
  primary.className = 'weather-primary';
  const kicker = document.createElement('div');
  kicker.className = 'weather-page-kicker';
  kicker.textContent = 'Actueel weer';
  const icon = document.createElement('div');
  icon.className = 'weather-icon-big ' + condClass;
  const tempRow = document.createElement('div');
  tempRow.className = 'weather-temp-row';
  const tempBig = document.createElement('div');
  tempBig.className = 'weather-temp-big';
  tempBig.textContent = '--';
  tempRow.appendChild(tempBig);
  const city = document.createElement('div');
  city.className = 'weather-city-big';
  city.textContent = weather.city || 'Weer';
  const cond = document.createElement('div');
  cond.className = 'weather-cond-big';
  cond.textContent = weather.condition || '';
  primary.appendChild(kicker);
  primary.appendChild(icon);
  primary.appendChild(tempRow);
  primary.appendChild(city);
  if (cond.textContent) primary.appendChild(cond);

  const statsGrid = document.createElement('div');
  statsGrid.className = 'weather-stats-grid';
  function addStat(label, value) {{
    const stat = document.createElement('div');
    stat.className = 'weather-stat-card';
    const labelEl = document.createElement('div');
    labelEl.className = 'weather-stat-label';
    labelEl.textContent = label;
    const valueEl = document.createElement('div');
    valueEl.className = 'weather-stat-value';
    valueEl.textContent = value || '--';
    stat.appendChild(labelEl);
    stat.appendChild(valueEl);
    statsGrid.appendChild(stat);
  }}
  addStat('Gevoel', weather.feels_like);
  addStat('Wind', weather.wind);
  addStat('Vochtigheid', weather.humidity);
  addStat('Hoog', weather.high);
  addStat('Laag', weather.low);
  addStat('Zon onder', weather.sunset);

  content.appendChild(primary);
  content.appendChild(statsGrid);
  wrapper.appendChild(content);
  target.appendChild(wrapper);
  swapLayers();

  const targetTemp = parseFloat(weather.temperature) || 0;
  const startTime = performance.now();
  function animateTemp(now) {{
    const progress = Math.min(1, (now - startTime) / 900);
    const eased = 1 - Math.pow(1 - progress, 3);
    tempBig.textContent = Math.round(targetTemp * eased) + '°C';
    if (progress < 1) requestAnimationFrame(animateTemp);
  }}
  requestAnimationFrame(animateTemp);

  scheduleNext(Math.max((item.duration_seconds || 10) * 1000, 12000));
}}

function playCurrent() {{ if (weatherClockTimer) {{ clearInterval(weatherClockTimer); weatherClockTimer = null; }} document.body.classList.remove('weather-active'); if (!items.length) {{ renderEmpty(); return; }} const item = items[currentIndex]; const target = nextLayer(); target.innerHTML = ''; if ((item.mimetype || '') === 'application/x-feed-page') {{ renderFeedPage(item, target); return; }} if ((item.mimetype || '') === 'application/x-weather-page') {{ renderWeatherPage(item, target); return; }} document.body.classList.remove('feed-active'); applyTickerVisibility(); if ((item.mimetype || '').startsWith('image/')) {{ const img = document.createElement('img'); img.style.objectFit = currentFit(); img.src = item.url + (item.url.includes('?') ? '&' : '?') + 'v=' + Date.now(); img.onload = () => {{ swapLayers(); scheduleNext((item.duration_seconds || 10) * 1000); }}; img.onerror = () => scheduleNext(4000); target.appendChild(img); return; }} if ((item.mimetype || '').startsWith('video/')) {{ const video = document.createElement('video'); video.style.objectFit = currentFit(); video.src = item.url + (item.url.includes('?') ? '&' : '?') + 'v=' + Date.now(); video.autoplay = true; video.muted = true; video.playsInline = true; video.oncanplay = () => swapLayers(); video.onended = () => scheduleNext(50); video.onerror = () => scheduleNext(4000); target.appendChild(video); return; }} if ((item.mimetype || '') === 'application/pdf') {{ const frame = document.createElement('iframe'); frame.style.objectFit = currentFit(); frame.src = item.url + (item.url.includes('?') ? '&' : '?') + 'v=' + Date.now(); target.appendChild(frame); swapLayers(); scheduleNext((item.duration_seconds || 12) * 1000); return; }} target.innerHTML = '<div id="empty">Onbekend bestandstype</div>'; swapLayers(); scheduleNext(5000); }} async function checkForUpdates() {{ if (refreshInFlight) return; refreshInFlight = true; try {{ const res = await fetch('/api/player/status?screen=' + encodeURIComponent(screenName) + '&screen_id=' + encodeURIComponent(screenId) + '&_=' + Date.now(), {{ headers: {{ 'X-Screen-ID': screenId, 'X-Screen-Token': screenToken }} }}); if (res.status === 401) {{ renderUnauthorized(); return; }} const data = await res.json(); if (data.revision && data.revision !== playerRevision) {{ playerRevision = data.revision; currentSignature = ''; await loadPlaylist(); await loadTicker(); }} }} catch (e) {{ console.error(e); }} finally {{ refreshInFlight = false; }} }} async function loadTicker() {{ try {{ const res = await fetch('/api/feed-ticker?screen=' + encodeURIComponent(screenName) + '&screen_id=' + encodeURIComponent(screenId) + '&_=' + Date.now(), {{ headers: {{ 'X-Screen-ID': screenId, 'X-Screen-Token': screenToken }} }}); const data = await res.json(); tickerText = data.text || ''; const inner = document.getElementById('newsTickerInner'); if (inner) inner.textContent = tickerText; applyTickerVisibility(); }} catch (e) {{ document.getElementById('newsTicker').style.display = 'none'; }} }} loadPlaylist(); loadTicker(); heartbeat(); setInterval(heartbeat, 30000); setInterval(checkForUpdates, 5000); setInterval(loadTicker, 60000);
  </script>
</body>
</html>
    """
    return html

@app.route('/uploads/<path:filename>')
def uploaded_file(filename: str) -> Response:
    return send_from_directory(UPLOAD_DIR, filename)
