"""Evaluate an LLM on the labelled vacancy sample: fit decision + cover letter, and measure cost.

Needs DEEPSEEK_API_KEY in the environment. Files used: data/sample.json, data/labels.json, data/profile.txt.
Usage: python llm_eval.py MODEL REASONING_EFFORT   (e.g. deepseek-flash none)
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "tests"
URL = "https://api.deepseek.com/chat/completions"

# USD per 1M tokens, peak rates (conservative): (input cache miss, input cache hit, output)
PRICES = {
    "deepseek-flash": (0.30, 0.006, 1.20),
    "deepseek-v4-pro": (1.32, 0.044, 3.96),
}

SYSTEM = """Ты помощник по поиску работы. Тебе дают профиль кандидата и текст вакансии. Верни JSON:
{
 "fit": "yes" | "maybe" | "no",
 "reason": "1-2 предложения по-русски: почему",
 "flutter_is_core": true | false,
 "required_grade": "junior" | "middle" | "senior" | "unknown",
 "location_or_schedule_issue": "кратко, если есть постоянное ограничение по стране, резидентству, рабочему времени по Москве или обязательному присутствию в офисе, иначе пустая строка",
 "letter": "сопроводительное письмо или пустая строка"
}
Правила оценки:
- "no": основная технология не Flutter/Dart (нативный Android/iOS, React Native, backend, продажи и т.д.), даже если Flutter упомянут вскользь или в описании компании.
- "yes": Flutter основной стек, требования реалистичны для кандидата.
- "maybe": Flutter основной стек, но требования заметно выше опыта кандидата (годы, опубликованные приложения и т.п.) или есть сомнения.
location_or_schedule_issue заполняй только для постоянных ограничений, которые мешают работать удалённо из другого часового пояса: работа только из определённой страны, требование резидентства, обязательные рабочие часы по московскому времени, регулярное (еженедельное, ежемесячное) присутствие в офисе или переезд. Разовые и редкие выезды (съёмки, мероприятия, встречи команды, конференции, командировки, «иногда в офис») НЕ упоминай. Если сомневаешься, постоянное ли ограничение, и в тексте нет слов о регулярности, оставь пустую строку.
Правила письма (только если fit не "no"): 3-6 предложений, по-русски, на «вы», живо и конкретно, без шаблонных фраз вроде «хочу предложить свою кандидатуру». Используй только факты из профиля, ничего не выдумывай, не называй сроки опыта числами, не обещай того, чего нет в профиле. Выбери 1-2 пункта профиля, которые реально совпадают с вакансией. Без markdown и эмодзи.
Верни только JSON."""


def call(model: str, effort: str, user: str, key: str) -> dict:
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
        "reasoning_effort": effort,
        "max_tokens": 4000,
    }
    if effort == "none":
        body["temperature"] = 0.2
    req = urllib.request.Request(
        URL,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)


def main() -> None:
    model, effort = sys.argv[1], sys.argv[2]
    key = os.environ["DEEPSEEK_API_KEY"]
    sample = json.loads((DATA / "sample.json").read_text(encoding="utf-8"))
    labels = json.loads((DATA / "labels.json").read_text(encoding="utf-8"))
    profile = (DATA / "profile.txt").read_text(encoding="utf-8")
    p_miss, p_hit, p_out = PRICES[model]

    rows, cost, t_in, t_out, t_reason = [], 0.0, 0, 0, 0
    t0 = time.time()
    for v in sample:
        if v["id"] not in labels:
            continue
        user = (
            f"ПРОФИЛЬ КАНДИДАТА:\n{profile}\n\nВАКАНСИЯ:\nНазвание: {v['name']}\nКомпания: {v['employer']}\n"
            f"Город: {v['area']}\nФормат: {', '.join(v['work_format'])}\nОпыт по hh: {v['experience']}\n"
            f"Описание: {v['description']}"
        )
        try:
            resp = call(model, effort, user, key)
            content = resp["choices"][0]["message"]["content"]
            out = json.loads(content)
        except (urllib.error.URLError, json.JSONDecodeError, KeyError) as e:
            out = {"fit": "error", "reason": str(e)[:150], "letter": ""}
            resp = {"usage": {}}
        u = resp.get("usage", {})
        hit = u.get("prompt_cache_hit_tokens", 0)
        miss = u.get("prompt_cache_miss_tokens", u.get("prompt_tokens", 0) - hit)
        comp = u.get("completion_tokens", 0)
        reason_toks = (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
        cost += (miss * p_miss + hit * p_hit + comp * p_out) / 1e6
        t_in += hit + miss
        t_out += comp
        t_reason += reason_toks
        rows.append({"id": v["id"], "name": v["name"], "employer": v["employer"], "gold": labels[v["id"]]["gold"], **out})

    tag = f"{model}_{effort}"
    (DATA / f"results_{tag}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

    # Decision quality: "apply" = yes/maybe, "skip" = no
    tp = sum(1 for r in rows if r["gold"] != "no" and r.get("fit") in ("yes", "maybe"))
    fn = sum(1 for r in rows if r["gold"] != "no" and r.get("fit") == "no")
    tn = sum(1 for r in rows if r["gold"] == "no" and r.get("fit") == "no")
    fp = sum(1 for r in rows if r["gold"] == "no" and r.get("fit") in ("yes", "maybe"))
    exact = sum(1 for r in rows if r["gold"] == r.get("fit"))
    errs = sum(1 for r in rows if r.get("fit") == "error")
    print(
        f"{tag}: n={len(rows)} exact={exact} | apply-correct={tp} missed-good(fn)={fn} "
        f"skip-correct={tn} false-apply(fp)={fp} errors={errs}"
    )
    print(f"   tokens in={t_in} out={t_out} (reasoning={t_reason}) | cost=${cost:.4f} | per vacancy=${cost / max(len(rows), 1):.4f} | {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
