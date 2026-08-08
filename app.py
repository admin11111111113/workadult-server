# -*- coding: utf-8 -*-
import json
import os
import secrets
import time
from datetime import datetime, timedelta
from functools import wraps

import firebase_admin
import requests
from firebase_admin import credentials, db
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from flask_cors import CORS

import moderation

SLOT_COUNT = 100
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()
SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "").strip() or secrets.token_hex(32)

# Счётчик на сайте реально стоит: Яндекс.Метрика 109925310 в index.html.
# Чтобы показать цифры в админке, нужен OAuth-токен в переменной YM_TOKEN на
# Render. Без токена ничего не выдумываем — честно говорим, что не подключено.
YM_COUNTER = os.environ.get("YM_COUNTER_ID", "109925310")
_ym_cache = {"ts": 0, "data": None}

_cred_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "").strip()
_db_url = os.environ.get("FIREBASE_DB_URL", "").strip()
if _cred_json and _db_url:
    cred = credentials.Certificate(json.loads(_cred_json))
    firebase_admin.initialize_app(cred, {"databaseURL": _db_url})
else:
    print("[warn] FIREBASE_SERVICE_ACCOUNT/FIREBASE_DB_URL не заданы")

app = Flask(__name__)
app.secret_key = SECRET_KEY
CORS(app, resources={r"/api/*": {"origins": "*"}})

_LISTINGS_REF = "/workadult_studios"
_VACANCIES_REF = "/workadult_vacancies"
_CATALOG_REF = "/workadult_catalog_studios"
_PRICING_REF = "/workadult_pricing"

VACANCY_FIELDS = ("org", "title", "salary", "desc", "contact")
CATALOG_FIELDS = ("name", "city", "percent", "desc", "contact")
CATALOG_FORMATS = ("studio", "home", "pair", "guys", "nonnude")
CATALOG_FMT_LABELS = {"studio": "В студии", "home": "Из дома", "pair": "Парой",
                      "guys": "Для парней", "nonnude": "Non Nude"}

# Особенности студии (для платных мест workadult_studios) — чекбоксы в форме
# «По городам» и в /api/listings. Форматы работы используют тот же словарь
# CATALOG_FORMATS/CATALOG_FMT_LABELS, что и формат-каталог studii-katalog.html.
STUDIO_FEATURES = ("weekly_pay", "flexible_schedule", "housing", "training", "city_center", "near_metro")
STUDIO_FEATURE_LABELS = {
    "weekly_pay": "Еженедельные выплаты", "flexible_schedule": "Гибкий график",
    "housing": "Предоставляем проживание", "training": "Обучение новичков",
    "city_center": "В центре города", "near_metro": "Рядом с метро",
}
STUDIO_SOCIAL_FIELDS = ("social_telegram", "social_instagram", "social_vk")
MAX_STUDIO_PHOTOS = 5

# Тарифы задаются в админке (вкладка «Обзор») и хранятся в Firebase — эти
# значения только запасной вариант, если настройки ещё не сохранялись.
#
# Модель «база + буст»: submit_price — разовая оплата за публикацию, объявление
# остаётся навсегда обычным (без цвета/приоритета). Поверх — необязательный
# помесячный буст (бронза/серебро/золото/на главной), поднимающий цвет и место
# в списке города (либо вообще на главную сайта). Не продлил буст — на главной
# больше не показывается, а в городе тихо съезжает обратно на обычный цвет и
# позицию; сама публикация НЕ пропадает (она разовая и навсегда).
PRICING_DEFAULTS = {
    "submit_price": 70,
    "bronze_count": 5, "bronze_price": 15,
    "silver_count": 5, "silver_price": 30,
    "gold_count": 5, "gold_price": 50,
    "home_count": 5, "home_price": 100,
}
BOOST_TIERS = ("home", "gold", "silver", "bronze")     # порядок значимости, сверху вниз
BOOST_LABELS = {"home": "🏠 На главной", "gold": "🥇 Золото",
                "silver": "🥈 Серебро", "bronze": "🥉 Бронза", "regular": "Обычное"}

def _get_pricing():
    raw = db.reference(_PRICING_REF).get() or {}
    out = dict(PRICING_DEFAULTS)
    for k in out:
        try:
            out[k] = max(0, int(raw.get(k, out[k])))
        except (TypeError, ValueError):
            pass
    return out

