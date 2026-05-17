# Bolt.new Setup (SalubCast Node)

Deze versie is opnieuw opgebouwd voor Bolt met Node/Express.

## Niet Doen: Supabase Next.js Wizard
Gebruik niet de Bolt/Supabase stappen met:

```bash
npm install @supabase/supabase-js @supabase/ssr
```

Maak ook geen `page.tsx`, `utils/supabase/server.ts`, `utils/supabase/client.ts` of `.env.local` aan. Die instructies zijn voor een Next.js app met Supabase Auth. SalubCast gebruikt Express en verbindt direct met Supabase Postgres via `SUPABASE_DB_URL`.

## 1. Project Importeren In Bolt
1. Maak een zip van de map `salubcast-production`.
2. Open `https://bolt.new`.
3. Kies `Import` / `Upload project`.
4. Upload de zip.

Alternatief:
1. Push dit project naar GitHub.
2. Kies in Bolt `Import from GitHub`.
3. Selecteer de repository.

## 2. Environment Variables
Voor Bolt preview zet je eerst deze variabelen, zodat de volledige app in Bolt zelf draait zonder externe Postgres TCP-verbinding:

```env
SALUBCAST_DB_MODE=pglite
SALUBCAST_SECRET=vervang-met-lange-random-string
SALUBCAST_PUBLIC_BASE_URL=https://jouw-bolt-app-url
SALUBCAST_SESSION_SECURE=auto

SALUBCAST_BOOTSTRAP_ADMIN_EMAIL=admin@jouwdomein.nl
SALUBCAST_BOOTSTRAP_ADMIN_NAME=Master Admin
SALUBCAST_BOOTSTRAP_ADMIN_PASSWORD=vervang-met-sterk-wachtwoord
```

Voor echte productie met Supabase Postgres gebruik je:

```env
SALUBCAST_DB_MODE=postgres
SALUBCAST_SECRET=vervang-met-lange-random-string
SALUBCAST_PUBLIC_BASE_URL=https://jouw-live-app-url
SALUBCAST_SESSION_SECURE=auto

SUPABASE_DB_URL=postgresql://postgres.<project-ref>:<db-password>@aws-0-eu-west-1.pooler.supabase.com:5432/postgres

SALUBCAST_BOOTSTRAP_ADMIN_EMAIL=admin@jouwdomein.nl
SALUBCAST_BOOTSTRAP_ADMIN_NAME=Master Admin
SALUBCAST_BOOTSTRAP_ADMIN_PASSWORD=vervang-met-sterk-wachtwoord
```

Optioneel:

```env
SALUBCAST_TIMEZONE=Europe/Amsterdam
SALUBCAST_MAX_UPLOAD_MB=250
SALUBCAST_ACTIVATION_TTL_MINUTES=30
SALUBCAST_WEATHER_CACHE_TTL_SECONDS=900
```

## 3. Install Command
Gebruik:

```bash
npm install
```

## 4. Start Command
Gebruik:

```bash
npm start
```

`package.json` wijst naar `src/server.js`, dus Bolt kan de app direct starten.

## 5. Belangrijke Check Na Deploy
1. Open je app-url.
2. Ga naar `/health`; dit moet `{"ok":true,...}` tonen.
3. Als je een wit scherm of setup-fout ziet, open `/setup-check` en controleer welke environment variable of databaseverbinding faalt.
4. Log in met je superadmin account.
5. Check dashboard: datastore moet `Supabase Postgres` tonen.
6. Genereer 1 nieuwe player-installer en test de player op een scherm.

## 6. Belangrijke Opmerking
- Gebruik bij `SALUBCAST_PUBLIC_BASE_URL` het echte domein van je Bolt-app.
- `SALUBCAST_SESSION_SECURE=auto` zet Secure cookies aan zodra de app via HTTPS/proxy draait, maar laat lokale `http://127.0.0.1` tests werken.
- Gebruik niet automatisch `*.supabase.co` als app-domein; dat is alleen je database/API-project.
- Bolt preview/WebContainer kan directe PostgreSQL TCP-verbindingen blokkeren. Gebruik daar `SALUBCAST_DB_MODE=pglite`. Voor een echte serverdeploy met Supabase gebruik je `SALUBCAST_DB_MODE=postgres` en `SUPABASE_DB_URL`.
- De player installer gebruikt `SALUBCAST_PUBLIC_BASE_URL` voor `server_url` en `activation_url` in `player_config.json`. Als deze variabele ontbreekt of per ongeluk naar `*.supabase.co` wijst, gebruikt de app de huidige request-host als fallback.
- Media uploads worden net als in de oude app lokaal opgeslagen in `data/uploads_clean`. Voor permanent productiegebruik is Supabase Storage een logische vervolgstap.
