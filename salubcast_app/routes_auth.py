from .shared import *

@app.route("/register", methods=["GET", "POST"])
def register() -> str:
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()
        new_company_name = request.form.get("new_company_name", "").strip()

        if not full_name or not email or not password:
            flash("Naam, e-mail en wachtwoord zijn verplicht.")
            return redirect(url_for("register"))
        if fetch_one("SELECT id FROM users WHERE email = ? LIMIT 1", (email,)):
            flash("Er bestaat al een account met dit e-mailadres.")
            return redirect(url_for("register"))

        if not new_company_name:
            flash("Open registratie is alleen toegestaan voor een nieuw bedrijf. Laat je toevoegen door een admin als je al een bedrijf hebt.")
            return redirect(url_for("register"))
        if fetch_one("SELECT id FROM companies WHERE lower(name) = lower(?) LIMIT 1", (new_company_name,)):
            flash("Er bestaat al een bedrijf met deze naam.")
            return redirect(url_for("register"))

        chosen_company_id = ""
        role = "user"
        base_slug = new_company_name.lower().strip().replace(" ", "-") or f"bedrijf-{secrets.token_hex(2)}"
        slug = base_slug
        suffix = 1
        while fetch_one("SELECT id FROM companies WHERE slug = ? LIMIT 1", (slug,)):
            suffix += 1
            slug = f"{base_slug}-{suffix}"
        chosen_company_id = str(uuid.uuid4())
        trial_days = parse_int(os.environ.get('SALUBCAST_DEFAULT_TRIAL_DAYS', '14'), 14, minimum=1, maximum=365)
        trial_ends_at = (datetime.now(timezone.utc) + timedelta(days=trial_days)).isoformat()
        execute(
            "INSERT INTO companies (id, name, slug, is_active, billing_email, plan_name, billing_status, trial_ends_at, created_at) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)",
            (chosen_company_id, new_company_name, slug, email, "starter", "trial", trial_ends_at, now_iso()),
        )
        role = "company_admin"

        salt = secrets.token_hex(8)
        execute(
            "INSERT INTO users (id, company_id, email, password_hash, salt, full_name, role, is_active, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (str(uuid.uuid4()), chosen_company_id, email, hash_password(password, salt), salt, full_name, role, now_iso()),
        )
        flash("Account aangemaakt. Je kunt nu inloggen.")
        return redirect(url_for("login"))

    content = render_template_string(
        """
        <div class="login-shell">
          <div class="card login-card">
            <div class="kicker">SalubCast onboarding</div>
            <h1>Maak een account</h1>
            <p class="muted">Open registratie maakt altijd een nieuw bedrijf aan. Voor toegang tot een bestaand bedrijf moet een admin je account aanmaken.</p>
            <form method="post">
              <input name="full_name" placeholder="Volledige naam">
              <input name="email" type="email" placeholder="E-mailadres">
              <input name="password" type="password" placeholder="Wachtwoord">
              <input name="new_company_name" placeholder="Nieuw bedrijf, bijv. NewAudioVisuals">
              <button type="submit">Account maken</button>
            </form>
            <div class="inline"><a href="{{ url_for('login') }}"><button class="secondary" style="width:auto;">Terug naar login</button></a></div>
          </div>
        </div>
        """,
    )
    return render_shell("Registreren", content)

