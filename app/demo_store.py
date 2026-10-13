"""A small stand-in for the Shopify storefront, for local end-to-end testing.

It behaves like the real integration: the product page embeds exactly the
same ``jasmin-personalize.js`` snippet, the cart stores line item properties,
and checkout sends an HMAC-signed ``orders/create`` webhook to the app's real
webhook endpoint.  Disable with ``DEMO_STORE=false``.
"""
from __future__ import annotations

import hashlib
import html
import json
import random
import secrets
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import shopify
from .config import Settings
from .services import NotFound, Services

COOKIE = "jasmin_demo_cart"
VARIANTS = [("matte", "Matte 350gsm", 3.50), ("cotton", "Cotton 600gsm", 4.75)]
SHOP_DOMAIN = "jasmin-demo.myshopify.com"


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def variant_id(template_id: str, key: str) -> int:
    return int(hashlib.sha1(f"{template_id}:{key}".encode()).hexdigest()[:10], 16)


def register(app: FastAPI, svc: Services, settings: Settings) -> None:
    carts: dict[str, list[dict]] = {}

    def products() -> list[dict]:
        out = []
        for t in svc.list_templates(status="published"):
            handle = (t["product_handles"] or [t["id"]])[0]
            out.append(
                {
                    "template": t,
                    "handle": handle,
                    "title": t["name"],
                    "images": [
                        f"{svc.assets_url(t['id'], t['revision'])}/p{p['index']}-original.jpg"
                        for p in t["definition"]["pages"]
                    ],
                    "variants": [
                        {"id": variant_id(t["id"], key), "title": title, "price": price}
                        for key, title, price in VARIANTS
                    ],
                }
            )
        return out

    def find_variant(vid: int) -> tuple[dict, dict] | None:
        for p in products():
            for v in p["variants"]:
                if v["id"] == vid:
                    return p, v
        return None

    def cart_of(request: Request) -> tuple[str, list[dict]]:
        cid = request.cookies.get(COOKIE)
        if not cid or cid not in carts:
            cid = secrets.token_urlsafe(12)
            carts[cid] = []
        return cid, carts[cid]

    def respond(content: str, cid: str, status: int = 200) -> HTMLResponse:
        resp = HTMLResponse(content, status_code=status)
        resp.set_cookie(COOKIE, cid, httponly=True, samesite="lax")
        return resp

    def layout(title: str, body: str, cart: list[dict]) -> str:
        count = sum(i["quantity"] for i in cart)
        return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · Jasmin Paper Co. (demo)</title>
<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="/static/css/base.css">
<link rel="stylesheet" href="/static/css/store.css">
</head><body>
<div class="demo-ribbon">Demo storefront: a local stand-in for your Shopify shop. Checkout sends a signed
<code>orders/create</code> webhook to this app. <a href="/admin">Open admin</a></div>
<header class="store-head">
  <a class="logo" href="/demo-store">Jasmin <em>Paper Co.</em></a>
  <nav><a href="/demo-store">Shop</a><a href="/demo-store/cart" class="cart-link">Cart <span>{count}</span></a></nav>
</header>
<main class="store-main">{body}</main>
<footer class="store-foot">Prototype. Product pages embed the same <code>jasmin-personalize.js</code> snippet you add to Shopify.</footer>
</body></html>"""

    @app.get("/demo-store", response_class=HTMLResponse, include_in_schema=False)
    def home(request: Request):
        cid, cart = cart_of(request)
        items = products()
        if not items:
            body = (
                '<section class="empty"><h1>No products yet</h1><p>Publish a template in the '
                '<a href="/admin">admin</a> (or run <code>python -m scripts.seed</code>) and it will appear here.</p></section>'
            )
        else:
            cards = "".join(
                f"""<a class="card" href="/demo-store/products/{esc(quote(p['handle']))}">
  <div class="card-img"><img src="{esc(p['images'][0])}" alt=""></div>
  <h2>{esc(p['title'])}</h2><p>From ${p['variants'][0]['price']:.2f} per card · Personalized</p></a>"""
                for p in items
            )
            body = f"""<section class="hero"><h1>Stationery, made personal</h1>
