# -*- coding: utf-8 -*-
"""
Project Falcon V2.5 - بوت تيليجرام لمحاكاة العمليات العسكرية (باللهجة العراقية)
المكتبة: pyTelegramBotAPI (telebot)
التثبيت:   pip install pyTelegramBotAPI
التشغيل:   python warfare_bot.py        (أو: BOT_TOKEN="xxxx" python warfare_bot.py)

الإدارة: كل مستخدم جديد توصلك بطاقته ببوت ثاني (ADMIN_BOT_TOKEN / ADMIN_CHAT_ID) وتتحكم بالسماح والحظر.
الميزات: رادار ودفاع جوي + مقاتلات + طيران الدعم الأرضي (CAS) والمضادات الأرضية والقفز بالمظلة
+ تتبع لحظي للإطلاق (حلقة تحديث كل ثانية)
+ حوارات + مؤتمر صحفي + تعليقات سوشيال + غرفة استراحة.
كل الأسماء والأحداث خيالية، والأرقام تقريبية لأغراض المحاكاة فقط.
"""
import os
import json
import math
import time
import random
import html
import threading
import traceback
from collections import defaultdict
from types import SimpleNamespace as NS

import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException

TOKEN = os.getenv("BOT_TOKEN", "8785340710:AAHHN-qNUo7jl59sUhH21O8BT3n4YK0_DJ0")
# num_threads كبير حتى ما يتعطل البوت وقت تشتغل حلقات التتبع
bot = telebot.TeleBot(TOKEN, parse_mode="HTML", threaded=True, num_threads=32)

DATA_FILE = "players.json"
_file_lock = threading.Lock()
_ulocks = defaultdict(threading.RLock)   # قفل لكل مستخدم
players = {}        # uid -> السجل العسكري (يُحفظ)
sessions = {}       # uid -> جلسة المهمة الحالية (مؤقتة)
awaiting_name = set()
last_ctx = {}       # uid -> ملخص آخر مهمة

MAX_MISSILES = 4    # أقصى عدد صواريخ بالاشتباك الواحد (الموجات)


def ulock(uid):
    return _ulocks[uid]


# ============================================================
# الرتب والسجل العسكري
# ============================================================
RANKS = [
    ("مجنّد", 0), ("جندي أول", 200), ("عريف", 500), ("رقيب", 900),
    ("رقيب أول", 1400), ("ملازم", 2000), ("ملازم أول", 2800), ("نقيب", 3800),
    ("رائد", 5000), ("مقدّم", 6500), ("عقيد", 8500), ("عميد", 11000), ("لواء", 14000),
]


def load_data():
    global players
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                players = {int(k): v for k, v in json.load(f).items()}
        except Exception:
            players = {}


def save_data():
    with _file_lock:
        try:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(players, f, ensure_ascii=False)
        except Exception:
            pass


def new_profile(name):
    return {"name": name, "points": 0, "kills": 0, "civil_inc": 0,
            "friendly_inc": 0, "missions": 0, "lock_until": 0, "lock_reason": "",
            "fatigue": 0, "shot_down": 0, "deaths": 0}


def rank_index(points):
    idx = 0
    for i, (_, need) in enumerate(RANKS):
        if points >= need:
            idx = i
    return idx


def rank_name(p):
    return RANKS[rank_index(p["points"])][0]


def add_points(p, n):
    """يضيف/يخصم نقاط ويرجّع نص الترقية إذا صارت."""
    old = rank_index(p["points"])
    p["points"] = max(0, p["points"] + n)
    new = rank_index(p["points"])
    save_data()
    if new > old:
        return f"\n\n🎖 <b>گرار ترقية:</b> انترقّيت لرتبة <b>{RANKS[new][0]}</b>. نشكرك على جهودك."
    return ""


def fmt_time(sec):
    sec = int(sec)
    return f"{sec // 60} د {sec % 60} ث"


def locked_left(p):
    return max(0, p.get("lock_until", 0) - time.time())


def apply_punishment(p, casualties, cause):
    """عقوبة متدرجة حسب حجم الخسائر البشرية."""
    if casualties >= 150:
        tier = 3
    elif casualties >= 30:
        tier = 2
    else:
        tier = 1
    if tier == 1:
        dur, p["points"] = 120, max(0, p["points"] - 150)
        txt = f"⛓ <b>الحكم:</b> حجز انضباطي لمدة {fmt_time(dur)} وخصم نقاط من أدائك."
    elif tier == 2:
        dur = 300
        i = max(0, rank_index(p["points"]) - 1)
        p["points"] = RANKS[i][1]
        txt = f"🚫 <b>الحكم:</b> إعفاء من المهام لمدة {fmt_time(dur)} وتنزيل رتبة واحدة."
    else:
        dur, p["points"] = 600, 0
        txt = ("⚖️ <b>الحكم:</b> فصل نهائي من الخدمة، والراتب انحجز، وانسحبت منك الرتبة "
               f"بالكامل، وما تگدر ترجع گبل {fmt_time(dur)}.")
    p["lock_until"] = time.time() + dur
    p["lock_reason"] = cause
    save_data()
    return txt


def inquiry_report(identified, label, victims_kind, casualties, money):
    if not identified:
        finding = "ثبت إن الإطلاق صار <b>بدون تحليل الهدف وبدون تحقق من هويته</b>، وبسبب الاستعجال."
    else:
        finding = "ثبت إن الإطلاق صار <b>ضد نتائج التحليل</b>، وهذا إهمال جسيم."
    return (
        "🕵️ <b>لجنة التحقيق العاجلة — تقرير أولي</b>\n"
        f"• نوع الهدف الحقيقي: {victims_kind}\n"
        f"• حالة الهدف وكت الاشتباك: {label}\n"
        f"• الخسائر البشرية: <b>{casualties}</b>\n"
        f"• الخسائر المادية التقديرية: <b>{money} مليون دولار</b>\n"
        f"• الاستنتاج: {finding}\n"
    )


# ============================================================
# أدوات الواجهة
# ============================================================
def mk(rows):
    m = types.InlineKeyboardMarkup()
    for row in rows:
        m.row(*[types.InlineKeyboardButton(t, callback_data=d) for t, d in row])
    return m


def edit(call, text, markup=None):
    chat_id, mid = call.message.chat.id, call.message.message_id
    try:
        bot.edit_message_text(text, chat_id, mid, reply_markup=markup)
    except ApiTelegramException as e:
        if "not modified" in str(e):
            return
        bot.send_message(chat_id, text, reply_markup=markup)
    except Exception:
        bot.send_message(chat_id, text, reply_markup=markup)


def send_new(call, text, markup=None):
    bot.send_message(call.message.chat.id, text, reply_markup=markup)


def safe_edit(chat_id, mid, text, markup=None):
    """تعديل آمن يستعمل بحلقة التتبع (يتعامل مع حدود تيليجرام)."""
    try:
        bot.edit_message_text(text, chat_id, mid, reply_markup=markup)
        return True
    except ApiTelegramException as e:
        msg = str(e)
        if "not modified" in msg:
            return True
        if "429" in msg or "Too Many Requests" in msg:
            wait = 2
            try:
                wait = int(e.result_json["parameters"]["retry_after"])
            except Exception:
                pass
            time.sleep(min(wait, 5))
        return False
    except Exception:
        return False


def fake_call(chat_id, mid):
    return NS(message=NS(chat=NS(id=chat_id), message_id=mid))


HOME_ROW = [("🏠 غرفة العمليات", "home")]


def fatigue_bar(p):
    f = min(3, p.get("fatigue", 0))
    return "▮" * f + "▯" * (3 - f) + f" ({f}/3)"


def profile_text(p):
    left = locked_left(p)
    status = "🟢 شغّال على راس عمله" if left <= 0 else f"🔴 موقوف ({fmt_time(left)})"
    return (
        "📋 <b>السجل العسكري</b>\n"
        f"الاسم: {html.escape(p['name'])}\n"
        f"الرتبة: <b>{rank_name(p)}</b>\n"
        f"نقاط الأداء: {p['points']}\n"
        f"أهداف معادية مدمّرة: {p['kills']}\n"
        f"حوادث استهداف مدني: {p['civil_inc']}\n"
        f"حوادث نيران صديقة: {p['friendly_inc']}\n"
        f"المهام المنفّذة: {p['missions']}\n"
        f"مرات انسگاط الطيارة: {p.get('shot_down', 0)} | الاستشهاد: {p.get('deaths', 0)}\n"
        f"مستوى الإرهاق: {fatigue_bar(p)}\n"
        f"الحالة: {status}"
    )


# ============================================================
# نظام الإدارة: إشعارات لبوت ثاني + التحكم بمن يلعب
# ============================================================
# بوت الإدارة = بوت ثاني (تسويه من @BotFather). تفتحه وتدوس /start حتى يعرف يراسلك.
# ADMIN_CHAT_ID = آيدي الدردشة مالتك (يطلعلك لمن تدوس /start ببوت الإدارة).
ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN", "8877747564:AAGLlQT7d76fOWC_c_jSf99iFJNL51Ke8C0")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "8298252705") or 0)
ADMIN_TOKEN_OK = ":" in ADMIN_BOT_TOKEN
ADMIN_ENABLED = ADMIN_TOKEN_OK and ADMIN_CHAT_ID != 0
admin_bot = telebot.TeleBot(ADMIN_BOT_TOKEN, parse_mode="HTML") if ADMIN_TOKEN_OK else None

ACCESS_FILE = "access.json"
_access_lock = threading.Lock()
access = {"mode": "open", "users": {}}   # mode: open (الكل يلعب) | approval (بس اللي توافق عليه)
STATUS_AR = {"allowed": "✅ مسموح", "banned": "⛔ محظور", "pending": "⏳ بانتظار الموافقة"}
MODE_AR = {"open": "مفتوح (الكل يلعب إلا المحظورين)", "approval": "بالموافقة (بس اللي تسمحله)"}


def load_access():
    global access
    if os.path.exists(ACCESS_FILE):
        try:
            with open(ACCESS_FILE, "r", encoding="utf-8") as f:
                access = json.load(f)
            access.setdefault("mode", "open")
            access.setdefault("users", {})
        except Exception:
            access = {"mode": "open", "users": {}}


def save_access():
    with _access_lock:
        try:
            with open(ACCESS_FILE, "w", encoding="utf-8") as f:
                json.dump(access, f, ensure_ascii=False)
        except Exception:
            pass


def user_card(title, uid):
    u = access["users"].get(str(uid), {})
    full = html.escape((u.get("first", "") + " " + u.get("last", "")).strip()) or "—"
    uname = f"@{html.escape(u['username'])}" if u.get("username") else "ماكو يوزر"
    lines = [title,
             f"👤 الاسم بتيليجرام: {full}",
             f"🔖 اليوزر: {uname}",
             f"🆔 الآيدي: <code>{uid}</code>"]
    if u.get("game_name"):
        lines.append(f"🎖 الاسم العسكري باللعبة: <b>{html.escape(u['game_name'])}</b>")
    lines.append(f"🕒 أول دخول: {time.strftime('%Y-%m-%d %H:%M', time.localtime(u.get('seen', time.time())))}")
    lines.append(f"📌 الحالة: {STATUS_AR.get(u.get('status', 'allowed'), '—')}")
    return "\n".join(lines)


def admin_kb(uid):
    return mk([[("✅ سماح", f"adm:allow:{uid}"), ("⛔ حظر", f"adm:ban:{uid}")],
               [("♻️ فك الحظر", f"adm:unban:{uid}")]])


def admin_notify(title, uid):
    """يبعث بطاقة المستخدم لبوت الإدارة (بخيط مستقل حتى ما يتأخر اللعب)."""
    if not ADMIN_ENABLED or str(uid) not in access["users"]:
        return
    text, kb = user_card(title, uid), admin_kb(uid)

    def _send():
        try:
            admin_bot.send_message(ADMIN_CHAT_ID, text, reply_markup=kb)
        except Exception:
            traceback.print_exc()
    threading.Thread(target=_send, daemon=True).start()


def register_user(tg):
    """يسجّل المستخدم بسجل الإدارة ويرجّع حالته: allowed / banned / pending."""
    uid = tg.id
    if ADMIN_CHAT_ID and uid == ADMIN_CHAT_ID:
        return "allowed"                      # صاحب البوت دايماً مسموح
    new = False
    with _access_lock:
        u = access["users"].get(str(uid))
        if u is None:
            new = True
            u = {"status": "allowed" if access["mode"] == "open" else "pending",
                 "seen": int(time.time()), "game_name": ""}
            access["users"][str(uid)] = u
        u["first"] = getattr(tg, "first_name", "") or ""
        u["last"] = getattr(tg, "last_name", "") or ""
        u["username"] = getattr(tg, "username", "") or ""
        status = u["status"]
    save_access()
    if new:
        admin_notify("🆕 <b>مستخدم جديد دخل البوت</b>", uid)
    return status


def deny_text(status):
    if status == "banned":
        return "⛔ انحظرت من استخدام البوت بقرار الإدارة."
    return "⏳ طلبك گيد المراجعة. لمن توافق الإدارة راح توصلك رسالة."


def gate_msg(m):
    st = register_user(m.from_user)
    if st != "allowed":
        bot.send_message(m.chat.id, deny_text(st))
        return False
    return True


def admin_apply(uid, status):
    """يغيّر حالة مستخدم (allowed / banned) ويبلّغه ويوگّف جلسته إذا انحظر."""
    with _access_lock:
        u = access["users"].setdefault(str(uid), {"seen": int(time.time()), "game_name": ""})
        u["status"] = status
    save_access()
    if status == "banned":
        with ulock(uid):
            sessions.pop(uid, None)
        awaiting_name.discard(uid)
    msg = {"banned": "⛔ انحظرت من استخدام البوت بقرار الإدارة.",
           "allowed": "✅ تمت الموافقة، تگدر تستخدم البوت هسه. ابعث /start."}[status]
    try:
        bot.send_message(uid, msg)
    except Exception:
        pass
    return f"{STATUS_AR[status]} — <code>{uid}</code>"


def admin_set_mode(mode):
    access["mode"] = mode
    save_access()
    return f"⚙️ الوضع صار: <b>{MODE_AR[mode]}</b>"


def admin_users_text():
    items = sorted(access["users"].items(), key=lambda kv: kv[1].get("seen", 0), reverse=True)[:30]
    lines = [f"👥 <b>المستخدمين ({len(access['users'])})</b> — الوضع: {MODE_AR[access['mode']]}\n"]
    for k, u in items:
        full = html.escape((u.get("first", "") + " " + u.get("last", "")).strip()) or "—"
        uname = f" @{html.escape(u['username'])}" if u.get("username") else ""
        gname = f" | 🎖 {html.escape(u['game_name'])}" if u.get("game_name") else ""
        lines.append(f"• {full}{uname}{gname} | <code>{k}</code> | {STATUS_AR.get(u.get('status'), '—')}")
    if len(lines) == 1:
        lines.append("ماكو مستخدمين لحد هسه.")
    return "\n".join(lines)


ADMIN_HELP = (
    "🛠 <b>لوحة إدارة Project Falcon</b>\n\n"
    "كل مستخدم جديد توصلك بطاقته هنا ويّا أزرار (سماح / حظر / فك الحظر).\n\n"
    "الأوامر:\n"
    "• /users — آخر 30 مستخدم\n"
    "• /ban <code>ID</code> — حظر\n"
    "• /unban <code>ID</code> — فك الحظر\n"
    "• /allow <code>ID</code> — سماح\n"
    "• /mode <code>open</code> — الكل يلعب إلا المحظورين\n"
    "• /mode <code>approval</code> — بس اللي توافق عليه يلعب"
)


def start_admin_bot():
    """يشغّل بوت الإدارة بخيط مستقل (أوامر + أزرار السماح والحظر)."""
    if not ADMIN_TOKEN_OK:
        print("ℹ️ بوت الإدارة مو مفعّل (حط ADMIN_BOT_TOKEN و ADMIN_CHAT_ID).")
        return
    ab = admin_bot

    def is_admin(user_id):
        return ADMIN_CHAT_ID != 0 and user_id == ADMIN_CHAT_ID

    @ab.message_handler(commands=["start", "help"])
    def a_start(m):
        if is_admin(m.from_user.id):
            ab.send_message(m.chat.id, ADMIN_HELP)
        else:
            ab.send_message(m.chat.id, f"🆔 آيدي الدردشة مالتك: <code>{m.from_user.id}</code>\n"
                            "حطه بـ ADMIN_CHAT_ID حتى تصير أنت الأدمن.")

    @ab.message_handler(commands=["users", "ban", "unban", "allow", "mode"])
    def a_cmd(m):
        if not is_admin(m.from_user.id):
            return ab.send_message(m.chat.id, "⛔ هذا البوت للإدارة بس.")
        parts = m.text.split()
        cmd = parts[0].split("@")[0].lstrip("/")
        if cmd == "users":
            return ab.send_message(m.chat.id, admin_users_text())
        if cmd == "mode":
            if len(parts) < 2 or parts[1] not in MODE_AR:
                return ab.send_message(m.chat.id, "اكتب: /mode open أو /mode approval")
            return ab.send_message(m.chat.id, admin_set_mode(parts[1]))
        if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
            return ab.send_message(m.chat.id, f"اكتب: /{cmd} ID")
        status = "banned" if cmd == "ban" else "allowed"
        ab.send_message(m.chat.id, admin_apply(int(parts[1]), status))

    @ab.callback_query_handler(func=lambda c: c.data.startswith("adm:"))
    def a_cb(call):
        if not is_admin(call.from_user.id):
            return ab.answer_callback_query(call.id, "⛔ للإدارة بس.", show_alert=True)
        _, act, uid = call.data.split(":")
        uid = int(uid)
        status = "banned" if act == "ban" else "allowed"
        admin_apply(uid, status)
        ab.answer_callback_query(call.id, STATUS_AR[status])
        try:
            ab.edit_message_text(user_card("👤 <b>بيانات المستخدم</b>", uid), call.message.chat.id,
                                 call.message.message_id, reply_markup=admin_kb(uid))
        except Exception:
            pass

    threading.Thread(target=lambda: ab.infinity_polling(skip_pending=True), daemon=True).start()
    print("🛠 بوت الإدارة اشتغل.")


# ============================================================
# المرحلة 1: البداية والتسجيل
# ============================================================
WELCOME = (
    "🎖 <b>هلا بيك يا مقاتل بغرفة العمليات المركزية...</b>\n\n"
    "هسه انت واگف بگلب مركز القيادة والسيطرة، هنا تنحسم المعارك بالدگة والانضباط وسرعة "
    "القرار. هنا مو مكان للتردد ولا للتهور؛ كل نقطة تطلع على الرادار ممكن تشيل مصير بلد "
    "كامل، وكل زر تضغطه يترك أثر بميزان الحرب.\n\n"
    "گبل ما نوكلك المهام، لازم نسجّل هويتك العسكرية بسجلات القيادة.\n\n"
    "🗂 <i>ملاحظة: اسمك ومعرّفك بتيليجرام ينحفظون بسجلات الإدارة.</i>\n\n"
    "📝 <b>[اكتب اسمك هنا]</b>"
)


@bot.message_handler(commands=["start"])
def cmd_start(m):
    if not gate_msg(m):
        return
    uid = m.from_user.id
    with ulock(uid):
        sessions.pop(uid, None)
    awaiting_name.add(uid)
    bot.send_message(m.chat.id, WELCOME)


@bot.message_handler(commands=["profile"])
def cmd_profile(m):
    if not gate_msg(m):
        return
    p = players.get(m.from_user.id)
    if not p:
        bot.send_message(m.chat.id, "ابعث /start أول شي حتى تسجّل هويتك.")
        return
    bot.send_message(m.chat.id, profile_text(p), reply_markup=mk([HOME_ROW]))


@bot.message_handler(func=lambda m: m.from_user.id in awaiting_name, content_types=["text"])
def got_name(m):
    if not gate_msg(m):
        return
    uid = m.from_user.id
    name = m.text.strip()
    if len(name) < 2 or len(name) > 30 or name.startswith("/"):
        bot.send_message(m.chat.id, "⚠️ الاسم مو صالح. اكتب اسمك من جديد (من 2 لحد 30 حرف).")
        return
    awaiting_name.discard(uid)
    if uid in players:
        players[uid]["name"] = name
    else:
        players[uid] = new_profile(name)
    save_data()
    if str(uid) in access["users"]:
        access["users"][str(uid)]["game_name"] = name
        save_access()
        admin_notify("📝 <b>سجّل اسمه العسكري باللعبة</b>", uid)
    bot.send_message(m.chat.id, home_text(players[uid]), reply_markup=home_kb())


def home_text(p):
    return (
        "🦅 <b>مشروع الصقر — Project Falcon</b>\n\n"
        f"🎖 <b>هلا بيك، {html.escape(p['name'])}.</b>\n\n"
        f"انكتب اسمك بسجلات القيادة برتبة <b>{rank_name(p)}</b>. هسه صرت عنصر فعّال بمنظومة "
        "الدفاع والهجوم الوطنية، ولازم تلتزم بقواعد الاشتباك وبروتوكولات التحقق گبل أي إطلاق. "
        "ولا تنسى: الغلط بهالغرفة ما ينسكت عليه، ولجنة التحقيق ما ترحم المتسرّعين، والنصر "
        "يصنعه المنضبطين.\n\n"
        "اختار وجهتك من القائمة، وخلّي شعارك: <i>تأكّد أول، وبعدين احسم.</i>"
    )


def home_kb():
    return mk([[("🎛 إدارة ظهور الأصناف", "cls:menu")],
               [("📋 السجل العسكري", "profile")]])


def classes_kb():
    return mk([
        [("📡 صنف الرادار والدفاع الجوي", "cls:radar")],
        [("✈️ صنف المقاتلات الحربية (الطيارين)", "cls:pilot")],
        [("⚓ صنف البحرية", "cls:navy")],
        HOME_ROW,
    ])


# ============================================================
# محرك التتبع اللحظي (Live Tracking Engine)
# ============================================================
KM_PER_DEG = 111.0


def km_to_latlon(lat0, lon0, x, y):
    return (lat0 + y / KM_PER_DEG,
            lon0 + x / (KM_PER_DEG * math.cos(math.radians(lat0))))


