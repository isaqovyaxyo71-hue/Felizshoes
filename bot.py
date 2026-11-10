"""
Feliz Shoes - ayollar poyabzali Telegram do'koni boti
aiogram 3 + SQLite. Owner paneli deyarli to'liq tugmalar orqali ishlaydi.
"""
import asyncio
import json
import logging
import os
from datetime import datetime
from html import escape as esc

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import BaseFilter, Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton as IB,
    InlineKeyboardMarkup as IM,
    KeyboardButton,
    Message,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DB = os.getenv("DB_PATH", "feliz.db")
SHOP = "Feliz shoes"

STATUS = {
    "new": "🆕 Yangi",
    "confirmed": "✅ Tasdiqlangan",
    "shipped": "🚚 Pochtaga topshirildi",
    "delivered": "📦 Yetkazildi",
    "canceled": "❌ Bekor qilindi",
}

DEFAULTS = {
    "contact_username": "",
    "phone": "",
    "delivery_price": "30000",
    "free_from": "0",
    "work_hours": "09:00 - 21:00",
    "shop_open": "1",
    "welcome": "👠 Feliz shoes botiga xush kelibsiz!\n\nEng sara ayollar poyabzallari shu yerda. Katalogni ko'ring va qulay buyurtma bering.",
    "about": "Feliz shoes - sifatli va zamonaviy ayollar poyabzallari. Butun O'zbekiston bo'ylab pochta orqali yetkazamiz. To'lov: yetkazilganda naqd.",
}
SET_META = {
    "contact_username": "👤 Username (mijoz yozadi)",
    "phone": "📞 Telefon raqam",
    "delivery_price": "🚚 Yetkazish narxi",
    "free_from": "🎁 Bepul yetkazish (shu summadan)",
    "work_hours": "🕘 Ish vaqti",
    "welcome": "👋 Salomlashuv matni",
    "about": "ℹ️ Biz haqimizda matni",
}
NUMERIC = {"delivery_price", "free_from"}


# ───────────── Baza ─────────────
async def q(sql, args=(), one=False, commit=False):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, args)
        if commit:
            await db.commit()
            return cur.lastrowid
        return await (cur.fetchone() if one else cur.fetchall())


