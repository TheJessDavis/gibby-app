# Deploying Gibby Class Manager

The app is pure Python (standard library only), so there is nothing to install.
It runs with `python server.py` and reads a few environment variables:

| Variable | Purpose | Example |
|---|---|---|
| `PORT` | Port to listen on (hosts set this automatically) | `8000` |
| `DATA_DIR` | Folder for the SQLite database. Point at a persistent volume. | `/data` |
| `SEED_PASSWORD` | Password the initial accounts are seeded with. **Set this.** | a strong value |

A `Dockerfile` is included and works on almost any host.

---

## Do these BEFORE going public (security)

1. **Change the default password.** Set `SEED_PASSWORD` to something strong, then
   delete `gibby.db` once so the accounts re-seed with it. Everyone signs in with
   that until you build per-user password changes.
2. **Use a persistent volume** for `DATA_DIR`. Without it, the database (and all
   registrations) resets every time the app restarts or redeploys.
3. **HTTPS only.** Every recommended host gives you HTTPS automatically; the app
   already marks its login cookie `Secure` when it sees an HTTPS request.
4. **Never commit `config.json`** (your real API/SMTP keys). It is gitignored.
5. Set the real API keys and `"live": true` / `"email_live": true` in `config.json`
   only when you are ready for real posting and email (see `config.example.json`).

---

## Option A — Render (easiest, web dashboard)

1. Create a free account at render.com.
2. Put this folder in a GitHub repo (see "Push to GitHub" below), or use Render's
   "Deploy an existing image / repo" flow.
3. New -> **Web Service** -> connect the repo. Render detects the `Dockerfile`.
4. Add a **Disk** (Settings -> Disks): mount path `/data`, 1 GB. This is what makes
   data persist. (Persistent disks are a paid plan, roughly $7/month total.)
5. Environment -> add `SEED_PASSWORD` = your strong password, and `DATA_DIR` = `/data`.
6. Deploy. Render gives you an HTTPS URL like `https://gibby.onrender.com`.

## Option B — Fly.io (cheap, persistent volumes, uses a CLI)

```bash
# one-time
brew install flyctl        # or: curl -L https://fly.io/install.sh | sh
fly auth signup
cd ~/gibby-app
fly launch --no-deploy     # detects the Dockerfile; pick a name/region
fly volumes create data --size 1        # persistent storage
# in fly.toml, set a [mounts] entry: source = "data", destination = "/data"
fly secrets set SEED_PASSWORD=your-strong-password
fly deploy
```

## Push to GitHub (for Option A, or just to back it up)

```bash
cd ~/gibby-app
git init && git add -A && git commit -m "Gibby Class Manager"
# create an empty repo on github.com, then:
git remote add origin https://github.com/<you>/gibby-app.git
git branch -M main && git push -u origin main
```
(`gibby.db` and `config.json` are gitignored, so your data and keys stay private.)

---

## After it's live

- Sign in as `jess@theeverett.org` with your `SEED_PASSWORD`.
- Add real credentials to `config.json` on the server (or as env vars) and flip
  `live` / `email_live` to `true` when ready.
- The scheduler runs automatically every hour once the app is up.
