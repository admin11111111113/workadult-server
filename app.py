# -*- coding: utf-8 -*-
"""workadult.pro — админка для размещения студий. 100 мест, ручной CRUD через /admin."""
import json
import os
import secrets
from functools import wraps

import firebase_admin
from firebase_admin import credentials, db
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from flask_cors import CORS

SLOT_COUNT = 100
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()
SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "").strip() or secrets.token_hex(32)

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

# Места 1-10 = $100/мес, 11-100 = $25/мес
def _slot_price(n):
    return 100 if n <= 10 else 25

PUBLIC_FIELDS = ("name", "city", "desc", "photo", "contacts", "url")


def _slot_key(n):
    return f"slot_{int(n):03d}"


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
    out = []
    for n in range(1, SLOT_COUNT + 1):
        rec = raw.get(_slot_key(n))
        if not rec or rec.get("status") != "active":
            out.append({"slot": n, "price": _slot_price(n), "listing": None})
            continue
        out.append({"slot": n, "price": _slot_price(n),
                    "listing": {k: rec.get(k, "") for k in PUBLIC_FIELDS}})
    return jsonify({"ok": True, "slots": out})


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
    slots = []
    for n in range(1, SLOT_COUNT + 1):
        rec = raw.get(_slot_key(n)) or {}
        slots.append({"slot": n, "price": _slot_price(n), **rec})
    total_clicks = sum(int(s.get("clicks", 0)) for s in slots)
    occupied = sum(1 for s in slots if s.get("status") == "active")
    return render_template("dashboard.html", slots=slots,
                           total_clicks=total_clicks, occupied=occupied,
                           slot_count=SLOT_COUNT)


@app.route("/admin/slot/<int:n>", methods=["POST"])
@_require_admin
def admin_slot_save(n):
    if not (1 <= n <= SLOT_COUNT):
        return redirect(url_for("admin_dashboard"))
    rec = {
        "name":     request.form.get("name", "").strip()[:120],
        "city":     request.form.get("city", "").strip()[:80],
        "desc":     request.form.get("desc", "").strip()[:600],
        "photo":    request.form.get("photo", "").strip()[:500],
        "contacts": request.form.get("contacts", "").strip()[:200],
        "url":      request.form.get("url", "").strip()[:300],
        "status":   "active",
    }
    ref = db.reference(f"{_LISTINGS_REF}/{_slot_key(n)}")
    existing = ref.get() or {}
    rec["clicks"] = int(existing.get("clicks", 0))
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


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
