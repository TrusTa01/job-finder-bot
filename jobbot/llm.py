"""DeepSeek calls: screening (cheap, no thinking) and cover letters (low thinking).

See docs/model-choice.md for why these models and settings were chosen.
"""
import json
import time

import requests
import yaml

from . import settings
from .filters import CFG

URL = "https://api.deepseek.com/chat/completions"
SCREEN_MODEL, SCREEN_EFFORT = "deepseek-flash", "none"
LETTER_MODEL, LETTER_EFFORT = "deepseek-flash", "low"

# USD per 1M tokens at peak rates: (input cache miss, input cache hit, output)
PRICES = {"deepseek-flash": (0.30, 0.006, 1.20), "deepseek-v4-pro": (1.32, 0.044, 3.96)}

PROMPTS_FILE = settings.ROOT / CFG.get("prompts", "prompts/example.yaml")  # prompt texts per candidate field: prompts/*.yaml
PROMPTS = yaml.safe_load(PROMPTS_FILE.read_text(encoding="utf-8")) or {}
_missing = [k for k in ("screen", "letter", "dm", "answer") if not isinstance(PROMPTS.get(k), str)]
if _missing:
    raise SystemExit(f"{PROMPTS_FILE.name}: missing prompt text(s): {', '.join(_missing)}")


def _prompt(key: str) -> str:
    """{availability} in the prompt file is replaced with the candidate's hours from config.yaml."""
    return PROMPTS[key].replace("{availability}", str(CFG.get("availability_msk", "")))


SCREEN_SYSTEM = _prompt("screen")
LETTER_SYSTEM = _prompt("letter")
DM_SYSTEM = _prompt("dm")
ANSWER_SYSTEM = _prompt("answer")


def _post(model: str, effort: str, system: str, user: str) -> tuple[dict, dict]:
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
        "reasoning_effort": effort,
        "max_tokens": 4000,
    }
    if effort == "none":
        body["temperature"] = 0.2
    headers = {"Authorization": f"Bearer {settings.get('DEEPSEEK_API_KEY')}"}
    last = "unknown"
    for attempt in range(3):
        try:
            r = requests.post(URL, json=body, headers=headers, timeout=180)
            if r.status_code == 200:
                data = r.json()
                return json.loads(data["choices"][0]["message"]["content"]), data.get("usage", {})
            last = f"HTTP {r.status_code}"
        except (requests.RequestException, json.JSONDecodeError, KeyError) as e:
            last = type(e).__name__
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"LLM call failed after retries: {last}")


def plain_dashes(text: str) -> str:
    """Long and medium dashes read like machine text: use a plain hyphen."""
    return text.replace(" — ", " - ").replace(" – ", " - ").replace("—", "-").replace("–", "-")


def _cost(model: str, usage: dict) -> tuple[int, int, float]:
    p_miss, p_hit, p_out = PRICES[model]
    hit = usage.get("prompt_cache_hit_tokens", 0)
    miss = usage.get("prompt_cache_miss_tokens", usage.get("prompt_tokens", 0) - hit)
    out = usage.get("completion_tokens", 0)
    return hit + miss, out, (miss * p_miss + hit * p_hit + out * p_out) / 1e6


def _vacancy_text(v: dict, description: str) -> str:
    salary = v.get("salary")
    return (
        f"Название: {v.get('name')}\nКомпания: {(v.get('employer') or {}).get('name', '')}\n"
        f"Город: {(v.get('area') or {}).get('name', '')}\n"
        f"Формат: {', '.join(f['id'] for f in v.get('work_format') or [])}\n"
        f"Опыт по hh: {(v.get('experience') or {}).get('name', '')}\n"
        f"Зарплата: {salary if salary else 'не указана'}\n"
        f"Описание: {' '.join(description.split())[:3500]}"
    )


def screen(profile: str, v: dict, description: str) -> tuple[dict, tuple]:
    user = f"ПРОФИЛЬ КАНДИДАТА:\n{profile}\n\nВАКАНСИЯ:\n{_vacancy_text(v, description)}"
    out, usage = _post(SCREEN_MODEL, SCREEN_EFFORT, SCREEN_SYSTEM, user)
    return out, ("screen", SCREEN_MODEL, SCREEN_EFFORT, *_cost(SCREEN_MODEL, usage))


