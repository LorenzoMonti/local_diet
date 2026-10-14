"""Lista della spesa: aggregazione deterministica degli ingredienti del piano."""
import json
from collections import defaultdict

from . import db


def _key(name: str, unit: str) -> tuple[str, str]:
    return (" ".join(name.lower().split()), unit)


def aggregate(plan_data: dict) -> list[dict]:
    totals: dict[tuple[str, str], dict] = {}
    for day in plan_data["days"]:
        for meal in day["meals"]:
            for ing in meal["ingredients"]:
                k = _key(ing["name"], ing["unit"])
                item = totals.setdefault(k, {"name": k[0], "unit": ing["unit"], "quantity": 0.0,
                                             "category": ing["category"]})
                if ing["unit"] != "q.b.":
                    item["quantity"] += ing["quantity"] or 0
    return list(totals.values())


def rebuild(plan_id: int):
    """Ricalcola le voci derivate dal piano, mantenendo lo stato 'spuntato' e le voci manuali."""
    with db.tx() as c:
        plan = c.execute("SELECT data FROM plans WHERE id=?", (plan_id,)).fetchone()
        items = aggregate(json.loads(plan["data"]))
        checked = {
            _key(r["name"], r["unit"] or "")
            for r in c.execute("SELECT name, unit FROM shopping_items WHERE plan_id=? AND manual=0 AND checked=1",
                               (plan_id,))
        }
        c.execute("DELETE FROM shopping_items WHERE plan_id=? AND manual=0", (plan_id,))
        for it in items:
            c.execute(
                "INSERT INTO shopping_items (plan_id, name, quantity, unit, category, checked) VALUES (?,?,?,?,?,?)",
                (plan_id, it["name"], it["quantity"] or None, it["unit"], it["category"],
                 int(_key(it["name"], it["unit"]) in checked)),
            )


def add_manual(items: list[dict]):
    plan = db.active_plan()
    with db.tx() as c:
        for it in items:
            c.execute(
                "INSERT INTO shopping_items (plan_id, name, quantity, unit, category, manual) VALUES (?,?,?,?,?,1)",
                (plan["id"] if plan else None, it["name"], it.get("quantity") or None, it.get("unit") or "",
                 it.get("category") or "Altro"),
            )


def grouped(plan_id: int | None) -> dict[str, list[dict]]:
    with db.tx() as c:
        rows = c.execute(
            "SELECT * FROM shopping_items WHERE plan_id IS ? OR manual=1 "
            "ORDER BY checked, category, name",
            (plan_id,),
        ).fetchall()
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r["category"]].append(dict(r))
    # "Altro" in fondo, il resto in ordine alfabetico
    return dict(sorted(groups.items(), key=lambda kv: (kv[0] == "Altro", kv[0])))


def fmt_qty(item: dict) -> str:
    q, u = item["quantity"], item["unit"] or ""
    if u == "q.b.":
        return "q.b."
    if not q:
        return u
    if u in ("g", "ml") and q >= 1000:
        return f"{q / 1000:g} {'kg' if u == 'g' else 'l'}"
    return f"{q:g} {u}".strip()
