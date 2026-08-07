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

# Тарифы задаются в админке (вкладка «Обзор») и хранятся в Firebase — эти
# значения только запасной вариант, если настройки ещё не сохранялись.
PRICING_DEFAULTS = {"premium_price": 100, "regular_price": 25, "premium_per_city": 5}

def _get_pricing():
    raw = db.reference(_PRICING_REF).get() or {}
    out = dict(PRICING_DEFAULTS)
    for k in out:
        try:
            out[k] = max(0, int(raw.get(k, out[k])))
        except (TypeError, ValueError):
            pass
    return out

PUBLIC_FIELDS = ("name", "city", "desc", "photo", "contacts", "url")

def _slot_key(n):
    return f"slot_{int(n):03d}"

def _is_expired(rec):
    """Проверяет истёк ли срок размещения"""
    expires_at = rec.get("expires_at")
    if not expires_at:
        return False
    return time.time() > expires_at

def _city_occupied_count(raw, city, exclude_slot=None):
    """Сколько мест в этом городе уже куплено (active/hidden — кроме exclude_slot).
    Нужно чтобы понять, каким по счёту в городе становится новое размещение."""
    count = 0
    for n in range(1, SLOT_COUNT + 1):
        if n == exclude_slot:
            continue
        rec = raw.get(_slot_key(n))
        if rec and rec.get("name") and rec.get("status") in ("active", "hidden") and rec.get("city") == city:
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
        # Скрываем если статус не active или срок истёк
        if not rec or rec.get("status") != "active" or _is_expired(rec):
            out.append({"slot": n, "price": pricing["regular_price"], "listing": None})
            continue
        # цена фиксируется в момент размещения (см. admin_slot_save) — так
        # смена тарифа в админке не задним числом меняет уже купленные места
        price = rec.get("price", pricing["regular_price"])
        premium = rec.get("premium", False)
        listing = {k: rec.get(k, "") for k in PUBLIC_FIELDS}
        listing["expires_at"] = rec.get("expires_at")
        listing["premium"] = premium
        out.append({"slot": n, "price": price, "listing": listing})
    return jsonify({"ok": True, "slots": out})

@app.route("/api/featured", methods=["GET"])
def api_featured():
    """Премиум-места (топ N в каждом городе — см. тарифы в админке) для ротации на главной"""
    raw = db.reference(_LISTINGS_REF).get() or {}
    featured = []
    for n in range(1, SLOT_COUNT + 1):
        rec = raw.get(_slot_key(n))
        if rec and rec.get("status") == "active" and not _is_expired(rec) and rec.get("premium"):
            listing = {k: rec.get(k, "") for k in PUBLIC_FIELDS}
            listing["slot"] = n
            featured.append(listing)
    return jsonify({"ok": True, "featured": featured})

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
        # Вычисляем дней осталось
        expires_at = rec.get("expires_at")
        days_left = None
        expired = False
        if expires_at:
            diff = expires_at - now
            if diff < 0:
                expired = True
                days_left = 0
            else:
                days_left = int(diff / 86400)
        slot = {"slot": n, "price": pricing["regular_price"], "premium": False,
               "days_left": days_left, "expired": expired, **rec}
        slots.append(slot)
    total_clicks = sum(int(s.get("clicks", 0)) for s in slots)
    occupied = sum(1 for s in slots if s.get("status") == "active" and not s.get("expired"))

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

    return render_template("dashboard.html", slots=slots,
                           total_clicks=total_clicks, occupied=occupied,
                           slot_count=SLOT_COUNT, vacancies=vacancies, vac_top5=vac_top5,
                           catalog_studios=catalog_studios, catalog_formats=CATALOG_FORMATS,
                           catalog_fmt_labels=CATALOG_FMT_LABELS, pricing=pricing)

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

    # Автоматически ставим срок 30 дней
    expires_at = time.time() + (30 * 24 * 3600)
    city = request.form.get("city", "").strip()[:80]

    # Цена/премиум считаются по месту В ГОРОДЕ: топ-N уже занятых мест в этом
    # городе (N и цены — из тарифов в админке) идут по премиум-цене, остальные
    # по обычной. Считаем на момент сохранения — так правки тарифов в
    # настройках применяются к новым и пересохранённым местам.
    raw = db.reference(_LISTINGS_REF).get() or {}
    pricing = _get_pricing()
    rank = _city_occupied_count(raw, city, exclude_slot=n) + 1
    premium = rank <= pricing["premium_per_city"]

    rec = {
        "name":       request.form.get("name", "").strip()[:120],
        "city":       city,
        "desc":       request.form.get("desc", "").strip()[:600],
        "photo":      request.form.get("photo", "").strip()[:500],
        "contacts":   request.form.get("contacts", "").strip()[:200],
        "url":        request.form.get("url", "").strip()[:300],
        "status":     "active",
        "expires_at": expires_at,
        "premium":    premium,
        "price":      pricing["premium_price"] if premium else pricing["regular_price"],
    }
    ref = db.reference(f"{_LISTINGS_REF}/{_slot_key(n)}")
    existing = raw.get(_slot_key(n)) or {}
    rec["clicks"] = int(existing.get("clicks", 0))
    # Если продлеваем — сохраняем старые клики
    ref.set(rec)
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/slot/<int:n>/extend", methods=["POST"])
@_require_admin
def admin_slot_extend(n):
    """Продлить на ещё 30 дней"""
    ref = db.reference(f"{_LISTINGS_REF}/{_slot_key(n)}")
    rec = ref.get()
    if rec:
        current = max(rec.get("expires_at", time.time()), time.time())
        rec["expires_at"] = current + (30 * 24 * 3600)
        rec["status"] = "active"
        ref.set(rec)
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/slot/<int:n>/hide", methods=["POST"])
@_require_admin
def admin_slot_hide(n):
    ref = db.reference(f"{_LISTINGS_REF}/{_slot_key(n)}")
    rec = ref.get()
    if rec:
        rec["status"] = "hidden" if rec.get("status") == "active" else "active"
        ref.set(rec)
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/slot/<int:n>/delete", methods=["POST"])
@_require_admin
def admin_slot_delete(n):
    db.reference(f"{_LISTINGS_REF}/{_slot_key(n)}").delete()
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