async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, username TEXT);
            CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS categories(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT);
            CREATE TABLE IF NOT EXISTS products(
                id INTEGER PRIMARY KEY AUTOINCREMENT, cat_id INTEGER, name TEXT, price INTEGER,
                sizes TEXT DEFAULT '', colors TEXT DEFAULT '', desc TEXT DEFAULT '',
                photos TEXT DEFAULT '[]', active INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS cart(
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, product_id INTEGER,
                size TEXT, qty INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS orders(
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, name TEXT,
                phone TEXT, address TEXT, items TEXT, subtotal INTEGER, fee INTEGER, total INTEGER,
                status TEXT DEFAULT 'new', created TEXT);
            """
        )
        await db.commit()
    if not await q("SELECT 1 FROM categories LIMIT 1", one=True):
        for n in ("Tufli", "Krossovka", "Sandal", "Botinka"):
            await q("INSERT INTO categories(name) VALUES(?)", (n,), commit=True)


async def get_set(key):
    r = await q("SELECT value FROM settings WHERE key=?", (key,), one=True)
    return r["value"] if r else DEFAULTS.get(key, "")


async def put_set(key, value):
    await q(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
        commit=True,
    )


async def admin_ids():
    rows = await q("SELECT id FROM admins")
    return [OWNER_ID] + [r["id"] for r in rows if r["id"] != OWNER_ID]


async def is_admin(uid):
    return uid == OWNER_ID or bool(await q("SELECT 1 FROM admins WHERE id=?", (uid,), one=True))


def money(n):
    return f"{int(n):,}".replace(",", " ") + " so'm"


def digits(t):
    d = "".join(c for c in t if c.isdigit())
    return int(d) if d else None


def split_sizes(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


async def edit_or_send(msg: Message, text, kb=None):
    try:
        await msg.edit_text(text, reply_markup=kb)
    except Exception:
        try:
            await msg.delete()
        except Exception:
            pass
        await msg.answer(text, reply_markup=kb)


async def send_card(msg: Message, text, photos, kb, delete=True):
    if delete:
        try:
            await msg.delete()
        except Exception:
            pass
    if photos:
        await msg.answer_photo(photos[0], caption=text, reply_markup=kb)
    else:
        await msg.answer(text, reply_markup=kb)


def main_kb(admin=False):
    b = ReplyKeyboardBuilder()
    b.row(KeyboardButton(text="🛍 Katalog"), KeyboardButton(text="🛒 Savatcha"))
    b.row(KeyboardButton(text="📦 Buyurtmalarim"), KeyboardButton(text="☎️ Bog'lanish"))
    b.row(KeyboardButton(text="ℹ️ Biz haqimizda"))
    if admin:
        b.row(KeyboardButton(text="⚙️ Admin panel"))
    return b.as_markup(resize_keyboard=True)


async def contact_markup():
    u = (await get_set("contact_username")).lstrip("@")
    return IM(inline_keyboard=[[IB(text="💬 Egasiga yozish", url=f"https://t.me/{u}")]]) if u else None


# ───────────── Holatlar ─────────────
class Order(StatesGroup):
    name = State()
    phone = State()
    address = State()
    confirm = State()


class AddP(StatesGroup):
    name = State()
    price = State()
    sizes = State()
    colors = State()
    desc = State()
    photos = State()


class EditP(StatesGroup):
    value = State()
    photos = State()


class SetS(StatesGroup):
    value = State()


class CatS(StatesGroup):
    name = State()


class Bc(StatesGroup):
    msg = State()


class AdmS(StatesGroup):
    id = State()


class IsAdmin(BaseFilter):
    async def __call__(self, event) -> bool:
        return await is_admin(event.from_user.id)


r = Router()  # mijoz
a = Router()  # owner / admin
fb = Router()  # zaxira
a.message.filter(IsAdmin())
a.callback_query.filter(IsAdmin())


# ═════════════ MIJOZ QISMI ═════════════
@r.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    u = m.from_user
    await q(
        "INSERT INTO users(id,name,username) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE "
        "SET name=excluded.name, username=excluded.username",
        (u.id, u.full_name, u.username),
        commit=True,
    )
    await m.answer(esc(await get_set("welcome")), reply_markup=main_kb(await is_admin(u.id)))


@r.message(F.text == "ℹ️ Biz haqimizda")
async def about(m: Message, state: FSMContext):
    await state.clear()
    t = f"<b>{SHOP}</b>\n\n{esc(await get_set('about'))}\n\n🕘 Ish vaqti: {esc(await get_set('work_hours'))}"
    await m.answer(t, reply_markup=await contact_markup())


@r.message(F.text == "☎️ Bog'lanish")
async def contact(m: Message, state: FSMContext):
    await state.clear()
    phone = await get_set("phone")
    t = "☎️ <b>Bog'lanish</b>\n"
    if phone:
        t += f"\n📞 {esc(phone)}"
    t += f"\n🕘 {esc(await get_set('work_hours'))}"
    await m.answer(t, reply_markup=await contact_markup())


async def show_cats(msg: Message, edit=False):
    cats = await q(
        "SELECT * FROM categories c WHERE EXISTS(SELECT 1 FROM products p WHERE p.cat_id=c.id AND p.active=1) ORDER BY id"
    )
    if not cats:
        text, kb = "Hozircha mahsulotlar qo'shilmagan. Tez orada! 🙌", None
    else:
        text = "👠 Kategoriyani tanlang:"
        kb = IM(inline_keyboard=[[IB(text=c["name"], callback_data=f"cat:{c['id']}")] for c in cats])
    if edit:
        await edit_or_send(msg, text, kb)
    else:
        await msg.answer(text, reply_markup=kb)


@r.message(F.text == "🛍 Katalog")
async def catalog(m: Message, state: FSMContext):
    await state.clear()
    await show_cats(m)


@r.callback_query(F.data == "home")
async def home_cb(cb):
    await show_cats(cb.message, edit=True)
    await cb.answer()


@r.callback_query(F.data.startswith("cat:"))
async def cat_cb(cb):
    cid = int(cb.data[4:])
    ps = await q("SELECT * FROM products WHERE cat_id=? AND active=1 ORDER BY id DESC", (cid,))
    rows = [[IB(text=f"{p['name']} — {money(p['price'])}", callback_data=f"p:{p['id']}")] for p in ps]
    rows.append([IB(text="⬅️ Orqaga", callback_data="home")])
    await edit_or_send(cb.message, "Modelni tanlang:", IM(inline_keyboard=rows))
    await cb.answer()


def product_text(p, admin=False):
    t = f"<b>{esc(p['name'])}</b>\n\n💰 Narx: <b>{money(p['price'])}</b>"
    if p["sizes"]:
        t += f"\n📏 O'lchamlar: {esc(p['sizes'])}"
    if p["colors"]:
        t += f"\n🎨 Ranglar: {esc(p['colors'])}"
    if p["desc"]:
        t += f"\n\n{esc(p['desc'])}"
    if admin:
        t += "\n\n" + ("✅ Sotuvda" if p["active"] else "⛔ Tugagan (mijozga ko'rinmaydi)")
    return t


@r.callback_query(F.data.startswith("p:"))
async def prod_cb(cb):
    pid = int(cb.data[2:])
    p = await q("SELECT * FROM products WHERE id=?", (pid,), one=True)
    if not p or not p["active"]:
        return await cb.answer("Bu mahsulot hozir mavjud emas", show_alert=True)
    sizes = split_sizes(p["sizes"])
    rows = []
    text = product_text(p)
    if sizes:
        text += "\n\n👇 Savatga qo'shish uchun o'lchamni tanlang"
        btns = [IB(text=s, callback_data=f"s:{pid}:{s}") for s in sizes]
        rows += [btns[i : i + 4] for i in range(0, len(btns), 4)]
    else:
        rows.append([IB(text="🛒 Savatga qo'shish", callback_data=f"s:{pid}:-")])
    photos = json.loads(p["photos"])
    if len(photos) > 1:
        rows.append([IB(text=f"🖼 Barcha rasmlar ({len(photos)})", callback_data=f"ph:{pid}")])
    rows.append([IB(text="⬅️ Orqaga", callback_data=f"cat:{p['cat_id']}")])
    await send_card(cb.message, text, photos, IM(inline_keyboard=rows))
    await cb.answer()


@r.callback_query(F.data.startswith("ph:"))
async def photos_cb(cb):
    from aiogram.types import InputMediaPhoto

    p = await q("SELECT photos FROM products WHERE id=?", (int(cb.data[3:]),), one=True)
    if p:
        ph = json.loads(p["photos"])[:10]
        await cb.message.answer_media_group([InputMediaPhoto(media=x) for x in ph])
    await cb.answer()


@r.callback_query(F.data.startswith("s:"))
async def add_cart(cb):
    _, pid, size = cb.data.split(":", 2)
    pid = int(pid)
    ex = await q(
        "SELECT id FROM cart WHERE user_id=? AND product_id=? AND size=?", (cb.from_user.id, pid, size), one=True
    )
    if ex:
        await q("UPDATE cart SET qty=qty+1 WHERE id=?", (ex["id"],), commit=True)
    else:
        await q(
            "INSERT INTO cart(user_id,product_id,size,qty) VALUES(?,?,?,1)", (cb.from_user.id, pid, size), commit=True
        )
    await cb.answer("✅ Savatga qo'shildi")
    kb = IM(
        inline_keyboard=[
            [IB(text="🛒 Savatcha", callback_data="cart")],
            [IB(text="🛍 Xaridni davom ettirish", callback_data="home")],
        ]
    )
    await cb.message.answer("✅ Savatga qo'shildi", reply_markup=kb)


# ── Savatcha ──
async def calc(uid):
    rows = await q(
        "SELECT c.id cid,c.size,c.qty,p.id pid,p.name,p.price FROM cart c "
        "JOIN products p ON p.id=c.product_id WHERE c.user_id=? AND p.active=1",
        (uid,),
    )
    sub = sum(x["price"] * x["qty"] for x in rows)
    free = int(await get_set("free_from") or 0)
    fee = 0 if (free and sub >= free) else int(await get_set("delivery_price") or 0)
    return rows, sub, fee


async def show_cart(msg: Message, uid, edit=True):
    rows, sub, fee = await calc(uid)
    if not rows:
        text, kb = "🛒 Savatchangiz bo'sh", IM(inline_keyboard=[[IB(text="🛍 Katalog", callback_data="home")]])
    else:
        text = "🛒 <b>Savatcha</b>\n\n"
        kbr = []
        for i, x in enumerate(rows, 1):
            sz = f" ({esc(x['size'])})" if x["size"] != "-" else ""
            text += f"{i}. {esc(x['name'])}{sz}\n    {x['qty']} × {money(x['price'])}\n"
            kbr.append(
                [
                    IB(text="➖", callback_data=f"c:-:{x['cid']}"),
                    IB(text=f"{i}. {x['qty']} dona", callback_data="noop"),
                    IB(text="➕", callback_data=f"c:+:{x['cid']}"),
                    IB(text="❌", callback_data=f"c:x:{x['cid']}"),
                ]
            )
        text += f"\nMahsulotlar: {money(sub)}\n🚚 Yetkazish: {'bepul' if fee == 0 else money(fee)}\n<b>Jami: {money(sub + fee)}</b>\n💵 To'lov: yetkazilganda naqd"
        kbr.append([IB(text="✅ Buyurtma berish", callback_data="c:order")])
        kbr.append([IB(text="🗑 Tozalash", callback_data="c:clear"), IB(text="🛍 Davom etish", callback_data="home")])
        kb = IM(inline_keyboard=kbr)
    if edit:
        await edit_or_send(msg, text, kb)
    else:
        await msg.answer(text, reply_markup=kb)


@r.message(F.text == "🛒 Savatcha")
async def cart_msg(m: Message, state: FSMContext):
    await state.clear()
    await show_cart(m, m.from_user.id, edit=False)


@r.callback_query(F.data == "cart")
async def cart_cb(cb):
    await show_cart(cb.message, cb.from_user.id)
    await cb.answer()


@r.callback_query(F.data == "noop")
async def noop(cb):
    await cb.answer()


@r.callback_query(F.data.startswith("c:"))
async def cart_action(cb, state: FSMContext):
    parts = cb.data.split(":")
    act = parts[1]
    uid = cb.from_user.id
    if act == "clear":
        await q("DELETE FROM cart WHERE user_id=?", (uid,), commit=True)
    elif act in ("+", "-", "x"):
        cid = int(parts[2])
        if act == "+":
            await q("UPDATE cart SET qty=qty+1 WHERE id=? AND user_id=?", (cid, uid), commit=True)
        elif act == "-":
            await q("UPDATE cart SET qty=qty-1 WHERE id=? AND user_id=?", (cid, uid), commit=True)
            await q("DELETE FROM cart WHERE qty<=0 AND user_id=?", (uid,), commit=True)
        else:
            await q("DELETE FROM cart WHERE id=? AND user_id=?", (cid, uid), commit=True)
    elif act == "order":
        if await get_set("shop_open") != "1":
            return await cb.answer("Hozir buyurtma qabul qilinmayapti. Keyinroq urinib ko'ring.", show_alert=True)
        rows, _, _ = await calc(uid)
        if not rows:
            return await cb.answer("Savatcha bo'sh", show_alert=True)
        await state.set_state(Order.name)
        await cb.message.answer("Ismingizni yozing:", reply_markup=ReplyKeyboardRemove())
        return await cb.answer()
    await show_cart(cb.message, uid)
    await cb.answer()


@r.message(StateFilter(Order.name), F.text)
async def order_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text.strip()[:60])
    kb = ReplyKeyboardBuilder()
    kb.row(KeyboardButton(text="📱 Raqamni yuborish", request_contact=True))
    await state.set_state(Order.phone)
    await m.answer(
        "Telefon raqamingizni yuboring (tugma bosing yoki +998901234567 shaklida yozing):",
        reply_markup=kb.as_markup(resize_keyboard=True, one_time_keyboard=True),
    )


@r.message(StateFilter(Order.phone))
async def order_phone(m: Message, state: FSMContext):
    phone = m.contact.phone_number if m.contact else (m.text or "")
    d = digits(phone)
    if not d or len(str(d)) < 9:
        return await m.answer("Raqam noto'g'ri. Qaytadan yuboring:")
    phone = "+" + str(d) if not phone.startswith("+") else phone
    await state.update_data(phone=phone)
    await state.set_state(Order.address)
    await m.answer(
        "📍 Yetkazish manzilini yozing:\nviloyat, tuman/shahar, ko'cha yoki pochta bo'limi",
        reply_markup=ReplyKeyboardRemove(),
    )


@r.message(StateFilter(Order.address), F.text)
async def order_address(m: Message, state: FSMContext):
    await state.update_data(address=m.text.strip()[:300])
    d = await state.get_data()
    rows, sub, fee = await calc(m.from_user.id)
    if not rows:
        await state.clear()
        return await m.answer("Savatcha bo'sh.", reply_markup=main_kb(await is_admin(m.from_user.id)))
    t = (
        f"📝 <b>Buyurtmani tekshiring</b>\n\n👤 {esc(d['name'])}\n📞 {esc(d['phone'])}\n📍 {esc(m.text)}\n\n"
        + "\n".join(
            f"• {esc(x['name'])}{'' if x['size']=='-' else ' ('+esc(x['size'])+')'} — {x['qty']} × {money(x['price'])}"
            for x in rows
        )
        + f"\n\n🚚 Yetkazish: {'bepul' if fee == 0 else money(fee)}\n<b>Jami: {money(sub + fee)}</b>\n💵 Yetkazilganda naqd to'lanadi"
    )
    kb = IM(
        inline_keyboard=[
            [IB(text="✅ Tasdiqlash", callback_data="o:ok"), IB(text="❌ Bekor qilish", callback_data="o:no")]
        ]
    )
    await state.set_state(Order.confirm)
    await m.answer(t, reply_markup=kb)


def order_text(o):
    items = json.loads(o["items"])
    lines = "\n".join(
        f"• {esc(i['name'])}{'' if i['size']=='-' else ' ('+esc(i['size'])+')'} — {i['qty']} × {money(i['price'])}"
        for i in items
    )
    un = f" (@{o['username']})" if o["username"] else ""
    return (
        f"🧾 <b>Buyurtma №{o['id']}</b> · {STATUS[o['status']]}\n🕒 {o['created']}\n\n"
        f"👤 <a href=\"tg://user?id={o['user_id']}\">{esc(o['name'])}</a>{un}\n📞 {esc(o['phone'])}\n📍 {esc(o['address'])}\n\n"
        f"{lines}\n\n🚚 Yetkazish: {money(o['fee'])}\n<b>Jami: {money(o['total'])}</b> (naqd)"
    )


def order_kb(oid):
    return IM(
        inline_keyboard=[
            [
                IB(text="✅ Tasdiqlash", callback_data=f"os:{oid}:confirmed"),
                IB(text="🚚 Yuborildi", callback_data=f"os:{oid}:shipped"),
            ],
            [
                IB(text="📦 Yetkazildi", callback_data=f"os:{oid}:delivered"),
                IB(text="❌ Bekor", callback_data=f"os:{oid}:canceled"),
            ],
        ]
    )


@r.callback_query(StateFilter(Order.confirm), F.data.in_({"o:ok", "o:no"}))
async def order_confirm(cb, state: FSMContext):
    uid = cb.from_user.id
    adm = await is_admin(uid)
    if cb.data == "o:no":
        await state.clear()
        await edit_or_send(cb.message, "❌ Buyurtma bekor qilindi.")
        await cb.message.answer("Menyu 👇", reply_markup=main_kb(adm))
        return await cb.answer()
    d = await state.get_data()
    rows, sub, fee = await calc(uid)
    if not rows:
        await state.clear()
        return await cb.answer("Savatcha bo'sh", show_alert=True)
    items = [{"pid": x["pid"], "name": x["name"], "size": x["size"], "qty": x["qty"], "price": x["price"]} for x in rows]
    oid = await q(
        "INSERT INTO orders(user_id,username,name,phone,address,items,subtotal,fee,total,status,created) "
        "VALUES(?,?,?,?,?,?,?,?,?,'new',?)",
        (uid, cb.from_user.username, d["name"], d["phone"], d["address"], json.dumps(items, ensure_ascii=False),
         sub, fee, sub + fee, datetime.now().strftime("%d.%m.%Y %H:%M")),
        commit=True,
    )
    await q("DELETE FROM cart WHERE user_id=?", (uid,), commit=True)
    await state.clear()
    o = await q("SELECT * FROM orders WHERE id=?", (oid,), one=True)
    for aid in await admin_ids():
        try:
            await cb.bot.send_message(aid, "🔔 <b>YANGI BUYURTMA!</b>\n\n" + order_text(o), reply_markup=order_kb(oid))
        except Exception as e:
            logging.warning("Adminga yuborib bo'lmadi %s: %s", aid, e)
    await edit_or_send(cb.message, f"✅ <b>Buyurtmangiz qabul qilindi!</b>\n№{oid}\n\nTez orada operator siz bilan bog'lanadi.")
    await cb.message.answer("Rahmat! 💖", reply_markup=main_kb(adm))
    ck = await contact_markup()
    if ck:
        await cb.message.answer("Savol bo'lsa egasiga yozing:", reply_markup=ck)
    await cb.answer()


@r.message(F.text == "📦 Buyurtmalarim")
async def my_orders(m: Message, state: FSMContext):
    await state.clear()
    rows = await q("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 8", (m.from_user.id,))
    if not rows:
        return await m.answer("Sizda hali buyurtmalar yo'q.")
    t = "📦 <b>Buyurtmalaringiz</b>\n\n" + "\n".join(
        f"№{o['id']} · {o['created']} · {money(o['total'])}\n   {STATUS[o['status']]}" for o in rows
    )
    await m.answer(t)


# ═════════════ OWNER / ADMIN QISMI ═════════════
def admin_menu(owner=False):
    rows = [
        [IB(text="➕ Mahsulot qo'shish", callback_data="a:addp"), IB(text="📋 Mahsulotlar", callback_data="a:plist")],
        [IB(text="📦 Buyurtmalar", callback_data="a:orders"), IB(text="📊 Statistika", callback_data="a:stats")],
        [IB(text="🗂 Kategoriyalar", callback_data="a:cats"), IB(text="📢 E'lon yuborish", callback_data="a:bc")],
        [IB(text="⚙️ Sozlamalar", callback_data="a:set")],
    ]
    if owner:
        rows.append([IB(text="👥 Adminlar", callback_data="a:adm")])
    return IM(inline_keyboard=rows)


BACK = [IB(text="⬅️ Admin menyu", callback_data="a:home")]


async def panel_text():
    n = await q("SELECT COUNT(*) c FROM orders WHERE status='new'", one=True)
    shop = "🟢 ochiq" if await get_set("shop_open") == "1" else "🔴 yopiq"
    return f"⚙️ <b>{SHOP} · Admin panel</b>\n\nDo'kon: {shop}\n🆕 Yangi buyurtmalar: <b>{n['c']}</b>"


@a.message(Command("admin"))
@a.message(F.text == "⚙️ Admin panel")
async def admin_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(await panel_text(), reply_markup=admin_menu(m.from_user.id == OWNER_ID))


@a.callback_query(F.data == "a:home")
async def admin_home(cb, state: FSMContext):
    await state.clear()
    await edit_or_send(cb.message, await panel_text(), admin_menu(cb.from_user.id == OWNER_ID))
    await cb.answer()


# ── Kategoriyalar ──
@a.callback_query(F.data == "a:cats")
async def cats_view(cb):
    cats = await q("SELECT * FROM categories ORDER BY id")
    rows = [[IB(text=f"🗑 {c['name']}", callback_data=f"a:cdel:{c['id']}")] for c in cats]
    rows.append([IB(text="➕ Kategoriya qo'shish", callback_data="a:cadd")])
    rows.append(BACK)
    await edit_or_send(cb.message, "🗂 <b>Kategoriyalar</b>\nO'chirish uchun nomini bosing:", IM(inline_keyboard=rows))
    await cb.answer()


@a.callback_query(F.data == "a:cadd")
async def cat_add(cb, state: FSMContext):
    await state.set_state(CatS.name)
    await cb.message.answer("Yangi kategoriya nomini yozing:")
    await cb.answer()


@a.message(StateFilter(CatS.name), F.text)
async def cat_add_name(m: Message, state: FSMContext):
    await q("INSERT INTO categories(name) VALUES(?)", (m.text.strip()[:40],), commit=True)
    await state.clear()
    await m.answer("✅ Kategoriya qo'shildi", reply_markup=IM(inline_keyboard=[[IB(text="🗂 Kategoriyalar", callback_data="a:cats")], BACK]))


@a.callback_query(F.data.startswith("a:cdel:"))
async def cat_del(cb):
    cid = int(cb.data.split(":")[2])
    if await q("SELECT 1 FROM products WHERE cat_id=?", (cid,), one=True):
        return await cb.answer("Bu kategoriyada mahsulotlar bor. Avval ularni o'chiring yoki ko'chiring.", show_alert=True)
    await q("DELETE FROM categories WHERE id=?", (cid,), commit=True)
    await cats_view(cb)


# ── Mahsulot qo'shish ──
@a.callback_query(F.data == "a:addp")
async def addp_start(cb, state: FSMContext):
    await state.clear()
    cats = await q("SELECT * FROM categories ORDER BY id")
    if not cats:
        return await cb.answer("Avval kategoriya qo'shing", show_alert=True)
    rows = [[IB(text=c["name"], callback_data=f"a:ac:{c['id']}")] for c in cats] + [BACK]
    await edit_or_send(cb.message, "➕ Qaysi kategoriyaga qo'shamiz?", IM(inline_keyboard=rows))
    await cb.answer()


@a.callback_query(F.data.startswith("a:ac:"))
async def addp_cat(cb, state: FSMContext):
    await state.update_data(cat=int(cb.data.split(":")[2]), photos=[])
    await state.set_state(AddP.name)
    await cb.message.answer("1/6 · Model nomini yozing:\n(masalan: Klassik tufli Rose)")
    await cb.answer()


@a.message(StateFilter(AddP.name), F.text)
async def addp_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text.strip()[:80])
    await state.set_state(AddP.price)
    await m.answer("2/6 · Narxini yozing (so'mda):\nmasalan: 189000")


@a.message(StateFilter(AddP.price), F.text)
async def addp_price(m: Message, state: FSMContext):
    p = digits(m.text)
    if not p:
        return await m.answer("Faqat raqam yozing, masalan 189000")
    await state.update_data(price=p)
    await state.set_state(AddP.sizes)
    await m.answer("3/6 · O'lchamlarni vergul bilan yozing:\nmasalan: 36,37,38,39,40\n(yo'q bo'lsa - yuboring)")


@a.message(StateFilter(AddP.sizes), F.text)
async def addp_sizes(m: Message, state: FSMContext):
    s = "" if m.text.strip() == "-" else ", ".join(split_sizes(m.text))
    await state.update_data(sizes=s)
    await state.set_state(AddP.colors)
    await m.answer("4/6 · Ranglarni yozing:\nmasalan: qora, oq, bej\n(yo'q bo'lsa - yuboring)")


@a.message(StateFilter(AddP.colors), F.text)
async def addp_colors(m: Message, state: FSMContext):
    await state.update_data(colors="" if m.text.strip() == "-" else m.text.strip()[:100])
    await state.set_state(AddP.desc)
    await m.answer("5/6 · Qisqa tavsif yozing (material, poshna balandligi...)\n(yo'q bo'lsa - yuboring)")


@a.message(StateFilter(AddP.desc), F.text)
async def addp_desc(m: Message, state: FSMContext):
    await state.update_data(desc="" if m.text.strip() == "-" else m.text.strip()[:600])
    await state.set_state(AddP.photos)
    await m.answer("6/6 · Rasmlarni yuboring (bir nechta bo'lishi mumkin). Tugagach «Tayyor» ni bosing 👇")


def done_kb(cb_data):
    return IM(inline_keyboard=[[IB(text="✅ Tayyor", callback_data=cb_data)]])


@a.message(StateFilter(AddP.photos), F.photo)
async def addp_photo(m: Message, state: FSMContext):
    d = await state.get_data()
    ph = d.get("photos", []) + [m.photo[-1].file_id]
    await state.update_data(photos=ph)
    await m.answer(f"📷 {len(ph)} ta rasm qabul qilindi. Yana yuboring yoki tugating:", reply_markup=done_kb("a:pdone"))


@a.callback_query(StateFilter(AddP.photos), F.data == "a:pdone")
async def addp_done(cb, state: FSMContext):
    d = await state.get_data()
    if not d.get("photos"):
        return await cb.answer("Kamida 1 ta rasm yuboring", show_alert=True)
    pid = await q(
        "INSERT INTO products(cat_id,name,price,sizes,colors,desc,photos) VALUES(?,?,?,?,?,?,?)",
        (d["cat"], d["name"], d["price"], d["sizes"], d["colors"], d["desc"], json.dumps(d["photos"])),
        commit=True,
    )
    await state.clear()
    await cb.message.answer("🎉 Mahsulot qo'shildi va mijozlarga ko'rinadi!")
    await admin_card(cb.message, pid, delete=False)
    await cb.answer()


# ── Mahsulotlarni boshqarish ──
@a.callback_query(F.data == "a:plist")
async def plist(cb):
    cats = await q("SELECT c.*, (SELECT COUNT(*) FROM products WHERE cat_id=c.id) n FROM categories c ORDER BY id")
    rows = [[IB(text=f"{c['name']} ({c['n']})", callback_data=f"a:pl:{c['id']}")] for c in cats] + [BACK]
    await edit_or_send(cb.message, "📋 Kategoriyani tanlang:", IM(inline_keyboard=rows))
    await cb.answer()


@a.callback_query(F.data.startswith("a:pl:"))
async def plist_cat(cb):
    cid = int(cb.data.split(":")[2])
    ps = await q("SELECT * FROM products WHERE cat_id=? ORDER BY id DESC", (cid,))
    rows = [
        [IB(text=f"{'✅' if p['active'] else '⛔'} {p['name']} — {money(p['price'])}", callback_data=f"a:pv:{p['id']}")]
        for p in ps
    ]
    rows.append([IB(text="⬅️ Orqaga", callback_data="a:plist")])
    await edit_or_send(cb.message, "Mahsulotni tanlang:" if ps else "Bu kategoriyada mahsulot yo'q.", IM(inline_keyboard=rows))
    await cb.answer()


async def admin_card(msg: Message, pid, delete=True):
    p = await q("SELECT * FROM products WHERE id=?", (pid,), one=True)
    if not p:
        return await msg.answer("Topilmadi")
    tg = "⛔ Tugadi deb belgilash" if p["active"] else "✅ Sotuvga qaytarish"
    kb = IM(
        inline_keyboard=[
            [IB(text=tg, callback_data=f"a:pt:{pid}")],
            [IB(text="💰 Narx", callback_data=f"a:pe:price:{pid}"), IB(text="✏️ Nom", callback_data=f"a:pe:name:{pid}")],
            [IB(text="📏 O'lcham", callback_data=f"a:pe:sizes:{pid}"), IB(text="🎨 Rang", callback_data=f"a:pe:colors:{pid}")],
            [IB(text="📝 Tavsif", callback_data=f"a:pe:desc:{pid}"), IB(text="🖼 Rasmlar", callback_data=f"a:pe:photos:{pid}")],
            [IB(text="🗑 O'chirish", callback_data=f"a:pd:{pid}")],
            [IB(text="⬅️ Orqaga", callback_data=f"a:pl:{p['cat_id']}")],
        ]
    )
    await send_card(msg, product_text(p, admin=True), json.loads(p["photos"]), kb, delete=delete)


@a.callback_query(F.data.startswith("a:pv:"))
async def pview(cb):
    await admin_card(cb.message, int(cb.data.split(":")[2]))
    await cb.answer()


@a.callback_query(F.data.startswith("a:pt:"))
async def ptoggle(cb):
    pid = int(cb.data.split(":")[2])
    await q("UPDATE products SET active=1-active WHERE id=?", (pid,), commit=True)
    await admin_card(cb.message, pid)
    await cb.answer("Yangilandi")


@a.callback_query(F.data.startswith("a:pd:"))
async def pdel(cb):
    pid = int(cb.data.split(":")[2])
    kb = IM(inline_keyboard=[[IB(text="🗑 Ha, o'chirish", callback_data=f"a:pdy:{pid}"), IB(text="Yo'q", callback_data=f"a:pv:{pid}")]])
    await edit_or_send(cb.message, "Mahsulot butunlay o'chirilsinmi?", kb)
    await cb.answer()


@a.callback_query(F.data.startswith("a:pdy:"))
async def pdel_yes(cb):
    pid = int(cb.data.split(":")[2])
    await q("DELETE FROM products WHERE id=?", (pid,), commit=True)
    await q("DELETE FROM cart WHERE product_id=?", (pid,), commit=True)
    await edit_or_send(cb.message, "🗑 O'chirildi", IM(inline_keyboard=[[IB(text="📋 Mahsulotlar", callback_data="a:plist")], BACK]))
    await cb.answer()


PROMPTS = {
    "price": "Yangi narxni yozing (so'mda):",
    "name": "Yangi nomni yozing:",
    "sizes": "Yangi o'lchamlar (vergul bilan), masalan 36,37,38 (o'chirish uchun -):",
    "colors": "Yangi ranglar (o'chirish uchun -):",
    "desc": "Yangi tavsif (o'chirish uchun -):",
}


@a.callback_query(F.data.startswith("a:pe:"))
async def pedit(cb, state: FSMContext):
    _, _, field, pid = cb.data.split(":")
    await state.clear()
    if field == "photos":
        await state.set_state(EditP.photos)
        await state.update_data(pid=int(pid), photos=[])
        await cb.message.answer("Yangi rasmlarni yuboring (eskilari almashtiriladi). Tugagach «Tayyor»:")
    else:
        await state.set_state(EditP.value)
        await state.update_data(pid=int(pid), field=field)
        await cb.message.answer(PROMPTS[field])
    await cb.answer()


@a.message(StateFilter(EditP.value), F.text)
async def pedit_value(m: Message, state: FSMContext):
    d = await state.get_data()
    f, t = d["field"], m.text.strip()
    if f == "price":
        t = digits(t)
        if not t:
            return await m.answer("Faqat raqam yozing")
    elif f == "sizes":
        t = "" if t == "-" else ", ".join(split_sizes(t))
    elif t == "-" and f != "name":
        t = ""
    await q(f"UPDATE products SET {f}=? WHERE id=?", (t, d["pid"]), commit=True)  # f - ruxsat etilgan ro'yxatdan
    await state.clear()
    await m.answer("✅ Saqlandi")
    await admin_card(m, d["pid"], delete=False)


@a.message(StateFilter(EditP.photos), F.photo)
async def pedit_photo(m: Message, state: FSMContext):
    d = await state.get_data()
    ph = d["photos"] + [m.photo[-1].file_id]
    await state.update_data(photos=ph)
    await m.answer(f"📷 {len(ph)} ta rasm. Yana yuboring yoki tugating:", reply_markup=done_kb("a:epdone"))


@a.callback_query(StateFilter(EditP.photos), F.data == "a:epdone")
async def pedit_done(cb, state: FSMContext):
    d = await state.get_data()
    if not d["photos"]:
        return await cb.answer("Kamida 1 ta rasm yuboring", show_alert=True)
    await q("UPDATE products SET photos=? WHERE id=?", (json.dumps(d["photos"]), d["pid"]), commit=True)
    await state.clear()
    await cb.message.answer("✅ Rasmlar yangilandi")
    await admin_card(cb.message, d["pid"], delete=False)
    await cb.answer()


# ── Buyurtmalar ──
@a.callback_query(F.data == "a:orders")
async def orders_menu(cb):
    cnt = {r_["status"]: r_["c"] for r_ in await q("SELECT status, COUNT(*) c FROM orders GROUP BY status")}
    rows = [[IB(text=f"{lab} ({cnt.get(k, 0)})", callback_data=f"a:ol:{k}")] for k, lab in STATUS.items()] + [BACK]
    await edit_or_send(cb.message, "📦 <b>Buyurtmalar</b>", IM(inline_keyboard=rows))
    await cb.answer()


@a.callback_query(F.data.startswith("a:ol:"))
async def orders_list(cb):
    st = cb.data.split(":")[2]
    os_ = await q("SELECT * FROM orders WHERE status=? ORDER BY id DESC LIMIT 20", (st,))
    rows = [[IB(text=f"№{o['id']} · {o['name']} · {money(o['total'])}", callback_data=f"a:ov:{o['id']}")] for o in os_]
    rows.append([IB(text="⬅️ Orqaga", callback_data="a:orders")])
    await edit_or_send(cb.message, f"{STATUS[st]}" + ("" if os_ else "\n\nBuyurtma yo'q."), IM(inline_keyboard=rows))
    await cb.answer()


@a.callback_query(F.data.startswith("a:ov:"))
async def order_view(cb):
    o = await q("SELECT * FROM orders WHERE id=?", (int(cb.data.split(":")[2]),), one=True)
    if o:
        kb = order_kb(o["id"])
        kb.inline_keyboard.append([IB(text="⬅️ Orqaga", callback_data=f"a:ol:{o['status']}")])
        await edit_or_send(cb.message, order_text(o), kb)
    await cb.answer()


CUSTOMER_MSG = {
    "confirmed": "✅ Buyurtmangiz №{id} tasdiqlandi. Tez orada pochtaga topshiramiz.",
    "shipped": "🚚 Buyurtmangiz №{id} pochtaga topshirildi. Yetkazilganda to'lov qilasiz.",
    "delivered": "📦 Buyurtmangiz №{id} yetkazildi. Xaridingiz uchun rahmat! 💖",
    "canceled": "❌ Buyurtmangiz №{id} bekor qilindi. Savol bo'lsa biz bilan bog'laning.",
}


@a.callback_query(F.data.startswith("os:"))
async def order_status(cb):
    _, oid, st = cb.data.split(":")
    oid = int(oid)
    o = await q("SELECT * FROM orders WHERE id=?", (oid,), one=True)
    if not o:
        return await cb.answer("Topilmadi", show_alert=True)
    if o["status"] == st:
        return await cb.answer("Allaqachon shu holatda")
    await q("UPDATE orders SET status=? WHERE id=?", (st, oid), commit=True)
    o = await q("SELECT * FROM orders WHERE id=?", (oid,), one=True)
    await edit_or_send(cb.message, order_text(o), order_kb(oid))
    try:
        await cb.bot.send_message(o["user_id"], CUSTOMER_MSG[st].format(id=oid))
    except Exception:
        pass
    await cb.answer("Mijozga xabar yuborildi")


# ── Statistika ──
@a.callback_query(F.data == "a:stats")
async def stats(cb):
    users = (await q("SELECT COUNT(*) c FROM users", one=True))["c"]
    total = (await q("SELECT COUNT(*) c FROM orders", one=True))["c"]
    rev = (await q("SELECT COALESCE(SUM(total),0) s FROM orders WHERE status='delivered'", one=True))["s"]
    pend = (await q("SELECT COALESCE(SUM(total),0) s FROM orders WHERE status IN ('new','confirmed','shipped')", one=True))["s"]
    top = {}
    for o in await q("SELECT items FROM orders WHERE status!='canceled'"):
        for i in json.loads(o["items"]):
            top[i["name"]] = top.get(i["name"], 0) + i["qty"]
    tt = "\n".join(f"{n}. {esc(k)} — {v} dona" for n, (k, v) in enumerate(sorted(top.items(), key=lambda x: -x[1])[:5], 1)) or "—"
    t = (
        f"📊 <b>Statistika</b>\n\n👥 Mijozlar: {users}\n🧾 Buyurtmalar: {total}\n"
        f"💰 Yetkazilgan sotuv: <b>{money(rev)}</b>\n⏳ Jarayondagi: {money(pend)}\n\n🔥 Top modellar:\n{tt}"
    )
    await edit_or_send(cb.message, t, IM(inline_keyboard=[BACK]))
    await cb.answer()


# ── Sozlamalar ──
async def settings_view():
    t = "⚙️ <b>Sozlamalar</b>\n\n"
    rows = []
    for k, lab in SET_META.items():
        v = await get_set(k)
        if k in NUMERIC:
            v = money(v)
        elif k == "contact_username" and v:
            v = "@" + v
        short = (v[:40] + "…") if len(v) > 40 else v
        t += f"{lab}: <b>{esc(short) or '— yo`q —'}</b>\n"
        rows.append([IB(text=f"✏️ {lab}", callback_data=f"a:se:{k}")])
    op = await get_set("shop_open") == "1"
    rows.append([IB(text="🟢 Do'kon OCHIQ (yopish)" if op else "🔴 Do'kon YOPIQ (ochish)", callback_data="a:sopen")])
    rows.append(BACK)
    return t, IM(inline_keyboard=rows)


@a.callback_query(F.data == "a:set")
async def set_menu(cb, state: FSMContext):
    await state.clear()
    t, kb = await settings_view()
    await edit_or_send(cb.message, t, kb)
    await cb.answer()


@a.callback_query(F.data == "a:sopen")
async def set_open(cb):
    await put_set("shop_open", "0" if await get_set("shop_open") == "1" else "1")
    t, kb = await settings_view()
    await edit_or_send(cb.message, t, kb)
    await cb.answer("Yangilandi")


@a.callback_query(F.data.startswith("a:se:"))
async def set_edit(cb, state: FSMContext):
    k = cb.data.split(":")[2]
    if k not in SET_META:
        return await cb.answer()
    hint = {
        "contact_username": "Telegram username yozing (masalan @feliz_owner). Mijozlar «Egasiga yozish» tugmasi orqali shu profilga yozadi.",
        "phone": "Telefon raqamni yozing:",
        "delivery_price": "Yetkazish narxini yozing (so'mda), bepul bo'lsa 0:",
        "free_from": "Qancha summadan yuqori xaridda yetkazish bepul bo'ladi? (o'chirish uchun 0)",
        "work_hours": "Ish vaqtini yozing:",
        "welcome": "Yangi salomlashuv matnini yozing:",
        "about": "«Biz haqimizda» matnini yozing:",
    }[k]
    await state.set_state(SetS.value)
    await state.update_data(key=k)
    await cb.message.answer(hint + "\n(tozalash uchun: -)")
    await cb.answer()


@a.message(StateFilter(SetS.value), F.text)
async def set_value(m: Message, state: FSMContext):
    k = (await state.get_data())["key"]
    v = m.text.strip()
    if v == "-":
        v = "0" if k in NUMERIC else ""
    elif k in NUMERIC:
        n = digits(v)
        if n is None:
            return await m.answer("Faqat raqam yozing")
        v = str(n)
    elif k == "contact_username":
        v = v.replace("https://t.me/", "").replace("t.me/", "").lstrip("@").strip()
    await put_set(k, v)
    await state.clear()
    t, kb = await settings_view()
    await m.answer("✅ Saqlandi\n\n" + t, reply_markup=kb)


# ── E'lon ──
@a.callback_query(F.data == "a:bc")
async def bc_start(cb, state: FSMContext):
    await state.set_state(Bc.msg)
    await cb.message.answer("📢 Barcha mijozlarga yuboriladigan xabarni yuboring (matn yoki rasm + matn):")
    await cb.answer()


@a.message(StateFilter(Bc.msg))
async def bc_msg(m: Message, state: FSMContext):
    await state.update_data(chat=m.chat.id, mid=m.message_id)
    n = (await q("SELECT COUNT(*) c FROM users", one=True))["c"]
    kb = IM(inline_keyboard=[[IB(text=f"✅ Yuborish ({n} ta)", callback_data="a:bcy"), IB(text="❌ Bekor", callback_data="a:home")]])
    await m.answer("Yuqoridagi xabar yuborilsinmi?", reply_markup=kb)


@a.callback_query(StateFilter(Bc.msg), F.data == "a:bcy")
async def bc_go(cb, state: FSMContext):
    d = await state.get_data()
    await state.clear()
    await cb.message.edit_text("⏳ Yuborilmoqda...")
    ok = bad = 0
    for u in await q("SELECT id FROM users"):
        try:
            await cb.bot.copy_message(u["id"], d["chat"], d["mid"])
            ok += 1
        except Exception:
            bad += 1
        await asyncio.sleep(0.05)
    await cb.message.answer(f"✅ Yuborildi: {ok}\n⚠️ Yetmadi: {bad}", reply_markup=IM(inline_keyboard=[BACK]))
    await cb.answer()


# ── Adminlar (faqat owner) ──
@a.callback_query(F.data == "a:adm")
async def adm_view(cb, state: FSMContext):
    if cb.from_user.id != OWNER_ID:
        return await cb.answer("Faqat owner uchun", show_alert=True)
    await state.clear()
    ids = await q("SELECT id FROM admins")
    rows = [[IB(text=f"🗑 {x['id']}", callback_data=f"a:admdel:{x['id']}")] for x in ids]
    rows.append([IB(text="➕ Admin qo'shish", callback_data="a:admadd")])
    rows.append(BACK)
    await edit_or_send(cb.message, "👥 <b>Adminlar</b>\nOwner: " + str(OWNER_ID), IM(inline_keyboard=rows))
    await cb.answer()


@a.callback_query(F.data == "a:admadd")
async def adm_add(cb, state: FSMContext):
    if cb.from_user.id != OWNER_ID:
        return await cb.answer("Faqat owner uchun", show_alert=True)
    await state.set_state(AdmS.id)
    await cb.message.answer("Yangi adminning Telegram ID raqamini yozing (u @userinfobot dan bilib oladi):")
    await cb.answer()


@a.message(StateFilter(AdmS.id), F.text)
async def adm_add_id(m: Message, state: FSMContext):
    n = digits(m.text)
    if m.from_user.id != OWNER_ID or not n:
        return await m.answer("Noto'g'ri ID")
    await q("INSERT OR IGNORE INTO admins(id) VALUES(?)", (n,), commit=True)
    await state.clear()
    await m.answer("✅ Admin qo'shildi", reply_markup=IM(inline_keyboard=[[IB(text="👥 Adminlar", callback_data="a:adm")], BACK]))


@a.callback_query(F.data.startswith("a:admdel:"))
async def adm_del(cb, state: FSMContext):
    if cb.from_user.id != OWNER_ID:
        return await cb.answer("Faqat owner uchun", show_alert=True)
    await q("DELETE FROM admins WHERE id=?", (int(cb.data.split(":")[2]),), commit=True)
    await adm_view(cb, state)


# ── Zaxira ──
@fb.message(StateFilter(None))
async def fallback(m: Message):
    await m.answer("Iltimos, menyudagi tugmalardan foydalaning 👇", reply_markup=main_kb(await is_admin(m.from_user.id)))


async def keep_alive():
    """Jim ishlaydi: mini veb-server + har 5 daqiqada o'ziga so'rov. Telegramga hech narsa yuborilmaydi."""
    from aiohttp import ClientSession, ClientTimeout, web

    async def ok(_):
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", ok)
    app.router.add_get("/health", ok)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080"))).start()

    url = os.getenv("SELF_URL", "").rstrip("/")
    if not url:
        await asyncio.Event().wait()  # faqat server ishlab turadi
    async with ClientSession(timeout=ClientTimeout(total=20)) as s:
        while True:
            await asyncio.sleep(300)  # 5 daqiqa
            try:
                async with s.get(url + "/health") as resp:
                    await resp.read()
            except Exception:
                pass  # xatoni ham jim yutamiz


async def main():
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    if not TOKEN or not OWNER_ID:
        raise SystemExit("`.env` faylida BOT_TOKEN va OWNER_ID ni to'ldiring")
    await init_db()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_routers(r, a, fb)
    keep = asyncio.create_task(keep_alive())  # noqa: F841 (havola saqlanadi)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
