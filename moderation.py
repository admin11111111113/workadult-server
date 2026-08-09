# -*- coding: utf-8 -*-
"""
Модерация объявлений через Telegram: единая очередь заявок (студии/вакансии/
резюме) с подачей через сайт (Telegram Login Widget).

Студии — тариф выбирается СРАЗУ в форме подачи на сайте (studii-podat.html),
оплата идёт СРАЗУ после отправки (без предварительного одобрения контента).
После успешной оплаты заявка уходит на финальное рассмотрение — админу
приходит уведомление и в Telegram (кнопки), и она видна в самой админке
(вкладка «Рассмотрение новых»); из ЛЮБОГО из двух мест можно опубликовать,
отредактировать перед публикацией или удалить. Деньги уже собраны на этом
этапе — если удаляют, возврат (если нужен) решается вручную, автоматически
не делается.

Тарифы студии — «база + буст»:
  • submit_price — обычное размещение (без цвета), разово, навсегда.
  • bronze/silver/gold — помесячный буст, поднимает цвет/место в городе,
    максимум N мест каждого на город (тарифы — в админке).
  • home — помесячный буст, топ на главной сайта (общий лимит, не по городу).
  Если выбранный платный уровень уже занят — заявка тихо откатывается на
  submit_price (обычное), и по желанию ставится в лист ожидания (уведомим,
  когда цветное место освободится — см. notify_waitlist()).

Вакансии/резюме — бесплатно: заявка → админ одобряет/отклоняет/редактирует
в Telegram кнопками → публикуется сразу по «Одобрить».

Оплата — USDT TRC-20 на ТОТ ЖЕ кошелёк, что у VideoRils (USDT_WALLET),
проверка — ТЕМ ЖЕ механизмом: он-чейн сверка через TronGrid по контракту
USDT-TRC20, допуск ±3 USDT, идемпотентность по txid.

Один бот на всё (WA_BOT_TOKEN) — тот же, что в Telegram Login Widget на
сайте. Пишет и подателю (оплата, буст, статус), и админу (заявки на
рассмотрение) в его личный чат WA_ADMIN_CHAT_ID. Раз бот общий, действия
админа ПРОВЕРЯЮТСЯ по chat_id — иначе любой пользователь того же бота мог
бы подделать callback_data и опубликовать/удалить чужую заявку.
"""
import os
import re
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
_SUPPORT_THREAD_REF = "/workadult_support_thread"  # tg_user_id -> открыт ли диалог с поддержкой
SUPPORT_THREAD_TTL = 48 * 3600
_TXID_RE = re.compile(r"^[0-9a-fA-F]{64}$")         # хэш транзакции Tron — ровно 64 hex-символа
_WAITLIST_REF = "/workadult_waitlist"              # город/тариф -> кто ждёт освобождения места
_VACANCY_LAST_POST_REF = "/workadult_vacancy_last_post"  # tg_user_id -> когда публиковал вакансию последний раз
VACANCY_POST_COOLDOWN = 7 * 24 * 3600               # 1 вакансия в неделю на студию

BOOST_ORDER = ("bronze", "silver", "gold", "home")   # порядок кнопок, дешёвый → дорогой
BOOST_BUTTON_LABEL = {"bronze": "🥉 Бронза", "silver": "🥈 Серебро",
                       "gold": "🥇 Золото", "home": "🏠 На главной"}
TIER_LABEL = dict(BOOST_BUTTON_LABEL, regular="Обычное")
KIND_LABEL = {"studio": "🏢 Студия", "vacancy": "💼 Вакансия", "resume": "🔎 Резюме"}

_deps = {}   # заполняется init_app() — переиспользуем логику тарифов/слотов из app.py


def init_app(app, get_pricing, listings_ref, vacancies_ref, slot_key, slot_count):
    _deps.update(get_pricing=get_pricing, listings_ref=listings_ref,
                 vacancies_ref=vacancies_ref, slot_key=slot_key, slot_count=slot_count)
    app.add_url_rule("/api/submit-studio", "submit_studio", _submit_studio, methods=["POST"])
    app.add_url_rule("/api/confirm-payment", "confirm_payment", _confirm_payment, methods=["POST"])
    app.add_url_rule("/api/report-payment-issue", "report_payment_issue", _report_payment_issue, methods=["POST"])
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


# ─────────────────────────── уровень места (эффективный, с учётом истечения) ──────────────
def _eff_tier(rec):
    t = rec.get("boost_tier") or "regular"
    if t == "regular":
        return "regular"
    exp = rec.get("boost_expires_at")
    if exp and time.time() > exp:
        return "regular"
    return t


def _tier_count(raw, tier, city=None, exclude_slot=None):
    slot_key = _deps["slot_key"]
    slot_count = _deps["slot_count"]
    c = 0
    for n in range(1, slot_count + 1):
        if n == exclude_slot:
            continue
        rec = raw.get(slot_key(n))
        if not rec or not rec.get("name"):
            continue
        if tier == "home":
            if _eff_tier(rec) == "home":
                c += 1
        elif rec.get("city") == city and _eff_tier(rec) == tier:
            c += 1
    return c