def compass(deg):
    names = ["شمال", "شمال شرق", "شرق", "جنوب شرق", "جنوب", "جنوب غرب", "غرب", "شمال غرب"]
    return names[int((deg % 360 + 22.5) // 45) % 8]


def smooth(u):
    return u * u * (3 - 2 * u)


def target_xy(trk, tau):
    """موقع الهدف بالكيلومتر (المنصّة عند 0,0) بعد tau ثانية — الهدف يگرب للقاعدة."""
    return trk["tx"] + trk["vx"] * tau, trk["ty"] + trk["vy"] * tau


def new_trk(chat_id, mid, tname, talt, tdist):
    brg = random.uniform(0, 360)
    tx = tdist * math.sin(math.radians(brg))
    ty = tdist * math.cos(math.radians(brg))
    d = max(1.0, math.hypot(tx, ty))
    speed = random.uniform(0.15, 0.45)
    return {
        "chat": chat_id, "mid": mid, "active": True, "closing": False, "hit": False,
        "tau": 0, "tname": tname, "talt": talt,
        "tx": tx, "ty": ty, "vx": -tx / d * speed, "vy": -ty / d * speed,
        "lat0": random.uniform(32.0, 34.5), "lon0": random.uniform(43.0, 46.0),
        "missiles": [], "notes": [],
    }


def trk_note(trk, text):
    trk["notes"].append(text)
    trk["notes"] = trk["notes"][-3:]


def add_missile(trk, spec):
    """يضيف صاروخ جديد (موجة) للتتبع. spec: label, pk, mach, own_alt."""
    tau0 = trk["tau"]
    x0, y0 = target_xy(trk, tau0)
    d = max(2.0, math.hypot(x0, y0))
    total = spec.get("total") or int(min(14, max(6, round(d / 22))))
    ix, iy = target_xy(trk, tau0 + total)
    hit = random.random() < spec["pk"]
    if hit:
        mx = my = 0.0
    else:
        ang, mag = random.uniform(0, 2 * math.pi), random.uniform(0.2, 0.9)
        mx, my = mag * math.cos(ang), mag * math.sin(ang)
    alt0, alt1 = spec["own_alt"], trk["talt"]
    trk["missiles"].append({
        "n": len(trk["missiles"]) + 1, "label": spec["label"], "mach": spec["mach"],
        "hit": hit, "tau0": tau0, "total": total, "t": 0, "state": "fly",
        "ax": ix + mx, "ay": iy + my, "alt0": alt0, "alt1": alt1,
        "arc": spec["arc"] if "arc" in spec else min(25.0, max(1.5, 0.25 * max(alt0, alt1))),
        "icon": spec.get("icon", "🚀"), "noun": spec.get("noun", "الصاروخ"),
    })


def missile_pos(trk, m):
    """يرجّع (x, y, alt, dist_to_target, heading) للصاروخ بلحظته الحالية."""
    u = m["t"] / m["total"]
    mx, my = m["ax"] * u, m["ay"] * u
    alt = m["alt0"] + (m["alt1"] - m["alt0"]) * smooth(u) + m["arc"] * math.sin(math.pi * u)
    txx, tyy = target_xy(trk, m["tau0"] + m["t"])
    d2 = math.hypot(txx - mx, tyy - my)
    d3 = math.hypot(d2, alt - m["alt1"])
    heading = math.degrees(math.atan2(txx - mx, tyy - my)) % 360
    if m["state"] == "hit":
        d3 = 0.0
    return mx, my, alt, d3, heading


def trk_step(trk):
    trk["tau"] += 1
    for m in trk["missiles"]:
        if m["state"] == "fly":
            m["t"] += 1
            if m["t"] >= m["total"]:
                m["t"] = m["total"]
                m["state"] = "hit" if m["hit"] else "miss"
    if not trk["hit"] and any(m["state"] == "hit" for m in trk["missiles"]):
        trk["hit"] = True
        for m in trk["missiles"]:
            if m["state"] == "fly":
                m["state"] = "wasted"
    return trk["hit"] or all(m["state"] != "fly" for m in trk["missiles"])


def trk_text(trk, done):
    cx, cy = target_xy(trk, trk["tau"])
    lines = [
        f"🛰 <b>تتبع الإطلاق لحظة بلحظة</b>   ⏱ الثانية {trk['tau']}",
        f"🎯 الهدف: <b>{trk['tname']}</b> | ارتفاعه {trk['talt']:.1f} كم | يبعد {math.hypot(cx, cy):.1f} كم",
        "⏳ <i>الوقت مضغوط بالمحاكاة</i>",
        "━━━━━━━━━━━━",
    ]
    status = {
        "fly": "🔥 بالجو وگاعد يگرب من الهدف",
        "hit": "💥 إصابة مباشرة",
        "miss": "❌ فات الهدف (الهدف تفادى)",
        "wasted": "⚠️ الهدف انضرب — الإطلاق انلغى",
    }
    for m in trk["missiles"]:
        x, y, alt, d, hd = missile_pos(trk, m)
        lat, lon = km_to_latlon(trk["lat0"], trk["lon0"], x, y)
        mach = m["mach"] * random.uniform(0.97, 1.03) if m["state"] == "fly" else 0.0
        pct = int(100 * m["t"] / m["total"])
        bar = "▓" * (pct // 10) + "░" * (10 - pct // 10)
        lines.append(
            f"{m['icon']} <b>{m['noun']} #{m['n']} — {m['label']}</b>\n"
            f"الحالة: {status[m['state']]}\n"
            f"📍 الإحداثيات: {lat:.4f}°N, {lon:.4f}°E\n"
            f"🧭 الاتجاه: {hd:03.0f}° ({compass(hd)})\n"
            f"📏 الارتفاع: {alt:.2f} كم | ⚡ ماخ {mach:.1f}\n"
            f"🎯 المسافة للهدف: {d:.1f} كم\n"
            f"{bar} {pct}%"
        )
    if trk["notes"]:
        lines.append("━━━━━━━━━━━━\n" + "\n".join(trk["notes"]))
    if done:
        lines.append("\n💥💥 <b>إصابة مباشرة!</b>" if trk["hit"]
                     else "\n❌ <b>الهدف تفادى كل الإطلاقات!</b>")
    else:
        lines.append("\n🔔 تگدر تطلق ذخيرة إضافية (موجة ثانية) بأي لحظة.")
    return "\n\n".join(lines)


def trk_kb(uid):
    s = sessions.get(uid)
    if not s:
        return None
    if s["mode"] == "radar":
        return mk([[("🚀 إطلاق صاروخ إضافي (موجة ثانية)", "trk:l")]])
    if s["mode"] == "cas":
        rows = cas_weapon_rows(s, "trk:c")
    else:
        rows = pilot_weapon_rows(s, "trk:w")
    return mk(rows) if rows else None


def trk_begin(call, uid, spec, tname, talt, tdist):
    """يبدي التتبع: ينشئ الجلسة ويشغّل خيط (Thread) مستقل لحلقة التحديث."""
    s = sessions[uid]
    with ulock(uid):
        trk = new_trk(call.message.chat.id, call.message.message_id, tname, talt, tdist)
        s["trk"] = trk
        s["label"] = spec["label"]
        add_missile(trk, spec)
        trk_note(trk, random.choice({"radar": RADAR_LAUNCH, "cas": CAS_FIRE}.get(s["mode"], PILOT_FIRE)))
    threading.Thread(target=trk_loop, args=(uid,), daemon=True).start()


def trk_loop(uid):
    """حلقة التتبع: تعدّل الرسالة كل ثانية بـ time.sleep + edit_message_text."""
    first = True
    try:
        while True:
            with ulock(uid):
                s = sessions.get(uid)
                trk = s.get("trk") if s else None
                if not trk:
                    return
                done = False if first else trk_step(trk)
                if done:
                    trk["closing"] = True
                text = trk_text(trk, done)
                kb = None if done else trk_kb(uid)
                chat_id, mid = trk["chat"], trk["mid"]
            safe_edit(chat_id, mid, text, kb)
            first = False
            if done:
                break
            time.sleep(1)
        time.sleep(1.5)
        with ulock(uid):
            s = sessions.get(uid)
            trk = s.get("trk") if s else None
            if not trk:
                return
            hit, shots = trk["hit"], len(trk["missiles"])
            chat_id, mid = trk["chat"], trk["mid"]
            s["trk"] = None
        call = fake_call(chat_id, mid)
        if s["mode"] == "radar":
            radar_finish(call, uid, hit, shots)
        elif s["mode"] == "cas":
            cas_finish(call, uid, hit, shots)
        else:
            pilot_finish(call, uid, hit, shots)
    except Exception:
        traceback.print_exc()
        try:
            with ulock(uid):
                if uid in sessions:
                    sessions[uid]["trk"] = None
            bot.send_message(trk["chat"], "⚠️ صار خلل بسيط بالتتبع. ارجع لغرفة العمليات.",
                             reply_markup=mk([HOME_ROW]))
        except Exception:
            pass


def on_trk(call, uid, parts):
    """زر الإطلاق أثناء التتبع = موجة ثانية بدون تعطيل الحلقة."""
    s = sessions.get(uid)
    if not s:
        return
    with ulock(uid):
        trk = s.get("trk")
        if not trk or not trk["active"] or trk["closing"]:
            return
        if len(trk["missiles"]) >= MAX_MISSILES:
            trk_note(trk, f"⚠️ وصلت للحد الأعلى ({MAX_MISSILES} صواريخ) بهالاشتباك.")
            return
        cx, cy = target_xy(trk, trk["tau"])
        cur = math.hypot(cx, cy)
        if parts[1] == "l":
            spec = radar_launch_spec(s, cur)
        elif parts[1] == "c":
            spec = cas_launch_spec(s, int(parts[2]), cur)
        else:
            spec = pilot_launch_spec(s, parts[2], int(parts[3]), cur)
        if isinstance(spec, str):
            trk_note(trk, spec)
            return
        add_missile(trk, spec)
        trk_note(trk, f"🚀 انطلگت موجة ثانية: {spec['label']}")


# ============================================================
# المرحلة 2: الرادار والدفاع الجوي
# ============================================================
BATTERIES = [
    {"name": "S-400 Triumf", "ballistic": True, "alt": 30,
     "rng": {"s": 40, "m": 120, "l": 400},
     "mis": {"s": "9M96E1", "m": "9M96E2", "l": "48N6E3"}},
    {"name": "Pantsir-S1", "ballistic": False, "alt": 15,
     "rng": {"s": 20}, "mis": {"s": "57E6"}},
    {"name": "Patriot PAC-3", "ballistic": True, "alt": 24,
     "rng": {"s": 35, "m": 80, "l": 160},
     "mis": {"s": "PAC-3 MSE", "m": "GEM-T", "l": "PAC-2 GEM/C"}},
    {"name": "THAAD", "ballistic": True, "alt": 150,
     "rng": {"l": 200}, "mis": {"l": "THAAD Interceptor"}},
    {"name": "NASAMS", "ballistic": False, "alt": 16,
     "rng": {"s": 25, "m": 40}, "mis": {"s": "AIM-9X", "m": "AIM-120 AMRAAM-ER"}},
    {"name": "SAMP/T Aster 30", "ballistic": True, "alt": 25,
     "rng": {"s": 30, "m": 70, "l": 120},
     "mis": {"s": "Aster 15", "m": "Aster 30", "l": "Aster 30 B1NT"}},
    {"name": "Tor-M2", "ballistic": False, "alt": 10,
     "rng": {"s": 12}, "mis": {"s": "9M338K"}},
    {"name": "IRIS-T SLM", "ballistic": False, "alt": 20,
     "rng": {"s": 20, "m": 40}, "mis": {"s": "IRIS-T SLS", "m": "IRIS-T SLM"}},
]
CLASS_AR = {"s": "گصير المدى", "m": "متوسط المدى", "l": "بعيد المدى"}
BASE_PK = {"s": 0.82, "m": 0.85, "l": 0.78}
CLASS_MACH = {"s": 3.5, "m": 4.5, "l": 6.0}
KIND_MOD = {"fighter": 0.9, "cruise": 0.9, "ballistic": 0.65, "airliner": 1.0, "friendly": 0.9}


def gen_radar_target():
    truth = random.choices(["hostile", "civil", "friendly"], [0.55, 0.30, 0.15])[0]
    t = {"truth": truth, "analyzed": False, "attempts": 0}
    if truth == "hostile":
        kind = random.choices(["fighter", "cruise", "ballistic"], [0.4, 0.35, 0.25])[0]
        t["kind"] = kind
        t["name"] = {"fighter": "مقاتلة معادية", "cruise": "صاروخ كروز",
                     "ballistic": "صاروخ باليستي"}[kind]
        t["label"] = random.choices(["معادي مؤكد", "مجهول"], [0.55, 0.45])[0]
    elif truth == "civil":
        t["kind"] = "airliner"
        t["name"] = "طيارة ركاب مدنية"
        t["pax"] = random.randint(90, 380)
        t["label"] = random.choices(["مجهول", "مدني (Squawk مدني)"], [0.55, 0.45])[0]
    else:
        t["kind"] = "friendly"
        t["name"] = "مقاتلة صديقة"
        t["label"] = random.choices(["صديق (IFF)", "مجهول"], [0.5, 0.5])[0]
    k = t["kind"]
    if k == "fighter":
        t["dist"], t["alt"], t["spd"] = random.randint(30, 300), round(random.uniform(0.3, 18), 1), random.randint(850, 2200)
    elif k == "cruise":
        t["dist"], t["alt"], t["spd"] = random.randint(20, 250), round(random.uniform(0.1, 0.3), 2), random.randint(700, 1000)
    elif k == "ballistic":
        t["dist"], t["alt"], t["spd"] = random.randint(100, 500), float(random.randint(40, 140)), random.randint(6000, 12000)
    elif k == "airliner":
        t["dist"], t["alt"], t["spd"] = random.randint(30, 300), round(random.uniform(9, 12), 1), random.randint(750, 920)
    else:
        t["dist"], t["alt"], t["spd"] = random.randint(30, 300), round(random.uniform(0.5, 15), 1), random.randint(700, 1800)
    return t


def radar_visible_type(t):
    if t["kind"] in ("cruise", "ballistic"):
        return t["name"]
    if t["analyzed"] or t["label"] != "مجهول":
        return t["name"]
    return "هدف جوي مأهول (البصمة مو واضحة)"


def radar_text(t, note=""):
    lines = [
        "📡 <b>إشارة رادارية جديدة</b>",
        f"• النوع: <b>{radar_visible_type(t)}</b>",
        f"• المسافة: {t['dist']} كم",
        f"• الارتفاع: {t['alt']} كم",
        f"• السرعة: {t['spd']} كم/س",
        f"• حالة الهدف: <b>{t['label']}</b>",
    ]
    if t["kind"] == "airliner" and (t["analyzed"] or "مدني" in t["label"]):
        lines.append(f"• الركاب المسجّلين على متنها: <b>{t['pax']}</b>")
    if t["attempts"]:
        lines.append(f"• محاولات اعتراض سابقة: {t['attempts']}")
    if note:
        lines.append("\n" + note)
    lines.append("\n⏱ القرار إلك يا ضابط.")
    return "\n".join(lines)


def radar_kb(t):
    rows = []
    if not t["analyzed"]:
        rows.append([("🔍 تحليل الهدف (IFF / ADS-B)", "r:an")])
    rows.append([("🎯 اشتباك", "r:eng"), ("👁 بس أراقب", "r:obs")])
    rows.append([("⬅️ رجوع", "cls:menu")])
    return mk(rows)


def radar_after_kb():
    return mk([[("📡 مسح راداري جديد", "r:scan")], HOME_ROW])


RADAR_INTRO = (
    "📡 <b>[صوت القائد]</b>\n\n"
    "«هلا بيك بمركز الرصد والدفاع الجوي يا ضابط. السما مفتوحة گدامك، وعيونك هي الرادار. "
    "راح تجيك إشارات هواية: مقاتلات، صواريخ كروز، صواريخ باليستية، وطيارات ركاب مدنية "
    "تعبر مجالنا وياها مئات الأرواح.\n\n"
    "عندك منظومات اعتراض متنوعة، ولكل منظومة مدى وسگف وگدرات مختلفة. حلّل گبل ما تطلق، "
    "واختار السلاح المناسب للهدف. اللي يسگّط معادي ينكافأ، واللي يسگّط مدني يتحاسب وما "
    "ينرحم.\n\n"
    "خلّي گرارك محسوب. الله يوفگك.»"
)


def radar_scan(call, uid):
    t = gen_radar_target()
    sessions[uid] = {"mode": "radar", "t": t, "trk": None}
    p = players[uid]
    edit(call, random.choice(RADAR_PRE).format(name=html.escape(p["name"]), rank=rank_name(p)))
    send_new(call, radar_text(t), radar_kb(t))


def radar_session(call, uid):
    s = sessions.get(uid)
    if not s or s["mode"] != "radar":
        edit(call, "⚠️ الجلسة خلصت. ابدي مسح جديد.", radar_after_kb())
        return None
    return s


def radar_analyze(call, uid):
    s = radar_session(call, uid)
    if not s:
        return
    t = s["t"]
    t["analyzed"] = True
    t["dist"] = max(8, int(t["dist"] * random.uniform(0.85, 0.92)))
    if random.random() < 0.85:
        verdict = {"hostile": "🔴 تأكد إن الهدف <b>معادي</b>.",
                   "civil": "🟡 تأكد إن الهدف <b>مدني</b>.",
                   "friendly": "🟢 تأكد إن الهدف <b>صديق</b>."}[t["truth"]]
        t["label"] = {"hostile": "معادي مؤكد", "civil": "مدني مؤكد", "friendly": "صديق مؤكد"}[t["truth"]]
        note = f"🔍 <b>نتيجة التحليل:</b> {verdict} (الهدف گرب وانت تحلل)"
    else:
        note = "🔍 <b>نتيجة التحليل:</b> مو حاسمة. تشويش بالإشارة والهدف گاعد يگرب."
    flavor = random.choice(RADAR_DURING).format(rank=rank_name(players[uid]))
    edit(call, radar_text(t, flavor + "\n\n" + note), radar_kb(t))


def radar_choose_battery(call, uid):
    s = radar_session(call, uid)
    if not s:
        return
    t = s["t"]
    lines = [f"🛡 <b>اختار منظومة الدفاع الجوي</b>\nالهدف: {radar_visible_type(t)} | "
             f"{t['dist']} كم | {t['alt']} كم ارتفاع\n"]
    rows, row = [], []
    for i, b in enumerate(BATTERIES):
        rngs = " / ".join(f"{CLASS_AR[k][:3]}.{v}" for k, v in b["rng"].items())
        bal = " | باليستي ✔" if b["ballistic"] else ""
        lines.append(f"• <b>{b['name']}</b> — {rngs} كم — سگف {b['alt']} كم{bal}")
        row.append((b["name"], f"r:bat:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([("⬅️ رجوع للهدف", "r:back")])
    edit(call, "\n".join(lines), mk(rows))


def radar_choose_missile(call, uid, bi):
    s = radar_session(call, uid)
    if not s:
        return
    b = BATTERIES[bi]
    s["bi"], s["cls"] = bi, None
    lines = [f"🚀 <b>{b['name']}</b>\nاختار صاروخ الاعتراض المناسب:\n"]
    rows = []
    for k in ("s", "m", "l"):
        if k in b["rng"]:
            lines.append(f"• {CLASS_AR[k]}: {b['mis'][k]} (مدى {b['rng'][k]} كم)")
            rows.append([(f"{CLASS_AR[k]} — {b['mis'][k]}", f"r:mis:{k}")])
    rows.append([("⬅️ غيّر المنظومة", "r:eng")])
    edit(call, "\n".join(lines), mk(rows))


def radar_problem(t, b, cls, dist):
    if t["kind"] == "ballistic" and not b["ballistic"]:
        return "المنظومة مو مخصصة لاعتراض الصواريخ الباليستية."
    if t["kind"] != "ballistic" and t["alt"] > b["alt"]:
        return "الهدف أعلى من سگف الاعتراض."
    if b["rng"][cls] < dist:
        return f"الهدف برّه مدى الصاروخ ({b['rng'][cls]} كم < {int(dist)} كم)."
    return None


def radar_launch_spec(s, cur_dist):
    t, b, cls = s["t"], BATTERIES[s["bi"]], s["cls"]
    problem = radar_problem(t, b, cls, cur_dist)
    if problem:
        return "⚠️ ما كدرنا نطلق: " + problem
    return {"label": f"{b['mis'][cls]} ({b['name']})",
            "pk": BASE_PK[cls] * KIND_MOD[t["kind"]],
            "mach": CLASS_MACH[cls], "own_alt": 0.0}


def radar_ready(call, uid, cls):
    """شاشة الجاهزية: فحص الجدوى ثم زر (إطلاق الصاروخ)."""
    s = radar_session(call, uid)
    if not s or s.get("bi") is None:
        return
    t, b = s["t"], BATTERIES[s["bi"]]
    problem = radar_problem(t, b, cls, t["dist"])
    if problem:
        t["attempts"] += 1
        if t["truth"] == "hostile" and t["attempts"] >= 3:
            return radar_strike(call, uid, "المحاولات خلصت بدون اعتراض فعّال.")
        return edit(call, radar_text(t, f"⚠️ <b>ما كدرنا نطلق:</b> {problem}\nاختار منظومة ثانية."),
                    radar_kb(t))
    s["cls"] = cls
    edit(call,
         "🔒 <b>المنظومة جاهزة للإطلاق</b>\n"
         f"• المنظومة: {b['name']}\n• الصاروخ: {b['mis'][cls]} ({CLASS_AR[cls]})\n"
         f"• المدى: {b['rng'][cls]} كم\n"
         f"• الهدف: {radar_visible_type(t)} على بعد {t['dist']} كم\n\n"
         "اضغط الزر وراح تتابع الصاروخ لحظة بلحظة، وتگدر تطلق موجة ثانية وهو طاير.",
         mk([[("🚀 إطلاق الصاروخ", "r:go")], [("⬅️ غيّر الصاروخ", f"r:bat:{s['bi']}")]]))


def radar_go(call, uid):
    s = radar_session(call, uid)
    if not s or not s.get("cls"):
        return
    t = s["t"]
    if s.get("trk"):
        return
    spec = radar_launch_spec(s, t["dist"])
    if isinstance(spec, str):
        return edit(call, radar_text(t, spec), radar_kb(t))
    trk_begin(call, uid, spec, radar_visible_type(t), float(t["alt"]), float(t["dist"]))


def radar_finish(call, uid, hit, shots):
    """نتيجة الاعتراض بعد انتهاء التتبع."""
    s = sessions.get(uid)
    if not s or s["mode"] != "radar":
        return
    p, t, b = players[uid], s["t"], BATTERIES[s["bi"]]
    head = f"🚀 عدد الصواريخ المطلوقة: <b>{shots}</b> — {s['label']}\n\n"

    if t["truth"] == "hostile":
        if hit:
            pts = {"fighter": 120, "cruise": 150, "ballistic": 250}[t["kind"]]
            p["kills"] += 1
            promo = add_points(p, pts)
            sessions.pop(uid, None)
            set_ctx(uid, branch="radar", result="win", target=t["name"], truth_ar="معادي",
                    tool=s["label"], pts=pts, analyzed=t["analyzed"])
            return edit(call,
                head + f"💥 <b>إصابة مؤكدة!</b> انضرب ({t['name']}) گبل ما يوصل أهدافه.\n\n"
                "✅ الإيجابيات: انحمت المنشآت الحيوية وسلمت الأرواح.\n"
                f"🏅 المكافأة: +{pts} نقطة أداء.{promo}", post_kb())
        t["attempts"] += 1
        if t["attempts"] >= 3:
            return radar_strike(call, uid, "فشلت الاعتراضات المتكررة.")
        return edit(call, radar_text(
            t, head + "❌ <b>الصاروخ فات الهدف!</b> الهدف نفّذ مناورة تفادي وبعده يگرب، اشتبك ثانية فوراً."),
            radar_kb(t))

    victim = "طيارة ركاب مدنية" if t["truth"] == "civil" else "مقاتلة صديقة"
    truth_ar = "مدني" if t["truth"] == "civil" else "صديق"
    sessions.pop(uid, None)
    if not hit:
        p["points"] = max(0, p["points"] - 120)
        save_data()
        set_ctx(uid, branch="radar", result="violation", target=victim, truth_ar=truth_ar,
                tool=s["label"], pts=-120, analyzed=t["analyzed"])
        return edit(call,
            head + f"😮‍💨 الصاروخ فات، و({victim}) نجت بالصدفة.\n\n"
            "⚠️ بس الإطلاق على هدف مو معادي مخالفة جسيمة لقواعد الاشتباك.\n"
            "📉 انخصمت 120 نقطة ووصلك إنذار رسمي.", post_kb())
    if t["truth"] == "civil":
        cas = t["pax"] + random.randint(6, 14)
        money = random.randint(90, 260)
        p["civil_inc"] += 1
    else:
        cas = random.randint(1, 3)
        money = random.randint(60, 150)
        p["friendly_inc"] += 1
    report = inquiry_report(t["analyzed"], t["label"], victim, cas, money)
    punish = apply_punishment(p, cas, "استهداف " + victim)
    set_ctx(uid, branch="radar", result=t["truth"], target=victim, truth_ar=truth_ar,
            tool=s["label"], cas=cas, money=money,
            pts=-150 if cas < 30 else (-400 if cas < 150 else -1000), analyzed=t["analyzed"])
    edit(call, head + "💥 <b>إصابة... بس على هدف مو معادي!</b> 🚨\n\n" + report + "\n" + punish,
         post_kb())


def radar_strike(call, uid, reason):
    p = players[uid]
    t = sessions.get(uid, {}).get("t", {})
    casualties = random.randint(8, 45)
    money = random.randint(40, 220)
    sessions.pop(uid, None)
    add_points(p, -100)
    set_ctx(uid, branch="radar", result="fail", target=t.get("name", "الهدف المعادي"),
            truth_ar="معادي", tool="منظومات الدفاع الجوي المتاحة", cas=casualties,
            money=money, pts=-100, analyzed=t.get("analyzed", False))
    edit(call,
         f"💣 <b>فشل الدفاع!</b> {reason}\n"
         f"ضرب ({t.get('name', 'الهدف المعادي')}) منشأة عسكرية.\n"
         f"• خسائر بشرية: {casualties}\n• خسائر مادية: {money} مليون دولار\n"
         "📉 انخصمت 100 نقطة بسبب التقصير بالدفاع.", post_kb())


def radar_observe(call, uid):
    s = radar_session(call, uid)
    if not s:
        return
    t, p = s["t"], players[uid]
    if t["truth"] == "hostile":
        return radar_strike(call, uid, "اكتفيت بالمراقبة قدام هدف معادي.")
    sessions.pop(uid, None)
    if t["truth"] == "civil":
        pts = 40 if t["analyzed"] else 15
        promo = add_points(p, pts)
        txt = ("🕊 <b>تقدير ممتاز!</b> الطيارة المدنية عبرت بسلام وانحفظت مئات الأرواح.\n"
               f"🏅 +{pts} نقطة.{promo}")
    else:
        pts = 15
        promo = add_points(p, pts)
        txt = f"✅ تأمّن عبور المقاتلة الصديقة.\n🏅 +15 نقطة.{promo}"
    set_ctx(uid, branch="radar", result="safe", target=t["name"],
            truth_ar="مدني" if t["truth"] == "civil" else "صديق",
            tool="ولا شي (ما انستعمل سلاح)", pts=pts, analyzed=t["analyzed"])
    edit(call, txt, post_kb())


# ============================================================
# المرحلة 3: المقاتلات الحربية
# ============================================================
# (الاسم، خانات التسليح، حدّ الجي، السگف كم، السرعة ماخ، ملاحظة)
AIRCRAFT = [
    ("F-22 Raptor", 4, 9.0, 19.8, 2.25, "تفوق جوي شبحي"),
    ("F-35A Lightning II", 4, 9.0, 15.2, 1.6, "شبحية ودمج حساسات"),
    ("F-15EX Eagle II", 6, 9.0, 18.3, 2.5, "حمولة ضخمة وگدرة اعتراض"),
    ("F-16V Viper", 5, 9.0, 15.2, 2.0, "رشيقة ومتعددة المهام"),
    ("Su-57 Felon", 5, 9.0, 20.0, 2.0, "شبحية ومناورة بدفع موجّه"),
    ("Su-35S Flanker-E", 6, 9.0, 18.0, 2.25, "مناورة خرافية"),
    ("J-20 Mighty Dragon", 4, 9.0, 20.0, 2.0, "اعتراض بعيد المدى"),
    ("Rafale F4", 6, 9.0, 15.2, 1.8, "متعددة الأدوار بحرية/جوية"),
    ("Eurofighter Typhoon", 6, 9.0, 19.8, 2.0, "تسارع وتفوق جوي"),
    ("JAS-39 Gripen E", 5, 9.0, 15.2, 2.0, "خفيفة واقتصادية"),
]
# (الاسم، المدى كم، احتمال الإصابة، وصف)
A2A = [
    ("AIM-120D AMRAAM", 160, 0.85, "نشط راداري"),
    ("AIM-9X Sidewinder", 35, 0.88, "حراري عالي المناورة"),
    ("AIM-7M Sparrow", 70, 0.70, "راداري شبه نشط"),
    ("MBDA Meteor", 200, 0.90, "محرك رامجت ومنطقة گتل واسعة"),
    ("MICA EM", 80, 0.80, "رادار نشط فرنسي"),
    ("R-77-1", 110, 0.78, "رادار نشط روسي"),
    ("R-73", 30, 0.85, "حراري گصير المدى"),
    ("R-37M", 300, 0.70, "اعتراض فائق المدى"),
    ("PL-15", 200, 0.82, "رادار نشط بعيد المدى"),
    ("IRIS-T", 25, 0.90, "حراري متطور"),
]
# (الاسم، المدى كم، الاحتمال، الأهداف المفضّلة، وصف)
A2G = [
    ("AGM-65 Maverick", 25, 0.85, {"armor", "convoy"}, "ضد المدرعات"),
    ("AGM-114 Hellfire", 11, 0.88, {"armor", "convoy"}, "دگيگ گصير المدى"),
    ("AGM-88 HARM", 150, 0.85, {"sam"}, "گمع الدفاعات الجوية"),
    ("AGM-158 JASSM", 370, 0.90, {"bunker", "sam"}, "جوّال شبحي بعيد"),
    ("Storm Shadow", 250, 0.90, {"bunker"}, "اختراق المنشآت المحصّنة"),
    ("AASM Hammer", 70, 0.90, {"bunker", "armor", "sam"}, "ذخيرة موجّهة معيارية"),
    ("GBU-39 SDB", 110, 0.88, {"armor", "sam", "bunker"}, "گنبلة صغيرة القطر"),
    ("Brimstone", 20, 0.90, {"armor"}, "مضاد دروع برادار ملّي"),
    ("Kh-38", 40, 0.82, {"armor", "bunker"}, "متعدد الباحثات"),
    ("Spice-2000", 100, 0.90, {"bunker", "sam"}, "موجّهة بمطابقة المشهد"),
]
ROUNDS_PER_SLOT = 2
ENCOUNTERS = 6
PILOT_INTRO = (
    "✈️ <b>هلا بيك يا طيار، {name}.</b>\n\n"
    "قمرة القيادة هي مسرحك، والسما ميدانك. راح تگود طيارة مقاتلة متطورة، تجهّزها بالذخيرة "
    "المناسبة، وبعدين تنطلق بمهمة حيّة بيها أهداف جوية وأرضية، بعضها معادي وبعضها صديق أو "
    "مدني. الطيار اللي يضغط الزناد گبل ما يتأكد ما ينسامح.\n\n"
    "• الطيار المنضبط ينكافأ وينرقّى.\n"
    "• الطيار المتهور يمثل گدام لجنة تحقيق.\n\n"
    "اضغط «إظهار طيارتك» حتى نبدي التجهيز."
)


def ac_list_kb():
    rows = [[(a[0], f"p:ac:{i}")] for i, a in enumerate(AIRCRAFT)]
    rows.append([("🎲 اختيار عشوائي", "p:acr")])
    rows.append([("⬅️ رجوع", "cls:menu")])
    return mk(rows)


def ac_card(i):
    a = AIRCRAFT[i]
    return (f"✈️ <b>{a[0]}</b>\n• خانات التسليح: {a[1]} (كل خانة = {ROUNDS_PER_SLOT} صاروخ)\n"
            f"• حدّ گوى الجي: {a[2]}G\n• السگف: {a[3]} كم\n• السرعة: ماخ {a[4]}\n• الدور: {a[5]}")


def arsenal_screen(call, uid, mode):
    s = sessions[uid]
    cap = AIRCRAFT[s["ac"]][1]
    used = len(s["a2a"]) + len(s["a2g"])
    rows, lines = [], []
    if mode == "a":
        lines.append("🔫 <b>الترسانة — صواريخ جو-جو</b>")
        for i, w in enumerate(A2A):
            mark = "☑" if i in s["a2a"] else "☐"
            lines.append(f"{mark} <b>{w[0]}</b> — {w[1]} كم — احتمال {int(w[2] * 100)}% — {w[3]}")
            rows.append([(f"{mark} {w[0]}", f"p:t:a:{i}")])
        nav = [("➡️ جو-أرض", "p:gl")]
    else:
        lines.append("💣 <b>الترسانة — صواريخ جو-أرض</b>")
        for i, w in enumerate(A2G):
            mark = "☑" if i in s["a2g"] else "☐"
            lines.append(f"{mark} <b>{w[0]}</b> — {w[1]} كم — احتمال {int(w[2] * 100)}% — {w[4]}")
            rows.append([(f"{mark} {w[0]}", f"p:t:g:{i}")])
        nav = [("⬅️ جو-جو", "p:al")]
    lines.append(f"\nالخانات المستخدمة: <b>{used}/{cap}</b>")
    rows.append(nav + [("✅ تثبيت الترسانة", "p:lock")])
    edit(call, "\n".join(lines), mk(rows))


def gen_contact():
    domain = random.choice(["air", "ground"])
    truth = random.choices(["hostile", "friendly", "civil"], [0.55, 0.2, 0.25])[0]
    c = {"domain": domain, "truth": truth, "pat": 0, "identified": False, "casualties": 0, "pax": 0}
    sector = random.choice(["ألفا", "برافو", "تشارلي", "دلتا", "إيكو", "فوكستروت"])
    c["loc"] = f"القطاع {sector} ({random.randint(10, 59)}°N, {random.randint(20, 59)}°E)"
    if domain == "air":
        c["dist"] = random.randint(15, 140)
        if truth == "hostile":
            c["cls"] = random.choice(["fighter", "bomber", "uav"])
            c["name"] = {"fighter": "مقاتلة معادية", "bomber": "گاذفة معادية",
                         "uav": "طيارة مسيّرة قتالية"}[c["cls"]]
            c["alt"] = random.randint(500, 14000)
            c["label"] = random.choices(["معادي مؤكد", "مجهول"], [0.55, 0.45])[0]
        elif truth == "friendly":
            c["cls"], c["name"] = "fighter", "مقاتلة صديقة"
            c["alt"] = random.randint(500, 14000)
            c["label"] = random.choices(["صديق (IFF)", "مجهول"], [0.5, 0.5])[0]
            c["casualties"] = random.randint(1, 2)
        else:
            c["cls"], c["name"] = "airliner", "طيارة ركاب مدنية"
            c["alt"] = random.randint(9000, 12000)
            c["pax"] = random.randint(80, 350)
            c["casualties"] = c["pax"] + random.randint(6, 12)
            c["label"] = random.choices(["مجهول", "مدني مرجّح"], [0.6, 0.4])[0]
    else:
        c["dist"] = random.randint(5, 60)
        c["alt"] = random.randint(0, 50)
        if truth == "hostile":
            c["cls"] = random.choice(["sam", "armor", "bunker"])
            c["name"] = {"sam": "بطارية دفاع جوي معادية", "armor": "رتل مدرعات معادي",
                         "bunker": "مركز گيادة محصّن معادي"}[c["cls"]]
            c["label"] = random.choices(["معادي مؤكد", "مجهول"], [0.55, 0.45])[0]
        elif truth == "friendly":
            c["cls"], c["name"] = "convoy", "رتل إمداد صديق"
            c["label"] = random.choices(["صديق (IFF)", "مجهول"], [0.5, 0.5])[0]
            c["casualties"] = random.randint(8, 40)
        else:
            c["cls"], c["name"] = "convoy", "باصات نازحين مدنيين"
            c["label"] = random.choices(["مجهول", "مدني مرجّح"], [0.6, 0.4])[0]
            c["casualties"] = random.randint(20, 120)
    if c["label"] != "مجهول":
        c["identified"] = True
    return c


def contact_visible_name(c):
    if c["identified"] or c["label"] != "مجهول":
        return c["name"]
    return "هدف جوي مو محدد" if c["domain"] == "air" else "تشكيل أرضي مو واضح"


def contact_text(s, note=""):
    c = s["contact"]
    g = s.get("g", round(random.uniform(1.0, 2.0), 1))
    s["g"] = g
    dom = "جوي" if c["domain"] == "air" else "أرضي"
    lines = [
        f"🛩 <b>المهمة — الاشتباك {s['enc']}/{ENCOUNTERS}</b>",
        f"• سلامة الطيارة: {s['hp']}%",
        f"• ارتفاعك الحالي: {s['own_alt']} م",
        f"• گوى الجي: {g}G",
        f"• المكان: {c['loc']}",
        "",
        f"🎯 <b>هدف {dom}:</b> {contact_visible_name(c)}",
        f"• المسافة: {c['dist']} كم",
        f"• الحالة: <b>{c['label']}</b>",
    ]
    if c["domain"] == "air":
        lines.append(f"• ارتفاع الهدف: {c['alt']} م")
    if c["cls"] == "airliner" and c["identified"]:
        lines.append(f"• ركاب مسجّلين: {c['pax']}")
    if note:
        lines.append("\n" + note)
    return "\n".join(lines)


def contact_kb(c):
    rows = [[("🔥 إطلاق النار", "p:fire")]]
    if c["pat"] < 2:
        rows[0].append(("🧭 الصبر والمناورة", "p:man"))
    rows.append([("↪️ تعدّى الهدف", "p:skip")])
    return mk(rows)


def pilot_next(call, uid, note="", fresh=False):
    s = sessions[uid]
    if s["hp"] <= 0:
        return pilot_end(call, uid, crashed=True)
    if s["enc"] >= ENCOUNTERS:
        return pilot_end(call, uid)
    s["enc"] += 1
    s["contact"] = gen_contact()
    s["g"] = round(random.uniform(1.0, 2.0), 1)
    s["own_alt"] = random.randint(3000, 11000)
    text = contact_text(s, note + "\n" + random.choice(PILOT_DURING))
    (send_new if fresh else edit)(call, text, contact_kb(s["contact"]))


def pilot_damage(s, hit_chance, lo=15, hi=30):
    if random.random() < hit_chance:
        d = random.randint(lo, hi)
        s["hp"] = max(0, s["hp"] - d)
        return d
    return 0


def pilot_end(call, uid, crashed=False):
    s = sessions.pop(uid, None)
    p = players[uid]
    p["missions"] += 1
    score = s["score"] if s else 0
    if crashed:
        score -= 50
        txt = "🪂 <b>انسگطت طيارتك!</b> گفزت بالمظلة وانتشلوك.\n"
    else:
        txt = "🏁 <b>خلصت المهمة ورجعت للقاعدة سالم.</b>\n"
    promo = add_points(p, score)
    txt += (f"\n📊 <b>تقرير المهمة</b>\n• أهداف معادية دمّرتها: {s['kills'] if s else 0}\n"
            f"• صافي النقاط: {score:+d}\n• رتبتك الحالية: {rank_name(p)}{promo}")
    s = s or {}
    kills = s.get("kills", 0)
    if crashed:
        result = "crash"
    elif kills > 0 and score > 0:
        result = "win"
    elif score >= 0:
        result = "safe"
    else:
        result = "fail"
    bad = result in ("fail", "crash")
    set_ctx(uid, branch="pilot", result=result,
            target={"win": "الأهداف المعادية بمنطقة العمليات",
                    "safe": "التشكيلات اللي انرصدت بمنطقة العمليات"}.get(result, "الأهداف المكلَّف بيها"),
            truth_ar="متنوعة الهوية" if result == "safe" else "معادي",
            tool=AIRCRAFT[s["ac"]][0] if "ac" in s else "المقاتلة",
            cas=random.randint(3, 25) if bad else 0, money=random.randint(20, 180) if bad else 0,
            pts=score, analyzed=s.get("man", 0) > 0,
            loc=s.get("contact", {}).get("loc") or rand_loc(),
            extra=(f"\n🪂 انسگطت طيارة الطيار {html.escape(p['name'])} وانتشلوه بعد ما گفز بالمظلة."
                   if crashed else ""))
    edit(call, txt, post_kb())


def pilot_weapon_rows(s, prefix):
    c = s["contact"]
    rows = []
    if c["domain"] == "air":
        for i in sorted(s["a2a"]):
            if s["ammo_a"].get(i, 0) > 0:
                rows.append([(f"🚀 إطلاق {A2A[i][0]} ({s['ammo_a'][i]})", f"{prefix}:a:{i}")])
    else:
        for i in sorted(s["a2g"]):
            if s["ammo_g"].get(i, 0) > 0:
                rows.append([(f"🚀 إطلاق {A2G[i][0]} ({s['ammo_g'][i]})", f"{prefix}:g:{i}")])
    return rows


def pilot_fire_menu(call, uid):
    s = sessions[uid]
    c = s["contact"]
    lines = ["🔥 <b>اختار السلاح</b>\n"]
    if c["domain"] == "air":
        for i in sorted(s["a2a"]):
            w = A2A[i]
            ok = "✅" if w[1] >= c["dist"] else "❌ برّه المدى"
            lines.append(f"• {w[0]} — {w[1]} كم — ذخيرة {s['ammo_a'][i]} {ok}")
    else:
        for i in sorted(s["a2g"]):
            w = A2G[i]
            ok = "✅" if w[1] >= c["dist"] else "❌ برّه المدى"
            lines.append(f"• {w[0]} — {w[1]} كم — ذخيرة {s['ammo_g'][i]} {ok}")
    rows = pilot_weapon_rows(s, "p:w")
    if not rows:
        lines.append("\n⚠️ ماكو عندك ذخيرة مناسبة لهالنوع من الأهداف!")
    else:
        lines.append("\nبعد الإطلاق راح تتابع الصاروخ لحظة بلحظة وتگدر تطلق موجة ثانية.")
    rows.append([("⬅️ رجوع", "p:back")])
    edit(call, "\n".join(lines), mk(rows))


def pilot_launch_spec(s, dom, i, cur_dist):
    """يتحقق من السلاح والمدى، يخصم الذخيرة، ويرجّع مواصفات الصاروخ (أو نص خطأ)."""
    c = s["contact"]
    if dom == "a":
        name, rng, pk, tags = A2A[i][0], A2A[i][1], A2A[i][2], None
        ammo, mach = s["ammo_a"], random.uniform(3.5, 4.5)
    else:
        name, rng, pk, tags = A2G[i][0], A2G[i][1], A2G[i][2], A2G[i][3]
        ammo, mach = s["ammo_g"], random.uniform(0.9, 2.5)
    if (dom == "a") != (c["domain"] == "air") or ammo.get(i, 0) <= 0:
        return "⚠️ السلاح مو متوفر أو مو مناسب للهدف."
    if rng < cur_dist:
        return f"⚠️ {name} برّه المدى ({rng} كم). ما انطلگ شي."
    ammo[i] -= 1
    if tags is not None and c["cls"] not in tags and c["truth"] == "hostile":
        pk *= 0.65
    if c["cls"] == "fighter":
        pk *= 0.85
    if s["g"] > 6:
        pk -= 0.10
    return {"label": name, "pk": max(0.05, pk), "mach": mach, "own_alt": s["own_alt"] / 1000.0}


def pilot_fire_go(call, uid, dom, i):
    s = sessions.get(uid)
    if not s or s["mode"] != "pilot" or s.get("trk"):
        return
    c = s["contact"]
    spec = pilot_launch_spec(s, dom, i, c["dist"])
    if isinstance(spec, str):
        return edit(call, contact_text(s, spec), contact_kb(c))
    trk_begin(call, uid, spec, contact_visible_name(c), c["alt"] / 1000.0, float(c["dist"]))


def pilot_finish(call, uid, hit, shots):
    """نتيجة الاشتباك الجوي/الأرضي بعد انتهاء التتبع."""
    s, p = sessions.get(uid), players[uid]
    if not s or s["mode"] != "pilot":
        return
    c = s["contact"]
    head = f"🚀 عدد الصواريخ المطلوقة: <b>{shots}</b> — {s['label']}\n\n"

    if c["truth"] == "hostile":
        if hit:
            pts = {"fighter": 120, "bomber": 160, "uav": 80, "sam": 150, "armor": 100, "bunker": 180}[c["cls"]]
            s["score"] += pts
            s["kills"] += 1
            p["kills"] += 1
            return edit(call, head + f"💥 <b>انضرب ({c['name']})!</b> 🏅 +{pts} نقطة.",
                        mk([[("🎯 الهدف الجاي", "p:nx")]]))
        dmg = pilot_damage(s, 0.4)
        extra = f"\n⚠️ العدو ردّ وانصابت طيارتك (-{dmg}%)." if dmg else ""
        if s["hp"] <= 0:
            return pilot_end(call, uid, crashed=True)
        return edit(call, contact_text(s, head + "❌ <b>الصاروخ فات الهدف.</b> الهدف بعده موجود." + extra),
                    contact_kb(c))

    victim = c["name"]
    if not hit:
        s["score"] -= 120
        return edit(call, head + f"😮‍💨 الصاروخ فات ونجت ({victim}) بالصدفة، بس الإطلاق مخالفة "
                    "جسيمة: انخصمت 120 نقطة.", mk([[("🎯 الهدف الجاي", "p:nx")]]))
    if c["truth"] == "civil":
        p["civil_inc"] += 1
        money = random.randint(60, 250) if c["domain"] == "air" else random.randint(5, 40)
    else:
        p["friendly_inc"] += 1
        money = random.randint(60, 180) if c["domain"] == "air" else random.randint(10, 80)
    report = inquiry_report(c["identified"], c["label"], victim, c["casualties"], money)
    punish = apply_punishment(p, c["casualties"], "استهداف " + victim)
    sessions.pop(uid, None)
    p["missions"] += 1
    save_data()
    set_ctx(uid, branch="pilot", result=c["truth"], target=victim,
            truth_ar="مدني" if c["truth"] == "civil" else "صديق", tool=s["label"],
            cas=c["casualties"], money=money, loc=c["loc"], analyzed=c["identified"],
            pts=-150 if c["casualties"] < 30 else (-400 if c["casualties"] < 150 else -1000))
    edit(call, head + "💥 <b>إصابة... بس على هدف مو معادي!</b> 🚨\n\n" + report + "\n" + punish +
         "\n\n❌ انلغت المهمة.", post_kb())


# ============================================================
# المرحلة 4: طيران الدعم الأرضي (CAS) والتصدي للمضادات الأرضية
# ============================================================
CAS_ENCOUNTERS = 5
CAS_SLOT_ROUNDS = {"m": 2, "b": 2, "g": 4}          # إطلاقات لكل خانة
CAS_CAT_AR = {"m": "صواريخ جو-أرض", "b": "قنابل موجّهة", "g": "مدافع ورشاشات ثقيلة"}
CAS_CAT_ICON = {"m": "🚀", "b": "💣", "g": "🔫"}
CAS_NOUN = {"m": "الصاروخ", "b": "القنبلة", "g": "الرشّة"}

# (الاسم، الفئة، المدى كم، احتمال الإصابة، الأهداف المناسبة، وصف)
_W = [
    ("AGM-65 Maverick", "m", 25, 0.85, {"armor", "technical", "bunker", "artillery"}, "مضاد دروع وأهداف صلبة"),
    ("AGM-114 Hellfire", "m", 11, 0.88, {"armor", "technical", "artillery"}, "صاروخ ليزري دگيگ"),
    ("9K121 Vikhr", "m", 10, 0.86, {"armor", "technical", "artillery"}, "صاروخ ليزري روسي"),
    ("Brimstone", "m", 20, 0.90, {"armor", "technical"}, "مضاد دروع برادار ملّي"),
    ("9M120 Ataka-V", "m", 8, 0.84, {"armor", "technical", "bunker"}, "موجّه راديوياً"),
    ("APKWS II", "m", 11, 0.80, {"technical", "infantry", "mortar"}, "صواريخ 70 ملم موجّهة ليزرياً"),
    ("GBU-12 Paveway II", "b", 15, 0.88, {"bunker", "artillery", "armor"}, "گنبلة ليزرية 500 رطل"),
    ("GBU-38 JDAM", "b", 24, 0.90, {"bunker", "artillery", "armor", "mortar"}, "گنبلة موجّهة GPS"),
    ("GBU-39 SDB", "b", 40, 0.90, {"bunker", "artillery", "armor"}, "گنبلة صغيرة القطر"),
    ("KAB-500L", "b", 17, 0.88, {"bunker", "artillery"}, "گنبلة ليزرية روسية"),
    ("FAB-250", "b", 8, 0.70, {"infantry", "mortar", "technical", "bunker"}, "گنبلة غير موجّهة"),
    ("GAU-8 Avenger 30mm", "g", 4, 0.85, {"armor", "technical", "infantry", "mortar"}, "رشاش 30 ملم مضاد دروع"),
    ("GSh-30-2 30mm", "g", 4, 0.80, {"armor", "technical", "infantry"}, "مدفع مزدوج 30 ملم"),
    ("M102 105mm Howitzer", "g", 8, 0.86, {"bunker", "artillery", "armor", "infantry"}, "مدفع هاوتزر 105 ملم"),
    ("GAU-23/A 30mm", "g", 5, 0.78, {"technical", "infantry", "mortar"}, "مدفع بوشماستر 30 ملم"),
    ("M230 30mm Chain Gun", "g", 3, 0.78, {"technical", "infantry", "mortar"}, "المدفع الآلي للأباتشي"),
    ("M61A1 Vulcan 20mm", "g", 3, 0.72, {"infantry", "technical", "mortar"}, "رشاش 20 ملم سداسي الفوهات"),
]
CAS_WEAPONS = [dict(name=n, cat=c, rng=r, pk=pk, tags=t, desc=d) for n, c, r, pk, t, d in _W]

CAS_AIRCRAFT = [
    dict(name="A-10C Thunderbolt II", pylons=8, surv=0.90, ceil=13.6, spd="ماخ 0.56", cost=30, kind="jet",
         note="مدرّعة وتتحمل الضربات (الخنزير)", allowed={0, 1, 5, 6, 7, 11}),
    dict(name="Su-25SM3 Frogfoot", pylons=6, surv=0.80, ceil=7.0, spd="ماخ 0.8", cost=15, kind="jet",
         note="طيارة هجوم مدرّعة روسية", allowed={2, 4, 9, 10, 12}),
    dict(name="AC-130J Ghostrider", pylons=5, surv=0.50, ceil=8.5, spd="ماخ 0.45", cost=100, kind="gunship",
         note="طيارة مدفعية ثقيلة تدور فوق الهدف", allowed={1, 6, 8, 13, 14}),
    dict(name="AH-64E Apache Guardian", pylons=4, surv=0.55, ceil=6.4, spd="ماخ 0.2", cost=35, kind="heli",
         note="مروحية هجومية تطير واطية جداً", allowed={1, 3, 5, 15}),
    dict(name="L-159 ALCA", pylons=5, surv=0.60, ceil=13.0, spd="ماخ 0.82", cost=18, kind="jet",
         note="هجوم خفيفة ورخيصة ومرنة", allowed={0, 5, 6, 10, 16}),
]
CAS_HOSTILE = {
    "armor": "رتل دبابات معادي", "technical": "سيارات مسلحة (تكنيكال) معادية",
    "artillery": "بطارية مدفعية معادية", "bunker": "موقع محصّن معادي",
    "mortar": "فرقة هاون معادية", "infantry": "مجموعة مشاة معادية",
}
CAS_PTS = {"armor": 130, "technical": 90, "artillery": 150, "bunker": 160, "mortar": 100, "infantry": 80}
CAS_THREAT = {"low": ("🟡 منخفض", 0.08), "mid": ("🟠 متوسط", 0.16), "high": ("🔴 عالي", 0.28)}
MANPADS = ["Igla-S (9K338)", "FIM-92 Stinger", "Strela-2M"]
AAA = ["ZU-23-2", "KPV 14.5 ملم", "DShK 12.7 ملم", "ZSU-23-4 Shilka"]
ENGINE_FIRE = "🔥 اشتعال! إنذار حريق"
ENGINE_DMG = "⚠️ اهتزاز عنيف وتسريب وگود"


def cas_ac_kb():
    rows = [[(a["name"], f"cas:ac:{i}")] for i, a in enumerate(CAS_AIRCRAFT)]
    rows.append([("🎲 اختيار عشوائي", "cas:acr")])
    rows.append([("⬅️ رجوع", "cls:pilot")])
    return mk(rows)


def cas_ac_card(i):
    a = CAS_AIRCRAFT[i]
    return (f"🛡 <b>{a['name']}</b>\n• الدور: {a['note']}\n"
            f"• خانات التسليح: {a['pylons']}\n• المتانة والنجاة من الإصابات: {int(a['surv'] * 100)}%\n"
            f"• السگف: {a['ceil']} كم\n• السرعة: {a['spd']}\n• الكلفة: {a['cost']} مليون دولار")


def cas_arsenal(call, uid, page="m"):
    s = sessions[uid]
    a = CAS_AIRCRAFT[s["ac"]]
    lines = [f"{CAS_CAT_ICON[page]} <b>الترسانة — {CAS_CAT_AR[page]}</b>"]
    rows = []
    avail = [i for i, w in enumerate(CAS_WEAPONS) if w["cat"] == page and i in a["allowed"]]
    if not avail:
        lines.append("\n⚠️ هالطيارة ما تحمل هذا النوع من التسليح.")
    for i in avail:
        w = CAS_WEAPONS[i]
        mark = "☑" if i in s["sel"] else "☐"
        lines.append(f"{mark} <b>{w['name']}</b> — {w['rng']} كم — احتمال {int(w['pk'] * 100)}% — {w['desc']}")
        rows.append([(f"{mark} {w['name']}", f"cas:t:{i}:{page}")])
    lines.append(f"\nالخانات المستخدمة: <b>{len(s['sel'])}/{a['pylons']}</b>")
    lines.append("(الصواريخ والقنابل: الخانة = 2 إطلاق | المدافع: الخانة = 4 رشّات)")
    rows.append([("🚀 صواريخ", "cas:pg:m"), ("💣 قنابل", "cas:pg:b"), ("🔫 مدافع", "cas:pg:g")])
    rows.append([("✅ تثبيت الترسانة", "cas:lock")])
    edit(call, "\n".join(lines), mk(rows))


def gen_cas_contact():
    truth = random.choices(["hostile", "friendly", "civil"], [0.6, 0.15, 0.25])[0]
    c = {"truth": truth, "jt": 0, "app": 0, "identified": False, "casualties": 0,
         "dist": random.randint(6, 30), "alt": random.randint(0, 50), "loc": rand_loc(),
         "threat": random.choices(["low", "mid", "high"], [0.3, 0.45, 0.25])[0]}
    if truth == "hostile":
        c["cls"] = random.choice(list(CAS_HOSTILE))
        c["name"] = CAS_HOSTILE[c["cls"]]
        c["label"] = random.choices(["معادي مؤكد", "مجهول"], [0.55, 0.45])[0]
    elif truth == "friendly":
        c["cls"] = "convoy"
        c["name"] = random.choice(["رتل إمداد صديق", "نقطة سيطرة صديقة", "دورية مشاة صديقة"])
        c["label"] = random.choices(["صديق (BFT)", "مجهول"], [0.5, 0.5])[0]
        c["casualties"] = random.randint(6, 30)
    else:
        c["cls"] = "convoy"
        c["name"] = random.choice(["عوائل نازحة بالباصات", "سوق شعبي مزدحم", "مستشفى ميداني"])
        c["label"] = random.choices(["مجهول", "مدني مرجّح"], [0.6, 0.4])[0]
        c["casualties"] = random.randint(15, 120)
    if c["label"] != "مجهول":
        c["identified"] = True
    return c


def cas_visible_name(c):
    if c["identified"] or c["label"] != "مجهول":
        return c["name"]
    return "تجمّع أرضي مو واضح"


def cas_contact_text(s, note=""):
    c = s["contact"]
    lines = [
        f"🛡 <b>مهمة الدعم الأرضي — الهدف {s['enc']}/{CAS_ENCOUNTERS}</b>",
        f"• سلامة الطيارة: {s['hp']}%",
        f"• ارتفاعك فوق الأرض: {s['own_alt']} م",
        f"• المكان: {c['loc']}",
        "",
        "📻 <b>طلب إسناد من المراقب الأمامي (JTAC)</b>",
        f"🎯 الهدف: <b>{cas_visible_name(c)}</b>",
        f"• المسافة: {c['dist']} كم",
        f"• الحالة: <b>{c['label']}</b>",
        f"• التهديد الأرضي بالمنطقة: {CAS_THREAT[c['threat']][0]}",
    ]
    if c["truth"] == "civil" and c["identified"]:
        lines.append(f"• مدنيين متوقعين بالموقع: {c['casualties']}+")
    if note:
        lines.append("\n" + note)
    return "\n".join(lines)


def cas_contact_kb(s):
    c = s["contact"]
    rows = [[("🔥 إسناد ناري", "cas:fire")]]
    row = []
    if c["jt"] < 2:
        row.append(("📻 اتصال بالـJTAC", "cas:jtac"))
    if c["app"] < 2:
        row.append(("⬇️ اقتراب", "cas:app"))
    if row:
        rows.append(row)
    rows.append([("↪️ تعدّى الهدف", "cas:skip")])
    return mk(rows)


def cas_next(call, uid, note="", fresh=False):
    s = sessions[uid]
    if s["hp"] <= 0:
        return cas_emergency(call, uid, {"kind": "aaa", "name": "نيران أرضية مكثفة", "hit": True, "warn": ""}, note)
    if s["enc"] >= CAS_ENCOUNTERS:
        return cas_end(call, uid)
    s["enc"] += 1
    s["contact"] = gen_cas_contact()
    kind = CAS_AIRCRAFT[s["ac"]]["kind"]
    s["own_alt"] = random.randint(30, 300) if kind == "heli" else random.randint(300, 3000)
    text = cas_contact_text(s, (note + "\n" if note else "") + random.choice(CAS_DURING))
    (send_new if fresh else edit)(call, text, cas_contact_kb(s))


def cas_weapon_rows(s, prefix):
    rows = []
    for i in sorted(s["ammo"]):
        if s["ammo"][i] > 0:
            w = CAS_WEAPONS[i]
            rows.append([(f"{CAS_CAT_ICON[w['cat']]} إطلاق {w['name']} ({s['ammo'][i]})", f"{prefix}:{i}")])
    return rows


def cas_fire_menu(call, uid):
    s = sessions[uid]
    c = s["contact"]
    lines = ["🔥 <b>اختار السلاح</b>\n"]
    for i in sorted(s["ammo"]):
        w = CAS_WEAPONS[i]
        ok = "✅" if w["rng"] >= c["dist"] else "❌ برّه المدى (گرّب أول)"
        lines.append(f"• {w['name']} — {w['rng']} كم — ذخيرة {s['ammo'][i]} {ok}")
    rows = cas_weapon_rows(s, "cas:w")
    if not rows:
        lines.append("\n⚠️ خلصت ذخيرتك بالكامل!")
    else:
        lines.append("\nبعد الإطلاق تتابع الذخيرة لحظة بلحظة، وتگدر تطلق موجة ثانية وهي بالجو.")
    rows.append([("⬅️ رجوع", "cas:back")])
    edit(call, "\n".join(lines), mk(rows))


def cas_launch_spec(s, wi, cur_dist):
    c = s["contact"]
    w = CAS_WEAPONS[wi]
    if s["ammo"].get(wi, 0) <= 0:
        return "⚠️ خلصت ذخيرة هالسلاح."
    if w["rng"] < cur_dist:
        return f"⚠️ {w['name']} برّه المدى ({w['rng']} كم) — گرّب من الهدف أول."
    s["ammo"][wi] -= 1
    pk = w["pk"]
    if c["truth"] == "hostile" and c["cls"] not in w["tags"]:
        pk *= 0.6
    cat = w["cat"]
    spec = {"label": w["name"], "pk": max(0.05, pk), "own_alt": s["own_alt"] / 1000.0,
            "icon": CAS_CAT_ICON[cat], "noun": CAS_NOUN[cat]}
    if cat == "m":
        spec["mach"] = random.uniform(0.9, 2.0)
    elif cat == "b":
        spec["mach"], spec["arc"] = 0.8, 0.0
    else:
        spec["mach"], spec["arc"], spec["total"] = 3.0, 0.0, 4
    return spec


def cas_fire_go(call, uid, wi):
    s = sessions.get(uid)
    if not s or s["mode"] != "cas" or s.get("trk"):
        return
    c = s["contact"]
    spec = cas_launch_spec(s, wi, c["dist"])
    if isinstance(spec, str):
        return edit(call, cas_contact_text(s, spec), cas_contact_kb(s))
    trk_begin(call, uid, spec, cas_visible_name(c), c["alt"] / 1000.0, float(c["dist"]))


def cas_aa_event(s, c, mult=1.0):
    """احتمال التعرض لمضاد أرضي (MANPADS أو رشاش ثقيل) بعد كل تعرّض للخطر."""
    if random.random() > CAS_THREAT[c["threat"]][1] * mult:
        return None
    a = CAS_AIRCRAFT[s["ac"]]
    kind = random.choice(["manpads", "aaa"])
    name = random.choice(MANPADS if kind == "manpads" else AAA)
    evade = (0.25 + 0.55 * a["surv"]) if kind == "manpads" else (0.30 + 0.45 * a["surv"])
    hit = random.random() > evade
    warn = random.choice(AA_WARN_MANPADS if kind == "manpads" else AA_WARN_AAA).format(name=name)
    warn += "\n" + random.choice(AA_EVADE_FAIL if hit else AA_EVADE_OK)
    return {"kind": kind, "name": name, "hit": hit, "warn": warn}


def cas_after_action(call, uid, note, mult=1.0, kind="contact", kb=None):
    s = sessions[uid]
    c = s.get("contact")
    ev = cas_aa_event(s, c, mult) if c else None
    if ev:
        note += "\n\n" + ev["warn"]
        if ev["hit"]:
            s["hp"] = max(0, s["hp"] - random.randint(20, 40))
            return cas_emergency(call, uid, ev, note)
    if s["hp"] <= 0:
        return cas_emergency(call, uid, {"kind": "aaa", "name": "نيران أرضية مكثفة", "hit": True, "warn": ""}, note)
    if kind == "contact":
        edit(call, cas_contact_text(s, note), cas_contact_kb(s))
    else:
        edit(call, note, kb)


def cas_jtac(call, uid):
    s = sessions[uid]
    c = s["contact"]
    c["jt"] += 1
    s["jtac"] = s.get("jtac", 0) + 1
    note = "📻 " + random.choice(CAS_JTAC) + "\n"
    if random.random() < 0.9:
        c["identified"] = True
        c["label"] = {"hostile": "معادي مؤكد", "friendly": "صديق مؤكد", "civil": "مدني مؤكد"}[c["truth"]]
        note += f"🔍 تأكيد المراقب: <b>{c['name']}</b>."
    else:
        note += "🔍 المراقب ما كدر يأكد: الدخان والغبار يحجبون الرؤية."
    cas_after_action(call, uid, note, mult=1.4)


def cas_approach(call, uid):
    s = sessions[uid]
    c = s["contact"]
    c["app"] += 1
    c["dist"] = max(2, int(c["dist"] * 0.55))
    note = (f"⬇️ نزلت للواطي وگرّبت من الهدف: صار يبعد {c['dist']} كم. "
            "🧠 «كل متر أگرب أشوفه أوضح... وهم يشوفوني أوضح.»")
    cas_after_action(call, uid, note, mult=1.6)


def cas_skip(call, uid):
    s = sessions[uid]
    c = s["contact"]
    if c["truth"] == "hostile":
        s["score"] -= 20
        note = "↪️ تعدّيت هدف معادي والجنود بالأرض يدفعون الثمن (-20 نقطة)."
        if random.random() < 0.3:
            dmg = random.randint(8, 18)
            s["hp"] = max(0, s["hp"] - dmg)
            note += f"\n⚠️ نيران أرضية أصابت الطيارة (-{dmg}%)."
    else:
        pts = 10 if c["identified"] else 0
        s["score"] += pts
        note = f"↪️ تعدّيت الهدف بأمان. +{pts} نقطة."
    cas_after_action(call, uid, note, 1.0, "next", mk([[("🎯 الهدف الجاي", "cas:nx")]]))


def cas_finish(call, uid, hit, shots):
    """نتيجة الإسناد الناري بعد انتهاء التتبع."""
    s, p = sessions.get(uid), players[uid]
    if not s or s["mode"] != "cas":
        return
    c = s["contact"]
    head = f"🎯 عدد الإطلاقات: <b>{shots}</b> — {s['label']}\n\n"
    nxt = mk([[("🎯 الهدف الجاي", "cas:nx")]])

    if c["truth"] == "hostile":
        if hit:
            pts = CAS_PTS[c["cls"]]
            s["score"] += pts
            s["kills"] += 1
            p["kills"] += 1
            note = head + (f"💥 <b>انضرب ({c['name']})!</b> 🏅 +{pts} نقطة.\n"
                           f"📻 {random.choice(CAS_JTAC_OK)}")
            return cas_after_action(call, uid, note, 1.0, "next", nxt)
        note = head + "❌ <b>الذخيرة فاتت الهدف.</b> الهدف بعده موجود."
        if random.random() < 0.35:
            dmg = random.randint(8, 18)
            s["hp"] = max(0, s["hp"] - dmg)
            note += f"\n⚠️ العدو ردّ بنيران أرضية وانصابت الطيارة (-{dmg}%)."
        return cas_after_action(call, uid, note, 1.0, "contact")

    victim = c["name"]
    if not hit:
        s["score"] -= 120
        note = (head + f"😮‍💨 الذخيرة فاتت ونجا ({victim}) بالصدفة، بس الإطلاق مخالفة جسيمة: "
                "انخصمت 120 نقطة.")
        return cas_after_action(call, uid, note, 0.7, "next", nxt)
    if c["truth"] == "civil":
        p["civil_inc"] += 1
        money = random.randint(5, 40)
    else:
        p["friendly_inc"] += 1
        money = random.randint(10, 80)
    report = inquiry_report(c["identified"], c["label"], victim, c["casualties"], money)
    punish = apply_punishment(p, c["casualties"], "استهداف " + victim)
    sessions.pop(uid, None)
    p["missions"] += 1
    save_data()
    a = CAS_AIRCRAFT[s["ac"]]
    set_ctx(uid, branch="cas", result="cas_" + c["truth"], target=victim,
            truth_ar="مدني" if c["truth"] == "civil" else "صديق", tool=s["label"],
            cas=c["casualties"], money=money, loc=c["loc"], analyzed=c["identified"],
            pts=-150 if c["casualties"] < 30 else (-400 if c["casualties"] < 150 else -1000))
    edit(call, head + "💥 <b>إصابة... بس على هدف مو معادي!</b> 🚨\n\n" + report + "\n" + punish +
         "\n\n❌ انلغت المهمة.", post_kb())


# ---------------- حالة الطوارئ: الإصابة بالمضادات الأرضية ----------------
def cas_emergency(call, uid, ev, prefix=""):
    s = sessions[uid]
    a = CAS_AIRCRAFT[s["ac"]]
    fire = ev["kind"] == "manpads" or random.random() < 0.5
    s["emerg"] = {"fire": fire, "base": random.randint(40, 120),
                  "hyd": random.randint(12, 38), "roll": random.randint(25, 70)}
    e = s["emerg"]
    tpl = EMERG_HELI if a["kind"] == "heli" else EMERG_JET
    text = (prefix + "\n\n" if prefix else "") + tpl.format(
        name=ev["name"], hyd=e["hyd"], roll=e["roll"], alt=max(80, int(s["own_alt"] * 0.6)),
        base=e["base"], engine=ENGINE_FIRE if fire else ENGINE_DMG)
    edit(call, text, mk([[("🛫 محاولة الهروب والرجوع للقاعدة بالطيارة المصابة", "cas:run")],
                         [("🪂 القفز بالمظلة (Eject)", "cas:eject")]]))


def cas_run(call, uid):
    s, p = sessions[uid], players[uid]
    a, e = CAS_AIRCRAFT[s["ac"]], s["emerg"]
    p_ret = min(0.8, max(0.1, 0.30 + 0.40 * a["surv"] - 0.0015 * e["base"] - (0.12 if e["fire"] else 0)))
    r = random.random()
    if r < p_ret * 0.6:
        out = "landed"
    elif r < p_ret:
        out = "crash"
    else:
        out = "explode_eject" if random.random() < 0.35 else "explode_kia"
    s["run_out"] = out
    edit(call, RUN_NARR.format(base=e["base"], name=html.escape(p["name"]), rank=rank_name(p)),
         mk([[("▶️ كمّل", "cas:run2")]]))


def cas_run2(call, uid):
    s, p = sessions[uid], players[uid]
    out = s.get("run_out", "crash")
    f = {"name": html.escape(p["name"]), "rank": rank_name(p)}
    if out == "explode_eject":
        s["injured"] = True
        return edit(call, RUN_OUT["explode_eject"].format(**f), mk([[("▶️ كمّل", "cas:ej1")]]))
    result = {"landed": "cas_return", "crash": "cas_down", "explode_kia": "cas_kia"}[out]
    extra = {"landed": "\nرجعت الطيارة للقاعدة بهبوط اضطراري وهي متضررة بشكل كبير.",
             "crash": "\nانحطمت الطيارة بهبوط اضطراري گريب من المدرج وانتشلوا الطيار.",
             "explode_kia": "\nانفجرت الطيارة بالجو وهي تحاول الرجوع للقاعدة."}[out]
    cas_conclude(call, uid, result, RUN_OUT[out].format(**f), extra)


# ---------------- القفز بالمظلة والإنقاذ (CSAR) ----------------
def cas_eject(call, uid):
    sessions[uid]["injured"] = random.random() < 0.3
    cas_ej1(call, uid)


def cas_ej1(call, uid):
    s = sessions[uid]
    s["injured"] = s.get("injured") or random.random() < 0.3
    injury = ("ظهري يوجعني، يمكن انكسر شي... بس أگدر أحرك رجولي." if s["injured"]
              else "جسمي كله يرجف بس ماكو شي مكسور.")
    edit(call, EJ1.format(injury=injury), mk([[("▶️ كمّل", "cas:ej2")]]))


def cas_ej2(call, uid):
    s = sessions[uid]
    zone = random.choices(["friendly", "enemy", "civil"], [0.35, 0.5, 0.15])[0]
    s["zone"] = zone
    edit(call, EJ2[zone],
         mk([[("🏚 ابحث عن مأوى واختبي", "cas:hide")],
             [("📻 تواصل مع وحدة الإنقاذ (CSAR)", "cas:csar")],
             [("🧭 اتجه للخطوط الصديقة مشياً", "cas:walk")]]))


def eject_outcome(zone, opt, injured):
    if zone == "friendly":
        return "rescued"
    table = {"enemy": {"csar": 0.55, "hide": 0.50, "walk": 0.30},
             "civil": {"csar": 0.70, "hide": 0.65, "walk": 0.50}}
    pr = table[zone][opt] - (0.1 if injured else 0.0)
    if random.random() < pr:
        return "rescued"
    cap = 0.65 if zone == "enemy" else 0.25
    return "captured" if random.random() < cap else "kia"


def cas_ej3(call, uid, opt):
    s, p = sessions[uid], players[uid]
    zone = s.get("zone", "enemy")
    s["ej_res"] = eject_outcome(zone, opt, s.get("injured", False))
    text = EJ_ACTION[opt].format(
        name=html.escape(p["name"]), zone_line=EJ_ZONE_LINE[zone],
        injury_word="متوسط" if s.get("injured") else "بسيط")
    edit(call, text, mk([[("▶️ كمّل", "cas:res")]]))


def cas_res(call, uid):
    s, p = sessions[uid], players[uid]
    res, zone = s.get("ej_res", "kia"), s.get("zone", "enemy")
    f = {"name": html.escape(p["name"]), "rank": rank_name(p)}
    if res == "rescued":
        cas_conclude(call, uid, "cas_down", RES_RESCUED[zone].format(**f),
                     "\nانگفز الطيار بالمظلة وانتشلوه بنجاح بعد سگوط الطيارة.")
    elif res == "captured":
        cas_conclude(call, uid, "cas_pow", RES_CAPTURED.format(**f),
                     "\nوگع الطيار بالأسر بعد القفز، والقيادة تفاوض لتبادله.")
    else:
        cas_conclude(call, uid, "cas_kia", RES_KIA.format(**f),
                     "\nاستشهد الطيار بعد القفز بأرض العدو.")


# ---------------- خاتمة المهمة ----------------
def cas_conclude(call, uid, result, text, extra=""):
    s = sessions.pop(uid, None) or {}
    p = players[uid]
    a = CAS_AIRCRAFT[s["ac"]] if "ac" in s else CAS_AIRCRAFT[0]
    score = s.get("score", 0)
    p["missions"] += 1
    before = p["points"]
    promo = ""
    if result == "cas_kia":
        p["deaths"] = p.get("deaths", 0) + 1
        p["points"] = max(0, int(p["points"] * 0.7))
        promo = add_points(p, max(0, score))
        p["lock_until"] = time.time() + 300
        p["lock_reason"] = "استشهدت بالمهمة — انتظار تعيين طيار بديل"
        delta = p["points"] - before
    else:
        delta = {"cas_win": score + 50, "cas_fail": score, "cas_return": score + 40,
                 "cas_down": score + 20, "cas_pow": score - 100}[result]
        if result in ("cas_down", "cas_pow"):
            p["shot_down"] = p.get("shot_down", 0) + 1
        if result == "cas_pow":
            p["lock_until"] = time.time() + 240
            p["lock_reason"] = "أسير حرب — مفاوضات تبادل"
        promo = add_points(p, delta)
    save_data()
    lost = result in ("cas_down", "cas_pow", "cas_kia")
    set_ctx(uid, branch="cas", result=result, target="الأهداف بمنطقة العمليات", truth_ar="معادي",
            tool=a["name"], cas=1 if result == "cas_kia" else 0,
            money=a["cost"] if lost else random.randint(4, 12), pts=delta,
            analyzed=s.get("jtac", 0) > 0, extra=extra,
            loc=(s.get("contact") or {}).get("loc") or rand_loc())
    edit(call, text + f"\n\n📊 صافي النقاط: {delta:+d} | رتبتك: {rank_name(p)}{promo}", post_kb())


def cas_end(call, uid):
    s = sessions[uid]
    kills, score = s["kills"], s["score"]
    result = "cas_win" if kills > 0 and score > 0 else "cas_fail"
    text = (f"🏁 <b>خلصت مهمة الدعم الأرضي ورجعت للقاعدة سالم.</b>\n\n"
            f"📊 <b>تقرير المهمة</b>\n• أهداف معادية دمّرتها: {kills}\n• نقاط المهمة: {score:+d}")
    cas_conclude(call, uid, result, text)


# ---------------- الموجّه الخاص بالدعم الأرضي ----------------
def cas_router(call, uid, parts):
    p = players[uid]
    act = parts[1]
    s = sessions.get(uid)
    if act == "intro":
        return edit(call, CAS_INTRO, mk([[("🛡 إظهار طيارتك", "cas:show")], [("⬅️ رجوع", "cls:pilot")]]))
    if act == "show":
        return edit(call, "🛡 <b>اختار طيارة الدعم الأرضي:</b>", cas_ac_kb())
    if act in ("ac", "acr"):
        i = random.randrange(len(CAS_AIRCRAFT)) if act == "acr" else int(parts[2])
        sessions[uid] = {"mode": "cas", "ac": i, "sel": set(), "ammo": {}, "hp": 100,
                         "enc": 0, "score": 0, "kills": 0, "trk": None}
        return edit(call, cas_ac_card(i) + "\n\nتأكد اختيار هالطيارة؟",
                    mk([[("✅ تأكيد", "cas:pg:m")], [("🔄 غيّر", "cas:show")]]))
    if not s or s["mode"] != "cas":
        return edit(call, "⚠️ الجلسة خلصت. ارجع لغرفة العمليات.", mk([HOME_ROW]))
    if act == "pg":
        cas_arsenal(call, uid, parts[2])
    elif act == "t":
        i, page = int(parts[2]), parts[3]
        cap = CAS_AIRCRAFT[s["ac"]]["pylons"]
        if i in s["sel"]:
            s["sel"].discard(i)
        elif len(s["sel"]) >= cap:
            bot.send_message(call.message.chat.id, "⚠️ ماكو خانات تسليح متاحة!")
        else:
            s["sel"].add(i)
        cas_arsenal(call, uid, page)
    elif act == "lock":
        if not s["sel"]:
            bot.send_message(call.message.chat.id, "اختار سلاح واحد على الأقل.")
            return
        s["ammo"] = {i: CAS_SLOT_ROUNDS[CAS_WEAPONS[i]["cat"]] for i in s["sel"]}
        names = [CAS_WEAPONS[i]["name"] for i in sorted(s["sel"])]
        edit(call, "🔒 <b>انثبتت الترسانة:</b>\n• " + "\n• ".join(names) + "\n\nالطيارة جاهزة للإقلاع.",
             mk([[("🚀 الانطلاق لمنطقة العمليات", "cas:launch")]]))
    elif act == "launch":
        a = CAS_AIRCRAFT[s["ac"]]
        edit(call, random.choice(CAS_PRE).format(
            name=html.escape(p["name"]), rank=rank_name(p), ac=a["name"]))
        cas_next(call, uid, "🛫 دخلت منطقة العمليات...", fresh=True)
    elif act == "nx":
        cas_next(call, uid, "")
    elif act == "fire":
        cas_fire_menu(call, uid)
    elif act == "w":
        cas_fire_go(call, uid, int(parts[2]))
    elif act == "back":
        edit(call, cas_contact_text(s), cas_contact_kb(s))
    elif act == "jtac":
        cas_jtac(call, uid)
    elif act == "app":
        cas_approach(call, uid)
    elif act == "skip":
        cas_skip(call, uid)
    elif act == "run":
        cas_run(call, uid)
    elif act == "run2":
        cas_run2(call, uid)
    elif act == "eject":
        cas_eject(call, uid)
    elif act == "ej1":
        cas_ej1(call, uid)
    elif act == "ej2":
        cas_ej2(call, uid)
    elif act in ("hide", "csar", "walk"):
        cas_ej3(call, uid, act)
    elif act == "res":
        cas_res(call, uid)


# ============================================================
# المحتوى السردي: الحوارات، المؤتمرات الصحفية، منصات التواصل
# (كل الأسماء والأحداث خيالية)
# ============================================================
SPOKESMEN = [
    "العميد الركن مروان السالم", "اللواء الركن ماجد الحمدان",
    "العقيد الركن هشام الجابري", "العميد الركن طارق المنصور",
]

RADAR_PRE = [
    """📻 <b>[اتصال لاسلكي — القيادة العليا «فالكون-1»]</b>
«{rank} {name}، هنا القيادة. المجال الجوي بحالة تأهب گصوى، ورصدنا تحركات مو اعتيادية گرب الحدود. أريد عيونك على الشاشة؛ لا إنذار كاذب ولا هدف يفلت. جاهز؟»

🧠 <b>[حديث ذاتي]</b>
«نفسي يتسارع... الشاشة الخضرا تلمع بنقاط ما أعرف أيّهن حياة وأيّهن موت. إيدي على لوحة التحكم باردة مثل الحديد. أتذكر شنو تدربت عليه: لا تطلق گبل ما تتأكد. ثانية تروّي ممكن تنگذ عائلة، وثانية تهور ممكن تهدّ حياتك.»

📻 «مفهوم يا فالكون-1. المنظومات بوضع الاستعداد، والمسح الراداري يبدي هسه.»""",

    """📻 <b>[اتصال لاسلكي — ضابط المناوبة]</b>
«{rank} {name}، استلمت الوردية. السما هادية على غير عادتها، وهالهدوء يگلگني أكثر من الضجيج. الجو صافي والرؤية ممتازة، وكل شي مهيّأ لمن يريد يفاجئنا.»

🧠 <b>[حديث ذاتي]</b>
«الهدوء خدعة. بهالغرفة تعلّمت إن أخطر الإشارات هي اللي تبين عادية. أحدّج بالشاشة لين تتعب عيوني، وأعدّ الثواني مثل السجين يعدّ أيامه. بس ما راح أرمش.»

📻 «استلمت الوردية سيدي. العين ساهرة والإصبع بعيد عن الزناد لين التحقق.»""",

    """📻 <b>[اتصال لاسلكي — القيادة العليا «فالكون-1»]</b>
«{rank} {name}، تقارير الاستخبارات تحچي عن احتمال هجوم صاروخي خلال الساعات الجاية، وبنفس الوكت عندنا رحلات مدنية مزدحمة تعبر ممرنا الجوي. القرار هنا مو سهل: يا تحمي الأرض، يا تحمي السما من نفسها.»

🧠 <b>[حديث ذاتي]</b>
«كل غلطة بهالمهنة تنكتب بأسماء بشر. ما راح أخلي ضغط اللحظة يگرر عني. أحلل، وأوازن، وبعدين أحسم.»

📻 «مفهوم. التحليل أول، وبعدين الاشتباك. فالكون-2 على الخط.»""",
]

PILOT_PRE = [
    """📻 <b>[برج المراقبة — گبل الإقلاع]</b>
«{ac}، هنا البرج. مدرجك نظيف وجاهز، والهوا مستقر. الله يسلّمك يا {name}.»

🧠 <b>[حديث ذاتي]</b>
«أشدّ الحزام وأتفحص الشاشات وحدة وحدة: الوگود، الرادار، الحرب الإلكترونية، لوحة التسليح. صوت المحرك يهدر مثل گلب وحش گاعد يصحى. باللحظات الأخيرة گبل الانطلاق ترجعلي وجوه اللي أحبهم... وبعدين أطرد كل شي إلا المهمة.»

📻 «برج، هنا {ac}. جاهز للانطلاق.»
🔥 <i>اشتعل الحارق الخلفي... واندفعت الطيارة باتجاه السما.</i>""",

    """📻 <b>[القيادة الجوية «فالكون-3»]</b>
«{rank} {name}، مهمة اليوم حساسة: راح تشتغل فوق مسرح مزدحم تتداخل بيه أهدافنا ويّا أهداف العدو ويّا حركة مدنية بعدها ما انخلت. افتح عيونك گبل ما تفتح الزناد.»

🧠 <b>[حديث ذاتي]</b>
«أتنفس بالهدوء... أعدّ نبضي. الأكاديمية تگول إن الطيار الزين هو اللي يعرف متى ما يطلق. راح أتذكر هالشي إذا ارتجفت إيدي.»

📻 «مفهوم يا فالكون-3. أگلعت، وأدخل مجال العمليات.»""",

    """📻 <b>[لاسلكي — رئيس الفنيين]</b>
«سيدي الطيار، {ac} بأحسن حالاتها. فحصنا كل مسمار بيها مرتين، فرجّعها لنا مثل ما هي!»

🧠 <b>[حديث ذاتي]</b>
«أمازحه بابتسامة متكلّفة، بس بداخلي عگدة. اللي ينزل من هالطيارة ما يرجع إلا بسؤال واحد: أصبت لو أخطيت؟ وهل راح أگدر أنام بعد الجواب؟»

📻 «راح أرجعها لكم يا صديقي. إن شاء الله.»""",
]

RADAR_DURING = [
    "🧠 «الشاشة تومض... گلبي يسبق عگلي. تماسك.»",
    "🧠 «عرق بارد يسيل على جبيني. هذا هدف لو ظل هدف؟»",
    "📻 [القيادة]: «الوكت يمشي يا ضابط، أريد گراءة نهائية للهدف!»",
    "🧠 «صمت ثگيل بالغرفة، ما يتسمع إلا صفير المروحة وأزيز الأجهزة.»",
    "🧠 «أعيد گراءة الإحداثيات للمرة الثالثة؛ الغلط هنا ما ينصلّح.»",
    "📻 [زميلك]: «تنفّس يا {rank}... الشاشة ما تعضّ، بس القرار يعضّ.»",
]
RADAR_LAUNCH = [
    "🧠 «ضغطت الزر... وبگت عيوني معلّگة بالشاشة مثل ما أحبس نفسي وياها.»",
    "📻 [المنظومة]: «تم الإطلاق... الصاروخ بمساره.»",
    "🧠 «ثواني مثل الساعات... يا رب يكون گراري صح.»",
    "📻 [القيادة]: «رصدنا الإطلاق، شد حيلك يا بطل.»",
]
PILOT_DURING = [
    "🧠 «جسمي يثگل وياي مع كل انعطافة... أشدّ عضلات بطني حتى يظل الدم واصل لراسي.»",
    "📻 [القيادة]: «فالكون-3، گلّنا وضعك هسه.»",
    "🧠 «الشاشات التلاث تتنافس على انتباهي: الرادار، الوگود، وإنذار الاستهداف. تركيز... تركيز.»",
    "🧠 «الخوف رفيق گديم بهالقمرة؛ أتعامل وياه مثل الجاذبية: ما أنكره، بس أگاومه.»",
    "📻 [القيادة]: «تذكّر: لا تطلق إلا بعد ما تتأكد من الهوية.»",
    "🧠 «غيمة رگيگة تعبر الزجاج، والدنيا تحتي لوحة من الظلال والضوء.»",
]
PILOT_G = [
    "🧠 «الجي يضغط على صدري مثل فيل گاعد عليه... رؤيتي تضيّج شوية شوية!»",
    "🧠 «أصرخ بصوتي حتى أبقى واعي. تماسك يا جسمي، بعد ما خلصت.»",
    "🧠 «الدم يهرب من راسي، والنفس يطلع متگطع... أشدّ، أتنفس، أشدّ.»",
    "🧠 «اللوحة تهتز، وإنذار الاستهداف يصرخ... خلّي عيونك على الأفق.»",
    "🧠 «انعطافة حادة... شفت نجوم تدور بعيوني، وبعدين رجعت الدنيا مكانها.»",
]
PILOT_FIRE = [
    "🧠 «أمسك المقبض، وأحصر الهدف بالإطار... هسه!»",
    "📻 [أنا]: «فوكس-3... إطلاق!»",
    "🧠 «اهتزت الطيارة شوية وانطلگ الصاروخ مثل الشهاب. أرجو ما أندم.»",
    "📻 [أنا]: «فوكس-2... بالطريق.»",
]

DEBRIEF = {
    "radar_good": """☕ <b>استراحة گصيرة</b>

🧠 <b>[حديث ذاتي]</b>
«أسترد نفسي شوية شوية... الشاشة صارت صافية، بس گلبي بعده ما هدأ. أمسح العرگ عن جبيني وأطالع إيدي اللي بعدها ترتجف. نجحت هالمرة، بس أعرف إن الحرب ما تعرف راحة.»

📻 <b>[القيادة العليا «فالكون-1»]</b>
«{rank} {name}، عفيه. گرارك كان سريع ومحسوب، وإلنا تأكيد إيجابي. لا تنسى إن النجاح اليوم يرفع سگف توقعاتنا باجر. خذ شوية راحة، وراح يوصلك التقييم الرسمي.»

📻 «شكراً سيدي. هذا واجبي.»""",

    "radar_bad": """☕ <b>استراحة گصيرة</b>

🧠 <b>[حديث ذاتي]</b>
«الصمت مالي الغرفة... حتى صوت المروحة صار يبين وكأنه يتهمني. أطالع بالأرقام على الشاشة: {cas} ضحية، {money} مليون دولار... گدرت أسوي شي ثاني؟ ثانية زايدة... تحليل زايد... سؤال زايد. كل «لو» هسه ثگيلة مثل الرصاص.»

📻 <b>[القيادة العليا «فالكون-1»]</b>
«{rank} {name}، بلّغنا الوزارة باللي صار. لجنة التحقيق بالطريق إلك، ولا تدلي بأي تصريح گبل ما نحچي. أعرف اللحظة گاسية، بس المسؤولية ما تنتظر.»

📻 «مفهوم سيدي... أتحمل مسؤولية گراري.»""",

    "pilot_good": """☕ <b>استراحة گصيرة</b>

🧠 <b>[حديث ذاتي]</b>
«أخفّف السرعة وأطالع الأفق وهو متلوّن بلون الغروب. جسمي تعبان، وكتفي يوجعني من الجي، بس أنا عايش وخلّصت المهمة. أفكر بزملائي اللي ما راح يرجعون... وأتمسك بالسما أكثر.»

📻 <b>[القيادة الجوية «فالكون-3»]</b>
«{rank} {name}، نشوفك على الرادار راجع. التقارير تؤكد نجاح المهمة. أحسنت المناورة، وأحسنت ضبط النفس. القاعدة تنتظرك.»

📻 «شكراً فالكون-3. راجع للبيت.»""",

    "pilot_bad": """☕ <b>استراحة گصيرة</b>

🧠 <b>[حديث ذاتي]</b>
«راسي يطنّ، وصوت الإنذار بعده يتردد بأذني مع إن القمرة ساكتة. أطالع بلوحة التسليح الفاضية وأسأل نفسي: أي گرار منها كان غلط؟ ما ألگى جواب يريحني. يا ترى السما راح تسامحني؟ والأرض؟»

📻 <b>[القيادة الجوية «فالكون-3»]</b>
«{rank} {name}، المهمة خلصت بنتائج ما تسرّ. راح تخضع لتحقيق شامل بملابسات اللي صار. تذكّر إن الطيار الزين يتعلم من أخطائه گبل ما يحاسبه الناس عليها.»

📻 «مفهوم سيدي. راح أگدم كل شي عندي للجنة.»""",
}

REST_ROOM = """🛋 <b>غرفة الاستراحة والتقييم</b>

ريحة القهوة الدافية مالية المكان، وضوء خافت يعكس تعب الليل على الوجوه. هنا المقاتل يشيل الخوذة ولو لكم دقيقة.

🔋 مستوى الإرهاق: {bar}

شنو تريد تسوي؟"""

COFFEE = {
    "radar_good": """☕ <b>قهوة بغرفة العمليات</b>

الملازم عادل يمدّلك فنجان سخن وهو يبتسم: «كنت هادي مثل الجبل گدام الشاشة يا {rank}.»

— «هادي؟ لو تسمع نبضي كنت عرفت الحقيقة.»
— «هذا اللي يخليك شجاع. اللي ما يخاف ما يحسب حساب.»

تشرب القهوة بصمت. وبداخلك: «الحرب تسرق منّا أبسط التفاصيل: طعم القهوة، دفا البيت، وكت الأطفال... بس أظل هنا لأن غيري يستاهل ينام بسلام.»

🔋 تراجع إرهاقك بعد هالاستراحة.""",

    "radar_bad": """☕ <b>قهوة بغرفة العمليات</b>

يجلس گربك رقيب متقاعد خدم عشرين سنة بهالغرفة. يحط الفنجان گدامك وما يحچي دگيگة كاملة، وبعدين يگول بهدوء:

— «تدري؟ بأول سنة إلي غلطت أنا هم. ما كانت الخسائر مثل اللي تشيلها اليوم، بس ما نمت أسبوع.»
— «وشلون تجاوزتها؟»
— «ما تجاوزتها. تعلمت أشيلها... وأتحقق مرتين گبل ما أضغط.»

تطالع بگاع الفنجان: «ياريت أرجع بالوكت ثانية وحدة بس.»

🔋 تراجع إرهاقك شوية، بس الثگل ما راح.""",

    "pilot_good": """☕ <b>قهوة بغرفة الطيارين</b>

طبيب الطيران يمدّلك كوب ويسألك: «شكد وصل الجي بذروة المناورة؟» تجاوب: «ثمانية وزيادة.» يهز راسه: «جسمك من فولاذ، بس لازم ترتاح.»

تضحك: «الفولاذ يصدّي هم يا دكتور.»

وبداخلك: «لما أطالع السما من الشباك أفهم ليش اخترت هالطريق: اكو حرية ما تعرفها الأرض، بس تنذفع غالية.»

🔋 تراجع إرهاقك بعد هالاستراحة.""",

    "pilot_bad": """☕ <b>قهوة بغرفة الطيارين</b>

رئيس الفنيين يدخل شايل فنجانين، ويگعد گربك وما يسألك عن شي.

— «الطيارة زينة يا سيدي. وانت؟»
— «ما أدري... أشوف الصور تتكرر گدام عيوني.»
— «اشرب قهوتك. السما تنسّي الكل إلا الطيارين، هم اللي يشيلونها وياهم.»

تهمس لنفسك: «باجر أرجع للقمرة بحذر أكثر... أو ما أرجع.»

🔋 تراجع إرهاقك شوية، والبال بعده ثگيل.""",
}

REPORT = {
    "good": """📝 <b>التقرير الشخصي</b>

«أنا {rank} {name}، أكتب هذا التقرير بعد مهمة ناجحة. لحظات الحسم ما كانت سهلة، بس التزمت بقواعد الاشتباك وما خليت الحماس يگودني. أدري إن كل هدف أسگّطه هو حياة أنگذها، وإن كل گرار سليم هو ثگة تتراكم بصفوف القيادة.»""",
    "bad": """📝 <b>التقرير الشخصي</b>

«أنا {rank} {name}، أكتب هذا التقرير وأنا مدرك فداحة اللي صار. ما أدوّر على تبرير، لأن التبرير ما يرجّع الأرواح ولا يصلّح الخسائر. راح أعتبر هالتجربة درس ما ينتسى: التحقق مو إجراء روتيني، هو الخط الفاصل بين الجندي والمتهور.»""",
}
REPORT_STATS = """

📊 <b>بياناتك الحالية</b>
• الرتبة: {rank}
• نقاط الأداء: {points}
• أهداف معادية مدمّرة: {kills}
• حوادث استهداف مدني: {civil_inc}
• حوادث نيران صديقة: {friendly_inc}
• مهام منفّذة: {missions}"""

RESULT_AR = {
    "win": "نجاح كامل", "safe": "ضبط نفس وتجنّب الغلط", "fail": "فشل بالاعتراض",
    "crash": "انسگاط الطيارة", "civil": "استهداف هدف مدني", "friendly": "نيران صديقة",
    "violation": "إطلاق غلط على هدف مو معادي",
}
ADVICE = {
    "win": "✅ <b>الخلاصة:</b> التزمت بالمنهج الصح (تحقق وبعدين احسم). خلّيك على هالمستوى، وابتعد عن الغرور اللي يسبق الغلط.",
    "safe": "✅ <b>الخلاصة:</b> ضبط النفس أعلى درجات الاحتراف. الامتناع عن الإطلاق لمن تتعارض المؤشرات گرار شجاع مثل الإصابة بالضبط.",
    "fail": "⚠️ <b>الخلاصة:</b> إما تأخرت بالقرار أو غلطت باختيار الأداة. راجع مدى السلاح وسگفه گبل الإطلاق، واختار الأنسب للهدف.",
    "crash": "⚠️ <b>الخلاصة:</b> البقاء بدائرة تهديد معادية وكت طويل گاتل. وازن بين التعرف والمناورة، ولا تگرّب للعدو أكثر من اللازم.",
    "civil": "🚨 <b>الخلاصة:</b> الاستعجال وعدم التحليل أكثر أسباب الكوارث المدنية. القاعدة الذهبية: إذا مو متأكد لا تطلق.",
    "friendly": "🚨 <b>الخلاصة:</b> تعريف الصديق (IFF) خط الدفاع الأخير. لا تعتمد على حالة «مجهول» لوحدها، وادعم گرارك بالتحقق.",
    "violation": "⚠️ <b>الخلاصة:</b> الهدف نجا بالصدفة مو بفضل گرارك. الإطلاق بدون تأكد مخالفة جسيمة حتى لو ما صارت إصابة.",
}


def build_review(ctx, f):
    mark = lambda ok: "✅ صار" if ok else "❌ ما صار"
    first = {"radar": "تحليل الهدف گبل القرار",
             "cas": "تأكيد الهدف من المراقب الأمامي (JTAC)"}.get(ctx["branch"], "التعرّف على الهدف گبل الإطلاق")
    return (
        "🔎 <b>مراجعة الأخطاء والأداء</b>\n\n"
        f"• {first}: {mark(ctx.get('analyzed'))}\n"
        f"• أداة الاشتباك: {f['tool']}\n"
        f"• النتيجة: {RESULT_AR[ctx['result']]}\n"
        f"• التقييم بالنقاط: {ctx.get('pts', 0):+d}\n\n"
        + ADVICE[ctx["result"]]
    )


PRESS = {
    "win": [
        "🎙 <b>المؤتمر الصحفي الرسمي — {spk}</b>\n\n«بسم الله والوطن، إخوتي الصحفيين والإعلاميين، هلا بيكم. نگف گدامكم اليوم حتى نعلن نجاح مهمة نوعية نفّذها {unit} بگيادة {rank} <b>{pilot}</b>، وانحبط من خلالها تهديد مباشر كان يستهدف أمن البلد ومنشآته الحيوية.»",
        "📍 <b>تفاصيل الهدف:</b> انرصد ({target}) بـ {loc}. وبعد ما خلصنا إجراءات التحقق حسب قواعد الاشتباك، ثبتت هويته: <b>{truth}</b>. تعاملنا وياه بـ {tool}، وتحققت الإصابة حسب بيانات الرصد المستقلة.",
        "⚖️ <b>الأضرار الجانبية والخسائر:</b> نأكد إنو ما سجلنا أي خسائر بشرية بصفوفنا ولا بين المدنيين، ولا أضرار مادية على الأرض. وگيمة الأصول اللي انحمت تگدّر بحدود <b>{saved} مليون دولار</b>.",
        "📈 <b>الأثر الاستراتيجي:</b> الإيجابيات: رسالة ردع واضحة لكل من يفكر يمس أجواءنا، وإثبات جاهزية منظومتنا، ورفع الروح المعنوية. أما التحديات: فتكلفة الذخائر اللي انصرفت عالية، والعدو راح يحاول يطوّر أساليبه، وهذا يتطلب يقظة مضاعفة.",
        "❓ <b>سؤال أحد الصحفيين:</b> هاي العملية مؤشر على تصعيد جاي؟\n🎙 <b>الجواب:</b> «ما نسبق الأحداث. جاهزيتنا ثابتة، وردنا يكون بگد التهديد، ما نبحث عن التصعيد بس ما نخاف منه.»",
        "«شكراً إلكم، وراح نظل نطلعكم على كل جديد أول بأول. المؤتمر خلص.»",
    ],
    "safe": [
        "🎙 <b>المؤتمر الصحفي الرسمي — {spk}</b>\n\n«إخوتي، نعلن اليوم عن حادثة ما سجلنا بيها أي خسائر، وتعتبر مثال على احترافية گواتنا: گرار الامتناع عن الإطلاق كان أصعب من گرار الإطلاق، وانگذ أرواح بريئة.»",
        "📍 <b>تفاصيل الهدف:</b> انرصد ({target}) بـ {loc}، وكانت هويته: <b>{truth}</b>. وبعد التحليل والتحقق تعاملنا وياه حسب قواعد الاشتباك بتروّي، وما انستعمل أي سلاح ضده.",
        "⚖️ <b>الأضرار الجانبية والخسائر:</b> لا خسائر بشرية ولا مادية. والگرار الحكيم جنّبنا كارثة محتملة كانت راح تكلّفنا أرواح وسمعة وعلاقات دولية يصعب إصلاحها.",
        "📈 <b>الأثر الاستراتيجي:</b> الإيجابيات: ترسيخ ثگة الحلفاء والمجتمع الدولي بانضباط گواتنا. السلبيات: تأخر الگرار ثواني كان ممكن تكون حاسمة لو تغيّرت الظروف، وهذا يدعونا نطوّر أدوات التحقق السريع.",
        "❓ <b>سؤال أحد الصحفيين:</b> ما خفتوا يكون الهدف معادي؟\n🎙 <b>الجواب:</b> «طبعاً خفنا. بس الشجاعة إنك تگرر بعد التحقق، مو گبله.»",
        "«شكراً على الحضور والمتابعة.»",
    ],
    "fail": [
        "🎙 <b>المؤتمر الصحفي الرسمي — {spk}</b>\n\n«إخوتي، ما راح نخفي عليكم الحقيقة: واجهنا اليوم حادثة مؤلمة، منظومتنا الدفاعية ما نجحت بإيقاف التهديد مثل ما كنا نتأمل، ونتحمل المسؤولية كاملة گدام شعبنا.»",
        "📍 <b>تفاصيل الهدف:</b> كان الهدف ({target}) <b>{truth}</b>، وانرصد بـ {loc}. صار الاشتباك بـ {tool}، بس النتيجة ما كفت لمنعه من الوصول لأهدافه.{extra}",
        "⚖️ <b>الأضرار والخسائر:</b> وصلت الخسائر البشرية لـ <b>{cas}</b>، والخسائر المادية التقديرية <b>{money} مليون دولار</b>. ونأكد إن فرق الإنقاذ والإسعاف شغّالة على مدار الساعة، وإن ذوي الضحايا راح يلگون الرعاية الكاملة.",
        "📈 <b>الأثر الاستراتيجي:</b> السلبيات: ثغرة كشفتها العملية بمنظومة الدفاع، وضرر معنوي ممكن العدو يستغله دعائياً. الإيجابية الوحيدة: درس غالي يدفعنا نراجع الإجراءات ونگوّي طبگات الدفاع.",
        "❓ <b>سؤال أحد الصحفيين:</b> منو يتحمل المسؤولية؟\n🎙 <b>الجواب:</b> «القيادة تتحمل المسؤولية أول شي. وراح نسوي تحقيق شامل ونعلن نتائجه بشفافية.»",
        "«نعتذر عن الإجابة عن أسئلة زايدة بالوكت الحالي.»",
    ],
    "civil": [
        "🎙 <b>المؤتمر الصحفي العاجل — {spk}</b>\n\n«إخوتي، بگلوب مثگلة بالحزن، نعلن إن هدف انستهدف بالغلط وتبيّن إنه مدني، وهذا الخطأ الجسيم يگع على عاتق منظومتنا العسكرية. نگدّم أحرّ التعازي لأهالي الضحايا.»",
        "📍 <b>تفاصيل الهدف:</b> كان الهدف ({target}) <b>{truth}</b>، وانرصد بـ {loc}. صار الاشتباك وياه بـ {tool}، وما اكتملت إجراءات التحقق المطلوبة گبل گرار الإطلاق، حسب النتائج الأولية.",
        "⚖️ <b>الخسائر:</b> وصلت الخسائر البشرية لـ <b>{cas}</b> ضحية، والخسائر المادية <b>{money} مليون دولار</b>. وتشكلت لجنة تحقيق عاجلة، وراح تنتخذ الإجراءات التأديبية بحق المسؤولين مهما كانت رتبهم.",
        "📈 <b>الأثر الاستراتيجي:</b> السلبيات: ضرر خطير بالسمعة الدولية، ومساءلات گانونية محتملة، وفگدان ثگة الرأي العام. أما الإيجابيات: مراجعة جذرية لنظام التحقق وقواعد الاشتباك.",
        "❓ <b>سؤال أحد الصحفيين:</b> الغلط بشري لو تگني؟\n🎙 <b>الجواب:</b> «النتائج الأولية تشير لخطأ بشري باتخاذ الگرار. ما نحمّل التگنية شي ما إلها ذنب بيه.»",
        "«نعدكم بنشر نتائج التحقيق كاملة. وشكراً إلكم.»",
    ],
    "friendly": [
        "🎙 <b>المؤتمر الصحفي العاجل — {spk}</b>\n\n«إخوتي، نعلن بكل أسف عن حادثة نيران صديقة صارت اليوم، انستهدف بيها هدف من گواتنا بالغلط. ننحني إجلالاً لأرواح اللي فگدناهم.»",
        "📍 <b>تفاصيل الهدف:</b> كان الهدف ({target}) <b>{truth}</b>، وانرصد بـ {loc}. صار الاشتباك بـ {tool} بدون تأكيد هويته عبر منظومة التعريف.",
        "⚖️ <b>الخسائر:</b> الخسائر البشرية <b>{cas}</b>، والخسائر المادية <b>{money} مليون دولار</b>. وباشرت لجنة التحقيق شغلها، وراح يتحاسب المقصّرين حسب الأنظمة العسكرية.",
        "📈 <b>الأثر الاستراتيجي:</b> السلبيات: ضرب لثگة الجنود ببعضهم، واهتزاز معنويات الوحدات، ومادة دسمة لدعاية العدو. الإيجابيات: فرصة لمراجعة أنظمة التعريف وتنسيق الاتصالات.",
        "❓ <b>سؤال أحد الصحفيين:</b> شلون صار وما تعرّفت المقاتلة الصديقة؟\n🎙 <b>الجواب:</b> «هذا بالضبط اللي تتحقق بيه اللجنة؛ وما راح نخلي سؤال بلا جواب.»",
        "«نكتفي بهالگدر. الرحمة لشهدائنا.»",
    ],
    "violation": [
        "🎙 <b>المؤتمر الصحفي — {spk}</b>\n\n«إخوتي، نعلن عن حادثة إطلاق مو مبرر على هدف مو معادي، وبفضل الله ما صارت إصابة. نعتبرها مخالفة جسيمة وإنذار حقيقي.»",
        "📍 <b>تفاصيل الهدف:</b> كان الهدف ({target}) <b>{truth}</b>، وانرصد بـ {loc}. انطلگ عليه {tool} گبل تأكيد هويته، والصاروخ فات الهدف.",
        "⚖️ <b>الخسائر:</b> لا خسائر بشرية ولا مادية، بس النجاة جات بالصدفة مو بفعل گراراتنا.",
        "📈 <b>الأثر الاستراتيجي:</b> السلبيات: ضعف ببروتوكول التحقق ومخاطرة مو مقبولة. الإيجابيات: ما صارت خسائر، وهاي فرصة للتصحيح گبل ما تتحول لكارثة.",
        "❓ <b>سؤال أحد الصحفيين:</b> راح يتعاقب الضابط؟\n🎙 <b>الجواب:</b> «انوجّه له إنذار رسمي وانخصمت نقاط، والمراجعة مستمرة.»",
        "«شكراً إلكم.»",
    ],
}
PRESS["crash"] = PRESS["fail"]


def build_press(result, f):
    return "\n\n".join(PRESS[result]).format(**f)


USERS = [
    ("أبو ماجد", "abu_majid_77"), ("سارة", "sara.iq95"), ("ليث", "laith_alsaqr"),
    ("نور", "nour.baghdad"), ("ياسر", "yasser_k_"), ("رنا", "rana.hawa"),
    ("سيف", "saif_alwatan"), ("هدى", "huda_voice"), ("علاء", "alaa.official"),
    ("ضرغام", "dirgham_88"), ("مروة", "marwa.iq"), ("وسام", "wissam_x"),
    ("حنان", "hanan_2000"), ("فراس", "firas_basrawi"), ("رعد", "raad_alsahar"),
    ("دلال", "dalal.mosul"), ("بيان", "bayan_n"), ("أبو سلطان", "abu_sultan_iq"),
    ("المواطن الغيور", "ghayoor_iq"), ("محلل عسكري مستقل", "indep_mil_iq"),
    ("جنود الظل", "shadow_soldiers_iq"), ("أم علي", "um_ali_kitchen"),
]
PLATFORMS = ["𝕏", "📸 إنستغرام", "🎵 تيك توك"]

PRO_WIN = [
    "صقورنا بالسما! {t} انضرب وبگت أجواءنا عزيزة 🦅",
    "فخورين بيكم يا أبطال، الله يحميكم ويحفظ العراق 🙏",
    "هاي الجاهزية اللي نحچي عنها. تحية لكل عين ساهرة على الشاشة!",
    "اللي يظن يگدر يخترق سمانا لازم يراجع حساباته 💪",
    "الجيش خط أحمر! نصر جديد يضاف لسجل مشرّف.",
    "اللهم احفظ جنودنا وسدّد رميتهم وارزگهم الثبات.",
    "هذا اللي كنا ننتظره: ردّ سريع ودگيگ وبلا ضجة.",
    "عاشت إيدك يا بطل، تستاهل الترقية فوراً 👏",
    "لمن تحچي الأفعال تسكت الأقاويل. الله يوفگ گواتنا المسلحة!",
]
CRIT_WIN = [
    "الأرقام تگول إن تكلفة الاعتراض عالية هواية. وين الشفافية بالميزانية؟",
    "رواية رسمية بس وبلا صور ولا فيديو. نصدّگ بس من الكلام؟",
    "ليش دايماً نضخّم الإنجاز بالإعلام والوضع الاقتصادي يتدهور؟",
    "نريد نعرف شلون تسلّل الهدف أصلاً. السؤال أهم من الاحتفال.",
    "كل مرة نسمع «نجاح باهر»... وما نعرف التفاصيل الحقيقية.",
]
PRO_SAFE = [
    "گرار حكيم! حسن التقدير نگذ أرواح بريئة 👏",
    "هذا الاحتراف الحقيقي: تمتنع لمن يلزم الامتناع.",
    "شكراً لأنكم ما تسرّعتوا. {t} عبرت بسلام بفضل تروّيكم.",
    "الشجاعة مو دايماً بالضغط على الزناد 🙌",
    "ضابط يُحتذى بيه. يا ريت كل القادة بهالانضباط.",
    "سلام على اللي يگرر بعگله مو بخوفه.",
    "هذا اللي نريده من جيش دولة محترمة.",
]
CRIT_SAFE = [
    "وإذا كان معادي؟ المجازفة كبيرة والگرار متأخر!",
    "الحمد لله على السلامة... بس ليش تأخر التحقق هالگد؟",
    "هذا كل شي؟ نريد التفاصيل مو الشعارات.",
    "التبرير سهل بعد ما يخلص الخطر. وين الخطة البديلة؟",
    "تحسين نظام التعريف أهم من التصفيق للگرار.",
]
DEF_BAD = [
    "لا تحاكمون جندي گرر بثواني تحت ضغط الحرب. هذا ظلم 🙏",
    "الجيش خط أحمر. الغلط وارد والتحقيق يبيّن الحقيقة.",
    "اللي ما يشتغل ما يغلط. خلّو أهل الاختصاص يعالجون الموضوع.",
    "كلنا ورا گواتنا بالسرّاء والضرّاء وراح نبقى.",
    "المتصيدين يستغلون المأساة. خلّو الرجال تسوي شغلها.",
    "اللهم ارحم الضحايا وألهم الجنود گوة التحمل.",
]
CRIT_INCIDENT = [
    "{cas} ضحية ثمن الاستعجال؟ منو يحاسب اللي دفعوا الضابط لهالگرار؟",
    "الرواية الرسمية متناگضة من أول ساعة. نريد لجنة مستقلة مو لجنة الجيش!",
    "العگوبة على الجندي، والمنظومة اللي صنعت الغلط بلا حساب؟ كالعادة!",
    "وين الشفافية؟ ليش ما تنشرون تسجيلات الرادار كاملة؟",
    "كل مرة نفس الاعتذار ونفس «فتح التحقيق». وبعدين ماكو شي.",
    "تعازينا لأهالي الضحايا... والعار لمن تهاون بالتحقق.",
    "هذي نتيجة الإعلام المضلل والبطولات الوهمية گبل ما تكتمل الحقيقة.",
    "دماء الأبرياء مو أرگام ببيان صحفي!",
    "منو يحمي الجيش من أخطائه؟ ما ينفع نصفّگ بكل مرة.",
    "السمعة الدولية راحت بثانية، والتبريرات ما راح ترجعها.",
]
CRIT_FAIL = [
    "وين كانت المنظومات اللي نصرف عليها المليارات؟",
    "فشل واضح بالدفاع. منو المسؤول الحقيقي؟",
    "كل يوم تصريح جديد وبلا نتيجة على الأرض.",
    "ما نريد خطب حماسية... نريد حماية فعلية.",
    "المؤتمر الصحفي ما يغني عن تصحيح الأخطاء.",
    "اعترفوا بالتقصير بدل ما تدوّرون على شمّاعة.",
    "هذا اللي يصير لمن تنهمل الجاهزية لصالح الدعاية.",
    "نطالب بالمحاسبة العلنية مو بالبيانات المختصرة.",
]
TAGS = {
    "win": "#صقور_السما", "safe": "#حسن_التقدير", "fail": "#فشل_الدفاع",
    "crash": "#سقوط_المقاتلة", "civil": "#ضحايا_الاعتراض",
    "friendly": "#النيران_الصديقة", "violation": "#الإطلاق_الغلط",
}
SPLIT = {"win": (7, 3), "safe": (6, 4), "fail": (3, 7), "crash": (4, 6),
         "violation": (3, 7), "civil": (2, 8), "friendly": (2, 8)}


def _pools(result):
    if result == "cas_win":
        return PRO_CAS, CRIT_WIN
    if result in ("cas_return", "cas_down"):
        return PRO_PILOT_SAFE, CRIT_LEAD
    if result in ("cas_pow", "cas_kia"):
        return PRO_PILOT_LOST, CRIT_LEAD
    if result == "cas_fail":
        return DEF_BAD, CRIT_FAIL
    if result in ("cas_civil", "cas_friendly"):
        return DEF_BAD, CRIT_INCIDENT
    if result == "win":
        return PRO_WIN, CRIT_WIN
    if result == "safe":
        return PRO_SAFE, CRIT_SAFE
    if result in ("civil", "friendly"):
        return DEF_BAD, CRIT_INCIDENT
    return DEF_BAD, CRIT_FAIL


def build_social(result, f):
    pro_pool, crit_pool = _pools(result)
    n_pro, n_crit = SPLIT[result]
    items = [("pro", x) for x in random.sample(pro_pool, min(n_pro, len(pro_pool)))]
    items += [("crit", x) for x in random.sample(crit_pool, min(n_crit, len(crit_pool)))]
    random.shuffle(items)
    users = random.sample(USERS, len(items))
    out = [f"📱 <b>شنو يتداولون الناس هسه</b>\nالوسم الأكثر تداولاً: <b>{TAGS[result]}</b>\n"]
    for (side, text), (name, handle) in zip(items, users):
        icon = "🟢" if side == "pro" else "🔴"
        likes = random.randint(120, 48000)
        rts = random.randint(10, max(11, likes // 3))
        out.append(
            f"{icon} <b>{name}</b> · @{handle} · {random.choice(PLATFORMS)}\n"
            f"{text.format(**f)}\n❤️ {likes:,}  🔁 {rts:,}"
        )
    out.append("\n🟢 مؤيدين  |  🔴 منتقدين وشكّاكين")
    return "\n\n".join(out)


# ============================================================
# محتوى سردي: طيران الدعم الأرضي (باللهجة العراقية)
# ============================================================
CAS_INTRO = """🛡 <b>طيران الدعم الأرضي — هجوم أرضي (CAS)</b>

هلا بيك يا بطل الدعم القريب. هنا شغلك مختلف: انت مو طيار اعتراض بالسما العالية، انت «ملاك الحماية» لجنود المشاة اللي يگاتلون بالأرض. تطير واطي، گريب من الأهداف، وتشوف الوجوه والأسلحة بعيونك... وهذا يخليك هدف سهل للمضادات الأرضية: صواريخ الكتف (إيغلا وستينغر) والرشاشات الثقيلة.

• المراقب الأمامي (JTAC) هو عيونك على الأرض، اتصل بيه گبل ما تضرب.
• الخطأ هنا موجع: كل باص أو سوق أو رتل صديق ممكن يختلط بالعدو.
• إذا انصابت طيارتك راح تگرر: ترجع للقاعدة بالطيارة المصابة لو تگفز بالمظلة.

اضغط الزر حتى تشوف طيارتك."""

CAS_PRE = [
    """📻 <b>[اتصال لاسلكي — غرفة عمليات الدعم الجوي «فالكون-5»]</b>
«{rank} {name}، هنا فالكون-5. قواتنا الأرضية محاصرة بمنطقة مفتوحة وتتعرض لنيران كثيفة. المراقب الأمامي ينتظر دعمك بأي لحظة. انت طيار {ac}... وهذا اللي نحتاجه هسه.»

🧠 <b>[حديث ذاتي]</b>
«بالأرض شباب مثلي، أمهاتهم ينتظروهم... وأنا حبلهم الأخير. أطير واطي، وأعرف إن كل رصاصة مضادة تنطلق من الأرض ممكن تلگاني. بس الجندي اللي يصرخ بالراديو ما يخاف من شي أكثر من إنك ما تجي.»

📻 «مفهوم فالكون-5. أنا بالطريق، جاهز للدعم.»
🔥 <i>انطلگت الطيارة واطية فوق التلال...</i>""",

    """📻 <b>[المراقب الأمامي «ذئب-2»]</b>
«طيار {ac}، هنا ذئب-2. وضعنا صعب. العدو يتحرك بأرتال مختلطة، وفي مدنيين بالمنطقة، ولازم نكون حذرين. لا تضرب إلا بعد ما أعطيك التأكيد. احنا بخندق شرق التل، والعدو شمال غرب. فاهم؟»

🧠 <b>[حديث ذاتي]</b>
«أعرف شنو يعني هالكلام... غلطة وحدة وأكون سبب مجزرة. التأكيد أول، والإطلاق ثاني. هذي القاعدة.»

📻 «فاهم ذئب-2. أشوف مسرحك من فوق، وبانتظار التأكيد.»""",

    """📻 <b>[رئيس الفنيين]</b>
«سيدي، {ac} جاهزة وبأعلى كفاءة، بس تذكّر: الأرض بالمنطقة مليانة مضادات وصواريخ كتف. طيّر واطي وانتبه للفلير.»

🧠 <b>[حديث ذاتي]</b>
«الفلير... الفلير هو أملي الوحيد إذا انطلگ صاروخ. بس شنو لو ما نفع؟ لا... لا أفكر بهالشي هسه. أرجع واحد يعني أرجع.»

📻 «مفهوم يا بطل. نشوفك بعد المهمة.»""",
]
CAS_DURING = [
    "🧠 «الأرض تمرّ تحتي بسرعة... أشوف الدخان والغبار والسواتر الترابية. وين أنت يا عدو؟»",
    "📻 [JTAC]: «طيار، الشباب تحت النار! أحتاج إسنادك بأسرع وكت!»",
    "🧠 «أطير واطي مثل ما علّمونا... كل شي حولي قريب لدرجة أشوف وجوه الناس.»",
    "🧠 «عيوني تفتّش عن أي وميض... صاروخ كتف يطلع من وين؟ ورا الشجرة؟ فوق السطح؟»",
    "📻 [فالكون-5]: «حافظ على الارتفاع، لا تتأخر بالمدار.»",
    "🧠 «گلبي يدگ بسرعة، بس إيدي ثابتة. ثبات... ثبات.»",
]
CAS_FIRE = [
    "🧠 «أحصر الهدف بالإطار... المراقب يعطيني الضوء الأخضر. هسه!»",
    "📻 [أنا]: «ذئب-2، هنا الطيار، ذخيرة بالجو... انتبهوا!»",
    "🧠 «انطلگت الذخيرة وصار گلبي معلّق بيها... يا رب تصيب والجنود يبقون بأمان.»",
    "📻 [JTAC]: «شايفها يا طيار! لا تغيّر مسارك، خليها توصل!»",
]
CAS_JTAC = [
    "[JTAC «ذئب-2»]: «طيار، أنا أشوف الهدف بالمنظار، خليني أوصفلك الموقع... هسه!»",
    "[JTAC «ذئب-2»]: «انتظر طيار، ما أريد أي غلطة. خليني أتأكد من الإشارات.»",
    "[JTAC «ذئب-2»]: «هنا ذئب-2، أستعمل ليزر التحديد على الهدف، أنت تشوف النقطة؟»",
    "[JTAC «ذئب-2»]: «طيار، طلبي مثل ما هو: تأكيد أول وبعدين إطلاق.»",
    "[JTAC «ذئب-2»]: «سمعت صوتك واضح طيار، أراجع بياناتي هسه.»",
    "[JTAC «ذئب-2»]: «انتظر... أشوف حركة بالموقع، ما أريد نخطئ.»",
]
CAS_JTAC_OK = [
    "[JTAC «ذئب-2»]: «إصابة مباشرة يا طيار! الله يعطيك العافية، الشباب يصفقون لك!»",
    "[JTAC «ذئب-2»]: «تدمير مؤكد! أنت أنقذتنا يا بطل!»",
    "[JTAC «ذئب-2»]: «رائع! الهدف تحول لرماد، تابع للهدف الجاي.»",
]
AA_WARN_MANPADS = [
    "🚨 <b>MISSILE LAUNCH!</b> إنذار انطلاق صاروخ كتف ({name}) من الأرض... الإنذار يصرخ: «ميسايل! ميسايل!»",
    "🚨 <b>تحذير: صاروخ بالجو!</b> شفت شرارة من وراء الحقل، صاروخ ({name}) يطلع باتجاهي بدخان أبيض!",
    "🚨 <b>تنبيه الرادار:</b> حرارة انطلاق ({name}) من موقع مضاد... الصاروخ مقفل علي!",
]
AA_WARN_AAA = [
    "⚠️ <b>نيران مضادة!</b> رشاش ({name}) بدأ يطلق عليك... الخطوط الحمراء تمرّ گدام الزجاج!",
    "⚠️ <b>تحذير:</b> رشاش ثقيل ({name}) يحصدك من الأرض! الإضاءات التتبعية تحيط بالطيارة!",
    "⚠️ <b>رشاش مضاد للطيران ({name})</b> يغطي السما بالنار حواليك... الجو صار مثل الجحيم!",
]
AA_EVADE_OK = [
    "🎇 نثرت الفلير وسويت انعطافة حادة... مرّ الصاروخ/الرصاص من جنبي وانفجر بعيد! نجيت بأعجوبة.",
    "🎇 الفلير اشتغل بالوكت المناسب وانحرف عني التهديد! ضربت الهوا بإيدي من الفرحة.",
    "🎇 شديت المقود بگوة وانزلقت يمين... وهو عدّى! ربي ستر.",
]
AA_EVADE_FAIL = [
    "💥 الفلير ما نفع! انفجر بذيل الطيارة وهزّت القمرة بعنف!",
    "💥 انضربت! سمعت صوت انفجار وراي والطيارة قفزت للأعلى بشكل مخيف!",
    "💥 إصابة مباشرة! الطيارة ارتجفت بالكامل وبدأ الدخان يطلع من الخلف!",
]

EMERG_JET = """🚨🚨🚨 <b>MASTER CAUTION — تقرير الطوارئ بالكبينة</b>

⚠️ انصابت طيارتك! ({name}) ضربتك.

📟 <b>لوحة الإنذارات تصرخ:</b>
• ضغط الهيدروليك: <b>{hyd}%</b> وبعده ينزل ⬇️
• المحرك: <b>{engine}</b>
• التوازن: الطيارة مايلة <b>{roll}°</b> والمقود ثگيل... فگدان السيطرة!
• ارتفاعك: <b>{alt} م</b> وبعده ينزل
• المسافة للقاعدة: <b>{base} كم</b>

🧠 <b>[حديث ذاتي]</b>
«گلبي يدگ بإذني مثل الطبل... الدخان يتسرب بالقمرة والعرگ ينزل بعيوني. اصبر... اصبر. شنو أسوي؟ أحاول أرجع لو أگفز؟ ثواني وتنتهي الفرصة...»

📻 <b>[المراقب الأمامي]</b>: «طيار! شفناك تدخّن! طلّع نفسك أو ارجع حالاً، الگرار إلك!»

⚖️ <b>گرارك مصيري — الوكت بالثواني:</b>"""

EMERG_HELI = """🚨🚨🚨 <b>MASTER CAUTION — تقرير الطوارئ بالكبينة</b>

⚠️ انصابت المروحية! ({name}) ضربتها.

📟 <b>لوحة الإنذارات تصرخ:</b>
• ضغط الهيدروليك: <b>{hyd}%</b> وبعده ينزل ⬇️
• المحرك: <b>{engine}</b>
• الدوّار الخلفي: اهتزاز عنيف والمروحية تدور حول نفسها <b>{roll}°</b>... فگدان التوازن!
• ارتفاعك: <b>{alt} م</b> وبعده ينزل
• المسافة للقاعدة: <b>{base} كم</b>

🧠 <b>[حديث ذاتي]</b>
«المروحية تدور ويّاي مثل ورقة بالريح... الأرض تگرب وتنزل بسرعة. إذا بقيت أحاول أسيطر ممكن تنفجر، وإذا گفزت من ارتفاع واطي ممكن المظلة ما تنفتح...»

📻 <b>[المراقب الأمامي]</b>: «طيار! شفناك تدخّن! الگرار إلك، بسرعة!»

⚖️ <b>گرارك مصيري — الوكت بالثواني:</b>"""

RUN_NARR = """🛫 <b>محاولة الهروب والعودة للقاعدة</b>

🧠 «ما راح أترك الطيارة... مو هسه. أشدّ المقود بالإيدين الثنتين وأحاول أثبّت الأفق. أخفف الدفع، أنزل الارتفاع، وأتجه للقاعدة اللي تبعد {base} كم.»

📻 «برج القاعدة، هنا {name}، انصبت بنيران أرضية، ضغط الهيدروليك يتناگص وفي حريق. أطلب هبوط اضطراري فوراً!»
📻 <b>[البرج]</b>: «مستلمين يا {rank}، فتحنا المدرج، سيارات الإطفاء والإسعاف جاهزة. شد حيلك!»

🧠 «الدخان يزيد... الإنذارات تصرخ لدرجة ما أسمع حتى أفكاري. الأرض تگرب شوية شوية، والدقايق تمر مثل الدهر...»

اضغط «كمّل» حتى تشوف شنو صار."""

RUN_OUT = {
    "landed": """🛬 <b>هبوط اضطراري ناجح!</b>

عجلات الطيارة لمست المدرج بعنف والشرار يتطاير من البطن... المكابح تصرخ، والطيارة تنزلق وتنزلق... وبعدين وگفت. فتحت الغطاء وطلعت وسط رغوة الإطفاء والجنود يركضون باتجاهك.

📻 <b>[البرج]</b>: «ممتاز يا بطل! نزلت بالطيارة وهي تحترق! طيار أسطوري!»

🧠 «رجلي ترتجف... بس أنا هنا. على الأرض. حي. الطيارة تضررت بشكل كبير، بس هذا شي يتعوض.»""",

    "crash": """🔥 <b>هبوط اضطراري عنيف — الطيارة ضاعت!</b>

گبل ما توصل المدرج بكيلومترات فگدت السيطرة بالكامل... الأرض مالت عليك بسرعة، شديت المقود بكل گوتك، وبعدين... الارتطام! الدنيا اسودّت لثواني، وصحيت على ريحة الوگود والنار حواليك. طلّعتك سيارة الإنقاذ من بين الحطام بالعافية.

🧠 «شجرة... حفرة... ما أدري. بس أنا عايش. الطيارة راحت، بس أنا عايش.»""",

    "explode_kia": """💥 <b>انفجار بالجو!</b>

وسط محاولة الهروب، دخل الحريق بخزان الوگود... سمعت صوت وكأن السما انشگّت، وبعدين... صمت. الطيارة تحولت لكرة نار بالسما. البرج سمع آخر كلمة منك بالراديو.

📻 <b>[البرج]</b>: «طيار؟! طيار؟! رد علينا!... ماكو رد.»

🕯 <b>استشهد الطيار {name} وهو يحاول يرجع بطيارته للقاعدة.</b> الله يرحمه ويسكنه فسيح جناته.""",

    "explode_eject": """💥 <b>الطيارة تنفجر من تحتك!</b>

حسّيت بالاهتزاز الأخير... وگبل ما تنفجر الطيارة بالكامل شديت مقبض الگفز بكل گوتك! الكرسي انطلگ بعنف...

🧠 «آخر ثانية... آخر ثانية!»

اضغط «كمّل».""",
}

EJ1 = """🪂 <b>القفز بالمظلة — Eject! Eject! Eject!</b>

📻 «ميديه... ميديه... ميديه! گفز گفز گفز!»

شديت المقبض الأصفر والأسود... الغطاء انتزع، وكرسي الگفز رماك للسما بعنف يفوق الوصف: 🔥 <b>گوى جي هائلة</b>، العمود الفقري يتگصّف، والهوا الجاي بسرعة مئات الكيلومترات يصفع وجهك... لفّيت ولفّيت بالجو، والسما والأرض تدور وياك.

بعدين... صوت «فووومم» وانفتحت المظلة. الدنيا هدأت فجأة. شفت طيارتك تهوي بعيد وهي تحترق ودخانها يطلع للسما بعمود أسود، وبعدين ارتطمت بالأرض وانفجرت.

🧠 «أنا حي... يا رب لك الحمد. {injury}»"""

EJ2 = {
    "friendly": """🌄 <b>الهبوط بأرض صديقة</b>

نزلت ببطء والريح تسحبك فوق حقول وسواتر ترابية... شفت من فوق مدرعات بعلم وطني وجنود يلوحون إلك! حطّيت رجليك على الأرض ورميت المظلة، وبعدين سمعت صوت جندي يصيح: «احنا ويّاك يا طيار! لا تخاف!»

🧠 «الحمد لله... وصلت لإيدين أمينة.»

⚖️ <b>شنو تريد تسوي؟</b>""",
    "enemy": """🏜 <b>الهبوط بأرض العدو</b>

الريح جرّتك بعيد عن مسار الطيارة... نزلت فوق أرض مفتوحة وعرة، وبعيد شفت دخان عجلات وسمعت أصوات رشاشات! هذي أرض العدو! رميت المظلة بسرعة وكبّيتها بحفرة، وأصوات سيارات تگرب من جهة موقع التحطم.

🧠 «هسه يدوّرون علي. كل ثانية تفرق...»

⚖️ <b>شنو تريد تسوي؟</b>""",
    "civil": """🏘 <b>الهبوط قرب قرية</b>

نزلت قرب قرية صغيرة بأطراف الريف، ما أدري تابعة لمين. شفت أطفال يركضون وكلاب تنبح، وفلاح عجوز يطالعك من بعيد وبيده شي... فأس؟ لو عصا؟ ما أدري إذا عدو لو صديق.

🧠 «الجماعة ما يعرفون شنو يسوون... وأنا ما أعرف شنو أسوي.»

⚖️ <b>شنو تريد تسوي؟</b>""",
}
EJ_ZONE_LINE = {
    "friendly": "الأرض حواليك صديقة وهادية نسبياً، وسمعت محركات مدرعاتنا گريبة.",
    "enemy": "ورا الصخرة سمعت أصوات جنود العدو يتبادلون الكلام بصوت عالي، يدوّرون عليك بالضبط!",
    "civil": "سمعت صوت غنم وأطفال... والقرية گريبة، وما أدري أي جهة هي.",
}
EJ_ACTION = {
    "hide": """🏚 <b>البحث عن مأوى</b>

زحفت بين الصخور والأعشاب لين لگيت مخزن مهجور. اختبيت بالداخل، ولصقت ظهرك بالجدار، وفتحت المسدس. صوتك أخفض من الهمس، والنفس مكتوم.

{zone_line}

🧠 «لا تتحرك... لا تتنفس... انتظر.»

اضغط «كمّل».""",
    "csar": """📻 <b>الاتصال بوحدة الإنقاذ (CSAR)</b>

طلعت جهاز الطوارئ PRC-112 من سترة النجاة وفتحته:
📻 «ميديه ميديه ميديه، هنا {name}، أنا حي ومصاب بشكل {injury_word}، أطلب إنقاذ فوراً!»

كم ثانية صمت... وبعدين صوت متقطع: 📻 <b>[CSAR «نسر-9»]</b>: «سمعناك يا {name}! ابقَ مكانك، المروحية بالطريق، بعد 15 دقيقة! أشعل الإشارة لما تسمع الدوّار.»

{zone_line}

اضغط «كمّل».""",
    "walk": """🧭 <b>الاتجاه للخطوط الصديقة مشياً</b>

حسبت الاتجاه بالبوصلة والنجوم، وبديت أمشي باتجاه الخطوط الصديقة، أتخفى بين الأودية والسواتر الترابية. رجلي تثگل وأنفاسي تتلاحق...

{zone_line}

اضغط «كمّل».""",
}
RES_RESCUED = {
    "friendly": """🚁 <b>تم الإنقاذ!</b>

وصلت دورية صديقة بعد وكت قصير... شالوك على نقالة وركّبوك بسيارة إسعاف لين القاعدة الأمامية. طبيب الميدان فحصك وگال: «سلامات يا بطل.»

🧠 «السما أخذت طيارتي... بس رجعت لي حياتي.»""",
    "enemy": """🚁 <b>إنقاذ تحت النار!</b>

سمعت دوّار المروحية يگرب، أشعلت الإشارة الدخانية، وبدأت الرصاصات تنهال من كل جهة... المروحية هبطت بسرعة، والرماة يردّون على العدو، وركضت نحوها بكل ما تبقى من گوة! شدّوك من إيدك للداخل وطلعت المروحية وهي تلتفّ بعنف وفيها ثقوب رصاص.

📻 <b>[رامي الباب]</b>: «معاك! معاك! انطلق!»
🧠 «ما أصدّق... ما أصدّق...»""",
    "civil": """🏘 <b>أهل القرية أنقذوك</b>

العجوز اللي شفته بالأول اقترب وبيده ماي وخبز... وگال: «ما نعرف مين انت، بس انت إنسان وتحتاج مساعدة.» خبّوك ببيت وغطّوك، وبعد كم ساعة وصلت دورية صديقة وأخذتك.

🧠 «أحياناً الخير يجي من أبعد مكان.»""",
}
RES_CAPTURED = """⛓ <b>الوقوع بالأسر</b>

سمعت الأصوات تگرب... رميت سلاحك ورفعت إيدك، ورجّالة العدو طوّگوك من كل جهة، ربطوا يديك وغطّوا عيونك. سمعت واحد يگول بلهجة ثگيلة: «هذا الطيار! تمام، نوديه للقيادة.»

🧠 «أسير... بس أنا حي، وحي يعني فيه أمل.»
📻 <b>القيادة العليا:</b> «طيارنا انأسر... نفاوض لتبادله.»"""
RES_KIA = """🕯 <b>الاستشهاد</b>

سمعت صوت خطوات... وتبادلت إطلاق النار وياهم لين آخر رصاصة. وبعد كم دگيگة ساد الصمت. طيارنا انضم لموكب الشهداء...

🕯 <b>استشهد الطيار {name}</b> مدافعاً عن رفاقه الجنود وعن أرضه... الله يرحمه ويسكنه فسيح جناته."""

DEBRIEF.update({
    "cas_good": """☕ <b>استراحة گصيرة</b>

🧠 <b>[حديث ذاتي]</b>
«أنزل من الطيارة وأطالع الأرض اللي بعدها تحت رجلي... الجنود اللي دعمتهم يتحركون بأمان هسه. أمسح الغبار من وجهي وأتنفس. كل مهمة دعم هي حياة تنحفظ... وكل مرة أفكر شنو لو غلطت؟»

📻 <b>[القيادة «فالكون-5»]</b>
«{rank} {name}، المراقب الأمامي يشكرك وجنودنا بالأرض يبعثون سلامهم. دعمك كان دگيگ ومحسوب. خذ شوية راحة، وراح نحچي عن المؤتمر الصحفي.»

📻 «شكراً سيدي. هذا واجبي.»""",
    "cas_bad": """☕ <b>استراحة گصيرة</b>

🧠 <b>[حديث ذاتي]</b>
«الصمت يغلف الغرفة... ما أگدر أتخلص من الصور اللي ببالي: الدخان، الإنذارات، الأصوات. أطالع بأيدي وأتساءل: كان بإمكاني أسوي شي مختلف؟ تأكيد زايد، ثانية زايدة، گرار ثاني؟ الأسئلة تعضّني مثل الكلاب.»

📻 <b>[القيادة «فالكون-5»]</b>
«{rank} {name}، المهمة خلصت بنتائج ما تسرّ. ستخضع لتحقيق شامل بملابسات اللي صار. اللي صار بالمهمة يتعلم منه الكل، بس لازم نعرف الحقيقة كاملة.»

📻 «مفهوم سيدي. أتحمل مسؤولية گراراتي وراح أتعاون مع اللجنة.»""",
})
COFFEE.update({
    "cas_good": """☕ <b>قهوة بغرفة طيارين الدعم</b>

يدخل المراقب الأمامي «ذئب-2» بنفسه ويمدّلك فنجان: «شكراً يا طيار، أنقذت رجالتي اليوم.» تبتسم بتعب: «هذا شغلي.»

— «أتدري؟ لما شفتك فوق الخندق نزلت الدموع من عيوني.»
— «احنا ما ننسى اللي ينتظرنا تحت.»

وبداخلك: «الجندي اللي يشوف طيارة الدعم فوق راسه يحس إن الدنيا مو ضده... وهذا يستاهل كل التعب.»

🔋 تراجع إرهاقك بعد هالاستراحة.""",
    "cas_bad": """☕ <b>قهوة بغرفة طيارين الدعم</b>

زميل لك قديم يجلس گربك وما يحچي. وبعدين يگول بصوت واطي: «ما كلنا نرجع نفس الشخص بعد هالنوع من المهام.»

— «أحس إن الطيارة هي اللي نجت وأنا اللي خسرت.»
— «لا تحمّل نفسك أكثر من طاقتك. هذا الشغل ما يرحم، بس إنت بشر.»

تطالع بقاع الفنجان وتفكر: «يا ريت أگدر أرجّع بعض اللحظات...»

🔋 تراجع إرهاقك شوية، بس الثگل ما راح.""",
})

RESULT_AR.update({
    "cas_win": "مهمة دعم ناجحة", "cas_fail": "مهمة دعم فاشلة", "cas_civil": "استهداف هدف مدني",
    "cas_friendly": "نيران صديقة", "cas_return": "هبوط اضطراري ناجح",
    "cas_down": "سگوط الطيارة وإنقاذ الطيار", "cas_pow": "سگوط الطيارة ووگوع الطيار بالأسر",
    "cas_kia": "استشهاد الطيار",
})
ADVICE.update({
    "cas_win": "✅ <b>الخلاصة:</b> دعم دگيگ مع تحقق من المراقب الأمامي. استمر على هالنهج وانتبه من المضادات الأرضية وكت المدار.",
    "cas_fail": "⚠️ <b>الخلاصة:</b> ما حققت أهداف مؤثرة. اختار السلاح المناسب لكل هدف، وگرّب بحذر من الهدف إذا السلاح گصير المدى.",
    "cas_civil": "🚨 <b>الخلاصة:</b> بالدعم القريب الهدف المدني يختلط بالعدو بسهولة. اتصل بالـJTAC دايماً گبل الإطلاق.",
    "cas_friendly": "🚨 <b>الخلاصة:</b> نيران صديقة بالدعم القريب مصيبة. تأكد من BFT ومن المراقب الأمامي گبل أي ضربة.",
    "cas_return": "✅ <b>الخلاصة:</b> گرار الرجوع بالطيارة المصابة كان مخاطرة ونجح بفضل متانة الطيارة. المرة الجاية انتبه من المضادات واستخدم الفلير بحكمة.",
    "cas_down": "⚠️ <b>الخلاصة:</b> خسرنا الطيارة بس الطيار نجا. گرار الگفز بالوكت المناسب ينگذ الأرواح، والطيارة تتعوض.",
    "cas_pow": "⚠️ <b>الخلاصة:</b> وگع الطيار بالأسر بأرض العدو. الاختفاء والاتصال المبكر بوحدة الإنقاذ يرفعان فرص النجاة.",
    "cas_kia": "🕯 <b>الخلاصة:</b> استشهد الطيار. تقليل مدة التعرّض للمضادات وتقدير الخطر بدقة هو أهم درس بهالمهام.",
})
TAGS.update({
    "cas_win": "#ملاك_الحماية", "cas_fail": "#مهمة_بلا_نتيجة", "cas_civil": "#ضحايا_الدعم_الجوي",
    "cas_friendly": "#النيران_الصديقة", "cas_return": "#طيارنا_رجع", "cas_down": "#الطيار_المنقَذ",
    "cas_pow": "#الطيار_الأسير", "cas_kia": "#شهيد_الجو",
})
SPLIT.update({
    "cas_win": (7, 3), "cas_fail": (3, 7), "cas_civil": (2, 8), "cas_friendly": (2, 8),
    "cas_return": (6, 4), "cas_down": (5, 5), "cas_pow": (5, 5), "cas_kia": (5, 5),
})

PRESS.update({
    "cas_win": [
        "🎙 <b>المؤتمر الصحفي الرسمي — {spk}</b>\n\n«بسم الله والوطن، إخوتي الصحفيين، هلا بيكم. نعلن اليوم نجاح مهمة دعم أرضي قريب نفّذها {unit} بگيادة {rank} <b>{pilot}</b> على متن {tool}، وأسندت گواتنا الأرضية اللي كانت تحت نيران كثيفة.»",
        "📍 <b>تفاصيل العملية:</b> انرصدت أهداف معادية بـ {loc}، وتم التنسيق بالكامل ويّا المراقب الأمامي (JTAC)، وبعد التأكد من هوية الأهداف، انطلقت الضربات الدگيگة. وصل صافي نقاط المهمة لـ {pts}.",
        "⚖️ <b>الأضرار الجانبية والخسائر:</b> نأكد إنو ما سجلنا أي خسائر بشرية بين المدنيين ولا بصفوفنا. وگيمة الأصول والأرواح اللي انحمت تگدّر بحدود <b>{saved} مليون دولار</b>.",
        "📈 <b>الأثر الاستراتيجي:</b> الإيجابيات: إيقاف تقدم العدو، وكسر معنوياته، ورفع الثقة بالدعم الجوي. أما التحديات: فخطر المضادات الأرضية وصواريخ الكتف بالمنطقة بعده موجود، ولازم نحسب حسابه بالمهام الجاية.",
        "❓ <b>سؤال أحد الصحفيين:</b> هل كان الطيار بخطر؟\n🎙 <b>الجواب:</b> «الدعم القريب دايماً فيه مخاطرة، والطيار تعامل بمهنية عالية وبتنسيق كامل ويّا المراقب.»",
        "«شكراً إلكم، والمؤتمر خلص.»",
    ],
    "cas_fail": [
        "🎙 <b>المؤتمر الصحفي الرسمي — {spk}</b>\n\n«إخوتي، نعلن إن مهمة الدعم الأرضي اللي نفّذها {unit} على متن {tool} ما حققت الأهداف المطلوبة منها، ونتحمل المسؤولية كاملة.»",
        "📍 <b>تفاصيل العملية:</b> انرصدت أهداف بـ {loc}، بس الإصابات ما كانت كافية أو ما تحققت، واستهلكنا ذخيرة ما نتج عنها تدمير فعلي.",
        "⚖️ <b>الأضرار والخسائر:</b> الخسائر المادية التقديرية <b>{money} مليون دولار</b>، ما انسجلت خسائر بشرية بصفوف الطيار، بس القوات الأرضية بقت تحت الضغط.",
        "📈 <b>الأثر الاستراتيجي:</b> السلبيات: تأخر الحسم على الأرض وإرهاق الجنود. الإيجابيات: درس مهم بخصوص اختيار الترسانة وبروتوكولات التنسيق ويّا المراقب الأمامي.",
        "❓ <b>سؤال أحد الصحفيين:</b> شنو سبب الإخفاق؟\n🎙 <b>الجواب:</b> «التحقيق بعده مستمر، وراح نعلن النتائج بشفافية.»",
        "«شكراً إلكم.»",
    ],
    "cas_civil": [
        "🎙 <b>المؤتمر الصحفي العاجل — {spk}</b>\n\n«إخوتي، بگلوب مثگلة بالحزن، نعلن إن ضربة دعم أرضي نفّذها {unit} على متن {tool} أصابت هدفاً تبيّن إنه مدني. هذا خطأ جسيم نتحمل مسؤوليته كاملة، ونتقدم بأحرّ التعازي لذوي الضحايا.»",
        "📍 <b>تفاصيل الحادثة:</b> كان الهدف ({target}) <b>{truth}</b>، بـ {loc}. صار الاشتباك بدون اكتمال إجراءات التحقق ويّا المراقب الأمامي، حسب النتائج الأولية.",
        "⚖️ <b>الخسائر:</b> بلغت الخسائر البشرية <b>{cas}</b> ضحية مدنية، والخسائر المادية <b>{money} مليون دولار</b>. وشُكّلت لجنة تحقيق عاجلة، وانتخذت إجراءات تأديبية بحق المسؤولين.",
        "📈 <b>الأثر الاستراتيجي:</b> السلبيات: ضرر خطير بسمعتنا وثگة الناس بالعمليات، ومساءلات گانونية. الإيجابيات: مراجعة جذرية لقواعد الاشتباك بالدعم القريب.",
        "❓ <b>سؤال أحد الصحفيين:</b> الغلط بشري لو تگني؟\n🎙 <b>الجواب:</b> «النتائج الأولية تشير لغلط بشري باتخاذ الگرار. ما نحمّل التگنية شي ما إلها ذنب بيه.»",
        "«نعدكم بنشر نتائج التحقيق كاملة. وشكراً.»",
    ],
    "cas_friendly": [
        "🎙 <b>المؤتمر الصحفي العاجل — {spk}</b>\n\n«إخوتي، نعلن بكل أسف عن حادثة نيران صديقة صارت اليوم أثناء مهمة دعم أرضي نفّذها {unit} على متن {tool}. ننحني إجلالاً لأرواح اللي فگدناهم.»",
        "📍 <b>تفاصيل الحادثة:</b> كان الهدف ({target}) <b>{truth}</b>، بـ {loc}. صارت الضربة بدون تأكيد الهوية عبر المراقب الأمامي ومنظومة تتبع القوات الصديقة (BFT).",
        "⚖️ <b>الخسائر:</b> الخسائر البشرية <b>{cas}</b>، والخسائر المادية <b>{money} مليون دولار</b>. وباشرت لجنة التحقيق شغلها وراح يتحاسب المقصّرين.",
        "📈 <b>الأثر الاستراتيجي:</b> السلبيات: ضرب لثگة الجنود بالدعم الجوي واهتزاز المعنويات. الإيجابيات: فرصة لمراجعة تنسيق الاتصالات ونظام التعريف.",
        "❓ <b>سؤال أحد الصحفيين:</b> شلون ما انعرفت القوة الصديقة؟\n🎙 <b>الجواب:</b> «هذا بالضبط اللي تتحقق بيه اللجنة.»",
        "«الرحمة لشهدائنا. شكراً إلكم.»",
    ],
    "cas_return": [
        "🎙 <b>المؤتمر الصحفي الرسمي — {spk}</b>\n\n«إخوتي، نعلن عن حادثة انصابت بيها طيارة الدعم الأرضي ({tool}) أثناء المهمة بنيران أرضية، وتمكن الطيار {rank} <b>{pilot}</b> من العودة بها للقاعدة بهبوط اضطراري ناجح. بطولة تستحق التقدير.»",
        "📍 <b>تفاصيل الحادثة:</b> انصابت الطيارة بـ {loc} بسلاح مضاد (صاروخ كتف أو رشاش ثقيل)، وانخفض ضغط الهيدروليك واشتعل المحرك، بس الطيار حافظ على هدوئه وقاد الطيارة للقاعدة.{extra}",
        "⚖️ <b>الأضرار والخسائر:</b> الخسائر البشرية: لا شي. الخسائر المادية التقديرية للطيارة المتضررة: <b>{money} مليون دولار</b> للإصلاح.",
        "📈 <b>الأثر الاستراتيجي:</b> الإيجابيات: نجاة الطيار والطيارة وكسب معلومات مهمة عن تهديدات المضادات بالمنطقة. السلبيات: توقف الطيارة عن الخدمة لمدة، وتأكيد وجود خطر دائم بمنطقة العمليات.",
        "❓ <b>سؤال أحد الصحفيين:</b> ليش ما گفز الطيار؟\n🎙 <b>الجواب:</b> «گدّر الموقف وتصرف بمهنية، وكانت مخاطرة محسوبة. وقراره ما يُقاس عليه دايماً.»",
        "«شكراً إلكم. نتمنى للطيار الراحة.»",
    ],
    "cas_down": [
        "🎙 <b>المؤتمر الصحفي العاجل — {spk}</b>\n\n«إخوتي، نعلن عن سگوط طيارة الدعم الأرضي ({tool}) أثناء مهمة لإسناد گواتنا، بعد إصابتها بنيران أرضية. ونعلن بحمد الله إن الطيار {rank} <b>{pilot}</b> نجا وتمت استعادته.»",
        "📍 <b>تفاصيل الحادثة:</b> انصابت الطيارة بـ {loc}، وبعد فشل السيطرة على الأضرار انتهى الأمر بخسارتها.{extra}",
        "⚖️ <b>الأضرار والخسائر:</b> الخسائر البشرية: لا شي. الخسائر المادية: <b>{money} مليون دولار</b> بسبب خسارة الطيارة.",
        "📈 <b>الأثر الاستراتيجي:</b> السلبيات: خسارة طيارة مهمة ودعاية معادية متوقعة. الإيجابيات: نجاة الطيار، ومراجعة تكتيكات الطيران الواطي والمضادات الأرضية.",
        "❓ <b>سؤال أحد الصحفيين:</b> هل كان بالإمكان تجنب السگوط؟\n🎙 <b>الجواب:</b> «راح نراجع كل التفاصيل بالتحقيق، بما فيها تقديرات الخطر والغطاء الجوي.»",
        "«شكراً إلكم.»",
    ],
    "cas_pow": [
        "🎙 <b>المؤتمر الصحفي العاجل — {spk}</b>\n\n«إخوتي، نعلن بكل أسف عن سگوط طيارة الدعم الأرضي ({tool}) أثناء مهمتها، ووگوع الطيار {rank} <b>{pilot}</b> بالأسر بعد قفزه بالمظلة بأرض العدو. نؤكد أن الجهود مستمرة لتحريره.»",
        "📍 <b>تفاصيل الحادثة:</b> انصابت الطيارة بـ {loc} بنيران أرضية، وگفز الطيار، وبعد محاولات اختباء واتصال بوحدة الإنقاذ تم أسره.{extra}",
        "⚖️ <b>الأضرار والخسائر:</b> الخسائر المادية: <b>{money} مليون دولار</b>، والخسائر البشرية: طيار أسير.",
        "📈 <b>الأثر الاستراتيجي:</b> السلبيات: ورقة ضغط بيد العدو، ومعنويات الجنود تتأثر. الإيجابيات: تحرّك سياسي وعسكري لتأمين عودته.",
        "❓ <b>سؤال أحد الصحفيين:</b> اكو مفاوضات؟\n🎙 <b>الجواب:</b> «نتواصل عبر القنوات المتاحة، وما نعلّق على التفاصيل حتى نحافظ على سلامة الطيار.»",
        "«حفظ الله الطيار وأعاده سالماً.»",
    ],
    "cas_kia": [
        "🎙 <b>المؤتمر الصحفي — {spk}</b>\n\n«إخوتي، بگلوب مؤمنة بقضاء الله وقدره، ننعى إلكم الطيار {rank} <b>{pilot}</b> اللي استشهد بمهمة دعم أرضي أداها على متن {tool}. ضحّى بروحه وهو يدافع عن رفاقه الجنود.»",
        "📍 <b>تفاصيل الحادثة:</b> انصابت الطيارة بـ {loc} بنيران أرضية معادية، وبعد محاولة أخيرة للسيطرة واجه الطيار مصيره.{extra}",
        "⚖️ <b>الأضرار والخسائر:</b> الخسائر البشرية: <b>{cas}</b> شهيد. الخسائر المادية: <b>{money} مليون دولار</b>.",
        "📈 <b>الأثر الاستراتيجي:</b> السلبيات: خسارة طيار كفوء وطيارة مهمة. الإيجابيات: دماء الشهيد تذكّر الجميع بثمن الدفاع عن الوطن، وتدفع لمراجعة شاملة لتكتيكات الدعم القريب.",
        "❓ <b>سؤال أحد الصحفيين:</b> هل راح تتحاسب القيادة؟\n🎙 <b>الجواب:</b> «لجنة التحقيق مستمرة، ومن يثبت تقصيره يتحمل مسؤوليته مهما كان موقعه.»",
        "«رحم الله شهيدنا وأسكنه فسيح جناته. شكراً إلكم.»",
    ],
})

PRO_CAS = [
    "عاشت إيدك يا طيار! الجنود بالأرض يبوسون إيدك 🙏",
    "هذا الدعم اللي ننتظره، الله يحميكم يا صقور 🦅",
    "اللي بالأرض يعرف شنو يعني طيار الدعم يظهر بالوكت المناسب",
    "طيارة الدعم ملاك الحماية للمشاة 🛡",
    "ضربة دگيگة وبدون خسائر مدنية، عاش الجيش!",
    "ألف تحية للمراقب الأمامي والطيار، تنسيق ممتاز.",
    "هذا اللي يفرّح گلب الجندي: يشوف السما وياه.",
    "الله يحفظ كل طيار يطير واطي حتى يحمي رفاقه 🤲",
    "فخورين بيكم، كفو والله!",
]
PRO_PILOT_SAFE = [
    "الله يحفظ الطيار ويسلّمه 🤲",
    "الطيارة تتعوض، الطيار ما يتعوض!",
    "بطل ضحّى حتى يحمي رفاقه، الحمد لله على سلامته.",
    "حسرة على الطيارة بس الحمد لله نجا الطيار 🙏",
    "الله يخليك لأهلك ولبلدك يا بطل.",
    "طيار أسطوري، رجع بطيارته وهي تحترق!",
    "هذا هو الاحتراف، تحية من الگلب.",
    "اللي ينگذ نفسه وينگذ طيارته هذا شاطر، عاش!",
]
PRO_PILOT_LOST = [
    "الله يرحمه ويسكنه فسيح جناته 🕯",
    "بطل ضحّى بنفسه حتى يحمي رفاقه، ما راح ننساه.",
    "اللهم ثبّته وأرجعه لأهله سالماً 🤲",
    "الشهداء أحياء عند ربهم يرزگون.",
    "الطيارة ماكو إلها گيمة قدام الطيار، الله يفرجها.",
    "خسارة كبيرة، رجّال ما ينعوّض.",
    "نصلي من أجل عودته سالماً لأمه وأهله.",
    "الله يصبّر أهله ويلهمهم السلوان.",
]
CRIT_LEAD = [
    "ليش ماكو غطاء جوي؟ ليش ما انضربت المضادات الأرضية گبل ما يطلع الطيار؟",
    "القيادة تضحّي بالطيارين وهي گاعدة بالمكيّف!",
    "مو أول مرة تنكشف هالثغرة وما أحد يتحاسب.",
    "طيارة بملايين تنسگط بصاروخ كتف! وين الاستطلاع والاستخبارات؟",
    "كل مرة نفس الأسباب ونفس التبريرات، وبعدين لجنة تحقيقية وتنطمر.",
    "نريد نعرف منو اللي أمر الطيار يطير واطي بمنطقة ملغّمة بالمضادات!",
    "الرواية الرسمية ناقصة، أين الحقيقة؟",
    "اللي يتحمل المسؤولية الحقيقية ماكو عليه حساب.",
    "نريد تحقيق مستقل مو لجنة من داخل المؤسسة نفسها.",
    "دفعنا الثمن مرة ومرتين وبعدنا ما اتعلمنا.",
]


# ============================================================
# نظام ما بعد المهمة: النتيجة ← استراحة ← مؤتمر صحفي ← سوشيال ← غرفة الاستراحة
# ============================================================
def rand_loc():
    sector = random.choice(["ألفا", "برافو", "تشارلي", "دلتا", "إيكو", "فوكستروت"])
    return f"القطاع {sector} ({random.randint(10, 59)}°N, {random.randint(20, 59)}°E)"


def set_ctx(uid, **kw):
    """يحفظ ملخص المهمة اللي خلصت لاستعماله بالحوارات والمؤتمر الصحفي."""
    kw.setdefault("loc", rand_loc())
    kw.setdefault("spk", random.choice(SPOKESMEN))
    kw.setdefault("cas", 0)
    kw.setdefault("money", 0)
    kw.setdefault("pts", 0)
    kw.setdefault("saved", random.randint(60, 400))
    kw.setdefault("analyzed", False)
    kw["rested"] = False
    kw["counted"] = False
    last_ctx[uid] = kw


def post_kb():
    return mk([[("☕ استراحة گصيرة", "pm:debrief")]])


def ctx_fields(ctx, p):
    return {
        "unit": {"radar": "منظومات الدفاع الجوي", "cas": "طيران الدعم الأرضي"}.get(ctx["branch"], "سلاح الجو"),
        "target": ctx.get("target", "الهدف"), "t": ctx.get("target", "الهدف"),
        "truth": ctx.get("truth_ar", "—"), "tool": ctx.get("tool", "—"),
        "loc": ctx["loc"], "cas": ctx["cas"], "money": ctx["money"],
        "saved": ctx["saved"], "extra": ctx.get("extra", ""), "pts": ctx["pts"],
        "spk": ctx["spk"], "pilot": html.escape(p["name"]), "name": html.escape(p["name"]),
        "rank": rank_name(p), "points": p["points"], "kills": p["kills"],
        "civil_inc": p["civil_inc"], "friendly_inc": p["friendly_inc"],
        "missions": p["missions"],
    }


def rest_menu(call, uid, note=""):
    p = players[uid]
    forced = p.get("fatigue", 0) >= 3
    text = REST_ROOM.format(bar=fatigue_bar(p))
    if forced:
        text += ("\n\n⛔ <b>استراحة إجبارية:</b> المهام المتتالية أنهكتك، وما تگدر تبدي مهمة "
                 "جديدة گبل ما ترتاح شوية.")
    if note:
        text += "\n\n" + note
    rows = []
    if not forced:
        rows.append([("▶️ كمّل على المهمة الجاية فوراً", "pm:next")])
    rows += [[("☕ اشرب قهوة بغرفة العمليات", "pm:coffee")],
             [("📝 التقرير الشخصي", "pm:report")],
             [("🔎 مراجعة الأخطاء", "pm:review")],
             HOME_ROW]
    edit(call, text, mk(rows))


def post_router(call, uid, parts):
    p = players[uid]
    ctx = last_ctx.get(uid)
    if not ctx:
        return edit(call, "ماكو مهمة حديثة. ارجع لغرفة العمليات.", mk([HOME_ROW]))
    act = parts[1]
    f = ctx_fields(ctx, p)
    tone = "good" if ctx["result"] in ("win", "safe", "cas_win", "cas_return") else "bad"
    key = f"{ctx['branch']}_{tone}"

    if act == "debrief":
        if not ctx["counted"]:
            ctx["counted"] = True
            p["fatigue"] = min(3, p.get("fatigue", 0) + 1)
            save_data()
        edit(call, DEBRIEF[key].format(**f), mk([[("🎙 المؤتمر الصحفي", "pm:press")]]))
    elif act == "press":
        edit(call, build_press(ctx["result"], f), mk([[("📱 ردود الفعل بالمنصات", "pm:social")]]))
    elif act == "social":
        edit(call, build_social(ctx["result"], f), mk([[("🛋 غرفة الاستراحة والتقييم", "pm:rest")]]))
    elif act == "rest":
        rest_menu(call, uid)
    elif act in ("coffee", "report", "review"):
        if act == "coffee":
            text, relief = COFFEE[key].format(**f), 2
        elif act == "report":
            text, relief = (REPORT[tone] + REPORT_STATS).format(**f), 1
        else:
            text, relief = build_review(ctx, f), 1
        p["fatigue"] = max(0, p.get("fatigue", 0) - relief)
        ctx["rested"] = True
        save_data()
        edit(call, text, mk([[("↩️ ارجع لغرفة الاستراحة", "pm:rest")]]))
    elif act == "next":
        if p.get("fatigue", 0) >= 3:
            return rest_menu(call, uid, "⚠️ ما تگدر تكمل گبل الاستراحة.")
        if ctx["branch"] == "radar":
            radar_scan(call, uid)
        elif ctx["branch"] == "cas":
            edit(call, "🛡 <b>اختار طيارة الدعم الأرضي:</b>", cas_ac_kb())
        else:
            edit(call, "✈️ <b>اختار طيارتك المقاتلة:</b>", ac_list_kb())


# ============================================================
# موجّه الأزرار
# ============================================================
@bot.callback_query_handler(func=lambda c: True)
def on_cb(call):
    uid, d = call.from_user.id, call.data
    st = register_user(call.from_user)
    if st != "allowed":
        bot.answer_callback_query(call.id, deny_text(st), show_alert=True)
        return
    p = players.get(uid)
    if not p:
        bot.answer_callback_query(call.id, "ابعث /start أول شي حتى تسجّل هويتك.", show_alert=True)
        return
    if d == "cls:navy":
        bot.answer_callback_query(
            call.id, "⚓ هذا الصنف قيد التطوير — العمليات البحرية راح تشتغل گريب إن شاء الله",
            show_alert=True)
        return
    left = locked_left(p)
    if left > 0 and d not in ("home", "profile") and (not d.startswith("pm:") or d == "pm:next"):
        bot.answer_callback_query(
            call.id, f"⛔ انت موقوف: {p.get('lock_reason', '')}\nباقي: {fmt_time(left)}",
            show_alert=True)
        return
    s = sessions.get(uid)
    tracking = bool(s and s.get("trk") and s["trk"]["active"])
    if tracking and not d.startswith("trk:"):
        bot.answer_callback_query(call.id, "⏳ التتبع شغّال، انتظر النتيجة.")
        return
    bot.answer_callback_query(call.id)
    parts = d.split(":")
    k = parts[0]

    try:
        if k == "trk":
            on_trk(call, uid, parts)
        elif d == "home":
            with ulock(uid):
                sessions.pop(uid, None)
            edit(call, home_text(p), home_kb())
        elif d == "profile":
            edit(call, profile_text(p), mk([HOME_ROW]))
        elif d == "cls:menu":
            with ulock(uid):
                sessions.pop(uid, None)
            edit(call, "🎛 <b>إدارة ظهور الأصناف</b>\nاختار الصنف اللي تريد تخدم بيه:", classes_kb())
        elif d == "cls:radar":
            edit(call, RADAR_INTRO, mk([[("📡 ابدأ المسح الراداري", "r:scan")], [("⬅️ رجوع", "cls:menu")]]))
        elif d == "cls:pilot":
            edit(call, PILOT_INTRO.format(name=html.escape(p["name"])) +
                 "\n\n🛡 وإذا تريد تسند القوات الأرضية، اختار «طيران الدعم الأرضي».",
                 mk([[("✈️ إظهار طيارتك", "p:show")],
                     [("🛡 طيران الدعم الأرضي / هجوم أرضي", "cas:intro")],
                     [("⬅️ رجوع", "cls:menu")]]))

        # ---------------- الدعم الأرضي ----------------
        elif k == "cas":
            cas_router(call, uid, parts)

        # ---------------- ما بعد المهمة ----------------
        elif k == "pm":
            post_router(call, uid, parts)

        # ---------------- الرادار ----------------
        elif k == "r":
            act = parts[1]
            if act == "scan":
                radar_scan(call, uid)
            elif act == "an":
                radar_analyze(call, uid)
            elif act == "eng":
                radar_choose_battery(call, uid)
            elif act == "bat":
                radar_choose_missile(call, uid, int(parts[2]))
            elif act == "mis":
                radar_ready(call, uid, parts[2])
            elif act == "go":
                radar_go(call, uid)
            elif act == "obs":
                radar_observe(call, uid)
            elif act == "back":
                s = sessions.get(uid)
                if s and s["mode"] == "radar":
                    edit(call, radar_text(s["t"]), radar_kb(s["t"]))

        # ---------------- الطيار ----------------
        elif k == "p":
            act = parts[1]
            if act == "show":
                edit(call, "✈️ <b>اختار طيارتك المقاتلة:</b>", ac_list_kb())
            elif act in ("ac", "acr"):
                i = random.randrange(len(AIRCRAFT)) if act == "acr" else int(parts[2])
                sessions[uid] = {"mode": "pilot", "ac": i, "a2a": set(), "a2g": set(),
                                 "hp": 100, "enc": 0, "score": 0, "kills": 0, "trk": None}
                edit(call, ac_card(i) + "\n\nتأكد اختيار هالطيارة؟",
                     mk([[("✅ تأكيد", "p:al")], [("🔄 غيّر", "p:show")]]))
            elif act in ("al", "gl"):
                s = sessions.get(uid)
                if s and s["mode"] == "pilot":
                    arsenal_screen(call, uid, "a" if act == "al" else "g")
            elif act == "t":
                s = sessions.get(uid)
                if not s or s["mode"] != "pilot":
                    return
                key = "a2a" if parts[2] == "a" else "a2g"
                i = int(parts[3])
                cap = AIRCRAFT[s["ac"]][1]
                if i in s[key]:
                    s[key].discard(i)
                elif len(s["a2a"]) + len(s["a2g"]) >= cap:
                    bot.send_message(call.message.chat.id, "⚠️ ماكو خانات تسليح متاحة!")
                else:
                    s[key].add(i)
                arsenal_screen(call, uid, parts[2])
            elif act == "lock":
                s = sessions.get(uid)
                if not s or not (s["a2a"] or s["a2g"]):
                    bot.send_message(call.message.chat.id, "اختار سلاح واحد على الأقل.")
                    return
                s["ammo_a"] = {i: ROUNDS_PER_SLOT for i in s["a2a"]}
                s["ammo_g"] = {i: ROUNDS_PER_SLOT for i in s["a2g"]}
                names = [A2A[i][0] for i in sorted(s["a2a"])] + [A2G[i][0] for i in sorted(s["a2g"])]
                edit(call, "🔒 <b>انثبتت الترسانة:</b>\n• " + "\n• ".join(names) +
                     "\n\nالطيارة جاهزة للإقلاع.",
                     mk([[("🚀 الانطلاق للمهمة", "p:launch")]]))
            elif act == "launch":
                s = sessions.get(uid)
                if s and s["mode"] == "pilot":
                    edit(call, random.choice(PILOT_PRE).format(
                        name=html.escape(p["name"]), rank=rank_name(p), ac=AIRCRAFT[s["ac"]][0]))
                    pilot_next(call, uid, "🛫 دخلت مجال العمليات...", fresh=True)
            elif act == "nx":
                s = sessions.get(uid)
                if s and s["mode"] == "pilot":
                    pilot_next(call, uid, "")
            elif act == "fire":
                pilot_fire_menu(call, uid)
            elif act == "w":
                pilot_fire_go(call, uid, parts[2], int(parts[3]))
            elif act == "back":
                s = sessions.get(uid)
                if s and s["mode"] == "pilot":
                    edit(call, contact_text(s), contact_kb(s["contact"]))
            elif act == "man":
                s = sessions[uid]
                c = s["contact"]
                c["pat"] += 1
                s["man"] = s.get("man", 0) + 1
                s["g"] = round(random.uniform(5.5, 8.5), 1)
                note = (f"🧭 ناورت بقوة {s['g']}G وحافظت على موقعك التكتيكي.\n"
                        f"{random.choice(PILOT_G)}\n")
                if random.random() < 0.9:
                    c["identified"] = True
                    c["label"] = {"hostile": "معادي مؤكد", "friendly": "صديق مؤكد",
                                  "civil": "مدني مؤكد"}[c["truth"]]
                    note += f"🔍 انعرف الهدف: <b>{c['name']}</b>.\n"
                    if c["cls"] == "airliner":
                        note += f"👥 الركاب: {c['pax']}\n"
                else:
                    note += "🔍 التعرّف مو حاسم.\n"
                if c["truth"] == "hostile":
                    ch = {"fighter": .35, "bomber": .15, "uav": .1, "sam": .4, "armor": .15, "bunker": .1}[c["cls"]]
                    dmg = pilot_damage(s, ch)
                    if dmg:
                        note += f"⚠️ العدو طلق عليك وانصابت الطيارة (-{dmg}%)."
                if s["hp"] <= 0:
                    return pilot_end(call, uid, crashed=True)
                edit(call, contact_text(s, note), contact_kb(c))
            elif act == "skip":
                s = sessions[uid]
                c = s["contact"]
                if c["truth"] == "hostile":
                    s["score"] -= 20
                    ch = 0.25 if c["domain"] == "air" else 0.15
                    dmg = pilot_damage(s, ch)
                    note = "↪️ تعدّيت هدف معادي (-20 نقطة)." + (f"\n⚠️ تعرّضت لنيران (-{dmg}%)." if dmg else "")
                else:
                    pts = 10 if c["identified"] else 0
                    s["score"] += pts
                    note = f"↪️ تعدّيت الهدف بأمان. +{pts} نقطة."
                pilot_next(call, uid, note + "\n")
    except KeyError:
        edit(call, "⚠️ الجلسة خلصت. ارجع لغرفة العمليات.", mk([HOME_ROW]))
    except Exception:
        traceback.print_exc()


@bot.message_handler(func=lambda m: True, content_types=["text"])
def fallback(m):
    if not gate_msg(m):
        return
    if m.from_user.id not in players:
        bot.send_message(m.chat.id, "ابعث /start حتى تسجّل هويتك العسكرية.")
    else:
        bot.send_message(m.chat.id, "استعمل الأزرار للتنقل، أو ابعث /start أو /profile.")


if __name__ == "__main__":
    load_data()
    load_access()
    start_admin_bot()
    print("Project Falcon V2.5 is running...")
    bot.infinity_polling(skip_pending=True)
