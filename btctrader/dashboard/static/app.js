/* Equity and drawdown charts (Chart.js 4) plus the table view.
   Chart.js may arrive late (CDN fallback), so the code waits for window.Chart.
   Series colors and text tokens are read from CSS custom properties, so light and
   dark mode use the validated palette from app.css. */
(function () {
  "use strict";

  var REFRESH_MS = 60000;
  var state = { days: 365, equity: null, drawdown: null, payload: null };
  var eurFmt = new Intl.NumberFormat("de-DE", { style: "currency", currency: "EUR" });
  var pctFmt = new Intl.NumberFormat("de-DE", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function theme() {
    return {
      bot: cssVar("--series-bot"),
      bh: cssVar("--series-bh"),
      dca: cssVar("--series-dca"),
      text: cssVar("--text-secondary"),
      grid: cssVar("--grid"),
      surface: cssVar("--surface-1")
    };
  }

  function deDate(iso) {
    return iso ? iso.slice(8, 10) + "." + iso.slice(5, 7) + "." + iso.slice(0, 4) : "";
  }

  /* Direct labels: series name at the last point (legend stays, identity is not color-alone). */
  var endLabels = {
    id: "endLabels",
    afterDatasetsDraw: function (chart) {
      var ctx = chart.ctx;
      var t = theme();
      ctx.save();
      ctx.font = "600 11px system-ui, sans-serif";
      ctx.fillStyle = t.text;
      ctx.textBaseline = "middle";
      var used = [];
      chart.data.datasets.forEach(function (ds, i) {
        var meta = chart.getDatasetMeta(i);
        if (meta.hidden || !meta.data.length) { return; }
        var last = meta.data[meta.data.length - 1];
        var y = last.y;
        used.forEach(function (u) { if (Math.abs(u - y) < 12) { y = u + 12; } });
        used.push(y);
        var x = Math.min(last.x + 6, chart.chartArea.right - 4);
        ctx.textAlign = x >= chart.chartArea.right - 40 ? "right" : "left";
        ctx.fillText(ds.label, x, y);
      });
      ctx.restore();
    }
  };

  function baseOptions(t, unit) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "index", intersect: false },
      layout: { padding: { right: 44 } },
      plugins: {
        legend: {
          position: "top",
          align: "start",
          labels: { color: t.text, boxWidth: 10, boxHeight: 10, usePointStyle: true, pointStyle: "line" }
        },
        tooltip: {
          backgroundColor: t.surface,
          titleColor: t.text,
          bodyColor: t.text,
          borderColor: t.grid,
          borderWidth: 1,
          callbacks: {
            title: function (items) { return items.length ? deDate(items[0].label) : ""; },
            label: function (item) {
              var v = item.parsed.y;
              return " " + item.dataset.label + ": " + (unit === "eur" ? eurFmt.format(v) : pctFmt.format(v) + " %");
            }
          }
        }
      },
      scales: {
        x: {
          ticks: { color: t.text, maxTicksLimit: 8, maxRotation: 0, callback: function (v) { return deDate(this.getLabelForValue(v)); } },
          grid: { display: false },
          border: { color: t.grid }
        },
        y: {
          ticks: { color: t.text, maxTicksLimit: 6, callback: function (v) { return unit === "eur" ? eurFmt.format(v) : pctFmt.format(v) + " %"; } },
          grid: { color: t.grid },
          border: { display: false }
        }
      },
      elements: { line: { borderWidth: 2, tension: 0 }, point: { radius: 0, hoverRadius: 5, hitRadius: 8 } }
    };
  }

  function lineDataset(label, values, color, fill) {
    return {
      label: label,
      data: values,
      borderColor: color,
      backgroundColor: fill ? color + "22" : color,
      fill: fill ? "origin" : false,
      pointHoverBackgroundColor: color,
      pointHoverBorderColor: theme().surface,
      pointHoverBorderWidth: 2
    };
  }

  function build(payload) {
    var t = theme();
    var s = payload.series;
    var eqEl = document.getElementById("equity-chart");
    var ddEl = document.getElementById("drawdown-chart");
    if (!eqEl || !ddEl) { return; }
    if (state.equity) { state.equity.destroy(); }
    if (state.drawdown) { state.drawdown.destroy(); }
    state.equity = new Chart(eqEl, {
      type: "line",
      data: {
        labels: s.dates,
        datasets: [
          lineDataset("Bot", s.equity, t.bot, false),
          lineDataset("Buy-and-Hold", s.bh, t.bh, false),
          lineDataset("DCA", s.dca, t.dca, false)
        ]
      },
      options: baseOptions(t, "eur"),
      plugins: [endLabels]
    });
    var ddOptions = baseOptions(t, "pct");
    ddOptions.scales.y.max = 0;
    state.drawdown = new Chart(ddEl, {
      type: "line",
      data: {
        labels: s.dates,
        datasets: [
          lineDataset("Bot", s.drawdown, t.bot, true),
          lineDataset("Buy-and-Hold", s.drawdown_bh, t.bh, true)
        ]
      },
      options: ddOptions,
      plugins: [endLabels]
    });
    fillTable(payload);
    note(payload);
  }

  function fillTable(payload) {
    var body = document.querySelector("#equity-table tbody");
    if (!body) { return; }
    var s = payload.series;
    var rows = [];
    for (var i = s.dates.length - 1; i >= 0; i--) {
      rows.push("<tr><td>" + deDate(s.dates[i]) + "</td><td class=\"num\">" + eurFmt.format(s.equity[i]) +
        "</td><td class=\"num\">" + eurFmt.format(s.bh[i]) + "</td><td class=\"num\">" + eurFmt.format(s.dca[i]) +
        "</td><td class=\"num\">" + pctFmt.format(s.drawdown[i]) + " %</td><td class=\"num\">" +
        pctFmt.format(s.drawdown_bh[i]) + " %</td></tr>");
    }
    body.innerHTML = rows.join("");
  }

  function note(payload) {
    var el = document.getElementById("equity-note");
    if (!el) { return; }
    var sm = payload.summary;
    if (!sm.rows) {
      el.textContent = payload.ledger_available ? "Noch keine Tages-Snapshots. Der Ledger schreibt täglich um 00:20 UTC einen Eintrag." : "Ledger-Datenbank noch nicht vorhanden.";
      return;
    }
    el.textContent = sm.rows + " Tage (" + deDate(sm.from) + " bis " + deDate(sm.to) + ") · Bot " +
      eurFmt.format(parseFloat(sm.equity_eur)) + " · B&H " + eurFmt.format(parseFloat(sm.bh_equity_eur)) +
      " · DCA " + eurFmt.format(parseFloat(sm.dca_equity_eur)) + " · max. Drawdown Bot " +
      pctFmt.format(sm.max_drawdown_bot_pct) + " %, B&H " + pctFmt.format(sm.max_drawdown_bh_pct) + " %";
  }

  function load() {
    fetch("/api/equity?days=" + state.days, { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (payload) {
        state.payload = payload;
        if (window.Chart) { build(payload); }
      })
      .catch(function (err) {
        var el = document.getElementById("equity-note");
        if (el) { el.textContent = "Daten konnten nicht geladen werden: " + err; }
      });
  }

  function bindRange() {
    var buttons = document.querySelectorAll(".range button[data-days]");
    buttons.forEach(function (b) {
      b.addEventListener("click", function () {
        buttons.forEach(function (o) { o.classList.remove("active"); });
        b.classList.add("active");
        state.days = parseInt(b.getAttribute("data-days"), 10) || 365;
        load();
      });
    });
  }

  function whenChartReady(fn) {
    if (window.Chart) { fn(); return; }
    var tries = 0;
    var timer = setInterval(function () {
      if (window.Chart || ++tries > 100) {
        clearInterval(timer);
        if (window.Chart) { fn(); }
        else {
          var el = document.getElementById("equity-note");
          if (el) { el.textContent = "Chart.js nicht geladen (weder lokal noch vom CDN). Die Tabellenansicht bleibt nutzbar."; }
          if (state.payload) { fillTable(state.payload); }
        }
      }
    }, 100);
  }

  function start() {
    bindRange();
    load();
    whenChartReady(function () { if (state.payload) { build(state.payload); } });
    setInterval(load, REFRESH_MS);
    var mq = window.matchMedia("(prefers-color-scheme: dark)");
    var rethemer = function () { if (state.payload && window.Chart) { build(state.payload); } };
    if (mq.addEventListener) { mq.addEventListener("change", rethemer); }
    document.addEventListener("vendor-loaded", function () { if (state.payload && window.Chart) { build(state.payload); } });
    /* The header's "Stand" is refreshed by the tiles partial itself (hx-swap-oob span rendered
       server-side in TZ_DISPLAY), so it never switches to the browser's clock or time zone. */
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
