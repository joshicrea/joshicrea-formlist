"""Normalize locally extracted Overture Places rows for formlist.py.

Runtime: Windows / Python 3.9+. This module intentionally does not download
the world Parquet files. The automatic regional extraction step needs a Parquet
reader such as DuckDB and should be enabled only after dependency approval.
"""
import csv
import json
from pathlib import Path


KOTO_BBOX = (139.774, 35.632, 139.849, 35.701)  # lon_min, lat_min, lon_max, lat_max
BEAUTY_TERMS = (
    "beauty", "salon", "hair", "nail", "esthetic", "spa", "massage",
    "barber", "eyelash", "eyebrow", "skin_care", "cosmetic",
)


def _first(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        for item in value:
            got = _first(item)
            if got:
                return got
        return ""
    if isinstance(value, dict):
        for key in ("url", "value", "primary", "freeform", "number"):
            got = _first(value.get(key))
            if got:
                return got
    return str(value)


def _load_jsonish(value):
    if not isinstance(value, str):
        return value
    s = value.strip()
    if not s or s[0] not in "[{":
        return value
    try:
        return json.loads(s)
    except ValueError:
        return value


def _taxonomy_values(props):
    out = []
    taxonomy = _load_jsonish(props.get("taxonomy"))
    basic = props.get("basic_category")
    if basic:
        out.append(str(basic))
    if isinstance(taxonomy, dict):
        for key in ("primary", "hierarchy", "alternates"):
            value = taxonomy.get(key)
            if isinstance(value, list):
                out.extend(str(v) for v in value)
            elif value:
                out.append(str(value))
    elif taxonomy:
        out.append(str(taxonomy))
    for legacy in ("category", "categories", "primary_category"):
        value = _load_jsonish(props.get(legacy))
        if isinstance(value, dict):
            out.extend(str(v) for v in value.values() if v)
        elif isinstance(value, list):
            out.extend(str(v) for v in value if v)
        elif value:
            out.append(str(value))
    return out


def is_beauty_place(props):
    text = " ".join(_taxonomy_values(props)).lower()
    return any(term in text for term in BEAUTY_TERMS)


def _coords(feature):
    props = feature.get("properties", feature)
    geom = feature.get("geometry") or {}
    if geom.get("type") == "Point" and isinstance(geom.get("coordinates"), list):
        lon, lat = geom["coordinates"][:2]
        return float(lon), float(lat)
    for lon_key, lat_key in (("lon", "lat"), ("longitude", "latitude"), ("x", "y")):
        if props.get(lon_key) not in (None, "") and props.get(lat_key) not in (None, ""):
            return float(props[lon_key]), float(props[lat_key])
    bbox = props.get("bbox") or feature.get("bbox")
    if isinstance(bbox, dict) and bbox.get("xmin") is not None and bbox.get("ymin") is not None:
        return float(bbox["xmin"]), float(bbox["ymin"])
    if isinstance(bbox, list) and len(bbox) >= 2:
        return float(bbox[0]), float(bbox[1])
    return None, None


def _inside_bbox(lon, lat, bbox):
    x1, y1, x2, y2 = bbox
    return lon is not None and lat is not None and x1 <= lon <= x2 and y1 <= lat <= y2


def _point_in_ring(lon, lat, ring):
    inside = False
    j = len(ring) - 1
    for i, point in enumerate(ring):
        xi, yi = point[:2]
        xj, yj = ring[j][:2]
        crosses = ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi)
        if crosses:
            inside = not inside
        j = i
    return inside


def _load_boundary(path):
    if not path:
        return []
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    geom = data.get("geometry") if data.get("type") == "Feature" else data
    if data.get("type") == "FeatureCollection":
        geom = data["features"][0]["geometry"]
    if geom.get("type") == "Polygon":
        return [geom["coordinates"][0]]
    if geom.get("type") == "MultiPolygon":
        return [poly[0] for poly in geom["coordinates"]]
    return []


def _inside_boundary(lon, lat, rings):
    if not rings:
        return True
    return any(_point_in_ring(lon, lat, ring) for ring in rings)


def normalize_feature(feature, area="東京都江東区", release="", license_text="", boundary_rings=None, bbox=KOTO_BBOX):
    props = feature.get("properties", feature)
    lon, lat = _coords(feature)
    addresses = _load_jsonish(props.get("addresses") or props.get("address"))
    addr = _first(addresses)
    locality = ""
    if isinstance(addresses, list) and addresses and isinstance(addresses[0], dict):
        locality = str(addresses[0].get("locality") or "")
    elif isinstance(addresses, dict):
        locality = str(addresses.get("locality") or "")

    area_text = f"{addr} {locality}".lower()
    in_named_area = "江東" in area_text or "koto" in area_text
    in_geo_area = _inside_bbox(lon, lat, bbox) and _inside_boundary(lon, lat, boundary_rings or [])
    if not (in_named_area or in_geo_area):
        return None
    if not is_beauty_place(props):
        return None

    name = _first(_load_jsonish(props.get("names")) or props.get("name"))
    if not name:
        return None
    websites = _load_jsonish(props.get("websites") or props.get("website"))
    phones = _load_jsonish(props.get("phones") or props.get("phone"))
    emails = _load_jsonish(props.get("emails") or props.get("email"))
    source = "Overture Places"
    if release:
        source += f" {release}"
    return {
        "name": name,
        "industry": "サロン",
        "area": area,
        "url": _first(websites),
        "source": source,
        "source_key": "overture",
        "source_detail": f"license={license_text}; release={release}".strip("; "),
        "osm_ref": str(props.get("id") or feature.get("id") or ""),
        "tel": _first(phones),
        "address": addr,
        "email": _first(emails),
        "overture_license": license_text,
        "overture_release": release,
    }


def _iter_records(path):
    p = Path(path)
    if p.suffix.lower() == ".csv":
        with p.open("r", encoding="utf-8-sig", newline="") as f:
            yield from csv.DictReader(f)
        return
    text = p.read_text(encoding="utf-8-sig")
    if p.suffix.lower() in (".jsonl", ".ndjson"):
        for line in text.splitlines():
            if line.strip():
                yield json.loads(line)
        return
    data = json.loads(text)
    if data.get("type") == "FeatureCollection":
        yield from data.get("features", [])
    elif isinstance(data, list):
        yield from data
    else:
        yield data


def fetch_file(path, area="東京都江東区", release="", license_text="", boundary_geojson=None, log=None):
    rings = _load_boundary(boundary_geojson)
    rows, seen, total = [], set(), 0
    for feature in _iter_records(path):
        total += 1
        row = normalize_feature(feature, area=area, release=release, license_text=license_text, boundary_rings=rings)
        if not row:
            continue
        key = row["osm_ref"] or (row["name"], row.get("address", ""), row.get("url", ""))
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    if log:
        log(f"  [overture] local rows={total}, matched beauty/Koto candidates={len(rows)}")
    return rows