def _resolve_tier(desired_tier, city, pricing):
    """По желаемому уровню и текущей занятости города возвращает
    (итоговый_уровень, ПОЛНАЯ_цена_к_оплате, буст_цена_за_месяц_или_None,
    откатили_ли_на_обычное). Буст ДОПОЛНЯЕТ разовую подачу, а не заменяет
    её — золото = submit_price + gold_price, не просто gold_price."""
    if desired_tier == "regular" or desired_tier not in BOOST_ORDER:
        return "regular", pricing["submit_price"], None, False
    listings_ref = _deps["listings_ref"]
    raw = db.reference(listings_ref).get() or {}
    cnt = _tier_count(raw, desired_tier, city)
    cap = pricing[f"{desired_tier}_count"]
    if cnt >= cap:
        return "regular", pricing["submit_price"], None, True
    boost_price = pricing[f"{desired_tier}_price"]
    return desired_tier, pricing["submit_price"] + boost_price, boost_price, False


# ─────────────────────────── лист ожидания на занятые уровни ──────────────
def _join_waitlist(city, tier, tg_user_id):
    db.reference(f"{_WAITLIST_REF}/{city}/{tier}").push(
        {"tg_user_id": tg_user_id, "requested_at": time.time()})


def notify_waitlist(city, tier):
    """Публичная — дёргается из app.py, когда место освобождается вручную
    (админ удалил/скрыл забустченное место или снял буст при сохранении).
    Уведомляет всех в очереди этого города+уровня и очищает список."""
    if tier not in BOOST_ORDER:
        return
    ref = db.reference(f"{_WAITLIST_REF}/{city}/{tier}")
    entries = ref.get() or {}
    if not entries:
        return
    label = TIER_LABEL.get(tier, tier)
    for _, e in entries.items():
        if isinstance(e, dict) and e.get("tg_user_id"):
            _send(WA_BOT_TOKEN, e["tg_user_id"],
                  f"🎉 В городе {city} освободилось место уровня {label}! "
                  f"Успейте подать заявку: https://workadult.pro/studii-podat.html")
    ref.delete()


# ─────────────────────────── приём заявок с сайта ──────────────
def _new_submission(sub_type, fields, auth, extra=None):
    tg_id = auth.get("id")
    if not tg_id:
        return None, "auth"
    sub = {
        "type": sub_type, "status": "pending", "fields": fields,
        "tg_user_id": tg_id, "tg_username": auth.get("username", ""),
        "tg_name": (str(auth.get("first_name", "")) + " " + str(auth.get("last_name", ""))).strip(),
        "created_at": datetime.now().isoformat(),
    }
    if extra:
        sub.update(extra)
    ref = db.reference(_SUBMISSIONS_REF).push(sub)
    return ref.key, sub