def _effective_boost(rec):
    """Текущий буст записи с учётом истечения: если срок буста прошёл — тихо
    считаем её «обычной» (без переписывания записи — просто на чтении)."""
    tier = rec.get("boost_tier") or "regular"
    if tier == "regular":
        return "regular", None
    exp = rec.get("boost_expires_at")
    if exp and time.time() > exp:
        return "regular", None
    return tier, rec.get("boost_price")

PUBLIC_FIELDS = ("name", "city", "desc", "photo", "contacts", "url",
                 "percent", "phone", "social_telegram", "social_instagram", "social_vk")

def _public_extras(rec):
    """Доп. поля студии, которые не влезают в плоский PUBLIC_FIELDS — списки фото
    и мультивыборы (форматы работы/особенности)."""
    photos = rec.get("photos") or []
    cover = rec.get("cover_photo") or (photos[0] if photos else "")
    return {
        "photos": photos,
        "cover_photo": cover,
        "formats": [f for f in CATALOG_FORMATS if rec.get("fmt_" + f)],
        "features": [f for f in STUDIO_FEATURES if rec.get("feat_" + f)],
    }

def _slot_key(n):
    return f"slot_{int(n):03d}"

def _city_boost_count(raw, city, tier, exclude_slot=None):
    """Сколько мест в городе СЕЙЧАС реально держат этот буст (с учётом истечения)."""
    count = 0
    for n in range(1, SLOT_COUNT + 1):
        if n == exclude_slot:
            continue
        rec = raw.get(_slot_key(n))
        if not rec or not rec.get("name") or rec.get("city") != city:
            continue
        eff_tier, _ = _effective_boost(rec)
        if eff_tier == tier:
            count += 1
    return count

def _home_boost_count(raw, exclude_slot=None):
    """Сколько мест сайта СЕЙЧАС держат буст «на главной» (общий лимит, не по городу)."""
    count = 0
    for n in range(1, SLOT_COUNT + 1):
        if n == exclude_slot:
            continue
        rec = raw.get(_slot_key(n))
        if not rec or not rec.get("name"):
            continue
        eff_tier, _ = _effective_boost(rec)
        if eff_tier == "home":
            count += 1
    return count

def _ym(tok, date1, date2):
    """Визиты и уникальные посетители за период (не суммируются по дням)."""
    r = requests.get("https://api-metrika.yandex.net/stat/v1/data",
                      params={"ids": YM_COUNTER, "metrics": "ym:s:visits,ym:s:users",
                              "date1": date1, "date2": date2},
                      headers={"Authorization": "OAuth " + tok}, timeout=10)
    tot = (r.json().get("totals") or [0, 0])
    return {"visits": int(tot[0]), "users": int(tot[1])}

def _site_traffic():
    tok = (os.environ.get("YM_TOKEN") or "").strip()
    if not tok:
        return {"ok": False, "counter": YM_COUNTER,
                "why": "на сервере нет токена доступа к Метрике (переменная YM_TOKEN "
                       "на Render не задана) — панель не может запросить цифры."}
    if _ym_cache["data"] and time.time() - _ym_cache["ts"] < 600:
        return _ym_cache["data"]
    try:
        out = {"ok": True, "counter": YM_COUNTER,
               "d1": _ym(tok, "today", "today"),
               "d7": _ym(tok, "7daysAgo", "today"),
               "d30": _ym(tok, "30daysAgo", "today")}
    except Exception as e:
        out = {"ok": False, "counter": YM_COUNTER,
               "why": "Метрика не ответила (%s) — проверь токен YM_TOKEN и права." % str(e)[:80]}
    _ym_cache.update({"ts": time.time(), "data": out})
    return out

