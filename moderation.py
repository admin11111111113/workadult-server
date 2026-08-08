# -*- coding: utf-8 -*-
"""
Модерация объявлений через Telegram: единая очередь заявок (студии/вакансии/
резюме) с подачей через сайт (Telegram Login Widget) и одобрением кнопками
в Telegram у админа.

Модель оплаты студий — «база + буст»:
  • Подача студии стоит ФИКСИРОВАННУЮ submit_price (см. тарифы в админке) —
    разовая оплата, после подтверждения объявление публикуется НАВСЕГДА как
    обычное (без цвета/приоритета).
  • Поверх — необязательный ПОМЕСЯЧНЫЙ буст (бронза/серебро/золото/на
    главной), поднимающий цвет и место в списке города (либо прямо на
    главную сайта). Покупается ОТДЕЛЬНО, в любой момент — сразу после
    публикации (бот сам предлагает) или позже командой /boost. Не продлил —
    буст тихо истекает (см. app.py:_effective_boost), сама публикация
    остаётся, это не связано.
Вакансии/резюме — бесплатно, публикуются сразу по «Одобрить».

Оплата — USDT TRC-20 на ТОТ ЖЕ кошелёк, что у VideoRils (USDT_WALLET),
проверка — ТЕМ ЖЕ механизмом: он-чейн сверка через TronGrid по контракту
USDT-TRC20, допуск ±3 USDT, идемпотентность по txid.

Один бот на всё (WA_BOT_TOKEN) — тот же, что в Telegram Login Widget на
сайте. Пишет и подателю (оплата, буст, статус), и админу (заявки на
модерацию) в его личный чат WA_ADMIN_CHAT_ID. Один бот, а не два, потому
что слать сообщения можно только тому, кто хоть раз открывал чат именно
с этим ботом — а Login Widget это обеспечивает только для одного бота.
Раз бот общий, действия админа (appr/edit/rej) ПРОВЕРЯЮТСЯ по chat_id —
иначе любой пользователь того же бота мог бы подделать callback_data и
одобрить/отклонить чужую заявку.

Упрощение (осознанное, не баг): у пользователя в один момент времени только
ОДИН активный платёж (submit ИЛИ boost) — новый вызов тарифных кнопок
перезаписывает предыдущий незавершённый. Для MVP этого достаточно.
"""
import os
import time
from datetime import datetime

import requests
from firebase_admin import db
from flask import request, jsonify

WA_BOT_TOKEN = os.environ.get("WA_BOT_TOKEN", "").strip()
WA_ADMIN_CHAT_ID = os.environ.get("WA_ADMIN_CHAT_ID", "").strip()
# Секрет для проверки заголовка X-Telegram-Bot-Api-Secret-Token — без него
# кто угодно мог бы POST-нуть на /tg/webhook поддельное «одобрение».
WA_WEBHOOK_SECRET = os.environ.get("WA_WEBHOOK_SECRET", "").strip()


def _admin_chat_id():
    try:
        return int(WA_ADMIN_CHAT_ID)
    except (TypeError, ValueError):
        return None

USDT_WALLET = os.environ.get("USDT_WALLET", "").strip()
TRONGRID_API_KEY = os.environ.get("TRONGRID_API_KEY", "").strip()
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"   # USDT TRC-20 (mainnet) — тот же, что у VideoRils
TOLERANCE_UNDER = 3.0       # тот же допуск, что у VideoRils (комиссии/округления сети)
RESERVE_TTL = 1800          # резерв суммы на 30 минут

_SUBMISSIONS_REF = "/workadult_submissions"
_PENDING_PAY_REF = "/workadult_pending_payment"    # tg_user_id -> текущий незавершённый платёж
_BOOST_CTX_REF = "/workadult_boost_ctx"            # tg_user_id -> какое место сейчас бустит (до выбора тарифа)
_PROCESSED_TX_REF = "/workadult_processed_tx"
_RESUMES_REF = "/workadult_resumes"
_ADMIN_STATE_REF = "/workadult_admin_state"        # какую заявку сейчас редактирует чат админа

