let lastId = 0;
async function refresh(){
  try {
    const res = await fetch(`/api/events?after=${lastId}`, {cache:"no-store"});
    if(!res.ok) return;
    const data = await res.json();
    document.getElementById("opens").textContent = data.totals.opens;
    document.getElementById("clicks").textContent = data.totals.clicks;
    document.getElementById("submissions").textContent = data.totals.submissions;
    document.getElementById("sim_visits").textContent = data.totals.sim_visits;
    document.getElementById("email_stage").textContent = data.totals.email_stage;
    document.getElementById("password_stage").textContent = data.totals.password_stage;
    document.getElementById("completed").textContent = data.totals.completed;
    const box = document.getElementById("activity");
    data.events.forEach(e => {
      lastId = Math.max(lastId, e.id);
      const row = document.createElement("div"); row.className="event";
      const dot = document.createElement("span"); dot.className="event-dot";
      const content = document.createElement("div");
      const title = document.createElement("b"); title.textContent=e.event_type.replaceAll("_"," ");
      const detail = document.createElement("span"); detail.textContent=`${e.email} · ${e.name}`;
      const time = document.createElement("time"); time.textContent=e.created_at;
      content.append(title,detail); row.append(dot,content,time); box.prepend(row);
    });
  } catch(e) {}
}
refresh(); setInterval(refresh,5000);
