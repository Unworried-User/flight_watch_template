"""Versturen via de Telegram Bot API."""
import re
from html import unescape

import requests

from .serpapi import schoon

MAX_LENGTE = 4000  # Telegram staat 4096 tekens per bericht toe


class TelegramFout(Exception):
    pass


def splits(tekst: str, max_lengte: int = MAX_LENGTE) -> list[str]:
    """Knipt op lege regels (tussen opties), zodat een optie niet over twee berichten valt."""
    delen, huidig = [], ""
    for blok in tekst.split("\n\n"):
        kandidaat = f"{huidig}\n\n{blok}" if huidig else blok
        if len(kandidaat) <= max_lengte:
            huidig = kandidaat
            continue
        if huidig:
            delen.append(huidig)
        while len(blok) > max_lengte:
            delen.append(blok[:max_lengte])
            blok = blok[max_lengte:]
        huidig = blok
    if huidig:
        delen.append(huidig)
    return delen


def stuur(token: str | None, chat_id: str | None, html: str, session: requests.Session | None = None,
          stil: bool = False, knoppen: list | None = None) -> None:
    """stil=True: het bericht komt gewoon binnen, maar de melding maakt geen geluid.
    knoppen: [[(tekst, url), ...], ...] linkknoppen onder het (laatste deel van het) bericht."""
    if not token or not chat_id:
        raise TelegramFout("TELEGRAM_TOKEN of TELEGRAM_CHAT_ID ontbreekt")
    s = session or requests.Session()
    delen = splits(html)
    for i, deel in enumerate(delen):
        bericht = {"chat_id": chat_id, "text": deel, "parse_mode": "HTML", "disable_web_page_preview": True,
                   # bij een opgesplitst rapport maakt alleen het eerste deel eventueel geluid
                   "disable_notification": stil or i > 0}
        if knoppen and i == len(delen) - 1:
            bericht["reply_markup"] = {"inline_keyboard": [[{"text": t, "url": u} for t, u in rij] for rij in knoppen]}
        try:
            r = s.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=30, json=bericht)
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            raise TelegramFout(schoon(f"Telegram onbereikbaar: {e}", token))
        if not data.get("ok"):
            raise TelegramFout(schoon(f"Telegram weigerde het bericht: {data.get('description')}", token))


def stuur_foto(token: str | None, chat_id: str | None, pad, onderschrift: str, stil: bool = True,
               session: requests.Session | None = None) -> None:
    if not token or not chat_id:
        raise TelegramFout("TELEGRAM_TOKEN of TELEGRAM_CHAT_ID ontbreekt")
    s = session or requests.Session()
    try:
        with open(pad, "rb") as f:
            data = s.post(f"https://api.telegram.org/bot{token}/sendPhoto", timeout=60,
                          data={"chat_id": chat_id, "caption": onderschrift[:1024], "parse_mode": "HTML",
                                "disable_notification": stil},
                          files={"photo": f}).json()
    except (requests.RequestException, ValueError) as e:
        raise TelegramFout(schoon(f"Telegram onbereikbaar: {e}", token))
    if not data.get("ok"):
        raise TelegramFout(schoon(f"Telegram weigerde de foto: {data.get('description')}", token))


def controleer(token: str | None, chat_id: str | None, session: requests.Session | None = None) -> None:
    """Controleert token en chat_id zonder een bericht te sturen (getMe en getChat)."""
    if not token or not chat_id:
        raise TelegramFout("TELEGRAM_TOKEN of TELEGRAM_CHAT_ID ontbreekt")
    s = session or requests.Session()
    for methode, params, uitleg in (("getMe", {}, "token ongeldig"),
                                    ("getChat", {"chat_id": chat_id}, "chat_id onbekend")):
        try:
            data = s.get(f"https://api.telegram.org/bot{token}/{methode}", params=params, timeout=30).json()
        except (requests.RequestException, ValueError) as e:
            raise TelegramFout(schoon(f"Telegram onbereikbaar: {e}", token))
        if not data.get("ok"):
            raise TelegramFout(schoon(f"Telegram: {uitleg} ({data.get('description')})", token))


def chat_ids(token: str) -> list[tuple[str, str]]:
    """Chats die de bot recent een bericht stuurden: [(chat_id, naam)]."""
    try:
        data = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30).json()
    except (requests.RequestException, ValueError) as e:
        raise TelegramFout(schoon(f"Telegram onbereikbaar: {e}", token))
    if not data.get("ok"):
        raise TelegramFout(schoon(f"Telegram: {data.get('description')}", token))
    gevonden = {}
    for u in data.get("result", []):
        chat = (u.get("message") or u.get("channel_post") or {}).get("chat")
        if chat:
            gevonden[str(chat["id"])] = chat.get("title") or chat.get("first_name") or chat.get("username") or ""
    return list(gevonden.items())


def naar_tekst(html: str) -> str:
    """Leesbare platte tekst voor de droge run: links als 'tekst <url>'."""
    tekst = re.sub(r'<a href="([^"]+)">([^<]+)</a>', r"\2 <\1>", html)
    tekst = re.sub(r"<blockquote[^>]*>", "  ▸ ", tekst).replace("</blockquote>", "")
    return unescape(re.sub(r"</?[bi]>", "", tekst))
