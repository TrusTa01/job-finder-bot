# Model choice notes

A reference for which model to use where. Based on a test of 22 real vacancies (18 that passed the hard filter and 4 clearly unsuitable ones), with a hand-labelled gold set. Prices are USD per 1M tokens at DeepSeek peak rates.

## Decision

| Task | Model | Reasoning effort | Why |
|---|---|---|---|
| Vacancy screening (fit yes/maybe/no, flags) | `deepseek-flash` | `none` | Same decisions as every other setup, cheapest and fastest |
| Cover letters | `deepseek-flash` | `low` | Letters tied more closely to the vacancy text, and it states gaps honestly |
| Not used | `deepseek-v4-pro` | any | No quality gain in this test, slower, unstable JSON at `low` |

## Results (22 vacancies)

| Setup | Wrong "skip" on good vacancies | Wrong "apply" on bad ones | Cost | Time |
|---|---|---|---|---|
| flash, none | 0 | 0 | $0.012 | 40 s |
| flash, low | 0 | 0 | $0.040 | 147 s |
| pro, none | 0 | 0 | $0.043 | 81 s |
| pro, low | 0 | 0 (+2 JSON errors) | $0.087 | 353 s |

Roughly $0.0005 per screened vacancy with `flash/none`, so about $0.30 per month at 20 vacancies a day.

## Observations

- Screening is an easy task: the cheapest setup was already perfect. Thinking mode adds cost and time without changing decisions.
- All setups were more cautious than the gold labels: for the best four vacancies they said `maybe`, not `yes`. This does not affect whether to apply.
- Letters: both models sometimes claim a skill only because the vacancy asks for it (for example a design tool the candidate does not use), and sometimes attribute a side project to the commercial one. Mitigations: prompt rules, and a human check of every letter before it is sent.
- A missing requirement must not reject a vacancy. Only a wrong main stack does. Gaps are handled in the letter by saying what the candidate is ready to pick up, never by claiming experience that does not exist.
- Caveat: only 22 vacancies and one labeller. Re-run the test when the prompt or model changes.

## Access

From Russian IPs (VPS and home) `api.anthropic.com`, `api.openai.com` and `openrouter.ai` return 403; `api.deepseek.com` answers directly. `api.telegram.org` does not connect without a proxy.
