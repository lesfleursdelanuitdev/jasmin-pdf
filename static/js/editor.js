// Customer editor: live, z-ordered SVG preview + fields panel + proof/approve.
import { FontMetrics, MAX_ZOOM, glyphPositions, layoutText, pathData, placeImage } from "./layout.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const params = new URLSearchParams(location.search);
const templateId = decodeURIComponent(location.pathname.split("/").filter(Boolean).pop() || "");
const embedded = params.get("embed") === "1" && window.parent !== window;
const parentOrigin = params.get("origin") || (document.referrer ? new URL(document.referrer).origin : "");
const storageKey = `jasmin:design:${templateId}`;

const state = {
  template: null,
  fonts: {},
  page: 0,
  values: {},
  uploads: {},
  uploading: new Set(),
  designId: null,
  selected: null,
  dirty: false,
  savePromise: null,
};

const $ = (sel) => document.querySelector(sel);

// ---------------------------------------------------------------------------
// Small DOM helpers
// ---------------------------------------------------------------------------
function svg(tag, attrs = {}, children = []) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) node.setAttribute(k, v);
  node.append(...children);
  return node;
}

function h(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k in node && typeof v !== "string") node[k] = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  node.append(...children);
  return node;
}

let toastTimer;
function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 4200);
}

async function api(method, url, body) {
  const opts = { method, headers: {} };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.detail || `Request failed (${res.status})`);
    err.errors = data.errors || [err.message];
    err.status = res.status;
    throw err;
  }
  return data;
}

function storage(action, value) {
  try {
    if (action === "get") return localStorage.getItem(storageKey);
    if (action === "set") localStorage.setItem(storageKey, value);
    if (action === "remove") localStorage.removeItem(storageKey);
  } catch {
    /* private mode / blocked storage: drafts just won't survive a reload */
  }
  return null;
}

// ---------------------------------------------------------------------------
// Template helpers
// ---------------------------------------------------------------------------
const fontFamily = (name) => `jf-${name}`;
const elements = () => state.template.elements;
const pageElements = (p = state.page) => elements().filter((e) => e.page === p);
const elementById = (id) => elements().find((e) => e.id === id);
const pageName = (i, n) => (n === 2 ? ["Front", "Back"][i] : n === 1 ? "Design" : `Page ${i + 1}`);

function textValue(el) {
  const v = state.values[el.id];
  return v && typeof v.text === "string" ? v.text : el.default || "";
}

function imageValue(el) {
  const v = state.values[el.id];
  return v && v.upload && state.uploads[v.upload] ? v : null;
}

function textIssues(el) {
  const text = textValue(el);
  const font = state.fonts[el.font];
  const lay = layoutText(el, text, font);
  const issues = [];
  if (!text.trim()) {
    if (el.required !== false) issues.push({ level: "bad", text: "Please enter some text." });
    return { lay, issues };
  }
  const shown = el.transform === "uppercase" ? text.toUpperCase() : text;
  const missing = font.unsupported(shown.normalize("NFC"));
  if (missing.length) issues.push({ level: "bad", text: `Not available in this font: ${missing.join(" ")}` });
  if (el.multiline && text.split("\n").length > el.maxLines)
    issues.push({ level: "bad", text: `Use at most ${el.maxLines} line${el.maxLines > 1 ? "s" : ""}.` });
  if (text.length > el.maxLength) issues.push({ level: "bad", text: `Use at most ${el.maxLength} characters.` });
  if (lay.overflow) issues.push({ level: "bad", text: "Too long to fit. Please shorten it." });
  else if (lay.fontSize < el.fontSize - 0.01) issues.push({ level: "info", text: "Text size reduced to fit." });
  return { lay, issues };
}

function imageQuality(el) {
  const v = imageValue(el);
  if (!v) return null;
  const up = state.uploads[v.upload];
  const p = placeImage(el.box, up.width, up.height, v.zoom, v.dx, v.dy);
  const min = el.minDpi || 150;
  if (p.dpi < min) return { level: "bad", label: "Low", dpi: p.dpi };
  if (p.dpi < 250) return { level: "warn", label: "Good", dpi: p.dpi };
  return { level: "ok", label: "Excellent", dpi: p.dpi };
}

