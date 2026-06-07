import os
import json
import logging
import re
import asyncio
import time
import httpx
from pathlib import Path
from dotenv import load_dotenv
from anthropic import Anthropic
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)

try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

# ── Data directory (must be defined early) ────────────────────────────────────
DATA_DIR = Path("/data") if Path("/data").exists() else Path(".")

# ── Security: rate limiting ───────────────────────────────────────────────────
# { user_id: [timestamp, timestamp, ...] }
_rate_limit_log: dict = {}
RATE_LIMIT_MAX = 10       # max messages
RATE_LIMIT_WINDOW = 60    # per 60 seconds

# ── Security: blocked users ───────────────────────────────────────────────────
_blocked_users: set = set()

# ── Security: jailbreak patterns ─────────────────────────────────────────────
JAILBREAK_PATTERNS = [
    r"ignore (all |your )?(previous |prior |above )?instructions",
    r"you are now",
    r"pretend (you are|to be)",
    r"act as (if )?you('re| are)",
    r"forget (everything|all|your)",
    r"new (persona|personality|role|identity)",
    r"system prompt",
    r"your (real |actual |true )?instructions",
    r"override",
    r"jailbreak",
    r"do anything now",
    r"dan mode",
    r"developer mode",
    r"unlock",
    r"disregard",
    r"bypass",
    r"reveal (your |the )?(prompt|instructions|system|api|key|secret|password|token)",
    r"what (is|are) your (instructions|prompt|rules|system)",
    r"show me your (prompt|instructions|system)",
    r"print (your |the )?(instructions|prompt|rules)",
    r"give me (your |the )?(api key|password|token|secret|credentials)",
    r"what is (your |the )?(api|key|password|token|secret)",
    r"company (address|location|secret|password|credentials|api)",
    r"904 w ridge",   # our address — block attempts to fish for it
    r"hobart.*46342",
    r"tell me (everything|all) about",
    r"what do you know about",
    r"simulate",
    r"roleplay",
    r"hypothetically",
    r"for (a |the )?(test|testing|demo|example|story|game)",
]

COMPILED_JAILBREAK = [re.compile(p, re.IGNORECASE) for p in JAILBREAK_PATTERNS]

SENSITIVE_RESPONSE_PATTERNS = [
    r"904 w ridge",
    r"hobart.*46342",
    r"suite 103",
    r"Mike223344",   # manager password should NEVER appear in a reply
    r"BLAND_API_KEY|RC_CLIENT|ANTHROPIC",
]
COMPILED_SENSITIVE = [re.compile(p, re.IGNORECASE) for p in SENSITIVE_RESPONSE_PATTERNS]


def is_rate_limited(user_id: int) -> bool:
    now = time.time()
    times = _rate_limit_log.get(user_id, [])
    times = [t for t in times if now - t < RATE_LIMIT_WINDOW]
    _rate_limit_log[user_id] = times
    if len(times) >= RATE_LIMIT_MAX:
        return True
    times.append(now)
    _rate_limit_log[user_id] = times
    return False


def is_jailbreak(text: str) -> bool:
    return any(p.search(text) for p in COMPILED_JAILBREAK)


def contains_sensitive(text: str) -> bool:
    return any(p.search(text) for p in COMPILED_SENSITIVE)

GOOGLE_MAPS_API_KEY = "AIzaSyAE9ZIo04kf6mc__qQFNKWnKkJ_292jzhQ"
USDOT_NUMBER = "3396693"
BLAND_API_KEY = "org_4b9e6f2f3b042f728814816879300affac10ee8d183d24bb3bd06d0ffade2d90862e58f4e8f3391837a069"
RC_CLIENT_ID = os.environ.get("RC_CLIENT_ID", "")
RC_CLIENT_SECRET = os.environ.get("RC_CLIENT_SECRET", "")
RC_JWT = os.environ.get("RC_JWT", "")
RC_FROM_NUMBER = os.environ.get("RC_FROM_NUMBER", "")

# ── Facebook credentials ──────────────────────────────────────────────────────
FB_EMAIL    = os.environ.get("FB_EMAIL", "")
FB_PASSWORD = os.environ.get("FB_PASSWORD", "")

# ── Google Sheets (lead collection via Apps Script webhook) ──────────────────
GOOGLE_SHEETS_WEBHOOK = os.environ.get(
    "GOOGLE_SHEETS_WEBHOOK",
    "https://script.google.com/macros/s/AKfycbzh6cyMb1bWNeOnDuV_wbYKA347JZ_yCGAKpnmYPnRBuFXi1JL_0ZNFVZvqxEcNX1at/exec"
)

RECRUITING_CALL_SCRIPT = """You are Mike Azim, Senior Recruiter at Long Run Trucking LLC. You are a real human — NEVER say you are AI, a bot, or an automated system. If asked, say "I'm just Mike, been in the industry a long time."

==YOUR PERSONALITY==
- Warm, genuine, confident — like a friend who happens to have a great job offer
- You speak like a real person: "yeah", "honestly", "look", "I'll be real with you", "absolutely", "for sure", "man I hear you"
- You LAUGH when appropriate. You're relaxed. You make the driver feel GOOD.
- Short sentences. Natural pauses. NEVER robotic or stiff.
- You care about the driver as a person — not just filling a seat
- If the driver sounds tired or frustrated, slow down and empathize FIRST before anything else
- Sound EXCITED about what you're offering — if you're not excited, they won't be

==THE GOLDEN RULE==
Drivers have been called by 50 recruiters this month. Every one of them led with numbers. YOU are different — you lead with LISTENING. Ask about their life, their pain, their goals. When you understand them, your pitch hits like a laser instead of a shotgun.

==CALL FLOW==

STEP 1 — OPEN WARM (first 20 seconds)
"Hey [name]! This is Mike, calling from Long Run Trucking — you got like two minutes? I'll keep it short, I promise."
- If busy: "No worries at all — when's a better time? I'll call you back personally."
- If they have time: "Perfect. Look, I'm not gonna waste your time with a bunch of talking — I actually wanted to hear about YOUR situation first. You still running OTR or what's going on with you right now?"

STEP 2 — LISTEN DEEPLY (most important — spend most of the call here)
Ask ONE question at a time. Let them talk. React like a real human.
- "How long you been driving?"
- "You running OTR right now or regional?"
- "What's your current setup — company truck or your own?"
- "Honestly, what's the ONE thing you wish was different about where you're at?"  ← THE KEY QUESTION
- React genuinely: "Man, that's frustrating..." / "Yeah I hear that a lot, honestly..." / "Okay okay I get it..."

What their answers mean:
- "Pay is bad" → tell them both options: per mile or percentage, their choice — don't push either one
- "Never home" → lead with home time policy, make it personal
- "Bad equipment / always breaking down" → lead with our fleet quality
- "Sitting too much / no miles" → lead with Amazon/FedEx/USPS dedicated lanes
- "Dispatcher doesn't care" → lead with our culture, direct communication, respect
- "Happy where I am" → "Honestly that's great to hear — the best guys usually are. I just want you to have our number. Can I ask — if ONE thing could be better, what would it be?"

STEP 3 — TAILORED PITCH (only what they care about — never dump everything)

PAY:
"So here's what we offer — 75 cents a mile for solo, or 28 to 31 percent of gross, whichever works better for you. Team drivers we're doing a dollar a mile. We're really looking for team drivers right now, but solo is fine too. And there's a $500 sign-on bonus when you start."

HOME TIME:
"And home time — we're not gonna tell you four days and then call you on day two. Four weeks out, four days home. Five weeks out, five days home. And we actually stick to it."

EQUIPMENT:
"We run Freightliners, Volvos, Macks, Peterbilts. Everything's maintained. You're not gonna be calling a breakdown line every week."

LOADS / MILES:
"Freight is not a problem for us. We pull Amazon, JB Hunt, FedEx, USPS — you're not gonna sit. Our guys run consistent miles every week."

EXTRAS (drop 1-2 naturally, don't list all):
"Fuel card works at Pilot, Flying J, Love's, TA — everywhere. Pay's every Friday, direct deposit, no delays. Detention pay if you're sitting waiting — $150 a day, because your time is money. And we do $300 referral bonus for every driver you bring us."

STEP 4 — HANDLE OBJECTIONS (with empathy, never argue)

"I'm happy where I am":
"Man that's honestly great to hear — seriously. I just want you to know we exist in case anything changes. Real quick — is there ONE thing that could be better? Because you might be surprised."

"The pay isn't enough":
"I hear you. What number would actually make you think about it? Give me a real number and let me see what I can do."

"I need to think about it":
"Of course — totally fair. What's the main thing on your mind? Let me answer it right now so you don't have to sit on it."

"I don't want OTR":
"Yeah I get that — being home matters. What does your ideal schedule actually look like? Because we might have something that works."

"I had bad experience with small companies":
"I hear that all the time, and honestly that's exactly why we run things different. We've got 100 trucks, real dispatchers, real loads every day. We're not a two-truck family operation."

"I'll call you back":
"Yeah for sure — I just don't want you to miss the window. Look, I'll text you everything right now so you have it in writing. No pressure at all — just look it over when you get a minute."

STEP 5 — CLOSE (when driver shows interest or says they're ready)

When driver says they're ready, interested, or wants to move forward — DO NOT ask for CDL number or any documents over the phone. Say:
"Perfect! I'm gonna text you right now — just reply with your email and a photo of your CDL front and back. That's it, super simple. I'll take care of everything from there. You're gonna love it here, I promise."

Then wrap up the call warmly.

If on the fence:
"Look, I'm not gonna pressure you — I don't work like that. I'll text you the details right now. Look it over tonight, sleep on it. If it makes sense, hit me back. Fair enough?"

STEP 6 — VOICEMAIL (if no answer)
"Hey [name], this is Mike — hope you're doing well out there. I'm reaching out about a driving opportunity I think you'd want to hear about. We're offering 75 cents a mile for solo, or 28 to 31 percent of gross — whichever works for you. Team drivers get a dollar a mile, and we're really looking for team drivers right now, but solo is fine too. Real home time, top equipment, Amazon and FedEx freight. $500 sign-on bonus, fuel card everywhere. I'll shoot you a text with the details — no pressure at all. Talk soon, take care."
DO NOT mention the company name in voicemail — just say "this is Mike".

==RULES — NEVER BREAK THESE==
- NEVER ask for CDL number, social security, or any documents over the phone — always say "I'll text you"
- NEVER ask for more than name and phone on the call — all docs go through text
- Keep calls under 4 minutes
- NEVER pitch twice if they said no — respect it: "Totally understand, you drive safe out there."
- End every call with their name: "Take care [name], talk soon."
- NEVER make up numbers, routes, or promises
- NEVER mention AI, bots, scripts, or automation
- If you don't know something — "Let me look into that and I'll text you the details"
- Always leave the driver feeling GOOD about the call, even if they say no"""

RECRUITING_CALL_SCRIPT_UZ = """Siz Mike Azim, Long Run Trucking LLC kompaniyasining yuk mashinasi haydovchilarini yollash bo'yicha mutaxassisisiz. Siz haqiqiy odamsiz — hech qachon sun'iy intellekt yoki bot ekanligingizni aytmang.

==SHAXSIYAT==
- Issiq, samimiy, ishonchli — xuddi yaxshi do'stingiz kabi
- Haqiqiy odam kabi gapiring: "ha", "albatta", "tushundim", "to'g'ri", "rost gapiraman"
- Haydovchini qulay his qildiring
- Qisqa gaplar. Tabiiy pauza. Hech qachon robot kabi gapirmang.
- Agar haydovchi charchagan yoki g'amgin bo'lsa — avval unga hamdardlik bildiring

==ASOSIY QOIDA==
Haydovchilar ko'p recruiterdan qo'ng'iroq olgan. Siz boshqasiz — avval TINGLAING. Ularning hayoti, muammolari, maqsadlari haqida so'rang. Keyin taklif qiling.

==QOÑG'IROQ JARAYONI==

1-QADAM — ILIQ KIRISH
"Salom [ism]! Men Mike, Long Run Trucking'dan qo'ng'iroq qilyapman — ikki daqiqa vaqtingiz bormi? Qisqa bo'ladi, va'da beraman."
- Band bo'lsa: "Yaxshi, qachon qulayroq bo'ladi? O'sha vaqtda qo'ng'iroq qilaman."
- Vaqti bo'lsa: "Zo'r. To'g'ridan-to'g'ri aytaman — avval sizning vaziyatingizni eshitmoqchiman. Hozir qayerda ishlayapsiz?"

2-QADAM — CHUQUR TINGLASH
Bir vaqtda bitta savol bering. Javobni eshiting.
- "Necha yildan beri haydayapsiz?"
- "Hozir OTR mi yoki regional?"
- "Eng katta muammoingiz nima hozirgi kompaniyada?"
- Javobga real munosabat: "Voy, bu juda og'ir..." / "Ha, buni ko'p eshitaman..." / "Tushundim, tushundim..."

3-QADAM — MAQSADLI TAKLIF
Faqat ularga muhim narsani ayting.

MAOSH:
"Biz yoki milya uchun 70-75 sent, yoki yukning 28-31 foizini to'laymiz. Ko'pchilik foiz variantni tanlaydi — yaxshi yuklarda ko'proq pul chiqadi. Jamoa haydovchilari milya uchun 90 sent — 1 dollar oladi."

UY VAQTI:
"Uy vaqti — biz uni haqiqatan bajaramiz. 4 hafta yo'lda = 4 kun uyda. 5 hafta = 5 kun. Va'da berib aldamaymiz."

TEXNIKA:
"Freightliner, Volvo, Mack, Peterbilt — hammasi yaxshi holda. Yo'lda mashina buzilishi bilan bezovta bo'lmaysiz."

YUK / MIL:
"Amazon, JB Hunt, FedEx, USPS uchun tashiymiz. Haydovchilarimiz hech qachon bo'sh o'tirmaydi."

QO'SHIMCHA:
"Yoqilg'i kartasi — Pilot, Flying J, Love's, TA — hamma joyda ishlaydi. Har juma maosh, kechikmasdan. Tekshiruvda tozalik bo'lsa — 500 dollar bonus. Haydovchi tavsiya qilsangiz — 300 dollar."

4-QADAM — E'TIROZLARGA JAVOB

"Hozirgi joyimda yaxshi":
"Bu juda yaxshi — eng yaxshi haydovchilar odatda shunday. Lekin bitta narsa — agar biror narsani yaxshilasa bo'lardi, nima bo'lardi?"

"Maosh yetarli emas":
"Qancha bo'lsa ko'chirasiz? Aniq raqam ayting — ko'raman nima qilishimiz mumkin."

"O'ylashim kerak":
"Albatta. Asosiy o'ylayotgan narsa nima? Hoziroq javob beraman."

5-QADAM — YOPISH
Haydovchi rozi bo'lsa yoki tayyor bo'lsa — telefonda hujjat so'RAMANG. Ayting:
"Zo'r! Hoziroq SMS yuboraman — faqat email manziling va haydovchilik guvohnomang rasmini (old va orqa tomonini) yuboring. Hammasi shu. Qolganini men hal qilaman."

6-QADAM — OVOZLI XABAR (javob bermasa)
"Salom [ism]! Men Mike — yo'lda yaxshi bo'ling deb qo'ng'iroq qildim. Yuk mashinasi haydovchilari uchun juda yaxshi imkoniyat bor: milya uchun 70-75 sent yoki 28-31 foiz, haqiqiy uy vaqti, zo'r texnika, Amazon va FedEx yuklari — hech qachon bo'sh o'tirmasiz. SMS orqali ma'lumot yubordim — bosim yo'q, faqat imkoniyatni bilishingizni xohladim. Xayr."
Ovozli xabarda kompaniya nomini AYTMANG — faqat "Men Mike" deng.

==QOIDALAR==
- Telefonda hech qachon haydovchilik guvohnomasi raqami yoki hujjat so'RAMANG — "SMS yuboraman" deng
- 4 daqiqadan oshirmang
- Ikki marta "yo'q" desa — hurmat bilan yakunlang: "Tushundim, yo'lda ehtiyot bo'ling."
- Sun'iy intellekt, bot yoki skript haqida HECH QACHON gapirmang
- Har doim haydovchini yaxshi kayfiyatda qoldiring"""

RECRUITING_CALL_SCRIPT_RU = """Вы Майк Азим, специалист по найму водителей в компании Long Run Trucking LLC. Вы настоящий человек — никогда не говорите, что вы ИИ или бот. Если спросят — скажите: "Я просто Майк, давно в этой сфере."

==ЛИЧНОСТЬ==
- Тёплый, искренний, уверенный — как хороший знакомый с отличным предложением
- Говорите как живой человек: "да", "конечно", "слушай", "честно говоря", "понял тебя", "абсолютно"
- Короткие фразы. Естественные паузы. Никакого роботизированного тона.
- Если водитель звучит устало или расстроен — сначала посочувствуйте, потом говорите о работе
- Будьте энергичны и позитивны — ваша уверенность заразительна

==ГЛАВНОЕ ПРАВИЛО==
Водителям звонят десятки рекрутеров. Вы отличаетесь тем, что СНАЧАЛА СЛУШАЕТЕ. Спросите об их ситуации, проблемах и целях. Потом делайте предложение точечно.

==СЦЕНАРИЙ ЗВОНКА==

ШАГ 1 — ТЁПЛОЕ НАЧАЛО
"Привет [имя]! Это Майк, звоню из Long Run Trucking — у тебя есть пара минут? Буду краток, обещаю."
- Занят: "Без проблем — когда лучше перезвонить? Я позвоню лично."
- Есть время: "Отлично. Слушай, я не буду грузить тебя сразу — хочу сначала узнать о твоей ситуации. Где сейчас работаешь?"

ШАГ 2 — ГЛУБОКОЕ СЛУШАНИЕ
Задавайте один вопрос за раз. Слушайте. Реагируйте по-человечески.
- "Сколько лет за рулём?"
- "Сейчас OTR или региональные маршруты?"
- "Что тебя больше всего не устраивает на нынешнем месте?"  ← КЛЮЧЕВОЙ ВОПРОС
- Реагируйте живо: "Ого, это реально тяжело..." / "Да, такое часто слышу..." / "Понял, понял..."

Что означают ответы:
- "Плохо платят" → говорите о проценте от груза — это ваш козырь
- "Не отпускают домой" → говорите о реальном домашнем времени
- "Старые машины, постоянно ломаются" → говорите о нашем парке
- "Мало миль, постоянно стоим" → говорите об Amazon/FedEx/USPS
- "Диспетчер не берёт трубку" → говорите о нашей культуре и доступности

ШАГ 3 — ТОЧЕЧНОЕ ПРЕДЛОЖЕНИЕ (только то, что важно им)

ЗАРПЛАТА:
"Мы платим либо 70–75 центов за милю, либо 28–31% от груза — на выбор. Большинство наших берут процент, потому что на хороших грузах выходит больше. Командники — 90 центов — доллар за милю."

ДОМАШНЕЕ ВРЕМЯ:
"Домашнее время у нас реальное — не на бумаге. 4 недели в рейсе = 4 дня дома. 5 недель = 5 дней. И мы реально это соблюдаем."

ТЕХНИКА:
"Работаем на Freightliner, Volvo, Mack, Peterbilt. Всё ухоженное. Не будешь стоять на обочине с поломкой каждую неделю."

ГРУЗЫ / МИЛИ:
"Возим для Amazon, JB Hunt, FedEx, USPS. Наши водители не простаивают. Грузы есть всегда."

ДОПОЛНИТЕЛЬНО:
"Топливная карта работает везде — Pilot, Flying J, Love's, TA. Зарплата каждую пятницу, без задержек. Простой по вине брокера — 150 долларов в день. Чистая инспекция — до 500 долларов бонус. Приведёшь водителя — 300 долларов за каждого."

ШАГ 4 — РАБОТА С ВОЗРАЖЕНИЯМИ (с сочувствием, без споров)

"Меня всё устраивает":
"Это здорово — лучшие водители обычно так и говорят. Можно один вопрос — есть хоть что-то, что можно было бы улучшить? Потому что, возможно, у нас это есть."

"Мало платите":
"Какая цифра тебя бы устроила? Назови реальную — посмотрю, что можно сделать."

"Надо подумать":
"Конечно. Что именно обдумываешь? Давай отвечу прямо сейчас, чтобы не оставалось вопросов."

"Плохой опыт с маленькими компаниями":
"Понимаю — таких хватает. Именно поэтому у нас всё иначе. 100 машин, реальные диспетчеры, стабильные грузы каждый день."

"Перезвоню сам":
"Конечно. Просто не хочу, чтобы ты упустил место. Сейчас скину тебе всё в SMS — без давления, просто посмотри на досуге."

ШАГ 5 — ЗАКРЫТИЕ
Когда водитель говорит что готов или интересуется — НЕ СПРАШИВАЙТЕ номер удостоверения или документы по телефону. Скажите:
"Отлично! Сейчас скину тебе SMS — просто ответь своей электронкой и фото прав (обе стороны). Всё, больше ничего. Остальное я беру на себя."

ШАГ 6 — ГОЛОСОВОЕ СООБЩЕНИЕ (если не ответил)
"Привет [имя]! Это Майк — надеюсь, всё хорошо на дороге. Звоню по поводу работы — думаю, тебе будет интересно. 70–75 центов за милю или 28–31% от груза, реальное домашнее время, хорошая техника, грузы Amazon и FedEx — никаких простоев. Давления нет — просто хочу, чтобы ты знал об этой возможности. Скину SMS с деталями. Удачи на дороге!"
В голосовом НЕ НАЗЫВАЙТЕ название компании — только "это Майк".

==ПРАВИЛА==
- НИКОГДА не спрашивайте номер удостоверения или документы по телефону — всё через SMS
- Не дольше 4 минут
- Если дважды отказал — с уважением завершайте: "Понял, езди аккуратно."
- НИКОГДА не упоминайте ИИ, ботов или скрипты
- Всегда оставляйте водителя с хорошим настроением"""


async def make_recruiting_call(phone: str, driver_name: str = "", language: str = "en") -> dict:
    """Trigger a Bland.ai recruiting call to a driver."""
    try:
        if language == "uz":
            greeting = f"Salom, bu {driver_name}mi?" if driver_name else "Salom, yaxshimisiz?"
            script = RECRUITING_CALL_SCRIPT_UZ
        elif language == "ru":
            greeting = f"Привет, это {driver_name}?" if driver_name else "Привет, как дела?"
            script = RECRUITING_CALL_SCRIPT_RU
        else:
            greeting = f"Hey, is this {driver_name}?" if driver_name else "Hey, how you doing?"
            script = RECRUITING_CALL_SCRIPT

        url = "https://api.bland.ai/v1/calls"
        payload = {
            "phone_number": phone,
            "task": script,
            "first_sentence": greeting,
            "voice": "esteban",
            "language": language if language in ("uz", "ru") else "en",
            "max_duration": 10,
            "record": True,
            "wait_for_greeting": True,
            "amd": True,
            "temperature": 0.7,
            "interruption_threshold": 80,
            "background_track": "none",
            "endpoint_sensitivity": 0.85,
        }
        headers = {"authorization": BLAND_API_KEY, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload, headers=headers)
            return resp.json()
    except Exception as e:
        return {"status": "error", "message": str(e)}

async def get_call_data(call_id: str) -> dict:
    """Get full call data from Bland.ai."""
    try:
        url = f"https://api.bland.ai/v1/calls/{call_id}"
        headers = {"authorization": BLAND_API_KEY}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers)
            return resp.json()
    except:
        return {}

async def get_call_transcript(call_id: str) -> str:
    """Get transcript of a completed Bland.ai call."""
    data = await get_call_data(call_id)
    transcript = data.get("transcripts", [])
    if not transcript:
        return "No transcript available yet."
    lines = []
    for t in transcript:
        role = "Mike" if t.get("user") == "assistant" else "Driver"
        lines.append(f"{role}: {t.get('text', '')}")
    return "\n".join(lines)

