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