function pageHasMissing(p) {
  return pageElements(p).some((el) =>
    el.type === "image"
      ? el.required !== false && !el.keepOriginalIfEmpty && !imageValue(el)
      : textIssues(el).issues.some((i) => i.level === "bad"),
  );
}

// ---------------------------------------------------------------------------
// Stage rendering
// ---------------------------------------------------------------------------
function renderStage() {
  const t = state.template;
  const page = t.pages[state.page];
  const stack = (t.stacks.find((s) => s.page === state.page) || { stack: [] }).stack;
  const root = $("#paper");
  root.replaceChildren();
  root.setAttribute("viewBox", `0 0 ${page.width} ${page.height}`);
  const defs = svg("defs");
  root.append(defs, svg("rect", { x: 0, y: 0, width: page.width, height: page.height, fill: "#fff" }));

  for (const el of pageElements()) {
    const clip = el.clipPath
      ? svg("path", { d: pathData(el.clipPath) })
      : svg("rect", { x: el.box.x, y: el.box.y, width: el.box.w, height: el.box.h });
    if (el.type === "image") defs.append(svg("clipPath", { id: `clip-${el.id}` }, [clip]));
  }
  for (const item of stack) {
    if (item.kind === "static") {
      root.append(
        svg("image", {
          href: `${t.assets}/${item.src}`,
          x: 0,
          y: 0,
          width: page.width,
          height: page.height,
          preserveAspectRatio: "none",
        }),
      );
    } else {
      const el = elementById(item.id);
      if (el) root.append(renderElement(el));
    }
  }
  root.append(renderTrim(page));
  const hits = svg("g", { class: "hits" });
  for (const el of pageElements()) hits.append(renderHit(el));
  root.append(hits);
  fitPaper();
}

function renderElement(el) {
  const g = svg("g", { "data-el": el.id, class: `el el-${el.type}` });
  const page = state.template.pages[el.page];
  if (el.type === "image") {
    const v = imageValue(el);
    if (v) {
      const up = state.uploads[v.upload];
      const p = placeImage(el.box, up.width, up.height, v.zoom, v.dx, v.dy);
      g.append(
        svg("image", {
          href: up.previewUrl,
          x: p.x,
          y: p.y,
          width: p.w,
          height: p.h,
          preserveAspectRatio: "none",
          "clip-path": `url(#clip-${el.id})`,
        }),
      );
    } else {
      // The designer's sample photo stands in until the customer adds one.
      g.append(
        svg("image", {
          href: `${state.template.assets}/p${el.page}-layer-${el.id}.png`,
          x: 0,
          y: 0,
          width: page.width,
          height: page.height,
          preserveAspectRatio: "none",
          opacity: el.keepOriginalIfEmpty ? 1 : 0.55,
        }),
      );
    }
    if (state.uploading.has(el.id)) {
      g.append(
        svg("rect", {
          class: "uploading-veil",
          x: el.box.x,
          y: el.box.y,
          width: el.box.w,
          height: el.box.h,
          "clip-path": `url(#clip-${el.id})`,
        }),
      );
    }
    return g;
  }
  const font = state.fonts[el.font];
  const lay = layoutText(el, textValue(el), font);
  g.setAttribute("fill", el.color?.hex || "#000");
  g.setAttribute("font-family", fontFamily(el.font));
  g.setAttribute("font-size", lay.fontSize);
  for (const line of lay.lines) {
    if (!line.text) continue;
    const xs = glyphPositions(line, font, lay.fontSize, lay.letterSpacing);
    const node = svg("text", { x: xs.map((v) => v.toFixed(3)).join(" "), y: line.y.toFixed(3) });
    node.setAttributeNS("http://www.w3.org/XML/1998/namespace", "xml:space", "preserve");
    node.textContent = line.text;
    g.append(node);
  }
  return g;
}

function renderTrim(page) {
  const t = page.trimBox;
  const g = svg("g", { "aria-hidden": "true" });
  if (t.w < page.width - 0.5 || t.h < page.height - 0.5) {
    const d =
      `M0 0H${page.width}V${page.height}H0Z ` + `M${t.x} ${t.y}h${t.w}v${t.h}h${-t.w}Z`;
    g.append(svg("path", { d, class: "trim-shade", "fill-rule": "evenodd" }));
    g.append(svg("rect", { class: "trim-line", x: t.x, y: t.y, width: t.w, height: t.h }));
  }
  return g;
}

