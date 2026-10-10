# job-autofinder

A personal job-search assistant. Every 15 minutes it looks for new vacancies, filters them, has an LLM screen each one and draft a short cover letter, and sends a report with the link to Telegram. Applications are sent by hand: the bot only prepares them.

## What it does

1. **hh.ru search.** Several title and full-text queries, remote vacancies only. Generic titles are kept only when the description mentions the target stack.
2. **Hard filter.** Stop-list for roles and keywords, seniority check. Salary never filters a vacancy out.
3. **LLM screening** (`fit`: yes / maybe / no, stack, required grade; the priority label is normal only for a clear fit at junior level without schedule or country problems) and a **cover letter** of a few sentences built only from facts in the candidate profile. See `docs/model-choice.md` for the models and costs.
4. **Telegram reports** with the vacancy, the verdict and the letter. Undelivered reports are resent on the next run.
5. **Other sources.** Habr Career, GetMatch and public Telegram job channels (`jobbot/tgchannels.py`, channel list in the module) are checked every few hours with the same LLM screening and draft as hh.ru (`jobbot/candidates.py`). For Telegram channels the draft is a short personal message to the post's author instead of a cover letter, because there you write to a person.
6. **Buttons and questionnaires** (`jobbot/listener.py`). Every report has ✕ (delete) and ✅ "Откликнулся" (records the response); under hh vacancies there is a checklist of which of your resumes fit. When a response has a questionnaire (hh field `has_test`), the report warns about it and lists ready-to-copy answers: `form_answers` in `config.yaml` (salary, city, experience) and `APPLY_PHONE` / `APPLY_TELEGRAM` from `.env`. **Reply to a vacancy card with the employer's questions** and the bot answers them in a developed, letter-like style from the profile (facts only, honest "no, but ready to learn" where something is missing). Your phone number never reaches the model: it sees placeholders that the bot fills in afterwards. Only your chat is served.
7. **Alerts** to the same chat when hh.ru is unreachable or the session expired, the model fails, the daily LLM budget is hit or the pass crashes; one daily heartbeat with statistics.

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

- `jobbot/` is the bot: `run.py` (pipeline), `hh.py`, `filters.py`, `llm.py`, `store.py`, `notify.py`, `alerts.py`, `listener.py` (✕ button), `settings.py`, and the secondary sources `habr.py`, `getmatch.py`, `tgchannels.py` with their shared step `candidates.py`.
- `deploy/` has the cron wrapper; `docs/deployment.md` is the step-by-step setup guide.
- `tools/` holds one-off scripts: `scan` (preview matches), `collect_sample` and `llm_eval` (model tests). Run them as `python -m tools.scan`.
- `data/` is local state (SQLite, caches, profile) and is never committed.

## Secrets

Tokens, keys and configs with personal data are not committed (see `.gitignore`). Examples live in `.env.example` and `config.example.yaml`. [gitleaks](https://github.com/gitleaks/gitleaks) scans every push on GitHub (`.github/workflows/gitleaks.yml`), and a local pre-commit hook can do the same before each commit (`winget install Gitleaks.Gitleaks`).
