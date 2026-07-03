#!/usr/bin/env python3
"""Import scraped Premium Filters cross-reference/fitment data into heventi.

Reads filtros_por_vehiculo.csv (vehicle <-> reference fitment; the source of
truth for `vehicles` / `applications` / `application_vehicles`) and
equivalencias.csv (competitor brand/code cross-references; the source of
`filter_cross_references`). aplicaciones.csv is intentionally not read: once
the scraper finishes covering every brand, filtros_por_vehiculo.csv alone
covers vehicle fitment, and aplicaciones.csv has no year column so it cannot
feed `vehicles` (year is NOT NULL there).

Competitor equivalent codes are never turned into product_variants — they are
lookup-only rows in `filter_cross_references`. An Application's `description`
never mentions Premium Filters' own SKU or any brand name: it follows the same
"<category label> - <model> - <model> - <model>" convention the front-end
builds in app/applications/lib/buildApplicationDescription.ts, so applications
created here look identical to ones created by hand in the UI.

Safe to re-run: every write is an upsert (ON CONFLICT ... DO NOTHING/UPDATE)
keyed off uniqueness already enforced by heventi's schema (or the new
`applications.external_ref`), so running this again as the scraper produces
more brands/motorcycles will not create duplicates.

Usage:
    HEVENTI_DATABASE_URL=postgres://... python import_to_heventi.py
"""
import csv
import os
import sys
from collections import defaultdict

import psycopg2

FILTROS_CSV = "filtros_por_vehiculo.csv"
EQUIVALENCIAS_CSV = "equivalencias.csv"

# referencia prefix -> applications.category slug. Must already exist in
# heventi's `categories` table. Unknown prefixes are logged and skipped rather
# than failing the whole import: the scraper is still running and will reach
# brands/prefixes (fuel filters, motorcycles) not yet mapped here.
PREFIX_TO_CATEGORY = {
    "ACP": "filtro-aire-cabina",
    "AIP": "filtro-aire",
    "OLP": "filtro-aceite",
}

MAX_DESCRIPTION_MODELS = 3
# Corrupted rows from the scraper's detail-page parser (scraper.py `detalle()`
# picks up a wrapper <tr> whose text concatenates the whole nested table) show
# up as one implausibly long field per reference; real brand/code values are
# always short, so this threshold safely filters them out.
MAX_BRAND_LEN = 25
MAX_CODE_LEN = 30


def _log(msg):
    print(f"[import] {msg}", file=sys.stderr)


def prefix_of(referencia):
    return referencia.split("-", 1)[0]


def engine_to_liters(motor_cc):
    """'1600' (cc, from filtros_por_vehiculo.csv) -> '1.6', to match the
    liters convention already used in heventi's seeded vehicles.engine."""
    motor_cc = (motor_cc or "").strip()
    if not motor_cc:
        return None
    try:
        return f"{int(motor_cc) / 1000:.1f}"
    except ValueError:
        return None


def read_filtros_por_vehiculo(path):
    rows = []
    seen = set()
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            referencia = (row.get("referencia") or "").strip()
            make = (row.get("fabricante") or "").strip().upper()
            model = (row.get("modelo") or "").strip()
            year_raw = (row.get("anio") or "").strip()
            engine = engine_to_liters(row.get("motor"))
            if not referencia or not make or not model or not year_raw:
                continue
            try:
                year = int(year_raw)
            except ValueError:
                continue
            key = (referencia, make, model, year, engine)
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "referencia": referencia,
                "make": make,
                "model": model,
                "year": year,
                "engine": engine,
            })
    return rows


def read_equivalencias(path):
    rows = []
    seen = set()
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            referencia = (row.get("referencia") or "").strip()
            brand = (row.get("fabricante_equivalente") or "").strip().upper()
            code = (row.get("codigo_equivalente") or "").strip()
            if not referencia or not brand or not code:
                continue
            if len(brand) > MAX_BRAND_LEN or len(code) > MAX_CODE_LEN:
                _log(f"fila corrupta descartada para {referencia}: fabricante={brand[:40]!r}...")
                continue
            key = (referencia, brand, code)
            if key in seen:
                continue
            seen.add(key)
            rows.append({"referencia": referencia, "brand": brand, "code": code})
    return rows


def dedupe_preserve_order(values):
    seen = set()
    out = []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def build_description(category_label, vehicles_for_ref):
    models = dedupe_preserve_order(v["model"] for v in vehicles_for_ref)[:MAX_DESCRIPTION_MODELS]
    parts = [category_label] if category_label else []
    parts.extend(models)
    return " - ".join(parts)