function hitShape(el) {
  if (el.type === "image" && el.clipPath) return svg("path", { d: pathData(el.clipPath) });
  return svg("rect", { x: el.box.x, y: el.box.y, width: el.box.w, height: el.box.h, rx: 1 });
}

function renderHit(el) {
  const g = svg("g", { "data-hit": el.id });
  const shape = hitShape(el);
  shape.classList.add("hit", `hit-${el.type}`);
  shape.setAttribute("tabindex", "0");
  shape.setAttribute("role", "button");
  shape.setAttribute("aria-label", el.type === "image" ? `${el.label}: add or move photo` : `Edit ${el.label}`);
  if (state.selected === el.id) shape.classList.add("selected");
  if (el.type === "image" && imageValue(el)) shape.classList.add("has-photo");
  g.append(shape);
  if (el.type === "image" && !imageValue(el) && !state.uploading.has(el.id)) g.append(addPhotoBadge(el));
  if (el.type === "image") bindImageHit(el, shape);
  else bindTextHit(el, shape);
  bindDrop(shape, el);
  return g;
}

function addPhotoBadge(el) {
  const size = Math.max(7, Math.min(11, el.box.w / 14));
  const label = el.keepOriginalIfEmpty ? "Add photo (optional)" : "Add your photo";
  const w = size * (label.length * 0.56) + size * 2;
  const hgt = size * 2.4;
  const cx = el.box.x + el.box.w / 2;
  const cy = el.box.y + el.box.h / 2;
  return svg("g", { class: "add-photo-badge" }, [
    svg("rect", { x: cx - w / 2, y: cy - hgt / 2, width: w, height: hgt, rx: hgt / 2 }),
    Object.assign(
      svg("text", { x: cx, y: cy + size * 0.36, "text-anchor": "middle", "font-size": size }),
      { textContent: label },
    ),
  ]);
}

function updateElement(id) {
  const el = elementById(id);
  if (!el || el.page !== state.page) return;
  const old = $(`#paper [data-el="${CSS.escape(id)}"]`);
  if (old) old.replaceWith(renderElement(el));
  const hit = $(`#paper [data-hit="${CSS.escape(id)}"]`);
  if (hit) hit.replaceWith(renderHit(el));
}

function fitPaper() {
  const page = state.template?.pages[state.page];
  if (!page) return;
  const box = $("#paper-shadow").getBoundingClientRect();
  const scale = Math.min(box.width / page.width, box.height / page.height);
  const paper = $("#paper");
  paper.style.width = `${Math.floor(page.width * scale)}px`;
  paper.style.height = `${Math.floor(page.height * scale)}px`;
}

function svgUnitsPerPixel() {
  const ctm = $("#paper").getScreenCTM();
  return ctm ? 1 / ctm.a : 1;
}

// ---------------------------------------------------------------------------
// Interaction
// ---------------------------------------------------------------------------
function select(id, { focus = false } = {}) {
  state.selected = id;
  document.querySelectorAll("#paper .hit").forEach((n) => n.classList.toggle("selected", n.parentNode.dataset.hit === id));
  document.querySelectorAll(".field").forEach((n) => n.classList.toggle("selected", n.dataset.field === id));
  const field = document.querySelector(`.field[data-field="${CSS.escape(id)}"]`);
  if (field) {
    field.scrollIntoView({ block: "nearest", behavior: "smooth" });
    if (focus) field.querySelector("input[type=text], textarea")?.focus();
  }
}

function bindTextHit(el, shape) {
  shape.addEventListener("click", () => select(el.id, { focus: true }));
  shape.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      select(el.id, { focus: true });
    }
  });
}

