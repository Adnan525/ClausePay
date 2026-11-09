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
    const stuck = (sale.refunds || []).find((refund) => refund.status === "collecting");
    if (left <= 0) return box;
    const go = async (confirmed) => {
      ui.request_id = ui.request_id || newId();
      ui.message = "Refunding…"; rerender();
      const { ok, data } = await api("/api/live/sales/" + sale.order_id + "/refund", { amount: ui.amount.trim(), request_id: ui.request_id, partners_confirmed: confirmed });
      if (data.needs_partner_transfer) { ui.needs = data.needs_partner_transfer; ui.message = ""; }
      else if (ok) { ui.amount = ""; ui.request_id = null; ui.needs = null; ui.open = false; ui.message = "✓ Refunded."; }
      else { ui.message = data.error || "Could not refund."; ui.needs = null; if (!data.keep_request) ui.request_id = null; }  // a half-finished refund keeps its id, so retrying never charges twice
      await reload();
    };
    if (stuck) {
      const resume = el("button", { type: "button", text: "Retry this refund" });
      resume.addEventListener("click", () => { ui.request_id = stuck.request_id; ui.amount = ""; go(!!stuck.partners_confirmed); });
      box.append(el("div", { class: "demo-note" }, el("strong", { text: "A refund of " + aud(stuck.amount) + " stopped half-way. " }),
        el("span", { text: (stuck.note || "") + " Retrying is safe: anyone already charged is not charged again." }), el("div", {}, resume)));
      return box;
    }
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
      const n = ui.needs, auto = n.automatic || [], manual = n.manual || [];
      const confirm = el("button", { type: "button", class: "ok", text: "Demo: " + manual.join(" and ") + (manual.length > 1 ? " have" : " has") + " transferred in. Continue the refund." });
      confirm.addEventListener("click", () => go(true));
      box.append(el("div", { class: "demo-note" }, el("strong", { text: "Partners must put money in first. " }),
        el("span", { text: "The contract says this refund (" + aud(n.amount) + ") is funded " + aud(n.from_reserve) + " from the reserve and the rest by the partners: " +
          Object.entries(n.transfers).map(([name, value]) => name + " " + aud(value)).join(", ") + ". " +
          (auto.length ? auto.join(" and ") + (auto.length > 1 ? "'s wallets are" : "'s wallet is") + " charged automatically. " : "") +
          (manual.length ? manual.join(" and ") + (manual.length > 1 ? " have" : " has") + " not authorized ClausePay yet: authorize them (Transactions page), or confirm here that they transferred the money themselves (demo)." : "") }),
        el("details", {}, el("summary", { text: "See the working" }), el("ul", {}, n.lines.map((line) => el("li", { text: line })))), confirm));
    }
    if (ui.message) box.append(el("p", { class: "role " + (ui.message.startsWith("✓") ? "ok-text" : ui.message.endsWith("…") ? "" : "bad"), text: ui.message }));
    return box;
  }

  // One row per partner: authorized or not, with a button that opens PayPal's approval page (like a sale does).
  function walletStrip(wallets, onChange) {
    const box = el("div", {});
    for (const wallet of wallets) {
      const row = el("div", { class: "walletrow" }, el("strong", { text: wallet.name }), el("span", { class: "role", text: wallet.email }));
      if (wallet.authorized) {
        const remove = el("button", { type: "button", class: "secondary small", text: "Remove" });
        remove.addEventListener("click", async () => { await api("/api/wallets/" + encodeURIComponent(wallet.name) + "/remove", {}); onChange(); });
        row.append(el("span", { class: "chip ready", text: "✓ wallet authorized" }), remove);
      } else {
        const button = el("button", { type: "button", text: "Authorize " + wallet.name });
        const note = el("span", { class: "role" });
        button.addEventListener("click", async () => {
          button.disabled = true;
          const { ok, data } = await api("/api/wallets/" + encodeURIComponent(wallet.name) + "/authorize", {});
          if (!ok) { note.textContent = data.error || "Could not start."; button.disabled = false; return; }
          const popup = window.open(data.approve_url, "paypal-wallet", "width=480,height=760");
          note.textContent = popup ? "PayPal opened in a new window. Log in as " + wallet.name + " and approve." : "Your browser blocked the window.";
          const timer = setInterval(async () => { await onChange(); if (!popup || popup.closed) { clearInterval(timer); button.disabled = false; } }, 2000);
        });
        row.append(el("span", { class: "chip", text: "not authorized" }), button, note);
      }
      box.append(row);
    }
    return box;
  }

  window.CP = { el, aud, api, newId, when, refundControls, walletStrip };
})();
