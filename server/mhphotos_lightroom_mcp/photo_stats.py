"""Photo statistics page: what the catalog snapshot says about how you shoot (0.1.29).

Works on the rows of the last catalog check (``audit/snapshot.json.gz``), so nothing is sent to Lightroom and
nothing leaves the Mac. ``compute`` returns plain numbers, ``render_html`` turns them into one self-contained
page (German and English texts, light and dark theme). Read only.
"""

from __future__ import annotations

import html
import json
import math
import re
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .catalog_insights import GB, photo_year

WEEKDAYS_DE = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
WEEKDAYS_EN = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
PRIMES = (14, 16, 20, 24, 28, 35, 40, 50, 58, 70, 85, 105, 135, 150, 200, 300)
HOME = (51.0, 10.0)  # fallback for the light calculation: Germany, when a photo carries no GPS
STALE_MONTHS = 12


# ---------------------------------------------------------------- helpers
def _dt(p: dict[str, Any]) -> datetime | None:
    t = str(p.get("t") or "")
    try:
        return datetime.fromisoformat(t[:19]) if len(t) >= 10 else None
    except ValueError:
        return None


def is_keeper(p: dict[str, Any]) -> bool:
    """A photo somebody looked at: stars, pick flag, colour label or an edit (and not rejected)."""
    if p.get("pick") == -1:
        return False
    return bool(p.get("r") or p.get("pick") == 1 or p.get("label") or p.get("edited"))


def _hit(p: dict[str, Any]) -> bool:
    return p.get("pick") != -1 and bool((p.get("r") or 0) >= 3 or p.get("pick") == 1)


def _orientation(p: dict[str, Any]) -> str | None:
    w, h = p.get("cw") or p.get("w"), p.get("ch") or p.get("h")
    if not w or not h:
        return None
    if abs(w - h) <= 0.02 * max(w, h):
        return "square"
    return "landscape" if w > h else "portrait"


def sun_times(day: date, lat: float, lon: float) -> tuple[float, float] | None:
    """Sunrise and sunset as local-clock decimal hours (NOAA approximation; the offset comes from the longitude,
    summer time is added for Central Europe). None in polar day/night."""
    n = day.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    lat_r = math.radians(lat)
    cos_ha = (math.cos(math.radians(90.833)) / (math.cos(lat_r) * math.cos(decl))) - math.tan(lat_r) * math.tan(decl)
    if not -1 <= cos_ha <= 1:
        return None
    ha = math.degrees(math.acos(cos_ha))
    rise_utc = 720 - 4 * (lon + ha) - eqtime
    set_utc = 720 - 4 * (lon - ha) - eqtime
    return rise_utc / 60, set_utc / 60


def _zone_hours(day: date, lon: float) -> float:
    """UTC offset for the clock that a photo was taken on: CET/CEST for Central Europe, else by longitude."""
    if -10 <= lon <= 30:
        return 2.0 if _eu_summer_time(day) else 1.0
    return round(lon / 15)


def _eu_summer_time(day: date) -> bool:
    def last_sunday(month: int) -> date:
        d = date(day.year, month, 31 if month in (3, 10) else 30)
        return d - timedelta(days=(d.weekday() + 1) % 7)
    return last_sunday(3) <= day < last_sunday(10)


def light_of(p: dict[str, Any], when: datetime) -> str:
    """blue (blue hour), golden (golden hour), day, night — from capture time and place."""
    lat, lon = (p.get("lat"), p.get("lon")) if p.get("lat") is not None and p.get("lon") is not None else HOME
    st = sun_times(when.date(), lat, lon)
    if st is None:
        return "day"
    off = _zone_hours(when.date(), lon)
    rise, sset = st[0] + off, st[1] + off
    h = when.hour + when.minute / 60
    for edge, lo, hi in ((rise, -0.25, 1.0), (sset, -1.0, 0.25)):  # golden hour: just after sunrise, just before sunset
        if edge + lo <= h <= edge + hi:
            return "golden"
    if rise - 0.75 <= h <= sset + 0.75:
        return "day" if rise + 1.0 < h < sset - 1.0 else "blue"
    return "night"