def gender_note() -> str:
    """Tells the model the candidate's gender, so first-person past tense agrees (откликнулась / откликнулся)."""
    if CFG.get("candidate_gender") == "female":
        return "ПОЛ КАНДИДАТА: женский. Глаголы в прошедшем времени от первого лица пиши в женском роде (сделала, работала, готова).\n\n"
    return "ПОЛ КАНДИДАТА: мужской. Глаголы в прошедшем времени от первого лица пиши в мужском роде (сделал, работал, готов).\n\n"


def write_letter(profile: str, v: dict, description: str, issue: str, direct_message: bool = False) -> tuple[str, tuple]:
    user = gender_note() + (
        f"ПРОФИЛЬ КАНДИДАТА:\n{profile}\n\nВАКАНСИЯ:\n{_vacancy_text(v, description)}\n\n"
        f"УПОМЯНИ ЧАСОВОЙ ПОЯС: {'да' if issue else 'нет'}"
    )
    out, usage = _post(LETTER_MODEL, LETTER_EFFORT, DM_SYSTEM if direct_message else LETTER_SYSTEM, user)
    return plain_dashes(str(out.get("letter", "")).strip()), ("letter", LETTER_MODEL, LETTER_EFFORT, *_cost(LETTER_MODEL, usage))


TRIAGE_SYSTEM = """Ты помогаешь кандидату искать работу напрямую у компаний. Тебе дают ключевые слова его специальности и текст одной веб-страницы. Верни JSON:
{
 "page_type": "company" | "listing" | "job_channels" | "other",
 "relevant": true | false,
 "kind": "studio" | "outstaff" | "agency" | "product" | "",
 "name": "название компании, как на странице, иначе пустая строка",
 "stack_hint": "чем занимается в области кандидата, 2-6 слов, иначе пустая строка",
 "summary": "одно предложение по-русски только о компании: чем занимается и что предлагает. Не пиши, чем она полезна кандидату",
 "contact_name": "",
 "contact_role": ""
}
page_type: "company" страница одной компании; "listing" каталог, рейтинг или статья со списком многих студий и компаний; "job_channels" список Telegram-каналов или чатов с вакансиями; "other" всё остальное (новость, блог, магазин, вакансия на агрегаторе).
relevant = true, если компания разрабатывает или нанимает людей в области кандидата (студия, аутсорс, аутстафф, IT-агентство, продуктовая компания с такой разработкой), либо listing и job_channels явно посвящены этой области. Если компания к области кандидата отношения не имеет, relevant = false.
Если дан список «НЕ РАССМАТРИВАЕТ»: когда компания работает только с этими технологиями или направлениями и не упоминает область кандидата, relevant = false. Если рядом с ними она работает и в области кандидата, relevant может быть true.
kind: studio заказная разработка, outstaff аренда специалистов, agency рекрутинговое или IT-агентство, product компания с собственным продуктом.
contact_name и contact_role заполняй только если человек, отвечающий за разработку или найм (CTO, технический директор, руководитель разработки или направления кандидата, основатель), назван на странице прямо; иначе пустые строки. Ничего не выдумывай.
Верни только JSON."""

PITCH_SYSTEM = """Ты пишешь первое сообщение от соискателя человеку, который решает, нужен ли ему разработчик: техническому директору, руководителю разработки или основателю студии или компании. Это не отклик на вакансию, вакансии может не быть. Верни JSON {"message": "..."}.
Тебе дают профиль кандидата, данные о компании и вариант сообщения:
- staff: кандидат ищет постоянную работу или удалёнку и интересуется, бывают ли у компании позиции в его области;
- contract: кандидат готов брать проекты или усиливать команды на контракте или аутстаффе, и спрашивает, бывает ли у компании такая потребность;
- later: предложение оставить контакт: если команда сейчас укомплектована, кандидат просит сохранить его контакты на случай, когда понадобится человек.
Требования:
- 3-5 предложений, не больше 600 символов, без списков и markdown. По-русски, на «вы», просто, как инженер инженеру.
- Первая строка «Здравствуйте!» (если имя адресата известно, «<Имя>, здравствуйте!»). Дальше обычным абзацем.
- Сначала одним-двумя фактами из профиля скажи, кто ты и чем занимаешься. Если в профиле есть ссылки на опубликованные приложения или проекты, назови одну, дословно как в профиле. Затем одной фразой, чем это может быть полезно компании, исходя из её данных, без громких обещаний.
- Не пиши «закрою ваши задачи», «решу ваши проблемы», «увеличу выручку» и подобное. Не делай вид, что знаешь внутренние проблемы компании.
- Не перечисляй стек списком: назови максимум три самые важные технологии. Про проект скажи только, что он делает; не упоминай эксплуатацию и подробности (продакшен, сопровождение, число пользователей, один разработчик, сроки).
- Не пиши фразы вроде «вам может быть полезно» или «для вашего аутстаффа полезно»: просто скажи, что готов делать (брать проекты, усиливать команду), и задай вопрос.
- Используй только факты из профиля. Не называй сроки опыта ни цифрами, ни словами («два года», «около года», «несколько лет»). Не выдумывай проекты, ссылки, имена, цифры и технологии, которых нет в профиле. Любое «работаю с X» должно дословно опираться на профиль. Не обещай условий, которых нет в профиле.
- Не используй длинное и среднее тире (— и –), только обычный дефис и запятые.
- Закончи лёгким вопросом, на который просто ответить, по варианту сообщения. Без эмодзи, лести и шаблонов вроде «с большим интересом изучил ваш сайт». Верни только JSON."""


