// Jasmin studio (admin): templates, fonts, orders, Shopify setup, backups.
const view = document.querySelector("#view");
const render = (...nodes) => view.replaceChildren(...nodes.filter(Boolean));
const SVG_NS = "http://www.w3.org/2000/svg";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function h(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k in node && typeof v !== "string") node[k] = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  node.append(...children.filter((c) => c !== null && c !== undefined && c !== false));
  return node;
}

function s(tag, attrs = {}, children = []) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) node.setAttribute(k, v);
  node.append(...children);
  return node;
}

let toastTimer;
function toast(message, error = false) {
  const el = document.querySelector("#toast");
  el.textContent = message;
  el.className = `toast${error ? " error" : ""}`;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 4000);
}

async function api(method, url, body) {
  const opts = { method, headers: {}, credentials: "same-origin" };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) location.href = "/admin/login";
  if (!res.ok) {
    const err = new Error((data.errors || [data.detail || `Request failed (${res.status})`]).join(" "));
    err.status = res.status;
    throw err;
  }
  return data;
}

const fmtDate = (iso) => (iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "");
const statusPill = (status) =>
  h("span", {
    class: `pill ${{ published: "pill-ok", draft: "pill-muted", received: "pill-warn", in_production: "pill", shipped: "pill-ok", ready: "pill-ok", pending: "pill-muted", error: "pill-bad" }[status] || "pill-muted"}`,
    text: status.replace("_", " "),
  });
const pageName = (i, n) => (n === 2 ? ["Front", "Back"][i] : `Page ${i + 1}`);