def max_aperture(lens: str | None) -> float | None:
    """Widest f-number from a lens name such as "FE 85mm F1.4 GM", "24-70mm f/2.8" or "35mm f/1.4-5.6"."""
    if not lens:
        return None
    for m in re.finditer(r"[fF]\s*/?\s*(\d+(?:[.,]\d+)?)", lens):
        v = float(m.group(1).replace(",", "."))
        if 0.7 <= v <= 8:
            return v
    m = re.search(r"\b1[:/](\d+(?:[.,]\d+)?)\b", lens)
    return float(m.group(1).replace(",", ".")) if m else None


def _share(part: int, whole: int) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


def _top(counter: Counter, n: int = 8) -> list[list[Any]]:
    return [[k, v] for k, v in counter.most_common(n)]


# ---------------------------------------------------------------- computation
def compute(photos: list[dict[str, Any]], years: list[int] | None = None, today: date | None = None) -> dict[str, Any]:
    """All numbers of the page. years limits the capture years (None = every year)."""
    today = today or datetime.now(UTC).date()
    pool = [p for p in photos if (p.get("fmt") or "").upper() != "VIDEO"]
    all_years = sorted({int(y) for y in (photo_year(p) for p in pool) if y.isdigit()})
    chosen = [p for p in pool if not years or (photo_year(p).isdigit() and int(photo_year(p)) in years)]
    total = len(chosen)
    out: dict[str, Any] = {"generated": today.isoformat(), "years_available": all_years,
                           "years": sorted(years) if years else all_years, "photos": total}
    if not total:
        return out

    days: Counter[str] = Counter()
    months: Counter[str] = Counter()
    by_year: Counter[str] = Counter()
    heat = [[0] * 24 for _ in range(7)]
    weekday: Counter[int] = Counter()
    light: Counter[str] = Counter()
    earliest = latest = None
    for p in chosen:
        by_year[photo_year(p)] += 1
        when = _dt(p)
        if not when:
            continue
        days[when.date().isoformat()] += 1
        months[f"{when.month:02d}"] += 1
        heat[when.weekday()][when.hour] += 1
        weekday[when.weekday()] += 1
        light[light_of(p, when)] += 1
        if when.time() != datetime.min.time():
            clock = when.hour * 60 + when.minute
            earliest = clock if earliest is None or clock < earliest else earliest
            latest = clock if latest is None or clock > latest else latest
    dated = sum(days.values())
    best_day = days.most_common(1)[0] if days else None
    streak = best = 0
    prev: date | None = None
    for d in sorted(days):
        cur = date.fromisoformat(d)
        streak = streak + 1 if prev and cur - prev == timedelta(days=1) else 1
        best = max(best, streak)
        prev = cur
    out["shooting_days"] = len(days)
    out["photos_per_day"] = round(dated / len(days), 1) if days else 0
    out["busiest_day"] = {"date": best_day[0], "photos": best_day[1]} if best_day else None
    out["longest_streak_days"] = best
    out["by_year"] = sorted(by_year.items())
    out["months"] = [[m, months.get(f"{m:02d}", 0)] for m in range(1, 13)]
    out["strongest_month"] = max(range(1, 13), key=lambda m: months.get(f"{m:02d}", 0)) if months else None
    out["empty_months"] = [m for m in range(1, 13) if not months.get(f"{m:02d}")] if dated else []
    out["weekday"] = [weekday.get(i, 0) for i in range(7)]
    out["heatmap"] = heat
    out["light"] = {k: _share(light.get(k, 0), dated) for k in ("golden", "blue", "day", "night")}
    out["earliest_minute"], out["latest_minute"] = earliest, latest

    # gear
    cams: Counter[str] = Counter(p["cam"] for p in chosen if p.get("cam"))
    lenses: Counter[str] = Counter(p["lens"] for p in chosen if p.get("lens"))
    out["cameras"] = _top(cams)
    out["lenses"] = _top(lenses)
    cam_years: dict[str, Counter[str]] = defaultdict(Counter)
    lens_last: dict[str, str] = {}
    lens_first: dict[str, str] = {}
    for p in pool:  # first/last use over the whole catalog, not only the chosen years
        t = str(p.get("t") or "")[:10]
        if not t:
            continue
        if p.get("lens"):
            lens_last[p["lens"]] = max(t, lens_last.get(p["lens"], t))
            lens_first[p["lens"]] = min(t, lens_first.get(p["lens"], t))
        if p.get("cam"):
            cam_years[p["cam"]][t[:4]] += 1
    out["gear_timeline"] = {"cameras": {c: sorted(v.items()) for c, v in cam_years.items()},
                            "lenses": {name: [lens_first[name], lens_last[name]] for name in lens_last}}
    limit = (today.replace(day=1) - timedelta(days=30 * (STALE_MONTHS - 1))).isoformat()
    out["lenses_unused"] = sorted([{"lens": name, "last_used": last} for name, last in lens_last.items() if last < limit],
                                  key=lambda r: r["last_used"])

    # shooting habits
    fl = Counter(int(p["fl"]) for p in chosen if p.get("fl"))
    ap = Counter(round(float(p["ap"]), 1) for p in chosen if p.get("ap"))
    iso = Counter(int(p["iso"]) for p in chosen if p.get("iso"))
    ss = Counter(round(float(p["ss"]), 5) for p in chosen if p.get("ss"))
    out["focal"] = _top(fl, 10)
    out["aperture"] = _top(ap, 10)
    out["iso"] = _top(iso, 10)
    out["shutter"] = [[(f"1/{round(1 / s)}" if s < 0.9 else f"{s:g} s"), n] for s, n in ss.most_common(10)]
    out["exposure_known"] = {"aperture": sum(ap.values()), "iso": sum(iso.values()), "shutter": sum(ss.values())}
    if iso:
        out["records"] = {"highest_iso": max(iso), "slowest_shutter": (min(ss) if ss else None),
                          "longest_focal": max(fl) if fl else None, "widest_focal": min(fl) if fl else None}
    orient = Counter(o for o in (_orientation(p) for p in chosen) if o)
    out["format"] = {k: _share(orient.get(k, 0), sum(orient.values())) for k in ("landscape", "portrait", "square")}
    cropped = [p for p in chosen if p.get("edited") and p.get("cw") and p.get("w")]
    out["crop_share"] = _share(sum(1 for p in cropped if p["cw"] < p["w"] - 2 or (p.get("ch") or 0) < (p.get("h") or 0) - 2),
                               len(cropped))

    # hit rate
    keepers = sum(1 for p in chosen if is_keeper(p))
    hits = sum(1 for p in chosen if _hit(p))
    out["keepers"] = {"count": keepers, "share": _share(keepers, total)}
    out["hit_rate"] = {"count": hits, "share": _share(hits, total)}
    out["hit_by_year"] = [[y, _share(sum(1 for p in chosen if photo_year(p) == y and _hit(p)), n)]
                          for y, n in sorted(by_year.items())]
    per_lens: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for p in chosen:
        if p.get("lens"):
            per_lens[p["lens"]][0] += 1
            per_lens[p["lens"]][1] += _hit(p)
    out["hit_by_lens"] = sorted(([k, v[0], _share(v[1], v[0])] for k, v in per_lens.items() if v[0] >= 20),
                                key=lambda r: -r[2])[:8]

    # storage
    unedited = [p for p in chosen if not is_keeper(p) and not p.get("kw")]
    size_all = sum(int(p.get("size") or 0) for p in chosen)
    size_un = sum(int(p.get("size") or 0) for p in unedited)
    by_year_gb: dict[str, float] = defaultdict(float)
    for p in unedited:
        by_year_gb[photo_year(p)] += int(p.get("size") or 0) / GB
    out["storage"] = {"gb": round(size_all / GB, 1), "never_touched": len(unedited),
                      "never_touched_share": _share(len(unedited), total), "never_touched_gb": round(size_un / GB, 1),
                      "by_year_gb": sorted((y, round(v, 1)) for y, v in by_year_gb.items())}

    # focal length finder and open aperture
    out["focal_finder"] = _focal_finder(chosen, lenses, fl)
    out["open_aperture"] = _open_aperture(chosen)
    out["profile"] = _profile(out, fl, ap)
    return out