@app.route("/login", methods=["GET", "POST"])
def login() -> str:
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = fetch_one(
            "SELECT users.*, companies.name AS company_name, companies.is_active AS company_active FROM users JOIN companies ON companies.id = users.company_id WHERE users.email = ? LIMIT 1",
            (email,),
        )
        ok = False
        needs_rehash = False
        if user and user['is_active'] == 1 and user['company_active'] == 1:
            ok, needs_rehash = verify_password(password, user['salt'], user['password_hash'])
        if ok:
            if needs_rehash:
                execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password, user['salt']), user['id']))
            session['user_id'] = user['id']
            session['company_id'] = user['company_id']
            session['company_name'] = user['company_name']
            session.pop('company_view_id', None)
            session.pop('company_view_name', None)
            session['user_name'] = user['full_name']
            session['user_email'] = user['email']
            session['user_role'] = user['role']
            flash("Welkom terug in SalubCast.")
            return redirect(url_for('dashboard'))
        flash("Inloggen mislukt. Check je gegevens of accountstatus.")
    content = """
    <div class="login-shell">
      <div class="card login-card">
        <div class="kicker">SalubCast access</div>
        <h1>Log in</h1>
        <form method="post">
          <input name="email" type="email" placeholder="E-mailadres">
          <input name="password" type="password" placeholder="Wachtwoord">
          <button type="submit">Inloggen</button>
        </form>
        <div class="inline">
          <a href="{{ url_for('register') }}"><button class="secondary" style="width:auto;">Account registreren</button></a>
          <a href="{{ url_for('forgot_password') }}"><button class="secondary" style="width:auto;">Wachtwoord vergeten</button></a>
        </div>
      </div>
    </div>
    """
    return render_shell("Login", content)

@app.route("/logout", methods=["GET", "POST"])
def logout() -> Response:
    session.clear()
    flash("Je bent uitgelogd.")
    return redirect(url_for("login"))

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password() -> str:
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = fetch_one("SELECT id, full_name FROM users WHERE email = ? AND is_active = 1 LIMIT 1", (email,))
        if user:
            token = secrets.token_urlsafe(32)
            expires_at = (datetime.now(timezone.utc) + timedelta(minutes=PASSWORD_RESET_TTL_MINUTES)).isoformat()
            execute(
                "INSERT INTO password_resets (id, user_id, token_hash, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), user["id"], hash_reset_token(token), expires_at, now_iso()),
            )
            reset_url = f"{external_base_url()}{url_for('reset_password', token=token)}"
            send_email(
                email,
                "SalubCast wachtwoord resetten",
                f"Hoi {user['full_name']},\n\n"
                f"Klik op de link hieronder om je wachtwoord te resetten. "
                f"De link is {PASSWORD_RESET_TTL_MINUTES} minuten geldig.\n\n{reset_url}\n\n"
                f"Heb je dit niet aangevraagd? Dan kun je deze e-mail negeren.",
            )
            log_event(email, "password_reset_requested", "user", user["id"], "Password reset email requested")
        flash("Als dit e-mailadres bij ons bekend is, hebben we een resetlink verstuurd.")
        return redirect(url_for("forgot_password"))
    content = """
    <div class="login-shell">
      <div class="card login-card">
        <div class="kicker">SalubCast access</div>
        <h1>Wachtwoord vergeten</h1>
        <p class="muted">Vul je e-mailadres in. Als dit adres bij ons bekend is, sturen we een resetlink.</p>
        <form method="post">
          <input name="email" type="email" placeholder="E-mailadres" required>
          <button type="submit">Resetlink versturen</button>
        </form>
        <div class="inline"><a href="{{ url_for('login') }}"><button class="secondary" style="width:auto;">Terug naar login</button></a></div>
      </div>
    </div>
    """
    return render_shell("Wachtwoord vergeten", content)

