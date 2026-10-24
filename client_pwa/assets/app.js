const sections = ["account", "payments", "documents", "notifications", "expenses"];
const panels = {
  login: document.querySelector("#login-panel"),
  password: document.querySelector("#password-panel"),
  portal: document.querySelector("#portal-panel")
};
const logoutButton = document.querySelector("#logout-button");
const installButton = document.querySelector("#install-button");
const loginError = document.querySelector("#login-error");
const passwordError = document.querySelector("#password-error");
let installPrompt = null;

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[char]);
}

function formatAmount(amount, currency) {
  const parsed = Number(amount);
  if (!Number.isFinite(parsed)) return `${escapeHtml(amount)} ${escapeHtml(currency)}`;
  return `${new Intl.NumberFormat("es-VE", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(parsed)} ${escapeHtml(currency)}`;
}

function show(panel) {
  Object.values(panels).forEach((element) => element.classList.add("hidden"));
  if (panel) panel.classList.remove("hidden");
  logoutButton.classList.toggle("hidden", panel !== panels.portal);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    credentials: "same-origin",
    cache: "no-store",
    headers: { ...(options.body ? { "Content-Type": "application/json" } : {}), ...options.headers }
  });
  if (response.status === 204) return null;
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(typeof payload.detail === "string" ? payload.detail : payload.detail?.message || "No se pudo completar la solicitud.");
    error.status = response.status;
    error.code = payload.detail?.code;
    throw error;
  }
  return payload;
}

function setMessage(element, message, isError = false) {
  element.textContent = message;
  element.classList.toggle("hidden", !message);
  element.classList.toggle("error", isError);
}

function emptyState(container, message) {
  container.innerHTML = `<p class="muted">${escapeHtml(message)}</p>`;
}

function renderSummary(summary) {
  document.querySelector("#client-name").textContent = summary.name;
  document.querySelector("#client-location").textContent = `${summary.residence} · Habitación ${summary.room || "—"} · Cama ${summary.bed || "—"}`;
  const entries = [
    ["Estado", summary.active ? "Activa" : "Finalizada"],
    ["Situación de pago", summary.payment_status],
    ["Próximo vencimiento", summary.next_payment],
    ["Fecha de ingreso", summary.admission_date],
    ...(summary.discharged_on ? [["Fecha de salida", summary.discharged_on]] : [])
  ];
  document.querySelector("#account-details").innerHTML = entries.map(([label, value]) =>
    `<div class="detail"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`
  ).join("");
}

function renderPayments(items) {
  const container = document.querySelector("#payments-list");
  if (!items.length) return emptyState(container, "Todavía no hay pagos registrados.");
  container.innerHTML = items.map((item) =>
    `<article class="record"><div><h3>${escapeHtml(item.kind)}</h3><p>Fecha: ${escapeHtml(item.paid_on)}${item.payment_method ? ` · ${escapeHtml(item.payment_method)}` : ""}</p></div><div><strong>${formatAmount(item.amount, item.currency)}</strong><br><a href="${escapeHtml(item.receipt_url)}">Descargar recibo</a></div></article>`
  ).join("");
}

function renderDocuments(items) {
  const container = document.querySelector("#documents-list");
  if (!items.length) return emptyState(container, "No hay documentos disponibles.");
  container.innerHTML = items.map((item) =>
    `<article class="record"><h3>${escapeHtml(item.name)}</h3><a href="${escapeHtml(item.url)}">Descargar PDF</a></article>`
  ).join("");
}

function residenceName(index) {
  return index === 0 ? "Residencia Carmen Luisa" : "Residencia Las Acacias";
}

function renderNotifications(items) {
  const container = document.querySelector("#notifications-list");
  if (!items.length) return emptyState(container, "Todavía no hay avisos.");
  container.innerHTML = items.map((item) => {
    const payload = item.payload;
    const title = item.kind === "expense" ? "Gasto registrado" : payload.message;
    const description = item.kind === "expense"
      ? `${payload.concept} · ${formatAmount(payload.amount, payload.currency)} · ${residenceName(payload.residence)} · ${payload.paid_on}`
      : `${residenceName(payload.residence)} · ${item.created_at}`;
    return `<article class="record"><div><h3>${escapeHtml(title)}</h3><p>${escapeHtml(description)}</p></div></article>`;
  }).join("");
}

function renderExpenses(items) {
  const container = document.querySelector("#expenses-list");
  if (!items.length) return emptyState(container, "Todavía no hay gastos publicados.");
  container.innerHTML = items.map((item) =>
    `<article class="record"><div><h3>${escapeHtml(item.concept)}</h3><p>${escapeHtml(item.residence)} · ${escapeHtml(item.paid_on)}</p></div><strong>${formatAmount(item.amount, item.currency)}</strong></article>`
  ).join("");
}

async function loadPortal() {
  const session = await api("/api/session");
  if (session.must_change_password) {
    show(panels.password);
    return;
  }
  const [summary, payments, documents, notifications, expenses] = await Promise.all([
    api("/api/client/summary"),
    api("/api/client/payments"),
    api("/api/client/documents"),
    api("/api/client/notifications"),
    api("/api/client/expenses")
  ]);
  renderSummary(summary);
  renderPayments(payments);
  renderDocuments(documents);
  renderNotifications(notifications);
  renderExpenses(expenses);
  show(panels.portal);
}

document.querySelector("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  setMessage(loginError, "");
  const formElement = event.currentTarget;
  const form = new FormData(formElement);
  try {
    await api("/api/login", {
      method: "POST",
      body: JSON.stringify({
        username: form.get("username"),
        password: form.get("password")
      })
    });
    formElement.reset();
    await loadPortal();
  } catch (error) {
    setMessage(loginError, error.message, true);
  }
});

document.querySelector("#password-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  setMessage(passwordError, "");
  const formElement = event.currentTarget;
  const form = new FormData(formElement);
  try {
    await api("/api/password", {
      method: "POST",
      body: JSON.stringify({
        current_password: form.get("current_password"),
        new_password: form.get("new_password")
      })
    });
    formElement.reset();
    show(panels.login);
    setMessage(loginError, "Contraseña actualizada. Inicia sesión con tu nueva contraseña.");
  } catch (error) {
    setMessage(passwordError, error.message, true);
  }
});

logoutButton.addEventListener("click", async () => {
  try {
    await api("/api/login", { method: "DELETE" });
    show(panels.login);
  } catch (error) {
    setMessage(document.querySelector("#session-message"), error.message, true);
  }
});

document.querySelectorAll(".nav-item").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    sections.forEach((section) => {
      document.querySelector(`#section-${section}`).classList.toggle("hidden", section !== button.dataset.section);
    });
  });
});

window.addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault();
  installPrompt = event;
  installButton.classList.remove("hidden");
});

installButton.addEventListener("click", async () => {
  if (!installPrompt) return;
  installPrompt.prompt();
  await installPrompt.userChoice;
  installPrompt = null;
  installButton.classList.add("hidden");
});

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/service-worker.js").catch((error) => {
    console.error("No se pudo registrar el modo instalable.", error);
  });
}

loadPortal().catch((error) => {
  if (error.status === 401) {
    show(panels.login);
  } else if (error.code === "password_change_required") {
    show(panels.password);
  } else {
    show(panels.login);
    setMessage(loginError, error.message, true);
  }
});
