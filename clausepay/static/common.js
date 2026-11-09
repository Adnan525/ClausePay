// Shared by the Transactions page (main app) and the Live sales page (debugging window).
(function () {
  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (key === "class") node.className = value; else if (key === "text") node.textContent = value; else node.setAttribute(key, value);
    }
    for (const child of children.flat()) if (child != null && child !== false) node.append(child);
    return node;
  }

  const aud = (value) => "A$" + Number(value).toLocaleString("en-AU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const newId = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));
  const when = (iso) => (iso ? new Date(iso).toLocaleString("en-AU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "");

  async function api(path, body) {
    try {
      const response = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      return { ok: response.ok, data: await response.json().catch(() => ({ error: "Unexpected reply from the server." })) };
    } catch (error) {
      return { ok: false, data: { error: "Lost the connection to the server." } };
    }
  }

  // The refund button and its steps for one sale. `ui` is a per-sale object that survives re-rendering:
  // { open, amount, request_id, message, needs }. `reload` fetches fresh data and re-renders the page.
  function refundControls(sale, ui, reload, rerender) {
    const box = el("div", { class: "refundbox" });
    const left = Number(sale.refundable);
    if (Number(sale.refunded) > 0) {
      box.append(el("div", { class: "role", text: "Refunded " + aud(sale.refunded) + " of " + aud(sale.gross) + (left > 0 ? ". " + aud(sale.refundable) + " left to refund." : ". Fully refunded.") }));
    }
    if (left <= 0) return box;
    const go = async (confirmed) => {
      ui.request_id = ui.request_id || newId();
      ui.message = "Refunding…"; rerender();
      const { ok, data } = await api("/api/live/sales/" + sale.order_id + "/refund", { amount: ui.amount.trim(), request_id: ui.request_id, partners_confirmed: confirmed });
      if (data.needs_partner_transfer) { ui.needs = data.needs_partner_transfer; ui.message = ""; }
      else if (ok) { ui.amount = ""; ui.request_id = null; ui.needs = null; ui.open = false; ui.message = "✓ Refunded."; }
      else { ui.message = data.error || "Could not refund."; ui.needs = null; ui.request_id = null; }
      await reload();
    };
    if (!ui.open) {
      const open = el("button", { type: "button", class: "secondary", text: "Refund…" });
      open.addEventListener("click", () => { ui.open = true; ui.message = ""; rerender(); });
      box.append(open);
    } else {
      const amount = el("input", { value: ui.amount, placeholder: Number(sale.refundable).toFixed(2) + " (all that is left)", inputmode: "decimal", autocomplete: "off", "aria-label": "Refund amount in A$" });
      amount.addEventListener("input", () => { ui.amount = amount.value; ui.request_id = null; ui.needs = null; });
      const refund = el("button", { type: "button", text: "Refund" });
      refund.addEventListener("click", () => go(false));
      const cancel = el("button", { type: "button", class: "secondary", text: "Cancel" });
      cancel.addEventListener("click", () => { ui.open = false; ui.needs = null; ui.request_id = null; ui.message = ""; rerender(); });
      box.append(el("div", { class: "refundrow" }, el("span", { text: "A$" }), amount, refund, cancel));
    }
    if (ui.needs) {
      const confirm = el("button", { type: "button", class: "ok", text: "Demo: partners have transferred in. Continue the refund." });
      confirm.addEventListener("click", () => go(true));
      box.append(el("div", { class: "demo-note" }, el("strong", { text: "Partners must put money in first. " }),
        el("span", { text: "The contract says this refund (" + aud(ui.needs.amount) + ") is funded " + aud(ui.needs.from_reserve) + " from the reserve and the rest by the partners: " +
          Object.entries(ui.needs.transfers).map(([name, value]) => name + " " + aud(value)).join(", ") + ". In production ClausePay would collect this from their saved PayPal wallets; here you confirm it." }),
        el("details", {}, el("summary", { text: "See the working" }), el("ul", {}, ui.needs.lines.map((line) => el("li", { text: line })))), confirm));
    }
    if (ui.message) box.append(el("p", { class: "role " + (ui.message.startsWith("✓") ? "ok-text" : ui.message.endsWith("…") ? "" : "bad"), text: ui.message }));
    return box;
  }

  window.CP = { el, aud, api, newId, when, refundControls };
})();