<p>Choose a design, add your photos and wording, and approve a print proof. We print and deliver.</p></section>
<section class="grid">{cards}</section>"""
        return respond(layout("Shop", body, cart), cid)

    @app.get("/demo-store/products/{handle}", response_class=HTMLResponse, include_in_schema=False)
    def product(handle: str, request: Request):
        cid, cart = cart_of(request)
        p = next((p for p in products() if p["handle"] == handle), None)
        if p is None:
            return respond(layout("Not found", "<section class='empty'><h1>Product not found</h1></section>", cart), cid, 404)
        app_url = str(request.base_url).rstrip("/")
        options = "".join(
            f'<option value="{v["id"]}">{esc(v["title"])} · ${v["price"]:.2f}</option>' for v in p["variants"]
        )
        gallery = "".join(f'<img src="{esc(src)}" alt="Design page {i + 1}">' for i, src in enumerate(p["images"]))
        body = f"""<section class="product">
  <div class="gallery">{gallery}</div>
  <div class="product-info">
    <h1>{esc(p['title'])}</h1>
    <p class="price">${p['variants'][0]['price']:.2f} <span>per card</span></p>
    <form action="/demo-store/cart/add" method="post" class="product-form">
      <label>Paper <select name="id">{options}</select></label>
      <label>Quantity <input type="number" name="quantity" value="50" min="1" max="1000"></label>
      <button type="submit" name="add" class="btn btn-primary">Add to cart</button>
      <div class="jasmin-personalize"
           data-app-url="{esc(app_url)}"
           data-product-handle="{esc(p['handle'])}"
           data-variant-id="{p['variants'][0]['id']}"
           data-cart-add-url="/demo-store/cart/add.js"
           data-cart-url="/demo-store/cart"></div>
    </form>
    <div class="desc"><p>Printed on heavyweight paper, trimmed to 5&times;7 in, with envelopes.</p>
    <p>Personalize it with your own photo and wording. You'll see an exact print proof before you order.</p></div>
  </div>
</section>
<script src="/static/shopify/jasmin-personalize.js" defer></script>"""
        return respond(layout(p["title"], body, cart), cid)

    def add_item(cart: list[dict], vid: int, quantity: int, properties: dict) -> dict:
        found = find_variant(vid)
        if found is None:
            raise NotFound("variant not found")
        p, v = found
        item = {
            "key": secrets.token_hex(6),
            "variant_id": vid,
            "title": p["title"],
            "variant_title": v["title"],
            "price": v["price"],
            "quantity": max(1, min(1000, int(quantity))),
            "properties": {str(k): str(v) for k, v in (properties or {}).items()},
            "handle": p["handle"],
        }
        cart.append(item)
        return item

    @app.post("/demo-store/cart/add.js", include_in_schema=False)
    async def cart_add_js(request: Request):
        cid, cart = cart_of(request)
        body = await request.json()
        added = [
            add_item(cart, int(i["id"]), int(i.get("quantity", 1)), i.get("properties") or {})
            for i in body.get("items", [])
        ]
        resp = JSONResponse({"items": added})
        resp.set_cookie(COOKIE, cid, httponly=True, samesite="lax")
        return resp

    @app.post("/demo-store/cart/add", include_in_schema=False)
    def cart_add_form(request: Request, id: int = Form(...), quantity: int = Form(1)):
        cid, cart = cart_of(request)
        add_item(cart, id, quantity, {})
        resp = RedirectResponse("/demo-store/cart", status_code=303)
        resp.set_cookie(COOKIE, cid, httponly=True, samesite="lax")
        return resp

    @app.post("/demo-store/cart/remove/{key}", include_in_schema=False)
    def cart_remove(key: str, request: Request):
        cid, cart = cart_of(request)
        cart[:] = [i for i in cart if i["key"] != key]
        resp = RedirectResponse("/demo-store/cart", status_code=303)
        resp.set_cookie(COOKIE, cid, httponly=True, samesite="lax")
        return resp

    @app.get("/demo-store/cart", response_class=HTMLResponse, include_in_schema=False)
    def cart_page(request: Request):
        cid, cart = cart_of(request)
        if not cart:
            body = "<section class='empty'><h1>Your cart is empty</h1><p><a href='/demo-store'>Continue shopping</a></p></section>"
            return respond(layout("Cart", body, cart), cid)
        rows = []
        for i in cart:
            visible = "".join(
                f"<li><strong>{esc(k)}:</strong> {esc(v)}</li>" for k, v in i["properties"].items() if not k.startswith("_")
            )
            thumb = i["properties"].get("_preview")
            img = f'<img src="{esc(thumb)}" alt="">' if thumb else ""
            rows.append(
                f"""<div class="line"><div class="line-img">{img}</div>
<div class="line-info"><h3>{esc(i['title'])}</h3><p>{esc(i['variant_title'])} · Qty {i['quantity']}</p><ul>{visible}</ul>
<form method="post" action="/demo-store/cart/remove/{esc(i['key'])}"><button class="btn-link">Remove</button></form></div>
<div class="line-price">${i['price'] * i['quantity']:.2f}</div></div>"""
            )
        total = sum(i["price"] * i["quantity"] for i in cart)
        body = f"""<section class="cart"><h1>Your cart</h1>{''.join(rows)}
