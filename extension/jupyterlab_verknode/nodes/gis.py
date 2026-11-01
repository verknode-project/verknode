"""GIS nodes, in plain Python (polars, numpy): GeoJSON in and out, distances, filters by radius, box or
polygon, points in polygons, nearest points.

Points are latitude / longitude columns (WGS 84, degrees); other shapes are GeoJSON geometries as text
in a "geometry" column (GeoJSON Reader). Map Display shows both.
"""
from __future__ import annotations

import json
import math
import os

import numpy as np
import polars as pl

from verknode.node import F, InvalidSettingsError, NodeModel, Port, TABLE

CAT = "GIS"
EARTH_KM = 6371.0088
UNITS = {"km": 1.0, "m": 1000.0, "mi": 0.621371, "nmi": 0.539957}
LAT_NAMES = ("lat", "latitude", "y", "lat_deg")
LON_NAMES = ("lon", "lng", "long", "longitude", "x", "lon_deg")
NUMERIC = ["double", "integer", "long", "string"]


# ---------------------------------------------------------------------------------------------------
# geometry helpers
# ---------------------------------------------------------------------------------------------------
def guess(df: pl.DataFrame, names) -> str | None:
    lower = {c.lower(): c for c in df.columns}
    return next((lower[n] for n in names if n in lower), None)


def lat_lon(df: pl.DataFrame, s: dict, lat_key="lat", lon_key="lon", which="") -> tuple[np.ndarray, np.ndarray]:
    """The latitude and longitude columns (the settings', else lat / lon, latitude / longitude ...)."""
    lat, lon = s.get(lat_key) or guess(df, LAT_NAMES), s.get(lon_key) or guess(df, LON_NAMES)
    if not lat or not lon:
        raise InvalidSettingsError(f"Pick the latitude and longitude columns{which}")
    for c in (lat, lon):
        if c not in df.columns:
            raise InvalidSettingsError(f"There is no column {c!r}{which}")
    cast = lambda c: df[c].cast(pl.Float64, strict=False).to_numpy().astype(float)  # noqa: E731
    return cast(lat), cast(lon)


