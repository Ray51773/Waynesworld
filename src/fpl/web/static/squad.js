// Squad builder. Keeps the fifteen client-side while you pick, then POSTs once.
(function () {
  const poolEl = document.getElementById("player-pool");
  if (!poolEl) return;

  const POOL = JSON.parse(poolEl.textContent);
  const BY_ID = new Map(POOL.map((p) => [p.element_id, p]));
  const REQUIRED = { GKP: 2, DEF: 5, MID: 5, FWD: 3 };
  const ORDER = ["GKP", "DEF", "MID", "FWD"];
  const MAX_PER_CLUB = 3;
  const BUDGET = 1000; // tenths

  // element_id -> purchase price in tenths
  let squad = new Map();

  const existingEl = document.getElementById("existing-squad");
  if (existingEl) {
    const existing = JSON.parse(existingEl.textContent);
    existing.players.forEach((p) => squad.set(p.element_id, p.purchase_price));
    document.getElementById("bank").value = (existing.bank / 10).toFixed(1);
    document.getElementById("free-transfers").value = existing.free_transfers;
  }

  const $ = (id) => document.getElementById(id);

  function sellValue(purchase, now) {
    if (now <= purchase) return now;
    return purchase + Math.floor((now - purchase) / 2);
  }

  function counts() {
    const byPos = { GKP: 0, DEF: 0, MID: 0, FWD: 0 };
    const byClub = {};
    for (const id of squad.keys()) {
      const p = BY_ID.get(id);
      byPos[p.position]++;
      byClub[p.team] = (byClub[p.team] || 0) + 1;
    }
    return { byPos, byClub };
  }

  function problems() {
    const out = [];
    const { byPos, byClub } = counts();
    for (const pos of ORDER) {
      if (byPos[pos] > REQUIRED[pos]) out.push(`Too many ${pos}: ${byPos[pos]} of ${REQUIRED[pos]}`);
    }
    for (const [club, n] of Object.entries(byClub)) {
      if (n > MAX_PER_CLUB) out.push(`${n} players from ${club}, the limit is ${MAX_PER_CLUB}`);
    }
    const spend = totalPurchase();
    const bank = Math.round(parseFloat($("bank").value || "0") * 10);
    if (spend + bank > BUDGET && squad.size === 15) {
      out.push(`Squad cost plus bank is £${((spend + bank) / 10).toFixed(1)}m, over the £100.0m limit`);
    }
    return out;
  }

  function totalPurchase() {
    let total = 0;
    for (const price of squad.values()) total += price;
    return total;
  }

  function totalSell() {
    let total = 0;
    for (const [id, purchase] of squad.entries()) {
      total += sellValue(purchase, BY_ID.get(id).now_cost);
    }
    return total;
  }

  function renderSquad() {
    const container = $("squad-slots");
    container.innerHTML = "";
    const { byPos } = counts();

    for (const pos of ORDER) {
      const group = document.createElement("div");
      group.className = "slot-group";
      const heading = document.createElement("div");
      heading.className = "slot-head";
      heading.innerHTML = `<span class="tag pos-${pos}">${pos}</span> <span class="muted">${byPos[pos]} of ${REQUIRED[pos]}</span>`;
      group.appendChild(heading);

      const members = [...squad.keys()].map((id) => BY_ID.get(id)).filter((p) => p.position === pos);
      members.sort((a, b) => b.now_cost - a.now_cost);

      for (const player of members) {
        const purchase = squad.get(player.element_id);
        const sell = sellValue(purchase, player.now_cost);
        const row = document.createElement("div");
        row.className = "slot";
        row.innerHTML = `
          <span class="slot-name">${player.web_name}</span>
          <span class="muted slot-team">${player.team}</span>
          <span class="num slot-now">${(player.now_cost / 10).toFixed(1)}</span>
          <label class="slot-paid">paid
            <input type="number" step="0.1" min="3.5" value="${(purchase / 10).toFixed(1)}"
                   data-id="${player.element_id}">
          </label>
          <span class="num slot-sell" title="selling value">${(sell / 10).toFixed(1)}</span>
          <button type="button" class="slot-remove" data-id="${player.element_id}">×</button>`;
        group.appendChild(row);
      }

      for (let i = members.length; i < REQUIRED[pos]; i++) {
        const empty = document.createElement("div");
        empty.className = "slot empty";
        empty.textContent = "empty";
        group.appendChild(empty);
      }
      container.appendChild(group);
    }

    container.querySelectorAll(".slot-remove").forEach((button) => {
      button.addEventListener("click", () => {
        squad.delete(parseInt(button.dataset.id, 10));
        render();
      });
    });
    container.querySelectorAll(".slot-paid input").forEach((input) => {
      input.addEventListener("change", () => {
        const id = parseInt(input.dataset.id, 10);
        squad.set(id, Math.round(parseFloat(input.value || "0") * 10));
        render();
      });
    });
  }

  function renderSummary() {
    $("squad-count").textContent = `${squad.size} of 15`;
    $("squad-value").textContent = (totalSell() / 10).toFixed(1);
    const bank = Math.round(parseFloat($("bank").value || "0") * 10);
    $("squad-remaining").textContent = ((BUDGET - totalPurchase() - bank) / 10).toFixed(1);

    const list = problems();
    const box = $("squad-problems");
    box.innerHTML = "";
    if (list.length) {
      box.className = "problems bad";
      list.forEach((text) => {
        const item = document.createElement("div");
        item.textContent = text;
        box.appendChild(item);
      });
    } else if (squad.size === 15) {
      box.className = "problems ok";
      box.textContent = "Legal squad. Save it, then open Transfers.";
    } else {
      box.className = "problems";
      box.textContent = `${15 - squad.size} more to pick.`;
    }
  }

  function renderPool() {
    const search = $("pool-search").value.trim().toLowerCase();
    const position = $("pool-position").value;
    const maxPrice = parseFloat($("pool-max").value || "99") * 10;
    const sort = $("pool-sort").value;

    let rows = POOL.filter((p) => {
      if (squad.has(p.element_id)) return false;
      if (position !== "all" && p.position !== position) return false;
      if (p.now_cost > maxPrice) return false;
      if (search && !(p.web_name.toLowerCase().includes(search) ||
                      (p.full_name || "").toLowerCase().includes(search))) return false;
      return true;
    });

    rows.sort((a, b) => (b[sort] ?? 0) - (a[sort] ?? 0));
    rows = rows.slice(0, 150);

    const body = $("pool-body");
    body.innerHTML = "";
    const { byPos, byClub } = counts();

    for (const player of rows) {
      const full = byPos[player.position] >= REQUIRED[player.position];
      const clubFull = (byClub[player.team] || 0) >= MAX_PER_CLUB;
      const tr = document.createElement("tr");
      tr.className = full || clubFull ? "row-blocked" : "row-add";
      tr.innerHTML = `
        <td>${player.web_name}${player.status !== "a" ? ' <span class="tag bad" title="' + (player.news || "") + '">!</span>' : ""}</td>
        <td>${player.team}</td>
        <td><span class="tag pos-${player.position}">${player.position}</span></td>
        <td class="num">${player.price.toFixed(1)}</td>
        <td class="num">${player.xp_next.toFixed(1)}</td>
        <td class="num"><b>${player.xp_horizon.toFixed(1)}</b></td>
        <td class="num">${player.selected_by_percent.toFixed(1)}%</td>`;
      if (!full && !clubFull) {
        tr.addEventListener("click", () => {
          squad.set(player.element_id, player.now_cost);
          render();
        });
      } else {
        tr.title = full ? `${player.position} slots are full` : `Already have ${MAX_PER_CLUB} from ${player.team}`;
      }
      body.appendChild(tr);
    }
  }

  function render() {
    renderSquad();
    renderSummary();
    renderPool();
  }

  ["pool-search", "pool-position", "pool-max", "pool-sort"].forEach((id) => {
    $(id).addEventListener("input", renderPool);
  });
  $("bank").addEventListener("input", renderSummary);

  $("clear-squad").addEventListener("click", () => {
    squad = new Map();
    render();
  });

  $("save-squad").addEventListener("click", async () => {
    const status = $("save-status");
    if (squad.size !== 15) {
      status.textContent = `Need 15 players, have ${squad.size}.`;
      status.className = "down";
      return;
    }
    status.textContent = "Saving…";
    status.className = "muted";

    const payload = {
      players: [...squad.entries()].map(([element_id, purchase_price]) => ({
        element_id, purchase_price,
      })),
      bank: Math.round(parseFloat($("bank").value || "0") * 10),
      free_transfers: parseInt($("free-transfers").value || "1", 10),
    };

    try {
      const response = await fetch("/squad/save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const result = await response.json();
      if (result.ok) {
        status.innerHTML = 'Saved. <a href="/transfers">See what to change →</a>';
        status.className = "up";
      } else {
        status.textContent = result.error || "Save failed.";
        status.className = "down";
      }
    } catch (error) {
      status.textContent = "Save failed: " + error.message;
      status.className = "down";
    }
  });

  render();
})();