def _submit_studio():
    """Студия: тариф выбирается прямо тут, оплата запускается сразу (без
    предварительного одобрения контента) — рассмотрение админом идёт ПОСЛЕ
    оплаты (см. _finish_submit_payment -> _notify_admin_review)."""
    data = request.get_json(force=True, silent=True) or {}
    auth = data.get("auth") or {}
    form = data.get("form") or {}
    tg_id = auth.get("id")
    if not tg_id:
        return jsonify({"ok": False, "error": "auth"}), 400

    fields = {
        "name": (form.get("studio") or form.get("name") or "").strip()[:120],
        "city": (form.get("city") or "").strip()[:80],
        "desc": (form.get("terms") or form.get("desc") or "").strip()[:600],
        "contact": (form.get("contact") or "").strip()[:200],
        "phone": (form.get("phone") or "").strip()[:40],
        "url": (form.get("url") or "").strip()[:300],
        # логотип/фото — 800×500, кроп на клиенте; или data:URI (JPEG ~60-190KB), или ссылка
        "photo": (form.get("logo") or form.get("photo") or "").strip()[:400000],
        "percent": (form.get("percent") or "").strip()[:60],
        "social_telegram": (form.get("social_telegram") or "").strip()[:120],
        "social_instagram": (form.get("social_instagram") or "").strip()[:120],
        "social_vk": (form.get("social_vk") or "").strip()[:120],
    }
    # Только для тарифа "на главной" — что рекламируем: карточку студии
    # или объявление о вакансии (обе тем же слотом/тем же $100/мес).
    promo_type = (form.get("promo_type") or "").strip()
    fields["promo_type"] = promo_type if promo_type in ("studio", "vacancy") else "studio"

    if not fields["name"] or not fields["city"] or not fields["contact"]:
        return jsonify({"ok": False, "error": "fields"}), 400

    desired_tier = (form.get("desired_tier") or "regular").strip()
    pricing = _deps["get_pricing"]()
    tier, price, boost_price, capped = _resolve_tier(desired_tier, fields["city"], pricing)

    sub_id, sub = _new_submission("studio", fields, auth, extra={
        "status": "awaiting_payment", "tier": tier, "price": price, "boost_price": boost_price,
    })

    amount = _reserve_amount(tg_id, price)
    db.reference(f"{_PENDING_PAY_REF}/{tg_id}").set({
        "kind": "submit", "sub_id": sub_id, "price": price, "amount": amount,
        "created_at": time.time(),
    })

    note = ""
    if capped:
        note = f"\n\n⚠️ Тариф «{TIER_LABEL.get(desired_tier, desired_tier)}» в городе {fields['city']} сейчас занят — оформляем как «{TIER_LABEL['regular']}» (${price})."
        if form.get("notify_waitlist") in ("on", "true", "1", True):
            _join_waitlist(fields["city"], desired_tier, tg_id)
            note += " Освободится место — напишем вам."

    perm = tier == "regular"
    breakdown = ""
    if boost_price:
        breakdown = f" (размещение ${pricing['submit_price']} + буст ${boost_price}/мес)"
    _send(WA_BOT_TOKEN, tg_id,
          f"💳 Тариф: {TIER_LABEL.get(tier, 'Обычное')}{breakdown}{note}\n\n"
          f"Переведите <b>{amount} USDT</b> в сети <b>TRC-20 (Tron)</b> на адрес:\n"
          f"<code>{USDT_WALLET}</code>\n\n"
          f"⚠️ Сумма с уникальными копейками — переведите {amount} (кошелёк может списать чуть больше, "
          f"это комиссия сети Tether ≈$1.5, не наша). Небольшая неточность не страшна, оплата пройдёт.\n"
          f"После оплаты пришлите сюда одним сообщением хэш транзакции (TxID).")
    return jsonify({"ok": True, "amount": amount, "address": USDT_WALLET, "tier": tier, "price": price, "capped": capped})


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

    tg_id = auth.get("id")
    last = db.reference(f"{_VACANCY_LAST_POST_REF}/{tg_id}").get() if tg_id else None
    if last and time.time() - last < VACANCY_POST_COOLDOWN:
        wait_days = int((VACANCY_POST_COOLDOWN - (time.time() - last)) / 86400) + 1
        return jsonify({"ok": False, "error": "cooldown",
                        "message": f"Можно публиковать не чаще раза в неделю. Попробуйте через {wait_days} дн."}), 429

    sub_id, sub = _new_submission("vacancy", fields, auth)
    if sub_id is None:
        return jsonify({"ok": False, "error": "auth"}), 400
    _notify_admin_pending(sub_id, sub)
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
    sub_id, sub = _new_submission("resume", fields, auth)
    if sub_id is None:
        return jsonify({"ok": False, "error": "auth"}), 400
    _notify_admin_pending(sub_id, sub)
    return jsonify({"ok": True})


def _notify_admin_pending(sub_id, sub):
    """Вакансии/резюме — бесплатные, ждут одобрения ДО публикации."""
    text = f"🆕 Новая заявка · {KIND_LABEL[sub['type']]}\n\n{_fmt_fields(sub['type'], sub['fields'])}\n\n👤 {_who(sub)}"
    buttons = [
        [{"text": "✅ Одобрить", "callback_data": f"appr:{sub_id}"},
         {"text": "✏️ Редактировать", "callback_data": f"edit:{sub_id}"}],
        [{"text": "❌ Отклонить", "callback_data": f"rej:{sub_id}"}],
    ]
    res = _send(WA_BOT_TOKEN, WA_ADMIN_CHAT_ID, text, buttons)
    msg_id = (res.get("result") or {}).get("message_id")
    if msg_id:
        db.reference(f"{_SUBMISSIONS_REF}/{sub_id}/admin_chat_msg_id").set(msg_id)


def _notify_admin_review(sub_id, sub):
    """Студия оплачена — финальное решение админа: опубликовать / отредактировать
    и опубликовать / удалить. Деньги уже собраны на этом шаге."""
    f = sub["fields"]
    tier = sub.get("tier", "regular")
    price = sub.get("price")
    boost_price = sub.get("boost_price")
    perm = tier == "regular"
    if perm:
        price_note = f"${price} разово"
    elif boost_price is not None:
        price_note = f"${price} (разово {price - boost_price} + буст ${boost_price}/мес)"
    else:
        price_note = f"${price}/мес"
    text = (f"💰 Оплачено · {TIER_LABEL.get(tier, 'Обычное')} ({price_note})\n\n"
            f"{_fmt_fields('studio', f)}\n\n👤 {_who(sub)}")
    buttons = [
        [{"text": "✅ Опубликовать", "callback_data": f"pub:{sub_id}"},
         {"text": "✏️ Редактировать", "callback_data": f"pedit:{sub_id}"}],
        [{"text": "🗑 Удалить", "callback_data": f"pdel:{sub_id}"}],
    ]
    res = _send(WA_BOT_TOKEN, WA_ADMIN_CHAT_ID, text, buttons)
    msg_id = (res.get("result") or {}).get("message_id")
    if msg_id:
        db.reference(f"{_SUBMISSIONS_REF}/{sub_id}/admin_chat_msg_id").set(msg_id)


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
            elif data.startswith(("pub:", "pedit:", "pdel:")):
                _handle_review_callback(cb)
            elif data.startswith("boostpick:") or data == "support":
                _handle_user_callback(cb)
            elif data.startswith("replypay:"):
                _handle_replypay_callback(cb)
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
        if state and state.get("replying_to"):
            target = state["replying_to"]
            db.reference(f"{_ADMIN_STATE_REF}/{chat_id}").delete()
            db.reference(f"{_SUPPORT_THREAD_REF}/{target}").set({"active": True, "started_at": time.time()})
            _send(WA_BOT_TOKEN, target, f"💬 <b>Ответ от поддержки:</b>\n\n{text}")
            _send(WA_BOT_TOKEN, chat_id, "✅ Отправлено пользователю.")
            return
        if state and state.get("editing"):
            _handle_admin_edit_text(chat_id, text, state)
            return
        if state and state.get("editing_review"):
            _handle_review_edit_text(chat_id, text, state)
            return
    _handle_user_message(chat_id, text, (msg.get("from") or {}).get("username"))