def haversine(lat1, lon1, lat2, lon2) -> np.ndarray:
    """Great-circle distances in km (arrays or numbers)."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(np.radians(np.subtract(lon2, lon1)) / 2) ** 2
    return 2 * EARTH_KM * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def parse_geometry(value) -> dict | None:
    """A GeoJSON geometry (text or dict; a Feature: its geometry)."""
    if value is None or value == "":
        return None
    g = json.loads(value) if isinstance(value, str) else value
    if isinstance(g, dict) and g.get("type") == "Feature":
        g = g.get("geometry")
    return g if isinstance(g, dict) and g.get("type") else None


def parse_polygon(text: str) -> dict:
    """A polygon from GeoJSON (Polygon, MultiPolygon, Feature, FeatureCollection) or "lat, lon" lines."""
    text = (text or "").strip()
    if not text:
        raise InvalidSettingsError("Give the polygon (GeoJSON, or one 'lat, lon' per line)")
    if text.startswith("{"):
        g = json.loads(text)
        if g.get("type") == "FeatureCollection":
            polys = [parse_geometry(f) for f in g.get("features", [])]
            coords = [p["coordinates"] if p["type"] == "MultiPolygon" else [p["coordinates"]]
                      for p in polys if p and p["type"] in ("Polygon", "MultiPolygon")]
            return {"type": "MultiPolygon", "coordinates": [c for cs in coords for c in cs]}
        g = parse_geometry(g)
        if not g or g["type"] not in ("Polygon", "MultiPolygon"):
            raise InvalidSettingsError("The GeoJSON is not a polygon")
        return g
    ring = []
    for line in text.replace(";", "\n").splitlines():
        if line.strip():
            try:
                lat, lon = [float(x) for x in line.replace(",", " ").split()[:2]]
            except ValueError:
                raise InvalidSettingsError(f"Not 'lat, lon': {line!r}") from None
            ring.append([lon, lat])
    if len(ring) < 3:
        raise InvalidSettingsError("A polygon needs at least 3 points")
    return {"type": "Polygon", "coordinates": [ring + [ring[0]]]}


def _polygons(g: dict) -> list:
    return [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"] if g["type"] == "MultiPolygon" else []


def _in_ring(lon: np.ndarray, lat: np.ndarray, ring) -> np.ndarray:
    """Ray casting: the points (arrays) inside a ring of [lon, lat]."""
    r = np.asarray(ring, dtype=float)
    inside = np.zeros(lon.shape, dtype=bool)
    xs, ys = r[:, 0], r[:, 1]
    for i in range(len(r) - 1):
        x1, y1, x2, y2 = xs[i], ys[i], xs[i + 1], ys[i + 1]
        crosses = (y1 > lat) != (y2 > lat)
        if crosses.any():
            with np.errstate(divide="ignore", invalid="ignore"):
                xc = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            inside ^= crosses & (lon < xc)
    return inside


def contains(g: dict, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """The points inside a (multi)polygon - holes left out."""
    out = np.zeros(lat.shape, dtype=bool)
    for poly in _polygons(g):
        outer = np.asarray(poly[0], dtype=float)
        box = (lon >= outer[:, 0].min()) & (lon <= outer[:, 0].max()) & (lat >= outer[:, 1].min()) & (lat <= outer[:, 1].max())
        if not box.any():
            continue
        idx = np.nonzero(box)[0]
        hit = _in_ring(lon[idx], lat[idx], poly[0])
        for hole in poly[1:]:
            hit &= ~_in_ring(lon[idx], lat[idx], hole)
        out[idx[hit]] = True
    return out


def _coords(g) -> list:
    """All [lon, lat] positions of a geometry."""
    t, c = g.get("type"), g.get("coordinates")
    if t == "Point":
        return [c]
    if t in ("MultiPoint", "LineString"):
        return list(c)
    if t in ("MultiLineString", "Polygon"):
        return [p for part in c for p in part]
    if t == "MultiPolygon":
        return [p for poly in c for ring in poly for p in ring]
    if t == "GeometryCollection":
        return [p for sub in g.get("geometries", []) for p in _coords(sub)]
    return []


def centroid(g: dict) -> tuple[float | None, float | None]:
    """(lat, lon) of a geometry's centre: a polygon's area centroid (its largest part), else the mean."""
    if not g:
        return None, None
    if g["type"] == "Point":
        return float(g["coordinates"][1]), float(g["coordinates"][0])
    best, best_area = None, -1.0
    for poly in _polygons(g):
        r = np.asarray(poly[0], dtype=float)
        x, y = r[:, 0], r[:, 1]
        cross = x[:-1] * y[1:] - x[1:] * y[:-1]
        area = cross.sum() / 2
        if abs(area) > best_area and area:
            best_area = abs(area)
            best = ((((y[:-1] + y[1:]) * cross).sum() / (6 * area)), (((x[:-1] + x[1:]) * cross).sum() / (6 * area)))
    if best:
        return float(best[0]), float(best[1])
    pts = _coords(g)
    if not pts:
        return None, None
    a = np.asarray(pts, dtype=float)
    return float(a[:, 1].mean()), float(a[:, 0].mean())


def bounds(g: dict) -> tuple | None:
    a = np.asarray(_coords(g), dtype=float)
    return None if not len(a) else (a[:, 1].min(), a[:, 0].min(), a[:, 1].max(), a[:, 0].max())


def _unique(name: str, taken) -> str:
    out = name
    while out in taken:
        out += "_2"
    return out


def _attach(left: pl.DataFrame, right: pl.DataFrame, idx: np.ndarray, drop=()) -> pl.DataFrame:
    """Left with the right's columns of the rows idx (-1: none; clashing names get _2)."""
    right = right.drop([c for c in drop if c in right.columns])
    taken = set(left.columns)
    right = right.rename({c: _unique(c, taken) for c in right.columns if c in taken})
    none = pl.Series(idx < 0)
    if right.height:
        picked = right[np.where(idx < 0, 0, idx)]
        picked = picked.with_columns([pl.when(none).then(None).otherwise(pl.col(c)).alias(c) for c in picked.columns])
    else:
        picked = pl.DataFrame({c: pl.Series(c, [None] * len(idx), dtype=t) for c, t in right.schema.items()})
    return pl.concat([left, picked.select(right.columns)], how="horizontal")


def lat_field(port=1, key="lat", label="Latitude column"):
    return F(key, "column", label, None, "Empty: lat / latitude.", port=port, kinds=NUMERIC)


def lon_field(port=1, key="lon", label="Longitude column"):
    return F(key, "column", label, None, "Empty: lon / lng / longitude.", port=port, kinds=NUMERIC)


