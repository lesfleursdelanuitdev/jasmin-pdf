"""Domain logic: templates, uploads, designs and orders."""
from __future__ import annotations

import copy
import hashlib
import io
import logging
import math
import re
import secrets
import shutil
import threading
import warnings
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

from .config import ROOT, Settings
from .db import dumps, loads, now, open_database
from .fonts import FontError, FontLibrary
from .pdf import render
from .pdf.analyze import AnalysisError, analyze_pdf, slugify
from .storage import Storage, open_storage

log = logging.getLogger("jasmin")

MAX_UPLOAD_BYTES = 30 * 1024 * 1024
# A photo is decoded in memory at four bytes a pixel and more than once, so its pixel count
# is bounded too: a small file can claim an enormous canvas. 64 megapixels is above what
# phone and consumer cameras produce.
MAX_UPLOAD_PIXELS = 64_000_000
UPLOAD_FORMATS = ["JPEG", "PNG", "WEBP"]
Image.MAX_IMAGE_PIXELS = MAX_UPLOAD_PIXELS
MAX_PDF_BYTES = 50 * 1024 * 1024  # the storage bucket takes files up to 50 MB
PREVIEW_EDGE = 1600
PROOF_DPI = 110
ORDER_STATUSES = ("received", "in_production", "shipped")


class NotFound(LookupError):
    pass


class Invalid(ValueError):
    def __init__(self, message: str, errors: list[str] | None = None):
        super().__init__(message)
        self.errors = errors or [message]


def new_id(prefix: str, nbytes: int = 9) -> str:
    return f"{prefix}_{secrets.token_urlsafe(nbytes).replace('-', 'x').replace('_', 'y')}"


def active_elements(definition: dict) -> list[dict]:
    return [e for e in definition["elements"] if e.get("enabled", True)]


def _render_view(definition: dict) -> dict:
    """The definition the renderer sees: disabled elements become static art."""
    return {**definition, "elements": active_elements(definition)}


# Element fields the designer may edit, with their coercion.
def _num(lo: float, hi: float):
    def coerce(v: Any) -> float:
        f = float(v)
        if not math.isfinite(f):
            raise ValueError("not a number")
        return min(max(f, lo), hi)

    return coerce


def _choice(*options: str):
    def coerce(v: Any) -> str:
        if v not in options:
            raise ValueError(f"must be one of {', '.join(options)}")
        return v

    return coerce


def _text(limit: int):
    def coerce(v: Any) -> str:
        return str(v)[:limit]

    return coerce


COMMON_FIELDS = {
    "label": _text(80),
    "help": _text(300),
    "enabled": bool,
    "required": bool,
}
TEXT_FIELDS = {
    **COMMON_FIELDS,
    "default": _text(2000),
    "placeholder": _text(200),
    "fontSize": _num(1, 400),
    "minFontSize": _num(1, 400),
    "lineHeight": _num(0.5, 4),
    "letterSpacing": _num(-0.2, 2),
    "align": _choice("left", "center", "right"),
    "valign": _choice("top", "middle", "bottom"),
    "autoFit": bool,
    "transform": _choice("none", "uppercase"),
    "multiline": bool,
    "maxLines": lambda v: int(_num(1, 20)(v)),
    "maxLength": lambda v: int(_num(1, 2000)(v)),
}
IMAGE_FIELDS = {
    **COMMON_FIELDS,
    "minDpi": _num(30, 1200),
    "keepOriginalIfEmpty": bool,
}


def _hex_color(value: str) -> dict:
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", value.strip())
    if not m:
        raise ValueError("colour must look like #a8834a")
    h = m.group(1).lower()
    rgb = [round(int(h[i : i + 2], 16) / 255, 4) for i in (0, 2, 4)]
    return {"space": "rgb", "values": rgb, "hex": f"#{h}"}


