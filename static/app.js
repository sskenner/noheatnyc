// No Heat NYC: live "is heat required?" answer, address search, chart tooltips.
(function () {
  var base = document.body.getAttribute("data-base") || "/";

  // ---- live answer ------------------------------------------------------
  function nycNow() {
    var parts = new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York", month: "numeric", day: "numeric",
      hour: "numeric", minute: "2-digit", hourCycle: "h23"
    }).formatToParts(new Date());
    var get = function (t) { return parseInt(parts.find(function (p) { return p.type === t; }).value, 10); };
    return { month: get("month"), day: get("day"), hour: get("hour") % 24 };
  }

  function getTemperatureF() {
    var url = "https://api.weather.gov/stations/KNYC/observations?limit=6";
    return fetch(url, { headers: { Accept: "application/geo+json" } })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (data) {
        var obs = (data.features || []).find(function (f) {
          return f.properties.temperature && f.properties.temperature.value !== null;
        });
        if (!obs) return null;
        return { f: Math.round(obs.properties.temperature.value * 9 / 5 + 32), at: new Date(obs.properties.timestamp) };
      })
      .catch(function () { return null; });
  }

  function answer(el, word, why, required) {
    var a = el.querySelector(".a");
    a.textContent = word;
    a.classList.toggle("no", !required);
    el.querySelector(".why").textContent = why;
  }

  var hero = document.getElementById("answer");
  if (hero) {
    var now = nycNow();
    var inSeason = now.month >= 10 || now.month <= 5;
    var night = now.hour >= 22 || now.hour < 6;
    if (!inSeason) {
      answer(hero, "NO", "Heat season runs October 1 to May 31. Hot water (at least 120°F) is required all year.", false);
    } else if (night) {
      answer(hero, "YES", "It's between 10 PM and 6 AM, so it must be at least 62°F inside, whatever the weather.", true);
    } else {
      getTemperatureF().then(function (t) {
        if (!t) {
          answer(hero, "MAYBE", "Between 6 AM and 10 PM, heat is required whenever it's below 55°F outside. We couldn't get the current temperature.", true);
          return;
        }
        var when = t.at.toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" });
        if (t.f < 55) {
          answer(hero, "YES", "It's " + t.f + "°F in Central Park (as of " + when + "). Below 55°F outside means it must be at least 68°F inside until 10 PM.", true);
        } else {
          answer(hero, "NOT YET", "It's " + t.f + "°F in Central Park (as of " + when + "), so daytime heat isn't required. From 10 PM it must be at least 62°F inside.", false);
        }
      });
    }
  }

  // ---- address search ---------------------------------------------------
  var ABBR = { st: "street", str: "street", ave: "avenue", av: "avenue", rd: "road", pl: "place", blvd: "boulevard",
    pkwy: "parkway", dr: "drive", ln: "lane", ct: "court", ter: "terrace", e: "east", w: "west", n: "north", s: "south" };
  function norm(s) {
    return s.toLowerCase().replace(/[.,#]/g, " ").replace(/\b(\d+)(st|nd|rd|th)\b/g, "$1")
      .split(/\s+/).filter(Boolean).map(function (w) { return ABBR[w] || w; }).join(" ");
  }

  var input = document.getElementById("q"), list = document.getElementById("results"), index = null;
  function load() {
    if (index) return Promise.resolve(index);
    return fetch(base + "search.json").then(function (r) { return r.json(); }).then(function (rows) {
      index = rows.map(function (r) { return { key: norm(r[0]), row: r }; });
      return index;
    });
  }
  function render(q) {
    var nq = norm(q);
    list.textContent = "";
    if (nq.length < 3) return;
    var hits = index.filter(function (e) { return e.key.indexOf(nq) !== -1; }).slice(0, 10);
    if (!hits.length) {
      var li = document.createElement("li");
      li.textContent = "No match. That building may have no heat complaints this season.";
      list.appendChild(li);
      return;
    }
    hits.forEach(function (e) {
      var r = e.row, li = document.createElement("li"), a = document.createElement("a"), s = document.createElement("span");
      a.href = base + r[3];
      a.textContent = r[0];
      s.className = "muted";
      s.textContent = " · " + r[1] + " · " + r[4].toLocaleString() + " complaints this season";
      li.appendChild(a); li.appendChild(s); list.appendChild(li);
    });
  }
  if (input) {
    input.addEventListener("focus", load, { once: true });
    input.addEventListener("input", function () { load().then(function () { render(input.value); }); });
  }

  // ---- chart tooltips ---------------------------------------------------
  document.querySelectorAll(".chart-wrap").forEach(function (wrap) {
    var tip = wrap.querySelector(".tip"), svg = wrap.querySelector("svg");
    wrap.querySelectorAll(".col").forEach(function (col) {
      col.setAttribute("tabindex", "0");
      function show() {
        var bar = col.querySelector(".bar").getBBox(), scale = svg.clientWidth / svg.viewBox.baseVal.width;
        tip.textContent = col.getAttribute("data-tip");
        tip.style.left = (bar.x + bar.width / 2) * scale + "px";
        tip.style.top = bar.y * scale + "px";
        tip.hidden = false;
      }
      col.addEventListener("mouseenter", show);
      col.addEventListener("focus", show);
      col.addEventListener("mouseleave", function () { tip.hidden = true; });
      col.addEventListener("blur", function () { tip.hidden = true; });
    });
  });
})();