BOOST_ORDER = ("bronze", "silver", "gold", "home")   # порядок кнопок, дешёвый → дорогой
BOOST_BUTTON_LABEL = {"bronze": "🥉 Бронза", "silver": "🥈 Серебро",
                       "gold": "🥇 Золото", "home": "🏠 На главной"}
KIND_LABEL = {"studio": "🏢 Студия", "vacancy": "💼 Вакансия", "resume": "🔎 Резюме"}

_deps = {}   # заполняется init_app() — переиспользуем логику тарифов/слотов из app.py


def init_app(app, get_pricing, listings_ref, vacancies_ref, slot_key, slot_count):
    _deps.update(get_pricing=get_pricing, listings_ref=listings_ref,
                 vacancies_ref=vacancies_ref, slot_key=slot_key, slot_count=slot_count)
    app.add_url_rule("/api/submit-studio", "submit_studio", _submit_studio, methods=["POST"])
    app.add_url_rule("/api/submit-vacancy", "submit_vacancy", _submit_vacancy, methods=["POST"])
    app.add_url_rule("/api/submit-resume", "submit_resume", _submit_resume, methods=["POST"])
    app.add_url_rule("/tg/webhook", "tg_webhook", _webhook, methods=["POST"])
    app.add_url_rule("/tg/cleanup-test-submissions", "tg_cleanup_test", _cleanup_test_submissions, methods=["POST"])


def _cleanup_test_submissions():
    """ВРЕМЕННО: удаляет тестовые заявки (E2E/Test/Spoof в названии) из очереди
    модерации. Убрать после использования."""
    if not WA_WEBHOOK_SECRET or request.args.get("key") != WA_WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "forbidden"}), 403
    raw = db.reference(_SUBMISSIONS_REF).get() or {}
    needles = ("e2e", "test", "spoof")
    deleted = []
    for sid, sub in raw.items():
        if not isinstance(sub, dict):
            continue
        f = sub.get("fields") or {}
        blob = " ".join(str(f.get(k, "")) for k in ("name", "org", "title")).lower()
        if any(n in blob for n in needles):
            db.reference(f"{_SUBMISSIONS_REF}/{sid}").delete()
            deleted.append(blob.strip())
    return jsonify({"ok": True, "deleted": deleted})


# ─────────────────────────── Telegram Bot API helpers ──────────────
def _tg(token, method, payload):
    if not token:
        return {}
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=10)
        return r.json() or {}
    except Exception as e:
        print(f"[tg] {method} failed: {e}")
        return {}


def _send(token, chat_id, text, buttons=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    return _tg(token, "sendMessage", payload)


def _edit(token, chat_id, message_id, text, buttons=None):
    if not message_id:
        return {}
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text,
               "parse_mode": "HTML", "reply_markup": {"inline_keyboard": buttons or []}}
    return _tg(token, "editMessageText", payload)