# ─────────────────────────── вакансии/резюме: одобрить ДО публикации (бесплатно) ──────────────
def _handle_admin_callback(cb):
    data = cb.get("data", "")
    cb_id = cb.get("id")
    chat_id = cb["message"]["chat"]["id"]
    msg_id = cb["message"]["message_id"]
    if chat_id != _admin_chat_id():
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
        approve_free(sub_id)
        _edit(WA_BOT_TOKEN, chat_id, msg_id,
              f"✅ Одобрено и опубликовано\n\n{_fmt_fields(sub['type'], sub['fields'])}")
        _answer_cb(WA_BOT_TOKEN, cb_id, "Одобрено")
    elif action == "rej":
        reject_pending(sub_id)
        _edit(WA_BOT_TOKEN, chat_id, msg_id,
              f"❌ Отклонено\n\n{_fmt_fields(sub['type'], sub['fields'])}")
        _answer_cb(WA_BOT_TOKEN, cb_id, "Отклонено")
    elif action == "edit":
        db.reference(f"{_ADMIN_STATE_REF}/{chat_id}").set({"editing": sub_id, "msg_id": msg_id})
        _send(WA_BOT_TOKEN, chat_id,
              "✏️ Пришлите новый текст объявления одним сообщением — он заменит текущий, и заявка сразу одобрится.")
        _answer_cb(WA_BOT_TOKEN, cb_id, "Жду текст")


def _handle_replypay_callback(cb):
    """«✍️ Ответить пользователю» под уведомлением о проблеме с оплатой —
    следующее текстовое сообщение админа в его личном чате пересылается
    тому пользователю от лица бота (см. _handle_message/replying_to)."""
    data = cb.get("data", "")
    cb_id = cb.get("id")
    admin_chat_id = cb["message"]["chat"]["id"]
    if admin_chat_id != _admin_chat_id():
        _answer_cb(WA_BOT_TOKEN, cb_id, "недоступно")
        return
    try:
        target_chat_id = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        _answer_cb(WA_BOT_TOKEN, cb_id, "ошибка")
        return
    db.reference(f"{_ADMIN_STATE_REF}/{admin_chat_id}").set({"replying_to": target_chat_id})
    _send(WA_BOT_TOKEN, admin_chat_id, "✍️ Напишите ответ пользователю следующим сообщением.")
    _answer_cb(WA_BOT_TOKEN, cb_id, "Жду ответ")


def _handle_admin_edit_text(chat_id, text, state):
    sub_id = state["editing"]
    ref = db.reference(f"{_SUBMISSIONS_REF}/{sub_id}")
    sub = ref.get()
    db.reference(f"{_ADMIN_STATE_REF}/{chat_id}").delete()
    if not sub or sub.get("status") != "pending":
        return
    f = dict(sub.get("fields") or {})
    if sub["type"] == "vacancy":
        f["desc"] = text[:600]
    elif sub["type"] == "resume":
        f["experience"] = text[:500]
    ref.child("fields").set(f)
    sub["fields"] = f
    approve_free(sub_id)
    _edit(WA_BOT_TOKEN, chat_id, state.get("msg_id"),
          f"✅ Отредактировано и опубликовано\n\n{_fmt_fields(sub['type'], f)}")


def approve_free(sub_id):
    """Публикует бесплатную заявку (вакансия/резюме). True при успехе."""
    ref = db.reference(f"{_SUBMISSIONS_REF}/{sub_id}")
    sub = ref.get()
    if not sub or sub.get("status") != "pending":
        return False
    f = sub["fields"]
    if sub["type"] == "vacancy":
        db.reference(_deps["vacancies_ref"]).push({
            "org": f.get("org", ""), "title": f.get("title", ""), "salary": f.get("salary", ""),
            "desc": f.get("desc", ""), "contact": f.get("contact", ""),
            "date": datetime.now().strftime("%Y-%m-%d"), "ts": time.time(),
            "tg_user_id": sub.get("tg_user_id"),
        })
        if sub.get("tg_user_id"):
            db.reference(f"{_VACANCY_LAST_POST_REF}/{sub['tg_user_id']}").set(time.time())
    elif sub["type"] == "resume":
        db.reference(_RESUMES_REF).push({
            "experience": f.get("experience", ""), "salary": f.get("salary", ""),
            "contact": f.get("contact", ""), "date": datetime.now().strftime("%Y-%m-%d"),
        })
    ref.update({"status": "published"})
    _send(WA_BOT_TOKEN, sub["tg_user_id"], "✅ Ваше объявление одобрено и опубликовано на сайте!")
    return True