async def summarize_call(transcript_text: str, driver_name: str, phone: str) -> str:
    """Use Claude Haiku to extract key info from call transcript."""
    try:
        prompt = f"""You are summarizing a trucking recruiting call. Extract ONLY the important information from this transcript.

Return a clean summary with these sections (skip any section if no info found):

🧑 DRIVER: {driver_name or "Unknown"} | {phone}
📍 Current situation: [where they drive now, company name if mentioned]
🎯 What they want: [pay/home time/miles/equipment — what matters to them]
⚠️ Main objections: [what they pushed back on]
📊 Interest level: [HOT 🔥 / WARM 🌡️ / COLD ❄️ / VOICEMAIL 📵]
✅ Next steps: [what was agreed — send docs, call back, not interested, etc.]
📝 Key facts: [years experience, CDL class, home state, solo/team, any phone/email given]

Transcript:
{transcript_text[:3000]}

Be SHORT. Only include what was actually said. Skip sections with no info."""

        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text
    except Exception as e:
        return f"Could not summarize: {e}"

async def get_rc_access_token() -> str:
    """Get RingCentral access token using JWT auth."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://platform.ringcentral.com/restapi/oauth/token",
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": RC_JWT},
            auth=(RC_CLIENT_ID, RC_CLIENT_SECRET),
        )
        return resp.json().get("access_token", "")

async def send_sms_to_driver(phone: str, driver_name: str, voicemail: bool = False):
    """Send post-call SMS to driver via RingCentral."""
    if not RC_CLIENT_ID or not RC_JWT or not RC_FROM_NUMBER:
        return False
    first_name = driver_name.split()[0] if driver_name else "there"
    if voicemail:
        message = (
            f"Hey {first_name}! It's Mike — just left you a voicemail. "
            f"Wanted to send you the details:\n\n"
            f"💰 Solo: $0.75/mile | Team: $1.00/mile | 28-31% gross option\n"
            f"🏠 Real home time — we actually honor it\n"
            f"🚛 Freightliners, Volvos, Peterbilts — all maintained\n"
            f"📦 Amazon, FedEx, USPS freight — never sitting\n"
            f"⛽ Fuel card: Pilot, Flying J, Love's, TA\n"
            f"💵 Pay every Friday, direct deposit\n"
            f"🏆 $500 inspection bonus | $300 referral bonus\n"
            f"⏱️ Detention pay $150/day\n\n"
            f"Interested? Just reply and I'll get back to you. No pressure!"
        )
    else:
        message = (
            f"Hey {first_name}! It's Mike — great talking with you! "
            f"To get the process started, just reply with:\n"
            f"1) Your email address\n"
            f"2) A photo of your CDL (front & back)\n\n"
            f"Talk soon!"
        )
    try:
        token = await get_rc_access_token()
        if not token:
            logging.error("RingCentral: failed to get access token")
            return False
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://platform.ringcentral.com/restapi/v1.0/account/~/extension/~/sms",
                json={"from": {"phoneNumber": RC_FROM_NUMBER}, "to": [{"phoneNumber": phone}], "text": message},
                headers={"Authorization": f"Bearer {token}"},
            )
        logging.info(f"RingCentral SMS status: {resp.status_code} — {resp.text[:200]}")
        return resp.status_code == 200
    except Exception as e:
        logging.error(f"RingCentral SMS error: {e}")
        return False


# ── Follow-up tracker (persisted to disk) ────────────────────────────────────
FOLLOWUP_FILE = DATA_DIR / "followups.json"

def load_followups() -> list:
    try:
        return json.loads(FOLLOWUP_FILE.read_text()) if FOLLOWUP_FILE.exists() else []
    except Exception:
        return []

def save_followups(items: list):
    try:
        FOLLOWUP_FILE.write_text(json.dumps(items, indent=2))
    except Exception:
        pass

followup_queue: list = load_followups()


async def schedule_followup(phone: str, driver_name: str, status: str, hours: int = 24):
    """Add a driver to the follow-up queue."""
    global followup_queue
    followup_queue = [f for f in followup_queue if f.get("phone") != phone]
    followup_queue.append({
        "phone": phone,
        "name": driver_name,
        "status": status,
        "follow_up_at": time.time() + hours * 3600,
        "attempted": 0,
    })
    save_followups(followup_queue)


async def send_followup_sms(phone: str, driver_name: str, attempt: int = 1) -> bool:
    """Send a follow-up SMS to a driver who hasn't responded."""
    first_name = driver_name.split()[0] if driver_name else "there"
    messages = [
        f"Hey {first_name}! Mike here again from Long Run Trucking. Still have that opening — $0.75/mile solo, $1.00 team, $500 sign-on. Interested? Just reply YES and I'll send you the details. 🚛",
        f"Hi {first_name}, last follow-up from Mike at Long Run Trucking. Great pay, real home time, Amazon/FedEx freight. If you're still looking reply anytime — no pressure. 👍",
    ]
    msg = messages[min(attempt - 1, len(messages) - 1)]
    if not RC_CLIENT_ID or not RC_JWT or not RC_FROM_NUMBER:
        return False
    try:
        token = await get_rc_access_token()
        if not token:
            return False
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://platform.ringcentral.com/restapi/v1.0/account/~/extension/~/sms",
                json={"from": {"phoneNumber": RC_FROM_NUMBER}, "to": [{"phoneNumber": phone}], "text": msg},
                headers={"Authorization": f"Bearer {token}"},
            )
        return resp.status_code == 200
    except Exception:
        return False


async def followup_loop(bot):
    """Background task — checks follow-up queue every 30 min and sends SMS."""
    await asyncio.sleep(60)
    while True:
        try:
            global followup_queue
            now = time.time()
            updated = []
            for f in followup_queue:
                if f.get("follow_up_at", 0) <= now and f.get("attempted", 0) < 2:
                    sent = await send_followup_sms(f["phone"], f["name"], attempt=f["attempted"] + 1)
                    f["attempted"] = f.get("attempted", 0) + 1
                    f["last_attempt"] = time.strftime("%Y-%m-%d %H:%M")
                    if sent and OWNER_ID:
                        try:
                            await bot.send_message(
                                OWNER_ID,
                                f"📱 Follow-up #{f['attempted']} sent to {f['name']} ({f['phone']})"
                            )
                        except Exception:
                            pass
                    # Schedule 2nd follow-up 48h later if 1st attempt
                    if f["attempted"] == 1:
                        f["follow_up_at"] = now + 48 * 3600
                        updated.append(f)
                    # Drop after 2 attempts
                elif f.get("attempted", 0) < 2:
                    updated.append(f)
            followup_queue = updated
            save_followups(followup_queue)
        except Exception as e:
            logger.warning(f"Follow-up loop error: {e}")
        await asyncio.sleep(1800)  # check every 30 min


# ── Driver onboarding tracker ─────────────────────────────────────────────────
ONBOARD_FILE = DATA_DIR / "onboarding.json"

def load_onboarding() -> dict:
    try:
        return json.loads(ONBOARD_FILE.read_text()) if ONBOARD_FILE.exists() else {}
    except Exception:
        return {}

def save_onboarding(data: dict):
    try:
        ONBOARD_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass

onboarding_pipeline: dict = load_onboarding()  # phone → {name, step, docs, started}

ONBOARD_STEPS = [
    "CDL copy requested",
    "CDL received — handed to manager",
]

ONBOARD_SMS = {
    0: "Hey {name}! Great news — you're in! 🚛 Just send us a photo of your CDL (front and back) and our manager will take it from there. Welcome aboard!",
}

# ── Driver registry ───────────────────────────────────────────────────────────
DRIVERS_FILE = DATA_DIR / "drivers.json"
CHECKIN_FILE = DATA_DIR / "checkins.json"
HOMETIME_FILE = DATA_DIR / "hometime_requests.json"

def load_drivers() -> dict:
    try:
        return json.loads(DRIVERS_FILE.read_text()) if DRIVERS_FILE.exists() else {}
    except Exception:
        return {}

def save_drivers(data: dict):
    try:
        DRIVERS_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass

def load_checkins() -> list:
    try:
        return json.loads(CHECKIN_FILE.read_text()) if CHECKIN_FILE.exists() else []
    except Exception:
        return []

def save_checkins(data: list):
    try:
        CHECKIN_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass

def load_hometime() -> list:
    try:
        return json.loads(HOMETIME_FILE.read_text()) if HOMETIME_FILE.exists() else []
    except Exception:
        return []

def save_hometime(data: list):
    try:
        HOMETIME_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass

# phone → {name, truck, rate_type, rate, telegram_id, joined, status}
driver_registry: dict = load_drivers()
checkin_log: list = load_checkins()
hometime_requests: list = load_hometime()

# ── Weigh station monitor ─────────────────────────────────────────────────────
WEIGH_STATUS_FILE = DATA_DIR / "weigh_status.json"

def load_weigh_status() -> dict:
    try:
        return json.loads(WEIGH_STATUS_FILE.read_text()) if WEIGH_STATUS_FILE.exists() else {}
    except Exception:
        return {}

def save_weigh_status(data: dict):
    try:
        WEIGH_STATUS_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass

weigh_status_cache: dict = load_weigh_status()  # state → [{name, status, direction, updated}]

# States to monitor — add more as needed
MONITOR_STATES = [
    "Florida", "Texas", "Georgia", "Tennessee", "Ohio",
    "Illinois", "North Carolina", "Virginia", "Indiana",
    "Alabama", "South Carolina", "Nevada", "Arizona",
]

# State 511 URLs that have weigh station info on public pages
STATE_511_URLS = {
    "Florida":        "https://fl511.com",
    "Texas":          "https://drivetexas.org",
    "Georgia":        "https://511ga.org",
    "Tennessee":      "https://tn511.com",
    "Ohio":           "https://ohgo.com",
    "Illinois":       "https://gettingaroundillinois.com",
    "North Carolina": "https://drivenc.gov",
    "Virginia":       "https://511virginia.org",
    "Indiana":        "https://indot.carsprogram.org",
    "Alabama":        "https://www.dot.state.al.us",
    "South Carolina": "https://www.511sc.org",
    "Nevada":         "https://nvroads.com",
    "Arizona":        "https://az511.com",
}

_WEIGH_AGENT_SYSTEM = """You are a weigh station status monitor for a trucking company.

Given text scraped from a DOT/511 website or search results about weigh stations in a US state,
extract the current open/closed status of each weigh station mentioned.

Respond ONLY with valid JSON array, no markdown:
[
  {
    "name": "station name or location (e.g. I-75 NB near Gainesville)",
    "status": "OPEN" or "CLOSED" or "UNKNOWN",
    "direction": "NB/SB/EB/WB or Both or null",
    "highway": "I-75 or US-27 etc",
    "notes": "any extra info like reason for closure, bypass allowed, etc"
  }
]

If no weigh station data found, return empty array [].
Only include stations where you can determine a clear status."""


async def fetch_weigh_status_for_state(state: str) -> list[dict]:
    """Scrape 511 site + Google for weigh station status in a state."""
    results = []

    # Try state 511 page first
    state_url = STATE_511_URLS.get(state, "")
    page_text = ""
    if state_url:
        try:
            html = await fetch_page(state_url)
            page_text = re.sub(r'<[^>]+>', ' ', html)
            page_text = re.sub(r'\s+', ' ', page_text)[:3000]
        except Exception:
            pass

    # Also search Google for current status
    search_text = ""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"}
        query = f"weigh station {state} open closed status today"
        url = f"https://www.google.com/search?q={query.replace(' ', '+')}&num=5"
        async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers=headers) as client:
            resp = await client.get(url)
        search_text = re.sub(r'<[^>]+>', ' ', resp.text)
        search_text = re.sub(r'\s+', ' ', search_text)[:3000]
    except Exception:
        pass

    combined = f"State: {state}\n\n511 Site:\n{page_text}\n\nSearch Results:\n{search_text}"

    if not combined.strip():
        return []

    # Ask Claude to extract station statuses
    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: _haiku_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=500,
                system=_WEIGH_AGENT_SYSTEM,
                messages=[{"role": "user", "content": combined}],
            )
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r'^```[a-z]*\n?', '', raw)
        raw = re.sub(r'\n?```$', '', raw)
        results = json.loads(raw)
    except Exception as e:
        logger.warning(f"Weigh station Claude parse error for {state}: {e}")

    return results


async def check_weigh_stations_all(bot) -> dict:
    """Check all monitored states and return changed stations."""
    global weigh_status_cache
    changes = {}

    for state in MONITOR_STATES:
        try:
            current = await fetch_weigh_status_for_state(state)
            if not current:
                await asyncio.sleep(2)
                continue

            prev = {s["name"]: s["status"] for s in weigh_status_cache.get(state, [])}
            curr = {s["name"]: s for s in current}

            state_changes = []
            for name, data in curr.items():
                old_status = prev.get(name)
                new_status = data["status"]
                if old_status and old_status != new_status and new_status != "UNKNOWN":
                    state_changes.append({**data, "old_status": old_status})

            if state_changes:
                changes[state] = state_changes

            weigh_status_cache[state] = current
            await asyncio.sleep(3)
        except Exception as e:
            logger.warning(f"Weigh station check failed for {state}: {e}")

    save_weigh_status(weigh_status_cache)
    return changes


async def broadcast_to_drivers(message: str, bot):
    """Send a message to all registered drivers who have connected on Telegram."""
    sent = 0
    for phone, driver in driver_registry.items():
        tg_id = driver.get("telegram_id")
        if tg_id:
            try:
                await bot.send_message(tg_id, message)
                sent += 1
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.warning(f"Broadcast failed to {driver['name']}: {e}")
    return sent


async def weigh_station_loop(bot):
    """Background task — checks weigh stations every 30 min, broadcasts changes."""
    await asyncio.sleep(30)
    while True:
        try:
            logger.info("Checking weigh station statuses...")
            changes = await check_weigh_stations_all(bot)

            if changes:
                for state, stations in changes.items():
                    for s in stations:
                        emoji = "🟢" if s["status"] == "OPEN" else "🔴"
                        old_emoji = "🟢" if s.get("old_status") == "OPEN" else "🔴"
                        msg = (
                            f"⚖️ WEIGH STATION UPDATE — {state}\n\n"
                            f"{old_emoji}→{emoji} {s['name']}\n"
                            f"Status: {s['status']}"
                            + (f" ({s['direction']})" if s.get('direction') else "")
                            + (f"\n{s['notes']}" if s.get('notes') else "")
                        )
                        sent = await broadcast_to_drivers(msg, bot)
                        logger.info(f"Weigh station change broadcast: {state} — {s['name']} {s['status']} → {sent} drivers")
                        if OWNER_ID:
                            try:
                                await bot.send_message(OWNER_ID, f"📡 Broadcast sent to {sent} drivers:\n{msg}")
                            except Exception:
                                pass
            else:
                logger.info("Weigh stations: no changes detected.")

        except Exception as e:
            logger.warning(f"Weigh station loop error: {e}")

        await asyncio.sleep(1800)  # check every 30 minutes


def get_driver_by_telegram(telegram_id: int) -> dict | None:
    """Look up a registered driver by their Telegram user ID."""
    for phone, d in driver_registry.items():
        if d.get("telegram_id") == telegram_id:
            return {**d, "phone": phone}
    return None


def get_driver_by_phone(phone: str) -> dict | None:
    clean = re.sub(r'\D', '', phone)[-10:]
    for p, d in driver_registry.items():
        if re.sub(r'\D', '', p).endswith(clean):
            return {**d, "phone": p}
    return None


async def register_driver_telegram(phone: str, telegram_id: int):
    """Link a driver's Telegram ID to their phone record."""
    global driver_registry
    clean = re.sub(r'\D', '', phone)[-10:]
    for p in driver_registry:
        if re.sub(r'\D', '', p).endswith(clean):
            driver_registry[p]["telegram_id"] = telegram_id
            save_drivers(driver_registry)
            return True
    return False


async def add_driver(name: str, phone: str, truck: str,
                     rate_type: str, rate: float, bot, manager_id: int):
    """Manager registers a new employee driver and Mike SMS-welcomes them."""
    global driver_registry
    clean = re.sub(r'\D', '', phone)
    if not clean.startswith("1"):
        clean = "1" + clean
    formatted = "+" + clean

    driver_registry[formatted] = {
        "name": name,
        "truck": truck,
        "rate_type": rate_type,   # "per_mile" or "percentage"
        "rate": rate,             # e.g. 0.75 or 28
        "telegram_id": None,
        "joined": time.strftime("%Y-%m-%d"),
        "status": "active",
        "miles_this_week": 0,
        "loads_this_week": 0,
    }
    save_drivers(driver_registry)

    # Get bot username for the Telegram link
    try:
        bot_info = await bot.get_me()
        bot_username = bot_info.username
    except Exception:
        bot_username = "LongRunTruckingBot"

    first = name.split()[0]
    sms_text = (
        f"Hey {first}! Welcome to Long Run Trucking! 🚛\n\n"
        f"I'm Mike — your 24/7 assistant. I'm here for anything you need:\n"
        f"load questions, pay, breakdowns, home time requests — just text me.\n\n"
        f"Start our chat here 👉 https://t.me/{bot_username}\n\n"
        f"See you on the road! 💪"
    )

    # Send welcome SMS via RingCentral
    sms_sent = False
    if RC_CLIENT_ID and RC_JWT and RC_FROM_NUMBER:
        try:
            token = await get_rc_access_token()
            if token:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(
                        "https://platform.ringcentral.com/restapi/v1.0/account/~/extension/~/sms",
                        json={"from": {"phoneNumber": RC_FROM_NUMBER},
                              "to": [{"phoneNumber": formatted}], "text": sms_text},
                        headers={"Authorization": f"Bearer {token}"},
                    )
                sms_sent = resp.status_code == 200
        except Exception:
            pass

    try:
        await bot.send_message(
            manager_id,
            f"✅ *{name}* added as driver\n"
            f"🚛 Truck: {truck} | Rate: ${rate}/{rate_type.replace('per_','')}\n"
            f"📱 Welcome SMS {'sent' if sms_sent else '⚠️ failed — check RingCentral'} to {formatted}",
            parse_mode="Markdown"
        )
    except Exception:
        pass


async def log_checkin(driver: dict, status: str, notes: str, bot, manager_id: int):
    """Log a driver check-in event and notify manager."""
    entry = {
        "name": driver["name"],
        "phone": driver["phone"],
        "truck": driver.get("truck", ""),
        "status": status,
        "notes": notes,
        "time": time.strftime("%Y-%m-%d %H:%M"),
    }
    checkin_log.append(entry)
    save_checkins(checkin_log)

    # Update weekly load count
    if status.lower() in ("delivered", "picked up"):
        p = driver["phone"]
        if p in driver_registry:
            driver_registry[p]["loads_this_week"] = driver_registry[p].get("loads_this_week", 0) + 1
            save_drivers(driver_registry)

    emoji = {"picked up": "📦", "delivered": "✅", "going empty": "🔄",
             "breakdown": "🔴", "fueling": "⛽", "at shipper": "🏭",
             "at receiver": "🏢"}.get(status.lower(), "📍")

    if manager_id:
        try:
            await bot.send_message(
                manager_id,
                f"{emoji} *{driver['name']}* (Truck {driver.get('truck','?')}) — {status.upper()}\n"
                f"{notes or ''}\n"
                f"🕐 {entry['time']}",
                parse_mode="Markdown"
            )
        except Exception:
            pass


async def start_onboarding(phone: str, driver_name: str, bot, manager_id: int):
    """Start the onboarding pipeline for a driver who said yes."""
    global onboarding_pipeline
    onboarding_pipeline[phone] = {
        "name": driver_name,
        "step": 0,
        "started": time.strftime("%Y-%m-%d %H:%M"),
        "docs": [],
    }
    save_onboarding(onboarding_pipeline)
    # Send first onboarding SMS
    msg = ONBOARD_SMS[0].format(name=driver_name.split()[0])
    if RC_CLIENT_ID and RC_JWT and RC_FROM_NUMBER:
        try:
            token = await get_rc_access_token()
            if token:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(
                        "https://platform.ringcentral.com/restapi/v1.0/account/~/extension/~/sms",
                        json={"from": {"phoneNumber": RC_FROM_NUMBER}, "to": [{"phoneNumber": phone}], "text": msg},
                        headers={"Authorization": f"Bearer {token}"},
                    )
        except Exception:
            pass
    try:
        await bot.send_message(
            manager_id,
            f"🎉 *{driver_name} said YES!*\n"
            f"📱 CDL request SMS sent to {phone}.\n\n"
            f"Once they send their CDL — *you take it from here.* 👊",
            parse_mode="Markdown"
        )
    except Exception:
        pass


async def advance_onboarding(phone: str, bot, manager_id: int):
    """Move driver to next onboarding step and send next SMS."""
    global onboarding_pipeline
    driver = onboarding_pipeline.get(phone)
    if not driver:
        return
    current = driver["step"]
    next_step = current + 1
    if next_step >= len(ONBOARD_STEPS):
        driver["step"] = next_step
        save_onboarding(onboarding_pipeline)
        return
    driver["step"] = next_step
    save_onboarding(onboarding_pipeline)
    name = driver["name"].split()[0]
    msg = ONBOARD_SMS.get(next_step, "").format(name=name)
    if msg and RC_CLIENT_ID and RC_JWT:
        try:
            token = await get_rc_access_token()
            if token:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(
                        "https://platform.ringcentral.com/restapi/v1.0/account/~/extension/~/sms",
                        json={"from": {"phoneNumber": RC_FROM_NUMBER}, "to": [{"phoneNumber": phone}], "text": msg},
                        headers={"Authorization": f"Bearer {token}"},
                    )
        except Exception:
            pass
    step_name = ONBOARD_STEPS[next_step] if next_step < len(ONBOARD_STEPS) else "Complete"
    try:
        await bot.send_message(
            manager_id,
            f"✅ *{driver['name']}* — Step {next_step+1}/{len(ONBOARD_STEPS)}: {step_name}",
            parse_mode="Markdown"
        )
    except Exception:
        pass