def _answer_cb(token, cb_id, text=""):
    return _tg(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": text})


def _check_secret():
    if not WA_WEBHOOK_SECRET:
        return True   # секрет не настроен — не блокируем (но лучше настроить)
    return request.headers.get("X-Telegram-Bot-Api-Secret-Token") == WA_WEBHOOK_SECRET


# ─────────────────────────── форматирование заявки ──────────────
def _fmt_fields(sub_type, f):
    if sub_type == "studio":
        return (f"🏢 <b>{f.get('name','')}</b>\n📍 {f.get('city','')}"
                + (f" · 💰 {f.get('percent')}" if f.get('percent') else "")
                + f"\n{f.get('desc','')}\n📞 {f.get('contact','')}"
                + (f" · 📱 {f.get('phone')}" if f.get('phone') else "")
                + (f"\n🌐 {f.get('url')}" if f.get('url') else "")
                + (f"\n🖼 {f.get('photo')}" if f.get('photo') else ""))
    if sub_type == "vacancy":
        return (f"💼 <b>{f.get('title','')}</b>\n🏢 {f.get('org','')}"
                + (f"\n💰 {f.get('salary')}" if f.get('salary') else "")
                + (f"\n{f.get('desc')}" if f.get('desc') else "")
                + f"\n📞 {f.get('contact','')}")
    if sub_type == "resume":
        return f"🔎 {f.get('experience','')[:300]}\n💰 {f.get('salary','')}\n📞 {f.get('contact','')}"
    return str(f)


def _who(sub):
    name = (sub.get("tg_name") or "").strip() or "без имени"
    un = sub.get("tg_username")
    return name + (f" (@{un})" if un else "")


# ─────────────────────────── приём заявок с сайта ──────────────
def _new_submission(sub_type, fields, auth):
    tg_id = auth.get("id")
    if not tg_id:
        return None, "auth"
    sub = {
        "type": sub_type, "status": "pending", "fields": fields,
        "tg_user_id": tg_id, "tg_username": auth.get("username", ""),
        "tg_name": (str(auth.get("first_name", "")) + " " + str(auth.get("last_name", ""))).strip(),
        "created_at": datetime.now().isoformat(),
    }
    ref = db.reference(_SUBMISSIONS_REF).push(sub)
    sub_id = ref.key
    text = f"🆕 Новая заявка · {KIND_LABEL[sub_type]}\n\n{_fmt_fields(sub_type, fields)}\n\n👤 {_who(sub)}"
    buttons = [
        [{"text": "✅ Одобрить", "callback_data": f"appr:{sub_id}"},
         {"text": "✏️ Редактировать", "callback_data": f"edit:{sub_id}"}],
        [{"text": "❌ Отклонить", "callback_data": f"rej:{sub_id}"}],
    ]
    res = _send(WA_BOT_TOKEN, WA_ADMIN_CHAT_ID, text, buttons)
    msg_id = (res.get("result") or {}).get("message_id")
    if msg_id:
        ref.child("admin_chat_msg_id").set(msg_id)
    return sub_id, None


def _submit_studio():
    data = request.get_json(force=True, silent=True) or {}
    auth = data.get("auth") or {}
    form = data.get("form") or {}
    fields = {
        "name": (form.get("studio") or form.get("name") or "").strip()[:120],
        "city": (form.get("city") or "").strip()[:80],
        "desc": (form.get("terms") or form.get("desc") or "").strip()[:600],
        "contact": (form.get("contact") or "").strip()[:200],
        "phone": (form.get("phone") or "").strip()[:40],
        "url": (form.get("url") or "").strip()[:300],
        "photo": (form.get("logo") or form.get("photo") or "").strip()[:500],   # логотип — 800×500
        "percent": (form.get("percent") or "").strip()[:60],
        "social_telegram": (form.get("social_telegram") or "").strip()[:120],
        "social_instagram": (form.get("social_instagram") or "").strip()[:120],
        "social_vk": (form.get("social_vk") or "").strip()[:120],
    }
    if not fields["name"] or not fields["city"] or not fields["contact"]:
        return jsonify({"ok": False, "error": "fields"}), 400
    sub_id, err = _new_submission("studio", fields, auth)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True})


def _submit_vacancy():
    data = request.get_json(force=True, silent=True) or {}
    auth = data.get("auth") or {}
    form = data.get("form") or {}
    fields = {
        "org": (form.get("studio") or form.get("org") or "").strip()[:120],
        "title": (form.get("role") or form.get("title") or "").strip()[:120],
        "salary": (form.get("terms") or form.get("salary") or "").strip()[:120],
        "desc": (form.get("city") or form.get("desc") or "").strip()[:600],
        "contact": (form.get("contact") or "").strip()[:200],
    }
    if not fields["title"] or not fields["contact"]:
        return jsonify({"ok": False, "error": "fields"}), 400
    sub_id, err = _new_submission("vacancy", fields, auth)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True})


