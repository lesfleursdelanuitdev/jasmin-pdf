/*
 * Jasmin personalisation for Shopify product pages.
 *
 * Include via the Liquid snippet (Admin > Shopify setup). It:
 *   1. asks the Jasmin app whether this product has a template,
 *   2. shows a "Personalize" button (and hides the plain Add to cart),
 *   3. opens the editor in a modal; when the customer approves the proof,
 *   4. adds the selected variant to the cart with line item properties:
 *        Personalization  - visible summary (cart, checkout, order)
 *        _design_id       - hidden; the webhook uses it to find the design
 *        _preview         - hidden; thumbnail of the approved proof
 */
(function () {
  "use strict";

  var STYLE =
    ".jasmin-btn{display:block;width:100%;margin:12px 0;padding:14px 20px;border:0;border-radius:4px;" +
    "background:#2b2622;color:#fff;font:600 15px/1.2 inherit;letter-spacing:.04em;cursor:pointer}" +
    ".jasmin-btn:hover{background:#000}.jasmin-btn[disabled]{opacity:.6;cursor:wait}" +
    ".jasmin-note{font-size:13px;opacity:.75;margin:4px 0 0}" +
    ".jasmin-overlay{position:fixed;inset:0;z-index:2147483000;background:rgba(20,16,12,.6);display:flex;" +
    "align-items:center;justify-content:center;padding:2vh 2vw}" +
    ".jasmin-frame{width:min(1280px,100%);height:min(900px,100%);border:0;border-radius:12px;background:#f5f1eb;" +
    "box-shadow:0 30px 80px rgba(0,0,0,.35)}" +
    "@media (max-width:820px){.jasmin-overlay{padding:0}.jasmin-frame{width:100%;height:100%;border-radius:0}}";

  function addStyle() {
    if (document.getElementById("jasmin-style")) return;
    var s = document.createElement("style");
    s.id = "jasmin-style";
    s.textContent = STYLE;
    document.head.appendChild(s);
  }

  function productForm(root) {
    return (
      (root.closest && root.closest('form[action*="/cart/add"]')) ||
      document.querySelector('form[action*="/cart/add"]')
    );
  }

  function currentVariant(root) {
    var form = productForm(root);
    var input = form && form.querySelector('[name="id"]');
    return (input && input.value) || root.getAttribute("data-variant-id");
  }

  function currentQuantity(root) {
    var form = productForm(root);
    var input = (form && form.querySelector('[name="quantity"]')) || document.querySelector('[name="quantity"]');
    var q = parseInt(input && input.value, 10);
    return q > 0 ? q : 1;
  }

  function hideBuyButtons(root) {
    var form = productForm(root);
    if (!form) return;
    var nodes = form.querySelectorAll('[type="submit"], [name="add"], .shopify-payment-button');
    for (var i = 0; i < nodes.length; i++) {
      if (!root.contains(nodes[i])) nodes[i].style.display = "none";
    }
  }

  function init(root) {
    var appUrl = (root.getAttribute("data-app-url") || "").replace(/\/$/, "");
    var handle = root.getAttribute("data-product-handle");
    var label = root.getAttribute("data-button-label") || "Personalize";
    var cartAddUrl = root.getAttribute("data-cart-add-url") || "/cart/add.js";
    var cartUrl = root.getAttribute("data-cart-url") || "/cart";
    if (!appUrl || !handle) return;
    var appOrigin = new URL(appUrl, location.href).origin;

    fetch(appUrl + "/api/storefront/products/" + encodeURIComponent(handle))
      .then(function (r) {
        return r.json();
      })
      .then(function (info) {
        if (!info || !info.templateId) return;
        addStyle();
        var button = document.createElement("button");
        button.type = "button";
        button.className = "jasmin-btn";
        button.textContent = label;
        var note = document.createElement("p");
        note.className = "jasmin-note";
        note.textContent = "Add your photos and wording, preview the print proof, then add to cart.";
        root.appendChild(button);
        root.appendChild(note);
        if (root.getAttribute("data-hide-add-to-cart") !== "false") hideBuyButtons(root);
        button.addEventListener("click", function () {
          open(info.templateId);
        });
      })
      .catch(function () {
        /* app unreachable: leave the normal product page alone */
      });

    function open(templateId) {
      var overlay = document.createElement("div");
      overlay.className = "jasmin-overlay";
      var frame = document.createElement("iframe");
      frame.className = "jasmin-frame";
      frame.title = "Personalize";
      frame.allow = "clipboard-write";
      frame.src =
        appUrl + "/editor/" + encodeURIComponent(templateId) + "?embed=1&origin=" + encodeURIComponent(location.origin);
      overlay.appendChild(frame);
      document.body.appendChild(overlay);
      var prevOverflow = document.body.style.overflow;
      document.body.style.overflow = "hidden";

      function close() {
        window.removeEventListener("message", onMessage);
        document.body.style.overflow = prevOverflow;
        overlay.remove();
      }

      function onMessage(event) {
        if (event.origin !== appOrigin || !event.data || typeof event.data !== "object") return;
        if (event.data.type === "jasmin:close") close();
        if (event.data.type === "jasmin:approved") addToCart(event.data, close);
      }

      window.addEventListener("message", onMessage);
    }

    function addToCart(design, close) {
      var body = {
        items: [
          {
            id: Number(currentVariant(root)),
            quantity: currentQuantity(root),
            properties: {
              Personalization: design.summary || "Personalised design",
              _design_id: design.designId,
              _preview: design.thumbnailUrl,
            },
          },
        ],
      };
      fetch(cartAddUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(body),
      })
        .then(function (r) {
          if (!r.ok) throw new Error("cart");
          location.href = cartUrl;
        })
        .catch(function () {
          close();
          alert("Your design is saved (reference " + design.designId + ") but could not be added to the cart. Please try again.");
        });
    }
  }

  function boot() {
    var roots = document.querySelectorAll(".jasmin-personalize");
    for (var i = 0; i < roots.length; i++) init(roots[i]);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
