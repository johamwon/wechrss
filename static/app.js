(() => {
  "use strict";

  // Theme switching
  const themeToggle = document.querySelector("[data-theme-toggle]");
  const savedTheme = localStorage.getItem("theme");
  const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  const initialTheme = savedTheme || (prefersDark ? "dark" : "light");
  document.documentElement.setAttribute("data-theme", initialTheme);
  updateThemeToggleUI(initialTheme);

  if (themeToggle) {
    themeToggle.addEventListener("click", () => {
      const current = document.documentElement.getAttribute("data-theme") || "light";
      const next = current === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      localStorage.setItem("theme", next);
      updateThemeToggleUI(next);
    });

  }
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
    if (!localStorage.getItem("theme")) {
      const systemTheme = e.matches ? "dark" : "light";
      document.documentElement.setAttribute("data-theme", systemTheme);
      updateThemeToggleUI(systemTheme);
    }
  });

  function updateThemeToggleUI(theme) {
    const toggle = document.querySelector("[data-theme-toggle]");
    if (!toggle) return;
    const icon = toggle.querySelector("[data-theme-icon]");
    if (icon) {
      icon.textContent = theme === "dark" ? "☀️" : "🌙";
    }
    toggle.setAttribute("aria-label", theme === "dark" ? "切换到浅色模式" : "切换到深色模式");
  }

  const toast = document.querySelector(".toast");
  let toastTimer;

  function showToast(message) {
    if (!toast) return;
    toast.textContent = message;
    toast.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove("show"), 1800);
  }

  async function copyText(value) {
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
    } catch (_) {
      const field = document.createElement("textarea");
      field.value = value;
      field.setAttribute("readonly", "");
      field.style.position = "fixed";
      field.style.opacity = "0";
      document.body.appendChild(field);
      field.select();
      document.execCommand("copy");
      field.remove();
    }
    showToast("已复制到剪贴板");
  }

  document.addEventListener("click", (event) => {
    const dismiss = event.target.closest("[data-dismiss]");
    if (dismiss) {
      dismiss.closest(".flash")?.remove();
      return;
    }

    const trigger = event.target.closest("[data-copy], [data-copy-target]");
    if (!trigger) return;
    const target = trigger.dataset.copyTarget
      ? document.querySelector(trigger.dataset.copyTarget)
      : null;
    copyText(target ? target.value || target.textContent : trigger.dataset.copy);
  });

  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  document.querySelectorAll("form[data-loading-form]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (event.defaultPrevented || form.hasAttribute("data-resolve-url")) return;
      const button = form.querySelector('button[type="submit"], button:not([type])');
      if (!button) return;
      button.disabled = true;
      button.dataset.busy = "1";
      button.dataset.originalLabel = button.textContent.trim();
      button.textContent = form.dataset.loadingLabel || "处理中…";
    });
  });

  const alertPanel = document.querySelector("[data-health-alerts]");
  async function pollHealth() {
    if (document.hidden) {
      setTimeout(pollHealth, 20000);
      return;
    }
    try {
      const response = await fetch("/api/experience", { cache: "no-store", credentials: "same-origin" });
      if (!response.ok) throw new Error("health unavailable");
      const state = await response.json();
      alertPanel?.querySelector("[data-offline]")?.remove();
      document.querySelectorAll("[data-sync-label]").forEach((label) => { label.textContent = state.sync_label; });
      const accountChip = document.querySelector(".account-chip");
      accountChip?.classList.toggle("online", state.configured && !state.account_issue);
      accountChip?.classList.toggle("offline", !state.configured || Boolean(state.account_issue));
      const alerts = state.alerts || [];
      const keys = alerts.map((alert) => alert.key).join(",");
      if (alertPanel) alertPanel.hidden = alerts.length === 0;
      if (alertPanel && keys !== alertPanel.dataset.alertKeys) {
        alertPanel.replaceChildren();
        alerts.forEach((alert) => {
          const row = document.createElement("div");
          row.className = "health-alert";
          const copy = document.createElement("div");
          const title = document.createElement("strong");
          title.textContent = alert.title;
          const message = document.createElement("p");
          message.textContent = alert.message;
          const action = document.createElement("a");
          action.className = "button";
          action.href = alert.href;
          action.textContent = alert.action;
          copy.append(title, message);
          row.append(copy, action);
          alertPanel.append(row);
        });
        alertPanel.hidden = alerts.length === 0;
        alertPanel.dataset.alertKeys = keys;
        if (alerts.length) showToast("有订阅需要处理，请查看页面顶部提示");
      }
    } catch (_) {
      // A disconnected browser must not look like a healthy service.
      if (alertPanel && !alertPanel.querySelector("[data-offline]")) {
        const warning = document.createElement("p");
        warning.dataset.offline = "1";
        warning.textContent = "暂时无法连接服务，状态可能不是最新。请检查服务是否运行后刷新页面。";
        alertPanel.append(warning);
        alertPanel.hidden = false;
      }
    }
    setTimeout(pollHealth, 20000);
  }
  if (alertPanel) {
    // Initial warnings are server rendered and persist across restarts.
    setTimeout(pollHealth, 20000);
  }

  const syncWatch = document.querySelector("[data-sync-watch]");
  if (syncWatch) {
    let failures = 0;
    async function pollSync() {
      try {
        const response = await fetch("/api/sources/" + syncWatch.dataset.syncWatch + "/status", {
          cache: "no-store", credentials: "same-origin",
        });
        if (!response.ok) throw new Error("sync status unavailable");
        const state = await response.json();
        failures = 0;
        if (!state.health.busy) {
          window.location.reload();
          return;
        }
      } catch (_) {
        failures += 1;
        if (failures === 3) {
          const warning = document.createElement("p");
          warning.setAttribute("role", "alert");
          warning.textContent = "暂时无法获取同步进度。请检查服务是否运行，然后刷新页面；请勿重复提交同步。";
          syncWatch.append(warning);
          return;
        }
      }
      setTimeout(pollSync, 1500);
    }
    setTimeout(pollSync, 1000);
  }

  document.querySelectorAll("form[data-resolve-url]").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = form.querySelector('button[type="submit"], button:not([type])');
      const originalLabel = button ? button.textContent.trim() : "";
      if (button) {
        button.disabled = true;
        button.dataset.busy = "1";
        button.textContent = form.dataset.loadingLabel || "补全中…";
      }
      try {
        const csrf = form.querySelector('input[name="csrf"]')?.value || "";
        const response = await fetch(form.action, {
          method: "POST",
          credentials: "same-origin",
          headers: {
            Accept: "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
          },
          body: new URLSearchParams(new FormData(form)),
        });
        if (!response.ok) throw new Error("request failed");
        const data = await response.json();
        if (!data.success) throw new Error(data.message || "补全失败");
        showToast("原文链接已补全");
        if (form.dataset.resolveUrl === "single" && data.url) {
          const actions = form.closest(".reader-actions");
          if (actions) {
            actions.innerHTML =
              '<a class="button primary" href="' + data.url + '" target="_blank" rel="noreferrer">打开微信原文 <span aria-hidden="true">↗</span></a>' +
              '<button class="button" type="button" data-copy="' + data.url + '"><svg viewBox="0 0 20 20" aria-hidden="true"><rect x="6.5" y="6.5" width="9" height="9" rx="1.5"/><path d="M4 13.5H3.5a1 1 0 0 1-1-1v-9a1 1 0 0 1 1-1h9a1 1 0 0 1 1 1V4"/></svg>复制链接</button>';
          }
        }
      } catch (err) {
        showToast("补全失败：" + (err.message || "未知错误"));
      } finally {
        if (button) {
          button.disabled = false;
          delete button.dataset.busy;
          button.textContent = originalLabel;
        }
      }
    });
  });

  const loginPanel = document.querySelector("[data-login-panel]");
  if (loginPanel) {
    const active = new Set(["requesting", "waiting", "scanned", "exchanging", "initializing"]);
    const labels = {
      idle: "空闲",
      requesting: "正在生成",
      waiting: "等待扫码",
      scanned: "等待确认",
      exchanging: "正在登录",
      initializing: "正在初始化",
      success: "已登录",
      expired: "已过期",
      declined: "已拒绝",
      cancelled: "已取消",
      error: "失败",
    };
    const titles = {
      requesting: "正在生成二维码",
      waiting: "等待微信扫码",
      scanned: "已扫码，请在手机上确认",
      exchanging: "正在连接微信读书",
      initializing: "正在初始化账号",
      success: "账号连接成功",
      expired: "二维码已过期",
      declined: "本次登录已拒绝",
      cancelled: "扫码登录已取消",
      error: "登录没有完成",
    };
    const badge = document.getElementById("login-badge");
    const title = document.getElementById("login-title");
    const message = document.getElementById("login-message");
    const wrap = document.getElementById("qr-wrap");
    const image = document.getElementById("qr-image");

    function applyLoginState(state) {
      const status = state.status || "idle";
      if (badge) {
        badge.textContent = labels[status] || status;
        badge.className = "badge " + status;
      }
      if (title && titles[status]) title.textContent = titles[status];
      if (message && state.message) message.textContent = state.message;
      if (wrap && image) {
        if (state.qr) {
          image.src = state.qr;
          wrap.hidden = false;
        } else {
          wrap.hidden = true;
          image.removeAttribute("src");
        }
      }
      return status;
    }

    async function pollLogin() {
      try {
        const response = await fetch("/api/login/status", {
          cache: "no-store",
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        });
        if (!response.ok) throw new Error("status request failed");
        const state = await response.json();
        const status = applyLoginState(state);
        if (status === "success") {
          setTimeout(() => {
            window.location.href = loginPanel.dataset.loginReturn || "/settings";
          }, 700);
          return;
        }
        if (active.has(status)) setTimeout(pollLogin, 1200);
      } catch (_) {
        setTimeout(pollLogin, 2500);
      }
    }

    if (loginPanel.dataset.loginPoll === "1") pollLogin();
  }

  // Batch operations - select all
  const selectAll = document.getElementById("select-all");
  if (selectAll) {
    const checkboxes = document.querySelectorAll('input[name="source_ids"]');

    selectAll.addEventListener("change", () => {
      checkboxes.forEach(cb => {
        cb.checked = selectAll.checked;
      });
    });

    // Update select-all state when individual checkboxes change
    checkboxes.forEach(cb => {
      cb.addEventListener("change", () => {
        const allChecked = Array.from(checkboxes).every(c => c.checked);
        const someChecked = Array.from(checkboxes).some(c => c.checked);
        selectAll.checked = allChecked;
        selectAll.indeterminate = someChecked && !allChecked;
      });
    });
  }

})();