<div class="subtotal">Subtotal <strong>${total:.2f}</strong></div></section>
<section class="checkout"><h2>Checkout</h2>
<form method="post" action="/demo-store/checkout" class="checkout-form">
<label>Full name <input type="text" name="name" required value="Emma Taylor"></label>
<label>Email <input type="email" name="email" required value="emma@example.com"></label>
<label>Address <input type="text" name="address1" required value="12 Willow Lane"></label>
<label>City <input type="text" name="city" required value="Hudson"></label>
<label>Postcode <input type="text" name="zip" required value="12534"></label>
<label>Country <input type="text" name="country" required value="United States"></label>
<button class="btn btn-accent" type="submit">Place order (demo, no payment)</button></form></section>"""
        return respond(layout("Cart", body, cart), cid)

    @app.post("/demo-store/checkout", include_in_schema=False)
    async def checkout(
        request: Request,
        name: str = Form(...),
        email: str = Form(...),
        address1: str = Form(...),
        city: str = Form(...),
        zip: str = Form(...),
        country: str = Form(...),
    ):
        cid, cart = cart_of(request)
        if not cart:
            return RedirectResponse("/demo-store/cart", status_code=303)
        count = svc.db.one("SELECT COUNT(*) AS n FROM orders WHERE source='demo'")["n"]
        order_id = str(random.randint(5_000_000_000_000, 5_999_999_999_999))
        first, _, last = name.strip().partition(" ")
        address = {"name": name, "address1": address1, "city": city, "zip": zip, "country": country}
        payload = {
            "id": int(order_id),
            "name": f"#{1001 + count}",
            "email": email,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "currency": "USD",
            "total_price": f"{sum(i['price'] * i['quantity'] for i in cart):.2f}",
            "customer": {"first_name": first, "last_name": last, "email": email},
            "shipping_address": address,
            "line_items": [
                {
                    "id": random.randint(10_000_000_000_000, 19_999_999_999_999),
                    "variant_id": i["variant_id"],
                    "title": i["title"],
                    "variant_title": i["variant_title"],
                    "quantity": i["quantity"],
                    "price": f"{i['price']:.2f}",
                    "properties": [{"name": k, "value": v} for k, v in i["properties"].items()],
                }
                for i in cart
            ],
        }
        body = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "X-Shopify-Topic": "orders/create",
            "X-Shopify-Shop-Domain": settings.shopify_shop_domain or SHOP_DOMAIN,
            "X-Shopify-Hmac-Sha256": shopify.sign(body, settings.webhook_secret),
            "X-Jasmin-Demo": "1",
        }
        # Deliver through the real webhook endpoint (signature check included).
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://demo-store.internal") as client:
            resp = await client.post("/webhooks/shopify/orders-create", content=body, headers=headers)
        if resp.status_code != 200:
            return HTMLResponse(f"Webhook failed: {esc(resp.text)}", status_code=502)
        cart.clear()
        r = RedirectResponse(f"/demo-store/orders/{order_id}", status_code=303)
        r.set_cookie(COOKIE, cid, httponly=True, samesite="lax")
        return r

    @app.get("/demo-store/orders/{order_id}", response_class=HTMLResponse, include_in_schema=False)
    def thanks(order_id: str, request: Request):
        cid, cart = cart_of(request)
        try:
            order = svc.get_order(order_id)
        except NotFound:
            order = None
        if order is None:
            body = (
                "<section class='empty'><h1>Thank you!</h1><p>Your order contained no personalized items, "
                "so the print studio was not notified.</p></section>"
            )
            return respond(layout("Thank you", body, cart), cid)
        items = "".join(
            f"<li><img src='{esc(i['thumbnailUrl'])}' alt=''><span>{esc(i['title'])} · {esc(i['variant'])} · Qty {i['quantity']}</span></li>"
            for i in order["items"]
        )
        body = f"""<section class="thanks"><span class="pill pill-ok">Order placed</span>
<h1>Thank you, {esc(order['customer'] or 'friend')}!</h1>
<p>Order <strong>{esc(order['name'])}</strong> was sent to the print studio via the Shopify webhook.</p>
<ul class="thanks-items">{items}</ul>
<p><a class="btn btn-primary" href="/admin#/orders/{esc(order['id'])}">See it in the admin (print files)</a>
<a class="btn" href="/demo-store">Back to the shop</a></p></section>"""
        return respond(layout("Thank you", body, cart), cid)