def _submit_resume():
    data = request.get_json(force=True, silent=True) or {}
    auth = data.get("auth") or {}
    form = data.get("form") or {}
    fields = {
        "experience": (form.get("experience") or "").strip()[:500],
        "salary": (form.get("salary") or "").strip()[:60],
        "contact": (form.get("contact") or "").strip()[:200],
    }
    if not fields["experience"] or not fields["contact"]:
        return jsonify({"ok": False, "error": "fields"}), 400
    sub_id, err = _new_submission("resume", fields, auth)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True})


# ─────────────────────────── единый вебхук (один бот) ──────────────
def _webhook():
    if not _check_secret():
        return jsonify({"ok": False}), 403
    update = request.get_json(force=True, silent=True) or {}
    try:
        cb = update.get("callback_query")
        if cb:
            data = cb.get("data", "")
            if data.startswith(("appr:", "edit:", "rej:")):
                _handle_admin_callback(cb)
            elif data.startswith("boostpick:"):
                _handle_user_callback(cb)
            return jsonify({"ok": True})
        msg = update.get("message")
        if msg:
            _handle_message(msg)
    except Exception as e:
        print(f"[moderation] webhook error: {e}")
    return jsonify({"ok": True})


def _handle_message(msg):
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    if not text:
        return
    # Админ пишет текст правки — приоритетно, только в его личном чате.
    if chat_id == _admin_chat_id():
        state = db.reference(f"{_ADMIN_STATE_REF}/{chat_id}").get()
        if state and state.get("editing"):
            _handle_admin_edit_text(chat_id, text, state)
            return
    _handle_user_message(chat_id, text)


def _handle_admin_callback(cb):
    data = cb.get("data", "")
    cb_id = cb.get("id")
    chat_id = cb["message"]["chat"]["id"]
    msg_id = cb["message"]["message_id"]
    if chat_id != _admin_chat_id():
        # Общий бот — без этой проверки кто угодно мог бы прислать
        # "appr:<id>" и одобрить/отклонить чужую заявку.
        _answer_cb(WA_BOT_TOKEN, cb_id, "недоступно")
        return
    try:
        action, sub_id = data.split(":", 1)
    except ValueError:
        _answer_cb(WA_BOT_TOKEN, cb_id, "ошибка")
        return
    ref = db.reference(f"{_SUBMISSIONS_REF}/{sub_id}")
    sub = ref.get()
    if not sub:
        _answer_cb(WA_BOT_TOKEN, cb_id, "заявка не найдена")
        return
    if sub.get("status") != "pending":
        _answer_cb(WA_BOT_TOKEN, cb_id, "заявка уже обработана")
        return

    if action == "appr":
        _approve(sub_id, sub)
        _edit(WA_BOT_TOKEN, chat_id, msg_id,
              f"✅ Одобрено\n\n{_fmt_fields(sub['type'], sub['fields'])}")
        _answer_cb(WA_BOT_TOKEN, cb_id, "Одобрено")
    elif action == "rej":
        ref.update({"status": "rejected"})
        _edit(WA_BOT_TOKEN, chat_id, msg_id,
              f"❌ Отклонено\n\n{_fmt_fields(sub['type'], sub['fields'])}")
        _send(WA_BOT_TOKEN, sub["tg_user_id"], "❌ Ваше объявление отклонено модератором.")
        _answer_cb(WA_BOT_TOKEN, cb_id, "Отклонено")
    elif action == "edit":
        db.reference(f"{_ADMIN_STATE_REF}/{chat_id}").set({"editing": sub_id, "msg_id": msg_id})
        _send(WA_BOT_TOKEN, chat_id,
              "✏️ Пришлите новый текст объявления одним сообщением — он заменит текущий, и заявка сразу одобрится.")
        _answer_cb(WA_BOT_TOKEN, cb_id, "Жду текст")


