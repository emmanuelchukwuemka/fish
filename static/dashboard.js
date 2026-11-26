const AT_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="icon"><circle cx="12" cy="12" r="4"/><path d="M16 12v1.5a2.5 2.5 0 0 0 5 0V12a9 9 0 1 0-5.5 8.3"/></svg>';
const EVENT_ICONS = {
  simulation_visit: ["blue", '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="icon"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>'],
  email_stage: ["orange", AT_ICON],
  password_stage: ["purple", AT_ICON],
  simulation_completed: ["green", '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="icon"><circle cx="12" cy="12" r="9"/><path d="m8.5 12.5 2.5 2.5 5-5"/></svg>'],
};

let lastId = 0;
async function refresh() {
  try {
    const res = await fetch(`/api/events?after=${lastId}`, { cache: "no-store" });
    if (!res.ok) return;
    const data = await res.json();
    document.getElementById("sim_visits").textContent = data.totals.sim_visits;
    document.getElementById("email_stage").textContent = data.totals.email_stage;
    document.getElementById("password_stage").textContent = data.totals.password_stage;
    document.getElementById("completed").textContent = data.totals.completed;
    const box = document.getElementById("activity");
    data.events.forEach((e) => {
      lastId = Math.max(lastId, e.id);
      const [colorClass, svg] = EVENT_ICONS[e.event_type] || ["blue", ""];

      const row = document.createElement("div");
      row.className = "event";

      const iconWrap = document.createElement("span");
      iconWrap.className = "event-icon " + colorClass;
      iconWrap.innerHTML = svg;

      const content = document.createElement("div");
      const title = document.createElement("b");
      title.textContent = e.event_type.replaceAll("_", " ");
      const detail = document.createElement("span");
      let text = `${e.email} · ${e.name}`;
      if (e.detail) text += ` · submitted: ${e.detail}`;
      if (e.event_type === "simulation_completed" && e.detail2) text += ` · confirmed: ${e.detail2}`;
      detail.textContent = text;
      content.append(title, detail);

      const time = document.createElement("time");
      time.setAttribute("data-iso", e.created_at);
      time.textContent = window.PHISHLAB_timeAgo ? window.PHISHLAB_timeAgo(e.created_at) : e.created_at;

      row.append(iconWrap, content, time);
      box.prepend(row);
    });
  } catch (e) {}
}
refresh();
setInterval(refresh, 5000);
