# job-autoresponder

A personal job-search assistant. Every 15 minutes it looks for new vacancies, filters them, has an LLM screen each one and draft a short cover letter, and sends a report with the link to Telegram. Applications are sent by hand: the bot only prepares them.

## What it does

1. **hh.ru search.** Several title and full-text queries, remote vacancies only. Generic titles are kept only when the description mentions the target stack.
2. **Hard filter.** Stop-list for roles and keywords, seniority check. Salary never filters a vacancy out, it only sets priority.
3. **LLM screening** (`fit`: yes / maybe / no, stack, required grade) and a **cover letter** of a few sentences built only from facts in the candidate profile. See `docs/model-choice.md` for the models and costs.
4. **Telegram reports** with the vacancy, the verdict and the letter. Undelivered reports are resent on the next run.
5. **Cheap watchers** (no LLM, link only) for sources that rarely have matching vacancies: Habr Career, GetMatch and public Telegram job channels (`jobbot/tgchannels.py`, channel list in the module).
6. **Alerts** to the same chat when hh.ru is unreachable or the session expired, the model fails, the daily LLM budget is hit or the pass crashes; one daily heartbeat with statistics.

## Heads-up about hh.ru

hh.ru closed its API for job seekers on 15 December 2025. Reading vacancies goes through [hh-applicant-tool](https://github.com/s3rgeym/hh-applicant-tool), which emulates the official mobile app. This goes against the service's rules, so there is a risk of the account being blocked. The project is meant for personal, read-only use. The session expires about every two weeks; the bot sends an alert with instructions when that happens.

## Running

Setup:

1. `python -m venv .venv`, then `pip install -r requirements.txt`.
2. Authorize `hh-applicant-tool` once on your own machine (it stores the session in `~/.config/hh-applicant-tool`).
3. Copy `.env.example` to `.env` and `config.example.yaml` to `config.yaml`, then fill them in.
4. Write a short candidate profile to `data/profile.txt`.

Run one pass: `python -m jobbot.run --limit 8` (add `--no-notify` to skip Telegram).

On a server, run `deploy/run_pass.sh` from cron every 15 minutes. It adds a random delay, takes a lock so passes never overlap, and runs less often at night. Telegram is usually blocked from Russian IPs, so set `TELEGRAM_PROXY_URL` in `.env`.

## Layout

- `jobbot/` is the bot: `run.py` (pipeline), `hh.py`, `filters.py`, `llm.py`, `store.py`, `notify.py`, `alerts.py`, `settings.py`, and the watchers `habr.py`, `getmatch.py`, `tgchannels.py`.
- `deploy/` has the cron wrapper; `docs/deployment.md` is the step-by-step setup guide.
- `tools/` holds one-off scripts: `scan` (preview matches), `collect_sample` and `llm_eval` (model tests). Run them as `python -m tools.scan`.
- `data/` is local state (SQLite, caches, profile) and is never committed.

## Secrets

Tokens, keys and configs with personal data are not committed (see `.gitignore`). Examples live in `.env.example` and `config.example.yaml`. A local pre-commit hook runs [gitleaks](https://github.com/gitleaks/gitleaks) on staged changes (`winget install Gitleaks.Gitleaks`).