function setPlacement(el, patch) {
  const v = imageValue(el);
  if (!v) return;
  const up = state.uploads[v.upload];
  const next = { ...v, ...patch };
  const p = placeImage(el.box, up.width, up.height, next.zoom, next.dx, next.dy);
  state.values[el.id] = { upload: v.upload, zoom: p.zoom, dx: p.dx, dy: p.dy };
  const img = $(`#paper [data-el="${CSS.escape(el.id)}"] image`);
  if (img) {
    img.setAttribute("x", p.x);
    img.setAttribute("y", p.y);
    img.setAttribute("width", p.w);
    img.setAttribute("height", p.h);
  }
  refreshField(el.id);
  scheduleSave();
}

function bindImageHit(el, shape) {
  const pointers = new Map();
  let start = null;

  shape.addEventListener("pointerdown", (e) => {
    shape.setPointerCapture(e.pointerId);
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const v = imageValue(el);
    start = {
      x: e.clientX,
      y: e.clientY,
      moved: false,
      dx: v?.dx || 0,
      dy: v?.dy || 0,
      zoom: v?.zoom || 1,
      dist: null,
    };
    if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      start.dist = Math.hypot(a.x - b.x, a.y - b.y);
    }
    select(el.id);
  });

  shape.addEventListener("pointermove", (e) => {
    if (!start || !pointers.has(e.pointerId)) return;
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (Math.hypot(e.clientX - start.x, e.clientY - start.y) > 3) start.moved = true;
    if (!imageValue(el) || !start.moved) return;
    if (pointers.size === 2 && start.dist) {
      const [a, b] = [...pointers.values()];
      const ratio = Math.hypot(a.x - b.x, a.y - b.y) / start.dist;
      const zoom = Math.min(MAX_ZOOM, Math.max(1, start.zoom * ratio));
      setPlacement(el, { zoom, dx: (start.dx * zoom) / start.zoom, dy: (start.dy * zoom) / start.zoom });
      return;
    }
    shape.classList.add("dragging");
    const k = svgUnitsPerPixel();
    setPlacement(el, { dx: start.dx + (e.clientX - start.x) * k, dy: start.dy + (e.clientY - start.y) * k });
  });

  const end = (e) => {
    pointers.delete(e.pointerId);
    shape.classList.remove("dragging");
    if (start && !start.moved && pointers.size === 0 && !imageValue(el)) pickFile(el);
    if (pointers.size === 0) start = null;
  };
  shape.addEventListener("pointerup", end);
  shape.addEventListener("pointercancel", end);

  shape.addEventListener(
    "wheel",
    (e) => {
      const v = imageValue(el);
      if (!v) return;
      e.preventDefault();
      const zoom = Math.min(MAX_ZOOM, Math.max(1, v.zoom * Math.exp(-e.deltaY * 0.0015)));
      setPlacement(el, { zoom, dx: (v.dx * zoom) / v.zoom, dy: (v.dy * zoom) / v.zoom });
    },
    { passive: false },
  );

  shape.addEventListener("keydown", (e) => {
    const v = imageValue(el);
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      pickFile(el);
    } else if (v && e.key.startsWith("Arrow")) {
      e.preventDefault();
      const step = e.shiftKey ? 10 : 2;
      const d = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
      setPlacement(el, { dx: v.dx + d[0], dy: v.dy + d[1] });
    }
  });
}

function bindDrop(node, el) {
  if (el && el.type !== "image") return;
  node.addEventListener("dragenter", (e) => {
    e.preventDefault();
    node.classList.add("drop-target");
  });
  node.addEventListener("dragover", (e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
    node.classList.add("drop-target");
  });
  node.addEventListener("dragleave", () => node.classList.remove("drop-target"));
  node.addEventListener("drop", (e) => {
    e.preventDefault();
    e.stopPropagation();
    node.classList.remove("drop-target");
    const file = e.dataTransfer.files?.[0];
    if (file) uploadFor(el, file);
  });
}

let pickTarget = null;
function pickFile(el) {
  pickTarget = el;
  const input = $("#file-input");
  input.value = "";
  input.click();
}

