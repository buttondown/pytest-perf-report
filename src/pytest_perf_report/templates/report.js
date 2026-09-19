function sortTable(table, idx, dir) {
  var tbody = table.tBodies[0];
  var rows = Array.prototype.slice.call(tbody.rows);
  function value(row) {
    var cell = row.cells[idx];
    return (cell && parseFloat(cell.dataset.v || cell.textContent)) || 0;
  }
  function cmp(a, b) { return dir === 'desc' ? value(b) - value(a) : value(a) - value(b); }
  table.dataset.sortIdx = idx;
  table.dataset.sortDir = dir;
  // Grouped: order the family/solo rows, then re-attach each family's cases
  // under it. Ungrouped: one flat order over the cases, families parked last.
  var grouped = !table.classList.contains('ungrouped');
  var heads = rows.filter(function (r) {
    return grouped ? !r.classList.contains('case') : !r.classList.contains('fam');
  });
  var cases = {};
  rows.forEach(function (r) {
    if (grouped && r.classList.contains('case')) {
      (cases[r.dataset.fam] = cases[r.dataset.fam] || []).push(r);
    }
  });
  heads.sort(cmp);
  heads.forEach(function (head) {
    tbody.appendChild(head);
    var kids = cases[head.dataset.fam];
    if (kids) { kids.sort(cmp); kids.forEach(function (k) { tbody.appendChild(k); }); }
  });
  if (!grouped) {
    rows.forEach(function (r) { if (r.classList.contains('fam')) tbody.appendChild(r); });
  }
}
document.querySelectorAll('th.sortable').forEach(function (th) {
  th.addEventListener('click', function () {
    var table = th.closest('table');
    var dir = th.dataset.dir === 'desc' ? 'asc' : 'desc';
    table.querySelectorAll('th.sortable').forEach(function (h) { delete h.dataset.dir; });
    th.dataset.dir = dir;
    sortTable(table, Array.prototype.indexOf.call(th.parentNode.children, th), dir);
  });
});
(function () {
  var table = document.getElementById('tests-table');
  if (!table) return;
  var toggle = document.getElementById('group-toggle');
  var rows = Array.prototype.slice.call(table.tBodies[0].rows);
  var families = {};
  rows.forEach(function (r) { if (r.classList.contains('fam')) families[r.dataset.fam] = r; });
  function apply() {
    var grouped = !table.classList.contains('ungrouped');
    rows.forEach(function (r) {
      if (r.classList.contains('fam')) {
        r.hidden = !grouped;
      } else if (r.classList.contains('case')) {
        var family = families[r.dataset.fam];
        r.hidden = grouped && !family.classList.contains('open');
      }
    });
  }
  Object.keys(families).forEach(function (key) {
    var family = families[key];
    family.addEventListener('click', function () {
      family.classList.toggle('open');
      family.querySelector('.caret').textContent =
        family.classList.contains('open') ? '▾' : '▸';
      apply();
    });
  });
  toggle.addEventListener('click', function () {
    var grouped = !table.classList.toggle('ungrouped');
    toggle.setAttribute('aria-pressed', String(grouped));
    sortTable(table, Number(table.dataset.sortIdx || 1), table.dataset.sortDir || 'desc');
    apply();
  });
  apply();
})();
var tabs = document.querySelectorAll('.tabs button');
function selectTab(name) {
  tabs.forEach(function (b) { b.setAttribute('aria-selected', String(b.dataset.tab === name)); });
  document.querySelectorAll('.tab-panel').forEach(function (p) { p.hidden = p.dataset.tab !== name; });
}
tabs.forEach(function (b) { b.addEventListener('click', function () { selectTab(b.dataset.tab); }); });
if (tabs.length) selectTab(tabs[0].dataset.tab);