def reject_pending(sub_id):
    ref = db.reference(f"{_SUBMISSIONS_REF}/{sub_id}")
    sub = ref.get()
    if not sub:
        return False
    ref.update({"status": "rejected"})
    _send(WA_BOT_TOKEN, sub["tg_user_id"], "❌ Ваше объявление отклонено модератором.")
    return True


# ─────────────────────────── студия: рассмотрение ПОСЛЕ оплаты ──────────────
def _handle_review_callback(cb):
    data = cb.get("data", "")
    cb_id = cb.get("id")
    chat_id = cb["message"]["chat"]["id"]
    msg_id = cb["message"]["message_id"]
    if chat_id != _admin_chat_id():
        _answer_cb(WA_BOT_TOKEN, cb_id, "недоступно")
        return
    try:
        action, sub_id = data.split(":", 1)
    except ValueError:
        _answer_cb(WA_BOT_TOKEN, cb_id, "ошибка")
        return
    ref = db.reference(f"{_SUBMISSIONS_REF}/{sub_id}")
    sub = ref.get()
    if not sub or sub.get("status") != "awaiting_review":
        _answer_cb(WA_BOT_TOKEN, cb_id, "заявка уже обработана")
        return

    if action == "pub":
        ok, why = publish_studio(sub_id)
        if ok:
            _edit(WA_BOT_TOKEN, chat_id, msg_id, f"✅ Опубликовано\n\n{_fmt_fields('studio', sub['fields'])}")
        _answer_cb(WA_BOT_TOKEN, cb_id, "Опубликовано" if ok else why)
    elif action == "pdel":
        reject_paid(sub_id)
        _edit(WA_BOT_TOKEN, chat_id, msg_id, f"🗑 Удалено\n\n{_fmt_fields('studio', sub['fields'])}")
        _answer_cb(WA_BOT_TOKEN, cb_id, "Удалено")
    elif action == "pedit":
        db.reference(f"{_ADMIN_STATE_REF}/{chat_id}").set({"editing_review": sub_id, "msg_id": msg_id})
        _send(WA_BOT_TOKEN, chat_id,
              "✏️ Пришлите новое описание одним сообщением — объявление сразу опубликуется с ним.")
        _answer_cb(WA_BOT_TOKEN, cb_id, "Жду текст")


def _handle_review_edit_text(chat_id, text, state):
    sub_id = state["editing_review"]
    ref = db.reference(f"{_SUBMISSIONS_REF}/{sub_id}")
    sub = ref.get()
    db.reference(f"{_ADMIN_STATE_REF}/{chat_id}").delete()
    if not sub or sub.get("status") != "awaiting_review":
        return
    f = dict(sub.get("fields") or {})
    f["desc"] = text[:600]
    ref.child("fields").set(f)
    ok, why = publish_studio(sub_id)
    if ok:
        _edit(WA_BOT_TOKEN, chat_id, state.get("msg_id"), f"✅ Отредактировано и опубликовано\n\n{_fmt_fields('studio', f)}")


def save_submission_fields(sub_id, new_fields):
    """Для веб-формы в админке — обновляет поля заявки, ещё ожидающей рассмотрения."""
    ref = db.reference(f"{_SUBMISSIONS_REF}/{sub_id}")
    if not ref.get():
        return False
    ref.child("fields").set(new_fields)
    return True


def admin_update_pending_payment(sub_id, new_fields, new_tier=None):
    """Правка ещё НЕ оплаченной заявки из веб-админки — поля +, по желанию,
    смена тарифа (пересчитывает price/boost_price и подтягивает уже
    выставленную сумму в pending_payment, если она ещё ждёт оплаты)."""
    ref = db.reference(f"{_SUBMISSIONS_REF}/{sub_id}")
    sub = ref.get()
    if not sub or sub.get("status") != "awaiting_payment":
        return False
    fields = dict(sub.get("fields") or {})
    fields.update({k: v for k, v in new_fields.items() if v is not None})
    updates = {"fields": fields}

    if new_tier and new_tier in ("regular",) + BOOST_ORDER:
        pricing = _deps["get_pricing"]()
        if new_tier == "regular":
            updates["tier"] = "regular"
            updates["price"] = pricing["submit_price"]
            updates["boost_price"] = None
        else:
            boost_price = pricing[f"{new_tier}_price"]
            updates["tier"] = new_tier
            updates["price"] = pricing["submit_price"] + boost_price
            updates["boost_price"] = boost_price
        tg_id = sub.get("tg_user_id")
        if tg_id:
            pending = db.reference(f"{_PENDING_PAY_REF}/{tg_id}").get()
            if pending and pending.get("sub_id") == sub_id:
                amount = _reserve_amount(tg_id, updates["price"])
                db.reference(f"{_PENDING_PAY_REF}/{tg_id}").update({"price": updates["price"], "amount": amount})

    ref.update(updates)
    return True


