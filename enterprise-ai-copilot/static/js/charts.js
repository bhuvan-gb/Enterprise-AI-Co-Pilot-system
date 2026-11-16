/* Chart.js helpers with theme-aware colours */
const PALETTE = ["#6B5BD2", "#0E1A3D", "#9A8CFF", "#2F6FDE", "#1E9E6A", "#C98512", "#D6453D"];
const STATUS_COLORS = { "To Do": "#9AA4C2", "In Progress": "#2F6FDE", "Completed": "#1E9E6A", "Overdue": "#D6453D" };
function themeColors() { const d = document.documentElement.dataset.bsTheme === "dark"; return { text: d ? "#9AA4C2" : "#667089", grid: d ? "#243056" : "#E3E6F0" }; }
function applyTheme(chart) {
  const c = themeColors();
  Object.values(chart.options.scales || {}).forEach(s => { s.ticks = Object.assign(s.ticks || {}, { color: c.text }); s.grid = Object.assign(s.grid || {}, { color: c.grid }); });
  if (chart.options.plugins?.legend?.labels) chart.options.plugins.legend.labels.color = c.text;
  chart.update("none");
}
function mkChart(id, type, data, extra = {}) {
  const el = document.getElementById(id); if (!el || typeof Chart === "undefined") return null;
  const c = themeColors(); const circular = type === "doughnut" || type === "pie";
  const options = Object.assign({ responsive: true, maintainAspectRatio: false,
    plugins: { legend: { position: circular ? "bottom" : "top", labels: { color: c.text, usePointStyle: true, boxWidth: 8 } } },
    scales: circular ? {} : { x: { ticks: { color: c.text }, grid: { display: false } }, y: { beginAtZero: true, ticks: { color: c.text, precision: 0 }, grid: { color: c.grid } } } }, extra);
  return new Chart(el, { type, data, options });
}
function hasData(arr) { return (arr || []).some(v => v > 0); }
function showEmpty(id) { const el = document.getElementById(id); if (!el) return; el.parentElement.innerHTML = '<div class="empty"><i class="fa-solid fa-chart-simple"></i><h6>No data for this selection</h6><p class="mb-0">Create tasks or change the filters to see this chart.</p></div>'; }
window.addEventListener("themechange", () => Object.values(Chart.instances).forEach(applyTheme));