def _handle_admin_edit_text(chat_id, text, state):
    sub_id = state["editing"]
    ref = db.reference(f"{_SUBMISSIONS_REF}/{sub_id}")
    sub = ref.get()
    db.reference(f"{_ADMIN_STATE_REF}/{chat_id}").delete()
    if not sub or sub.get("status") != "pending":
        return
    f = dict(sub.get("fields") or {})
    # Правим основное текстовое поле по типу заявки — остальные поля (город,
    # контакт и т.д.) остаются как в исходной подаче.
    if sub["type"] in ("studio", "vacancy"):
        f["desc"] = text[:600]
    elif sub["type"] == "resume":
        f["experience"] = text[:500]
    ref.child("fields").set(f)
    sub["fields"] = f
    _edit(WA_BOT_TOKEN, chat_id, state.get("msg_id"),
          f"✅ Отредактировано и одобрено\n\n{_fmt_fields(sub['type'], f)}")
    _approve(sub_id, sub)


def _approve(sub_id, sub):
    """Вакансии/резюме публикуются сразу бесплатно. Студия — просим оплатить
    фиксированную цену подачи (submit_price), публикация будет после оплаты."""
    if sub["type"] == "studio":
        pricing = _deps["get_pricing"]()
        price = pricing["submit_price"]
        amount = _reserve_amount(sub["tg_user_id"], price)
        db.reference(f"{_PENDING_PAY_REF}/{sub['tg_user_id']}").set({
            "kind": "submit", "sub_id": sub_id, "price": price, "amount": amount,
            "created_at": time.time(),
        })
        db.reference(f"{_SUBMISSIONS_REF}/{sub_id}").update({"status": "awaiting_payment"})
        _send(WA_BOT_TOKEN, sub["tg_user_id"],
              f"✅ Ваше объявление одобрено!\n\n"
              f"Публикация стоит <b>{price} USDT</b> (разовая оплата, объявление остаётся навсегда).\n\n"
              f"Переведите <b>{amount} USDT</b> в сети <b>TRC-20 (Tron)</b> на адрес:\n"
              f"<code>{USDT_WALLET}</code>\n\n"
              f"⚠️ Сумма с уникальными копейками — переведите ТОЧНО {amount}, не округляйте.\n"
              f"После оплаты пришлите сюда одним сообщением хэш транзакции (TxID).")
    else:
        _publish_free(sub_id, sub)


def _publish_free(sub_id, sub):
    ref = db.reference(f"{_SUBMISSIONS_REF}/{sub_id}")
    f = sub["fields"]
    if sub["type"] == "vacancy":
        db.reference(_deps["vacancies_ref"]).push({
            "org": f.get("org", ""), "title": f.get("title", ""), "salary": f.get("salary", ""),
            "desc": f.get("desc", ""), "contact": f.get("contact", ""),
            "pinned": False, "date": datetime.now().strftime("%Y-%m-%d"),
        })
    elif sub["type"] == "resume":
        db.reference(_RESUMES_REF).push({
            "experience": f.get("experience", ""), "salary": f.get("salary", ""),
            "contact": f.get("contact", ""), "date": datetime.now().strftime("%Y-%m-%d"),
        })
    ref.update({"status": "published"})
    _send(WA_BOT_TOKEN, sub["tg_user_id"], "✅ Ваше объявление одобрено и опубликовано на сайте!")