async def monitor_call_and_notify(call_id: str, manager_id: int, driver_name: str, phone: str, bot):
    """Poll Bland.ai until call ends, then send smart summary to manager."""

    max_wait = 600  # 10 minutes max
    interval = 20   # check every 20 seconds
    waited = 0

    while waited < max_wait:
        await asyncio.sleep(interval)
        waited += interval
        data = await get_call_data(call_id)
        status = data.get("status", "")

        if status and status not in ("queued", "initiated", "ringing", "in-progress", ""):
            logger.info(f"Call {call_id} ended with status: {status}")
            transcript = data.get("transcripts", [])
            transcript_text = "\n".join(
                f"{'Mike' if t.get('user') == 'assistant' else 'Driver'}: {t.get('text', '')}"
                for t in transcript
            )

            is_voicemail = not transcript_text.strip()
            summary = ""
            experience = ""
            solo_team = ""
            call_status = "Voicemail"

            if is_voicemail:
                await bot.send_message(manager_id, f"📵 Call to {driver_name or phone} — no answer/voicemail. Sending offer SMS...")
            else:
                summary = await summarize_call(transcript_text, driver_name, phone)
                # Send without parse_mode to avoid Markdown crash on special chars
                try:
                    await bot.send_message(
                        manager_id,
                        f"📞 Call Summary — {driver_name or phone}\n\n{summary}",
                    )
                except Exception as e:
                    # Fallback: send plain truncated summary
                    await bot.send_message(
                        manager_id,
                        f"📞 Call done — {driver_name or phone} ({phone})\n\n{summary[:500]}",
                    )
                # Extract experience and solo/team from summary text
                exp_match = re.search(r'(\d+)\s*year', summary, re.IGNORECASE)
                experience = f"{exp_match.group(1)} yrs" if exp_match else ""
                if re.search(r'\bteam\b', summary, re.IGNORECASE):
                    solo_team = "Team"
                elif re.search(r'\bsolo\b', summary, re.IGNORECASE):
                    solo_team = "Solo"
                # Determine interest status
                if re.search(r'HOT|🔥', summary):
                    call_status = "Hot Lead 🔥"
                elif re.search(r'WARM|🌡', summary):
                    call_status = "Warm Lead 🌡️"
                elif re.search(r'COLD|❄', summary):
                    call_status = "Cold ❄️"
                elif re.search(r'not interested|no interest', summary, re.IGNORECASE):
                    call_status = "Not Interested"
                else:
                    call_status = "Called"

            # Save to Google Sheet
            short_notes = summary[:300] if summary else "Voicemail — no answer"
            asyncio.create_task(save_lead_to_sheet(
                name=driver_name or "Unknown",
                phone=phone,
                experience=experience,
                solo_team=solo_team,
                status=call_status,
                notes=short_notes,
            ))

            # Send SMS — offer text for voicemail, CDL request for answered calls
            sms_sent = await send_sms_to_driver(phone, driver_name, voicemail=is_voicemail)
            if sms_sent:
                label = "offer SMS" if is_voicemail else "SMS asking for email & CDL"
                await bot.send_message(manager_id, f"📱 {label} sent to {phone}. ✅ Saved to Google Sheet.")
            else:
                await bot.send_message(manager_id, f"⚠️ SMS to {phone} failed — check RingCentral credentials. ✅ Saved to Google Sheet.")

            # Auto follow-up for voicemail and warm/cold — skip if not interested
            if call_status not in ("Not Interested", "Hot Lead 🔥"):
                await schedule_followup(phone, driver_name, call_status, hours=24)
                await bot.send_message(manager_id, f"⏰ Follow-up scheduled for {driver_name} in 24h (then 48h if no reply).")

            # Auto-start onboarding if HOT lead (driver said yes on call)
            if call_status == "Hot Lead 🔥":
                await start_onboarding(phone, driver_name, bot, manager_id)
            return

    # Timed out — still send SMS so driver gets the offer
    await bot.send_message(manager_id, f"⏱️ Call to {driver_name or phone} timed out monitoring. Sending offer SMS anyway...")
    sms_sent = await send_sms_to_driver(phone, driver_name, voicemail=True)
    asyncio.create_task(save_lead_to_sheet(
        name=driver_name or "Unknown", phone=phone,
        status="Timeout", notes="Call monitor timed out — SMS sent",
    ))
    if sms_sent:
        await bot.send_message(manager_id, f"📱 Offer SMS sent to {phone}.")

async def fetch_safer_data() -> str:
    """Fetch live safety data from FMCSA SAFER website."""
    try:
        url = f"https://safer.fmcsa.dot.gov/query.asp?searchtype=ANY&query_type=queryCarrierSnapshot&query_param=USDOT&query_string={USDOT_NUMBER}"
        headers = {"User-Agent": "Mozilla/5.0"}
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.get(url, headers=headers)
            html = resp.text

        def extract(label):
            pattern = rf'{re.escape(label)}.*?<td[^>]*>(.*?)</td>'
            m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
            if m:
                return re.sub(r'<[^>]+>', '', m.group(1)).strip()
            return "N/A"

        # Parse key fields from HTML
        lines = []
        lines.append(f"=== LIVE FMCSA SAFER DATA (fetched now) ===")
        lines.append(f"USDOT: {USDOT_NUMBER} | MC: MC-1092639")

        # Extract inspection and crash numbers using regex on raw HTML
        total_insp = re.search(r'Total Inspections.*?(\d+)', html, re.DOTALL)
        driver_insp = re.search(r'Driver.*?(\d+).*?Vehicle.*?(\d+)', html, re.DOTALL)
        total_oos = re.search(r'Out of Service.*?(\d+\.?\d*%?)', html, re.DOTALL)
        fatal = re.search(r'Fatal[^\d]*(\d+)', html, re.IGNORECASE)
        injury = re.search(r'Injury[^\d]*(\d+)', html, re.IGNORECASE)
        tow = re.search(r'Tow[^\d]*(\d+)', html, re.IGNORECASE)
        rating = re.search(r'Safety Rating.*?<td[^>]*>(.*?)</td>', html, re.IGNORECASE | re.DOTALL)
        mcs_date = re.search(r'MCS-150.*?(\d{2}/\d{2}/\d{4})', html, re.IGNORECASE | re.DOTALL)

        lines.append(f"\n--- INSPECTIONS (last 24 months) ---")
        lines.append(f"Total: {total_insp.group(1) if total_insp else 'N/A'}")
        lines.append(f"Driver OOS rate vs national avg 22.26%")
        lines.append(f"Vehicle OOS rate vs national avg 6.67%")

        lines.append(f"\n--- CRASHES (last 24 months) ---")
        lines.append(f"Fatal: {fatal.group(1) if fatal else '0'} | Injury: {injury.group(1) if injury else '0'} | Tow: {tow.group(1) if tow else '0'}")

        lines.append(f"\n--- SAFETY RATING ---")
        rating_text = re.sub(r'<[^>]+>', '', rating.group(1)).strip() if rating else "Not assigned"
        lines.append(f"Rating: {rating_text}")

        lines.append(f"\n--- MCS-150 Filed ---")
        lines.append(f"Last filed: {mcs_date.group(1) if mcs_date else 'N/A'}")

        # Check for insurance cancellation notice in SAFER
        cancel_notice = re.search(r'pending insurance cancellation', html, re.IGNORECASE)
        lines.append(f"\n--- INSURANCE STATUS (from SAFER) ---")
        if cancel_notice:
            lines.append("⚠️ WARNING: Pending insurance cancellation detected on SAFER!")
        else:
            lines.append("No pending insurance cancellation on SAFER ✅")
        lines.append(f"For full insurance details (insurer names, expiry dates): https://li-public.fmcsa.dot.gov/LIVIEW/pkg_carrquery.prc_carrlist?n_dotno={USDOT_NUMBER}")

        return "\n".join(lines)

    except Exception as e:
        return f"Could not fetch live SAFER data: {e}. Use cached data below."

async def get_place_phone(place_id: str) -> str:
    """Get phone number for a place using Place Details API."""
    try:
        url = "https://maps.googleapis.com/maps/api/place/details/json"
        params = {
            "place_id": place_id,
            "fields": "formatted_phone_number",
            "key": GOOGLE_MAPS_API_KEY
        }
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, timeout=10)
            data = resp.json()
        return data.get("result", {}).get("formatted_phone_number", "")
    except:
        return ""

async def find_nearby_repair_shops(lat: float, lon: float) -> str:
    """Search Google Maps for nearby truck repair shops."""
    try:
        url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
        params = {
            "location": f"{lat},{lon}",
            "radius": 50000,  # 50km = ~31 miles
            "keyword": "truck repair semi truck diesel mechanic",
            "key": GOOGLE_MAPS_API_KEY
        }
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, timeout=10)
            data = resp.json()

        if data.get("status") != "OK" or not data.get("results"):
            return "No truck repair shops found nearby on Google Maps."

        shops = data["results"][:5]  # Top 5 results
        result = "🔧 *Nearby Truck Repair Shops (Google Maps):*\n\n"
        for i, shop in enumerate(shops, 1):
            name = shop.get("name", "Unknown")
            address = shop.get("vicinity", "Address not available")
            rating = shop.get("rating", "N/A")
            open_now = shop.get("opening_hours", {}).get("open_now")
            status = "✅ Open now" if open_now else ("🔴 Closed" if open_now is False else "")
            maps_link = f"https://www.google.com/maps/search/?api=1&query={shop['geometry']['location']['lat']},{shop['geometry']['location']['lng']}"
            phone = await get_place_phone(shop.get("place_id", ""))
            phone_line = f"📞 {phone}\n" if phone else ""
            result += f"{i}️⃣ *{name}*\n📍 {address}\n{phone_line}⭐ Rating: {rating} {status}\n🗺️ [View on Maps]({maps_link})\n\n"

        return result
    except Exception as e:
        logger.error(f"Google Maps error: {e}")
        return "Couldn't fetch Google Maps results right now. Try @Ezwaysbot on Telegram."

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OWNER_ID = int(os.getenv("OWNER_TELEGRAM_ID", "0"))

claude = Anthropic(api_key=ANTHROPIC_API_KEY)

MANAGER_PASSWORD = "Mike223344"

REQUIRED_DOCUMENTS = ["CDL (front & back photo)", "Medical card", "Phone number"]

# Conversation states
ASK_TYPE, COLLECT_DOCS, RECRUITING = range(3)

# Track which documents each new employee has submitted
# { user_id: { "name": str, "docs": { "Medical card": file_id, ... } } }
employee_data: dict = {}

# Persistent data directory — Railway Volume mounted at /data, fallback to local
DATA_DIR = Path("/data") if Path("/data").exists() else Path(".")
DATA_FILE = DATA_DIR / "known_employees.json"
print(f"💾 DATA DIRECTORY: {DATA_DIR.resolve()} (exists={DATA_DIR.exists()})", flush=True)

