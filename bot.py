"""
Газобетон — Новостной дайджест бот (v3)
Красивый формат с комментариями AI и трендом дня.
"""

import asyncio
import logging
import os
import re
import json
import hashlib
from datetime import datetime, timedelta, timezone

import httpx
import feedparser
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot
from telegram.constants import ParseMode
import anthropic

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
DIGEST_HOUR       = int(os.environ.get("DIGEST_HOUR", "8"))
DIGEST_MINUTE     = int(os.environ.get("DIGEST_MINUTE", "0"))
SEEN_FILE         = "seen_items.json"

FEEDS = [
    "https://news.google.com/rss/search?q=%D0%B3%D0%B0%D0%B7%D0%BE%D0%B1%D0%B5%D1%82%D0%BE%D0%BD&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=%D0%B3%D0%B0%D0%B7%D0%BE%D0%B1%D0%B5%D1%82%D0%BE%D0%BD%D0%BD%D1%8B%D0%B5+%D0%B1%D0%BB%D0%BE%D0%BA%D0%B8&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=%D1%8F%D1%87%D0%B5%D0%B8%D1%81%D1%82%D1%8B%D0%B9+%D0%B1%D0%B5%D1%82%D0%BE%D0%BD&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=%D1%81%D1%82%D1%80%D0%BE%D0%B9%D0%BC%D0%B0%D1%82%D0%B5%D1%80%D0%B8%D0%B0%D0%BB%D1%8B+%D0%98%D0%96%D0%A1&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=%D0%B0%D0%B2%D1%82%D0%BE%D0%BA%D0%BB%D0%B0%D0%B2%D0%BD%D1%8B%D0%B9+%D0%B3%D0%B0%D0%B7%D0%BE%D0%B1%D0%B5%D1%82%D0%BE%D0%BD&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=%D0%98%D0%96%D0%A1+%D1%81%D1%82%D1%80%D0%BE%D0%B8%D1%82%D0%B5%D0%BB%D1%8C%D1%81%D1%82%D0%B2%D0%BE&hl=ru&gl=RU&ceid=RU:ru",
    "https://realty.ria.ru/export/rss2/archive/index.xml",
    "https://stroygaz.ru/rss/",
]

KEYWORDS = [
    "газобетон", "газоблок", "ячеистый бетон", "автоклав",
    "газобетонные блоки", "стеновой блок", "ижс", "газосиликат",
    "строительные блоки", "пеноблок", "кладка", "стройматериал",
    "малоэтажное", "загородный дом", "ипотека", "строительство дома",
]

NUMBERS = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
DIVIDER = "─────────────────"

def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def save_seen(seen: set) -> None:
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen), f, ensure_ascii=False)

def item_id(entry) -> str:
    uid = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.md5(uid.encode()).hexdigest()

def is_relevant(entry) -> bool:
    text = " ".join([entry.get("title",""), entry.get("summary","")]).lower()
    return any(kw in text for kw in KEYWORDS)

def entry_date(entry) -> datetime:
    for field in ("published_parsed", "updated_parsed"):
        t = entry.get(field)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return datetime.now(timezone.utc)

async def fetch_feed(url: str) -> list:
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; GazobetonBot/3.0)"})
            r.raise_for_status()
        return feedparser.parse(r.text).entries
    except Exception as e:
        log.warning("Не удалось загрузить %s: %s", url, e)
        return []

async def collect_candidates(seen: set) -> list:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    candidates = []
    results = await asyncio.gather(*[fetch_feed(url) for url in FEEDS])
    for entries in results:
        for entry in entries:
            uid = item_id(entry)
            if uid in seen:
                continue
            if entry_date(entry) < cutoff:
                continue
            candidates.append({
                "uid":     uid,
                "title":   entry.get("title", "").strip(),
                "link":    entry.get("link", ""),
                "summary": entry.get("summary", "")[:400].strip(),
                "date":    entry_date(entry).strftime("%d.%m.%Y"),
            })
    seen_titles = set()
    unique = []
    for c in candidates:
        t = c["title"].lower()[:60]
        if t not in seen_titles:
            seen_titles.add(t)
            unique.append(c)
    log.info("Всего уникальных новостей: %d", len(unique))
    return unique

