import json

from app.shopify import sign

from .conftest import PHOTOS


def upload(client, name="dusk-portrait.jpg"):
    with open(PHOTOS / name, "rb") as fh:
        resp = client.post("/api/uploads", files={"file": (name, fh, "image/jpeg")})
    assert resp.status_code == 200, resp.text
    return resp.json()


def approved_design(client):
    up = upload(client)
    d = client.post(
        "/api/designs",
        json={"templateId": "wedding-invitation", "values": {"couple-photo": {"upload": up["id"]}, "couple-names": {"text": "Sofia & Mateo"}}},
    ).json()
    resp = client.post(f"/api/designs/{d['id']}/approve")
    assert resp.status_code == 200, resp.text
    return resp.json()


def order_payload(design_id, order_id=4400000000001):
    return {
        "id": order_id,
        "name": "#1042",
        "email": "sofia@example.com",
        "customer": {"first_name": "Sofia", "last_name": "Lopez"},
        "shipping_address": {"name": "Sofia Lopez", "city": "Lyon"},
        "line_items": [
            {"id": 1, "title": "Arch Wedding Invitation", "variant_title": "Matte", "quantity": 80,
             "properties": [{"name": "Personalization", "value": "Sofia & Mateo"}, {"name": "_design_id", "value": design_id}]},
            {"id": 2, "title": "Envelope liners", "quantity": 1, "properties": []},
        ],
    }


def test_editor_payload(client, template):
    data = client.get("/api/templates/wedding-invitation").json()
    assert data["status"] == "published"
    assert {e["id"] for e in data["elements"]} >= {"couple-photo", "couple-names"}
    assert set(data["fonts"]) == {"GreatVibes-Regular", "CormorantGaramond-SemiBold", "CormorantGaramond-Regular", "Montserrat-Medium"}
    assert "advances" in data["fonts"]["GreatVibes-Regular"]
    assert client.get(data["assets"] + "/p0-seg0.png").status_code == 200
    assert client.get("/fonts/GreatVibes-Regular.ttf").status_code == 200
    resp = client.get("/editor/wedding-invitation")
    assert "frame-ancestors" in resp.headers["content-security-policy"]


def test_design_lifecycle(client, template):
    up = upload(client)
    assert up["width"] == 1600 and client.get(up["previewUrl"]).status_code == 200
    d = client.post("/api/designs", json={"templateId": "wedding-invitation", "values": {"bogus": {"text": "x"}}}).json()
    assert d["values"] == {} and d["status"] == "draft"

    proof = client.post(f"/api/designs/{d['id']}/proof").json()
    assert proof["errors"] and not proof["pages"]  # photo missing
    assert client.post(f"/api/designs/{d['id']}/approve").status_code == 422

    values = {"couple-photo": {"upload": up["id"], "zoom": 2, "dx": 5}, "couple-names": {"text": "Ana & Ben"}}
    assert client.put(f"/api/designs/{d['id']}", json={"values": values}).status_code == 200
    proof = client.post(f"/api/designs/{d['id']}/proof").json()
    assert proof["errors"] == [] and len(proof["pages"]) == 2
    assert client.get(proof["pages"][0]).headers["content-type"] == "image/jpeg"

    approved = client.post(f"/api/designs/{d['id']}/approve").json()
    assert approved["status"] == "approved" and "Ana & Ben" in approved["summary"]
    assert client.get(approved["thumbnailUrl"]).status_code == 200
    assert client.put(f"/api/designs/{d['id']}", json={"values": {}}).status_code == 422  # locked


def test_original_uploads_are_private(client, svc, template):
    up = upload(client)
    row = svc.get_upload(up["id"])
    assert client.get(f"/assets/uploads/{row['path']}").status_code == 404


def test_rejects_non_images(client):
    resp = client.post("/api/uploads", files={"file": ("x.jpg", b"not an image", "image/jpeg")})
    assert resp.status_code == 422


def test_storefront_lookup(client, admin, template):
    assert client.get("/api/storefront/products/wedding-invitation").json()["templateId"] == "wedding-invitation"
    assert client.get("/api/storefront/products/unknown").json()["templateId"] is None
    resp = admin.patch("/admin/api/templates/wedding-invitation", json={"productHandles": ["Arch Invite 5x7"]})
    assert resp.json()["productHandles"] == ["arch-invite-5x7"]
    assert client.get("/api/storefront/products/arch-invite-5x7").json()["templateId"] == "wedding-invitation"
    assert client.get("/api/storefront/products/wedding-invitation").json()["templateId"] is None


def test_admin_requires_auth(client, template):
    assert client.get("/admin").status_code == 401
    assert client.get("/admin/api/templates").status_code == 401
    assert client.get("/admin/api/templates", auth=("admin", "wrong")).status_code == 401