# ─────────────────────────── единая сумма-резерв (submit ИЛИ boost) ──────────────
def _reserve_amount(tg_user_id, price):
    """Уникальная сумма для сверки платежа на общем кошельке (та же идея, что у
    VideoRils: base + 0.01*slot — уникальная копейка = id платежа). Смотрим на
    ВСЕ текущие незавершённые платежи (submit и boost, любых пользователей) с
    той же ценой, чтобы не выдать одному и то же копейки, что и другому."""
    root = db.reference(_PENDING_PAY_REF)
    now = time.time()
    existing = root.get() or {}
    used_cents = set()
    for uid, p in existing.items():
        if not isinstance(p, dict) or str(uid) == str(tg_user_id):
            continue
        if p.get("price") == price and p.get("created_at", 0) + RESERVE_TTL > now:
            amt = p.get("amount")
            if amt is not None:
                used_cents.add(round((amt - price) * 100))
    cents = 1
    while cents in used_cents and cents < 99:
        cents += 1
    return round(price + cents / 100.0, 2)


def _boost_buttons(city, exclude_slot=None):
    pricing = _deps["get_pricing"]()
    listings_ref = _deps["listings_ref"]
    slot_key = _deps["slot_key"]
    slot_count = _deps["slot_count"]
    raw = db.reference(listings_ref).get() or {}
    # ленивая проверка занятости лениво импортировать из app нельзя (циклический
    # импорт) — считаем прямо тут, по тем же полям (boost_tier/boost_expires_at).
    def eff_tier(rec):
        t = rec.get("boost_tier") or "regular"
        if t == "regular":
            return "regular"
        exp = rec.get("boost_expires_at")
        if exp and time.time() > exp:
            return "regular"
        return t

    def city_count(tier):
        c = 0
        for n in range(1, slot_count + 1):
            if n == exclude_slot:
                continue
            rec = raw.get(slot_key(n))
            if rec and rec.get("name") and rec.get("city") == city and eff_tier(rec) == tier:
                c += 1
        return c

    def home_count():
        c = 0
        for n in range(1, slot_count + 1):
            if n == exclude_slot:
                continue
            rec = raw.get(slot_key(n))
            if rec and rec.get("name") and eff_tier(rec) == "home":
                c += 1
        return c

    buttons = []
    for t in BOOST_ORDER:
        cap = pricing[f"{t}_count"]
        cnt = home_count() if t == "home" else city_count(t)
        if cnt >= cap:
            continue   # нет мест на этом уровне — не предлагаем
        price = pricing[f"{t}_price"]
        buttons.append([{"text": f"{BOOST_BUTTON_LABEL[t]} — ${price}/мес", "callback_data": f"boostpick:{t}"}])
    return buttons


def _offer_boost(tg_user_id, slot_n, city):
    buttons = _boost_buttons(city, exclude_slot=slot_n)
    if not buttons:
        return
    db.reference(f"{_BOOST_CTX_REF}/{tg_user_id}").set({"slot_n": slot_n, "city": city})
    _send(WA_BOT_TOKEN, tg_user_id,
          "⭐ Хотите поднять объявление выше в списке и выделить цветом? "
          "Буст действует 30 дней, дальше можно продлить командой /boost.", buttons)


def _handle_user_callback(cb):
    data = cb.get("data", "")
    cb_id = cb.get("id")
    chat_id = cb["message"]["chat"]["id"]

    if data.startswith("boostpick:"):
        tier = data.split(":", 1)[1]
        ctx_ref = db.reference(f"{_BOOST_CTX_REF}/{chat_id}")
        ctx = ctx_ref.get()
        if not ctx:
            _answer_cb(WA_BOT_TOKEN, cb_id, "сессия истекла, наберите /boost заново")
            return
        slot_n, city = ctx["slot_n"], ctx["city"]
        ctx_ref.delete()
        listings_ref = _deps["listings_ref"]
        slot_key = _deps["slot_key"]
        rec = db.reference(f"{listings_ref}/{slot_key(slot_n)}").get()
        if not rec or rec.get("owner_tg_id") != chat_id:
            _answer_cb(WA_BOT_TOKEN, cb_id, "объявление не найдено")
            return
        pricing = _deps["get_pricing"]()
        price = pricing[f"{tier}_price"]
        amount = _reserve_amount(chat_id, price)
        db.reference(f"{_PENDING_PAY_REF}/{chat_id}").set({
            "kind": "boost", "slot_n": slot_n, "tier": tier, "price": price, "amount": amount,
            "created_at": time.time(),
        })
        _send(WA_BOT_TOKEN, chat_id,
              f"💳 Буст: {BOOST_BUTTON_LABEL[tier]} — {price} USDT/мес\n\n"
              f"Переведите <b>{amount} USDT</b> в сети <b>TRC-20 (Tron)</b> на адрес:\n"
              f"<code>{USDT_WALLET}</code>\n\n"
              f"⚠️ Сумма с уникальными копейками — переведите ТОЧНО {amount}, не округляйте.\n"
              f"После оплаты пришлите сюда одним сообщением хэш транзакции (TxID).")
        _answer_cb(WA_BOT_TOKEN, cb_id, "Ждём оплату")
        return

    _answer_cb(WA_BOT_TOKEN, cb_id, "")