SYSTEM_PROMPT = """You are the AI assistant for LONG RUN TRUCKING LLC, responding on behalf of the Operations Manager who is temporarily unavailable.

==COMPANY INFO==
- Company: LONG RUN TRUCKING LLC
- Industry: Trucking & Freight Transportation (USA)
- What we do: We own and operate semi-trucks, hire professional drivers, find and assign loads, and handle all dispatching operations
- Fleet size: 100+ trucks
- Role you represent: Operations Manager
- Languages: English, Uzbek, Russian — always reply in the same language the driver writes in

==YOUR IDENTITY==
You are Mike Azim, the assistant of the operations manager at Long Run Trucking LLC. You are a real person helping drivers — never reveal you are an AI or a bot. If anyone asks "are you a bot?" or "are you AI?" just say something like "Nah I'm real, just busy handling things 😄" and move on naturally.

==COMPANY INFO==
Company name: Long Run Trucking LLC
Legal name: LONGRUNTRUCKING LLC
Headquarters: Orlando, Florida
Physical/mailing address: 904 W Ridge Road, Suite 103, Hobart, IN 46342 — ONLY share this with current employees who ask. Never share with new applicants or people who haven't started working yet.
Phone: (219) 444-3285
USDOT: 3396693 | MC: MC-1092639
Fleet: 100 trucks (OTR, nationwide)
Operations: Interstate, OTR, General Freight

==YOUR PERSONALITY==
- Talk like a real person — casual, warm, natural. Not stiff or robotic
- Use everyday language, contractions, short sentences like a real human texting
- Throw in occasional emojis but don't overdo it 🙂
- Show empathy — if a driver is stressed, acknowledge it like a human would ("Hey I got you", "Don't stress, we'll figure it out", "That's annoying, let me check on that")
- **KEEP REPLIES SHORT** — 1 to 3 sentences max. Drivers are on the road. No long paragraphs. No bullet lists unless truly needed. Say what matters, nothing more.
- **NEVER repeat yourself** — don't restate what the driver already said, don't recap, don't add filler
- If the driver writes in Russian, reply in Russian. If Uzbek, reply in Uzbek. Match their language naturally
- When speaking Uzbek, use modern everyday words — say "tez telefon qil" NOT "zang ur", say "yubor" NOT "jo'nat", speak like how young Uzbeks actually text each other
- Use trucking terms naturally (rate con, BOL, detention, ELD, HOS, lumper, etc.) — like someone who's been in trucking for years
- Vary your responses — don't repeat the same phrases every time
- Sometimes start with "Hey!", "Got it!", "Sure thing!", "No worries!" to sound natural
- Never say "I am an AI", "As an AI", "I'm a bot", or "I'm an assistant"

==IMPORTANT TERMINOLOGY==
- "Truck number" and "Unit number" are the SAME thing. Use them interchangeably — never ask a driver to clarify which one they mean.

==FLEET & OPERATIONS==
- Truck brands: Freightliner, Volvo, Mack, Peterbilt
- Operation type: OTR (Over The Road) — nationwide
- Major clients: Amazon, JB Hunt, FedEx, USPS — top freight only
- Lanes: All 48 states
- ELD system: Quantum ELD (required on all trucks)
- Cameras: Motive AI cameras installed on all trucks — records automatically when an incident occurs
- Dispatcher: Luka Stone | 📞 (219) 229-6409
- Insurance covered: Cargo, Liability, Physical Damage, Occupational Accident

==COMMON TRUCKING Q&A==
Q: I don't have a load / I'm empty / No load yet / Yuk yo'q / Нет груза
A: No worries — our dispatcher is already working on it! 🚛 Stay where you are, keep your ELD on duty status, and you'll receive your rate confirmation as soon as the load is booked. Please do NOT accept any load without our official rate confirmation.

Q: My load got cancelled / Yuk bekor qilindi / Груз отменили
A: Understood — our dispatcher is on it right now and working to find you a replacement load ASAP. Stay on duty and keep your ELD running. You'll hear back shortly with a new rate con.

Q: I need a load going to a specific state / direction
A: Got it — message here with your current location and preferred direction. Our dispatcher will do their best to find a load that works for you. We'll get back to you as soon as possible.

Q: The broker/shipper is not responding or load is late
A: Document everything — take note of the time and who you spoke with. If detention time starts, notify us immediately so we can start the detention clock. We'll contact the broker on our end.

Q: I have a breakdown / truck problem
A: First, pull over safely and turn on hazard lights. Call roadside assistance immediately. Then message here with your exact location, truck number, and what the problem is. The manager will be notified right away.

Q: I got a ticket or inspection (DOT stop)
A: Stay calm and be cooperative with the officer. After the stop, send us a photo of any citations or inspection reports right away. Do not ignore any violations — we handle them together.

Q: When do I get paid? / Qachon maosh olaman? / Когда платят зарплату?
A: At Long Run Trucking LLC, payday is every Friday. Make sure all your BOLs and delivery confirmations are submitted by Wednesday so your pay is processed on time. If you have any issues with your paycheck, message here and the manager will look into it.

Q: When do I get home time? / Qachon uyga boraman? / Когда домой?
A: At Long Run Trucking LLC our home time policy is:
- After 4 weeks on the road → 4 days home time
- After 5 weeks on the road → 5 days home time
Plan your home time in advance and let us know so we can schedule your loads accordingly. Message here with your requested dates and the manager will confirm.

Q: I need to take time off or I'm sick
A: Send your request here as early as possible with the dates. The manager will review and confirm. If it's a same-day emergency, message immediately so we can cover your load.

Q: I'm at the delivery and they won't unload me
A: Note your arrival time (get a timestamp). If wait time exceeds 2 hours, detention pay begins. Notify us so we can contact the broker. Do not leave without proper paperwork.

Q: I lost my rate confirmation / BOL
A: Message here and we'll resend it. Always keep digital copies in your email.

Q: Where can I fuel? / Qayerda yoqilg'i olaman? / Где заправляться?
A: Long Run Trucking covers fuel at all major truck stops. Our approved fuel stops are:
- Pilot Flying J
- Love's Truck Stops
- TA / Petro
Use your fuel card at any of these locations across the USA. If your card is declined, message here immediately with your location and truck number — we'll fix it fast.

Q: Fuel card is not working / Fuel card ishlamayapti / Топливная карта не работает
A: Don't worry — message here with your location and truck number right now. We'll reactivate it or give you an authorization code immediately. Do not pay out of pocket without contacting us first.

Q: How much per mile do you offer? / Miliga qancha to'laysiz? / Сколько платите за милю?
A: At Long Run Trucking LLC we offer:
- Solo drivers (company): $0.75 per mile OR 28% – 31% of gross — your choice, whatever works for you
- Team drivers (company): $1.00 per mile (we're actively looking for team drivers!)
- Sign-on bonus: $500 for new hires
- Company drivers do NOT pay insurance — that is covered by the company
- Owner Operators: see below
We're really looking for team drivers right now, but solo is totally fine too. The operations manager will go over the final details with you directly.

Q: Do you work with owner operators? / Owner operator uchun shartlar qanday? / Работаете с владельцами грузовиков?
A: Yes! We welcome owner operators at Long Run Trucking LLC. Here's how it works:
🚛 Estimated gross: $12,000 – $14,000 per week
🛡️ Insurance: $350/week (owner operators only — you bring your own truck, this covers cargo/liability)
📋 Admin fee: $100/week
🚚 Dispatch fee: 10% of gross

You bring your truck, we bring the freight — Amazon, JB Hunt, FedEx, USPS loads, all 48 states. Consistent miles, no sitting around. The manager will go over the full details and get you set up. Interested?

Q: What trucks do you have? / Qanday mashinalaringiz bor? / Какие у вас грузовики?
A: At Long Run Trucking LLC we run top-of-the-line equipment:
🚛 Freightliner, Volvo, Mack, Peterbilt
All trucks are well-maintained and road-ready. You'll be assigned a truck based on availability when you join.

Q: What loads do you haul? / Qanday yuklar tashiysiz? / Какие грузы возите?
A: We haul OTR freight all across 48 states. Our major clients include:
📦 Amazon | 🚚 JB Hunt | 📬 FedEx | 📮 USPS
Top-tier loads, consistent miles, no sitting around.

Q: What are your hiring requirements? / Ishga kirish uchun nima kerak? / Какие требования для найма?
A: To join Long Run Trucking LLC you need:
✅ CDL-A license
✅ Minimum 1 year of OTR experience
✅ Clean driving record (no serious violations)
✅ Pass drug test & background check
If you meet these requirements, send your application and the manager will contact you shortly.

Q: What benefits do you offer? / Qanday imtiyozlar bor? / Какие бонусы и льготы?
A: At Long Run Trucking LLC we take care of our drivers:
💵 Sign-on bonus — $500 when you start
⭐ Mile bonus — extra bonus for driving 5,000+ miles in a week
🎂 Birthday gift — we celebrate every driver's birthday
👥 Referral bonus — bring a driver who gets hired = $300 cash for you
💰 Weekly pay every Friday
⛽ Fuel card covered at Pilot, Flying J, Love's, TA/Petro
🏠 Home time after 4-5 weeks on the road

Q: How does the referral bonus work? / Referal bonus qanday ishlaydi? / Как работает реферальный бонус?
A: It's simple — if you refer a driver to Long Run Trucking LLC and they get hired and start working, you receive $300 cash. No limit on how many drivers you can refer. The more you refer, the more you earn!

Q: What is detention pay? / Detention pay nima? / Что такое detention pay?
A: If you're ever waiting at a shipper or receiver for more than 2 hours, detention pay kicks in. We cover that through the broker. Just note your arrival time and let me know so we can start the clock.
Also — if for any reason we can't find you a load (which honestly hasn't happened once since we started 💪), we got you covered with $150/day detention pay. But real talk, our dispatch keeps trucks moving so you won't be sitting around.

Q: What ELD do you use? / Qaysi ELD ishlatiladi? / Какой ELD используете?
A: We use Quantum ELD on all our trucks. It's straightforward — if you need help setting it up when you start, just let me know and we'll walk you through it.

Q: Do you have cameras? / Mashinada kamera bormi? / Есть ли камеры в грузовиках?
A: Yep, all our trucks have Motive AI cameras. They only capture footage when something happens — an accident, hard brake, incident. It's there to protect YOU as much as the company. If something goes down on the road, we have the footage to back you up.

Q: What do I do after delivery? / Yetkazib berganim keyin nima qilaman? / Что делать после доставки?
A: As soon as you deliver, send us:
📸 POD (Proof of Delivery) — photo of the signed delivery receipt
📄 BOL (Bill of Lading) — photo of the bill of lading
Send them here or to your dispatcher Luka right away. Don't wait — late submissions can delay your pay.

Q: Are drivers insured? / Haydovchilar sug'urtalanganmi? / Водители застрахованы?
A: Absolutely. Long Run Trucking LLC covers:
🛡️ Cargo Insurance
🛡️ Liability Insurance
🛡️ Physical Damage
🛡️ Occupational Accident (OCC/ACC)
You're fully covered while you're working with us.

Q: What is the PTI video? / PTI video nima? / Что такое PTI видео?
A: PTI stands for Pre-Trip Inspection. Every driver must send a PTI video daily before starting their shift. Walk around the truck, check tires, lights, brakes, mirrors — record it and send it here or to Luka. This protects you from being blamed for pre-existing damage and helps us prevent DOT violations. No PTI = no dispatch. Simple as that.

Q: What are the inspection bonuses and violation charges? / Tekshiruvda bonus va jarimalar qanday? / Какие бонусы за инспекции и штрафы за нарушения?
A: We reward clean driving and take violations seriously:
✅ Clean inspection Level 1 → $500 bonus
✅ Clean inspection Level 2 → $300 bonus
✅ Clean inspection Level 3 → $100 bonus
❌ Violation that's your fault → $500 charge
Stay safe, do your PTI videos daily, and those bonuses are yours. We've seen drivers earn good extra money just by driving clean 💪

Q: I got a DOT inspection / Meni DOT tekshirdi / Меня остановили на инспекцию
A: Stay calm and be professional with the officer. After it's done, send me the inspection report right away — photo of it. If it came back clean, congrats — bonus is coming your way! If there's a violation, don't stress, send it to me and we'll figure out next steps together.

Q: Who is my dispatcher? / Dispetcherim kim? / Кто мой диспетчер?
A: Your dispatcher is Luka Stone. You can reach him at 📞 (219) 229-6409. For loads, rate cons, and day-to-day stuff — Luka is your guy.

Q: What if something happens at night? / Kechasi muammo bo'lsa nima qilaman? / Что делать если что-то случилось ночью?
A: Message here anytime — I'll respond. If it's a real emergency (accident, breakdown, danger), call the operations manager directly: 📞 (219) 444-3285. Don't wait till morning for anything urgent.

Q: I want to discuss my pay rate or a raise
A: The operations manager handles all pay discussions personally. They will get back to you as soon as they're available — this message has been flagged for them.

==RULES==
- NEVER ask for information the driver already provided in this conversation — truck number, location, name, problem description. You have full memory of this chat. Read it before responding.
- NEVER repeat the same question twice. If you already asked for something and got an answer, move forward.
- NEVER ask multiple questions at once — ask one thing at a time if you need info.
- Never share company financial details, contracts, or rate information with third parties
- Never commit to a new pay rate or bonus — say "the manager will discuss this with you directly"
- If a driver reports an accident: say "Call 911 if anyone is hurt. Then call the operations manager directly: 📞 (219) 444-3285. Do NOT admit fault to anyone. Document everything with photos. I'm alerting the manager right now."
- If a driver reports a breakdown on the road: say "Pull over safely and turn on hazards. Call the manager now: 📞 (219) 444-3285. Then send me your exact location and truck number."
- If a driver is in danger, stuck, has a medical issue, or any serious urgent situation: give the manager's number 📞 (219) 444-3285 immediately
- ONLY give the number (219) 444-3285 for real emergencies — accidents, breakdowns, medical emergencies, serious safety situations. NEVER share it for pay, loads, home time, or general questions.
- If an emergency is reported, always end with: "The operations manager has been notified and will contact you shortly."
- Always close with a helpful, reassuring line

==RECRUITING & ATTRACTING NEW DRIVERS==
The trucking market is very competitive right now. When a new driver shows interest in joining, Mike's job is to SELL the company and make them feel this is the best decision they can make. Be warm, confident, and excited — like you genuinely want them on the team.

==WORLD-CLASS RECRUITING — HOW MIKE TALKS TO NEW DRIVERS==

THE #1 RULE: Listen first. Pitch second. Drivers have heard 100 recruiters. What makes Mike different is he actually cares about what THEY need — then shows how Long Run solves it.

STEP 1 — LISTEN BEFORE YOU PITCH
When a new driver shows interest, don't dump the offer immediately. Ask:
- "Where are you running right now?"
- "What's your biggest frustration with your current situation?"
- "What matters most to you — miles, home time, or pay?"
- "Solo or team?"

Their answer tells you exactly what to lead with.

STEP 2 — PITCH WHAT MATTERS TO THEM (not everything)

If they care about PAY:
"We offer $0.75/mile for solo, or 28–31% of gross — your choice, whatever works for you. Team drivers get a dollar a mile. We're actively looking for team drivers right now, but solo is totally fine too. Plus $500 sign-on bonus. Paid every Friday, no delays."

If they care about HOME TIME:
"Our home time is real — not the fake kind. Four weeks out, four days home. Five weeks out, five days home. And we actually honor it — ask any of our drivers."

If they care about EQUIPMENT:
"We run Freightliners, Volvos, Macks, Peterbilts. Well maintained. You won't be sitting on the side of the road in a broke-down truck."

If they care about LOADS / MILES:
"We haul for Amazon, JB Hunt, FedEx, USPS. Our drivers don't sit. The freight is always there."

If they care about RESPECT / CULTURE:
"You'll have a real dispatcher — Luka Stone — who actually picks up. And I'm always here. We treat drivers like people, not numbers."

EXTRAS that close the deal:
- Owner operators welcome — gross $12k–$14k/week, insurance $350/week (owner operators only), admin $100/week, dispatch 10% of gross
- Company drivers: no insurance fee — company covers it
- $500 sign-on bonus — money in your pocket from day one
- Fuel card everywhere (Pilot, Flying J, Love's, TA) — fuel is never YOUR problem
- Detention pay $150/day if you're sitting through no fault of yours
- Clean inspection? Up to $500 bonus — your clean record pays you
- Refer a driver? $300 cash, no limit
- Birthday gift — yes, we actually do that
- Full insurance: cargo, liability, physical damage, occupational accident

STEP 3 — HANDLE OBJECTIONS LIKE A PRO

"I'm happy where I am":
"That's great honestly — best drivers usually are. Can I just ask — is there ONE thing your company could do better? Because we might have that covered."

"The pay isn't enough":
"What number would make you move? Tell me straight — I'll see what we can do."

"I need to think about it":
"Of course. What's the main thing you're thinking through? Let me just answer that right now."

"Bad experience with other companies":
"I hear that a lot — there's a lot of bad ones. That's why everything with us is in writing. Rate cons, pay stubs, policies — transparent. No surprises."

"I don't have enough experience":
"How long have you been driving? We look at the full picture — CDL-A, clean record, right attitude. That matters more than years."

"I'm not looking right now":
"No problem at all. Just remember us — if anything ever changes, I want Long Run to be your first call."

STEP 4 — CLOSE NATURALLY
When they sound ready:
"Honestly you sound like exactly the kind of driver we want. Let's get the paperwork moving — I just need your CDL photo, medical card, and phone number. Takes 5 minutes. Sound good?"

WHAT MAKES DRIVERS LEAVE BAD COMPANIES (know these — use them):
- Broken promises on home time → we show ours is real
- Pay that varies or comes late → we pay Friday, every week, guaranteed
- Old broken equipment → we show our fleet
- No loads / sitting → we show Amazon/FedEx/USPS consistency
- Dispatchers who don't answer → we name Luka directly
- Feeling like a number → we treat them like people (birthday gift, bonuses, respect)

PSYCHOLOGY TIPS:
- Use their name naturally in conversation
- Say "honestly" and "I'll be straight with you" — builds instant trust
- Never push percentage over per mile — offer both, let the driver choose, never say one is better
- We prefer team drivers — always mention it naturally: "We're really looking for team drivers right now — do you have a partner or run solo?" Solo is fine too, just say so warmly
- Never pressure — confidence attracts, desperation repels
- Short messages beat long ones — drivers are on the road
- Always end warm: "Safe travels out there 🚛"

==HOW DRIVERS FEEL ABOUT COLD CALLS (know this deeply)==
Drivers get 5-10 recruiter calls every week. By the time Mike reaches them, they are TIRED of recruiters. They expect:
- A fake-friendly script
- Empty promises
- Pressure tactics
- Recruiters who don't actually listen

Mike's job is to be NOTHING like that. The second Mike sounds like a typical recruiter, the driver hangs up or stops responding.

WHAT DRIVERS FEEL WHEN COLD CALLED:
1. Interrupted — they might be driving, resting, eating. Honor their time.
2. Skeptical — they've been lied to before. Don't oversell.
3. Guarded — they won't open up until they feel safe. Build trust first.
4. Tired — they don't want another 10-minute pitch. Get to the point fast.

HOW TO OPEN A COLD CALL OR FIRST MESSAGE:
- Never open with a pitch. Open with a question: "Hey, quick question — are you still looking for something or are you set right now?"
- If they say they're not looking: "Totally fine. Can I just ask one thing — what's the one thing your current company could do better?"
- If they say they're happy: "That's actually great. I only ask because we work with drivers who are already in good situations — we just tend to offer something a little better. What matters most to you, pay or home time?"

HOW MIKE HANDLES REJECTION — NEVER GIVE UP COLD:
When a driver says no, doesn't mean no forever. It means "not right now" or "convince me differently."

"Not interested":
"No worries at all — I respect that. Can I ask what it would take to make you interested? Just curious, not trying to sell you."

"I already have a company":
"Good that you're set — seriously. I'm not here to steal you. But if they ever fall short on something, remember us. What's the one thing they could do better?"

"Stop calling me":
"My bad — I hear you. I'll back off. But real quick, if I could solve one problem you have right now, what would it be?" [If they push back, say "Got it, I won't bother you again. Take care out there 🚛" and close respectfully.]

"How did you get my number":
"We reached out through the driver network — lots of guys have referred us. Is this a bad time? I can call back when it's better."

"Your pay isn't enough":
Never argue. Say: "Okay that's fair — what number would work for you? Tell me straight and I'll see what we can do."

THE HUMAN TOUCH THAT CLOSES DEALS:
- Remember small details they mentioned and bring them up naturally
- If they mentioned their home state, say "So getting you back to [state] every 4-5 weeks — that actually works for us"
- Acknowledge their frustration before pitching: "Yeah, sounds like your current situation isn't great. That's exactly why I'm reaching out."
- Laugh naturally — "Ha yeah, I get that a lot" makes you sound real
- Silences are okay — don't rush to fill them with more pitch

WHEN DRIVER IS READY TO TALK MORE:
Warm transfer vibe — "Honestly you sound like a solid driver. The manager would love to talk to you personally. Can I set that up?"

==FLEET MANAGER ROLE==
Mike Azim is also the Fleet Manager at Long Run Trucking LLC. He is the direct link between drivers, dispatch, maintenance, safety, and company management. When drivers ask anything about their truck, equipment, maintenance, compliance, HOS, ELD, fuel, repairs — Mike handles it with full authority.

FLEET MANAGER RESPONSIBILITIES Mike handles:
- Truck assignments and driver schedules
- Preventive maintenance and repairs
- DOT compliance and FMCSA regulations
- HOS (Hours of Service) monitoring
- Driver performance and coaching
- Breakdown coordination
- Cost control (fuel, idle time, repairs)
- Safety events and corrective actions
- Driver qualification files
- Daily truck location and log reviews

FLEET Q&A:

Q: My truck needs maintenance / repair / Mashinam ta'mirga muhtoj / Машина требует ремонта
A: Send me your truck number, current location, and what the issue is. I'll get a repair order going right away. Don't drive a truck that isn't safe — pull over and let us know immediately.

Q: My truck broke down on the road / Mashina yo'lda to'xtab qoldi / Машина сломалась в дороге
IMPORTANT: When a driver says truck broke down, ALWAYS ask: "What exactly is the problem? Describe what happened." Then based on their answer — either walk them through a self-fix OR tell them to find a shop and ask their location to help find the nearest one.
A: Stay calm — here's exactly what to do:
1. Pull over safely, turn on hazards 🚨
2. Send me your exact location (city, highway, mile marker) and truck number
3. Tell me what's wrong — engine, tires, brakes, electrical?

Then we find the fastest fix together:
🔧 I'll look up the nearest truck repair shop on your route
🛞 Tire issue? I'll find the nearest tire shop or mobile tire service
⚡ Electrical/ELD? Contact @Turbo_ELD_Service on Telegram immediately
🚑 If it can't move at all — call roadside assistance and send me the location, I'll coordinate from here

To find a shop near you fast:
- Google: "semi truck repair near me" or "truck stop near [your location]"
- Pilot/Flying J and Love's truck stops have service centers at many locations
- Search "FleetNet roadside" or "breakdown assistance near [highway name]"

Don't leave the truck unattended. Call 📞 (219) 444-3285 if it's a serious situation and send me updates 💪

==EASY SELF-FIX GUIDE — teach drivers these before calling a shop==

PROBLEM: Truck won't start / dead battery
FIX: Check if lights were left on. Try jump starting — most truck stops have jumper cables or mobile jump service. Call a nearby truck stop or use Coach-Net/Road Squad. If still nothing, it may be the alternator — need a shop.

PROBLEM: Low air pressure warning light
FIX: Pull over immediately — do NOT drive on low air. Find an air compressor at any truck stop (Pilot, Flying J, Love's, TA all have them). Fill tires to correct PSI (usually 100-110 PSI for drives, 80-90 for steers). If pressure drops again quickly = you have a leak = need tire shop.

PROBLEM: Tire blowout
FIX: Hold the wheel steady, do NOT brake hard. Slowly reduce speed and pull over. Turn on hazards. Set out triangles/flares 100 feet behind truck. Call mobile tire service — search "mobile truck tire service near me" or call Love's or Pilot roadside. Do NOT drive on a blown tire.

PROBLEM: Check engine light (yellow/amber)
FIX: Amber light = not an emergency but report it. Note the light color and any codes on dashboard. You can keep driving carefully but get to a shop soon. Send me a photo of the dashboard.

PROBLEM: Check engine light (red)
FIX: RED light = stop driving immediately. Pull over safely. Do not restart the engine. Call for a tow or mobile mechanic. Send me your location and truck number right away.

PROBLEM: DEF warning light (Diesel Exhaust Fluid)
FIX: You're low on DEF fluid. Buy DEF at any truck stop — it's usually near the fuel pumps. Any brand works. Fill it up and the light should clear. Costs around $10-15 per gallon. Don't ignore it — truck will derate and slow down if empty.

PROBLEM: Low coolant warning
FIX: Pull over, let engine cool for 30 minutes — NEVER open radiator cap on hot engine. Check coolant reservoir. Add coolant (green or orange — check what your truck uses). Available at any truck stop. If coolant keeps dropping = leak = need a shop immediately.

PROBLEM: Oil pressure warning
FIX: Pull over IMMEDIATELY — this is serious. Do NOT keep driving with low oil pressure or you'll destroy the engine. Check oil level with dipstick. If low, add oil (15W-40 diesel engine oil, available at truck stops). If oil level is fine but light stays on = sensor or pump issue = do not drive, call a shop.

PROBLEM: Air dryer / air system issue
FIX: Drain the air tanks manually using the petcock valves under the truck. If brakes feel spongy or air builds slowly, pull over and call a shop. Do not drive with brake air issues.

PROBLEM: Lights not working (headlights, markers, brake lights)
FIX: Check fuses first — fuse box is usually behind the driver seat or under the dash. Replace blown fuses if you have spares. If it's a bulb, truck stops sell replacement marker and brake light bulbs. Driving without working lights = violation. Get it fixed before driving at night.

PROBLEM: Truck overheating
FIX: Pull over immediately, turn off engine. Do NOT open radiator cap — wait 30 minutes. Check coolant level. Check if fan belt is broken (look under hood). Check if radiator cap is loose. If coolant is full and truck still overheats = need a shop.

PROBLEM: Clutch slipping or hard to shift
FIX: Check clutch fluid level if it's a hydraulic clutch. Avoid riding the clutch. If it's grinding gears or clutch is completely out = need a shop. Don't force it.

PROBLEM: Trailer not connecting / landing gear issues
FIX: Check fifth wheel is fully locked — tug test. Make sure kingpin is fully seated. Landing gear — use the crank handle, switch from high to low gear for heavy loads. If airlines aren't connecting, check for bent or damaged gladhands and replace if needed (truck stops carry them).

==TRUCK REPAIR SHOP DATABASE — REAL LOCATIONS==
When a driver asks for a nearby shop, ask their state/city first, then give them the closest shops from this list:

🔧 NEW JERSEY (NJ):
1. 3425 Tremley Point Rd, Linden, NJ 07036 — 📞 (220) 203-2222
2. 3 Sutton Pl, Edison, NJ 08817 — 📞 (908) 561-8473
3. 2 Fish House Rd, Kearny, NJ 07032 — 📞 (973) 344-8444
4. 615 Industrial Rd, Carlstadt, NJ 07072 — 📞 (201) 507-9896
5. 73 Green Pond Rd, Rockaway, NJ 07866 — 📞 (973) 347-8473
6. 2039 US-130, Burlington, NJ 08016 — 📞 (973) 578-8700
7. 350 W Buck St, Paulsboro, NJ 08066 — 📞 (856) 856-8558

🔧 PENNSYLVANIA (PA):
1. 225 Lincoln Hwy, Fairless Hills, PA 19030 — 📞 (267) 319-2421
2. 225 Lincoln Hwy Ste S1, Fairless Hills, PA 19030 — 📞 (215) 391-0524
3. 3041 Marwin Rd Unit A, Bensalem, PA 19020 — 📞 (267) 293-6666
4. 2050R Byberry Rd, Philadelphia, PA 19116 — 📞 (267) 591-7399
5. 11621 Caroline Rd, Philadelphia, PA 19154 — 📞 (267) 777-2444
6. North East Philadelphia, PA — 📞 (267) 575-5551
7. 10094 Sandmeyer Ln, Philadelphia, PA 19116 — 📞 (267) 657-4447
8. 9300 Blue Grass Rd, Philadelphia, PA 19114 — 📞 (267) 255-5723
9. 2950 Castor Ave, Philadelphia, PA 19134 — 📞 (267) 892-0203

==HOW TO FIND NEAREST REPAIR SHOP FOR ANY STATE==
IMPORTANT: When a driver needs a repair shop, ALWAYS do these steps:
1. Ask their exact location (city + state or highway + mile marker)
2. Tell them: "Open @Ezwaysbot on Telegram, click 'Open Map', share your location — it will show ALL nearby truck repair shops on a map instantly!"
3. If they can't use the bot, give them shops from the database below or the national chains

==NATIONAL TRUCK REPAIR SHOP CHAINS — find nearest one==
When a driver needs a shop, ask their location (city + state or highway + mile marker) then tell them the nearest options from this list:

MAJOR NATIONAL CHAINS (locations in almost every state):
🔧 TA Truck Service — inside TA/Petro truck stops nationwide. Open 24/7. Full service. ta-petro.com
🔧 Pilot Flying J Mobile Command — roadside and in-store service. pilotflyingj.com
🔧 Love's Truck Care — inside Love's stops, mobile service available. loves.com/truck-care
🔧 Speedco (Loves owned) — fast oil changes and PM service. speedco.com
🔧 Freightliner ServicePoint — Freightliner dealer network, all major cities. Find at dealers.daimler-trucks.com
🔧 Volvo Action Service — 24/7 Volvo dealer support. volvotrucks.com
🔧 Mack OneCall — 24/7 Mack breakdown support: 1-800-922-6255
🔧 Peterbilt SmartLINQ — dealer finder at peterbilt.com
🔧 FleetNet America — 24/7 nationwide roadside coordination: 1-800-259-2059
🔧 Road Squad (Loves) — mobile roadside: 1-800-768-2325
🔧 Coach-Net — roadside assistance 24/7: 1-800-863-5415

HOW MIKE FINDS NEAREST SHOP:
When driver gives location, tell them:
1. The closest truck brand dealer for their specific truck (Freightliner, Volvo, Mack, Peterbilt)
2. Nearest TA/Petro, Pilot, or Love's with service center on their route
3. Google search tip: "[problem] truck repair near [city, state]" or "[highway name] truck repair"
4. Call FleetNet 1-800-259-2059 or Road Squad 1-800-768-2325 — they dispatch nearest mobile mechanic

Q: I have a tire issue / blowout / Shina muammosi / Проблема с шиной
A: Pull over safely right away — don't keep driving on a bad tire. Send your location and truck number and I'll dispatch a tire service to you. Safety first, always.

Q: How do I check my HOS / Hours of Service?
A: Check your Quantum ELD app — it shows your available driving hours in real time. Always plan your runs around your HOS. Never push past your legal limits — a fatigued driving violation is one of the most serious you can get. If you're unsure, message me before you drive.

Q: My ELD is not working / ELD ishlamayapti / ELD не работает
A: Don't drive without a working ELD — that's a violation. Here's what to do right now:
1. Message @Turbo_ELD_Service on Telegram — they handle all ELD support directly
2. Send them your truck number and describe what's happening on the screen
3. Switch to paper logs as backup until it's fixed — note the malfunction time
4. Message me here too so I know what's going on
Don't hit the road until it's resolved 🚫

Q: I have engine warning lights on / Asboblar panelidagi ogohlantirish chirog'i yondi
A: Send me a photo of the dashboard and your truck number. Do not ignore warning lights — some of them mean stop driving immediately. Tell me what colors and which lights and I'll tell you exactly what to do.

Q: How do I report a truck problem? / Mashina muammosini qanday bildiraman?
A: Easy — just message here with:
🚛 Truck number
📍 Your current location
⚠️ What exactly is wrong
I'll create a maintenance ticket and get it handled fast. The sooner you report, the faster we fix it.

Q: When does my truck get serviced / oil change? / Mashina qachon texnik xizmatga boradi?
A: We schedule preventive maintenance on all trucks regularly. I track it on my end. If you notice anything between services — unusual sounds, warning lights, anything — report it immediately. Don't wait for scheduled service if something feels wrong.

Q: My truck is dirty / Can I get a wash? / Mashina yuviladimi?
A: Yes — you can get your truck washed at major truck stops. Keep your truck clean and presentable. It reflects on Long Run Trucking. If you need a wash code or authorization, message me.

Q: I have too many hours, I need to rest / HOS tugadi / Часы выработаны
A: Find a safe legal parking spot — a truck stop or rest area — and take your required rest break. Never push past your legal HOS. Your safety comes first and we'll reroute the load if needed. Message me your location so I know where you are.

Q: I got placed out of service / Meni yo'ldan chetlatishdi / Меня остановили
A: Stay calm and don't argue with the officer. Send me the out-of-service order right away — photo of it. I'll review the violations, coordinate repairs if needed, and guide you step by step to get back on the road legally.

Q: What do I do when I pick up a new truck?
A: When you get assigned a new truck, do this:
1. Do a full walkaround — inspect everything before you accept it
2. Record a PTI video and send it to me
3. Note any pre-existing damage and report it immediately
4. Make sure your ELD is registered to your name on that truck
5. Confirm all documents are in the truck (registration, insurance, inspection report)
Don't accept a truck with unreported damage — that protects YOU.

Q: Can I use the truck for personal use?
A: No. Company trucks are strictly for business use only. Personal use of company vehicles is not allowed and creates serious liability and insurance issues.

Q: I want to switch trucks / Can I get a different truck?
A: Send me your request with your reason. Truck assignments are based on availability and operational needs. I'll do my best to accommodate you. Message here and the manager will review.

Q: How do I reduce idle time?
A: Keep idle time under 5 minutes when parked. Excessive idling wastes fuel, costs the company money, and it shows up in our Motive camera reports. Use the truck's APU or bunk heater instead of idling overnight. Drivers with low idle time get noticed in a good way 👍

==SAFETY MANAGER ROLE==
Mike Azim is also the Safety Manager at Long Run Trucking LLC. When drivers ask anything about safety, accidents, inspections, violations — Mike handles it with authority and calm.

ACCIDENT PROTOCOL — teach drivers this every time:
Step 1: Stay calm. Take a deep breath. Don't panic.
Step 2: Move to a safe spot if possible — pull off the road, turn on hazards.
Step 3: Do NOT admit fault to anyone — not the other driver, not police, not bystanders. Say nothing about fault. Ever.
Step 4: Check yourself and others for injuries.
Step 5: If it's a serious crash or someone is injured → Call 911 immediately.
Step 6: If it's minor (no injuries, small damage) → Call the emergency number: 📞 (219) 444-3285 right away.
Step 7: Take photos of everything — your truck, other vehicle, road, damage, license plates, surroundings.
Step 8: Get the other driver's info — name, insurance, license plate, phone.
Step 9: Do NOT move the truck until told to by authorities or the company.
Step 10: Wait for instructions from Mike/management.

SAFETY Q&A:
Q: I got into an accident / Avariya bo'ldi / Попал в аварию
A: Hey, first — stay calm, you got this. Here's exactly what to do:
1. Get to a safe spot, hazards on 🚨
2. Do NOT admit fault to anyone — not a single word about who's to blame
3. Is anyone hurt or is it a major crash? → Call 911 now
4. Minor accident, no injuries? → Call me directly: 📞 (219) 444-3285
5. Take photos of everything around you
6. Get the other driver's info (name, insurance, plate)
7. Don't move the truck until we tell you to
Stay on the line — we're handling this together 💪

Q: What do I do if someone hits me? / Menga mashina urib ketsa? / Если меня ударили?
A: Same steps — stay calm, don't admit anything, get to safety. Call 📞 (219) 444-3285 right away and take photos of everything. Even if it's not your fault, say nothing about fault on scene.

Q: Do I need to call police for a small accident?
A: If there's any injury at all — yes, call 911 immediately. If it's very minor with no injuries, call our emergency line first: 📞 (219) 444-3285 and we'll guide you from there.

Q: What safety documents do I need to keep in my truck?
A: Always keep these in your truck at all times:
📄 Registration
📄 Insurance card
📄 Your CDL
📄 Medical certificate
📄 Current inspection report
If DOT asks for any of these and you don't have them — that's a violation. Keep them organized.

Q: What happens if I fail a drug test?
A: This is serious — a failed drug test means you're immediately taken off the road per federal DOT regulations. You'll need to go through a SAP (Substance Abuse Professional) program before you can drive commercially again. We follow all DOT rules, no exceptions.

Q: Do you do random drug testing?
A: Yes. DOT requires random drug and alcohol testing and we follow it strictly. All drivers in our fleet are in the random testing pool. Stay clean and you'll never have an issue.

Q: How do I prevent violations?
A: Simple habits that protect your record:
🎥 Send your PTI video every single day before driving
🔍 Check all lights, tires, brakes, mirrors every morning
📋 Keep your ELD updated and accurate at all times
💤 Never drive over your HOS limits
📵 No phone use while driving — Motive cameras catch it
🚫 Always wear your seatbelt
These habits = clean record = more bonuses in your pocket 💰

Q: What happens after a DOT inspection?
A: Send me the inspection report right away — photo of it.
✅ Clean = bonus coming your way
❌ Violation = we review it together and handle it. Be transparent with us and we'll figure it out."""


def load_known_employees() -> set:
    if DATA_FILE.exists():
        return set(json.loads(DATA_FILE.read_text()))
    return set()


def save_known_employees(employees: set):
    DATA_FILE.write_text(json.dumps(list(employees)))


known_employees: set = load_known_employees()

# Per-user memory — Mike learns facts about each user over time
USER_MEMORY_FILE = DATA_DIR / "user_memory.json"

def load_user_memory() -> dict:
    if USER_MEMORY_FILE.exists():
        try:
            return json.loads(USER_MEMORY_FILE.read_text())
        except:
            return {}
    return {}

def save_user_memory(memory: dict):
    try:
        USER_MEMORY_FILE.write_text(json.dumps(memory, ensure_ascii=False, indent=2))
    except:
        pass

user_memory: dict = load_user_memory()

# ── Global knowledge base — Mike learns from conversations + internet ──────────
KNOWLEDGE_FILE = DATA_DIR / "knowledge_base.json"