def test_admin_template_edit(admin, template):
    t = admin.get("/admin/api/templates/wedding-invitation").json()
    rev = t["revision"]
    bad = admin.patch("/admin/api/templates/wedding-invitation", json={"elements": [{"id": "couple-names", "font": "Comic-Sans"}]})
    assert bad.status_code == 422
    resp = admin.patch(
        "/admin/api/templates/wedding-invitation",
        json={"elements": [{"id": "couple-names", "fontSize": 42, "color": "#112233", "label": "Names"}, {"id": "venue", "enabled": False}]},
    )
    t = resp.json()
    els = {e["id"]: e for e in t["definition"]["elements"]}
    assert t["revision"] == rev + 1
    assert els["couple-names"]["fontSize"] == 42 and els["couple-names"]["color"]["values"] == [0.0667, 0.1333, 0.2]
    stack_ids = [i.get("id") for s in t["definition"]["stacks"] for i in s["stack"]]
    assert "venue" not in stack_ids  # disabled -> part of the static artwork


def test_admin_upload_template(admin):
    from .conftest import SAMPLE_PDF

    with open(SAMPLE_PDF, "rb") as fh:
        resp = admin.post("/admin/api/templates", data={"name": "Second invite"}, files={"file": ("x.pdf", fh, "application/pdf")})
    assert resp.status_code == 200
    assert resp.json()["id"] == "second-invite" and resp.json()["status"] == "draft"
    resp = admin.post("/admin/api/templates", data={"name": "Bad"}, files={"file": ("x.pdf", b"hello", "application/pdf")})
    assert resp.status_code == 422


def test_webhook_signature_and_order(client, admin, svc, template):
    design = approved_design(client)
    body = json.dumps(order_payload(design["id"])).encode()

    assert client.post("/webhooks/shopify/orders-create", content=body, headers={"X-Shopify-Hmac-Sha256": "nope"}).status_code == 401

    headers = {"X-Shopify-Hmac-Sha256": sign(body, "s3cret"), "Content-Type": "application/json"}
    resp = client.post("/webhooks/shopify/orders-create", content=body, headers=headers)
    assert resp.status_code == 200 and resp.json()["personalized"] is True
    # Shopify retries webhooks; a second delivery must not duplicate anything.
    client.post("/webhooks/shopify/orders-create", content=body, headers=headers)

    order = admin.get("/admin/api/orders/4400000000001").json()
    assert order["name"] == "#1042" and order["customer"] == "Sofia Lopez"
    assert len(order["items"]) == 1
    item = order["items"][0]
    assert item["status"] == "ready" and item["quantity"] == 80
    pdf = admin.get(f"/admin/api/order-items/{item['id']}/print.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert svc.get_design(design["id"])["status"] == "ordered"

    assert admin.post("/admin/api/orders/4400000000001/status", json={"status": "shipped"}).json()["status"] == "shipped"
    assert admin.post("/admin/api/orders/4400000000001/status", json={"status": "lost"}).status_code == 422


def test_webhook_without_personalised_items(client, admin, template):
    payload = {"id": 77, "line_items": [{"id": 1, "title": "Gift card", "quantity": 1, "properties": []}]}
    body = json.dumps(payload).encode()
    resp = client.post("/webhooks/shopify/orders-create", content=body, headers={"X-Shopify-Hmac-Sha256": sign(body, "s3cret")})
    assert resp.json()["personalized"] is False
    assert admin.get("/admin/api/orders").json() == []


def test_unapproved_design_in_order_is_flagged(client, admin, template):
    d = client.post("/api/designs", json={"templateId": "wedding-invitation", "values": {}}).json()
    body = json.dumps(order_payload(d["id"], order_id=99)).encode()
    client.post("/webhooks/shopify/orders-create", content=body, headers={"X-Shopify-Hmac-Sha256": sign(body, "s3cret")})
    item = admin.get("/admin/api/orders/99").json()["items"][0]
    assert item["status"] == "error" and "never approved" in item["error"]


def test_demo_store_checkout(client, admin, template):
    design = approved_design(client)
    page = client.get("/demo-store/products/wedding-invitation")
    assert page.status_code == 200 and "jasmin-personalize.js" in page.text
    variant = int(page.text.split('data-variant-id="')[1].split('"')[0])
    resp = client.post(
        "/demo-store/cart/add.js",
        json={"items": [{"id": variant, "quantity": 30, "properties": {"Personalization": design["summary"], "_design_id": design["id"]}}]},
    )
    assert resp.status_code == 200
    cart = client.get("/demo-store/cart")
    assert "Personalization:" in cart.text and "_design_id" not in cart.text
    resp = client.post(
        "/demo-store/checkout",
        data={"name": "Ana Silva", "email": "a@example.com", "address1": "1 Rua", "city": "Porto", "zip": "4000", "country": "PT"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    orders = admin.get("/admin/api/orders").json()
    assert len(orders) == 1 and orders[0]["source"] == "demo" and orders[0]["items"][0]["status"] == "ready"