def _require_admin(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("admin_ok"):
            return redirect(url_for("admin_login"))
        return fn(*a, **kw)
    return wrapper

# ─────────────────────────── публичный API ──────────────
@app.route("/api/listings", methods=["GET"])
def api_listings():
    raw = db.reference(_LISTINGS_REF).get() or {}
    pricing = _get_pricing()
    out = []
    for n in range(1, SLOT_COUNT + 1):
        rec = raw.get(_slot_key(n))
        # Публикация разовая и навсегда — скрываем только по ручному статусу
        # (админ «скрыл») или если места вообще нет.
        if not rec or rec.get("status") != "active":
            out.append({"slot": n, "price": pricing["submit_price"], "listing": None})
            continue
        tier, boost_price = _effective_boost(rec)
        listing = {k: rec.get(k, "") for k in PUBLIC_FIELDS}
        listing.update(_public_extras(rec))
        listing["tier"] = tier
        listing["boost_price"] = boost_price
        out.append({"slot": n, "price": pricing["submit_price"], "listing": listing})
    return jsonify({"ok": True, "slots": out})

@app.route("/api/featured", methods=["GET"])
def api_featured():
    """Места с бустом «на главной» (см. тарифы в админке) для ротации на главной сайта"""
    raw = db.reference(_LISTINGS_REF).get() or {}
    featured = []
    for n in range(1, SLOT_COUNT + 1):
        rec = raw.get(_slot_key(n))
        if not rec or not rec.get("name") or rec.get("status") != "active":
            continue
        tier, _ = _effective_boost(rec)
        if tier == "home":
            listing = {k: rec.get(k, "") for k in PUBLIC_FIELDS}
            listing.update(_public_extras(rec))
            listing["slot"] = n
            listing["tier"] = tier
            featured.append(listing)
    return jsonify({"ok": True, "featured": featured})

@app.route("/api/tier-availability", methods=["GET"])
def api_tier_availability():
    """Занятость платных уровней в городе — форма подачи на сайте показывает
    это перед выбором тарифа (и предлагает лист ожидания на занятые)."""
    city = (request.args.get("city") or "").strip()[:80]
    raw = db.reference(_LISTINGS_REF).get() or {}
    pricing = _get_pricing()
    tiers = {}
    for t in ("bronze", "silver", "gold"):
        used = _city_boost_count(raw, city, t) if city else 0
        cap = pricing[f"{t}_count"]
        tiers[t] = {"cap": cap, "used": used, "available": used < cap, "price": pricing[f"{t}_price"]}
    home_used = _home_boost_count(raw)
    home_cap = pricing["home_count"]
    tiers["home"] = {"cap": home_cap, "used": home_used, "available": home_used < home_cap, "price": pricing["home_price"]}
    return jsonify({"ok": True, "city": city, "submit_price": pricing["submit_price"], "tiers": tiers})

@app.route("/api/board", methods=["GET"])
def api_board():
    """Вакансии, добавленные вручную из админки — слой поверх board.json,
    самоподача через workadult-bots не трогается, сайт подмешивает эти же
    записи к своим."""
    raw = db.reference(_VACANCIES_REF).get() or {}
    vacancies = []
    for key, rec in raw.items():
        if not isinstance(rec, dict):
            continue
        item = {f: rec.get(f, "") for f in VACANCY_FIELDS}
        item["pinned"] = bool(rec.get("pinned"))
        item["date"] = rec.get("date", "")
        vacancies.append(item)
    return jsonify({"ok": True, "vacancies": vacancies})

@app.route("/api/catalog", methods=["GET"])
def api_catalog():
    """Студии, добавленные вручную из админки — слой поверх studios.json,
    самоподача через workadult-bots (Telegram + модерация) не трогается."""
    raw = db.reference(_CATALOG_REF).get() or {}
    studios = []
    for key, rec in raw.items():
        if not isinstance(rec, dict):
            continue
        item = {f: rec.get(f, "") for f in CATALOG_FIELDS}
        item["id"] = key
        item["formats"] = [f for f in CATALOG_FORMATS if rec.get("fmt_" + f)]
        item["verified"] = bool(rec.get("verified"))
        item["premium"] = bool(rec.get("premium"))
        studios.append(item)
    return jsonify({"ok": True, "studios": studios})

@app.route("/api/click/<int:n>", methods=["POST"])
def api_click(n):
    if not (1 <= n <= SLOT_COUNT):
        return jsonify({"ok": False}), 400
    ref = db.reference(f"{_LISTINGS_REF}/{_slot_key(n)}")
    def txn(cur):
        if not cur or cur.get("status") != "active":
            return cur
        cur["clicks"] = int(cur.get("clicks", 0)) + 1
        return cur
    ref.transaction(txn)
    return jsonify({"ok": True})

# ─────────────────────────────────── админка ────────────────────────────────
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "GET":
        return render_template("login.html", error=None)
    pw = request.form.get("password", "")
    if not ADMIN_PASSWORD:
        return render_template("login.html", error="ADMIN_PASSWORD не задан на сервере")
    if secrets.compare_digest(pw, ADMIN_PASSWORD):
        session["admin_ok"] = True
        return redirect(url_for("admin_dashboard"))
    return render_template("login.html", error="Неверный пароль")

@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("admin_ok", None)
    return redirect(url_for("admin_login"))

@app.route("/admin", methods=["GET"])
@_require_admin
def admin_dashboard():
    raw = db.reference(_LISTINGS_REF).get() or {}
    pricing = _get_pricing()
    slots = []
    now = time.time()
    for n in range(1, SLOT_COUNT + 1):
        rec = raw.get(_slot_key(n)) or {}
        eff_tier, eff_price = ("regular", None)
        boost_days_left = None
        if rec:
            eff_tier, eff_price = _effective_boost(rec)
            exp = rec.get("boost_expires_at")
            if exp and eff_tier != "regular":
                boost_days_left = max(0, int((exp - now) / 86400))
        slot = {"slot": n, **rec, "boost_tier": eff_tier, "boost_price": eff_price,
               "boost_days_left": boost_days_left}
        slots.append(slot)
    total_clicks = sum(int(s.get("clicks", 0)) for s in slots)
    occupied = sum(1 for s in slots if s.get("status") == "active" and s.get("name"))

    raw_vac = db.reference(_VACANCIES_REF).get() or {}
    vacancies = []
    for key, rec in raw_vac.items():
        if not isinstance(rec, dict):
            continue
        vacancies.append({"key": key, **rec})
    # то же ТОП-5, что реально показывается на главной сайта (закреп, по дате)
    vac_top5 = sorted((v for v in vacancies if v.get("pinned")),
                      key=lambda v: v.get("date") or "", reverse=True)[:5]

    raw_cat = db.reference(_CATALOG_REF).get() or {}
    catalog_studios = []
    for key, rec in raw_cat.items():
        if not isinstance(rec, dict):
            continue
        catalog_studios.append({"key": key, **rec})

    # Рассмотрение новых: студии, уже оплаченные и ждущие публикации/правки/
    # удаления, + бесплатные вакансии/резюме, ждущие одобрения. То же самое,
    # что приходит в Telegram — тут просто дублируется веб-интерфейсом.
    raw_sub = db.reference(moderation._SUBMISSIONS_REF).get() or {}
    review_studios, review_free = [], []
    for key, rec in raw_sub.items():
        if not isinstance(rec, dict):
            continue
        item = {"key": key, **rec}
        if rec.get("type") == "studio" and rec.get("status") == "awaiting_review":
            review_studios.append(item)
        elif rec.get("type") in ("vacancy", "resume") and rec.get("status") == "pending":
            review_free.append(item)
    review_studios.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    review_free.sort(key=lambda x: x.get("created_at") or "", reverse=True)

    return render_template("dashboard.html", slots=slots,
                           total_clicks=total_clicks, occupied=occupied,
                           slot_count=SLOT_COUNT, vacancies=vacancies, vac_top5=vac_top5,
                           catalog_studios=catalog_studios, catalog_formats=CATALOG_FORMATS,
                           catalog_fmt_labels=CATALOG_FMT_LABELS, pricing=pricing,
                           boost_labels=BOOST_LABELS, boost_tiers=BOOST_TIERS,
                           studio_features=STUDIO_FEATURES, studio_feature_labels=STUDIO_FEATURE_LABELS,
                           review_studios=review_studios, review_free=review_free,
                           moderation_tier_label=moderation.TIER_LABEL)

@app.route("/admin/pricing/save", methods=["POST"])
@_require_admin
def admin_pricing_save():
    out = {}
    for k, default in PRICING_DEFAULTS.items():
        try:
            out[k] = max(0, int(request.form.get(k, default)))
        except ValueError:
            out[k] = default
    db.reference(_PRICING_REF).set(out)
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/slot/<int:n>", methods=["POST"])
@_require_admin
def admin_slot_save(n):
    if not (1 <= n <= SLOT_COUNT):
        return redirect(url_for("admin_dashboard"))

    city = request.form.get("city", "").strip()[:80]
    boost_tier = request.form.get("boost_tier", "regular").strip()
    if boost_tier not in BOOST_TIERS:
        boost_tier = "regular"

    # Публикация — всегда сразу и навсегда (админ размещает вручную, без
    # оплаты). Буст — необязательный, помесячный (+30 дней), задаётся тут же.
    pricing = _get_pricing()
    boost_expires_at = None
    boost_price = None
    if boost_tier != "regular":
        boost_expires_at = time.time() + (30 * 24 * 3600)
        boost_price = pricing[f"{boost_tier}_price"]

    photos = [p.strip()[:500] for p in request.form.getlist("photos") if p.strip()][:MAX_STUDIO_PHOTOS]
    cover_photo = request.form.get("cover_photo", "").strip()
    if cover_photo not in photos:
        cover_photo = photos[0] if photos else ""

    rec = {
        "name":            request.form.get("name", "").strip()[:120],
        "city":            city,
        "desc":            request.form.get("desc", "").strip()[:600],
        "photos":          photos,
        "cover_photo":     cover_photo,
        "photo":           cover_photo,   # обратная совместимость со старым публичным полем
        "contacts":        request.form.get("contacts", "").strip()[:200],
        "phone":           request.form.get("phone", "").strip()[:40],
        "url":             request.form.get("url", "").strip()[:300],
        "percent":         request.form.get("percent", "").strip()[:60],
        "social_telegram":  request.form.get("social_telegram", "").strip()[:120],
        "social_instagram": request.form.get("social_instagram", "").strip()[:120],
        "social_vk":        request.form.get("social_vk", "").strip()[:120],
        "status":          "active",
        "boost_tier":      boost_tier,
        "boost_expires_at": boost_expires_at,
        "boost_price":     boost_price,
    }
    picked_fmt = request.form.getlist("fmt")
    for f in CATALOG_FORMATS:
        rec["fmt_" + f] = f in picked_fmt
    picked_feat = request.form.getlist("feature")
    for f in STUDIO_FEATURES:
        rec["feat_" + f] = f in picked_feat

    ref = db.reference(f"{_LISTINGS_REF}/{_slot_key(n)}")
    existing = ref.get() or {}
    rec["clicks"] = int(existing.get("clicks", 0))
    ref.set(rec)

    old_tier = existing.get("boost_tier")
    if old_tier in ("bronze", "silver", "gold", "home") and old_tier != boost_tier:
        moderation.notify_waitlist(existing.get("city", ""), old_tier)

    return redirect(url_for("admin_dashboard"))

@app.route("/admin/slot/<int:n>/extend", methods=["POST"])
@_require_admin
def admin_slot_extend(n):
    """Продлить текущий буст ещё на 30 дней («обычное» — без буста, продлевать нечего)"""
    ref = db.reference(f"{_LISTINGS_REF}/{_slot_key(n)}")
    rec = ref.get()
    if rec and rec.get("boost_tier") and rec.get("boost_tier") != "regular":
        current = max(rec.get("boost_expires_at") or time.time(), time.time())
        rec["boost_expires_at"] = current + (30 * 24 * 3600)
        ref.set(rec)
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/slot/<int:n>/hide", methods=["POST"])
@_require_admin
def admin_slot_hide(n):
    ref = db.reference(f"{_LISTINGS_REF}/{_slot_key(n)}")
    rec = ref.get()
    if rec:
        was_active = rec.get("status") == "active"
        rec["status"] = "hidden" if was_active else "active"
        ref.set(rec)
        tier = rec.get("boost_tier")
        if was_active and tier in ("bronze", "silver", "gold", "home"):
            moderation.notify_waitlist(rec.get("city", ""), tier)
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/slot/<int:n>/delete", methods=["POST"])
@_require_admin
def admin_slot_delete(n):
    ref = db.reference(f"{_LISTINGS_REF}/{_slot_key(n)}")
    rec = ref.get()
    ref.delete()
    if rec:
        tier = rec.get("boost_tier")
        if tier in ("bronze", "silver", "gold", "home"):
            moderation.notify_waitlist(rec.get("city", ""), tier)
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/slot/<int:n>/move", methods=["POST"])
@_require_admin
def admin_slot_move(n):
    try:
        target = int(request.form.get("target", ""))
    except ValueError:
        return redirect(url_for("admin_dashboard"))
    if not (1 <= target <= SLOT_COUNT) or target == n:
        return redirect(url_for("admin_dashboard"))
    src_ref = db.reference(f"{_LISTINGS_REF}/{_slot_key(n)}")
    dst_ref = db.reference(f"{_LISTINGS_REF}/{_slot_key(target)}")
    rec = src_ref.get()
    if rec and not dst_ref.get():
        dst_ref.set(rec)
        src_ref.delete()
    return redirect(url_for("admin_dashboard"))


# ─────────────────────────── Рассмотрение новых ──────────────
# Та же очередь, что приходит в Telegram (оплаченные студии + бесплатные
# вакансии/резюме) — здесь дублируется веб-формой, действия идут через те же
# публичные функции moderation.py, так что состояние всегда согласовано вне
# зависимости от того, откуда админ нажал: из ТГ или из админки.

@app.route("/admin/review/<sub_id>/publish", methods=["POST"])
@_require_admin
def admin_review_publish(sub_id):
    moderation.publish_studio(sub_id)
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/review/<sub_id>/save", methods=["POST"])
@_require_admin
def admin_review_save(sub_id):
    ref = db.reference(f"{moderation._SUBMISSIONS_REF}/{sub_id}")
    sub = ref.get() or {}
    fields = dict(sub.get("fields") or {})
    for key in fields.keys():
        if key in request.form:
            fields[key] = request.form.get(key, "").strip()
    moderation.save_submission_fields(sub_id, fields)
    if request.form.get("publish") == "on":
        moderation.publish_studio(sub_id)
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/review/<sub_id>/delete", methods=["POST"])
@_require_admin
def admin_review_delete(sub_id):
    moderation.reject_paid(sub_id)
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/review-free/<sub_id>/approve", methods=["POST"])
@_require_admin
def admin_review_free_approve(sub_id):
    moderation.approve_free(sub_id)
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/review-free/<sub_id>/reject", methods=["POST"])
@_require_admin
def admin_review_free_reject(sub_id):
    moderation.reject_pending(sub_id)
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/vacancy/save", methods=["POST"])
@_require_admin
def admin_vacancy_save():
    """Добавить новую вакансию (без key) или отредактировать существующую (с key)."""
    key = request.form.get("key", "").strip()
    ref_root = db.reference(_VACANCIES_REF)
    existing = ref_root.child(key).get() if key else None
    rec = {
        "org":     request.form.get("org", "").strip()[:120],
        "title":   request.form.get("title", "").strip()[:120],
        "salary":  request.form.get("salary", "").strip()[:120],
        "desc":    request.form.get("desc", "").strip()[:600],
        "contact": request.form.get("contact", "").strip()[:200],
        "pinned":  request.form.get("pinned") == "on",
        "date":    (existing or {}).get("date") or datetime.now().strftime("%Y-%m-%d"),
    }
    if key and existing is not None:
        ref_root.child(key).set(rec)
    else:
        ref_root.push(rec)
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/vacancy/<key>/delete", methods=["POST"])
@_require_admin
def admin_vacancy_delete(key):
    db.reference(_VACANCIES_REF).child(key).delete()
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/catalog/save", methods=["POST"])
@_require_admin
def admin_catalog_save():
    """Добавить новую студию в каталог (без key) или отредактировать (с key)."""
    key = request.form.get("key", "").strip()
    ref_root = db.reference(_CATALOG_REF)
    rec = {
        "name":     request.form.get("name", "").strip()[:120],
        "city":     request.form.get("city", "").strip()[:80],
        "percent":  request.form.get("percent", "").strip()[:60],
        "desc":     request.form.get("desc", "").strip()[:600],
        "contact":  request.form.get("contact", "").strip()[:200],
        "verified": request.form.get("verified") == "on",
        "premium":  request.form.get("premium") == "on",
    }
    picked = request.form.getlist("fmt")
    for f in CATALOG_FORMATS:
        rec["fmt_" + f] = f in picked
    if key:
        ref_root.child(key).set(rec)
    else:
        ref_root.push(rec)
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/catalog/<key>/delete", methods=["POST"])
@_require_admin
def admin_catalog_delete(key):
    db.reference(_CATALOG_REF).child(key).delete()
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/api/traffic")
@_require_admin
def admin_api_traffic():
    return jsonify(_site_traffic())

@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})

@app.route("/tg/fix-pricing", methods=["POST"])
def _tmp_fix_pricing():
    """ВРЕМЕННО: сбрасывает /workadult_pricing на правильные текущие тарифы
    (в Firebase застряли значения со старой схемы gold/silver/bronze/regular).
    Убрать после использования."""
    if request.args.get("key") != os.environ.get("WA_WEBHOOK_SECRET", "").strip():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    db.reference(_PRICING_REF).set(dict(PRICING_DEFAULTS))
    return jsonify({"ok": True, "pricing": PRICING_DEFAULTS})

moderation.init_app(app, get_pricing=_get_pricing, listings_ref=_LISTINGS_REF,
                     vacancies_ref=_VACANCIES_REF, slot_key=_slot_key, slot_count=SLOT_COUNT)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