def _focal_finder(chosen: list[dict[str, Any]], lenses: Counter[str], fl: Counter[int]) -> dict[str, Any]:
    """Prime lengths by use, and the prime that would cover most photos taken at the end of a zoom."""
    total = sum(fl.values())
    if not total:
        return {}
    owned_primes = set()
    zoom_names = {name for name in lenses if re.search(r"\d\s*[-–]\s*\d", name)}
    for name in lenses:
        if name not in zoom_names:
            m = re.search(r"(\d{2,3})\s*mm", name, re.IGNORECASE)
            if m:
                owned_primes.add(int(m.group(1)))
    on_zoom: Counter[int] = Counter(int(p["fl"]) for p in chosen if p.get("fl") and p.get("lens") in zoom_names)
    best_prime, best_cover = None, 0
    for prime in PRIMES:
        if prime in owned_primes:
            continue
        cover = sum(n for f, n in on_zoom.items() if abs(math.log(f / prime)) <= 0.09)  # within ~9 % of the length
        if cover > best_cover:
            best_prime, best_cover = prime, cover
    return {"suggestion": best_prime, "photos_covered": best_cover,
            "share_of_zoom": _share(best_cover, sum(on_zoom.values())), "owned_primes": sorted(owned_primes),
            "zoom_focal": _top(on_zoom, 8)}