def admin_mark_paid_and_publish(sub_id):
    """Админ вручную подтверждает оплату (без TxID) — когда автопоиск не
    сработал, но факт оплаты проверен другим способом (например, в переписке
    по «Связаться с поддержкой»). Переводит заявку в awaiting_review и сразу
    публикует, тем же путём, что и настоящая on-chain оплата."""
    ref = db.reference(f"{_SUBMISSIONS_REF}/{sub_id}")
    sub = ref.get()
    if not sub or sub.get("status") != "awaiting_payment":
        return False, "заявка уже обработана"
    ref.update({"status": "awaiting_review", "confirmed_by": "admin_manual"})
    tg_id = sub.get("tg_user_id")
    if tg_id:
        db.reference(f"{_PENDING_PAY_REF}/{tg_id}").delete()
    return publish_studio(sub_id)


def delete_pending_payment(sub_id):
    """Удаляет неоплаченную заявку без публикации (снимает и связанный
    ожидающий платёж, если он ещё висит на этого пользователя)."""
    ref = db.reference(f"{_SUBMISSIONS_REF}/{sub_id}")
    sub = ref.get()
    if not sub or sub.get("status") != "awaiting_payment":
        return False
    tg_id = sub.get("tg_user_id")
    if tg_id:
        pending = db.reference(f"{_PENDING_PAY_REF}/{tg_id}").get()
        if pending and pending.get("sub_id") == sub_id:
            db.reference(f"{_PENDING_PAY_REF}/{tg_id}").delete()
    ref.delete()
    return True


def publish_studio(sub_id):
    """Публикует уже оплаченную студию в свободный слот. Вызывается и из
    Telegram-кнопки, и из веб-формы админки. Возвращает (ok, причина_если_нет)."""
    ref = db.reference(f"{_SUBMISSIONS_REF}/{sub_id}")
    sub = ref.get()
    if not sub or sub.get("status") != "awaiting_review":
        return False, "заявка уже обработана"
    f = sub["fields"]
    listings_ref = _deps["listings_ref"]
    slot_key = _deps["slot_key"]
    slot_count = _deps["slot_count"]
    tier = sub.get("tier", "regular")
    boost_price = sub.get("boost_price")  # цена буста за месяц (без разовой подачи), None для regular

    raw = db.reference(listings_ref).get() or {}
    free_n = None
    for n in range(1, slot_count + 1):
        rec = raw.get(slot_key(n))
        if not rec or not rec.get("name"):
            free_n = n
            break
    if free_n is None:
        return False, f"нет свободных мест из {slot_count}"

    photo = f.get("photo", "")
    expires_at = None if tier == "regular" else time.time() + (30 * 24 * 3600)
    db.reference(f"{listings_ref}/{slot_key(free_n)}").set({
        "name": f.get("name", ""), "city": f.get("city", ""), "desc": f.get("desc", ""),
        "photos": [photo] if photo else [], "cover_photo": photo, "photo": photo,
        "contacts": f.get("contact", ""), "phone": f.get("phone", ""), "url": f.get("url", ""),
        "percent": f.get("percent", ""),
        "social_telegram": f.get("social_telegram", ""), "social_instagram": f.get("social_instagram", ""),
        "social_vk": f.get("social_vk", ""),
        "status": "active", "boost_tier": tier, "boost_expires_at": expires_at,
        "boost_price": boost_price,
        "clicks": 0, "owner_tg_id": sub["tg_user_id"],
        "promo_type": f.get("promo_type") or "studio",
    })
    ref.update({"status": "published", "published_slot": free_n})
    _send(WA_BOT_TOKEN, sub["tg_user_id"], "🎉 Ваше объявление опубликовано на сайте!")
    if tier == "regular":
        _offer_boost(sub["tg_user_id"], free_n, f.get("city", ""))
    return True, "ok"


