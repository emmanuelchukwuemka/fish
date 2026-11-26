(function () {
  function timeAgo(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    const sec = Math.round((Date.now() - d.getTime()) / 1000);
    if (sec < 5) return "just now";
    if (sec < 60) return sec + "s ago";
    const min = Math.round(sec / 60);
    if (min < 60) return min + (min === 1 ? " min ago" : " mins ago");
    const hr = Math.round(min / 60);
    if (hr < 24) return hr + (hr === 1 ? " hour ago" : " hours ago");
    const day = Math.round(hr / 24);
    return day + (day === 1 ? " day ago" : " days ago");
  }
  function applyTimeAgo() {
    document.querySelectorAll("time[data-iso]").forEach(function (el) {
      el.textContent = timeAgo(el.getAttribute("data-iso"));
    });
  }
  window.PHISHLAB_timeAgo = timeAgo;
  applyTimeAgo();
  setInterval(applyTimeAgo, 15000);

  const search = document.getElementById("pageSearch");
  if (search) {
    search.addEventListener("input", function () {
      const q = search.value.trim().toLowerCase();
      document.querySelectorAll(".table-wrap table tbody tr").forEach(function (row) {
        row.style.display = !q || row.textContent.toLowerCase().includes(q) ? "" : "none";
      });
    });
  }

  // Password column eye toggle: no real password is ever stored, so this
  // only switches between masked dots and an explicit "not stored" notice.
  document.querySelectorAll(".pw-toggle").forEach(function (btn) {
    btn.addEventListener("click", function () {
      const wrap = btn.closest(".pw-mask");
      const dots = wrap.querySelector(".pw-dots");
      const real = wrap.querySelector(".pw-real");
      const revealed = wrap.getAttribute("data-revealed") === "true";
      wrap.setAttribute("data-revealed", String(!revealed));
      dots.classList.toggle("hidden", !revealed);
      real.classList.toggle("hidden", revealed);
    });
  });
})();
