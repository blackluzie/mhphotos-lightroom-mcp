"""A trip as a story: days, places, route and best photos, as the base of a blog draft for mhphotos.de.

Input are capture-date records (photos_by_date) of the trip. Output is a day-by-day outline and a Markdown
draft with placeholders that Claude (or Martin) turns into the finished text. Read only.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import date
from typing import Any

WEEKDAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    r = 6371.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def _point(r: dict[str, Any]) -> tuple[float, float] | None:
    gps = r.get("gps")
    if isinstance(gps, dict) and isinstance(gps.get("latitude"), int | float):
        return float(gps["latitude"]), float(gps["longitude"])
    return None


def _place_name(place: str | None) -> str | None:
    """'Hafen, Palma, Balearen, Spanien' -> 'Palma' (the city part when there is one)."""
    if not place:
        return None
    parts = [p.strip() for p in place.split(",") if p.strip()]
    if len(parts) >= 3:
        return parts[-3]
    return parts[0] if parts else None


def outline(records: list[dict[str, Any]], best_per_day: int = 3, min_rating: int = 3) -> dict[str, Any]:
    days: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in sorted(records, key=lambda r: str(r.get("t") or "")):
        if r.get("t"):
            days[str(r["t"])[:10]].append(r)
    out_days = []
    total_km = 0.0
    countries: Counter[str] = Counter()
    for day, recs in sorted(days.items()):
        places: list[str] = []
        for r in recs:
            name = _place_name(r.get("place"))
            if name and name not in places:
                places.append(name)
            if r.get("place"):
                countries[str(r["place"]).split(",")[-1].strip()] += 1
        points = [p for p in (_point(r) for r in recs) if p]
        km = 0.0
        thinned: list[tuple[float, float]] = []
        for p in points:
            if not thinned or _km(thinned[-1], p) >= 0.2:
                if thinned:
                    km += _km(thinned[-1], p)
                thinned.append(p)
        total_km += km
        ranked = sorted((r for r in recs if int(r.get("r") or 0) >= min_rating),
                        key=lambda r: (-int(r.get("r") or 0), str(r.get("t"))))
        weekday = WEEKDAYS[date.fromisoformat(day).weekday()]
        out_days.append({
            "day": day, "weekday": weekday, "photos": len(recs), "places": places[:8],
            "km": round(km, 1), "start": thinned[0] if thinned else None, "end": thinned[-1] if thinned else None,
            "best": [{"id": r["id"], "file": r.get("file"), "rating": int(r.get("r") or 0),
                      "captured": r.get("t"), "place": r.get("place")} for r in ranked[:best_per_day]],
        })
    return {"days": out_days, "photos": sum(d["photos"] for d in out_days), "km_total": round(total_km, 1),
            "countries": [c for c, _ in countries.most_common(6)]}


def markdown(title: str, plan: dict[str, Any]) -> str:
    lines = [f"# {title}", "", "_Einleitung: Worum ging es, wer war dabei, was war das Highlight?_", ""]
    if plan.get("countries"):
        lines += [(f"**Stationen:** {', '.join(plan['countries'])} · **Strecke:** etwa {plan['km_total']:.0f} km "
                   "(aus den GPS-Daten der Fotos)"), ""]
    for n, day in enumerate(plan["days"], 1):
        when = f"{day['weekday']}, {day['day'][8:10]}.{day['day'][5:7]}.{day['day'][:4]}"
        head = " → ".join(day["places"][:4]) if day["places"] else "Unterwegs"
        lines += [f"## Tag {n}: {head}", "", f"*{when}*", ""]
        if day["km"] >= 1:
            lines.append(f"Route an diesem Tag: etwa {day['km']:.0f} km.")
        if day["end"]:
            lat, lon = day["end"]
            lines.append(f"[Karte](https://www.openstreetmap.org/?mlat={lat:.5f}&mlon={lon:.5f}#map=12/{lat:.4f}/{lon:.4f})")
        lines += ["", "_Text: Was haben wir gesehen und erlebt?_", ""]
        for b in day["best"]:
            lines.append(f"[Foto: {b['file']} · {'★' * b['rating']} · Lightroom {b['id']}]")
        lines.append("")
    lines += ["## Fazit", "", "_Fazit und Tipps für alle, die die Reise nachmachen wollen._", ""]
    return "\n".join(lines)