def select_best_with_ai(candidates: list) -> list:
    if not candidates:
        return []
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    items_json = json.dumps(
        [{"i": i, "title": c["title"], "summary": c["summary"][:300]}
         for i, c in enumerate(candidates[:30])],
        ensure_ascii=False, indent=2
    )
    prompt = f"""Ты эксперт-аналитик для продавца автоклавных газобетонных блоков (Калуга и Московский регион, бренды Калужский газобетон и Бонолит).

Список свежих новостей:
{items_json}

Выбери от 1 до 10 самых полезных по темам:
рынок газобетона, цены на блоки, новые ГОСТы, строительство ИЖС, малоэтажное строительство, сравнение стройматериалов, новости производителей газобетона, спрос на стройматериалы, ипотека, загородное строительство.

Исключи: политику, криминал, спорт, IT без связи со стройкой.

Для каждой выбранной новости напиши деловой комментарий 2-3 предложения — что это значит для продавца газобетонных блоков.

Придумай Тренд дня — одну короткую фразу-вывод.

Ответь ТОЛЬКО валидным JSON:
{{"items":[{{"i":0,"comment":"текст"}},{{"i":3,"comment":"текст"}}],"trend":"Тренд дня"}}"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)
        result = []
        for entry in data.get("items", []):
            idx = entry.get("i")
            if idx is not None and 0 <= idx < len(candidates):
                item = dict(candidates[idx])
                item["comment"] = entry.get("comment", "")
                result.append(item)
        result = result[:10]
        if result:
            result[0]["_trend"] = data.get("trend", "")
        return result
    except Exception as e:
        log.error("Ошибка AI-отбора: %s", e)
        relevant = [c for c in candidates if is_relevant(c)]
        return relevant[:5] if relevant else candidates[:3]

def clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&amp;", "&").replace("&quot;", '"').replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return text.strip()

def safe_link(url: str) -> str:
    url = url.strip().replace('"', "").replace("'", "").replace(" ", "")
    if url.startswith("/"):
        url = "https://news.google.com" + url
    return url

def get_source(url: str) -> str:
    m = re.search(r"https?://(?:www\.)?([^/]+)", url)
    if m:
        domain = m.group(1)
        domain = re.sub(r"\.(ru|com|net|org|info)$", "", domain, flags=re.I)
        return domain
    return "источник"

def format_digest(items: list) -> str:
    today = datetime.now().strftime("%d.%m.%Y")
    trend = items[0].get("_trend", "") if items else ""
    parts = [
        "🧱 <b>ГАЗОБЕТОННЫЙ ДАЙДЖЕСТ</b>",
        today,
        DIVIDER,
    ]
    for i, item in enumerate(items):
        title   = clean(item.get("title", "Без заголовка"))
        link    = safe_link(item.get("link", ""))
        comment = clean(item.get("comment", ""))
        source  = get_source(link)
        num     = NUMBERS[i] if i < len(NUMBERS) else f"{i+1}."
        parts.append(f"{num} <b>{title}</b>")
        if comment:
            parts.append(comment)
        if link.startswith("http"):
            parts.append(f"🔗 {source} ({link})")
        parts.append(DIVIDER)
    if trend:
        parts.append(f"📌 <b>Тренд дня:</b> {clean(trend)}")
    return "\n".join(parts)

async def send_digest():
    log.info("Запускаю сборку дайджеста…")
    seen = load_seen()
    candidates = await collect_candidates(seen)
    log.info("Кандидатов собрано: %d", len(candidates))
    if not candidates:
        log.info("Новых новостей нет.")
        return
    selected = select_best_with_ai(candidates)
    log.info("Отобрано AI: %d новостей", len(selected))
    if not selected:
        log.info("AI не нашёл подходящих новостей.")
        return
    text = format_digest(selected)
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    log.info("Дайджест отправлен! (%d новостей)", len(selected))
    for item in selected:
        seen.add(item["uid"])
    save_seen(seen)

async def main():
    log.info("Бот запускается. Дайджест каждый день в %02d:%02d МСК", DIGEST_HOUR, DIGEST_MINUTE)
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(send_digest, trigger="cron", hour=DIGEST_HOUR, minute=DIGEST_MINUTE)
    scheduler.start()
    if os.environ.get("SEND_ON_START", "false").lower() == "true":
        log.info("SEND_ON_START=true — отправляю сейчас…")
        await send_digest()
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