def main():
    dsn = os.environ.get("HEVENTI_DATABASE_URL")
    if not dsn:
        _log("HEVENTI_DATABASE_URL no está definida. Apuntá primero al Postgres local de heventi (supabase start).")
        sys.exit(1)

    fitment_rows = read_filtros_por_vehiculo(FILTROS_CSV)
    equivalencia_rows = read_equivalencias(EQUIVALENCIAS_CSV)

    vehicles_by_ref = defaultdict(list)
    for row in fitment_rows:
        vehicles_by_ref[row["referencia"]].append(row)

    referencias = sorted(vehicles_by_ref)
    _log(f"{len(referencias)} referencias, {len(fitment_rows)} filas de ajuste, {len(equivalencia_rows)} filas de equivalencia")

    conn = psycopg2.connect(dsn)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT slug, name FROM categories")
                category_labels = dict(cur.fetchall())

                app_id_by_ref = {}
                skipped_prefixes = set()

                for referencia in referencias:
                    category = PREFIX_TO_CATEGORY.get(prefix_of(referencia))
                    if category is None:
                        skipped_prefixes.add(prefix_of(referencia))
                        continue

                    category_label = category_labels.get(category, category)
                    description = build_description(category_label, vehicles_by_ref[referencia])

                    cur.execute(
                        """
                        INSERT INTO applications (category, description, external_ref)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (external_ref) WHERE external_ref IS NOT NULL DO UPDATE
                          SET category = EXCLUDED.category,
                              description = EXCLUDED.description,
                              updated_at = now()
                        RETURNING id
                        """,
                        (category, description, referencia),
                    )
                    app_id_by_ref[referencia] = cur.fetchone()[0]

                if skipped_prefixes:
                    _log(f"prefijos sin categoria mapeada, referencias omitidas: {sorted(skipped_prefixes)}")

                vehicle_id_by_key = {}
                unique_vehicles = {
                    (row["make"], row["model"], row["year"], row["engine"])
                    for row in fitment_rows
                    if row["referencia"] in app_id_by_ref
                }
                for make, model, year, engine in sorted(unique_vehicles, key=lambda t: (t[0], t[1], t[2], t[3] or "")):
                    cur.execute(
                        """
                        INSERT INTO vehicles (make, model, year, engine)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (make, model, year, (COALESCE(engine, ''::text))) DO UPDATE
                          SET make = EXCLUDED.make
                        RETURNING id
                        """,
                        (make, model, year, engine),
                    )
                    vehicle_id_by_key[(make, model, year, engine)] = cur.fetchone()[0]

                app_vehicle_links = 0
                for row in fitment_rows:
                    app_id = app_id_by_ref.get(row["referencia"])
                    if app_id is None:
                        continue
                    vehicle_id = vehicle_id_by_key[(row["make"], row["model"], row["year"], row["engine"])]
                    cur.execute(
                        """
                        INSERT INTO application_vehicles (application_id, vehicle_id)
                        VALUES (%s, %s)
                        ON CONFLICT (application_id, vehicle_id) DO NOTHING
                        """,
                        (app_id, vehicle_id),
                    )
                    app_vehicle_links += 1

                variant_links = 0
                variant_conflicts = 0
                for referencia, app_id in app_id_by_ref.items():
                    cur.execute(
                        "SELECT id, application_id FROM product_variants WHERE sku = %s AND deleted_at IS NULL",
                        (referencia,),
                    )
                    variant = cur.fetchone()
                    if variant is None:
                        continue
                    variant_id, existing_app_id = variant
                    if existing_app_id is not None and existing_app_id != app_id:
                        variant_conflicts += 1
                        _log(f"variant {variant_id} (sku={referencia}) ya pertenece a application {existing_app_id}, no se reasigna")
                        continue
                    cur.execute(
                        "UPDATE product_variants SET application_id = %s, updated_at = now() WHERE id = %s",
                        (app_id, variant_id),
                    )
                    variant_links += 1

                cross_ref_rows = 0
                for row in equivalencia_rows:
                    app_id = app_id_by_ref.get(row["referencia"])
                    if app_id is None:
                        continue
                    cur.execute(
                        """
                        INSERT INTO filter_cross_references (application_id, brand, code)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (application_id, brand, code) DO NOTHING
                        """,
                        (app_id, row["brand"], row["code"]),
                    )
                    cross_ref_rows += 1

        _log(
            f"listo: {len(app_id_by_ref)} applications, {len(vehicle_id_by_key)} vehicles, "
            f"{app_vehicle_links} application_vehicles, {variant_links} variantes vinculadas "
            f"({variant_conflicts} en conflicto), {cross_ref_rows} filter_cross_references"
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