def reject_paid(sub_id):
    """Удаляет уже оплаченную заявку без публикации (возврат — вручную, не
    автоматом). Используется и из Telegram, и из веб-формы."""
    ref = db.reference(f"{_SUBMISSIONS_REF}/{sub_id}")
    sub = ref.get()
    if not sub:
        return False
    ref.update({"status": "rejected"})
    _send(WA_BOT_TOKEN, sub["tg_user_id"],
          "❌ Ваше объявление не прошло проверку и не будет опубликовано. По вопросам оплаты — напишите в поддержку.")
    return True


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
    raw = db.reference(listings_ref).get() or {}
    buttons = []
    for t in BOOST_ORDER:
        cap = pricing[f"{t}_count"]
        cnt = _tier_count(raw, t, city, exclude_slot=exclude_slot)
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
              f"⚠️ Сумма с уникальными копейками — переведите {amount} (кошелёк может списать чуть больше, "
          f"это комиссия сети Tether ≈$1.5, не наша). Небольшая неточность не страшна, оплата пройдёт.\n"
              f"После оплаты пришлите сюда одним сообщением хэш транзакции (TxID).")
        _answer_cb(WA_BOT_TOKEN, cb_id, "Ждём оплату")
        return

    if data == "support":
        username = (cb.get("from") or {}).get("username")
        _notify_payment_help(chat_id, username)
        _answer_cb(WA_BOT_TOKEN, cb_id, "Сообщение отправлено администратору")
        return

    _answer_cb(WA_BOT_TOKEN, cb_id, "")


def _process_txid(chat_id, txid):
    """Проверка и зачисление оплаты по TxID — общая для Telegram-бота (сообщение
    в чат) и веб-кнопки «Я оплатил» на сайте. Возвращает (ok, код_ошибки_или_None).
    Сообщения в Telegram шлём в обоих случаях — чтобы у пользователя остался след
    независимо от того, откуда пришёл TxID."""
    pending = db.reference(f"{_PENDING_PAY_REF}/{chat_id}").get()
    if not pending or not pending.get("amount"):
        return False, "no_pending"

    if _tx_already_used(txid):
        _send(WA_BOT_TOKEN, chat_id, "⚠️ Этот TxID уже был использован ранее.")
        return False, "used"

    amount = pending.get("amount")
    paid = _verify_tx_onchain(txid, amount)
    if paid is None:
        _send(WA_BOT_TOKEN, chat_id,
              "❌ Платёж не найден или сумма меньше нужной. Проверьте перевод и пришлите TxID ещё раз "
              "(обычно платёж подтверждается в сети за 1-3 минуты).",
              buttons=[[{"text": "💬 Связаться с поддержкой", "callback_data": "support"}]])
        return False, "not_found"

    if pending.get("kind") == "submit":
        _finish_submit_payment(chat_id, pending, txid, paid)
    elif pending.get("kind") == "boost":
        _finish_boost_payment(chat_id, pending, txid, paid)

    db.reference(f"{_PROCESSED_TX_REF}/{txid}").set({
        "tg_user_id": chat_id, "amount": paid, "kind": pending.get("kind"),
        "processed_at": datetime.now().isoformat()})
    db.reference(f"{_PENDING_PAY_REF}/{chat_id}").delete()
    return True, None


def _forward_user_message_to_admin(chat_id, text, username=None):
    """Продолжение диалога с поддержкой — пользователь пишет ПОСЛЕ того, как
    уже нажал «Связаться с поддержкой» (или получил ответ от админа).
    Пересылаем в тот же личный чат админа с той же кнопкой «Ответить»."""
    who = f"id {chat_id}" + (f" (@{username})" if username else "")
    _send(WA_BOT_TOKEN, WA_ADMIN_CHAT_ID, f"💬 <b>Сообщение от пользователя</b> ({who}):\n\n{text}",
          buttons=[[{"text": "✍️ Ответить", "callback_data": f"replypay:{chat_id}"}]])
    _send(WA_BOT_TOKEN, chat_id, "✅ Сообщение отправлено в поддержку.")


def _handle_user_message(chat_id, text, username=None):
    if text == "/boost":
        _cmd_boost(chat_id)
        return

    # Пока открыт диалог с поддержкой — обычный текст форвардим админу, а не
    # пытаемся понять его как TxID. Настоящий хэш транзакции (ровно 64 hex-
    # символа, например для буста — тот путь всё ещё только через бота)
    # распознаём и проверяем как обычно, даже если диалог открыт.
    thread = db.reference(f"{_SUPPORT_THREAD_REF}/{chat_id}").get()
    thread_active = thread and thread.get("active") and (time.time() - thread.get("started_at", 0)) < SUPPORT_THREAD_TTL
    if thread_active and not _TXID_RE.match(text.strip()):
        _forward_user_message_to_admin(chat_id, text, username)
        return

    _process_txid(chat_id, text)


def _confirm_payment():
    """POST /api/confirm-payment — кнопка «Подтвердить оплату» на сайте: TxID
    вводить не нужно (как у VideoRils) — сами ищем подходящий входящий платёж
    на кошелёк. Фронт опрашивает этот эндпоинт, пока не найдёт (или не бросит)."""
    data = request.get_json(force=True, silent=True) or {}
    auth = data.get("auth") or {}
    tg_id = auth.get("id")
    if not tg_id:
        return jsonify({"ok": False, "error": "fields"}), 400
    pending = db.reference(f"{_PENDING_PAY_REF}/{tg_id}").get()
    if not pending or not pending.get("amount"):
        return jsonify({"ok": False, "error": "no_pending"})
    txid = _find_incoming_payment(pending["amount"])
    if not txid:
        return jsonify({"ok": False, "error": "not_found"})
    ok, err = _process_txid(tg_id, txid)
    return jsonify({"ok": ok, "error": err})


