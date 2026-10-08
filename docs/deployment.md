# Deployment guide

How to run your own copy of the bot on a VPS. It takes about an hour, most of it spent on writing the candidate profile and the search config. Nothing here sends applications: the bot only finds vacancies and posts a report with a draft letter to your Telegram.

## What you need

| Item | Notes |
|---|---|
| A VPS in your own country | Ubuntu 22.04/24.04, 1 vCPU, 1-2 GB RAM, about 3-5 USD a month. hh.ru must be reachable from its IP. |
| A computer for the one-time hh.ru login | Windows, macOS or Linux with Python 3.11+. The login needs a browser or phone and may show a captcha. |
| An hh.ru account | With at least one published resume. |
| A DeepSeek API key | platform.deepseek.com, top up a few dollars. Expect well under 1 USD a month. Other OpenAI-compatible providers need small changes in `jobbot/llm.py`. |
| A Telegram bot | Create it with @BotFather and keep the token. |
| A SOCKS5 proxy outside your country | Only if `api.telegram.org` is blocked from your VPS (it is from Russian IPs). It is used for Telegram traffic only, never for hh.ru. |

**Risk you accept.** hh.ru closed its job-seeker API on 15 December 2025. The bot reads vacancies through `hh-applicant-tool`, which pretends to be the official mobile app. That goes against the service's rules and the account can be blocked. The bot only reads (search and vacancy pages) and never applies, which keeps the footprint small, but the risk is not zero. Use an account you can afford to lose.

## 1. Prepare the VPS

Log in as root and create an unprivileged user, then work as that user:

```bash
adduser bot
mkdir -p /home/bot/.ssh && cp ~/.ssh/authorized_keys /home/bot/.ssh/
chown -R bot:bot /home/bot/.ssh
apt update && apt install -y python3-venv git
```

Use SSH keys and turn off password login in `/etc/ssh/sshd_config` (`PasswordAuthentication no`). Do not give the bot user sudo.

## 2. Get the code

```bash
su - bot
git clone <your fork or copy of the repository> job-autoresponder
cd job-autoresponder
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 3. Log in to hh.ru (on your own computer)

Do this on your computer, not on the VPS, so the login comes from your usual IP and you can solve a captcha:

```bash
pip install hh-applicant-tool
hh-applicant-tool authorize
```

Follow the prompts. The session is stored in `~/.config/hh-applicant-tool` (on Windows `%USERPROFILE%\.config\hh-applicant-tool`). Copy that whole folder to the same path of the `bot` user on the VPS:

```bash
scp -r ~/.config/hh-applicant-tool bot@YOUR_VPS:~/.config/
```

Check that it works on the VPS:

```bash
.venv/bin/hh-applicant-tool -c ~/.config/hh-applicant-tool call-api /me
```

You should get JSON with your account data. The token expires about every two weeks; the bot then sends a Telegram alert and you repeat this step.

## 4. Secrets: `.env`

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

- `DEEPSEEK_API_KEY`: your key. Leave `OPENROUTER_API_KEY` and `LLM_PROXY_URL` empty (DeepSeek is reachable directly from Russia; set `LLM_PROXY_URL` only if it is blocked for you).
- `TELEGRAM_BOT_TOKEN`: the token from @BotFather.
- `TELEGRAM_CHAT_ID`: your chat id. Send any message to your bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` (through the proxy if needed) and read `message.chat.id`.
- `TELEGRAM_PROXY_URL`: `socks5://user:pass@host:port` if Telegram is blocked. The bot converts it to `socks5h` so DNS goes through the proxy too.

Never commit `.env`. It is in `.gitignore`.

## 5. Your search config and profile

```bash
cp config.example.yaml config.yaml
mkdir -p data && nano data/profile.txt
```

**`config.yaml`** defines what to look for. The main parts:
- `search.queries`: hh.ru searches. `field: name` searches titles, `field: all` searches the full text.
- `required_work_format: REMOTE`: change if you want hybrid or on-site.
- `title_signals` and `description_signals`: words that make a vacancy relevant. Generic titles such as "Mobile developer" are kept only when the description contains a description signal.
- `seniority`, `stop_roles_keywords`, `stop_text_keywords`: what to skip.
- `limits.daily_llm_budget_usd`: the bot stops using the LLM and alerts when the last 24 hours cost this much.

Run `python -m tools.scan` on your computer to preview what the filter lets through before you start spending on the LLM.

**`data/profile.txt`** is a plain-text description of you that goes into every prompt: stack, real experience, what you have not done, availability. This is the most important file. The letter writer is told to use only facts from it, so anything not written there will not appear in letters, and anything false written there will. Include an explicit "I have not worked with ..." list; the bot uses it to write honest one-sentence gaps instead of inventing skills.

Also edit the prompts in `jobbot/llm.py` (`SCREEN_SYSTEM`, `LETTER_SYSTEM`) and the time zone sentence if you are in another time zone. They are written for one specific candidate and stack.

## 6. First run

```bash
.venv/bin/python -m jobbot.run --no-notify --limit 3
```

This stores results without writing to Telegram. Read the output: `found` is how many vacancies matched your queries, `new` how many were unseen, `ready` how many passed screening. Then run without `--no-notify` once to see a real message in Telegram.

The first real run finds the whole 30-day backlog. The `--limit` flag caps how many vacancies are sent to the LLM per pass, so the backlog is processed over several passes instead of in one burst.

## 7. Schedule it

```bash
crontab -e
```

```
*/15 * * * * /home/bot/job-autoresponder/deploy/run_pass.sh
```

`deploy/run_pass.sh` runs every pass during the day (06:00-21:59 Moscow time) and only the first pass of each hour at night, adds a random delay of up to two minutes, and takes a lock so passes never overlap. Change the hours inside the script if your day differs. Output goes to `data/run.log`.

## What you will receive

- A message per suitable hh.ru vacancy: title, company, link, verdict, and a draft cover letter.
- Links from the cheap watchers (Habr Career, GetMatch, public Telegram channels listed in `jobbot/tgchannels.py`). Edit `CHANNELS` there to follow other channels.
- Alerts: hh.ru unreachable or session expired, vacancy descriptions not loading, model failing, daily budget hit, crash. The same problem is reported at most every six hours.
- One heartbeat message a day after 09:00 Moscow time with the previous day's numbers.

## Maintenance

| Task | How |
|---|---|
| Renew the hh.ru session (about every 2 weeks) | Repeat step 3 when the alert arrives. |
| Update the code | Pull or copy the changed files to the VPS, no restart needed (cron starts a new process each time). |
| See what happened | `tail -50 data/run.log` |
| Check spending | The last line of a pass prints the total LLM spend in USD. |
| Reset state | Stop cron, delete `data/jobs.sqlite`; the next run treats everything as new. |
| Stop the bot | Remove the cron line. |

## Troubleshooting

- **"hh.ru не вернул ни одной вакансии"**: the session expired or hh.ru blocked the IP. Repeat step 3; if it still fails, test `call-api /me` by hand.
- **No Telegram messages but passes succeed**: check the proxy and chat id; failed sends are retried on the next pass, so look at `data/run.log` for `telegram send failed`.
- **Every letter sounds like someone else**: the prompts in `jobbot/llm.py` and `data/profile.txt` are still written for the original author.
- **Many irrelevant vacancies**: tighten `description_signals` and `stop_roles_keywords`, then rerun `python -m tools.scan`.
- **Costs higher than expected**: lower `--limit` in `run_pass.sh` or `daily_llm_budget_usd` in `config.yaml`.