# ---------------------------------------------------------------------------------------------------
# GeoJSON
# ---------------------------------------------------------------------------------------------------
class GeoJSONReader(NodeModel):
    factory = "verknode.gis.GeoJSONReader"
    name = "GeoJSON Reader"
    category = CAT
    node_type = "Source"
    icon = "GeoJ"
    short_description = "Reads GeoJSON features (a file or URL) into a table."
    description = (
        "One row per feature: its properties as columns, geometry_type, the geometry as GeoJSON text and lat / lon "
        "of its centre (a polygon's centroid) - for Map Display, Point in Polygon, Geo Distance. Reads a file, or "
        "a URL (http / https)."
    )
    out_ports = [Port(TABLE, "Features")]
    fields = [F("path", "string", "File or URL", "", "A .geojson / .json file or an http(s) URL.", path=True, extensions=[".geojson", ".json"])]

    def _load(self, s, ctx):
        p = (s.get("path") or "").strip()
        if p.startswith(("http://", "https://")):
            from ._http import request

            return request("GET", p, timeout=60, ctx=ctx).json()
        p = ctx.resolve_path(p)
        if not os.path.isfile(p):
            raise InvalidSettingsError(f"File does not exist: {p}")
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def configure(self, in_specs, s, ctx):
        p = (s.get("path") or "").strip()
        if not p:
            raise InvalidSettingsError("No file path specified")
        if p.startswith(("http://", "https://")):
            return [None]
        return [self.read(self._load(s, ctx)).schema]

    def read(self, g) -> pl.DataFrame:
        feats = g.get("features", []) if g.get("type") == "FeatureCollection" else \
            [g] if g.get("type") == "Feature" else [{"type": "Feature", "properties": {}, "geometry": g}]
        rows = []
        for f in feats:
            geom = f.get("geometry")
            lat, lon = centroid(geom) if geom else (None, None)
            props = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in (f.get("properties") or {}).items()}
            if f.get("id") is not None and "id" not in props:
                props = {"id": str(f["id"]), **props}
            rows.append({**props, "geometry_type": (geom or {}).get("type"), "lat": lat, "lon": lon,
                         "geometry": json.dumps(geom) if geom else None})
        if not rows:
            return pl.DataFrame(schema={"geometry_type": pl.String, "lat": pl.Float64, "lon": pl.Float64, "geometry": pl.String})
        return pl.DataFrame(rows, infer_schema_length=None, strict=False)

    def execute(self, inputs, s, ctx):
        df = self.read(self._load(s, ctx))
        ctx.log(f"Read {df.height} features")
        return [df]


class GeoJSONWriter(NodeModel):
    factory = "verknode.gis.GeoJSONWriter"
    name = "GeoJSON Writer"
    category = CAT
    node_type = "Sink"
    icon = "GeoJ"
    short_description = "Writes a table as a GeoJSON FeatureCollection."
    description = (
        "Each row becomes a feature: its geometry from the geometry column (GeoJSON text), else a point from the "
        "latitude / longitude columns; the other columns are its properties."
    )
    in_ports = [Port(TABLE, "Table to write")]
    out_ports = []
    fields = [
        F("path", "string", "File", "features.geojson", path="save", extensions=[".geojson", ".json"]),
        F("geometry", "column", "Geometry column", None, "GeoJSON text. Empty: geometry, else the point columns.",
          port=1, kinds=["string"]),
        lat_field(), lon_field(),
    ]

    def configure(self, in_specs, s, ctx):
        ctx.resolve_path(s.get("path"))
        return []

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        gcol = s.get("geometry") or ("geometry" if "geometry" in df.columns else None)
        if gcol:
            geoms = [parse_geometry(v) for v in df[gcol].to_list()]
            drop = {gcol}
        else:
            lat, lon = lat_lon(df, s)
            geoms = [None if math.isnan(a) or math.isnan(o) else {"type": "Point", "coordinates": [o, a]} for a, o in zip(lat, lon)]
            drop = {s.get("lat") or guess(df, LAT_NAMES), s.get("lon") or guess(df, LON_NAMES)}
        props = df.drop([c for c in drop if c in df.columns])
        props = props.with_columns([pl.col(c).cast(pl.String) for c, t in props.schema.items()
                                    if t.is_temporal() or isinstance(t, (pl.Decimal, pl.Object))])
        feats = [{"type": "Feature", "properties": p, "geometry": g} for p, g in zip(props.iter_rows(named=True), geoms)]
        p = ctx.resolve_path(s.get("path"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"type": "FeatureCollection", "features": feats}, f, default=str)
        ctx.log(f"Wrote {len(feats)} features to {p}")
        return []