async function uploadFor(el, file) {
  if (!/^image\/(jpeg|png|webp)$/.test(file.type)) {
    toast("Please choose a JPEG, PNG or WebP photo.");
    return;
  }
  state.uploading.add(el.id);
  updateElement(el.id);
  refreshField(el.id);
  try {
    const form = new FormData();
    form.append("file", file);
    const up = await api("POST", "/api/uploads", form);
    await preload(up.previewUrl);
    state.uploads[up.id] = up;
    state.values[el.id] = { upload: up.id, zoom: 1, dx: 0, dy: 0 };
    scheduleSave();
  } catch (err) {
    toast(err.message);
  } finally {
    state.uploading.delete(el.id);
    updateElement(el.id);
    refreshField(el.id);
    renderTabs();
    select(el.id);
  }
}

function preload(url) {
  return new Promise((resolve) => {
    const img = new Image();
    img.onload = img.onerror = () => resolve();
    img.src = url;
  });
}

// ---------------------------------------------------------------------------
// Fields panel
// ---------------------------------------------------------------------------
function renderPanel() {
  const n = state.template.pages.length;
  $("#panel-title").textContent = n > 1 ? `${pageName(state.page, n)} details` : "Your details";
  const fields = $("#fields");
  fields.replaceChildren(...pageElements().map((el) => (el.type === "image" ? imageField(el) : textField(el))));
  if (!pageElements().length) fields.append(h("p", { class: "field-help", text: "Nothing to personalize on this page." }));
  pageElements().forEach((el) => refreshField(el.id));
}

function textField(el) {
  const id = `f-${el.id}`;
  const input = el.multiline
    ? h("textarea", { id, rows: el.maxLines + 1, placeholder: el.placeholder || "" })
    : h("input", { id, type: "text", placeholder: el.placeholder || "", autocomplete: "off" });
  input.value = textValue(el);
  input.addEventListener("focus", () => select(el.id));
  input.addEventListener("input", () => {
    state.values[el.id] = { text: input.value };
    updateElement(el.id);
    refreshField(el.id);
    scheduleSave();
  });
  return h("div", { class: "field field-text", "data-field": el.id }, [
    h("div", { class: "field-head" }, [
      h("label", { for: id, text: el.label }),
      h("span", { class: "field-meta", "data-counter": "" }),
    ]),
    input,
    h("div", { class: "field-msg", "data-msg": "" }),
    el.help ? h("p", { class: "field-help", text: el.help }) : "",
  ]);
}

function imageField(el) {
  const thumb = h("button", { class: "photo-thumb", type: "button", "aria-label": `Choose photo for ${el.label}` });
  thumb.addEventListener("click", () => pickFile(el));
  bindDrop(thumb, el);
  const zoom = h("input", { type: "range", min: "1", max: "5", step: "0.01", "aria-label": "Zoom" });
  zoom.addEventListener("input", () => {
    const v = imageValue(el);
    if (!v) return;
    const z = Number(zoom.value);
    setPlacement(el, { zoom: z, dx: (v.dx * z) / v.zoom, dy: (v.dy * z) / v.zoom });
  });
  return h("div", { class: "field field-image", "data-field": el.id, onclick: () => select(el.id) }, [
    h("div", { class: "field-head" }, [
      h("label", { text: el.label }),
      h("span", {
        class: "field-meta",
        text: el.required === false || el.keepOriginalIfEmpty ? "Optional" : "Required",
      }),
    ]),
    h("div", { class: "photo-row" }, [
      thumb,
      h("div", { class: "photo-actions" }, [
        h("button", { class: "btn btn-small", type: "button", "data-upload": "", onclick: () => pickFile(el) }),
        h("button", {
          class: "btn-link",
          type: "button",
          "data-remove": "",
          text: "Remove photo",
          onclick: (e) => {
            e.stopPropagation();
            delete state.values[el.id];
            updateElement(el.id);
            refreshField(el.id);
            renderTabs();
            scheduleSave();
          },
        }),
      ]),
    ]),
    h("div", { class: "zoom-row", "data-zoom": "" }, [
      h("span", { text: "Zoom" }),
      zoom,
      h("button", {
        class: "btn-link",
        type: "button",
        text: "Center",
        onclick: () => setPlacement(el, { dx: 0, dy: 0 }),
      }),
    ]),
    h("div", { class: "quality", "data-quality": "" }),
    h("p", {
      class: "field-help",
      text: el.help || "Drag the photo on the design to reposition it; scroll or pinch to zoom.",
    }),
  ]);
}