def triage_page(terms: list[str], url: str, title: str, text: str, exclude_terms: list[str] | None = None) -> tuple[dict, tuple]:
    """Outreach: what kind of page this is and whether the company is relevant to the candidate's field.
    exclude_terms (outreach.exclude_terms): technologies or directions the candidate does not consider."""
    avoid = f"НЕ РАССМАТРИВАЕТ: {', '.join(exclude_terms)}\n" if exclude_terms else ""
    user = (f"КЛЮЧЕВЫЕ СЛОВА СПЕЦИАЛЬНОСТИ КАНДИДАТА: {', '.join(terms) or 'не заданы'}\n{avoid}\n"
            f"СТРАНИЦА: {url}\nЗаголовок: {title}\nТекст: {text[:3500]}")
    out, usage = _post(SCREEN_MODEL, SCREEN_EFFORT, TRIAGE_SYSTEM, user)
    return out, ("triage", SCREEN_MODEL, SCREEN_EFFORT, *_cost(SCREEN_MODEL, usage))


def write_pitch(profile: str, target: dict, variant: str) -> tuple[str, tuple]:
    """Outreach: first message to a company's technical lead. variant: staff | contract | later."""
    who = target.get("contact_name") or ""
    user = gender_note() + (
        f"ПРОФИЛЬ КАНДИДАТА:\n{profile}\n\nКОМПАНИЯ: {target.get('name') or ''}\nТип: {target.get('kind') or ''}\n"
        f"Чем занимается: {target.get('stack_hint') or ''}. {target.get('summary') or ''}\n"
        f"Адресат: {who + ', ' + target['contact_role'] if who and target.get('contact_role') else who or 'имя неизвестно'}\n\n"
        f"ВАРИАНТ СООБЩЕНИЯ: {variant}"
    )
    out, usage = _post(LETTER_MODEL, LETTER_EFFORT, PITCH_SYSTEM, user)
    return plain_dashes(str(out.get("message", "")).strip()), ("pitch", LETTER_MODEL, LETTER_EFFORT, *_cost(LETTER_MODEL, usage))


def answer_questions(profile: str, card: str, description: str, ready_answers: str, question: str) -> tuple[list[tuple[str, str]], tuple]:
    user = gender_note() + (
        f"ПРОФИЛЬ КАНДИДАТА:\n{profile}\n\nГОТОВЫЕ ОТВЕТЫ:\n{ready_answers}\n\n"
        f"КАРТОЧКА ВАКАНСИИ (из бота):\n{card[:3000]}\n\n"
        f"ОПИСАНИЕ ВАКАНСИИ:\n{' '.join(description.split())[:3500]}\n\n"
        f"ВОПРОСЫ РАБОТОДАТЕЛЯ:\n{question[:3000]}"
    )
    out, usage = _post(LETTER_MODEL, LETTER_EFFORT, ANSWER_SYSTEM, user)
    items = [(str(i.get("question", "")).strip(), str(i.get("answer", "")).strip()) for i in out.get("items") or [] if isinstance(i, dict)]
    return items, ("answer", LETTER_MODEL, LETTER_EFFORT, *_cost(LETTER_MODEL, usage))