# ---------------------------------------------------------------------------------------------------
# distances and filters
# ---------------------------------------------------------------------------------------------------
class GeoDistance(NodeModel):
    factory = "verknode.gis.GeoDistance"
    name = "Geo Distance"
    category = CAT
    icon = "↔"
    short_description = "Great-circle distance of each row to a point, or between two coordinate pairs."
    description = (
        "To a point: the distance of each row's latitude / longitude to a fixed point. Between columns: between "
        "two pairs of coordinates of the same row. The haversine formula (a sphere: about 0.5% off at most). "
        "Also the initial bearing (degrees from north), if wanted."
    )
    in_ports = [Port(TABLE, "Table")]
    out_ports = [Port(TABLE, "With distance")]
    fields = [
        F("mode", "enum", "Distance", "To a point", options=["To a point", "Between columns"]),
        lat_field(), lon_field(),
        F("to_lat", "float", "Point: latitude", 51.5074), F("to_lon", "float", "Point: longitude", -0.1278),
        lat_field(key="lat2", label="Second latitude column"), lon_field(key="lon2", label="Second longitude column"),
        F("unit", "enum", "Unit", "km", options=list(UNITS)),
        F("column", "string", "Distance column", "distance_km"),
        F("bearing", "bool", "Add the bearing", False),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        lat, lon = lat_lon(df, s)
        if (s.get("mode") or "To a point") == "To a point":
            lat2, lon2 = np.full(lat.shape, float(s.get("to_lat") or 0)), np.full(lat.shape, float(s.get("to_lon") or 0))
        else:
            if not s.get("lat2") or not s.get("lon2"):
                raise InvalidSettingsError("Pick the second latitude and longitude columns")
            lat2, lon2 = lat_lon(df, s, "lat2", "lon2", " (second)")
        d = haversine(lat, lon, lat2, lon2) * UNITS[s.get("unit") or "km"]
        out = df.with_columns(pl.Series(s.get("column") or "distance", d).fill_nan(None))
        if s.get("bearing"):
            p1, p2, dl = np.radians(lat), np.radians(lat2), np.radians(lon2 - lon)
            b = (np.degrees(np.arctan2(np.sin(dl) * np.cos(p2), np.cos(p1) * np.sin(p2) - np.sin(p1) * np.cos(p2) * np.cos(dl))) + 360) % 360
            out = out.with_columns(pl.Series("bearing_deg", b).fill_nan(None))
        return [out]


class GeoFilter(NodeModel):
    factory = "verknode.gis.GeoFilter"
    name = "Geo Filter"
    category = CAT
    icon = "◎"
    short_description = "Rows within a radius of a point, in a bounding box, or inside a polygon."
    description = (
        "Keeps the rows whose latitude / longitude lie within a radius (km) of a point, inside a bounding box "
        "(south, west, north, east), or inside a polygon - GeoJSON (Polygon, MultiPolygon, a Feature or "
        "FeatureCollection) or one 'lat, lon' per line. The second output has the rest."
    )
    in_ports = [Port(TABLE, "Table")]
    out_ports = [Port(TABLE, "Inside"), Port(TABLE, "Outside")]
    fields = [
        F("mode", "enum", "Keep rows", "Within a radius", options=["Within a radius", "In a bounding box", "Inside a polygon"]),
        lat_field(), lon_field(),
        F("center_lat", "float", "Centre latitude", 51.5074), F("center_lon", "float", "Centre longitude", -0.1278),
        F("radius_km", "float", "Radius (km)", 10.0),
        F("bbox", "string", "Bounding box", "51.28, -0.51, 51.69, 0.33", "south, west, north, east"),
        F("polygon", "code", "Polygon", "", "GeoJSON, or 'lat, lon' per line."),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        lat, lon = lat_lon(df, s)
        mode = s.get("mode") or "Within a radius"
        if mode == "Within a radius":
            keep = haversine(lat, lon, float(s.get("center_lat") or 0), float(s.get("center_lon") or 0)) <= float(s.get("radius_km") or 0)
        elif mode == "In a bounding box":
            try:
                south, west, north, east = [float(x) for x in str(s.get("bbox") or "").replace(";", ",").split(",")]
            except ValueError:
                raise InvalidSettingsError("The bounding box is: south, west, north, east") from None
            inlon = (lon >= west) & (lon <= east) if west <= east else (lon >= west) | (lon <= east)  # (across 180°)
            keep = (lat >= south) & (lat <= north) & inlon
        else:
            keep = contains(parse_polygon(s.get("polygon")), lat, lon)
        mask = pl.Series(keep, dtype=pl.Boolean)
        return [df.filter(mask), df.filter(~mask)]


# ---------------------------------------------------------------------------------------------------
# joins
# ---------------------------------------------------------------------------------------------------
class PointInPolygon(NodeModel):
    factory = "verknode.gis.PointInPolygon"
    name = "Point in Polygon"
    category = CAT
    icon = "⬠"
    short_description = "Spatial join: each point gets the columns of the polygon it lies in."
    description = (
        "The points (latitude / longitude of the first table) get the columns of the polygon of the second table "
        "(a GeoJSON geometry column, e.g. of GeoJSON Reader) that contains them - the first one, if they overlap. "
        "Points in no polygon keep empty columns, or are left out. The second output counts the points per "
        "polygon."
    )
    in_ports = [Port(TABLE, "Points"), Port(TABLE, "Polygons")]
    out_ports = [Port(TABLE, "Points with polygon"), Port(TABLE, "Polygons with counts")]
    fields = [
        lat_field(), lon_field(),
        F("geometry", "column", "Polygon geometry column", None, "GeoJSON text. Empty: geometry.", port=2, kinds=["string"]),
        F("keep_outside", "bool", "Keep points in no polygon", True),
    ]

    def execute(self, inputs, s, ctx):
        pts, polys = inputs
        lat, lon = lat_lon(pts, s)
        gcol = s.get("geometry") or "geometry"
        if gcol not in polys.columns:
            raise InvalidSettingsError(f"The polygons have no column {gcol!r}")
        idx = np.full(lat.shape, -1, dtype=np.int64)
        counts = []
        for k, v in enumerate(polys[gcol].to_list()):
            ctx.check_canceled()
            g = parse_geometry(v)
            hit = contains(g, lat, lon) if g else np.zeros(lat.shape, dtype=bool)
            counts.append(int(hit.sum()))
            idx[(idx < 0) & hit] = k
        out = _attach(pts, polys, idx, drop=(gcol, "lat", "lon", "geometry_type"))
        if not s.get("keep_outside", True):
            out = out.filter(pl.Series(idx >= 0))
        return [out, polys.with_columns(pl.Series("point_count", counts, dtype=pl.Int64))]


class NearestPoint(NodeModel):
    factory = "verknode.gis.NearestPoint"
    name = "Nearest Point"
    category = CAT
    icon = "⇢"
    short_description = "For each point of the first table, the nearest point(s) of the second, with distance."
    description = (
        "Each row of the first table gets the columns of the nearest row of the second table (by great-circle "
        "distance) and the distance in km - or the k nearest, one row each with their rank. Within a maximum "
        "distance, if one is given (0: any)."
    )
    in_ports = [Port(TABLE, "Points"), Port(TABLE, "Candidates")]
    out_ports = [Port(TABLE, "With nearest")]
    fields = [
        lat_field(), lon_field(),
        lat_field(port=2, key="lat2", label="Candidate latitude column"),
        lon_field(port=2, key="lon2", label="Candidate longitude column"),
        F("k", "int", "Nearest how many", 1),
        F("max_km", "float", "Maximum distance (km)", 0.0, "0: any."),
    ]

    def execute(self, inputs, s, ctx):
        a, b = inputs
        lat, lon = lat_lon(a, s)
        lat2, lon2 = lat_lon(b, s, "lat2", "lon2", " (candidates)")
        k, max_km = max(1, int(s.get("k") or 1)), float(s.get("max_km") or 0)
        left_rows, right_rows, dists, ranks = [], [], [], []
        for i in range(len(lat)):
            if i % 500 == 0:
                ctx.check_canceled()
            d = haversine(lat[i], lon[i], lat2, lon2)
            d = np.where(np.isnan(d), np.inf, d)
            order = np.argsort(d)[:k] if len(d) else []
            found = [j for j in order if np.isfinite(d[j]) and (not max_km or d[j] <= max_km)]
            for r, j in enumerate(found or [-1]):
                left_rows.append(i)
                right_rows.append(int(j))
                dists.append(float(d[j]) if j >= 0 else None)
                ranks.append(r + 1 if j >= 0 else None)
        left = a[left_rows] if left_rows else a.clear()
        out = _attach(left, b, np.asarray(right_rows, dtype=np.int64), drop=("geometry",))
        cols = [pl.Series(_unique("distance_km", out.columns), dists, dtype=pl.Float64)]
        if k > 1:
            cols.append(pl.Series(_unique("rank", out.columns), ranks, dtype=pl.Int64))
        return [out.with_columns(cols)]
