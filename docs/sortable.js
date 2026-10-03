// Minimal sortable tables: click a header in any table.sortable to sort by that column.
(function () {
  function val(cell) {
    var v = cell.getAttribute('data-v');
    if (v === null) v = cell.textContent.trim();
    var n = parseFloat(String(v).replace(/,/g, ''));
    return isNaN(n) || !/^-?[\d.,]+$/.test(String(v).trim()) ? String(v).toLowerCase() : n;
  }
  document.querySelectorAll('table.sortable').forEach(function (table) {
    var heads = table.querySelectorAll('thead th');
    heads.forEach(function (th, idx) {
      th.tabIndex = 0;
      th.setAttribute('role', 'button');
      function sort() {
        var asc = th.getAttribute('aria-sort') !== 'ascending';
        heads.forEach(function (h) { h.removeAttribute('aria-sort'); });
        th.setAttribute('aria-sort', asc ? 'ascending' : 'descending');
        var body = table.tBodies[0];
        var rows = Array.prototype.slice.call(body.rows);
        rows.sort(function (a, b) {
          var x = val(a.cells[idx]), y = val(b.cells[idx]);
          if (typeof x !== typeof y) { x = String(x); y = String(y); }
          return (x < y ? -1 : x > y ? 1 : 0) * (asc ? 1 : -1);
        });
        rows.forEach(function (r) { body.appendChild(r); });
      }
      th.addEventListener('click', sort);
      th.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); sort(); } });
    });
  });
})();