def _open_aperture(chosen: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: dict[str, list[Any]] = {}
    for p in chosen:
        widest = max_aperture(p.get("lens"))
        if not widest or not p.get("ap"):
            continue
        r = rows.setdefault(p["lens"], [widest, 0, 0, []])
        r[1] += 1
        r[2] += float(p["ap"]) <= widest * 1.1
        r[3].append(float(p["ap"]))
    out = []
    for lens, (widest, n, wide, aps) in rows.items():
        if n < 20:
            continue
        aps.sort()
        out.append({"lens": lens, "widest": widest, "photos": n, "at_widest_share": _share(wide, n),
                    "median_aperture": aps[len(aps) // 2]})
    out.sort(key=lambda r: -r["photos"])
    return out[:8]


def _profile(out: dict[str, Any], fl: Counter[int], ap: Counter[float]) -> dict[str, Any]:
    top_fl = fl.most_common(1)[0][0] if fl else None
    top_ap = ap.most_common(1)[0][0] if ap else None
    fmt = max(out["format"], key=lambda k: out["format"][k]) if any(out["format"].values()) else None
    light = out.get("light") or {}
    return {"focal": top_fl, "aperture": top_ap, "format": fmt, "day_light": (light.get("day", 0) >= 60)}


# ---------------------------------------------------------------- rendering
def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def compute_all(photos: list[dict[str, Any]], today: date | None = None) -> dict[str, Any]:
    """Whole catalog plus one block per capture year (the page switches between them offline)."""
    out = compute(photos, None, today)
    out["by_year_stats"] = {str(y): compute(photos, [y], today) for y in out.get("years_available", [])}
    return out


def render_html(stats: dict[str, Any], name: str | None = None) -> str:
    """One self-contained page: everything inline, no network access."""
    title = html.escape(name or "Meine Foto-Statistik")
    return _TEMPLATE.replace("__TITLE__", title).replace("__DATA__", _json(stats))


_TEMPLATE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f6f4ef;--card:#fff;--ink:#1d1d1f;--mute:#6b6b70;--line:#e3e0d8;--acc:#b5651d;--acc2:#2f6f73}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#121214;--card:#1c1c20;--ink:#ececee;--mute:#9a9aa2;--line:#2c2c32;--acc:#e0914a;--acc2:#5fb3b8}}
:root[data-theme=dark]{--bg:#121214;--card:#1c1c20;--ink:#ececee;--mute:#9a9aa2;--line:#2c2c32;--acc:#e0914a;--acc2:#5fb3b8}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:980px;margin:0 auto;padding:16px}
header{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;justify-content:space-between;margin:8px 0 16px}
h1{font-size:1.5rem;margin:0}h2{font-size:1.05rem;margin:0 0 4px}
.sub{color:var(--mute);font-size:.9rem;margin:0 0 12px}
.bar{display:flex;flex-wrap:wrap;gap:6px}
button{font:inherit;font-size:.85rem;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:999px;padding:4px 12px;cursor:pointer}
button.on{background:var(--acc);border-color:var(--acc);color:#fff}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
.kpi{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:12px}
.kpi div{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 12px}
.kpi b{display:block;font-size:1.5rem;color:var(--acc)}.kpi span{color:var(--mute);font-size:.8rem}
.row{display:grid;grid-template-columns:minmax(0,1.3fr) 2fr auto;gap:8px;align-items:center;font-size:.85rem;margin:3px 0}
.row i{display:block;height:10px;border-radius:5px;background:var(--acc2)}.row span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.row em{font-style:normal;color:var(--mute);min-width:3ch;text-align:right}
.heat{display:grid;grid-template-columns:2.2em repeat(24,1fr);gap:2px;font-size:.62rem;color:var(--mute)}
.heat div{aspect-ratio:1;border-radius:2px;background:var(--acc);min-height:8px}
.note{font-size:.9rem;color:var(--mute);margin:6px 0 0}
.tip{border-left:3px solid var(--acc);padding:2px 10px;margin:8px 0}
footer{color:var(--mute);font-size:.8rem;text-align:center;margin:20px 0}
</style></head><body><main>
<header><h1 id="ttl"></h1><div class="bar"><span id="yrs" class="bar"></span><span class="bar"><button id="lang"></button><button id="theme"></button></span></div></header>
<div id="app"></div>
<footer id="foot"></footer>
</main>
<script>
const ALL=__DATA__;
const T={de:{title:"Meine Foto-Statistik",photos:"Fotos",days:"Aufnahmetage",perday:"Fotos pro Tag",streak:"Längste Serie (Tage)",best:"Stärkster Tag",
 year:"Jahre",allyears:"Alle Jahre",months:"Fotos pro Monat",heat:"Wann ich fotografiere (Wochentag × Uhrzeit)",cams:"Kameras",lenses:"Objektive",
 focal:"Brennweiten",aper:"Blenden",iso:"ISO",shut:"Verschlusszeiten",format:"Format",crop:"der bearbeiteten Bilder sind beschnitten",
 keep:"Behalten",hit:"Trefferquote (ab 3 Sterne oder Flagge)",unt:"nie angefasst",gb:"GB",light:"Lichtarten",golden:"Goldene Stunde",blue:"Blaue Stunde",day:"Tageslicht",night:"Nacht",
 unused:"Objektive, die du seit über einem Jahr nicht benutzt hast",none:"Keine.",finder:"Brennweiten-Finder",open:"Lohnt sich die Offenblende?",
 store:"Speicher aufräumen",concl:"Schlussfolgerungen",profile:"Dein Profil",noexp:"Blende, ISO und Verschlusszeit fehlen noch: Plug-in 0.1.29 installieren und die Katalogprüfung (ohne Jahr) neu starten.",
 landscape:"Querformat",portrait:"Hochformat",square:"Quadrat",strong:"Stärkster Monat",empty:"Ohne Fotos",rec:"Rekorde",early:"Frühestes Bild",late:"Spätestes Bild",
 foot:"Alles lokal berechnet, es wird nichts hochgeladen. Stand: ",wk:["Mo","Di","Mi","Do","Fr","Sa","So"],mo:["Jan","Feb","Mär","Apr","Mai","Jun","Jul","Aug","Sep","Okt","Nov","Dez"]},
 en:{title:"My photo stats",photos:"Photos",days:"Shooting days",perday:"Photos per day",streak:"Longest streak (days)",best:"Busiest day",
 year:"Years",allyears:"All years",months:"Photos per month",heat:"When I shoot (weekday × hour)",cams:"Cameras",lenses:"Lenses",
 focal:"Focal lengths",aper:"Apertures",iso:"ISO",shut:"Shutter speeds",format:"Format",crop:"of the edited photos are cropped",
 keep:"Keepers",hit:"Hit rate (3+ stars or flagged)",unt:"never touched",gb:"GB",light:"Light",golden:"Golden hour",blue:"Blue hour",day:"Daylight",night:"Night",
 unused:"Lenses you have not used for over a year",none:"None.",finder:"Focal length finder",open:"Is the wide aperture worth it?",
 store:"Clean up storage",concl:"Conclusions",profile:"Your profile",noexp:"Aperture, ISO and shutter speed are missing: install plug-in 0.1.29 and rerun the catalog check (without a year).",
 landscape:"Landscape",portrait:"Portrait",square:"Square",strong:"Strongest month",empty:"Without photos",rec:"Records",early:"Earliest photo",late:"Latest photo",
 foot:"Everything is computed locally, nothing is uploaded. As of: ",wk:["Mon","Tue","Wed","Thu","Fri","Sat","Sun"],mo:["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]}};
let lang=(navigator.language||"de").startsWith("de")?"de":"en",sel=null;window.__S=ALL;
const fmt=n=>Number(n).toLocaleString(lang==="de"?"de-DE":"en-US"),clock=m=>m==null?"–":String(Math.floor(m/60)).padStart(2,"0")+":"+String(m%60).padStart(2,"0");
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const bars=(rows,unit)=>{const m=Math.max(1,...rows.map(r=>r[1]));return rows.map(r=>`<div class="row"><span title="${esc(r[0])}">${esc(r[0])}</span><i style="width:${Math.max(2,100*r[1]/m)}%"></i><em>${fmt(r[1])}${unit||""}</em></div>`).join("")||""};
function render(){
 const t=T[lang],d=window.__S;document.documentElement.lang=lang;document.getElementById("ttl").textContent=t.title;
 document.title=t.title;document.getElementById("lang").textContent=lang==="de"?"English":"Deutsch";
 const dark=document.documentElement.dataset.theme==="dark"||(!document.documentElement.dataset.theme&&matchMedia("(prefers-color-scheme:dark)").matches);
 document.getElementById("theme").textContent=dark?"☀":"☾";
 document.getElementById("yrs").innerHTML=`<button class="${sel?"":"on"}" data-y="all">${t.allyears}</button>`+ALL.years_available.map(y=>`<button class="${sel===y?"on":""}" data-y="${y}">${y}</button>`).join("");
 let h="";
 if(!d.photos){document.getElementById("app").innerHTML=`<p>${t.none}</p>`;return}
 h+=`<div class="kpi"><div><b>${fmt(d.photos)}</b><span>${t.photos}</span></div><div><b>${fmt(d.shooting_days)}</b><span>${t.days}</span></div><div><b>${d.photos_per_day}</b><span>${t.perday}</span></div><div><b>${d.hit_rate.share} %</b><span>${t.hit}</span></div><div><b>${d.storage.never_touched_share} %</b><span>${t.unt}</span></div></div>`;
 const exp=d.exposure_known;
 h+=`<div class="grid">`;
 h+=`<div class="card"><h2>${t.profile}</h2>${profile(d,t)}</div>`;
 h+=`<div class="card"><h2>${t.months}</h2>${bars(d.months.map(m=>[t.mo[m[0]-1],m[1]]))}<p class="note">${d.strongest_month?t.strong+": "+t.mo[d.strongest_month-1]:""}${d.empty_months.length?" · "+t.empty+": "+d.empty_months.map(m=>t.mo[m-1]).join(", "):""}</p></div>`;
 h+=`<div class="card" style="grid-column:1/-1"><h2>${t.heat}</h2>${heat(d,t)}<p class="note">${t.best}: ${d.busiest_day?d.busiest_day.date+" ("+fmt(d.busiest_day.photos)+")":"–"} · ${t.streak}: ${d.longest_streak_days} · ${t.early}: ${clock(d.earliest_minute)} · ${t.late}: ${clock(d.latest_minute)}</p></div>`;
 h+=`<div class="card"><h2>${t.cams}</h2>${bars(d.cameras)}</div><div class="card"><h2>${t.lenses}</h2>${bars(d.lenses)}</div>`;
 h+=`<div class="card"><h2>${t.focal}</h2>${bars(d.focal.map(r=>[r[0]+" mm",r[1]]))}</div>`;
 if(exp.aperture||exp.iso){h+=`<div class="card"><h2>${t.aper}</h2>${bars(d.aperture.map(r=>["f/"+r[0],r[1]]))}</div><div class="card"><h2>${t.iso}</h2>${bars(d.iso.map(r=>["ISO "+r[0],r[1]]))}</div><div class="card"><h2>${t.shut}</h2>${bars(d.shutter)}</div>`}else h+=`<div class="card"><p class="note">${t.noexp}</p></div>`;
 h+=`<div class="card"><h2>${t.format}</h2>${bars(["landscape","portrait","square"].map(k=>[t[k],d.format[k]]),"%")}<p class="note">${d.crop_share} % ${t.crop}</p></div>`;
 h+=`<div class="card"><h2>${t.light}</h2>${bars(["golden","blue","day","night"].map(k=>[t[k],d.light[k]]),"%")}</div>`;
 h+=`<div class="card"><h2>${t.hit}</h2>${bars(d.hit_by_year.map(r=>[r[0],r[1]]),"%")}${d.hit_by_lens.length?"<p class=note>"+d.hit_by_lens.slice(0,3).map(r=>esc(r[0])+": "+r[2]+" %").join("<br>")+"</p>":""}</div>`;
 h+=`<div class="card"><h2>${t.unused}</h2>${d.lenses_unused.length?d.lenses_unused.map(r=>`<div class="row"><span>${esc(r.lens)}</span><span></span><em>${r.last_used}</em></div>`).join(""):`<p class="note">${t.none}</p>`}</div>`;
 h+=`<div class="card"><h2>${t.finder}</h2>${finder(d,t)}</div>`;
 h+=`<div class="card"><h2>${t.open}</h2>${open_(d,t)}</div>`;
 h+=`<div class="card"><h2>${t.store}</h2><p>${fmt(d.storage.never_touched)} ${t.photos} · ${d.storage.never_touched_gb} ${t.gb} (${d.storage.never_touched_share} %)</p>${bars(d.storage.by_year_gb," "+t.gb)}</div>`;
 if(d.records)h+=`<div class="card"><h2>${t.rec}</h2><p class="note">ISO ${fmt(d.records.highest_iso)}${d.records.longest_focal?" · "+d.records.widest_focal+"–"+d.records.longest_focal+" mm":""}</p></div>`;
 h+=`<div class="card" style="grid-column:1/-1"><h2>${t.concl}</h2>${concl(d,t)}</div></div>`;
 document.getElementById("app").innerHTML=h;
 document.getElementById("foot").textContent=t.foot+d.generated;
}
function heat(d,t){const m=Math.max(1,...d.heatmap.flat());let h='<div class="heat"><div style="background:none"></div>'+[...Array(24).keys()].map(i=>`<div style="background:none;aspect-ratio:auto;text-align:center">${i%3?"":i}</div>`).join("");
 d.heatmap.forEach((row,i)=>{h+=`<div style="background:none;aspect-ratio:auto;display:flex;align-items:center">${t.wk[i]}</div>`+row.map((v,k)=>`<div title="${t.wk[i]} ${k}:00 · ${v}" style="opacity:${v?0.12+0.88*v/m:0.05}"></div>`).join("")});return h+"</div>"}
function profile(d,t){const p=d.profile,bits=[];if(p.focal)bits.push(p.focal+" mm");if(p.aperture)bits.push("f/"+p.aperture);if(p.format)bits.push(t[p.format]);bits.push(p.day_light?t.day:t.golden);
 return `<p style="font-size:1.2rem;color:var(--acc);margin:4px 0">${bits.join(" · ")}</p><p class="note">${(d.cameras[0]||[""])[0]} · ${(d.lenses[0]||[""])[0]}</p>`}
function finder(d,t){const f=d.focal_finder;if(!f||!f.suggestion)return `<p class="note">${t.none}</p>`;
 return `<p>${lang==="de"?`Ein ${f.suggestion}-mm-Festbrennweite würde ${fmt(f.photos_covered)} deiner Zoom-Fotos abdecken (${f.share_of_zoom} %).`:`A ${f.suggestion} mm prime would cover ${fmt(f.photos_covered)} of your zoom photos (${f.share_of_zoom} %).`}</p>`+bars(f.zoom_focal.map(r=>[r[0]+" mm",r[1]]))}
function open_(d,t){if(!d.open_aperture.length)return `<p class="note">${t.none}</p>`;
 return d.open_aperture.map(r=>`<p class="note" style="margin:4px 0"><b style="color:var(--ink)">${esc(r.lens)}</b><br>f/${r.widest}: ${r.at_widest_share} % · ${lang==="de"?"Median":"median"} f/${r.median_aperture}</p>`).join("")}
function concl(d,t){const o=[],de=lang==="de";
 if(d.focal_finder&&d.focal_finder.suggestion)o.push(de?`Nächstes Objektiv: eine ${d.focal_finder.suggestion}-mm-Festbrennweite.`:`Next lens: a ${d.focal_finder.suggestion} mm prime.`);
 if(d.lenses_unused.length)o.push(de?`${d.lenses_unused.length} Objektiv(e) seit über einem Jahr ungenutzt: Verkaufskandidaten.`:`${d.lenses_unused.length} lens(es) unused for over a year: candidates to sell.`);
 o.push(de?`${d.storage.never_touched_share} % der Fotos (${d.storage.never_touched_gb} GB) wurden nie bewertet oder bearbeitet.`:`${d.storage.never_touched_share} % of the photos (${d.storage.never_touched_gb} GB) were never rated or edited.`);
 if(d.light.golden<10)o.push(de?`Nur ${d.light.golden} % in der goldenen Stunde.`:`Only ${d.light.golden} % in the golden hour.`);
 const w=d.open_aperture.find(r=>r.at_widest_share<25);if(w)o.push(de?`${w.lens}: nur ${w.at_widest_share} % bei Offenblende, eine weniger lichtstarke Variante würde reichen.`:`${w.lens}: only ${w.at_widest_share} % at the widest aperture, a slower lens would do.`);
 return o.map(x=>`<div class="tip">${esc(x)}</div>`).join("")}
function select(y){sel=y==="all"?null:+y;window.__S=sel?(ALL.by_year_stats||{})[sel]||ALL:ALL;render()}
document.addEventListener("click",e=>{const b=e.target.closest("button");if(!b)return;
 if(b.id==="lang"){lang=lang==="de"?"en":"de";render()}else if(b.id==="theme"){const r=document.documentElement,dark=r.dataset.theme==="dark"||(!r.dataset.theme&&matchMedia("(prefers-color-scheme:dark)").matches);r.dataset.theme=dark?"light":"dark";render()}else if(b.dataset.y)select(b.dataset.y)});
render();
</script></body></html>
"""