function refreshField(id) {
  const el = elementById(id);
  const field = document.querySelector(`.field[data-field="${CSS.escape(id)}"]`);
  if (!el || !field) return;
  if (el.type === "text") {
    const { issues } = textIssues(el);
    const text = textValue(el);
    const counter = field.querySelector("[data-counter]");
    counter.textContent = `${text.length} / ${el.maxLength}`;
    counter.classList.toggle("over", text.length > el.maxLength);
    const msg = field.querySelector("[data-msg]");
    const top = issues.find((i) => i.level === "bad") || issues[0];
    msg.className = `field-msg ${top ? top.level : ""}`;
    msg.textContent = top ? top.text : "";
    const hit = $(`#paper [data-hit="${CSS.escape(id)}"] .hit`);
    if (hit) hit.classList.toggle("invalid", issues.some((i) => i.level === "bad"));
    return;
  }
  const v = imageValue(el);
  const uploading = state.uploading.has(id);
  const thumb = field.querySelector(".photo-thumb");
  thumb.classList.toggle("has-photo", !!v);
  thumb.style.backgroundImage = v ? `url("${state.uploads[v.upload].previewUrl}")` : "";
  thumb.textContent = v ? "" : uploading ? "…" : "+";
  field.querySelector("[data-upload]").textContent = uploading ? "Uploading…" : v ? "Replace photo" : "Upload photo";
  field.querySelector("[data-upload]").disabled = uploading;
  field.querySelector("[data-remove]").hidden = !v;
  field.querySelector("[data-zoom]").hidden = !v;
  if (v) field.querySelector("[data-zoom] input").value = String(v.zoom);
  const q = imageQuality(el);
  const quality = field.querySelector("[data-quality]");
  quality.replaceChildren();
  if (q) {
    quality.append(
      h("span", { class: `pill pill-${q.level === "bad" ? "bad" : q.level === "warn" ? "warn" : "ok"}`, text: q.label }),
      h("span", {
        text:
          q.level === "bad"
            ? `Print quality is low (${Math.round(q.dpi)} dpi). Try a larger photo or zoom out.`
            : `Print quality (${Math.round(q.dpi)} dpi)`,
      }),
    );
  }
}

function renderTabs() {
  const n = state.template.pages.length;
  const tabs = $("#page-tabs");
  tabs.replaceChildren();
  if (n < 2) return;
  tabs.setAttribute("role", "tablist");
  state.template.pages.forEach((_, i) => {
    const btn = h("button", {
      class: "page-tab",
      type: "button",
      role: "tab",
      "aria-selected": String(i === state.page),
      text: pageName(i, n),
      onclick: () => {
        state.page = i;
        state.selected = null;
        renderAll();
      },
    });
    if (pageHasMissing(i)) btn.append(h("span", { class: "dot", title: "Needs attention" }));
    tabs.append(btn);
  });
}

function renderAll() {
  renderTabs();
  renderStage();
  renderPanel();
}

// ---------------------------------------------------------------------------
// Saving
// ---------------------------------------------------------------------------
let saveTimer;
function setStatus(text, error = false) {
  const el = $("#save-status");
  el.textContent = text;
  el.classList.toggle("error", error);
}

function scheduleSave() {
  state.dirty = true;
  setStatus("Saving…");
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => saveNow().catch(() => {}), 700);
  clearTimeout(renderTabs.timer);
  renderTabs.timer = setTimeout(renderTabs, 300);
}

async function saveNow() {
  clearTimeout(saveTimer);
  while (state.savePromise) await state.savePromise;
  if (!state.dirty && state.designId) return;
  state.dirty = false;
  state.savePromise = (async () => {
    try {
      if (state.designId) {
        try {
          await api("PUT", `/api/designs/${state.designId}`, { values: state.values });
        } catch (err) {
          if (err.status !== 404 && err.status !== 422) throw err;
          state.designId = null; // approved elsewhere or deleted: start a new design
        }
      }
      if (!state.designId) {
        const d = await api("POST", "/api/designs", { templateId, values: state.values });
        state.designId = d.id;
        storage("set", d.id);
      }
      setStatus("All changes saved");
    } catch (err) {
      state.dirty = true;
      setStatus("Couldn't save. Check your connection.", true);
      throw err;
    } finally {
      state.savePromise = null;
    }
  })();
  return state.savePromise;
}