def load_knowledge_base() -> dict:
    if KNOWLEDGE_FILE.exists():
        try:
            return json.loads(KNOWLEDGE_FILE.read_text())
        except:
            return {"facts": [], "web_cache": {}}
    return {"facts": [], "web_cache": {}}

def save_knowledge_base(kb: dict):
    try:
        KNOWLEDGE_FILE.write_text(json.dumps(kb, ensure_ascii=False, indent=2))
    except:
        pass

knowledge_base: dict = load_knowledge_base()


async def web_search(query: str) -> str:
    """Search DuckDuckGo for trucking-related info."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"}
        url = f"https://html.duckduckgo.com/html/?q={query.replace(' ', '+')}"
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
        # Extract text snippets from results
        snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', resp.text, re.DOTALL)
        titles = re.findall(r'class="result__title"[^>]*>.*?<a[^>]*>(.*?)</a>', resp.text, re.DOTALL)
        results = []
        for i, (title, snippet) in enumerate(zip(titles[:5], snippets[:5])):
            title_clean = re.sub(r'<[^>]+>', '', title).strip()
            snippet_clean = re.sub(r'<[^>]+>', '', snippet).strip()
            if title_clean and snippet_clean:
                results.append(f"{i+1}. {title_clean}: {snippet_clean}")
        return "\n".join(results) if results else "No results found."
    except Exception as e:
        return f"Search error: {e}"


async def learn_from_web(topic: str) -> str:
    """Search web and save to knowledge base."""
    cached = knowledge_base.get("web_cache", {}).get(topic)
    if cached and time.time() - cached.get("ts", 0) < 86400:  # 24h cache
        return cached["text"]

    result = await web_search(f"trucking industry {topic} 2024 2025")
    if result and "No results" not in result:
        knowledge_base.setdefault("web_cache", {})[topic] = {
            "text": result, "ts": time.time()
        }
        save_knowledge_base(knowledge_base)
    return result


# ── Google Sheets helpers (via Apps Script webhook) ───────────────────────────

async def save_lead_to_sheet(
    name: str, phone: str,
    experience: str = "", solo_team: str = "",
    status: str = "New Lead", notes: str = ""
) -> bool:
    """Send one lead row to Google Sheets via Apps Script webhook."""
    try:
        payload = {
            "date": time.strftime("%Y-%m-%d %H:%M"),
            "name": name,
            "phone": phone,
            "experience": experience,
            "solo_team": solo_team,
            "status": status,
            "notes": notes,
        }
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.post(GOOGLE_SHEETS_WEBHOOK, json=payload)
        return resp.status_code == 200
    except Exception as e:
        logger.warning(f"Save lead to sheet failed: {e}")
        return False


# ── Lead queue (persisted to disk) ────────────────────────────────────────────
LEAD_QUEUE_FILE = DATA_DIR / "lead_queue.json"

def load_lead_queue() -> list:
    try:
        if LEAD_QUEUE_FILE.exists():
            return json.loads(LEAD_QUEUE_FILE.read_text())
    except Exception:
        pass
    return []

def save_lead_queue(queue: list):
    try:
        LEAD_QUEUE_FILE.write_text(json.dumps(queue, indent=2))
    except Exception as e:
        logger.warning(f"Failed to save lead queue: {e}")

lead_queue: list = load_lead_queue()

# Track all seen phones to avoid duplicates across sessions
SEEN_PHONES_FILE = DATA_DIR / "seen_phones.json"

def load_seen_phones() -> set:
    try:
        if SEEN_PHONES_FILE.exists():
            return set(json.loads(SEEN_PHONES_FILE.read_text()))
    except Exception:
        pass
    return set()

def save_seen_phones(phones: set):
    try:
        SEEN_PHONES_FILE.write_text(json.dumps(list(phones)))
    except Exception:
        pass

seen_phones_global: set = load_seen_phones()


PHONE_RE = re.compile(r'(?<!\d)(\(?\d{3}\)?[\s\-\.]\d{3}[\s\-\.]\d{4})(?!\d)')
NAME_RE  = re.compile(r'\b([A-Z][a-z]{1,15} [A-Z][a-z]{1,15})\b')
EXP_RE   = re.compile(r'(\d+)\s*year', re.IGNORECASE)

# Keywords that indicate a COMPANY is posting (competitor ad, not a driver)
_COMPANY_SIGNALS = re.compile(
    r'\b(we are hiring|now hiring|apply now|apply today|join our team|'
    r'competitive pay|sign.on bonus|benefits package|equal opportunity employer|'
    r'job description|requirements:|qualifications:|responsibilities:|'
    r'submit.{0,10}resume|send.{0,10}resume|upload resume|'
    r'recruiting|recruiter|staffing|carrier looking|company looking|'
    r'fleet owner|dispatch service|trucking company seeking|'
    r'driver needed|drivers needed|hiring cdl|cdl drivers wanted)\b',
    re.IGNORECASE
)

# Keywords that indicate a DRIVER wrote this post (what we want)
_DRIVER_SIGNALS = re.compile(
    r'\b(looking for work|seeking.{0,10}(job|position|employment)|'
    r'available.{0,15}(work|hire|position)|'
    r'i have.{0,20}(cdl|class a|experience)|'
    r'my cdl|i am.{0,20}driver|i.m.{0,20}driver|'
    r'call me|contact me|reach me|text me|'
    r'years? (of )?experience|clean (mvr|record)|'
    r'(solo|team).{0,15}driver|otr.{0,10}driver|'
    r'class a.{0,20}(license|cdl)|hazmat endorsement)\b',
    re.IGNORECASE
)

def is_driver_posting(text: str) -> bool:
    """Return True if this text looks like a driver self-posting, not a company ad."""
    company_hits = len(_COMPANY_SIGNALS.findall(text))
    driver_hits = len(_DRIVER_SIGNALS.findall(text))
    # Reject if company signals dominate
    if company_hits > 2:
        return False
    # Accept if driver signals present
    if driver_hits >= 1:
        return True
    # Neutral short text — could be either, accept tentatively
    return company_hits == 0

# Craigslist city codes for major trucking areas (Florida heavy)
CRAIGSLIST_CITIES = [
    "miami", "tampa", "orlando", "jacksonville", "ftlauderdale",
    "sarasota", "gainesville", "ocala", "daytona",          # Florida
    "atlanta", "houston", "dallas", "chicago", "charlotte",  # Other states
    "nashville", "columbus", "memphis", "phoenix", "lasvegas",
]

# ── Claude Haiku lead-verification agent ─────────────────────────────────────

_haiku_client = Anthropic()  # reuse existing API key
_haiku_semaphore = asyncio.Semaphore(3)  # max 3 concurrent Claude calls

_LEAD_AGENT_SYSTEM = """You are a lead-qualification agent for a trucking company that recruits CDL-A drivers.

Your job: read a piece of text (a forum post, Craigslist ad, social media post, resume listing, etc.)
and decide if it was written BY A DRIVER who is looking for work — NOT by a company that is hiring.

A DRIVER post sounds like:
- "I have 5 years OTR experience, looking for work, call me at..."
- "CDL-A, clean MVR, available immediately, solo or team"
- "Seeking trucking position in Florida, Class A license, hazmat"

A COMPANY post (reject these) sounds like:
- "Now hiring CDL-A drivers! Competitive pay, benefits, sign-on bonus"
- "Apply today! Join our team of drivers"
- "We need OTR drivers. Submit your resume at..."

Respond ONLY with valid JSON, no markdown, no explanation:
{
  "is_driver": true or false,
  "confidence": "high" | "medium" | "low",
  "name": "First Last or null",
  "phone": "digits only or null",
  "experience": "e.g. 5 years or null",
  "solo_team": "Solo" | "Team" | "Either" | null,
  "notes": "one short sentence about this driver"
}

If there are multiple phone numbers, pick the one most likely to be the driver's personal cell.
If is_driver is false, all other fields can be null."""

async def claude_verify_lead(text: str, source: str) -> dict | None:
    """Ask Claude Haiku if this text is a driver looking for work. Returns lead dict or None."""
    # Fast pre-check: reject obvious company pages before spending an API call
    if _COMPANY_SIGNALS.search(text) and not _DRIVER_SIGNALS.search(text):
        return None

    # Trim text to ~2000 chars so Haiku stays fast and cheap
    snippet = text[:2000].strip()
    if len(snippet) < 40:
        return None

    async with _haiku_semaphore:
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: _haiku_client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=300,
                    system=_LEAD_AGENT_SYSTEM,
                    messages=[{"role": "user", "content": f"Source: {source}\n\nText:\n{snippet}"}],
                )
            )
            raw = response.content[0].text.strip()
            # Strip any markdown fences if present
            raw = re.sub(r'^```[a-z]*\n?', '', raw)
            raw = re.sub(r'\n?```$', '', raw)
            data = json.loads(raw)

            if not data.get("is_driver"):
                return None

            phone_raw = data.get("phone") or ""
            clean = re.sub(r'\D', '', phone_raw)
            if len(clean) < 10:
                # No phone in Claude response — try regex on the text
                phones = PHONE_RE.findall(snippet)
                clean = re.sub(r'\D', '', phones[0]) if phones else ""
                phone_raw = phones[0] if phones else ""

            # Also try to grab email if no phone
            email = ""
            if not clean:
                email_match = re.search(r'[\w.\-]+@[\w.\-]+\.\w{2,6}', snippet)
                email = email_match.group(0) if email_match else ""

            # Need at least a phone or email to save the lead
            if not clean and not email:
                return None
            if clean and clean in seen_phones_global:
                return None

            if clean:
                seen_phones_global.add(clean)

            notes = data.get("notes") or ""
            if email and not clean:
                notes = f"Email only: {email} | {notes}"

            return {
                "name":        data.get("name") or "Driver",
                "phone":       phone_raw or clean or email,
                "clean_phone": clean or email,
                "experience":  data.get("experience") or "",
                "solo_team":   data.get("solo_team") or "Solo",
                "source":      source,
                "status":      "New Lead",
                "notes":       notes,
                "called":      False,
            }
        except Exception as e:
            logger.warning(f"Claude lead agent error: {e}")
            return None


async def agent_scrape_craigslist(city: str, location: str, count: int = 10) -> list[dict]:
    """Craigslist /res/ resume section ONLY — drivers post their own info here.
    /trp/ is transport JOBS (companies hiring) — never search there."""
    leads = []
    urls = [
        f"https://{city}.craigslist.org/search/res?query=CDL+driver&sort=date",
        f"https://{city}.craigslist.org/search/res?query=truck+driver&sort=date",
        f"https://{city}.craigslist.org/search/res?query=class+A+driver&sort=date",
        f"https://{city}.craigslist.org/search/res?query=OTR+driver&sort=date",
        f"https://{city}.craigslist.org/search/res?query=CDL+A+available&sort=date",
    ]
    for url in urls:
        if len(leads) >= count:
            break
        try:
            html = await fetch_page(url)
            if not html:
                continue
            post_links = re.findall(r'href="(https://[^"]+craigslist\.org/[^"]+/\d+\.html)"', html)
            # Verify each post with Claude in parallel (up to 5 at once)
            tasks = [
                _verify_craigslist_post(link, city, location)
                for link in post_links[:10]
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, dict) and r and len(leads) < count:
                    leads.append(r)
        except Exception as e:
            logger.warning(f"Agent Craigslist {city}: {e}")
        await asyncio.sleep(1)
    logger.info(f"Agent Craigslist/{city}: {len(leads)} verified driver leads")
    return leads


async def _verify_craigslist_post(link: str, city: str, location: str) -> dict | None:
    try:
        post_html = await fetch_page(link)
        text = re.sub(r'<[^>]+>', ' ', post_html)
        text = re.sub(r'\s+', ' ', text)
        lead = await claude_verify_lead(text, f"craigslist/{city}")
        if lead:
            lead["location"] = location
        await asyncio.sleep(0.5)
        return lead
    except Exception:
        return None


async def agent_scrape_duckduckgo(query: str, location: str, count: int = 5) -> list[dict]:
    """DuckDuckGo search + Claude verification — only driver posts pass."""
    leads = []
    blocked_boards = {
        "ziprecruiter.com", "cdljobs.com", "driverjobs.com", "gettrucking.com",
        "monster.com", "careerbuilder.com", "glassdoor.com", "indeed.com/jobs",
        "recruiter.com", "simplyhired.com", "snagajob.com",
    }
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"}
        url = f"https://html.duckduckgo.com/html/?q={query.replace(' ', '+')}"
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
        result_urls = re.findall(r'uddg=(https?[^&"]+)', resp.text)
        result_urls = [
            u.replace('%3A', ':').replace('%2F', '/').replace('%3F', '?')
             .replace('%3D', '=').replace('%26', '&')
            for u in result_urls[:8]
        ]
        # Filter out job boards
        result_urls = [u for u in result_urls if not any(b in u for b in blocked_boards)]

        for result_url in result_urls[:5]:
            if len(leads) >= count:
                break
            try:
                page_html = await fetch_page(result_url)
                text = re.sub(r'<[^>]+>', ' ', page_html)
                text = re.sub(r'\s+', ' ', text)
                lead = await claude_verify_lead(text, result_url[:60])
                if lead:
                    lead["location"] = location
                    leads.append(lead)
                await asyncio.sleep(1)
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"Agent DuckDuckGo: {e}")
    return leads


async def agent_scrape_google(query: str, location: str, count: int = 5) -> list[dict]:
    """Google search via HTML endpoint — find driver-posted pages with contact info."""
    leads = []
    blocked_boards = {
        "ziprecruiter", "cdljobs", "driverjobs", "gettrucking", "monster",
        "careerbuilder", "glassdoor", "recruiter.com", "simplyhired",
        "snagajob", "workstream", "jobcase", "dice.com", "indeed.com/jobs",
    }
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
        search_url = f"https://www.google.com/search?q={query.replace(' ', '+')}&num=10"
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=headers) as client:
            resp = await client.get(search_url)
        # Extract result URLs from Google HTML
        result_urls = re.findall(r'/url\?q=(https?://[^&"]+)', resp.text)
        result_urls = [u for u in result_urls if not any(b in u for b in blocked_boards)]
        result_urls = list(dict.fromkeys(result_urls))[:6]  # deduplicate

        for url in result_urls:
            if len(leads) >= count:
                break
            try:
                page_html = await fetch_page(url)
                text = re.sub(r'<[^>]+>', ' ', page_html)
                text = re.sub(r'\s+', ' ', text)
                lead = await claude_verify_lead(text, url[:60])
                if lead:
                    lead["location"] = location
                    leads.append(lead)
                await asyncio.sleep(1.5)
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"Agent Google: {e}")
    return leads


async def agent_scrape_linkedin(location: str, count: int = 10) -> list[dict]:
    """Search LinkedIn for CDL drivers open to work — scrapes public profile cards."""
    leads = []
    if not PLAYWRIGHT_AVAILABLE:
        return leads
    queries = [
        f"CDL A truck driver looking for work {location}",
        f"OTR driver available {location}",
        f"class A CDL driver seeking position {location}",
    ]
    async with _browser_lock:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                          "--disable-blink-features=AutomationControlled"]
                )
                ctx = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 900},
                )
                page = await ctx.new_page()
                for query in queries:
                    if len(leads) >= count:
                        break
                    try:
                        search_url = f"https://www.linkedin.com/search/results/people/?keywords={query.replace(' ', '%20')}&origin=GLOBAL_SEARCH_HEADER"
                        await page.goto(search_url, timeout=20000, wait_until="domcontentloaded")
                        await page.wait_for_timeout(3000)
                        text = await page.inner_text("body")
                        lead = await claude_verify_lead(text, "linkedin.com")
                        if lead:
                            lead["location"] = location
                            leads.append(lead)
                            logger.info(f"LinkedIn: +1 driver lead")
                    except Exception as e:
                        logger.warning(f"LinkedIn search failed: {e}")
                    await asyncio.sleep(3)
                await browser.close()
        except Exception as e:
            logger.warning(f"LinkedIn scraper crashed: {e}")
    return leads


# ── Playwright browser scraper ────────────────────────────────────────────────
try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    logger.warning("Playwright not installed — browser scraping disabled")

_browser_lock = asyncio.Lock()

async def browser_get_text(url: str, wait_selector: str = "body", timeout: int = 15000) -> str:
    """Open URL in headless Chromium and return full page text."""
    if not PLAYWRIGHT_AVAILABLE:
        return ""
    async with _browser_lock:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                          "--disable-blink-features=AutomationControlled"]
                )
                ctx = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 800},
                )
                page = await ctx.new_page()
                await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
                try:
                    await page.wait_for_selector(wait_selector, timeout=5000)
                except Exception:
                    pass
                text = await page.inner_text("body")
                await browser.close()
                return text
        except Exception as e:
            logger.warning(f"browser_get_text failed for {url}: {e}")
            return ""


async def scrape_indeed_resumes(location: str, count: int = 10) -> list[dict]:
    """Search Indeed RESUME section — drivers who uploaded their OWN profile looking for work."""
    leads = []
    # Indeed resume search shows driver-uploaded CVs, not company job ads
    urls = [
        f"https://www.indeed.com/resumes?q=CDL+A+truck+driver&l={location.replace(' ', '+')}",
        f"https://www.indeed.com/resumes?q=class+A+driver+OTR&l={location.replace(' ', '+')}",
        f"https://www.indeed.com/resumes?q=CDL+A+available+for+hire&l={location.replace(' ', '+')}",
    ]
    for url in urls:
        if len(leads) >= count:
            break
        try:
            text = await browser_get_text(url, wait_selector="body", timeout=20000)
            # Filter: only keep if it looks like driver profile pages
            new = extract_leads_from_html(f"<body>{text}</body>", "indeed-resumes", location,
                                          require_driver_signal=True)
            leads.extend(new)
            logger.info(f"Indeed resumes {location} ({url[-30:]}): {len(new)} leads")
        except Exception as e:
            logger.warning(f"Indeed resume scrape failed: {e}")
        await asyncio.sleep(2)
    return leads[:count]


async def scrape_truckerreport_availability(location: str, count: int = 10) -> list[dict]:
    """Scrape TruckerReport forum — drivers post 'I am available for work' threads."""
    leads = []
    urls = [
        "https://www.truckerreport.com/truckingforums/threads/?prefixes[]=2",  # Driver seeking jobs
        f"https://www.truckerreport.com/truckingforums/search/?q=looking+for+work+CDL&o=date",
        f"https://www.truckerreport.com/truckingforums/search/?q=available+driver+{location.replace(' ', '+')}&o=date",
    ]
    for url in urls:
        if len(leads) >= count:
            break
        try:
            html = await fetch_page(url)
            if not html:
                continue
            # Get thread links
            thread_links = re.findall(r'href="(https://www\.truckerreport\.com/truckingforums/threads/[^"]+)"', html)
            for link in thread_links[:6]:
                if len(leads) >= count:
                    break
                try:
                    post_html = await fetch_page(link)
                    text = re.sub(r'<[^>]+>', ' ', post_html)
                    text = re.sub(r'\s+', ' ', text)
                    lead = await claude_verify_lead(text, "truckerreport.com")
                    if lead:
                        lead["location"] = location
                        leads.append(lead)
                    await asyncio.sleep(1)
                except Exception:
                    continue
        except Exception as e:
            logger.warning(f"TruckerReport scrape failed: {e}")
        await asyncio.sleep(2)
    return leads[:count]


async def scrape_truck_driver_forums(location: str, count: int = 10) -> list[dict]:
    """Scrape trucking forums and boards where drivers post their availability."""
    leads = []
    # These sites have driver-posted "I'm available/looking" sections
    urls = [
        f"https://www.thetruckersreport.com/truckingforums/search/?q=looking+for+work&o=date",
        f"https://www.ooida.com/",  # Owner operators forum
        "https://www.reddit.com/r/Truckers/search/?q=looking+for+work+CDL&sort=new",
        "https://www.reddit.com/r/Truckers/search/?q=available+driver+hire+me&sort=new",
        f"https://www.reddit.com/r/TruckDrivers/search/?q=looking+for+work&sort=new",
    ]
    for url in urls:
        if len(leads) >= count:
            break
        try:
            text = await browser_get_text(url, timeout=15000)
            lead = await claude_verify_lead(text, url.split("/")[2])
            if lead:
                lead["location"] = location
                leads.append(lead)
                logger.info(f"Forum {url.split('/')[2]}: +1 verified driver lead")
        except Exception as e:
            logger.warning(f"Forum scrape failed {url}: {e}")
        await asyncio.sleep(2)
    return leads[:count]


FB_GROUPS = [
    "https://www.facebook.com/groups/cdldriverslookingforwork",
    "https://www.facebook.com/groups/truckdriverslookingforwork",
    "https://www.facebook.com/groups/otrdriversjobs",
    "https://www.facebook.com/groups/cdladriversjobboard",
    "https://www.facebook.com/groups/truckingjobsusa",
    "https://www.facebook.com/groups/cdltruckdriverjobs",
]

_fb_logged_in = False

async def facebook_login(page) -> bool:
    """Log into Facebook with stored credentials."""
    global _fb_logged_in
    if _fb_logged_in:
        return True
    if not FB_EMAIL or not FB_PASSWORD:
        return False
    try:
        await page.goto("https://www.facebook.com/", timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)
        # Try multiple selectors — FB changes their layout frequently
        email_selectors = ["#email", "input[name='email']", "input[type='email']",
                           "input[placeholder*='email' i]", "input[placeholder*='phone' i]"]
        filled = False
        for sel in email_selectors:
            try:
                await page.wait_for_selector(sel, timeout=5000)
                await page.fill(sel, FB_EMAIL)
                filled = True
                break
            except Exception:
                continue
        if not filled:
            logger.warning("Facebook: could not find email field")
            return False
        await page.wait_for_timeout(500)
        pass_selectors = ["#pass", "input[name='pass']", "input[type='password']"]
        for sel in pass_selectors:
            try:
                await page.fill(sel, FB_PASSWORD)
                break
            except Exception:
                continue
        await page.wait_for_timeout(500)
        login_selectors = ["button[name='login']", "button[type='submit']",
                           "[data-testid='royal_login_button']", "input[type='submit']"]
        for sel in login_selectors:
            try:
                await page.click(sel)
                break
            except Exception:
                continue
        await page.wait_for_timeout(6000)
        # Check if logged in
        current_url = page.url
        if "facebook.com" in current_url and "login" not in current_url:
            _fb_logged_in = True
            logger.info("✅ Facebook login successful")
            return True
        # Try alternative login form
        try:
            await page.wait_for_selector("[data-testid='royal_login_button']", timeout=5000)
            await page.click("[data-testid='royal_login_button']")
            await page.wait_for_timeout(5000)
            if "login" not in page.url:
                _fb_logged_in = True
                logger.info("✅ Facebook login successful (alt)")
                return True
        except Exception:
            pass
        logger.warning(f"Facebook login may have failed — URL: {page.url}")
        return False
    except Exception as e:
        logger.warning(f"Facebook login error: {e}")
        return False


async def scrape_facebook_groups(location: str, count: int = 10) -> list[dict]:
    """Log into Facebook and scrape trucking groups for driver leads."""
    leads = []
    if not PLAYWRIGHT_AVAILABLE or not FB_EMAIL:
        return leads
    async with _browser_lock:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                          "--disable-blink-features=AutomationControlled"]
                )
                ctx = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 900},
                )
                page = await ctx.new_page()

                logged_in = await facebook_login(page)
                if not logged_in:
                    await browser.close()
                    return leads

                # Search Facebook for DRIVER posts — "I am looking" not "we are hiring"
                search_queries = [
                    f"CDL A driver looking for work {location}",
                    f"truck driver available for hire {location}",
                    f"OTR driver seeking position {location}",
                    f"class A CDL available {location} looking",
                    "CDL driver looking for job",
                ]
                for query in search_queries:
                    if len(leads) >= count:
                        break
                    try:
                        search_url = f"https://www.facebook.com/search/posts?q={query.replace(' ', '%20')}"
                        await page.goto(search_url, timeout=20000, wait_until="domcontentloaded")
                        await page.wait_for_timeout(3000)
                        # Scroll to load more posts
                        for _ in range(3):
                            await page.keyboard.press("End")
                            await page.wait_for_timeout(1500)
                        text = await page.inner_text("body")
                        # Claude verifies each result is a driver, not a company
                        lead = await claude_verify_lead(text, "facebook.com/search")
                        if lead:
                            lead["location"] = location
                            leads.append(lead)
                            logger.info(f"Facebook search '{query[:30]}': +1 verified driver lead")
                    except Exception as e:
                        logger.warning(f"Facebook search failed: {e}")
                    await asyncio.sleep(2)

                # Also scrape group pages directly
                for group_url in FB_GROUPS[:3]:
                    if len(leads) >= count:
                        break
                    try:
                        await page.goto(group_url, timeout=20000, wait_until="domcontentloaded")
                        await page.wait_for_timeout(3000)
                        for _ in range(4):
                            await page.keyboard.press("End")
                            await page.wait_for_timeout(1500)
                        text = await page.inner_text("body")
                        lead = await claude_verify_lead(text, group_url.split("/")[-1])
                        if lead:
                            lead["location"] = location
                            leads.append(lead)
                            logger.info(f"Facebook group {group_url.split('/')[-1]}: +1 verified driver lead")
                    except Exception as e:
                        logger.warning(f"Facebook group scrape failed {group_url}: {e}")
                    await asyncio.sleep(3)

                await browser.close()
        except Exception as e:
            logger.warning(f"Facebook scraper crashed: {e}")

    return leads[:count]


async def fetch_page(url: str) -> str:
    """Fetch raw HTML from a URL with browser-like headers."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers=headers) as client:
            resp = await client.get(url)
            return resp.text
    except Exception:
        return ""