def _notify_payment_help(chat_id, username=None, note=None):
    """Общая логика «нужна помощь с оплатой» — и веб-кнопка «Написать нам»
    (report-payment-issue), и inline-кнопка «💬 Связаться с поддержкой» под
    сообщением «платёж не найден» прямо в этом же Telegram-боте. Открывает
    диалог с поддержкой — дальнейшие сообщения этого пользователя боту
    (кроме похожих на TxID) идут админу, пока диалог не протухнет."""
    db.reference(f"{_SUPPORT_THREAD_REF}/{chat_id}").set({"active": True, "started_at": time.time()})
    pending = db.reference(f"{_PENDING_PAY_REF}/{chat_id}").get()
    who = f"id {chat_id}" + (f" (@{username})" if username else "")
    text = f"⚠️ <b>Нужна помощь с оплатой</b>\n\nОт: {who}\n"
    if pending:
        text += f"Ожидаемая сумма: {pending.get('amount')} USDT · тип: {pending.get('kind')}\n"
        if pending.get("kind") == "submit" and pending.get("sub_id"):
            sub = db.reference(f"{_SUBMISSIONS_REF}/{pending['sub_id']}").get()
            if sub:
                f = sub.get("fields") or {}
                text += f"Заявка: <b>{f.get('name', '')}</b>, {f.get('city', '')}, контакт {f.get('contact', '')}\n"
        elif pending.get("kind") == "boost" and pending.get("slot_n"):
            listings_ref = _deps["listings_ref"]
            slot_key = _deps["slot_key"]
            rec = db.reference(f"{listings_ref}/{slot_key(pending['slot_n'])}").get()
            if rec:
                text += f"Место: <b>{rec.get('name', '')}</b>, {rec.get('city', '')}\n"
    if note:
        text += f"\nСообщение от пользователя:\n{note}"
    _send(WA_BOT_TOKEN, WA_ADMIN_CHAT_ID, text,
          buttons=[[{"text": "✍️ Ответить пользователю", "callback_data": f"replypay:{chat_id}"}]])
    _send(WA_BOT_TOKEN, chat_id, "✅ Мы получили ваш запрос — администратор проверит оплату вручную и ответит здесь, в Telegram.")


def _report_payment_issue():
    """POST /api/report-payment-issue — если автопоиск не находит платёж,
    кнопка «Написать нам» на сайте шлёт админу контекст (кто, сколько,
    заявка) + сообщение от пользователя, чтобы проверить и подтвердить
    вручную через веб-админку/Telegram."""
    data = request.get_json(force=True, silent=True) or {}
    auth = data.get("auth") or {}
    tg_id = auth.get("id")
    note = (data.get("message") or "").strip()[:500]
    if not tg_id:
        return jsonify({"ok": False, "error": "fields"}), 400
    _notify_payment_help(tg_id, auth.get("username"), note)
    return jsonify({"ok": True})


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


def _find_incoming_payment(min_amount):
    """Автопоиск платежа без TxID от пользователя — как у VideoRils: смотрим
    последние входящие USDT-переводы на наш кошелёк и ищем подходящую сумму,
    которая ещё не была засчитана. Возвращает txid или None."""
    url = f"https://api.trongrid.io/v1/accounts/{USDT_WALLET}/transactions/trc20"
    params = {"limit": 30, "contract_address": USDT_CONTRACT, "only_to": "true"}
    headers = {"TRON-PRO-API-KEY": TRONGRID_API_KEY} if TRONGRID_API_KEY else {}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=20)
        if r.status_code != 200:
            print(f"[pay] TronGrid HTTP {r.status_code} при поиске входящих")
            return None
        for tx in (r.json() or {}).get("data", []) or []:
            if tx.get("to") != USDT_WALLET:
                continue
            txid = tx.get("transaction_id")
            if not txid or _tx_already_used(txid):
                continue
            try:
                paid = int(tx.get("value", "0")) / 1_000_000.0
            except (TypeError, ValueError):
                continue
            if paid >= (min_amount - TOLERANCE_UNDER):
                return txid
        return None
    except Exception as e:
        print(f"[pay] ошибка поиска входящих платежей: {e}")
        return None


def _finish_submit_payment(chat_id, pending, txid, paid):
    """Оплата подачи подтверждена — НЕ публикуем сразу, отправляем на финальное
    рассмотрение (Telegram-кнопки + видно в веб-админке)."""
    sub_id = pending["sub_id"]
    sub_ref = db.reference(f"{_SUBMISSIONS_REF}/{sub_id}")
    sub = sub_ref.get()
    if not sub:
        return
    sub_ref.update({"status": "awaiting_review", "txid": txid, "paid_amount": paid})
    sub["status"] = "awaiting_review"
    _send(WA_BOT_TOKEN, chat_id,
          "✅ Оплата подтверждена! Объявление отправлено на финальную проверку — опубликуем в ближайшее время.")
    _notify_admin_review(sub_id, sub)


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
