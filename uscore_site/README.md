# uScore Leaderboard

Automated pipeline + public website for the uScore pitch-uniqueness model.

- `sql/schema.sql` — run once in Supabase to create the database.
- `pipeline/refresh.py` — pulls fresh Statcast data daily and recomputes uScore.
- `.github/workflows/refresh.yml` — runs the pipeline every day for free via GitHub Actions.
- `site/` — the public leaderboard website (GitHub Pages).

See the setup guide for full step-by-step instructions (account creation, keys, deployment).