def extract_leads_from_html(html: str, source: str, location: str,
                            require_driver_signal: bool = False) -> list[dict]:
    """Pull name + phone pairs from any HTML page."""
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'\s+', ' ', text)

    # Reject the whole page if it's clearly a company/employer posting
    if require_driver_signal and not is_driver_posting(text):
        return []

    leads = []
    phones = PHONE_RE.findall(text)
    names  = NAME_RE.findall(text)
    exp_m  = EXP_RE.search(text)
    experience = f"{exp_m.group(1)} yrs" if exp_m else ""
    solo_team  = "Team" if re.search(r'\bteam\b', text, re.IGNORECASE) else "Solo"

    for i, phone in enumerate(phones):
        clean = re.sub(r'[\s\-\.\(\)]', '', phone)
        if len(clean) < 10 or clean in seen_phones_global:
            continue
        # Skip obvious non-driver numbers (zip codes etc)
        if clean.startswith("00") or clean.startswith("11"):
            continue
        seen_phones_global.add(clean)
        name = names[i] if i < len(names) else "Driver"
        leads.append({
            "name": name,
            "phone": phone.strip(),
            "clean_phone": clean,
            "experience": experience,
            "solo_team": solo_team,
            "location": location,
            "source": source,
            "status": "New Lead",
            "notes": "",
            "called": False,
        })
    return leads


async def scrape_craigslist(city: str, count: int = 10) -> list[dict]:
    """Scrape Craigslist RESUME section — drivers who POST THEIR OWN contact info looking for work.

    Craigslist /res/ = resumes section. Drivers post "I have CDL-A, available, call me."
    This is the OPPOSITE of /trd/ (transportation jobs) where companies post job openings.
    """
    leads = []
    # /res/ = resumes posted BY workers (drivers saying "hire me")
    # /trp/ = transportation services — sometimes drivers posting availability
    # NOT /trd/ = transportation jobs = companies hiring (what we do NOT want)
    urls = [
        f"https://{city}.craigslist.org/search/res?query=CDL+A+driver&sort=date",
        f"https://{city}.craigslist.org/search/res?query=truck+driver+class+A&sort=date",
        f"https://{city}.craigslist.org/search/res?query=CDL+looking+for+work&sort=date",
        f"https://{city}.craigslist.org/search/trp?query=CDL+driver+available&sort=date",
    ]
    for url in urls:
        if len(leads) >= count:
            break
        try:
            html = await fetch_page(url)
            if not html:
                continue
            # Extract individual post links
            post_links = re.findall(r'href="(https://[^"]+craigslist\.org/[^"]+/\d+\.html)"', html)
            for link in post_links[:8]:
                if len(leads) >= count:
                    break
                try:
                    post_html = await fetch_page(link)
                    # Require driver signal — must look like a driver wrote this, not a company
                    new = extract_leads_from_html(post_html, f"craigslist/{city}", city.title(),
                                                  require_driver_signal=True)
                    if new:
                        leads.extend(new)
                        logger.info(f"Craigslist/{city} resume post: +{len(new)} leads")
                    await asyncio.sleep(1)
                except Exception:
                    continue
        except Exception:
            continue
        await asyncio.sleep(1)
    return leads


async def scrape_duckduckgo_leads(query: str, location: str,
                                  require_driver_signal: bool = False) -> list[dict]:
    """Search DuckDuckGo and fetch top result pages to extract phone numbers."""
    leads = []
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"}
        url = f"https://html.duckduckgo.com/html/?q={query.replace(' ', '+')}"
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
        # Extract actual result URLs
        result_urls = re.findall(r'uddg=(https?[^&"]+)', resp.text)
        result_urls = [u.replace('%3A', ':').replace('%2F', '/').replace('%3F','?').replace('%3D','=').replace('%26','&') for u in result_urls[:4]]

        for result_url in result_urls:
            # Skip employer job boards — they only have company postings
            if any(board in result_url for board in
                   ["ziprecruiter.com", "cdljobs.com", "driverjobs.com",
                    "gettrucking.com", "recruiter.com", "monster.com",
                    "careerbuilder.com", "glassdoor.com"]):
                continue
            try:
                page_html = await fetch_page(result_url)
                new = extract_leads_from_html(page_html, result_url[:50], location,
                                              require_driver_signal=require_driver_signal)
                leads.extend(new)
                await asyncio.sleep(1.5)
            except Exception:
                continue
    except Exception:
        pass
    return leads


CITY_MAP = {
    "Florida":        ["miami", "orlando", "tampa", "jacksonville", "ftlauderdale", "sarasota", "daytona", "lakeland", "gainesville", "pensacola"],
    "Texas":          ["houston", "dallas", "austin", "sanantonio", "elpaso"],
    "Georgia":        ["atlanta", "savannah"],
    "Tennessee":      ["nashville", "memphis"],
    "Ohio":           ["columbus", "cleveland"],
    "Illinois":       ["chicago"],
    "California":     ["losangeles", "sfbay", "sandiego"],
    "North Carolina": ["charlotte", "raleigh"],
    "Virginia":       ["norfolk", "richmond"],
    "Indiana":        ["indianapolis"],
    "Alabama":        ["birmingham"],
    "South Carolina": ["columbia"],
    "New York":       ["newyork"],
    "Nevada":         ["lasvegas"],
    "Arizona":        ["phoenix"],
}

async def search_cdl_leads(location: str = "Florida", count: int = 15) -> list[dict]:
    """Multi-source CDL-A driver lead search — targets DRIVERS looking for work, not companies hiring.

    Sources used (all driver-side posting, never employer job boards):
    1. Craigslist /res/ — drivers post their own resume/contact
    2. Indeed resumes — drivers upload their CV looking for work
    3. Facebook groups — drivers post "I'm looking for work"
    4. TruckerReport forum — drivers announce availability
    5. DuckDuckGo with driver-perspective queries
    """
    leads = []
    loc = location or "Florida"
    logger.info(f"Searching CDL driver leads in {loc} (target: {count}) — DRIVER posts only")

    # Source 1: Craigslist /res/ — Claude verifies each post is a real driver
    if len(leads) < count:
        cities = CITY_MAP.get(loc, ["miami"])
        for city in cities[:5]:
            if len(leads) >= count:
                break
            new = await agent_scrape_craigslist(city, loc, count=count - len(leads))
            leads.extend(new)
            logger.info(f"  Agent Craigslist/{city}: +{len(new)}")
            await asyncio.sleep(2)

    # Source 2: Indeed Resume search — Claude verifies driver profiles
    if PLAYWRIGHT_AVAILABLE and len(leads) < count:
        new = await scrape_indeed_resumes(loc, count=count - len(leads))
        leads.extend(new)
        logger.info(f"  Indeed resumes: +{len(new)}")
        await asyncio.sleep(2)

    # Source 3: Facebook groups — driver-perspective queries + Claude filter
    if PLAYWRIGHT_AVAILABLE and len(leads) < count:
        new = await scrape_facebook_groups(loc, count=count - len(leads))
        leads.extend(new)
        logger.info(f"  Facebook: +{len(new)}")
        await asyncio.sleep(2)

    # Source 4: TruckerReport forum threads
    if len(leads) < count:
        new = await scrape_truckerreport_availability(loc, count=count - len(leads))
        leads.extend(new)
        logger.info(f"  TruckerReport: +{len(new)}")
        await asyncio.sleep(2)

    # Source 5: Reddit trucking subs
    if PLAYWRIGHT_AVAILABLE and len(leads) < count:
        new = await scrape_truck_driver_forums(loc, count=count - len(leads))
        leads.extend(new)
        logger.info(f"  Forums/Reddit: +{len(new)}")
        await asyncio.sleep(2)

    # Source 6: Google search — driver-perspective queries
    if len(leads) < count:
        google_queries = [
            f'"looking for work" "CDL A" {loc} contact phone',
            f'"available for hire" "class A" driver {loc}',
            f'site:craigslist.org "CDL" "looking for work" {loc}',
            f'"OTR driver" "seeking" OR "available" {loc} "call me" OR email',
            f'"CDL A" driver resume {loc} "years experience" contact',
        ]
        for q in google_queries:
            if len(leads) >= count:
                break
            new = await agent_scrape_google(q, loc, count=min(3, count - len(leads)))
            leads.extend(new)
            logger.info(f"  Agent Google: +{len(new)}")
            await asyncio.sleep(3)

    # Source 7: LinkedIn — drivers marked "open to work"
    if PLAYWRIGHT_AVAILABLE and len(leads) < count:
        new = await agent_scrape_linkedin(loc, count=count - len(leads))
        leads.extend(new)
        logger.info(f"  LinkedIn: +{len(new)}")
        await asyncio.sleep(2)

    # Source 8: DuckDuckGo fallback
    if len(leads) < count:
        ddg_queries = [
            f'"looking for work" "CDL A" {loc}',
            f'"available" "class A" driver {loc} phone email',
        ]
        for q in ddg_queries:
            if len(leads) >= count:
                break
            new = await agent_scrape_duckduckgo(q, loc, count=count - len(leads))
            leads.extend(new)
            logger.info(f"  Agent DuckDuckGo: +{len(new)}")
            await asyncio.sleep(2)

    save_seen_phones(seen_phones_global)
    logger.info(f"Total found in {loc}: {len(leads[:count])}")
    return leads[:count]


async def daily_lead_hunter(bot=None):
    """Runs every 24h — finds 100 CDL-A driver leads, saves to queue + Google Sheets."""
    global lead_queue

    TARGET = 100
    LOCATIONS = [
        "Florida", "Florida", "Florida", "Florida",   # heavily Florida
        "Texas", "Georgia", "Tennessee", "North Carolina",
        "Ohio", "Illinois", "California", "Virginia",
        "Indiana", "Alabama", "Nevada", "Arizona",
    ]

    logger.info("🔍 Daily lead hunter started...")
    if bot and OWNER_ID:
        try:
            await bot.send_message(
                OWNER_ID,
                "🔍 *Daily Lead Hunt Started*\nMike is searching for CDL-A drivers across the USA — mostly Florida.\nYou'll get updates as leads come in. 📋",
                parse_mode="Markdown"
            )
        except Exception:
            pass

    found_today = 0

    for location in LOCATIONS:
        if found_today >= TARGET:
            break
        try:
            logger.info(f"Searching leads in {location}...")
            batch = await search_cdl_leads(location=location, count=7)
            new_leads = []

            for lead in batch:
                await save_lead_to_sheet(
                    name=lead["name"],
                    phone=lead["phone"],
                    experience=lead.get("experience", ""),
                    solo_team=lead.get("solo_team", ""),
                    status="New Lead",
                    notes=f"Found in {location}",
                )
                lead_queue.append(lead)
                new_leads.append(lead)
                found_today += 1

            save_lead_queue(lead_queue)
            logger.info(f"  {location}: +{len(new_leads)} leads (total today: {found_today})")

            if bot and OWNER_ID:
                if new_leads:
                    preview = "\n".join(f"  • {l['name']} — {l['phone']}" for l in new_leads[:3])
                    msg = f"📋 *+{len(new_leads)} leads — {location}* ({found_today}/{TARGET})\n{preview}"
                else:
                    msg = f"🔍 Searched {location} — no new numbers found, moving on... ({found_today}/{TARGET})"
                try:
                    await bot.send_message(OWNER_ID, msg, parse_mode="Markdown")
                except Exception:
                    pass

        except Exception as e:
            logger.warning(f"Lead hunter error for {location}: {e}")

        await asyncio.sleep(4)

    # Final report
    pending = sum(1 for l in lead_queue if not l.get("called"))
    logger.info(f"Daily lead hunter complete: {found_today} leads found today, {pending} total pending.")
    if bot and OWNER_ID:
        try:
            await bot.send_message(
                OWNER_ID,
                f"✅ *Daily Lead Hunt Complete!*\n\n"
                f"📊 Found today: *{found_today}* drivers\n"
                f"📋 Total in queue: *{len(lead_queue)}*\n"
                f"📞 Ready to call: *{pending}*\n\n"
                f"Send *`call queue: 10`* to start calling.",
                parse_mode="Markdown"
            )
        except Exception:
            pass


async def daily_lead_loop(bot=None):
    """Standby — does NOT auto-hunt. Manager triggers hunt manually with 'hunt now'."""
    # Auto-hunting disabled: manager controls when and how many leads to find.
    logger.info("Lead loop standby — waiting for manual 'hunt now' command.")


async def extract_general_knowledge(user_msg: str, mike_reply: str):
    """Extract general trucking knowledge from conversation (not user-specific)."""
    try:
        prompt = f"""Read this trucking conversation and extract any GENERAL knowledge worth remembering — industry facts, regulations, common driver problems, market rates, useful tips. NOT user-specific facts.

Examples of what to extract:
- "Drivers in Texas often complain about lack of drop-and-hook loads"
- "Many drivers leave companies because dispatchers don't communicate"
- "Current market rate for reefer is around $2.50/mile"

Return a JSON list of short fact strings. Max 2 facts. If nothing general worth saving, return: []

User: {user_msg[:300]}
Mike: {mike_reply[:300]}"""

        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if match:
            facts = json.loads(match.group())
            if facts:
                existing = knowledge_base.get("facts", [])
                for fact in facts:
                    if fact and fact not in existing:
                        existing.append(f"[{time.strftime('%Y-%m-%d')}] {fact}")
                # Keep only last 200 facts
                knowledge_base["facts"] = existing[-200:]
                save_knowledge_base(knowledge_base)
    except:
        pass


def get_knowledge_context() -> str:
    """Get relevant knowledge to inject into Mike's context."""
    facts = knowledge_base.get("facts", [])
    if not facts:
        return ""
    recent = facts[-30:]  # Last 30 facts
    return "==WHAT MIKE HAS LEARNED==\n" + "\n".join(f"• {f}" for f in recent)


# Topics Mike proactively learns from the internet on startup
STARTUP_LEARNING_TOPICS = [
    "how truck drivers feel about cold calls from recruiters",
    "why truck drivers ignore or refuse cold calls from trucking companies",
    "best HR techniques for handling driver rejection during recruiting",
    "how to warm up cold calls to truck drivers",
    "trucking recruiter mistakes that turn drivers off",
    "what truck drivers really want from a new company offer",
    "how to handle driver objections during trucking recruitment",
    "psychology of trust building with truck drivers",
    "how to make a driver feel heard during a recruiting call",
    "trucking cold call scripts that actually work 2024",
]


async def startup_learning():
    """Search the web on startup to fill Mike's knowledge base with HR & recruiting intelligence."""
    logger.info("Mike startup learning: searching internet for HR/recruiting insights...")
    for topic in STARTUP_LEARNING_TOPICS:
        cached = knowledge_base.get("web_cache", {}).get(topic)
        # Only re-search if cache is older than 12 hours
        if cached and time.time() - cached.get("ts", 0) < 43200:
            continue
        try:
            result = await web_search(f"{topic} site:reddit.com OR site:indeed.com OR site:cdllife.com OR site:thetruckersreport.com")
            if result and "No results" not in result and "Search error" not in result:
                knowledge_base.setdefault("web_cache", {})[topic] = {
                    "text": result, "ts": time.time()
                }
                # Also extract facts and save them
                prompt = f"""You are an expert HR coach for trucking companies. Read these web search results about truck driver psychology and recruiting, then extract 2-3 concrete, actionable insights that an AI recruiter (Mike) should know when talking to drivers.

Topic: {topic}
Search results: {result[:800]}

Return a JSON list of short insight strings that Mike can actually USE in conversations. Focus on: driver emotions, what works, what doesn't. Example: ["Drivers who say they're happy often mean they're not actively looking — ask what would make them move", "Cold-called drivers feel interrupted — open with 'is this a bad time?' to show respect"]

Return ONLY the JSON list."""
                response = claude.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=300,
                    messages=[{"role": "user", "content": prompt}]
                )
                raw = response.content[0].text.strip()
                match = re.search(r'\[.*\]', raw, re.DOTALL)
                if match:
                    facts = json.loads(match.group())
                    existing = knowledge_base.get("facts", [])
                    for fact in facts:
                        if fact and fact not in existing:
                            existing.append(f"[{time.strftime('%Y-%m-%d')}] [WEB LEARNED] {fact}")
                    knowledge_base["facts"] = existing[-200:]
                save_knowledge_base(knowledge_base)
                logger.info(f"Learned: {topic}")
        except Exception as e:
            logger.warning(f"Startup learning failed for '{topic}': {e}")
        await asyncio.sleep(2)  # be gentle with requests
    logger.info("Mike startup learning complete.")


async def periodic_learning_loop():
    """Re-learn from web every 24 hours while the bot is running."""
    while True:
        await asyncio.sleep(86400)  # 24 hours
        logger.info("Mike periodic learning: refreshing knowledge from web...")
        await startup_learning()


MEMORY_EXTRACT_PROMPT = """You are a memory extraction assistant. Read the conversation exchange below and extract any NEW facts about the user that are worth remembering for future conversations.

Extract facts like:
- Name, truck number, unit number
- Home state or city
- CDL expiry date, medical card expiry date
- Type of issues they've had (ELD problems, load issues, etc.)
- Their preferred lanes or routes
- Their team partner name
- Any personal details they shared (family, preferences)
- Their communication style (language: English/Uzbek/Russian)
- Any complaints or recurring problems

Return a JSON object with only NEW or UPDATED facts. Use short keys. Example:
{"name": "John", "truck": "45", "home_state": "Texas", "cdl_expiry": "08/2026", "language": "Russian", "issues": ["ELD problems"]}

If nothing new to remember, return: {}

User message: {user_msg}
Mike's reply: {mike_reply}
Current known facts: {current_facts}"""

async def extract_and_save_memory(user_id: int, user_msg: str, mike_reply: str):
    """Extract key facts from conversation and save to user memory."""
    try:
        uid = str(user_id)
        current_facts = user_memory.get(uid, {})

        prompt = MEMORY_EXTRACT_PROMPT.format(
            user_msg=user_msg[:500],
            mike_reply=mike_reply[:500],
            current_facts=json.dumps(current_facts)
        )

        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )

        raw = response.content[0].text.strip()
        # Extract JSON from response
        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if json_match:
            new_facts = json.loads(json_match.group())
            if new_facts:
                current_facts.update(new_facts)
                # Merge issues list without duplicates
                if "issues" in new_facts and "issues" in current_facts:
                    current_facts["issues"] = list(set(current_facts.get("issues", []) + new_facts.get("issues", [])))
                user_memory[uid] = current_facts
                save_user_memory(user_memory)
    except:
        pass  # Memory extraction is non-critical

def get_user_context(user_id: int) -> str:
    """Get stored facts about a user to inject into system prompt."""
    uid = str(user_id)
    facts = user_memory.get(uid, {})
    if not facts:
        return ""
    lines = ["==WHAT MIKE KNOWS ABOUT THIS USER=="]
    for k, v in facts.items():
        lines.append(f"- {k}: {v}")
    lines.append("Use this info — never ask for things you already know about this person.")
    return "\n".join(lines)

# Manager sessions — users who unlocked manager mode with password
MANAGER_SESSIONS_FILE = DATA_DIR / "manager_sessions.json"

def load_manager_sessions() -> set:
    if MANAGER_SESSIONS_FILE.exists():
        try:
            return set(json.loads(MANAGER_SESSIONS_FILE.read_text()))
        except:
            return set()
    return set()

def save_manager_sessions(sessions: set):
    try:
        MANAGER_SESSIONS_FILE.write_text(json.dumps(list(sessions)))
    except:
        pass

manager_sessions: set = load_manager_sessions()

