#!/usr/bin/env python3
"""
Municipality Finder
===================

Given a geographic location (latitude/longitude), a street address, or both,
determine the U.S. municipality (city / village / town) that the location
belongs to.

If a *second* municipality lies within a 3-mile radius of the location, both
municipalities are reported, each with a confidence rating indicating which one
the location most likely belongs to. (The location always falls inside exactly
one municipal boundary, so that one gets the higher confidence -- the neighbor
is reported because slight errors in the address/coordinates could place it
just across the line.)

Data source
-----------
The authoritative geography comes from the free U.S. Census Bureau Geocoder
(no API key required). In Wisconsin -- and most of the U.S. -- the
"County Subdivisions" layer contains cities, villages, and towns as Minor Civil
Divisions, which is exactly the notion of "municipality" we want.

The optional confidence reasoning uses the Anthropic Claude API when the
ANTHROPIC_API_KEY environment variable is set; otherwise a deterministic
distance-based heuristic is used.

Usage
-----
    python3 municipality_finder.py --address "215 N Main St, Oshkosh, WI"
    python3 municipality_finder.py --lat 43.0731 --lon -89.4012
    python3 municipality_finder.py --coords "43.0731,-89.4012"
    python3 municipality_finder.py --address "..." --lat 43.07 --lon -89.40
    python3 municipality_finder.py --lat 43.07 --lon -89.40 --json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

CENSUS_BASE = "https://geocoding.geo.census.gov/geocoder"
BENCHMARK = "Public_AR_Current"
VINTAGE = "Current_Current"

# This tool operates in Wisconsin (Cru Concrete). If an address doesn't name a
# state, we assume Wisconsin so the geocoder doesn't wander off to a same-named
# street in another state (e.g. "821 W Johnson St" -> Ray City, GA). Override
# with the DEFAULT_STATE env var, or set it to "" to disable the assumption.
DEFAULT_STATE = os.environ.get("DEFAULT_STATE", "WI")

# US state names + abbreviations, used to detect whether an address already
# names a state before we append the default.
_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}
_US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
}

# The Census layers that describe a "municipality". County Subdivisions holds
# WI cities/villages/towns (MCDs); Incorporated Places is used as a fallback.
MUNICIPALITY_LAYERS = ("County Subdivisions", "Incorporated Places")

# Neighbor scan: sample points arranged on rings around the location. Any
# distinct municipality found on a sample within RADIUS_MILES counts as a
# neighbor whose territory is within that radius.
RADIUS_MILES = 3.0
RING_RADII_MILES = (1.0, 2.0, 3.0)   # rings to sample
BEARINGS = 12                        # sample points per ring (every 30 deg)

HTTP_TIMEOUT = 30
HTTP_RETRIES = 3
EARTH_RADIUS_MILES = 3958.7613

CLAUDE_MODEL = "claude-sonnet-5"

# The Claude API key is read from the ANTHROPIC_API_KEY environment variable.
# NEVER hardcode it here -- committing a key to git will get it auto-revoked and
# leaked. Locally: `export ANTHROPIC_API_KEY=...`. On Render: add it as an
# Environment Variable in the dashboard. If unset, the tool still works and
# falls back to the deterministic confidence heuristic.


# --------------------------------------------------------------------------- #
# Small geo helpers
# --------------------------------------------------------------------------- #

def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(min(1.0, math.sqrt(a)))


def destination_point(lat: float, lon: float, bearing_deg: float,
                      distance_miles: float) -> tuple[float, float]:
    """Point reached by travelling `distance_miles` on `bearing_deg` from start."""
    ang = distance_miles / EARTH_RADIUS_MILES
    brg = math.radians(bearing_deg)
    p1 = math.radians(lat)
    l1 = math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(ang) +
                   math.cos(p1) * math.sin(ang) * math.cos(brg))
    l2 = l1 + math.atan2(math.sin(brg) * math.sin(ang) * math.cos(p1),
                         math.cos(ang) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


# --------------------------------------------------------------------------- #
# Census geocoder client
# --------------------------------------------------------------------------- #

class CensusError(RuntimeError):
    pass


def _get_json(url: str) -> dict:
    last_err: Exception | None = None
    for attempt in range(HTTP_RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "municipality-finder/1.0"})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return json.load(resp)
        except Exception as exc:  # noqa: BLE001 - network is flaky; retry
            last_err = exc
            time.sleep(0.6 * (attempt + 1))
    raise CensusError(f"Census request failed after {HTTP_RETRIES} tries: {last_err}")


def _address_has_state(address: str) -> bool:
    """True if the address text already names a US state (abbrev or full name)."""
    lowered = address.lower()
    if any(name in lowered for name in _US_STATE_NAMES):
        return True
    # Tokenize on commas/spaces and look for a 2-letter state abbreviation.
    tokens = [t.strip(" ,.").upper() for t in address.replace(",", " ").split()]
    return any(t in _US_STATES for t in tokens)


def geocode_address(address: str) -> dict | None:
    """Forward-geocode a one-line address -> {lat, lon, matched_address}."""
    query = address
    assumed_state = None
    if DEFAULT_STATE and not _address_has_state(address):
        query = f"{address}, {DEFAULT_STATE}"
        assumed_state = DEFAULT_STATE
    params = urllib.parse.urlencode({
        "address": query,
        "benchmark": BENCHMARK,
        "format": "json",
    })
    data = _get_json(f"{CENSUS_BASE}/locations/onelineaddress?{params}")
    matches = data.get("result", {}).get("addressMatches", [])
    if not matches:
        return None
    best = matches[0]
    coords = best["coordinates"]
    return {
        "lat": float(coords["y"]),
        "lon": float(coords["x"]),
        "matched_address": best.get("matchedAddress", query),
        "num_matches": len(matches),
        "assumed_state": assumed_state,
    }


def reverse_geographies(lat: float, lon: float) -> dict:
    """Return the Census `geographies` dict for a coordinate."""
    params = urllib.parse.urlencode({
        "x": lon,
        "y": lat,
        "benchmark": BENCHMARK,
        "vintage": VINTAGE,
        "format": "json",
        "layers": "all",
    })
    data = _get_json(f"{CENSUS_BASE}/geographies/coordinates?{params}")
    return data.get("result", {}).get("geographies", {})


# --------------------------------------------------------------------------- #
# Municipality extraction
# --------------------------------------------------------------------------- #

_TYPE_SUFFIXES = ("city", "village", "town", "borough", "township")


def _classify(name: str) -> tuple[str, str]:
    """Split a Census NAME like 'Madison city' -> ('Madison', 'city')."""
    lowered = name.strip()
    for suffix in _TYPE_SUFFIXES:
        if lowered.lower().endswith(" " + suffix):
            return lowered[: -(len(suffix) + 1)].strip(), suffix
    return lowered, "municipality"


def extract_municipality(geographies: dict) -> dict | None:
    """Pull the containing municipality from a geographies dict."""
    for layer in MUNICIPALITY_LAYERS:
        entries = geographies.get(layer) or []
        if entries:
            raw_name = entries[0].get("NAME") or entries[0].get("BASENAME", "")
            if not raw_name:
                continue
            base, kind = _classify(raw_name)
            county = None
            counties = geographies.get("Counties") or []
            if counties:
                county = counties[0].get("NAME")
            state = None
            states = geographies.get("States") or []
            if states:
                state = states[0].get("STUSAB") or states[0].get("NAME")
            return {
                "name": base,
                "type": kind,
                "full_name": raw_name,
                "layer": layer,
                "county": county,
                "state": state,
            }
    return None


# --------------------------------------------------------------------------- #
# Neighbor scan (3-mile radius)
# --------------------------------------------------------------------------- #

def find_neighbors(lat: float, lon: float, primary_name: str) -> list[dict]:
    """
    Sample rings around the point and collect distinct municipalities other than
    the primary one. The approximate distance to a neighbor is the distance to
    the nearest sample point that fell inside it (an upper bound on the true
    distance to that municipality's boundary).
    """
    found: dict[str, dict] = {}
    for radius in RING_RADII_MILES:
        for i in range(BEARINGS):
            bearing = (360.0 / BEARINGS) * i
            slat, slon = destination_point(lat, lon, bearing, radius)
            try:
                geos = reverse_geographies(slat, slon)
            except CensusError:
                continue
            muni = extract_municipality(geos)
            if not muni or muni["name"] == primary_name:
                continue
            dist = haversine_miles(lat, lon, slat, slon)
            key = muni["full_name"]
            if key not in found or dist < found[key]["approx_miles"]:
                found[key] = {
                    "name": muni["name"],
                    "type": muni["type"],
                    "full_name": muni["full_name"],
                    "county": muni["county"],
                    "state": muni["state"],
                    "approx_miles": round(dist, 2),
                }
    return sorted(found.values(), key=lambda m: m["approx_miles"])


# --------------------------------------------------------------------------- #
# Confidence rating
# --------------------------------------------------------------------------- #

def heuristic_confidence(primary: dict, neighbors: list[dict]) -> dict:
    """
    Deterministic fallback. The point is inside the primary municipality, so it
    gets the majority of confidence; neighbors share the remainder weighted by
    how close their boundary is (closer -> more plausible the input was off).
    """
    if not neighbors:
        return {"primary": 100.0, "neighbors": {}, "reasoning": (
            "The location falls squarely inside this municipality and no other "
            "municipality lies within the 3-mile search radius.")}

    # Each neighbor's "doubt weight" grows as it gets closer to the point.
    weights = {}
    for n in neighbors:
        d = max(0.0, min(RADIUS_MILES, n["approx_miles"]))
        # 0 miles away -> weight 1.0 ; RADIUS miles away -> weight ~0.05
        weights[n["full_name"]] = max(0.05, 1.0 - d / RADIUS_MILES)

    # Total doubt caps out so the primary never drops below 50%.
    total_weight = sum(weights.values())
    max_doubt = 0.45  # neighbors can claim at most 45% combined
    doubt_scale = min(max_doubt, 0.30 * total_weight)
    primary_conf = (1.0 - doubt_scale) * 100.0

    neigh_conf = {}
    if total_weight > 0:
        for name, w in weights.items():
            neigh_conf[name] = round(doubt_scale * (w / total_weight) * 100.0, 1)

    nearest = neighbors[0]
    reasoning = (
        f"The location is inside {primary['name']} {primary['type']}. "
        f"{nearest['name']} {nearest['type']} lies about {nearest['approx_miles']} "
        f"miles away, so there is some chance a slightly-off coordinate or address "
        f"actually belongs there -- but the boundary data places the point in "
        f"{primary['name']}.")
    return {"primary": round(primary_conf, 1), "neighbors": neigh_conf,
            "reasoning": reasoning}


def claude_confidence(primary: dict, neighbors: list[dict],
                      location_desc: str) -> dict | None:
    """Use Claude to produce confidence ratings + reasoning. None on any failure."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic
    except ImportError:
        return None

    facts = {
        "location": location_desc,
        "containing_municipality": {
            "name": primary["name"], "type": primary["type"],
            "county": primary["county"], "state": primary["state"],
            "note": "The Census boundary data places the point INSIDE this one.",
        },
        "neighbors_within_3_miles": [
            {"name": n["name"], "type": n["type"],
             "approx_miles_to_boundary": n["approx_miles"]}
            for n in neighbors
        ],
    }
    prompt = (
        "You are assigning confidence to which U.S. municipality a location "
        "belongs to. The authoritative Census boundary data already places the "
        "point inside `containing_municipality` -- that is the answer and should "
        "receive the highest confidence. Neighbors are reported only because a "
        "small error in the input coordinates/address could place the point just "
        "across a nearby boundary; assign them modest confidence that grows the "
        "closer their boundary is.\n\n"
        f"Facts:\n{json.dumps(facts, indent=2)}\n\n"
        "Respond with ONLY a JSON object of this exact shape:\n"
        '{"primary_confidence": <number 0-100>, '
        '"neighbor_confidence": {"<municipality full name>": <number 0-100>, ...}, '
        '"reasoning": "<one or two sentences>"}\n'
        "All confidences should sum to about 100."
    )
    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
        parsed = json.loads(text.strip())
        raw_neigh = {k: float(v) for k, v in parsed.get("neighbor_confidence", {}).items()}
        # Claude may key neighbors slightly differently (e.g. "Town of Algoma"
        # vs "Algoma town"). Remap onto our canonical full_names by matching on
        # the base municipality name contained in the key.
        neigh_conf: dict[str, float] = {}
        for n in neighbors:
            match = raw_neigh.get(n["full_name"])
            if match is None:
                for key, val in raw_neigh.items():
                    if n["name"].lower() in key.lower():
                        match = val
                        break
            if match is not None:
                neigh_conf[n["full_name"]] = match
        return {
            "primary": float(parsed["primary_confidence"]),
            "neighbors": neigh_conf,
            "reasoning": parsed.get("reasoning", ""),
        }
    except Exception:  # noqa: BLE001 - fall back to heuristic on any error
        return None


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def find_municipality(address: str | None, lat: float | None,
                      lon: float | None) -> dict:
    resolved_from = None
    matched_address = None
    note = None

    if lat is not None and lon is not None:
        resolved_from = "coordinates"
        if address:
            resolved_from = "coordinates (address also supplied)"
    elif address:
        geo = geocode_address(address)
        if not geo:
            raise CensusError(
                f"Could not geocode address: {address!r}. Try adding the city "
                f"and state, e.g. '{address}, Madison, WI'.")
        lat, lon = geo["lat"], geo["lon"]
        matched_address = geo["matched_address"]
        resolved_from = "address"
        if geo.get("assumed_state"):
            note = (f"No state was given, so '{geo['assumed_state']}' was assumed. "
                    f"Matched: {matched_address}. If that's the wrong place, add "
                    f"the city/state to the address.")
    else:
        raise ValueError("Provide an address, coordinates, or both.")

    geos = reverse_geographies(lat, lon)
    primary = extract_municipality(geos)
    if not primary:
        raise CensusError(
            "No municipality found for this location (it may be in an "
            "unincorporated area outside any Census county subdivision).")

    neighbors = find_neighbors(lat, lon, primary["name"])

    location_desc = matched_address or f"{lat:.5f}, {lon:.5f}"
    claude_result = claude_confidence(primary, neighbors, location_desc)
    if claude_result is not None:
        conf, conf_source = claude_result, "claude"
    else:
        conf, conf_source = heuristic_confidence(primary, neighbors), "heuristic"

    return {
        "input": {"address": address, "lat": lat, "lon": lon},
        "resolved_from": resolved_from,
        "matched_address": matched_address,
        "coordinates": {"lat": lat, "lon": lon},
        "primary": primary,
        "neighbors_within_3mi": neighbors,
        "confidence": conf,
        "confidence_source": conf_source,
        "note": note,
    }


# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #

def render(result: dict) -> str:
    p = result["primary"]
    conf = result["confidence"]
    lines = []
    lines.append("=" * 60)
    lines.append("MUNICIPALITY RESULT")
    lines.append("=" * 60)
    if result.get("note"):
        lines.append(f"NOTE: {result['note']}")
    if result.get("matched_address"):
        lines.append(f"Matched address : {result['matched_address']}")
    c = result["coordinates"]
    lines.append(f"Coordinates     : {c['lat']:.5f}, {c['lon']:.5f}  "
                 f"(resolved from {result['resolved_from']})")
    loc = ", ".join(x for x in (p["county"], p["state"]) if x)
    lines.append("")
    lines.append(f"  → {p['name']} {p['type']}"
                 + (f"   [{loc}]" if loc else "")
                 + f"    confidence {conf['primary']:.0f}%")

    neighbors = result["neighbors_within_3mi"]
    if neighbors:
        lines.append("")
        lines.append(f"Other municipalities within {RADIUS_MILES:.0f} miles:")
        for n in neighbors:
            nconf = conf["neighbors"].get(n["full_name"])
            nconf_str = f"confidence {nconf:.0f}%" if nconf is not None else ""
            lines.append(f"  • {n['name']} {n['type']}  "
                         f"(~{n['approx_miles']} mi)   {nconf_str}")
    else:
        lines.append("")
        lines.append(f"No other municipality within {RADIUS_MILES:.0f} miles.")

    if conf.get("reasoning"):
        lines.append("")
        lines.append("Reasoning: " + conf["reasoning"])
    lines.append("=" * 60)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find the U.S. municipality for an address and/or coordinates.")
    parser.add_argument("--address", "-a", help="Street address to look up.")
    parser.add_argument("--lat", type=float, help="Latitude (decimal degrees).")
    parser.add_argument("--lon", type=float, help="Longitude (decimal degrees).")
    parser.add_argument("--coords", "-c",
                        help='Coordinates as "lat,lon" (alternative to --lat/--lon).')
    parser.add_argument("--json", action="store_true",
                        help="Emit raw JSON instead of formatted text.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    lat, lon = args.lat, args.lon
    if args.coords:
        try:
            slat, slon = args.coords.split(",")
            lat, lon = float(slat.strip()), float(slon.strip())
        except ValueError:
            print('Error: --coords must look like "43.0731,-89.4012"', file=sys.stderr)
            return 2

    if not args.address and (lat is None or lon is None):
        print("Error: provide --address, or --lat/--lon, or --coords.", file=sys.stderr)
        return 2

    try:
        result = find_municipality(args.address, lat, lon)
    except (CensusError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