class Services:
    def __init__(self, settings: Settings, storage: Storage | None = None):
        self.settings = settings
        data = settings.data_dir
        self.dirs = {
            "pdfs": data / "pdfs",
            "templates": data / "templates",
            "uploads": data / "uploads",
            "designs": data / "designs",
            "print": data / "print",
            "fonts": data / "fonts",
        }
        for d in self.dirs.values():
            d.mkdir(parents=True, exist_ok=True)
        self.db = open_database(data / "jasmin.db", settings.database_url, settings.database_url_unpooled)
        # The store of record for everything under data (see app/storage.py).
        self.storage = storage or open_storage(settings)
        self._upload_lock = threading.Lock()
        if self.storage.remote:
            self.storage.ensure_bucket()
            self.storage.sync_down("fonts")
        self.fonts = FontLibrary(ROOT / "fonts", self.dirs["fonts"])

    # ------------------------------------------------------------------
    # Templates
    # ------------------------------------------------------------------
    def _template_row(self, row: dict | None) -> dict:
        if row is None:
            raise NotFound("template not found")
        row["definition"] = loads(row["definition"], {})
        row["product_handles"] = loads(row["product_handles"], [])
        return row

    def get_template(self, template_id: str) -> dict:
        return self._template_row(self.db.one("SELECT * FROM templates WHERE id=?", (template_id,)))

    def list_templates(self, status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM templates" + (" WHERE status=?" if status else "") + " ORDER BY updated_at DESC"
        return [self._template_row(r) for r in self.db.all(sql, (status,) if status else ())]

    def _pdf(self, pdf_file: str) -> Path:
        self.storage.ensure(f"pdfs/{pdf_file}")
        return self.dirs["pdfs"] / pdf_file

    def pdf_path(self, template: dict) -> Path:
        return self._pdf(template["pdf_file"])

    def assets_dir(self, template_id: str, revision: int) -> Path:
        return self.dirs["templates"] / template_id / f"r{revision}"

    @staticmethod
    def _assets_key(template_id: str, revision: int) -> str:
        return f"templates/{template_id}/r{revision}"

    def assets_url(self, template_id: str, revision: int) -> str:
        return f"/assets/templates/{template_id}/r{revision}"

    def create_template(self, name: str, pdf_bytes: bytes, template_id: str | None = None) -> dict:
        if not pdf_bytes.startswith(b"%PDF"):
            raise Invalid("That file is not a PDF.")
        if len(pdf_bytes) > MAX_PDF_BYTES:
            raise Invalid("PDF is larger than 50 MB.")
        digest = hashlib.sha256(pdf_bytes).hexdigest()[:32]
        pdf_file = f"{digest}.pdf"
        pdf_path = self.dirs["pdfs"] / pdf_file
        if not self.storage.ensure(f"pdfs/{pdf_file}"):
            pdf_path.write_bytes(pdf_bytes)
        self.storage.put(f"pdfs/{pdf_file}")
        try:
            analysis = analyze_pdf(pdf_path, self.fonts)
        except AnalysisError as exc:
            raise Invalid(str(exc)) from exc

        base = slugify(template_id or name)[:48]
        tid, n = base, 2
        while self.db.one("SELECT id FROM templates WHERE id=?", (tid,)):
            tid, n = f"{base}-{n}", n + 1
        definition = {**analysis, "name": name}
        definition["stacks"] = self._build_assets(tid, 1, definition, pdf_path)
        ts = now()
        self.db.execute(
            "INSERT INTO templates (id, name, status, pdf_file, revision, definition, product_handles, created_at, updated_at)"
            " VALUES (?, ?, 'draft', ?, 1, ?, '[]', ?, ?)",
            (tid, name, pdf_file, dumps(definition), ts, ts),
        )
        return self.get_template(tid)

    def _build_assets(self, tid: str, revision: int, definition: dict, pdf_path: Path) -> list[dict]:
        out = self.assets_dir(tid, revision)
        if out.exists():
            shutil.rmtree(out)
        self.storage.delete_tree(self._assets_key(tid, revision))
        stacks = render.build_template_assets(_render_view(definition), pdf_path, out)
        self.storage.put_tree(self._assets_key(tid, revision))
        return stacks

    def update_template(self, template_id: str, patch: dict) -> dict:
        t = self.get_template(template_id)
        definition = copy.deepcopy(t["definition"])
        name = t["name"]
        if "name" in patch:
            name = str(patch["name"]).strip()[:120] or name
            definition["name"] = name
        rebuild = False
        if "elements" in patch:
            by_id = {e["id"]: e for e in definition["elements"]}
            for change in patch["elements"]:
                el = by_id.get(change.get("id"))
                if el is None:
                    raise Invalid(f"Unknown element {change.get('id')!r}")
                before = el.get("enabled", True)
                self._apply_element_change(el, change)
                rebuild = rebuild or before != el.get("enabled", True)
        handles = t["product_handles"]
        if "productHandles" in patch:
            handles = sorted({slugify(h) for h in patch["productHandles"] if str(h).strip()})
            for h in handles:
                clash = self.db.template_for_handle(h, exclude=template_id)
                if clash:
                    raise Invalid(f"Product '{h}' is already linked to template '{clash['id']}'.")
        status = t["status"]
        if "status" in patch:
            if patch["status"] not in ("draft", "published"):
                raise Invalid("status must be draft or published")
            status = patch["status"]
        revision = t["revision"] + 1
        if rebuild or not self.storage.ensure_tree(self._assets_key(template_id, t["revision"])):
            definition["stacks"] = self._build_assets(template_id, revision, definition, self.pdf_path(t))
        else:
            # A save that failed half-way may have left this revision's folder behind.
            shutil.rmtree(self.assets_dir(template_id, revision), ignore_errors=True)
            shutil.copytree(self.assets_dir(template_id, t["revision"]), self.assets_dir(template_id, revision))
            self.storage.put_tree(self._assets_key(template_id, revision))
        self.db.execute(
            "UPDATE templates SET name=?, status=?, revision=?, definition=?, product_handles=?, updated_at=? WHERE id=?",
            (name, status, revision, dumps(definition), dumps(handles), now(), template_id),
        )
        return self.get_template(template_id)

    def _apply_element_change(self, el: dict, change: dict) -> None:
        fields = TEXT_FIELDS if el["type"] == "text" else IMAGE_FIELDS
        try:
            for key, value in change.items():
                if key in ("id", "type", "layer", "page", "sample", "clipPath", "sourceFont"):
                    continue
                if key == "box":
                    el["box"] = {k: round(_num(-5000, 5000)(value[k]), 3) for k in ("x", "y", "w", "h")}
                    if el["box"]["w"] <= 1 or el["box"]["h"] <= 1:
                        raise ValueError("box is too small")
                elif key == "font" and el["type"] == "text":
                    if self.fonts.get(value) is None:
                        raise ValueError(f"font {value!r} is not installed")
                    el["font"] = value
                elif key == "color" and el["type"] == "text":
                    if isinstance(value, str):
                        el["color"] = _hex_color(value)
                    elif isinstance(value, dict) and value.get("hex") != el.get("color", {}).get("hex"):
                        el["color"] = _hex_color(value.get("hex", ""))
                elif key in fields:
                    el[key] = fields[key](value)
            if el["type"] == "text":
                el["minFontSize"] = min(el.get("minFontSize", el["fontSize"]), el["fontSize"])
        except (ValueError, TypeError, KeyError) as exc:
            raise Invalid(f"{el.get('label', el['id'])}: {exc}") from exc

    def delete_template(self, template_id: str) -> None:
        self.get_template(template_id)
        self.db.execute("DELETE FROM templates WHERE id=?", (template_id,))
        shutil.rmtree(self.dirs["templates"] / template_id, ignore_errors=True)
        self.storage.delete_tree(f"templates/{template_id}")

    def template_for_product(self, handle: str) -> dict | None:
        """Published template linked to a Shopify product handle.

        Explicit links (admin) win; otherwise a template whose id equals the
        handle and that has no explicit links is used.
        """
        handle = slugify(handle)
        row = self.db.template_for_handle(handle, published=True) or self.db.one(
            "SELECT * FROM templates WHERE id=? AND status='published' AND product_handles='[]'",
            (handle,),
        )
        return self._template_row(row) if row else None

    def editor_payload(self, t: dict) -> dict:
        definition = t["definition"]
        elements = active_elements(definition)
        fonts = {e["font"] for e in elements if e["type"] == "text"}
        return {
            "id": t["id"],
            "name": t["name"],
            "status": t["status"],
            "revision": t["revision"],
            "assets": self.assets_url(t["id"], t["revision"]),
            "pages": definition["pages"],
            "stacks": definition["stacks"],
            "elements": elements,
            "fonts": {name: self.fonts.require(name).metrics() for name in sorted(fonts)},
        }

    # ------------------------------------------------------------------
    # Uploads
    # ------------------------------------------------------------------
    def save_upload(self, filename: str, data: bytes) -> dict:
        if len(data) > MAX_UPLOAD_BYTES:
            raise Invalid("Photos must be smaller than 30 MB.")
        too_large = "That photo is too large (more than 64 megapixels). Please upload a smaller version of it."
        # One photo is decoded at a time: several large ones at once would not fit in memory.
        with self._upload_lock:
            return self._save_upload(filename, data, too_large)

    def _save_upload(self, filename: str, data: bytes, too_large: str) -> dict:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                # The size is read from the header here; nothing is decoded until load().
                with Image.open(io.BytesIO(data), formats=UPLOAD_FORMATS) as probe:
                    if probe.width * probe.height > MAX_UPLOAD_PIXELS:
                        raise Invalid(too_large)
                    probe.verify()
                img = Image.open(io.BytesIO(data), formats=UPLOAD_FORMATS)
                img = ImageOps.exif_transpose(img)
                img.load()
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise Invalid(too_large) from exc
        except (UnidentifiedImageError, OSError) as exc:
            raise Invalid("That file is not a supported image (use JPEG, PNG or WebP).") from exc
        if min(img.size) < 32:
            raise Invalid("That image is too small.")
        uid = new_id("img")
        has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
        if has_alpha:
            img = img.convert("RGBA")
            path = self.dirs["uploads"] / f"{uid}.png"
            img.save(path)
        else:
            img = img.convert("RGB")
            path = self.dirs["uploads"] / f"{uid}.jpg"
            img.save(path, quality=95, subsampling=0)
        # Scaled straight from the photo: no second full-size copy is made for it.
        preview = img if max(img.size) <= PREVIEW_EDGE else ImageOps.contain(img, (PREVIEW_EDGE, PREVIEW_EDGE), Image.LANCZOS)
        preview_path = self.dirs["uploads"] / f"{uid}-preview.jpg"
        preview.convert("RGB").save(preview_path, quality=85)
        self.storage.put(f"uploads/{path.name}")
        self.storage.put(f"uploads/{preview_path.name}")
        self.db.execute(
            "INSERT INTO uploads (id, filename, path, preview_path, width, height, created_at) VALUES (?,?,?,?,?,?,?)",
            (uid, filename[:200], path.name, preview_path.name, img.width, img.height, now()),
        )
        return self.upload_public(self.get_upload(uid))

    def get_upload(self, upload_id: str) -> dict:
        row = self.db.one("SELECT * FROM uploads WHERE id=?", (upload_id,))
        if row is None:
            raise NotFound("upload not found")
        return row

    def upload_public(self, row: dict) -> dict:
        return {
            "id": row["id"],
            "width": row["width"],
            "height": row["height"],
            "previewUrl": f"/assets/uploads/{row['preview_path']}",
        }

    def upload_ref(self, upload_id: str) -> render.UploadRef | None:
        try:
            row = self.get_upload(upload_id)
        except NotFound:
            return None
        self.storage.ensure(f"uploads/{row['path']}")
        return render.UploadRef(self.dirs["uploads"] / row["path"], row["width"], row["height"])

    # ------------------------------------------------------------------
    # Designs
    # ------------------------------------------------------------------
    def _clean_values(self, definition: dict, values: Any) -> dict:
        if not isinstance(values, dict):
            raise Invalid("values must be an object")
        clean: dict[str, dict] = {}
        for el in definition["elements"]:
            v = values.get(el["id"])
            if not isinstance(v, dict):
                continue
            if el["type"] == "text":
                if "text" in v:
                    clean[el["id"]] = {"text": str(v["text"])[:4000]}
            else:
                upload = v.get("upload")
                if upload:
                    item = {"upload": str(upload)[:64]}
                    for key in ("zoom", "dx", "dy"):
                        try:
                            f = float(v.get(key, 1 if key == "zoom" else 0))
                        except (TypeError, ValueError):
                            continue
                        if math.isfinite(f):
                            item[key] = f
                    clean[el["id"]] = item
        return clean

    def _design_row(self, row: dict | None) -> dict:
        if row is None:
            raise NotFound("design not found")
        row["values"] = loads(row["values"], {})
        row["warnings"] = loads(row["warnings"], [])
        row["snapshot"] = loads(row["snapshot"], None)
        return row

    def get_design(self, design_id: str) -> dict:
        return self._design_row(self.db.one("SELECT * FROM designs WHERE id=?", (design_id,)))

    def create_design(self, template_id: str, values: Any) -> dict:
        t = self.get_template(template_id)
        did = new_id("dsn")
        ts = now()
        clean = self._clean_values(t["definition"], values or {})
        self.db.execute(
            'INSERT INTO designs (id, template_id, template_revision, "values", status, created_at, updated_at)'
            " VALUES (?,?,?,?, 'draft', ?, ?)",
            (did, template_id, t["revision"], dumps(clean), ts, ts),
        )
        return self.get_design(did)

    def update_design(self, design_id: str, values: Any) -> dict:
        d = self.get_design(design_id)
        if d["status"] != "draft":
            raise Invalid("This design has already been approved and can no longer be changed.")
        t = self.get_template(d["template_id"])
        clean = self._clean_values(t["definition"], values)
        self.db.execute(
            'UPDATE designs SET "values"=?, template_revision=?, updated_at=? WHERE id=?',
            (dumps(clean), t["revision"], now(), design_id),
        )
        return self.get_design(design_id)

    def design_public(self, d: dict) -> dict:
        out = {
            "id": d["id"],
            "templateId": d["template_id"],
            "status": d["status"],
            "values": d["values"],
            "summary": d["summary"],
            "warnings": d["warnings"],
        }
        if d["status"] != "draft":
            out["thumbnailUrl"] = f"/api/designs/{d['id']}/thumbnail.jpg"
            out["proofPdfUrl"] = f"/api/designs/{d['id']}/proof.pdf"
        return out

    def design_dir(self, design_id: str) -> Path:
        return self.dirs["designs"] / design_id

    def _render(self, definition: dict, values: dict) -> bytes:
        t_pdf = self._pdf(definition["_pdf_file"])
        return render.render_design_pdf(
            _render_view(definition), t_pdf, values, self.upload_ref, self.fonts, self.settings.print_max_dpi
        )

    def _definition_with_pdf(self, design: dict) -> dict:
        if design["snapshot"]:
            return design["snapshot"]
        t = self.get_template(design["template_id"])
        return {**t["definition"], "_pdf_file": t["pdf_file"], "_revision": t["revision"]}

    def validate_design(self, design: dict) -> tuple[list[str], list[str]]:
        definition = self._definition_with_pdf(design)
        return render.validate_values(_render_view(definition), design["values"], self.upload_ref, self.fonts)

    def proof(self, design_id: str) -> dict:
        d = self.get_design(design_id)
        definition = self._definition_with_pdf(d)
        errors, warnings = render.validate_values(_render_view(definition), d["values"], self.upload_ref, self.fonts)
        if errors:
            return {"errors": errors, "warnings": warnings, "pages": []}
        pdf = self._render(definition, d["values"])
        out = self.design_dir(design_id)
        out.mkdir(parents=True, exist_ok=True)
        (out / "proof.pdf").write_bytes(pdf)
        stamp = secrets.token_hex(4)
        pages = []
        for p in range(len(definition["pages"])):
            render.render_png(pdf, p, PROOF_DPI).save(out / f"proof-p{p}.jpg", quality=88)
            pages.append(f"/api/designs/{design_id}/proof-p{p}.jpg?v={stamp}")
        thumb = render.render_png(pdf, 0, 60)
        trim = definition["pages"][0]["trimBox"]
        k = 60 / 72
        thumb = thumb.crop(
            (round(trim["x"] * k), round(trim["y"] * k), round((trim["x"] + trim["w"]) * k), round((trim["y"] + trim["h"]) * k))
        )
        thumb.save(out / "thumbnail.jpg", quality=85)
        self.storage.put_tree(f"designs/{design_id}")
        return {"errors": [], "warnings": warnings, "pages": pages, "pdfUrl": f"/api/designs/{design_id}/proof.pdf?v={stamp}"}

    def approve(self, design_id: str) -> dict:
        d = self.get_design(design_id)
        if d["status"] != "draft":
            return self.design_public(d)
        definition = self._definition_with_pdf(d)
        result = self.proof(design_id)
        if result["errors"]:
            raise Invalid("Please fix the highlighted fields.", result["errors"])
        summary = self._summary(definition, d["values"])
        ts = now()
        self.db.execute(
            "UPDATE designs SET status='approved', snapshot=?, summary=?, warnings=?, approved_at=?, updated_at=? WHERE id=?",
            (dumps(definition), summary, dumps(result["warnings"]), ts, ts, design_id),
        )
        return self.design_public(self.get_design(design_id))

    @staticmethod
    def _summary(definition: dict, values: dict) -> str:
        parts = []
        for el in active_elements(definition):
            if el["type"] != "text":
                continue
            text = (values.get(el["id"]) or {}).get("text", el.get("default", ""))
            text = " ".join((text or "").split())
            if text:
                parts.append(text)
        summary = " / ".join(parts) or "Personalised design"
        return summary if len(summary) <= 90 else summary[:87].rstrip() + "..."

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------
    def record_order(self, payload: dict, source: str = "shopify", shop: str = "") -> dict | None:
        """Store a Shopify ``orders/create`` payload; returns None if nothing is personalised."""
        order_id = str(payload.get("id") or "")
        if not order_id:
            raise Invalid("order payload has no id")
        items = []
        for li in payload.get("line_items") or []:
            props = {str(p.get("name")): p.get("value") for p in (li.get("properties") or []) if isinstance(p, dict)}
            design_id = props.get("_design_id") or props.get("_jasmin_design")
            if design_id:
                items.append((li, str(design_id)))
        if not items:
            return None
        customer = payload.get("customer") or {}
        address = payload.get("shipping_address") or {}
        name = " ".join(filter(None, [customer.get("first_name"), customer.get("last_name")])) or address.get("name") or ""
        if not self.db.one("SELECT id FROM orders WHERE id=?", (order_id,)):
            self.db.execute(
                "INSERT INTO orders (id, name, email, customer, shop, source, status, total, currency, shipping_address, created_at, raw)"
                " VALUES (?,?,?,?,?,?, 'received', ?,?,?,?,?)",
                (
                    order_id,
                    payload.get("name") or f"#{order_id}",
                    payload.get("email") or payload.get("contact_email") or "",
                    name,
                    shop,
                    source,
                    str(payload.get("total_price") or ""),
                    payload.get("currency") or "",
                    dumps(address),
                    payload.get("created_at") or now(),
                    dumps(payload),
                ),
            )
        for li, design_id in items:
            self.db.execute(
                "INSERT INTO order_items (order_id, line_item_id, design_id, title, variant, quantity)"
                " VALUES (?,?,?,?,?,?) ON CONFLICT (order_id, line_item_id) DO NOTHING",
                (order_id, str(li.get("id")), design_id, li.get("title") or "", li.get("variant_title") or "", int(li.get("quantity") or 1)),
            )
        return self.get_order(order_id)

    def generate_print_files(self, order_id: str) -> None:
        for item in self.db.all("SELECT * FROM order_items WHERE order_id=? AND status<>'ready'", (order_id,)):
            self.generate_print_file(item["id"])

    def generate_print_file(self, item_id: int) -> dict:
        item = self.db.one("SELECT * FROM order_items WHERE id=?", (item_id,))
        if item is None:
            raise NotFound("order item not found")
        order = self.db.one("SELECT * FROM orders WHERE id=?", (item["order_id"],))
        try:
            d = self.get_design(item["design_id"])
            if d["status"] == "draft":
                raise Invalid("design was never approved by the customer")
            definition = self._definition_with_pdf(d)
            pdf = self._render(definition, d["values"])
            folder = self.dirs["print"] / slugify(order["id"])
            folder.mkdir(parents=True, exist_ok=True)
            order_no = slugify(order["name"] or order["id"])
            filename = f"{order_no}-{item['line_item_id']}-{slugify(d['template_id'])}-qty{item['quantity']}.pdf"
            (folder / filename).write_bytes(pdf)
            self.storage.put(f"print/{folder.name}/{filename}")
            self.db.execute(
                "UPDATE order_items SET print_file=?, status='ready', error=NULL WHERE id=?",
                (f"{folder.name}/{filename}", item_id),
            )
            self.db.execute("UPDATE designs SET status='ordered', updated_at=? WHERE id=?", (now(), d["id"]))
        except Exception as exc:  # whatever it was, the item must say so and not stay "pending" for ever
            log.exception("print file for item %s failed", item_id)
            self.db.execute("UPDATE order_items SET status='error', error=? WHERE id=?", (str(exc), item_id))
        return self.db.one("SELECT * FROM order_items WHERE id=?", (item_id,))  # type: ignore[return-value]

    @staticmethod
    def _order_out(order: dict, items: list[dict]) -> dict:
        order["shipping_address"] = loads(order["shipping_address"], {})
        order.pop("raw", None)
        order["items"] = items
        for item in items:
            item["thumbnailUrl"] = f"/api/designs/{item['design_id']}/thumbnail.jpg" if item["design_id"] else None
        return order

    def pending_orders(self) -> list[str]:
        """Orders with an item whose print file was never made (the app stopped while it was being rendered)."""
        return [r["order_id"] for r in self.db.all("SELECT DISTINCT order_id FROM order_items WHERE status='pending'")]

    def get_order(self, order_id: str) -> dict:
        order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
        if order is None:
            raise NotFound("order not found")
        return self._order_out(order, self.db.all("SELECT * FROM order_items WHERE order_id=? ORDER BY id", (order_id,)))

    def list_orders(self) -> list[dict]:
        # Two queries however many orders there are.
        items: dict[str, list[dict]] = {}
        for item in self.db.all("SELECT * FROM order_items ORDER BY id"):
            items.setdefault(item["order_id"], []).append(item)
        return [self._order_out(o, items.get(o["id"], [])) for o in self.db.all("SELECT * FROM orders ORDER BY created_at DESC")]

    def set_order_status(self, order_id: str, status: str) -> dict:
        self.get_order(order_id)
        if status not in ORDER_STATUSES:
            raise Invalid(f"status must be one of {', '.join(ORDER_STATUSES)}")
        self.db.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
        return self.get_order(order_id)