@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token: str) -> str:
    reset_row = fetch_one("SELECT * FROM password_resets WHERE token_hash = ? LIMIT 1", (hash_reset_token(token),))
    expires_at = parse_iso(reset_row["expires_at"]) if reset_row else None
    valid = bool(reset_row) and not reset_row["used_at"] and expires_at is not None and datetime.now(timezone.utc) <= expires_at
    if not valid:
        content = """
        <div class="login-shell">
          <div class="card login-card">
            <div class="kicker">SalubCast access</div>
            <h1>Link ongeldig</h1>
            <p class="muted">Deze resetlink is ongeldig, al gebruikt of verlopen.</p>
            <div class="inline"><a href="{{ url_for('forgot_password') }}"><button style="width:auto;">Nieuwe link aanvragen</button></a></div>
          </div>
        </div>
        """
        return render_shell("Link ongeldig", content)

    if request.method == "POST":
        password = request.form.get("password", "").strip()
        confirm = request.form.get("confirm_password", "").strip()
        if not password or len(password) < 8:
            flash("Wachtwoord moet minimaal 8 tekens zijn.")
            return redirect(url_for("reset_password", token=token))
        if password != confirm:
            flash("Wachtwoorden komen niet overeen.")
            return redirect(url_for("reset_password", token=token))
        user = fetch_one("SELECT * FROM users WHERE id = ? LIMIT 1", (reset_row["user_id"],))
        salt = secrets.token_hex(8)
        execute("UPDATE users SET password_hash = ?, salt = ? WHERE id = ?", (hash_password(password, salt), salt, user["id"]))
        execute("UPDATE password_resets SET used_at = ? WHERE id = ?", (now_iso(), reset_row["id"]))
        log_event(user["email"], "password_reset_completed", "user", user["id"], "Password reset via emailed link")
        flash("Wachtwoord bijgewerkt. Je kunt nu inloggen.")
        return redirect(url_for("login"))

    content = """
    <div class="login-shell">
      <div class="card login-card">
        <div class="kicker">SalubCast access</div>
        <h1>Nieuw wachtwoord instellen</h1>
        <form method="post">
          <input name="password" type="password" placeholder="Nieuw wachtwoord" required minlength="8">
          <input name="confirm_password" type="password" placeholder="Bevestig wachtwoord" required minlength="8">
          <button type="submit">Wachtwoord instellen</button>
        </form>
      </div>
    </div>
    """
    return render_shell("Nieuw wachtwoord", content)

@app.route("/branding", methods=["GET", "POST"])
@login_required
def branding_settings():
    company_id = current_company_id()
    if request.method == "POST":
        file = request.files.get("logo")
        if file and file.filename:
            valid, message = validate_upload_file(file)
            if not valid:
                flash(message)
                return redirect(url_for("branding_settings"))
            ext = secure_filename(file.filename).rsplit('.', 1)[-1].lower()
            filename = f"{company_id}_{int(time.time())}.{ext}"
            path = company_logo_path(filename)
            file.save(path)
            old_logo = get_company_branding_filename(company_id)
            execute("UPDATE companies SET logo_filename = ? WHERE id = ?", (filename, company_id))
            if old_logo and old_logo != filename:
                old_path = company_logo_path(old_logo)
                if old_path.exists():
                    old_path.unlink()
            log_event(actor_label(), 'branding_updated', 'company', company_id, f'Logo updated to {filename}', company_id)
            flash("Logo opgeslagen voor dit bedrijf.")
        return redirect(url_for("branding_settings"))
    content = render_template_string(
        """
        <div class="grid two">
          <div class="card">
            <h2>Branding</h2>
            <p class="muted">Upload hier het logo van jouw bedrijf. Dit logo is tenant-specifiek en verschijnt op login en dashboard.</p>
            <form method="post" enctype="multipart/form-data">
              <input type="file" name="logo" accept="image/*">
              <button type="submit">Logo uploaden</button>
            </form>
          </div>
          <div class="card">
            <h2>Preview</h2>
            {% if logo_url %}
            <img src="{{ logo_url }}" class="logo-preview">
            {% else %}
            <div class="muted">Nog geen logo geupload voor dit bedrijf.</div>
            {% endif %}
          </div>
        </div>
        """,
        logo_url=current_company_branding_url(),
    )
    return render_shell("Branding", content)

@app.route("/company-branding/<path:filename>")
def company_branding_logo(filename: str):
    safe_name = safe_branding_filename(filename)
    if safe_name:
        path = company_logo_path(safe_name)
        if path.exists():
            return send_from_directory(company_logo_dir(), safe_name)
    return Response(status=404)

@app.route("/branding_logo.png")
def branding_logo():
    if BRAND_LOGO.exists():
        return send_from_directory(BASE_DIR, "branding_logo.png")
    return Response(status=404)