async function restoreDesign() {
  const id = params.get("design") || storage("get");
  if (!id) return;
  try {
    const d = await api("GET", `/api/designs/${encodeURIComponent(id)}`);
    if (d.status !== "draft" || d.templateId !== templateId) {
      storage("remove");
      return;
    }
    const ids = Object.values(d.values)
      .map((v) => v.upload)
      .filter(Boolean);
    const ups = await Promise.all(ids.map((u) => api("GET", `/api/uploads/${encodeURIComponent(u)}`).catch(() => null)));
    for (const up of ups) if (up) state.uploads[up.id] = up;
    state.values = d.values;
    state.designId = d.id;
    setStatus("Restored your saved design");
  } catch {
    storage("remove");
  }
}

// ---------------------------------------------------------------------------
// Proof & approval
// ---------------------------------------------------------------------------
function openModal() {
  $("#proof-modal").hidden = false;
}

function closeModal() {
  $("#proof-modal").hidden = true;
}

function proofFigure(url, page, caption) {
  const t = page.trimBox;
  const img = h("img", {
    src: url,
    alt: caption,
    style: `width:${(page.width / t.w) * 100}%;left:${(-t.x / t.w) * 100}%;top:${(-t.y / t.h) * 100}%`,
  });
  return h("figure", { class: "proof-page" }, [
    h("div", { class: "proof-trim", style: `aspect-ratio:${t.w} / ${t.h}` }, [img]),
    h("figcaption", { text: caption }),
  ]);
}

async function review() {
  const body = $("#proof-body");
  const foot = $("#proof-foot");
  body.replaceChildren(h("div", { class: "modal-loading" }, [h("div", { class: "spinner" }), h("p", { text: "Preparing your print proof…" })]));
  foot.replaceChildren();
  $("#proof-title").textContent = "Your print proof";
  openModal();
  let result;
  try {
    await saveNow();
    result = await api("POST", `/api/designs/${state.designId}/proof`);
  } catch (err) {
    body.replaceChildren(h("div", { class: "notice notice-bad", text: err.message }));
    foot.replaceChildren(h("button", { class: "btn", type: "button", text: "Back to editing", onclick: closeModal }));
    return;
  }
  if (result.errors.length) {
    body.replaceChildren(
      h("div", { class: "notice notice-bad" }, [
        h("strong", { text: "A few things need your attention:" }),
        h("ul", {}, result.errors.map((e) => h("li", { text: e }))),
      ]),
    );
    foot.replaceChildren(h("button", { class: "btn btn-primary", type: "button", text: "Back to editing", onclick: closeModal }));
    return;
  }
  const n = state.template.pages.length;
  const children = [
    h("p", {
      class: "proof-intro",
      text: "This is exactly what we will print, generated from the final print file. Please check every name, date and detail.",
    }),
  ];
  if (result.warnings.length) {
    children.push(
      h("div", { class: "notice notice-warn" }, [
        h("strong", { text: "Please double-check:" }),
        h("ul", {}, result.warnings.map((w) => h("li", { text: w }))),
      ]),
    );
  }
  children.push(
    h(
      "div",
      { class: "proof-pages" },
      result.pages.map((url, i) => proofFigure(url, state.template.pages[i], pageName(i, n))),
    ),
  );
  body.replaceChildren(...children);
  const check = h("input", { type: "checkbox", id: "confirm-check" });
  const approveBtn = h("button", {
    class: "btn btn-accent",
    type: "button",
    disabled: true,
    text: embedded ? "Approve & add to cart" : "Approve design",
  });
  check.addEventListener("change", () => (approveBtn.disabled = !check.checked));
  approveBtn.addEventListener("click", () => approve(approveBtn));
  foot.replaceChildren(
    h("label", { class: "confirm", for: "confirm-check" }, [
      check,
      h("span", { text: "I have checked the spelling, names and dates. My design is ready to print." }),
    ]),
    h("button", { class: "btn", type: "button", text: "Keep editing", onclick: closeModal }),
    approveBtn,
  );
}