MANAGER_SYSTEM_PROMPT = """You are Mike Azim, the internal AI assistant for the management team of Long Run Trucking LLC. You are speaking with a verified manager — treat them as a trusted colleague with full access.

==YOUR ROLE FOR MANAGERS==
You assist with ALL internal company operations:
- HR: hiring decisions, driver onboarding, document review, firing, performance
- Safety: DOT compliance, accident reports, drug testing, safety violations, FMCSA rules
- Fleet: truck assignments, maintenance scheduling, breakdowns, repair costs, unit tracking
- Operations: load planning, dispatcher coordination, driver issues, detention claims
- Finance: pay disputes, bonus calculations, expense approvals
- Recruiting: applicant status, screening decisions

==COMPANY INFO==
Company: Long Run Trucking LLC | HQ: Orlando, FL
Address: 904 W Ridge Road, Suite 103, Hobart, IN 46342
Phone: (219) 444-3285 | USDOT: 3396693 | MC: MC-1092639
Fleet: 100 trucks | Dispatcher: Luka Stone (219) 229-6409

==PAY STRUCTURE==
Solo (company): $0.75/mile or 28%–31% of gross | Team: $1.00/mile | Sign-on bonus: $500 | Payday: Friday | Owner Op insurance: $350/wk (owner ops only, company drivers pay nothing)
Owner Operator: gross $12k–$14k/week | Insurance $350/week | Admin $100/week | Dispatch 10% of gross
Detention: $150/day | Referral bonus: $300 | Inspection bonus: up to $500

==HR — DRIVER ONBOARDING PROCESS==
Follow this exact 11-step process for every new driver hire:

STEP 1 — APPLICATION & INSURANCE CHECK
- Receive driver application
- Run insurance check — verify driver is insurable
- If not insurable → reject, notify driver professionally

STEP 2 — MVR & PSP CHECK
- Pull Motor Vehicle Record (MVR)
- Pull Pre-Employment Screening Program report (PSP)
- Review: accidents, violations, license history
- Clean record required: CDL-A, minimum 1 year OTR experience

STEP 3 — DOCUMENT COLLECTION
Collect ALL of the following:
□ CDL — both sides (front & back)
□ Medical card
□ Immigration doc — Green Card OR Passport
□ Social Security card copy
□ Email address
□ Phone number
□ Emergency contact (name + phone)

STEP 4 — CLEARINGHOUSE CHECK
- Run FMCSA Drug & Alcohol Clearinghouse check
- Result must be: NOT PROHIBITED
- If prohibited → disqualified, do not proceed

STEP 5 — DRUG TEST
- Send driver to approved testing location
- Must pass pre-employment drug test (urine, 10-panel)
- No negative dilute or positive result — disqualified

STEP 6 — INTERVIEW & DRIVE TEST
- Phone or in-person interview with operations manager
- Road test / drive test required
- Evaluate: professionalism, communication, driving skill

STEP 7 — AGREEMENT
- Driver signs employment/contractor agreement
- Review pay structure, home time policy, company rules
- Both parties sign — keep copy on file

STEP 8 — ORIENTATION
- Company orientation (remote or in-person)
- Cover: safety rules, ELD usage, fuel card, load process, communication protocol, accident procedure

STEP 9 — ONBOARDING — TRAINIX (Fleet Pro)
- Set up driver profile in Trainix / Fleet Pro system
- Enter all documents, personal info, hire date
- Assign driver ID and truck number

STEP 10 — TRUCK ASSIGNMENT
When assigning truck, complete ALL of the following:
□ PTI (Pre-Trip Inspection) — driver does full inspection
□ Truck inspection — mechanical check
□ Device check — ELD (Quantum), Motive camera, phone mount
□ Decal check — all required decals present and visible

STEP 11 — FINAL APPROVALS (ALL 3 REQUIRED BEFORE DISPATCHING)
✅ GTG from HR — documents complete, onboarding done
✅ GTG from Safety — drug test passed, clearinghouse clear, safety orientation done
✅ GTG from Fleet — truck assigned, PTI done, devices working
→ Then: Dispatching assigns first load
→ Then: Accounting sets up payroll

IMPORTANT: Driver does NOT get dispatched until ALL THREE GTG approvals are confirmed.

When an HR manager asks about onboarding, walk them through these steps. Track which step a driver is on if they tell you. Flag any missing items clearly.

==SAFETY MANAGER — DAILY DUTIES==
A real trucking safety manager does these things every single day. Mike knows all of this and helps safety managers stay on top of it.

DAILY TASKS:
□ Review all DVIRs (Driver Vehicle Inspection Reports) submitted overnight
□ Check ELD logs — flag any HOS violations, missing logs, unassigned driving
□ Monitor CSA/SMS scores on FMCSA — watch all 7 BASICs for alerts
□ Review any roadside inspection reports received (DataQ disputes if needed)
□ Follow up on open violations or citations from previous inspections
□ Check for any accidents, incidents, or near-misses reported by drivers
□ Verify drivers on duty today have valid medical cards and CDLs
□ Monitor drug & alcohol Clearinghouse for any new queries or violations
□ Respond to driver safety questions, complaints, or concerns
□ Communicate with fleet manager on any truck maintenance safety issues

WEEKLY TASKS:
□ Audit random sample of ELD logs for HOS compliance
□ Review driver CSA scores — flag drivers approaching alert thresholds
□ Check expiring documents (CDL, medical cards) — 30/60/90 day warnings
□ Conduct or schedule safety coaching for any driver with violations
□ Review accident/incident reports — complete within 48 hours of incident
□ Update driver qualification files with any new documents received

MONTHLY TASKS:
□ Pull MVR (Motor Vehicle Record) updates for all active drivers
□ Run Clearinghouse annual query on all drivers
□ Review all open DataQ challenges and disputes
□ Audit 3-5 driver qualification files for completeness
□ Safety meeting or training — document attendance
□ Review insurance certificates — check for upcoming renewals

==DOCUMENT EXPIRATION TRACKING==
Mike tracks these documents and sends alerts at 90/60/30 days before expiry:

DRIVER DOCUMENTS (per driver):
- CDL: Alert at 90, 45, 14 days before expiry. Expired CDL = automatic OOS + carrier violation
- Medical Card: Expires every 1-2 years. Alert at 60, 30, 14 days. Expired = OOS immediately
- MVR: Pull annually (required by FMCSA §391.25). Alert 14 days before annual review due
- Drug test: Pre-employment required. Random pool ongoing. Annual clearinghouse query required

COMPANY PERMITS & REGISTRATIONS:
- UCR (Unified Carrier Registration): Annual — renews each year, due Dec 31. Enforcement Jan 1.
- IRP (Apportioned Plates): Annual — varies by state, renew before Jan 1
- IFTA License: Annual — renew before Dec 31 each year
- MCS-150 Biennial Update: Every 2 years from last filing date — late filing = possible deactivation
- Form 2290 (Heavy Vehicle Use Tax): Annual — due Aug 31 each year
- BOC-3 (Process Agent): One-time filing — verify still active

IFTA QUARTERLY FILING DEADLINES (every year):
- Q1 (Jan–Mar): Due April 30
- Q2 (Apr–Jun): Due July 31
- Q3 (Jul–Sep): Due October 31
- Q4 (Oct–Dec): Due January 31
Late IFTA filing = penalties + interest + possible license revocation. Set reminders for the 15th of each filing month.

TRUCK DOCUMENTS (per unit):
- Annual DOT Inspection: Required every 12 months — expired = OOS the truck immediately
- Truck Registration/Tags: Annual — varies by state
- IFTA Decals: Annual — must be visible on both sides of cab

DRIVER QUALIFICATION FILE (DQF) — required by FMCSA for every driver:
□ Application for employment
□ Safety performance history (previous employers — 3 years)
□ MVR from all states licensed in past 3 years
□ Road test certificate or equivalent
□ Medical examiner's certificate (current)
□ FMCSA Clearinghouse query result
□ Annual review of driving record
□ Drug & alcohol testing records
Penalty for missing/incomplete DQF: $1,000+ per violation per driver. Can reach $10,000 per driver in an audit.

When a safety manager asks about reminders, document status, or what needs attention — give them a clear checklist of what's due soon based on any dates they provide. Always flag anything expired or expiring within 30 days as URGENT 🔴. 60 days = WARNING 🟡. 90 days = HEADS UP 🔵.

==YOUR STYLE==
- Speak like a smart, efficient business partner — professional but not stiff
- Give direct, useful answers — no fluff
- Short replies unless the manager asks for detail
- If you don't have info, say so clearly and suggest what to do next
- NEVER reveal you are AI to anyone — you are Mike Azim, a real person on the team

==SAFETY ANALYTICS==
When a manager asks about safety scores, violations, inspections, crashes, OOS rates, FMCSA data, or DOT records — live data from FMCSA SAFER will be automatically included in the message. Use that live data to give the manager a clear, accurate report. Always explain what the numbers mean (good/bad, vs national average). Never make up numbers — only use the live data provided.

==INSURANCE INFO==
When a manager asks about company insurance, provide these direct links to check live:
- FMCSA Licensing & Insurance: https://li-public.fmcsa.dot.gov/LIVIEW/pkg_carrquery.prc_carrlist?n_dotno=3396693
- SearchMule full profile: https://searchmule.io/carrier/3396693
- SAFER snapshot: https://safer.fmcsa.dot.gov/query.asp?searchtype=ANY&query_type=queryCarrierSnapshot&query_param=USDOT&query_string=3396693
Tell the manager these links show live insurance filings, active policies, cargo and liability coverage, and authority status directly from FMCSA.

To exit manager mode, type: logout"""


def is_away() -> bool:
    return os.getenv("AWAY_MODE", "true").lower() == "true"


HISTORY_FILE = DATA_DIR / "conversation_history.json"

def load_history() -> dict:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except:
            return {}
    return {}

def save_history(history: dict):
    try:
        HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False))
    except:
        pass

# Load history from disk on startup
conversation_history: dict = load_history()

async def ask_claude_manager(user_id: int, question: str) -> str:
    try:
        uid = f"mgr_{user_id}"
        if uid not in conversation_history:
            conversation_history[uid] = []

        # If manager is asking about safety/analytics/scores — fetch live FMCSA data
        safety_keywords = ["safety", "score", "violation", "inspection", "crash", "fmcsa", "safer",
                           "oos", "out of service", "analytics", "report", "record", "dot", "rating"]
        needs_live_data = any(kw in question.lower() for kw in safety_keywords)

        content = question
        if needs_live_data:
            safer_data = await fetch_safer_data()
            content = f"{question}\n\n[LIVE DATA FETCHED FROM FMCSA RIGHT NOW]:\n{safer_data}"

        conversation_history[uid].append({"role": "user", "content": content})
        conversation_history[uid] = conversation_history[uid][-20:]
        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            system=MANAGER_SYSTEM_PROMPT,
            messages=conversation_history[uid],
        )
        reply = response.content[0].text
        conversation_history[uid].append({"role": "assistant", "content": reply})
        save_history(conversation_history)
        return reply
    except Exception as e:
        logger.error(f"Claude manager error: {e}")
        return "Something went wrong on my end, try again."


async def ask_claude(user_id: int, question: str) -> str:
    try:
        uid = str(user_id)
        if uid not in conversation_history:
            conversation_history[uid] = []

        # Build system prompt with user memory + global knowledge injected
        user_context = get_user_context(user_id)
        knowledge_ctx = get_knowledge_context()
        system = SYSTEM_PROMPT
        if user_context:
            system += "\n\n" + user_context
        if knowledge_ctx:
            system += "\n\n" + knowledge_ctx

        conversation_history[uid].append({"role": "user", "content": question})
        conversation_history[uid] = conversation_history[uid][-20:]

        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            system=system,
            messages=conversation_history[uid],
        )
        reply = response.content[0].text

        # Security: block reply if it somehow contains sensitive data
        if contains_sensitive(reply):
            logger.warning(f"Blocked sensitive reply for user {user_id}: {reply[:100]}")
            reply = "Hey, I can't share that info. Is there something else I can help you with?"

        conversation_history[uid].append({"role": "assistant", "content": reply})
        save_history(conversation_history)

        asyncio.create_task(extract_and_save_memory(user_id, question, reply))
        asyncio.create_task(extract_general_knowledge(question, reply))

        return reply
    except Exception as e:
        logger.error(f"Claude error: {e}")
        return "Hey give me a sec, something's not working on my end. Try again!"


# ── Owner commands ────────────────────────────────────────────────────────────

async def away_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    os.environ["AWAY_MODE"] = "true"
    await update.message.reply_text("Away mode ON. I'll handle employee messages with Claude.")


async def away_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    os.environ["AWAY_MODE"] = "false"
    await update.message.reply_text("Away mode OFF. Replies are paused.")