@app.route("/player-installer", methods=["GET", "POST"])
@superadmin_required
def player_installer() -> str:
    company_id = current_company_id()
    screens = fetch_all("SELECT * FROM screens WHERE company_id = ? ORDER BY name", (company_id,))
    if request.method == "POST":
        screen_id = request.form.get("screen_id", "")
        server_base_url = request.form.get("server_base_url", external_base_url()).strip().rstrip("/")
        screen = fetch_one("SELECT * FROM screens WHERE id = ? AND company_id = ? LIMIT 1", (screen_id, company_id))
        if not screen:
            flash("Scherm niet gevonden.")
            return redirect(url_for("player_installer"))
        if not server_base_url.startswith(("http://", "https://")):
            flash("Server URL moet met http:// of https:// beginnen.")
            return redirect(url_for("player_installer"))
        code = generate_activation_code()
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=ACTIVATION_CODE_TTL_MINUTES)).isoformat()
        execute(
            "UPDATE screens SET activation_code = ?, activation_expires_at = ?, activation_used_at = NULL, activated_at = NULL WHERE id = ?",
            (code, expires_at, screen_id),
        )
        log_event(actor_label(), 'player_installer_created', 'screen', screen_id, f'Installer generated for {screen["name"]}', company_id)
        package_io = create_player_package_zip(server_base_url, screen['id'], screen['name'], code, screen['token'])
        filename = f"{screen['name'].replace(' ', '_')}_player_package.zip"
        return send_file(package_io, mimetype="application/zip", as_attachment=True, download_name=filename)

    recent_screen = fetch_one("SELECT * FROM screens WHERE company_id = ? AND activation_code IS NOT NULL ORDER BY activation_expires_at DESC, created_at DESC LIMIT 1", (company_id,))
    example_package_name = f"{recent_screen['name'].replace(' ', '_')}_player_package.zip" if recent_screen else "jouw_player_package.zip"
    content = render_template_string(
        """
        <div class="grid two">
          <div class="card">
            <h2>Player installer generator</h2>
            <p class="muted">Maakt player-bestanden voor Windows en Linux met activatiecode en screen-id. Activatiecode verloopt na {{ ttl }} minuten.</p>
            <form method="post">
              <select name="screen_id">{% for s in screens %}<option value="{{ s['id'] }}">{{ s['name'] }} - {{ s['location'] }}</option>{% endfor %}</select>
              <input name="server_base_url" placeholder="Bijv. https://tv.jouwdomein.nl" value="{{ server_base_url }}">
              <button type="submit">Installer-bestanden maken</button>
            </form>
          </div>
          <div class="card">
            <h2>Activatie</h2>
            {% if recent_screen and recent_screen['activation_code'] %}
            <div class="muted">Laatste scherm: <strong>{{ recent_screen['name'] }}</strong></div>
            <div class="codebox">{{ recent_screen['activation_code'] }}</div>
            <p class="muted">Geldig tot: {{ recent_screen['activation_expires_at'] or '-' }}</p>
            {% else %}
            <div class="muted">Nog geen activatiecode gegenereerd.</div>
            {% endif %}
          </div>
        </div>
        <div class="grid two" style="margin-top:16px;">
          <div class="card">
            <h2>Linux snelstart</h2>
            <p class="muted">Nieuwe packages bevatten nu twee klikbare Linux-bestanden: <code>Install_SalubCast_Player.sh</code> en <code>Install_SalubCast_Player.desktop</code>. Begin met het <code>.sh</code>-bestand; dat werkt op de meeste Linux-desktops betrouwbaarder dan een <code>.desktop</code>-bestand.</p>
            <textarea class="codebox" id="linux-install-command" readonly onclick="this.focus(); this.select();" style="width:100%; min-height:170px; resize:vertical;">mkdir -p ~/salubcast-player
cd ~/salubcast-player
unzip ~/Downloads/{{ example_package_name }}
chmod +x start_player.sh install_linux.sh install_and_start_linux.sh open_player_launcher.sh Install_SalubCast_Player.sh Install_SalubCast_Player.desktop
bash install_linux.sh
bash start_player.sh</textarea>
            <div class="inline" style="margin-top:12px;">
              <button type="button" onclick="copyCode('linux-install-command', this)" style="width:auto;">Kopieer Linux install</button>
              <button type="button" class="secondary" onclick="copyCode('linux-launch-command', this)" style="width:auto;">Kopieer launcher-start</button>
            </div>
          </div>
          <div class="card">
            <h2>Linux klikbestand</h2>
            <p class="muted">Eerst proberen: dubbelklik <code>Install_SalubCast_Player.sh</code> in de uitgepakte map. Als jouw Linux-pc dat bestand vraagt te openen in plaats van uit te voeren, kies dan <strong>Run</strong> of gebruik het alles-in-een script hieronder.</p>
            <textarea class="codebox" id="linux-launch-command" readonly onclick="this.focus(); this.select();" style="width:100%; min-height:160px; resize:vertical;">cd ~/salubcast-player
bash install_and_start_linux.sh

bash open_player_launcher.sh

systemctl --user enable --now salubcast-player.service</textarea>
            <p class="muted" style="margin-top:12px;">Voer <code>~/.config/autostart/salubcast-player.desktop</code> niet direct uit in Terminal. Dat bestand is alleen voor autostart bij inloggen. De bestanden in de uitgepakte map zijn de juiste klikbestanden.</p>
          </div>
        </div>
        <script>
        function copyWithFallback(text) {
          const helper = document.createElement('textarea');
          helper.value = text;
          helper.setAttribute('readonly', '');
          helper.style.position = 'fixed';
          helper.style.opacity = '0';
          document.body.appendChild(helper);
          helper.focus();
          helper.select();
          const ok = document.execCommand('copy');
          document.body.removeChild(helper);
          return ok;
        }
        async function copyCode(id, btn) {
          const node = document.getElementById(id);
          const text = node.value || node.innerText;
          try {
            if (navigator.clipboard && window.isSecureContext) {
              await navigator.clipboard.writeText(text);
            } else if (!copyWithFallback(text)) {
              throw new Error('fallback failed');
            }
            const original = btn.textContent;
            btn.textContent = 'Gekopieerd';
            setTimeout(() => { btn.textContent = original; }, 1400);
          } catch (err) {
            if (node.select) {
              node.focus();
              node.select();
            }
            alert('Kopieren lukte niet automatisch. De tekst is nu geselecteerd; gebruik Ctrl+C.');
          }
        }
        </script>
        """,
        screens=screens,
        recent_screen=recent_screen,
        ttl=ACTIVATION_CODE_TTL_MINUTES,
        example_package_name=example_package_name,
        server_base_url=external_base_url(),
    )
    return render_shell("Player installer", content)

@app.route("/activate-player")
def activate_player() -> str:
    code = request.args.get("code", "").strip().upper()
    screen = fetch_one("SELECT * FROM screens WHERE activation_code = ? LIMIT 1", (code,))
    expires_at = parse_iso(screen['activation_expires_at']) if screen else None
    if not screen or not expires_at or datetime.now(timezone.utc) > expires_at:
        return "Activatiecode ongeldig of verlopen.", 404
    execute("UPDATE screens SET activation_used_at = ?, activated_at = COALESCE(activated_at, ?), activation_code = NULL, activation_expires_at = NULL WHERE id = ?", (now_iso(), now_iso(), screen['id']))
    log_event('player', 'screen_activated', 'screen', screen['id'], f'Activated from IP {request.remote_addr}', screen['company_id'])
    response = redirect(url_for("player_page", screen=screen['name'], screen_id=screen['id']))
    cookie_max_age = 60 * 60 * 24 * 365
    response.set_cookie("salubcast_screen_id", screen["id"], max_age=cookie_max_age, samesite="Lax", httponly=True)
    response.set_cookie("salubcast_screen_token", screen["token"], max_age=cookie_max_age, samesite="Lax", httponly=True)
    return response