function copyButton(text) {
  return h("button", {
    class: "btn btn-small",
    type: "button",
    text: "Copy",
    onclick: async (e) => {
      try {
        await navigator.clipboard.writeText(text);
        e.target.textContent = "Copied";
      } catch {
        toast("Copy failed; select the text instead.", true);
      }
    },
  });
}

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------
let leaveGuard = null;
const routes = [
  [/^#\/templates\/([^/]+)$/, (m) => templateView(decodeURIComponent(m[1]))],
  [/^#\/orders\/([^/]+)$/, (m) => orderView(decodeURIComponent(m[1]))],
  [/^#\/orders$/, () => ordersView()],
  [/^#\/fonts$/, () => fontsView()],
  [/^#\/shopify$/, () => shopifyView()],
  [/^#\/backups$/, () => backupsView()],
  [/.*/, () => templatesView()],
];

async function route() {
  leaveGuard = null;
  const hash = location.hash || "#/templates";
  const section = hash.split("/")[1] || "templates";
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === section));
  for (const [re, fn] of routes) {
    const m = hash.match(re);
    if (m) {
      render(h("div", { class: "empty-state" }, [h("div", { class: "spinner", style: "margin:auto" })]));
      try {
        await fn(m);
      } catch (err) {
        render(h("div", { class: "notice notice-bad", text: err.message }));
      }
      view.focus();
      return;
    }
  }
}

window.addEventListener("hashchange", route);
window.addEventListener("beforeunload", (e) => {
  if (leaveGuard && leaveGuard()) e.preventDefault();
});

// ---------------------------------------------------------------------------
// Templates list
// ---------------------------------------------------------------------------
async function templatesView() {
  const templates = await api("GET", "/admin/api/templates");
  const nameInput = h("input", { type: "text", name: "name", placeholder: "e.g. Arch Wedding Invitation", required: true });
  const fileInput = h("input", { type: "file", name: "file", accept: "application/pdf", required: true });
  const submit = h("button", { class: "btn btn-primary", type: "submit", text: "Analyze PDF" });
  const form = h("form", { class: "upload-form" }, [
    h("label", {}, ["Template name", nameInput]),
    h("label", {}, ["Layered PDF", fileInput]),
    submit,
  ]);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    submit.disabled = true;
    submit.textContent = "Analyzing…";
    try {
      const fd = new FormData(form);
      const t = await api("POST", "/admin/api/templates", fd);
      toast(`Found ${t.definition.elements.length} editable elements`);
      location.hash = `#/templates/${encodeURIComponent(t.id)}`;
    } catch (err) {
      toast(err.message, true);
      submit.disabled = false;
      submit.textContent = "Analyze PDF";
    }
  });

  const grid = templates.length
    ? h(
        "div",
        { class: "tpl-grid" },
        templates.map((t) =>
          h("a", { class: "tpl-card", href: `#/templates/${encodeURIComponent(t.id)}` }, [
            h("div", { class: "tpl-thumb" }, [h("img", { src: `${t.assets}/p0-original.jpg`, alt: "" })]),
            h("div", { class: "tpl-meta" }, [
              h("h3", { text: t.name }),
              h("div", {}, [statusPill(t.status)]),
              h("span", {
                class: "small muted",
                text: `${t.definition.elements.filter((e) => e.enabled !== false).length} editable · ${t.definition.pages.length} page${t.definition.pages.length > 1 ? "s" : ""}`,
              }),
              h("span", {
                class: "small muted",
                text: `Shopify: ${t.productHandles.length ? t.productHandles.join(", ") : `${t.id} (default)`}`,
              }),
            ]),
          ]),
        ),
      )
    : h("div", { class: "card empty-state" }, [h("p", { text: "No templates yet. Upload your first layered PDF above." })]);

  render(
    h("div", { class: "view-head" }, [h("h1", { text: "Templates" })]),
    h("div", { class: "card" }, [h("h2", { text: "New template" }), form]),
    h("div", { class: "card guide" }, [
      h("h2", { text: "Preparing a PDF" }),
      h("ol", {}, [
        h("li", { html: "Design as usual; put each editable item on its <b>own layer</b>." }),
        h("li", {
          html: "Name editable layers with a <code>-customize</code> suffix: <code>couple-photo-customize</code>, <code>names-customize</code>…",
        }),
        h("li", { html: "Photo layers: place a sample photo inside a clipping shape (arch, circle, rectangle)." }),
        h("li", { html: "Text layers: one font per layer; use the font you want customers to get." }),
        h("li", {
          html: "Export as PDF <b>with layers</b> (Illustrator: <i>Create Acrobat Layers from Top-Level Layers</i>; InDesign: <i>Create Acrobat Layers</i>), with bleed.",
        }),
      ]),
    ]),
    h("div", { class: "view-head", style: "margin-top:24px" }, [h("h2", { text: "Your templates" })]),
    grid,
  );
}

// ---------------------------------------------------------------------------
// Template detail / editor
// ---------------------------------------------------------------------------
async function templateView(id) {
  const [t, fonts] = await Promise.all([
    api("GET", `/admin/api/templates/${encodeURIComponent(id)}`),
    api("GET", "/admin/api/fonts"),
  ]);
  const st = {
    t,
    elements: structuredClone(t.definition.elements),
    name: t.name,
    handles: t.productHandles.join(", "),
    page: 0,
    selected: null,
    dirty: false,
  };
  leaveGuard = () => st.dirty;
  const def = t.definition;
  const pages = def.pages;

  const saveBtn = h("button", { class: "btn btn-primary", type: "button", text: "Save changes", disabled: true });
  const saveNote = h("span", { class: "small muted", text: "All changes saved" });
  function markDirty() {
    st.dirty = true;
    saveBtn.disabled = false;
    saveNote.textContent = "Unsaved changes";
  }

  async function save(extra = {}) {
    saveBtn.disabled = true;
    saveNote.textContent = "Saving…";
    try {
      const updated = await api("PATCH", `/admin/api/templates/${encodeURIComponent(id)}`, {
        name: st.name,
        productHandles: st.handles.split(",").map((x) => x.trim()).filter(Boolean),
        elements: st.elements,
        ...extra,
      });
      st.dirty = false;
      toast("Saved");
      return updated;
    } catch (err) {
      saveBtn.disabled = false;
      saveNote.textContent = "Not saved";
      toast(err.message, true);
      throw err;
    }
  }
  saveBtn.addEventListener("click", async () => {
    try {
      await save();
      templateView(id);
    } catch {
      /* toast already shown */
    }
  });

  const publishBtn = h("button", {
    class: `btn ${t.status === "published" ? "" : "btn-accent"}`,
    type: "button",
    text: t.status === "published" ? "Unpublish" : "Publish",
    onclick: async () => {
      try {
        await save({ status: t.status === "published" ? "draft" : "published" });
        templateView(id);
      } catch {
        /* toast already shown */
      }
    },
  });

  // Preview with element boxes --------------------------------------------
  const previewHost = h("div", { class: "preview-wrap" });
  const pageSwitch = h("div", { class: "page-switch" });

  function renderPreview() {
    const page = pages[st.page];
    pageSwitch.replaceChildren(
      ...(pages.length > 1
        ? pages.map((_, i) =>
            h("button", {
              class: "btn btn-small",
              type: "button",
              "aria-pressed": String(i === st.page),
              text: pageName(i, pages.length),
              onclick: () => {
                st.page = i;
                renderPreview();
              },
            }),
          )
        : []),
    );
    const svg = s("svg", { viewBox: `0 0 ${page.width} ${page.height}` });
    const tb = page.trimBox;
    svg.append(s("rect", { class: "trim", x: tb.x, y: tb.y, width: tb.w, height: tb.h }));
    for (const el of st.elements.filter((e) => e.page === st.page)) {
      const cls = `box box-${el.type}${el.enabled === false ? " disabled" : ""}${st.selected === el.id ? " selected" : ""}`;
      const b = el.box;
      const shape =
        el.type === "image" && el.clipPath
          ? s("path", { d: pathD(el.clipPath), class: cls })
          : s("rect", { x: b.x, y: b.y, width: b.w, height: b.h, class: cls });
      shape.addEventListener("pointerdown", (e) => startDrag(e, el, "move"));
      svg.append(shape);
      svg.append(
        Object.assign(s("text", { x: b.x, y: Math.max(8, b.y - 3), class: `box-label ${el.type}` }), {
          textContent: el.label,
        }),
      );
      if (st.selected === el.id && el.type === "text") {
        const handle = s("rect", { class: "handle", x: b.x + b.w - 3, y: b.y + b.h - 3, width: 6, height: 6 });
        handle.addEventListener("pointerdown", (e) => startDrag(e, el, "resize"));
        svg.append(handle);
      }
    }
    const preview = h("div", { class: "preview" }, [
      h("img", { src: `${t.assets}/p${st.page}-original.jpg`, alt: "Original design" }),
      svg,
    ]);
    previewHost.replaceChildren(preview);
  }

  function startDrag(e, el, mode) {
    e.preventDefault();
    e.stopPropagation();
    // Measure before select() re-renders the preview.
    const svg = e.target.ownerSVGElement;
    const k = svg.viewBox.baseVal.width / svg.getBoundingClientRect().width;
    select(el.id);
    if (el.type !== "text" || !Number.isFinite(k)) return;
    const start = { x: e.clientX, y: e.clientY, box: { ...el.box } };
    const move = (ev) => {
      const dx = (ev.clientX - start.x) * k;
      const dy = (ev.clientY - start.y) * k;
      if (mode === "move") el.box = { ...start.box, x: round(start.box.x + dx), y: round(start.box.y + dy) };
      else el.box = { ...start.box, w: round(Math.max(4, start.box.w + dx)), h: round(Math.max(4, start.box.h + dy)) };
      markDirty();
      renderPreview();
      syncBoxInputs(el);
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }

  // Element cards --------------------------------------------------------------
  const list = h("div", { class: "el-list" });
  const boxInputs = {};

  function select(elId) {
    st.selected = elId;
    const el = st.elements.find((e) => e.id === elId);
    if (el && el.page !== st.page) st.page = el.page;
    renderPreview();
    list.querySelectorAll(".el-card").forEach((c) => {
      const on = c.dataset.id === elId;
      c.classList.toggle("selected", on);
      if (on && !c.open) c.open = true;
    });
  }

  function syncBoxInputs(el) {
    const inputs = boxInputs[el.id];
    if (!inputs) return;
    for (const key of ["x", "y", "w", "h"]) inputs[key].value = el.box[key];
  }

  function field(label, input, cls = "") {
    return h("label", { class: cls }, [label, input]);
  }

  function bind(el, key, input, parse = (v) => v) {
    input.addEventListener("input", () => {
      el[key] = input.type === "checkbox" ? input.checked : parse(input.value);
      markDirty();
      if (key === "enabled" || key === "label") renderPreview();
    });
    return input;
  }

  function textInput(el, key, attrs = {}) {
    const input = h("input", { type: "text", value: el[key] ?? "", ...attrs });
    return bind(el, key, input);
  }

  function numInput(el, key, step = "0.1") {
    const input = h("input", { type: "number", step, value: el[key] ?? "" });
    return bind(el, key, input, Number);
  }

  function checkInput(el, key, fallback = false) {
    const input = h("input", { type: "checkbox" });
    input.checked = el[key] ?? fallback;
    return bind(el, key, input);
  }

  function selectInput(el, key, options) {
    const input = h(
      "select",
      {},
      options.map(([value, label]) => h("option", { value, text: label, selected: el[key] === value })),
    );
    return bind(el, key, input);
  }

  function elementCard(el) {
    const grid = h("div", { class: "form-grid" }, [
      field("Label shown to customers", textInput(el, "label"), "full"),
      field("Help text", textInput(el, "help", { placeholder: "Optional hint under the field" }), "full"),
      h("label", { class: "check" }, [checkInput(el, "enabled", true), "Customers can edit this"]),
      h("label", { class: "check" }, [checkInput(el, "required", true), "Required"]),
    ]);
    if (el.type === "text") {
      const color = h("input", { type: "color", value: el.color?.hex || "#000000" });
      color.addEventListener("input", () => {
        el.color = { ...(el.color || {}), hex: color.value };
        markDirty();
      });
      const defaultText = h("textarea", { rows: 2 });
      defaultText.value = el.default || "";
      bind(el, "default", defaultText);
      boxInputs[el.id] = {};
      const boxField = (k) => {
        const input = h("input", { type: "number", step: "0.5", value: el.box[k] });
        input.addEventListener("input", () => {
          el.box = { ...el.box, [k]: Number(input.value) };
          markDirty();
          renderPreview();
        });
        boxInputs[el.id][k] = input;
        return field(`Box ${k} (pt)`, input);
      };
      grid.append(
        field("Default text", defaultText, "full"),
        field(
          "Font",
          selectInput(
            el,
            "font",
            fonts.map((f) => [f.name, `${f.family} ${f.style}`]),
          ),
          "full",
        ),
        field("Font size (pt)", numInput(el, "fontSize")),
        field("Smallest size when shrinking", numInput(el, "minFontSize")),
        field("Colour", color),
        field("Alignment", selectInput(el, "align", [["left", "Left"], ["center", "Center"], ["right", "Right"]])),
        field("Line height (× size)", numInput(el, "lineHeight", "0.01")),
        field("Letter spacing (em)", numInput(el, "letterSpacing", "0.005")),
        field("Max characters", numInput(el, "maxLength", "1")),
        field("Max lines", numInput(el, "maxLines", "1")),
        field("Vertical alignment", selectInput(el, "valign", [["top", "Top"], ["middle", "Middle"], ["bottom", "Bottom"]])),
        field("Placeholder", textInput(el, "placeholder")),
        h("label", { class: "check" }, [checkInput(el, "multiline"), "Multiple lines"]),
        h("label", { class: "check" }, [checkInput(el, "autoFit", true), "Shrink to fit"]),
        h("label", { class: "check full" }, [
          (() => {
            const input = h("input", { type: "checkbox" });
            input.checked = el.transform === "uppercase";
            input.addEventListener("change", () => {
              el.transform = input.checked ? "uppercase" : "none";
              markDirty();
            });
            return input;
          })(),
          "UPPERCASE",
        ]),
        boxField("x"),
        boxField("y"),
        boxField("w"),
        boxField("h"),
        h("p", {
          class: "small muted full",
          text: `Detected font in PDF: ${el.sourceFont || "?"}. Drag the box on the preview to move it; use the corner to resize.`,
        }),
      );
    } else {
      grid.append(
        field("Minimum print resolution (dpi)", numInput(el, "minDpi", "1")),
        h("label", { class: "check" }, [checkInput(el, "keepOriginalIfEmpty"), "Keep my image if left empty"]),
        h("p", {
          class: "small muted full",
          text: `Slot ${Math.round(el.box.w)}×${Math.round(el.box.h)} pt (${((el.box.w / 72) * 2.54).toFixed(1)}×${((el.box.h / 72) * 2.54).toFixed(1)} cm)${el.clipPath ? ", custom shape from the PDF clip path" : ""}. For 300 dpi, customers need at least ${Math.round((el.box.w / 72) * 300)}×${Math.round((el.box.h / 72) * 300)} px.`,
        }),
      );
    }
    const card = h("details", { class: "el-card", "data-id": el.id }, [
      // The browser opens or closes the card first. Select the element only when the card was opened:
      // select() opens the selected card, so calling it after a close would open it again.
      h("summary", { onclick: () => setTimeout(() => card.open && select(el.id)) }, [
        h("span", { class: `pill ${el.type === "image" ? "pill-muted" : ""}`, text: el.type === "image" ? "Photo" : "Text" }),
        h("strong", { text: el.label }),
        h("span", { class: "small muted", text: pageName(el.page, pages.length) }),
      ]),
      h("div", { class: "el-body" }, [grid]),
    ]);
    return card;
  }

  list.replaceChildren(...st.elements.map(elementCard));

  const nameInput = h("input", { type: "text", value: st.name });
  nameInput.addEventListener("input", () => {
    st.name = nameInput.value;
    markDirty();
  });
  const handlesInput = h("input", { type: "text", value: st.handles, placeholder: t.id });
  handlesInput.addEventListener("input", () => {
    st.handles = handlesInput.value;
    markDirty();
  });

  const layersTable = h("table", { class: "layers" }, [
    h("thead", {}, [h("tr", {}, [h("th", { text: "Page" }), h("th", { text: "Layer" }), h("th", { text: "Becomes" })])]),
    h(
      "tbody",
      {},
      def.layers.map((l) =>
        h("tr", {}, [
          h("td", { text: pageName(l.page, pages.length) }),
          h("td", {}, [h("code", { text: l.name })]),
          h("td", { text: l.element ? `Editable: ${l.element}` : l.customizable ? "Customizable (no content)" : "Fixed artwork" }),
        ]),
      ),
    ),
  ]);

  renderPreview();
  render(
    h("div", { class: "view-head" }, [
      h("div", { style: "margin-right:auto" }, [
        h("a", { class: "crumb", href: "#/templates", text: "← Templates" }),
        h("h1", { text: t.name }),
      ]),
      statusPill(t.status),
      h("a", { class: "btn", href: t.editorUrl, target: "_blank", rel: "noopener", text: "Open customer editor ↗" }),
      publishBtn,
    ]),
    def.warnings.length
      ? h("div", { class: "notice notice-warn" }, [
          h("strong", { text: "Check these:" }),
          h("ul", {}, def.warnings.map((w) => h("li", { text: w }))),
        ])
      : null,
    h("div", { class: "tpl-layout" }, [
      h("div", { class: "card preview-card" }, [
        h("h2", { text: "Detected elements" }),
        pageSwitch,
        previewHost,
        h("p", { class: "small muted", text: "Blue: photo slots (shape from your PDF). Brown: text boxes. Dashed line: trim." }),
      ]),
      h("div", {}, [
        h("div", { class: "card" }, [
          h("h2", { text: "Product" }),
          h("div", { class: "form-grid" }, [
            field("Template name", nameInput, "full"),
            field("Shopify product handle(s), comma separated", handlesInput, "full"),
            h("p", {
              class: "small muted full",
              html: `The handle is the last part of the product URL (<code>/products/<b>handle</b></code>). Leave empty to use <code>${t.id}</code>.`,
            }),
          ]),
        ]),
        h("div", { class: "card" }, [h("h2", { text: "Editable elements" }), list]),
        h("div", { class: "card" }, [h("h2", { text: "Layers in the PDF" }), layersTable]),
        h("div", { class: "card" }, [
          h("h2", { text: "Danger zone" }),
          h("button", {
            class: "btn",
            type: "button",
            text: "Delete template",
            onclick: async () => {
              if (!confirm(`Delete "${t.name}"? Existing orders keep their print files.`)) return;
              await api("DELETE", `/admin/api/templates/${encodeURIComponent(id)}`);
              st.dirty = false;
              location.hash = "#/templates";
            },
          }),
          h("a", { class: "btn", style: "margin-left:8px", href: `/admin/api/templates/${encodeURIComponent(id)}/source.pdf`, text: "Download source PDF" }),
        ]),
        h("div", { class: "savebar" }, [saveNote, saveBtn]),
      ]),
    ]),
  );
}

function pathD(segs) {
  return segs
    .map(([op, ...v]) => (op === "m" ? `M${v[0]} ${v[1]}` : op === "l" ? `L${v[0]} ${v[1]}` : op === "c" ? `C${v.join(" ")}` : "Z"))
    .join(" ");
}

const round = (v) => Math.round(v * 10) / 10;

// ---------------------------------------------------------------------------
// Fonts
// ---------------------------------------------------------------------------
async function fontsView() {
  const fonts = await api("GET", "/admin/api/fonts");
  const style = h("style", {
    text: fonts.map((f) => `@font-face{font-family:"pv-${f.name}";src:url("${f.url}")}`).join("\n"),
  });
  const fileInput = h("input", { type: "file", accept: ".ttf,font/ttf", required: true });
  const submit = h("button", { class: "btn btn-primary", type: "submit", text: "Upload font" });
  const form = h("form", { class: "upload-form", style: "grid-template-columns:1fr auto" }, [
    h("label", {}, ["TrueType font (.ttf)", fileInput]),
    submit,
  ]);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData();
    fd.append("file", fileInput.files[0]);
    submit.disabled = true;
    try {
      const f = await api("POST", "/admin/api/fonts", fd);
      toast(`Installed ${f.family} ${f.style}`);
      fontsView();
    } catch (err) {
      toast(err.message, true);
      submit.disabled = false;
    }
  });
  render(
    style,
    h("div", { class: "view-head" }, [h("h1", { text: "Fonts" })]),
    h("div", { class: "card" }, [
      h("h2", { text: "Add a font" }),
      h("p", {
        class: "muted small",
        html: "Customer text is typeset with the <b>full</b> font file (PDFs only embed the letters of your sample text). The font's PostScript name should match the one in your PDF so it's picked automatically. Make sure your licence allows <b>web embedding</b> and <b>PDF embedding</b>.",
      }),
      form,
    ]),
    h(
      "div",
      { class: "card" },
      fonts.map((f) =>
        h("div", { class: "font-row" }, [
          h("div", {}, [h("strong", { text: `${f.family} ${f.style}` }), h("div", { class: "small muted" }, [h("code", { text: f.name })])]),
          h("div", { class: "font-sample", style: `font-family:"pv-${f.name}"`, text: "Emma & James · Saturday, June 13" }),
          h("span", { class: `pill ${f.source === "bundled" ? "pill-muted" : "pill-ok"}`, text: f.source }),
        ]),
      ),
    ),
  );
}

// ---------------------------------------------------------------------------
// Orders
// ---------------------------------------------------------------------------
async function ordersView() {
  const orders = await api("GET", "/admin/api/orders");
  const rows = orders.map((o) => {
    const ready = o.items.filter((i) => i.status === "ready").length;
    const tr = h("tr", { class: "link", onclick: () => (location.hash = `#/orders/${encodeURIComponent(o.id)}`) }, [
      h("td", {}, [h("strong", { text: o.name }), o.source === "demo" ? h("span", { class: "pill pill-muted", style: "margin-left:6px", text: "demo" }) : null]),
      h("td", { text: fmtDate(o.created_at) }),
      h("td", { text: o.customer || o.email }),
      h("td", { text: `${o.items.length} personalized` }),
      h("td", {}, [statusPill(ready === o.items.length ? "ready" : o.items.some((i) => i.status === "error") ? "error" : "pending")]),
      h("td", {}, [statusPill(o.status)]),
    ]);
    return tr;
  });
  render(
    h("div", { class: "view-head" }, [h("h1", { text: "Orders" })]),
    orders.length
      ? h("table", { class: "table" }, [
          h("thead", {}, [
            h("tr", {}, ["Order", "Date", "Customer", "Items", "Print files", "Status"].map((x) => h("th", { text: x }))),
          ]),
          h("tbody", {}, rows),
        ])
      : h("div", { class: "card empty-state" }, [
          h("p", { text: "No personalized orders yet." }),
          h("p", { class: "small", html: 'Place a test order in the <a href="/demo-store" target="_blank">demo store</a> or connect Shopify.' }),
        ]),
  );
}

async function orderView(id) {
  const o = await api("GET", `/admin/api/orders/${encodeURIComponent(id)}`);
  const statusSelect = h(
    "select",
    { style: "width:auto" },
    [["received", "Received"], ["in_production", "In production"], ["shipped", "Shipped"]].map(([v, l]) =>
      h("option", { value: v, text: l, selected: o.status === v }),
    ),
  );
  statusSelect.addEventListener("change", async () => {
    try {
      await api("POST", `/admin/api/orders/${encodeURIComponent(id)}/status`, { status: statusSelect.value });
      toast("Status updated");
    } catch (err) {
      toast(err.message, true);
    }
  });
  const a = o.shipping_address || {};
  render(
    h("div", { class: "view-head" }, [
      h("div", { style: "margin-right:auto" }, [
        h("a", { class: "crumb", href: "#/orders", text: "← Orders" }),
        h("h1", { text: `Order ${o.name}` }),
      ]),
      statusSelect,
    ]),
    h("div", { class: "card" }, [
      h("div", { class: "kv" }, [
        h("span", { class: "muted", text: "Customer" }),
        h("span", { text: `${o.customer || "-"} · ${o.email || ""}` }),
        h("span", { class: "muted", text: "Ship to" }),
        h("span", { text: [a.name, a.address1, a.city, a.zip, a.country].filter(Boolean).join(", ") || "-" }),
        h("span", { class: "muted", text: "Placed" }),
        h("span", { text: fmtDate(o.created_at) }),
        h("span", { class: "muted", text: "Shopify order id" }),
        h("span", {}, [h("code", { text: o.id })]),
      ]),
    ]),
    h("div", { class: "card" }, [
      h("h2", { text: "Personalized items" }),
      ...o.items.map((i) =>
        h("div", { class: "item-row" }, [
          h("img", { src: i.thumbnailUrl, alt: "" }),
          h("div", {}, [
            h("strong", { text: i.title }),
            h("div", { class: "small muted", text: `${i.variant} · Quantity ${i.quantity}` }),
            h("div", { class: "small muted" }, ["Design ", h("code", { text: i.design_id })]),
            i.error ? h("div", { class: "small", style: "color:var(--bad)", text: i.error }) : null,
          ]),
          h("div", { class: "item-actions" }, [
            statusPill(i.status),
            i.print_file
              ? h("a", { class: "btn btn-primary btn-small", href: `/admin/api/order-items/${i.id}/print.pdf`, text: "Download print PDF" })
              : null,
            h("button", {
              class: "btn btn-small",
              type: "button",
              text: "Regenerate",
              onclick: async () => {
                await api("POST", `/admin/api/order-items/${i.id}/regenerate`);
                toast("Print file regenerated");
                orderView(id);
              },
            }),
          ]),
        ]),
      ),
    ]),
  );
}

// ---------------------------------------------------------------------------
// Shopify setup
// ---------------------------------------------------------------------------
async function shopifyView() {
  const c = await api("GET", "/admin/api/shopify");
  const flag = (cond, yes, no) => h("span", { class: `pill ${cond ? "pill-ok" : "pill-warn"}`, text: cond ? yes : no });
  render(
    h("div", { class: "view-head" }, [h("h1", { text: "Shopify setup" })]),
    c.appUrl.includes("localhost")
      ? h("div", {
          class: "notice notice-warn",
          html: `<b>APP_URL is ${c.appUrl}.</b> Shopify needs a public HTTPS address: deploy the app (or use a tunnel such as <code>cloudflared tunnel --url http://localhost:8000</code>) and set <code>APP_URL</code>.`,
        })
      : null,
    h("div", { class: "card" }, [
      h("div", { class: "steps" }, [
        h("div", { class: "step" }, [
          h("h3", { text: "Link templates to products" }),
          h("p", {
            class: "muted",
            html: 'Create the product in Shopify as usual (price, variants for paper type, quantity). Then enter its handle on the template page, or give the template the same id as the handle. <a href="#/templates">Templates →</a>',
          }),
        ]),
        h("div", { class: "step" }, [
          h("h3", { text: "Add the Personalize button to your product page" }),
          h("p", {
            class: "muted",
            html: "Online Store → Themes → Customize → Products → Default product → <b>Add block → Custom Liquid</b>, paste this, and save. It hides the normal Add to cart for linked products.",
          }),
          h("pre", { text: c.snippet }),
          copyButton(c.snippet),
        ]),
        h("div", { class: "step" }, [
          h("h3", { text: "Send new orders to Jasmin" }),
          h("p", {
            class: "muted",
            html: "Settings → Notifications → Webhooks → <b>Create webhook</b>: event <i>Order creation</i>, format <i>JSON</i>, URL below. Copy the signing secret shown on that page into <code>SHOPIFY_WEBHOOK_SECRET</code>.",
          }),
          h("pre", { text: c.webhookUrl }),
          h("p", {}, [copyButton(c.webhookUrl), " ", flag(c.webhookSecretConfigured, "Secret configured", "SHOPIFY_WEBHOOK_SECRET not set (demo secret in use)")]),
        ]),
        h("div", { class: "step" }, [
          h("h3", { text: "Optional: tag orders in Shopify" }),
          h("p", {
            class: "muted",
            html: "Create a custom app (Settings → Apps → Develop apps) with the <code>write_orders</code> scope and set <code>SHOPIFY_SHOP_DOMAIN</code> and <code>SHOPIFY_ADMIN_TOKEN</code>. Orders are then tagged <code>jasmin-print-ready</code>, <code>jasmin-in-production</code> and <code>jasmin-shipped</code>.",
          }),
          flag(c.adminApiConfigured, "Admin API connected", "Not connected"),
        ]),
      ]),
    ]),
    h("div", { class: "card" }, [
      h("h2", { text: "Current configuration" }),
      h("div", { class: "kv" }, [
        h("span", { class: "muted", text: "APP_URL" }),
        h("code", { text: c.appUrl }),
        h("span", { class: "muted", text: "Shop domain" }),
        h("span", { text: c.shopDomain || "(not set)" }),
        h("span", { class: "muted", text: "Editor may be embedded by" }),
        h("span", { text: c.frameAncestors.join(" ") }),
        h("span", { class: "muted", text: "Demo store" }),
        h("span", { html: c.demoStore ? '<a href="/demo-store" target="_blank">enabled</a>' : "disabled" }),
      ]),
    ]),
  );
}

// The signed-in address and a Sign out button, when the admin is behind Supabase sign-in.
api("GET", "/admin/api/me")
  .then(({ email }) => {
    if (!email) return;
    const account = document.querySelector("#account");
    account.querySelector(".account-email").textContent = email;
    account.hidden = false;
  })
  .catch(() => {});


// ---------------------------------------------------------------------------
// Backups
// ---------------------------------------------------------------------------
const fmtSize = (bytes) => (bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`);

async function backupsView() {
  const status = h("p", { class: "muted", role: "status" });
  const button = h("button", { class: "btn btn-primary", type: "button", text: "Back up now" });
  const listHost = h("div");
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

  function show(data) {
    button.disabled = data.running;
    button.textContent = data.running ? "Backing up…" : "Back up now";
    status.textContent = data.running
      ? "A backup is being made. This page updates when it is done."
      : data.error
        ? `The last backup failed: ${data.error}`
        : data.backups.length
          ? `Last backup: ${fmtDate(data.backups[0].created)}.`
          : "No backup has been made yet.";
    status.className = data.error && !data.running ? "notice notice-bad" : "muted";
    listHost.replaceChildren(
      data.backups.length
        ? h("table", { class: "table backups" }, [
            h("thead", {}, [h("tr", {}, ["Made", "How", "Contains", "Size", ""].map((x) => h("th", { text: x })))]),
            h(
              "tbody",
              {},
              data.backups.map((b) =>
                h("tr", {}, [
                  h("td", {}, [h("strong", { text: fmtDate(b.created) })]),
                  h("td", {}, [h("span", { class: `pill ${b.kind === "automatic" ? "pill-muted" : "pill"}`, text: b.kind })]),
                  h("td", {
                    text: b.readable
                      ? `${plural(b.tables.templates ?? 0, "template")}, ${plural(b.tables.designs ?? 0, "design")}, ${plural(b.tables.orders ?? 0, "order")}, ${plural(b.files, "file")}`
                      : "Cannot be read",
                  }),
                  h("td", { text: fmtSize(b.size) }),
                  h("td", {}, [h("a", { class: "btn btn-small", href: `/admin/api/backups/${encodeURIComponent(b.name)}`, download: b.name, text: "Download" })]),
                ]),
              ),
            ),
          ])
        : h("div", { class: "card empty-state" }, [h("p", { text: "No backups yet." })]),
    );
  }

  // While one is being made, look again every two seconds, for as long as this page is shown.
  async function refresh() {
    if (location.hash !== "#/backups") return;
    const data = await api("GET", "/admin/api/backups");
    show(data);
    if (data.running) setTimeout(() => refresh().catch(() => {}), 2000);
    return data;
  }

  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await api("POST", "/admin/api/backups");
    } catch (err) {
      if (err.status !== 409) toast(err.message, true);
    }
    refresh().catch((err) => toast(err.message, true));
  });

  const data = await api("GET", "/admin/api/backups");
  render(
    h("div", { class: "view-head" }, [h("h1", { text: "Backups" })]),
    h("div", { class: "card" }, [
      h("p", {
        text: "A backup is one zip file with everything in Jasmin studio: templates, customer photos, designs, proofs, orders and print files. Backups are kept on the server, apart from the database and the file storage.",
      }),
      h("p", {
        class: "muted",
        text: `${data.automatic ? `One is made automatically every day at ${String(data.hourUtc).padStart(2, "0")}:00 UTC. ` : "Automatic backups are off. "}The newest ${data.keep} are kept; older ones are removed. Download one now and then to keep a copy somewhere else.`,
      }),
      button,
      status,
    ]),
    h("div", { class: "view-head", style: "margin-top:24px" }, [h("h2", { text: "Backups on the server" })]),
    listHost,
  );
  show(data);
  if (data.running) setTimeout(() => refresh().catch(() => {}), 2000);
}

route();