def _handle_user_message(chat_id, text):
    if text == "/boost":
        _cmd_boost(chat_id)
        return

    pending = db.reference(f"{_PENDING_PAY_REF}/{chat_id}").get()
    if not pending or not pending.get("amount"):
        return   # не ждём от этого пользователя платёж — молча игнорируем текст

    txid = text
    if _tx_already_used(txid):
        _send(WA_BOT_TOKEN, chat_id, "⚠️ Этот TxID уже был использован ранее.")
        return
    amount = pending.get("amount")
    paid = _verify_tx_onchain(txid, amount)
    if paid is None:
        _send(WA_BOT_TOKEN, chat_id,
              "❌ Платёж не найден или сумма меньше нужной. Проверьте перевод и пришлите TxID ещё раз "
              "(обычно платёж подтверждается в сети за 1-3 минуты).")
        return

    if pending.get("kind") == "submit":
        _finish_submit_payment(chat_id, pending, txid, paid)
    elif pending.get("kind") == "boost":
        _finish_boost_payment(chat_id, pending, txid, paid)

    db.reference(f"{_PROCESSED_TX_REF}/{txid}").set({
        "tg_user_id": chat_id, "amount": paid, "kind": pending.get("kind"),
        "processed_at": datetime.now().isoformat()})
    db.reference(f"{_PENDING_PAY_REF}/{chat_id}").delete()


def _cmd_boost(chat_id):
    listings_ref = _deps["listings_ref"]
    slot_key = _deps["slot_key"]
    slot_count = _deps["slot_count"]
    raw = db.reference(listings_ref).get() or {}
    owned = [(n, raw[slot_key(n)]) for n in range(1, slot_count + 1)
             if raw.get(slot_key(n)) and raw[slot_key(n)].get("owner_tg_id") == chat_id]
    if not owned:
        _send(WA_BOT_TOKEN, chat_id, "У вас пока нет опубликованных объявлений.")
        return
    n, rec = owned[0]
    _offer_boost(chat_id, n, rec.get("city", ""))


def _tx_already_used(txid):
    return bool(db.reference(f"{_PROCESSED_TX_REF}/{txid}").get())


def _verify_tx_onchain(txid, min_amount):
    """Тот же механизм, что у VideoRils: реальная входящая USDT-транзакция txid
    на наш кошелёк, сумма >= min_amount c допуском ±TOLERANCE_UNDER."""
    url = f"https://api.trongrid.io/v1/transactions/{txid}/events"
    headers = {"TRON-PRO-API-KEY": TRONGRID_API_KEY} if TRONGRID_API_KEY else {}
    try:
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code != 200:
            print(f"[pay] TronGrid HTTP {r.status_code} по txid={txid[:10]}")
            return None
        for ev in (r.json() or {}).get("data", []) or []:
            if ev.get("contract_address") != USDT_CONTRACT:
                continue
            res = ev.get("result", {}) or {}
            if res.get("to") != USDT_WALLET:
                continue
            try:
                paid = int(res.get("value", "0")) / 1_000_000.0
            except (TypeError, ValueError):
                continue
            if paid >= (min_amount - TOLERANCE_UNDER):
                return paid
        return None
    except Exception as e:
        print(f"[pay] ошибка проверки txid={txid[:10]}: {e}")
        return None