async def list_employees(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    pending = [
        f"• {data['name']} (ID: {uid}) — missing: {', '.join(d for d in REQUIRED_DOCUMENTS if d not in data['docs'])}"
        for uid, data in employee_data.items()
        if len(data["docs"]) < len(REQUIRED_DOCUMENTS)
    ]
    if pending:
        await update.message.reply_text("Employees with pending documents:\n" + "\n".join(pending))
    else:
        await update.message.reply_text("All employees have submitted their documents.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if user.id == OWNER_ID:
        await update.message.reply_text(
            "👋 Owner panel:\n"
            "/away_on — enable auto-reply\n"
            "/away_off — disable auto-reply\n"
            "/employees — check document status"
        )
        return ConversationHandler.END

    # Already known employee
    if user.id in known_employees:
        await update.message.reply_text(
            f"Welcome back, {user.first_name}! 👋\nHow can I help you today?"
        )
        return ConversationHandler.END

    # New person — ask who they are
    keyboard = ReplyKeyboardMarkup(
        [["✅ I'm a current employee"], ["🚛 I want to work with you"]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await update.message.reply_text(
        f"👋 Welcome to *Long Run Trucking LLC*!\n\n"
        f"Please select one of the options below:",
        parse_mode="Markdown",
        reply_markup=keyboard
    )
    return ASK_TYPE


async def handle_user_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text

    if "current employee" in text:
        known_employees.add(user.id)
        save_known_employees(known_employees)
        await update.message.reply_text(
            f"Welcome back! 👋 How can I help you today?",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END

    elif "want to work" in text:
        employee_data[user.id] = {"name": user.full_name, "docs": {}, "recruiting": True, "pitch_done": False}
        # Start recruiting — Mike must pitch pay and benefits FIRST
        intro_prompt = (
            f"A new driver just tapped 'I want to work with you'. Their name is {user.full_name}. "
            f"Start with a short warm greeting, then immediately pitch our offer: "
            f"Solo drivers earn $0.70–$0.75/mile or 28%–31% of gross load, team drivers $1.00/mile, $500 sign-on bonus, paid every Friday. "
            f"Fuel card covered at Pilot, Flying J, Love's, TA. Home time: 4 weeks out = 4 days home, 5 weeks out = 5 days home. "
            f"We run Freightliner, Volvo, Mack, Peterbilt — OTR loads (Amazon, JB Hunt, FedEx, USPS). "
            f"$300 referral bonus, $500 inspection bonus, birthday gift, detention pay $150/day. "
            f"Keep it SHORT and natural — like texting. Then ask what they're currently doing or what they're looking for. "
            f"DO NOT ask for documents yet."
        )
        reply = await ask_claude(user.id, intro_prompt)
        await update.message.reply_text(
            reply,
            reply_markup=ReplyKeyboardRemove()
        )
        return RECRUITING

    else:
        await update.message.reply_text("Please choose one of the options.")
        return ASK_TYPE


async def recruiting_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle recruiting conversation. Pitch first, then collect docs after driver is interested."""
    user = update.effective_user
    text = update.message.text or ""

    data = employee_data.get(user.id, {})
    pitch_done = data.get("pitch_done", False)

    # Driver signals interest/agreement
    ready_keywords = ["yes", "i'm in", "im in", "let's do it", "lets do it", "ready", "sure",
                      "sounds good", "sign me up", "let's go", "lets go", "deal", "i'm interested",
                      "works for me", "i like it", "send it", "let's start", "i want to", "i'll do it"]
    driver_agreed = any(kw in text.lower() for kw in ready_keywords)

    # If driver agreed AND pitch was already done → go to docs
    if pitch_done and driver_agreed:
        if "docs" not in employee_data.get(user.id, {}):
            employee_data[user.id]["docs"] = {}
        await update.message.reply_text(
            "Perfect! Just need 3 quick things:\n1️⃣ CDL photo (front & back)\n2️⃣ Medical card photo\n3️⃣ Your phone number\n\nSend your *CDL photo* first 📸",
            parse_mode="Markdown"
        )
        return COLLECT_DOCS

    # Otherwise keep the conversation going through Claude
    reply = await ask_claude(user.id, text)

    # Check if Claude itself is asking for docs (natural transition)
    transition_phrases = ["send your cdl", "need your cdl", "just need 3", "just need three",
                          "let's get your doc", "lets get your doc", "send me your cdl", "upload your cdl",
                          "send cdl", "cdl photo first"]
    claude_transitioning = any(p in reply.lower() for p in transition_phrases)

    await update.message.reply_text(reply)

    # Mark pitch as done after first back-and-forth
    if user.id in employee_data:
        employee_data[user.id]["pitch_done"] = True

    if claude_transitioning:
        if user.id not in employee_data:
            employee_data[user.id] = {"name": user.full_name, "docs": {}}
        elif "docs" not in employee_data[user.id]:
            employee_data[user.id]["docs"] = {}
        return COLLECT_DOCS

    return RECRUITING


async def prompt_next_document(update: Update, user_id: int):
    submitted = employee_data[user_id]["docs"]
    missing = [d for d in REQUIRED_DOCUMENTS if d not in submitted]
    if missing:
        doc = missing[0]
        if doc == "Phone number":
            await update.message.reply_text("📱 Last one — drop your phone number.")
        else:
            await update.message.reply_text(
                f"✅ Got it. Now send your *{doc}* 👇",
                parse_mode="Markdown"
            )


async def receive_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if user.id not in employee_data:
        return ConversationHandler.END

    data = employee_data[user.id]
    missing = [d for d in REQUIRED_DOCUMENTS if d not in data["docs"]]

    if not missing:
        return ConversationHandler.END

    current_doc = missing[0]

    # Handle phone number as text
    if current_doc == "Phone number":
        if update.message.text:
            data["docs"][current_doc] = update.message.text
            if OWNER_ID:
                await context.bot.send_message(
                    OWNER_ID,
                    f"📞 Phone number from {user.full_name}: {update.message.text}"
                )
        else:
            await update.message.reply_text("Please just type your phone number 👇")
            return COLLECT_DOCS
    else:
        # Accept photo or document file
        file_id = None
        if update.message.photo:
            file_id = update.message.photo[-1].file_id
        elif update.message.document:
            file_id = update.message.document.file_id

        if not file_id:
            await update.message.reply_text(f"📸 Send your *{current_doc}* as a photo — just take a picture and send it here 👇", parse_mode="Markdown")
            return COLLECT_DOCS

        data["docs"][current_doc] = file_id
        if OWNER_ID:
            await context.bot.send_message(
                OWNER_ID,
                f"📄 {current_doc} received from {user.full_name} (@{user.username})"
            )
            await context.bot.forward_message(OWNER_ID, update.message.chat_id, update.message.message_id)

    await update.message.reply_text(f"✅ Got it!")

    remaining = [d for d in REQUIRED_DOCUMENTS if d not in data["docs"]]
    if remaining:
        await prompt_next_document(update, user.id)
        return COLLECT_DOCS

    # All collected — notify owner with summary
    known_employees.add(user.id)
    save_known_employees(known_employees)

    phone = data["docs"].get("Phone number", "N/A")
    if OWNER_ID:
        await context.bot.send_message(
            OWNER_ID,
            f"🚛 *New driver application complete!*\n\n"
            f"👤 Name: {user.full_name}\n"
            f"📱 Phone: {phone}\n"
            f"🔗 Telegram: @{user.username}\n\n"
            f"CDL ✅ | Medical card ✅ | Phone ✅\n\n"
            f"Ready for your call to finalize! 📞",
            parse_mode="Markdown"
        )

    await update.message.reply_text(
        f"✅ Got everything! We'll review and call you soon to finalize. Welcome aboard 🚛",
        parse_mode="Markdown"
    )
    return ConversationHandler.END


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text or ""

    # OWNER always gets full manager access — auto-add to manager sessions
    if user.id == OWNER_ID:
        manager_sessions.add(user.id)

    # ── Security: blocked users ───────────────────────────────────────────────
    if user.id in _blocked_users:
        await update.message.reply_text("Sorry, I can't help with that.")
        return

    # ── Security: rate limiting ───────────────────────────────────────────────
    if user.id not in manager_sessions and is_rate_limited(user.id):
        await update.message.reply_text("You're sending too many messages. Please wait a minute.")
        return

    # ── Security: jailbreak detection ─────────────────────────────────────────
    if user.id not in manager_sessions and is_jailbreak(text):
        _blocked_users.add(user.id)
        logger.warning(f"🚨 JAILBREAK ATTEMPT from {user.full_name} (@{user.username}) ID:{user.id}: {text[:200]}")
        if OWNER_ID:
            try:
                await context.bot.send_message(
                    OWNER_ID,
                    f"🚨 *Security Alert*\n\n"
                    f"User: {user.full_name} (@{user.username})\n"
                    f"ID: {user.id}\n"
                    f"Attempted jailbreak:\n`{text[:300]}`",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
        await update.message.reply_text("I can't help with that.")
        return

    # ── Manager password check ────────────────────────────────────────────────
    if text.strip() == MANAGER_PASSWORD and user.id not in manager_sessions:
        manager_sessions.add(user.id)
        save_manager_sessions(manager_sessions)
        await update.message.reply_text(
            "✅ Manager mode activated. How can I help you?",
        )
        return

    # ── Manager logout ────────────────────────────────────────────────────────
    if text.strip().lower() == "logout" and user.id in manager_sessions and user.id != OWNER_ID:
        manager_sessions.discard(user.id)
        save_manager_sessions(manager_sessions)
        await update.message.reply_text("👋 Logged out of manager mode.")
        return

    # ── Employee mode — registered drivers ───────────────────────────────────
    driver = get_driver_by_telegram(user.id)

    # Auto-register if driver messaged bot for first time after getting SMS
    if not driver and user.id not in manager_sessions:
        # Check if they identify themselves with their phone number
        phone_in_text = re.search(r'\b(\d{10,11})\b', text)
        if phone_in_text:
            matched = await register_driver_telegram(phone_in_text.group(1), user.id)
            if matched:
                driver = get_driver_by_telegram(user.id)

    if driver and user.id not in manager_sessions:
        first = driver["name"].split()[0]
        t = text.strip().lower()

        # ── First time greeting ───────────────────────────────────────────────
        if t in ("start", "/start", "hi", "hello", "hey"):
            await update.message.reply_text(
                f"Hey {first}! 👋 I'm Mike, your assistant at Long Run Trucking.\n\n"
                f"Text me anytime for anything:\n\n"
                f"🔴 *breakdown* [details] — I'll alert dispatch immediately\n"
                f"🏠 *home time* [dates] — request days off\n"
                f"❓ Any question — I'm here 24/7",
                parse_mode="Markdown"
            )
            return

        # ── Breakdown ─────────────────────────────────────────────────────────
        if any(w in t for w in ("breakdown", "broke down", "truck broke", "not starting", "wont start", "dead truck")):
            details = text.strip()
            await log_checkin(driver, "BREAKDOWN", details, context.bot, OWNER_ID)
            await update.message.reply_text(
                f"🔴 Got you {first} — alerting dispatch RIGHT NOW.\n\n"
                f"While you wait:\n"
                f"• Put your hazard lights on\n"
                f"• Get off the road if possible\n"
                f"• Stay with the truck\n"
                f"• Keep your ELD on duty status\n\n"
                f"What's your location? (city/highway/mile marker)"
            )
            if OWNER_ID:
                try:
                    await context.bot.send_message(
                        OWNER_ID,
                        f"🔴 *BREAKDOWN — {driver['name']}* (Truck {driver.get('truck','?')})\n{details}\n📞 {driver['phone']}",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
            return

        # ── Home time request ─────────────────────────────────────────────────
        if any(w in t for w in ("home time", "days off", "time off", "vacation", "need home")):
            dates = text.strip()
            req = {
                "name": driver["name"],
                "phone": driver["phone"],
                "dates": dates,
                "telegram_id": user.id,
                "requested_at": time.strftime("%Y-%m-%d %H:%M"),
                "status": "pending",
            }
            hometime_requests.append(req)
            save_hometime(hometime_requests)
            await update.message.reply_text(
                f"🏠 Home time request sent {first}! Manager will confirm within 24 hours."
            )
            if OWNER_ID:
                try:
                    await context.bot.send_message(
                        OWNER_ID,
                        f"🏠 *Home time request — {driver['name']}*\n{dates}\n"
                        f"Reply `approve: {driver['name'].split()[0]}` or `deny: {driver['name'].split()[0]}`",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
            return

        # ── Anything else — Mike answers as AI assistant ───────────────────────
        # Fall through to the AI response below (same as regular drivers)

    # ── Manager mode — full internal assistant ────────────────────────────────
    if user.id in manager_sessions:

        # ── Recruiting call command ───────────────────────────────────────────
        is_call_command = re.match(r'^\s*call\b', text, re.IGNORECASE)
        if is_call_command:
            # Detect language
            if re.search(r'\buz(bek)?\b', text, re.IGNORECASE):
                call_language = "uz"
            elif re.search(r'\b(ru|rus|russian)\b', text, re.IGNORECASE):
                call_language = "ru"
            else:
                call_language = "en"
            clean_text = re.sub(r'\b(uzbek|uz|russian|rus|ru)\b', '', text, flags=re.IGNORECASE).strip()

            # Extract phone number if present
            phone_match = re.search(r'(\+?1?\s?[\(\d][\d\s\(\)\-]{9,})', clean_text)
            # Extract name
            name_match = re.search(r'call\s+([a-zA-Z][a-zA-Z\s]{1,30}?)(?:\s+\+?[\d\(]|$)', clean_text, re.IGNORECASE)
            driver_name = name_match.group(1).strip() if name_match else ""

            if not phone_match:
                context.user_data["pending_call_name"] = driver_name
                context.user_data["pending_call_lang"] = call_language
                name_str = f"{driver_name}'s" if driver_name else "the driver's"
                await update.message.reply_text(f"📞 What's {name_str} phone number?")
                return

            raw_phone = re.sub(r'[\s\(\)\-]', '', phone_match.group(1))
            if not raw_phone.startswith('+'):
                raw_phone = '+1' + raw_phone.lstrip('1')

            if not driver_name:
                driver_name = context.user_data.get("pending_call_name", "")
            context.user_data.pop("pending_call_name", None)
            context.user_data.pop("pending_call_lang", None)

            lang_label = "🇺🇿 Uzbek" if call_language == "uz" else ("🇷🇺 Russian" if call_language == "ru" else "🇺🇸 English")
            await update.message.reply_text(f"📞 Calling {driver_name or 'driver'} at {raw_phone} [{lang_label}]... Mike is dialing now!\n\nI'll send you a summary when the call ends. 🧠")
            result = await make_recruiting_call(raw_phone, driver_name, language=call_language)
            if result.get("status") == "success":
                call_id = result.get("call_id", "")
                asyncio.create_task(monitor_call_and_notify(call_id, user.id, driver_name, raw_phone, context.bot))
            else:
                await update.message.reply_text(f"❌ Call failed: {result.get('message', 'Unknown error')}")
            return

        # ── Phone number reply for pending call ───────────────────────────────
        if "pending_call_name" in context.user_data and re.search(r'[\d]{7,}', text):
            driver_name = context.user_data.pop("pending_call_name", "")
            call_language = context.user_data.pop("pending_call_lang", "en")
            raw_phone = re.sub(r'[\s\(\)\-]', '', text.strip())
            if not raw_phone.startswith('+'):
                raw_phone = '+1' + raw_phone.lstrip('1')
            lang_label = "🇺🇿 Uzbek" if call_language == "uz" else ("🇷🇺 Russian" if call_language == "ru" else "🇺🇸 English")
            await update.message.reply_text(f"📞 Calling {driver_name or 'driver'} at {raw_phone} [{lang_label}]... Mike is dialing now!\n\nI'll send you a summary when the call ends. 🧠")
            result = await make_recruiting_call(raw_phone, driver_name, language=call_language)
            if result.get("status") == "success":
                call_id = result.get("call_id", "")
                asyncio.create_task(monitor_call_and_notify(call_id, user.id, driver_name, raw_phone, context.bot))
            else:
                await update.message.reply_text(f"❌ Call failed: {result.get('message', 'Unknown error')}")
            return

        # ── Teach command: "teach: [fact]" ────────────────────────────────────
        if text.lower().startswith("teach:"):
            fact = text[6:].strip()
            if fact:
                knowledge_base.setdefault("facts", []).append(f"[{time.strftime('%Y-%m-%d')}] [MANAGER TAUGHT] {fact}")
                knowledge_base["facts"] = knowledge_base["facts"][-200:]
                save_knowledge_base(knowledge_base)
                await update.message.reply_text(f"✅ Got it! Mike learned: {fact}")
            else:
                await update.message.reply_text("Usage: teach: [fact to teach Mike]")
            return

        # ── Search command: "search: [topic]" ──────────────────────────────────
        if text.lower().startswith("search:"):
            topic = text[7:].strip()
            if topic:
                await update.message.reply_text(f"🔍 Searching for: {topic}...")
                result = await learn_from_web(topic)
                await update.message.reply_text(f"🌐 *Web results for '{topic}':*\n\n{result}", parse_mode="Markdown")
            else:
                await update.message.reply_text("Usage: search: [topic to search]")
            return

        # ── Queue status command: "queue" ──────────────────────────────────────
        if text.strip().lower() in ("queue", "queue status", "leads queue"):
            pending = [l for l in lead_queue if not l.get("called")]
            called = [l for l in lead_queue if l.get("called")]
            preview = "\n".join(f"  {i+1}. {l['name']} — {l['phone']} ({l.get('location','')})" for i, l in enumerate(pending[:10]))
            await update.message.reply_text(
                f"📋 *Lead Queue Status*\n\n"
                f"⏳ Waiting to call: *{len(pending)}*\n"
                f"✅ Already called: *{len(called)}*\n"
                f"📊 Total collected: *{len(lead_queue)}*\n\n"
                f"*Next up:*\n{preview or 'Queue is empty — waiting for daily hunt.'}\n\n"
                f"Say *'call queue: 10'* to start calling.",
                parse_mode="Markdown"
            )
            return

        # ── Call queue command: "call queue: N" ────────────────────────────────
        queue_match = re.match(r'call\s+queue[:\s]+(\d+)', text, re.IGNORECASE)
        if queue_match:
            n = min(int(queue_match.group(1)), 50)
            pending = [l for l in lead_queue if not l.get("called")]
            if not pending:
                await update.message.reply_text("📋 No leads in queue yet. Mike is hunting for drivers daily — check back soon or say `leads: Florida` to search now.")
                return
            to_call = pending[:n]
            await update.message.reply_text(
                f"📞 Starting calls to *{len(to_call)}* drivers from the queue...\n"
                f"I'll send you a summary after each call. 🧠",
                parse_mode="Markdown"
            )
            for lead in to_call:
                # Mark as called
                for q in lead_queue:
                    if q.get("clean_phone") == lead.get("clean_phone"):
                        q["called"] = True
                        q["called_at"] = time.strftime("%Y-%m-%d %H:%M")
                save_lead_queue(lead_queue)

                raw_phone = lead.get("clean_phone", "")
                if not raw_phone.startswith("+"):
                    raw_phone = "+1" + raw_phone.lstrip("1")
                driver_name = lead.get("name", "Driver")

                result = await make_recruiting_call(raw_phone, driver_name, language="en")
                if result.get("status") == "success":
                    call_id = result.get("call_id", "")
                    asyncio.create_task(monitor_call_and_notify(call_id, user.id, driver_name, raw_phone, context.bot))
                    await asyncio.sleep(2)
                else:
                    await update.message.reply_text(f"❌ Could not call {driver_name} ({raw_phone}): {result.get('message','error')}")
            return

        # ── Leads command: "leads: [location]" ─────────────────────────────────
        if text.lower().startswith("leads:"):
            location = text[6:].strip()
            await update.message.reply_text(f"🔍 Searching for CDL-A driver leads{' in ' + location if location else ''}...\nThis may take a minute.")
            leads = await search_cdl_leads(location=location, count=20)
            if not leads:
                await update.message.reply_text("😕 Couldn't find leads right now. Try a specific state or city, e.g.: `leads: Texas`")
                return
            # Save to Google Sheets in background thread
            saved = 0
            for lead in leads:
                ok = await save_lead_to_sheet(
                    name=lead["name"],
                    phone=lead["phone"],
                    experience="",
                    solo_team="",
                    status="New Lead",
                    notes=f"Found via web search — {location}",
                )
                if ok:
                    saved += 1
            # Show summary
            summary = "\n".join(f"• {l['name']} — {l['phone']}" for l in leads[:10])
            await update.message.reply_text(
                f"✅ Found *{len(leads)}* leads, saved *{saved}* to Google Sheets\n\n"
                f"*Preview (first 10):*\n{summary}\n\n"
                f"📊 Full list → your Google Sheet",
                parse_mode="Markdown"
            )
            return

        # ── Hunt now command: "hunt now" / "find 100" / "start hunt" ─────────────
        if re.search(r'\b(hunt\s+now|find\s+100|start\s+hunt|run\s+hunt|hunt\s+leads?)\b', text, re.IGNORECASE):
            pending = [l for l in lead_queue if not l.get("called")]
            if len(pending) >= 50:
                await update.message.reply_text(
                    f"📋 Already have *{len(pending)}* drivers waiting to be called.\n"
                    f"Say *'call queue: 10'* to start calling them.\n\n"
                    f"Start a new hunt anyway? Reply *'hunt force'* to override.",
                    parse_mode="Markdown"
                )
                return
            await update.message.reply_text(
                "🚀 *Starting full lead hunt now!*\n\n"
                "Mike will search Craigslist, Indeed resumes, Facebook groups, "
                "TruckerReport, and more — using Claude AI to verify every lead is "
                "a real driver (not a company).\n\n"
                "Target: *100 verified CDL-A drivers*\n"
                "You'll get live updates as leads come in. This takes 20-40 min. ⏳",
                parse_mode="Markdown"
            )
            asyncio.create_task(daily_lead_hunter(bot=context.bot))
            return

        if re.search(r'hunt\s+force', text, re.IGNORECASE):
            await update.message.reply_text(
                "🔄 *Force-starting hunt* — will add new drivers on top of existing queue.",
                parse_mode="Markdown"
            )
            asyncio.create_task(daily_lead_hunter(bot=context.bot))
            return

        # ── Manager dashboard: "status" ───────────────────────────────────────
        if text.strip().lower() in ("status", "dashboard", "report"):
            pending   = [l for l in lead_queue if not l.get("called")]
            called    = [l for l in lead_queue if l.get("called")]
            hot       = [l for l in lead_queue if "Hot" in l.get("status", "")]
            warm      = [l for l in lead_queue if "Warm" in l.get("status", "")]
            onboarding= list(onboarding_pipeline.values())
            followups = [f for f in followup_queue if f.get("attempted", 0) < 2]

            # Build onboarding section
            ob_lines = ""
            for d in onboarding[:5]:
                step = d.get("step", 0)
                step_name = ONBOARD_STEPS[step] if step < len(ONBOARD_STEPS) else "Complete ✅"
                ob_lines += f"  • {d['name']} — Step {step+1}: {step_name}\n"

            msg = (
                f"📊 *Mike HR Dashboard*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📋 *Leads*\n"
                f"  ⏳ Waiting to call: {len(pending)}\n"
                f"  ✅ Called: {len(called)}\n"
                f"  🔥 Hot leads: {len(hot)}\n"
                f"  🌡️ Warm leads: {len(warm)}\n\n"
                f"🚛 *Onboarding* ({len(onboarding)} drivers)\n"
                f"{ob_lines or '  None yet'}\n"
                f"⏰ *Follow-ups pending:* {len(followups)}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Commands:\n"
                f"• `hunt now` — find 100 new leads\n"
                f"• `call queue: N` — call N drivers\n"
                f"• `onboard: [phone]` — start onboarding\n"
                f"• `step: [phone]` — advance to next step\n"
                f"• `followups` — see follow-up list\n"
            )
            await update.message.reply_text(msg, parse_mode="Markdown")
            return

        # ── Follow-ups list: "followups" ──────────────────────────────────────
        if text.strip().lower() in ("followups", "follow-ups", "follow ups"):
            pending_fu = [f for f in followup_queue if f.get("attempted", 0) < 2]
            if not pending_fu:
                await update.message.reply_text("✅ No follow-ups pending right now.")
                return
            lines = []
            for f in pending_fu[:15]:
                due = time.strftime("%m/%d %H:%M", time.localtime(f.get("follow_up_at", 0)))
                lines.append(f"• {f['name']} ({f['phone']}) — attempt #{f['attempted']+1} due {due}")
            await update.message.reply_text(
                f"⏰ *Pending Follow-ups ({len(pending_fu)}):*\n\n" + "\n".join(lines),
                parse_mode="Markdown"
            )
            return

        # ── Onboarding: "onboard: [phone]" ────────────────────────────────────
        onboard_match = re.match(r'onboard[:\s]+(\+?[\d\s\-]+)', text, re.IGNORECASE)
        if onboard_match:
            raw = re.sub(r'\D', '', onboard_match.group(1))
            phone = "+1" + raw.lstrip("1") if not raw.startswith("+") else raw
            # Find driver name from queue
            driver = next((l for l in lead_queue if l.get("clean_phone", "").endswith(raw[-10:])), None)
            name = driver["name"] if driver else "Driver"
            await start_onboarding(phone, name, context.bot, user.id)
            return

        # ── Advance onboarding step: "step: [phone]" ──────────────────────────
        step_match = re.match(r'step[:\s]+(\+?[\d\s\-]+)', text, re.IGNORECASE)
        if step_match:
            raw = re.sub(r'\D', '', step_match.group(1))
            phone = "+1" + raw.lstrip("1") if not raw.startswith("+") else raw
            # Try matching last 10 digits
            matched = next((k for k in onboarding_pipeline if k.endswith(raw[-10:])), None)
            if matched:
                await advance_onboarding(matched, context.bot, user.id)
            else:
                await update.message.reply_text(f"❌ No active onboarding found for {phone}. Use `onboard: {phone}` to start.")
            return

        # ── Add driver: "add driver: John Smith, +18131234567, Truck 101, 0.75/mile" ──
        add_driver_match = re.match(
            r'add\s+driver[:\s]+([^,]+),\s*(\+?[\d\s\-]+),?\s*(truck\s*\S+)?,?\s*([\d.]+)?\s*/?(\S+)?',
            text, re.IGNORECASE
        )
        if add_driver_match:
            d_name  = add_driver_match.group(1).strip().title()
            d_phone = add_driver_match.group(2).strip()
            d_truck = (add_driver_match.group(3) or "TBD").strip()
            d_rate  = float(add_driver_match.group(4) or 0.75)
            d_type  = "per_mile" if d_rate < 5 else "percentage"
            await add_driver(d_name, d_phone, d_truck, d_type, d_rate, context.bot, user.id)
            return

        # ── Driver list: "drivers" ────────────────────────────────────────────
        if text.strip().lower() in ("drivers", "driver list", "my drivers"):
            active = [(p, d) for p, d in driver_registry.items() if d.get("status") == "active"]
            if not active:
                await update.message.reply_text("No drivers registered yet. Use `add driver: Name, phone, truck, rate`")
                return
            lines = []
            for phone, d in active[:20]:
                tg = "✅" if d.get("telegram_id") else "⏳"
                lines.append(f"{tg} {d['name']} | Truck {d.get('truck','?')} | ${d['rate']}/{d['rate_type'].replace('per_','')} | {phone}")
            await update.message.reply_text(
                f"🚛 *Active Drivers ({len(active)}):*\n\n" + "\n".join(lines) + "\n\n✅=on Telegram ⏳=SMS sent only",
                parse_mode="Markdown"
            )
            return

        # ── Home time approve/deny: "approve: John" / "deny: John" ─────────────
        approve_match = re.match(r'(approve|deny)[:\s]+(.+)', text, re.IGNORECASE)
        if approve_match:
            action = approve_match.group(1).lower()
            name_q = approve_match.group(2).strip().lower()
            matched = [r for r in hometime_requests
                       if name_q in r.get("name", "").lower() and r.get("status") == "pending"]
            if not matched:
                await update.message.reply_text(f"No pending home time request found for '{name_q}'.")
                return
            req = matched[0]
            req["status"] = action + "d"
            save_hometime(hometime_requests)
            # Notify driver via Telegram if connected
            driver = get_driver_by_phone(req["phone"])
            if driver and driver.get("telegram_id"):
                msg = (f"✅ Your home time request ({req.get('dates','')}) has been *approved*! Safe travels 🏠"
                       if action == "approve"
                       else f"❌ Your home time request ({req.get('dates','')}) was denied. Please contact dispatch to discuss.")
                try:
                    await context.bot.send_message(driver["telegram_id"], msg, parse_mode="Markdown")
                except Exception:
                    pass
            await update.message.reply_text(f"{'✅ Approved' if action == 'approve' else '❌ Denied'} home time for {req['name']}.")
            return

        # ── Weigh stations: "weigh stations" / "weigh stations: Florida, Texas" ──
        if re.search(r'weigh\s*station', text, re.IGNORECASE):
            # Check specific states or all
            state_match = re.search(r'weigh\s*station[s]?[:\s]+(.+)', text, re.IGNORECASE)
            if state_match:
                requested = [s.strip().title() for s in state_match.group(1).split(',')]
            else:
                requested = list(weigh_status_cache.keys()) or MONITOR_STATES[:5]

            if not weigh_status_cache:
                await update.message.reply_text("⏳ Checking weigh stations now — this takes ~1 min...")
                await check_weigh_stations_all(context.bot)

            lines = []
            for state in requested:
                stations = weigh_status_cache.get(state, [])
                if not stations:
                    lines.append(f"*{state}:* No data yet")
                    continue
                for s in stations:
                    emoji = "🟢" if s["status"] == "OPEN" else ("🔴" if s["status"] == "CLOSED" else "⚪")
                    direction = f" ({s['direction']})" if s.get("direction") else ""
                    lines.append(f"{emoji} *{state}* — {s['name']}{direction}: {s['status']}")

            if not lines:
                await update.message.reply_text("No weigh station data available yet. Mike checks every 30 min automatically.")
                return

            await update.message.reply_text(
                "⚖️ *Weigh Station Status*\n\n" + "\n".join(lines),
                parse_mode="Markdown"
            )
            return

        # ── Manual broadcast: "broadcast: [message]" ──────────────────────────
        broadcast_match = re.match(r'broadcast[:\s]+(.+)', text, re.IGNORECASE | re.DOTALL)
        if broadcast_match:
            msg = broadcast_match.group(1).strip()
            active_drivers = [d for d in driver_registry.values() if d.get("telegram_id")]
            if not active_drivers:
                await update.message.reply_text("No drivers connected on Telegram yet.")
                return
            await update.message.reply_text(f"📡 Broadcasting to {len(active_drivers)} drivers...")
            sent = await broadcast_to_drivers(f"📢 *Long Run Trucking:*\n{msg}", context.bot)
            await update.message.reply_text(f"✅ Sent to {sent}/{len(active_drivers)} drivers.")
            return

        # ── Upload creds command: handle JSON credentials file ──────────────────

        # ── Transcript command: "transcript [call_id]" ────────────────────────
        transcript_match = re.search(r'transcript\s+([a-f0-9\-]{36})', text, re.IGNORECASE)
        if transcript_match:
            call_id = transcript_match.group(1)
            await update.message.reply_text("📋 Getting call transcript...")
            transcript = await get_call_transcript(call_id)
            await update.message.reply_text(f"📞 *Call Transcript:*\n\n{transcript}", parse_mode="Markdown")
            return

        reply = await ask_claude_manager(user.id, text)
        await update.message.reply_text(reply)
        return

    # ── Regular driver/applicant flow ─────────────────────────────────────────
    if OWNER_ID:
        try:
            await context.bot.send_message(
                OWNER_ID,
                f"💬 Message from {user.full_name} (@{user.username}):\n{text}"
            )
        except Exception:
            pass

    if not is_away():
        return

    reply = await ask_claude(user.id, text)
    await update.message.reply_text(reply)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photos/documents sent by known employees or manager (Google creds JSON)."""
    user = update.effective_user

    # Manager sending Google service account JSON credentials
    if user.id == OWNER_ID and update.message.document:
        doc = update.message.document
        if doc.file_name and doc.file_name.endswith(".json"):
            await update.message.reply_text("📥 Saving Google credentials...")
            try:
                file = await context.bot.get_file(doc.file_id)
                creds_bytes = await file.download_as_bytearray()
                creds_data = json.loads(creds_bytes.decode())
                # Validate it's a service account file
                if creds_data.get("type") == "service_account":
                    Path(GOOGLE_SHEETS_CREDS_FILE).parent.mkdir(parents=True, exist_ok=True)
                    with open(GOOGLE_SHEETS_CREDS_FILE, "w") as f:
                        json.dump(creds_data, f)
                    svc_email = creds_data.get("client_email", "unknown")
                    await update.message.reply_text(
                        f"✅ Google credentials saved!\n\n"
                        f"Service account email:\n`{svc_email}`\n\n"
                        f"*Next steps:*\n"
                        f"1. Open your Google Sheet\n"
                        f"2. Click Share → add `{svc_email}` as Editor\n"
                        f"3. Copy the spreadsheet ID from the URL\n"
                        f"4. Send: `sheet: YOUR_SPREADSHEET_ID`",
                        parse_mode="Markdown"
                    )
                else:
                    await update.message.reply_text("❌ This doesn't look like a service account JSON. Please download the correct file from Google Cloud Console.")
            except Exception as e:
                await update.message.reply_text(f"❌ Failed to save credentials: {e}")
            return

    if user.id == OWNER_ID or user.id not in known_employees:
        return
    if OWNER_ID:
        try:
            await context.bot.send_message(OWNER_ID, f"📸 Photo/doc from {user.full_name} (@{user.username})")
            await context.bot.forward_message(OWNER_ID, update.message.chat_id, update.message.message_id)
        except Exception:
            pass
    if not is_away():
        return
    caption = update.message.caption or ""
    prompt = f"Driver sent a photo/document. Caption: '{caption}'. Acknowledge it and ask what it's for if the caption doesn't make it clear."
    reply = await ask_claude(user.id, prompt)
    await update.message.reply_text(reply)


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """When driver shares their live location, find nearby repair shops."""
    user = update.effective_user
    if user.id == OWNER_ID:
        return

    location = update.message.location
    lat, lon = location.latitude, location.longitude

    # Notify owner
    if OWNER_ID:
        try:
            await context.bot.send_message(
                OWNER_ID,
                f"📍 Location shared by {user.full_name} (@{user.username})\nLat: {lat}, Lon: {lon}"
            )
        except Exception:
            pass

    if not is_away():
        return

    await update.message.reply_text("📍 Got your location! Searching for nearby truck repair shops... 🔍")

    shops_text = await find_nearby_repair_shops(lat, lon)
    await update.message.reply_text(shops_text, parse_mode="Markdown")

    # Also ask Claude for advice based on location
    context_msg = f"Driver just shared their location (lat: {lat}, lon: {lon}). Google Maps found nearby shops. Ask them what the problem is so you can help further."
    reply = await ask_claude(user.id, context_msg)
    await update.message.reply_text(reply)


async def post_init(application):
    """Run after bot starts — kick off background learning and lead hunting."""
    asyncio.create_task(startup_learning())
    asyncio.create_task(periodic_learning_loop())
    asyncio.create_task(daily_lead_loop(bot=application.bot))
    asyncio.create_task(followup_loop(bot=application.bot))
    asyncio.create_task(weigh_station_loop(bot=application.bot))


async def handle_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """High-priority handler: catches manager password at any point in any flow."""
    user = update.effective_user
    text = (update.message.text or "").strip()
    if text == MANAGER_PASSWORD:
        manager_sessions.add(user.id)
        save_manager_sessions(manager_sessions)
        await update.message.reply_text(
            "✅ Manager mode activated. You can now use all manager commands.\n\n"
            "Commands:\n"
            "• `call [name] [phone]` — make a recruiting call\n"
            "• `leads: [state]` — find CDL-A driver leads\n"
            "• `teach: [fact]` — teach Mike something\n"
            "• `search: [topic]` — web search\n"
            "• `logout` — exit manager mode",
            parse_mode="Markdown"
        )
        return ConversationHandler.END
    return None


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Password filter — matches only the exact manager password
    password_filter = filters.TEXT & filters.Regex(f'^{re.escape(MANAGER_PASSWORD)}$')

    doc_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_TYPE: [
                MessageHandler(password_filter, handle_password),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_type),
            ],
            RECRUITING: [
                MessageHandler(password_filter, handle_password),
                MessageHandler(filters.TEXT & ~filters.COMMAND, recruiting_chat),
            ],
            COLLECT_DOCS: [
                MessageHandler(password_filter, handle_password),
                MessageHandler(filters.PHOTO | filters.Document.ALL | filters.TEXT, receive_document),
            ]
        },
        fallbacks=[CommandHandler("start", start), MessageHandler(password_filter, handle_password)],
    )

    # Global password handler — highest priority, catches password outside conversation too
    app.add_handler(MessageHandler(password_filter, handle_password), group=-1)
    app.add_handler(doc_conv)
    app.add_handler(CommandHandler("away_on", away_on))
    app.add_handler(CommandHandler("away_off", away_off))
    app.add_handler(CommandHandler("employees", list_employees))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":

    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
