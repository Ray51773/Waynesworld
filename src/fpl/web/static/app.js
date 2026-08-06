// Live deadline countdown. Everything else is server-rendered.
(function () {
  const el = document.querySelector("[data-deadline]");
  if (!el) return;

  const target = new Date(el.getAttribute("data-deadline"));

  function tick() {
    const ms = target - new Date();
    if (ms <= 0) {
      el.textContent = "passed";
      return;
    }
    const s = Math.floor(ms / 1000);
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    el.textContent = d > 0 ? `${d}d ${h}h ${m}m` : h > 0 ? `${h}h ${m}m ${sec}s` : `${m}m ${sec}s`;
  }

  tick();
  setInterval(tick, 1000);
})();

// Submit the player filters on change, so there is no Apply button to forget.
(function () {
  const form = document.getElementById("filters");
  if (!form) return;
  form.querySelectorAll("select, input[type=number]").forEach((input) => {
    input.addEventListener("change", () => form.submit());
  });
})();

// Refresh button: kick off a data refresh, then poll until it finishes.
(function () {
  const button = document.getElementById("refresh-button");
  const status = document.getElementById("refresh-status");
  if (!button || !status) return;

  let polling = null;

  function setStatus(text, className) {
    status.textContent = text;
    status.className = "refresh-status" + (className ? " " + className : "");
  }

  async function poll() {
    try {
      const state = await (await fetch("/api/refresh")).json();
      if (state.running) return;

      clearInterval(polling);
      polling = null;
      button.disabled = false;
      button.textContent = "Refresh data";

      if (state.error) {
        setStatus(state.error, "down");
        return;
      }
      setStatus(state.summary || "Done.", "up");
      // Reload so every figure on the page reflects the new data.
      setTimeout(() => window.location.reload(), 1200);
    } catch (error) {
      clearInterval(polling);
      polling = null;
      button.disabled = false;
      button.textContent = "Refresh data";
      setStatus("Lost contact with the server.", "down");
    }
  }

  button.addEventListener("click", async () => {
    button.disabled = true;
    button.textContent = "Refreshing…";
    setStatus("Fetching latest prices, injuries and news…");

    try {
      const response = await fetch("/api/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ players: false }),
      });
      const result = await response.json();
      if (!result.ok) {
        setStatus(result.error || "Could not start.", "down");
        button.disabled = false;
        button.textContent = "Refresh data";
        return;
      }
      polling = setInterval(poll, 1000);
    } catch (error) {
      setStatus("Could not reach the server.", "down");
      button.disabled = false;
      button.textContent = "Refresh data";
    }
  });
})();
