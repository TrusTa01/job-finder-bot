# job-autoresponder

A personal job-search assistant. It watches for new vacancies, scores them against configurable criteria, drafts cover letters and sends reports to Telegram. Applications go out in "draft for approval" mode; automatic sending is a separate switch.

> Status: early development, nothing runs yet.

## Planned features

1. **Sources as separate modules.** hh.ru first, then Telegram channels, then Habr Career.
2. **Filter and scoring.** A configurable stop-list (roles, keywords, formats) and LLM-based match scoring. Salary only affects priority, it never filters a vacancy out.
3. **Cover letters** tailored to each vacancy from my resume.
4. **Monitoring.** New vacancies are checked every 5-10 minutes during the day and hourly at night (Moscow time). Applications go out with a random 1-5 minute delay. Older matching vacancies are applied to in a batch spread over a day or two.
5. **Telegram notifications:** vacancy, score, letter text, link.

## Heads-up about hh.ru

hh.ru closed its API for job seekers on 15 December 2025. For applying, the plan is to use [hh-applicant-tool](https://github.com/s3rgeym/hh-applicant-tool), which emulates the official mobile app. This goes against the service's rules, so there is a risk of the account being blocked. The project is meant for personal use and a small volume of applications (tens per day).

## Running

Nothing to run yet. Instructions will appear once the core is ready.

## Secrets

Tokens, keys and configs with personal data are not committed (see `.gitignore`). An example of the environment variables will live in `.env.example`.