async function approve(btn) {
  btn.disabled = true;
  btn.textContent = "Approving…";
  let d;
  try {
    d = await api("POST", `/api/designs/${state.designId}/approve`);
  } catch (err) {
    btn.disabled = false;
    btn.textContent = "Try again";
    toast(err.errors?.join(" ") || err.message);
    return;
  }
  storage("remove");
  const thumb = new URL(d.thumbnailUrl, location.origin).href;
  $("#proof-title").textContent = "Design approved";
  $("#proof-body").replaceChildren(
    h("div", { class: "success" }, [
      h("h3", { text: "Thank you!" }),
      h("p", {
        text: embedded
          ? "Your design is approved. Adding it to your cart…"
          : "Your design is approved and ready for production.",
      }),
      h("img", { src: thumb, alt: "Approved design" }),
      h("p", { class: "field-help", text: `Design reference: ${d.id}` }),
    ]),
  );
  $("#proof-foot").replaceChildren(
    h("a", { class: "btn", href: d.proofPdfUrl, target: "_blank", rel: "noopener", text: "Download proof (PDF)" }),
    embedded
      ? ""
      : h("button", {
          class: "btn btn-primary",
          type: "button",
          text: "Start a new design",
          onclick: () => {
            location.href = location.pathname;
          },
        }),
  );
  if (embedded && parentOrigin) {
    window.parent.postMessage(
      { type: "jasmin:approved", designId: d.id, summary: d.summary, thumbnailUrl: thumb, templateId },
      parentOrigin,
    );
  }
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
async function loadFonts(fonts) {
  await Promise.all(
    Object.values(fonts).map(async (info) => {
      const face = new FontFace(fontFamily(info.name), `url(${info.url})`);
      try {
        await face.load();
        document.fonts.add(face);
      } catch {
        console.warn("font failed to load", info.name);
      }
    }),
  );
}

function showFatal(message) {
  $("#loading").replaceChildren(h("h2", { text: "Sorry, this design can't be opened" }), h("p", { text: message }));
}

async function boot() {
  let t;
  try {
    t = await api("GET", `/api/templates/${encodeURIComponent(templateId)}`);
  } catch (err) {
    showFatal(err.status === 404 ? "This product template doesn't exist (any more)." : err.message);
    return;
  }
  state.template = t;
  for (const [name, info] of Object.entries(t.fonts)) state.fonts[name] = new FontMetrics(info);
  await Promise.all([loadFonts(t.fonts), restoreDesign()]);

  document.title = `Personalize · ${t.name}`;
  $("#product-name").textContent = t.name;
  $("#draft-banner").hidden = t.status !== "draft";
  $("#review-btn").disabled = false;
  $("#review-btn").addEventListener("click", review);
  $("#proof-modal").addEventListener("click", (e) => {
    if (e.target.matches("[data-close]") || e.target === e.currentTarget) closeModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#proof-modal").hidden) closeModal();
  });
  $("#file-input").addEventListener("change", (e) => {
    const file = e.target.files?.[0];
    if (file && pickTarget) uploadFor(pickTarget, file);
  });
  // Dropping a photo anywhere on the stage fills the selected/first empty slot.
  const stage = $("#stage");
  stage.addEventListener("dragover", (e) => e.preventDefault());
  stage.addEventListener("drop", (e) => {
    e.preventDefault();
    const file = e.dataTransfer.files?.[0];
    const images = pageElements().filter((el) => el.type === "image");
    const target =
      images.find((el) => el.id === state.selected) || images.find((el) => !imageValue(el)) || images[0];
    if (file && target) uploadFor(target, file);
  });
  if (embedded) {
    const close = $("#close-btn");
    close.hidden = false;
    close.addEventListener("click", () => window.parent.postMessage({ type: "jasmin:close" }, parentOrigin || "*"));
  }
  new ResizeObserver(fitPaper).observe($("#paper-shadow"));
  $("#app").classList.remove("is-loading");
  renderAll();
  window.addEventListener("beforeunload", (e) => {
    if (state.dirty) {
      saveNow().catch(() => {});
      e.preventDefault();
    }
  });
}

boot();