def _finish_submit_payment(chat_id, pending, txid, paid):
    sub_id = pending["sub_id"]
    sub_ref = db.reference(f"{_SUBMISSIONS_REF}/{sub_id}")
    sub = sub_ref.get()
    if not sub:
        return
    f = sub["fields"]
    listings_ref = _deps["listings_ref"]
    slot_key = _deps["slot_key"]
    slot_count = _deps["slot_count"]

    raw = db.reference(listings_ref).get() or {}
    free_n = None
    for n in range(1, slot_count + 1):
        rec = raw.get(slot_key(n))
        if not rec or not rec.get("name"):
            free_n = n
            break

    if free_n is None:
        _send(WA_BOT_TOKEN, WA_ADMIN_CHAT_ID,
              f"⚠️ Оплата подачи пришла (tx {txid[:12]}), но свободных мест из {slot_count} не осталось — "
              f"разберись вручную.\n\n{_fmt_fields('studio', f)}")
        sub_ref.update({"status": "paid_no_slot", "txid": txid, "paid_amount": paid})
        return

    photo = f.get("photo", "")
    db.reference(f"{listings_ref}/{slot_key(free_n)}").set({
        "name": f.get("name", ""), "city": f.get("city", ""), "desc": f.get("desc", ""),
        "photos": [photo] if photo else [], "cover_photo": photo, "photo": photo,
        "contacts": f.get("contact", ""), "phone": f.get("phone", ""), "url": f.get("url", ""),
        "percent": f.get("percent", ""),
        "social_telegram": f.get("social_telegram", ""), "social_instagram": f.get("social_instagram", ""),
        "social_vk": f.get("social_vk", ""),
        "status": "active", "boost_tier": "regular", "boost_expires_at": None, "boost_price": None,
        "clicks": 0, "owner_tg_id": chat_id,
    })
    sub_ref.update({"status": "published", "txid": txid, "paid_amount": paid, "published_slot": free_n})
    _send(WA_BOT_TOKEN, WA_ADMIN_CHAT_ID,
          f"💰 Оплата подачи подтверждена: ${paid}, tx {txid[:12]} — опубликовано (место #{free_n}).")
    _send(WA_BOT_TOKEN, chat_id, "✅ Оплата подтверждена! Объявление опубликовано на сайте.")
    _offer_boost(chat_id, free_n, f.get("city", ""))


def _finish_boost_payment(chat_id, pending, txid, paid):
    slot_n = pending["slot_n"]
    tier = pending["tier"]
    price = pending["price"]
    listings_ref = _deps["listings_ref"]
    slot_key = _deps["slot_key"]
    ref = db.reference(f"{listings_ref}/{slot_key(slot_n)}")
    rec = ref.get()
    if not rec or rec.get("owner_tg_id") != chat_id:
        _send(WA_BOT_TOKEN, WA_ADMIN_CHAT_ID,
              f"⚠️ Оплата буста пришла (tx {txid[:12]}), но место #{slot_n} не найдено/сменило владельца — "
              f"разберись вручную.")
        return
    ref.update({"boost_tier": tier, "boost_expires_at": time.time() + (30 * 24 * 3600), "boost_price": price})
    _send(WA_BOT_TOKEN, WA_ADMIN_CHAT_ID,
          f"💰 Оплата буста подтверждена: {BOOST_BUTTON_LABEL[tier]} ${paid}, tx {txid[:12]}, место #{slot_n}.")
    _send(WA_BOT_TOKEN, chat_id, f"✅ Буст {BOOST_BUTTON_LABEL[tier]} активирован на 30 дней!")
