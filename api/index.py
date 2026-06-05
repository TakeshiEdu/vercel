from __future__ import annotations

import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, send_from_directory

BASE_URL = "https://www.busdoko-oita.jp"
TRANSIT_SEARCH_URL = f"{BASE_URL}/map/Transit/GetTransitLocationInfoByFreeword"
TRANSIT_RESULT_URL = f"{BASE_URL}/map/Transit/Result"
APPROACH_SEARCH_URL = f"{BASE_URL}/map/Approach/GetKeywordCompletionByFreeword"
APPROACH_RESULT_URL = f"{BASE_URL}/map/Approach/Result"
APPROACH_CHANGE_NORIBA_URL = f"{BASE_URL}/map/Approach/ChangeNoriba"
# 公式バス位置情報API（スクレイピング不要のJSON API）
BUS_LOCATION_URL = f"{BASE_URL}/map/Approach/BusLocationByCourseBusId"
CONVERT_TO_TOKYO_URL = f"{BASE_URL}/map/SearchStation/ConvertToTokyoLatLng"
SEARCH_STATION_FOR_MAP_URL = f"{BASE_URL}/map/SearchStation/SearchStationForMap"
TIMETABLE_DATA_URL = f"{BASE_URL}/map/ViewTimeTable/TimeTableAll"
ROUTE_DETAIL_URL = f"{BASE_URL}/map/ViewTimeTable/RouteDetail"

ROOT = Path(__file__).resolve().parent.parent
STITCH_DIR = ROOT / "public"

app = Flask(__name__, static_folder=str(STITCH_DIR), static_url_path="")

_HTTP = requests.Session()
_TTL_CACHE: dict[str, tuple[float, Any]] = {}
APPROACH_FETCH_WORKERS = 4
BUS_LOCATION_FETCH_WORKERS = 4


def _cache_get(key: str) -> Any | None:
    item = _TTL_CACHE.get(key)
    if not item:
        return None
    expire_at, value = item
    if expire_at < time.time():
        _TTL_CACHE.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: Any, ttl_sec: int) -> Any:
    _TTL_CACHE[key] = (time.time() + max(ttl_sec, 1), value)
    return value


def _cache_key(prefix: str, payload: Any) -> str:
    return f"{prefix}:{json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)}"


def _safe_get_json(url: str, params: dict[str, Any] | None = None, ttl_sec: int = 30) -> Any:
    key = _cache_key("get_json", {"url": url, "params": params or {}})
    cached = _cache_get(key)
    if cached is not None:
        return cached

    response = _HTTP.get(url, params=params, timeout=12)
    response.raise_for_status()
    return _cache_set(key, response.json(), ttl_sec)


def _normalize_stop_key(text: str) -> str:
    return re.sub(r"[\s　()（）]", "", str(text or "")).strip().lower()


def _sort_stop_candidates(candidates: list[dict[str, Any]], keyword: str) -> list[dict[str, Any]]:
    q = _normalize_stop_key(keyword)

    def _rank(item: dict[str, Any]) -> tuple[int, int]:
        name = _normalize_stop_key(str(item.get("Text") or item.get("Name") or ""))
        if q and name == q:
            return (0, len(name))
        if q and name.startswith(q):
            return (1, len(name))
        if q and q in name:
            return (2, len(name))
        if q and name in q:
            return (3, len(name))
        return (4, len(name))

    return sorted(candidates, key=_rank)


def get_stop_info(keyword: str) -> dict[str, Any] | None:
    data = _safe_get_json(TRANSIT_SEARCH_URL, {"freeword": keyword})
    if not data:
        return None
    stops = [d for d in data if d.get("Type") == 3]
    return stops[0] if stops else data[0]


def get_stop_candidates(keyword: str, limit: int = 8) -> list[dict[str, Any]]:
    data = _safe_get_json(TRANSIT_SEARCH_URL, {"freeword": keyword})
    if not data:
        return []

    stops = [d for d in data if d.get("Type") == 3]
    if not stops:
        stops = data

    # Keep ordering from upstream but remove exact duplicates.
    unique: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for s in stops:
        key = (
            s.get("Text"),
            s.get("Latitude"),
            s.get("Longitude"),
            s.get("StationSid"),
            s.get("StationCode"),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(s)

    unique = _sort_stop_candidates(unique, keyword)
    return unique[: max(limit, 1)]


def suggest_stops(keyword: str) -> list[dict[str, Any]]:
    if not keyword:
        return []

    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()

    for q in [f"{keyword}バス停", keyword]:
        try:
            rows = _safe_get_json(APPROACH_SEARCH_URL, {"selectLang": "ja", "freeword": q})
        except Exception:
            continue

        for row in rows:
            if row.get("Category") != "バス":
                continue
            name = row.get("Name") or row.get("Text") or ""
            if not name:
                continue
            pos = row.get("Position") or {}
            station_code = str(row.get("StationCode") or "")
            company_id = str(row.get("CompanyID") or "")
            lat = str(pos.get("Latitude") or "")
            lng = str(pos.get("Longitude") or "")
            dedupe_key = (name, station_code, company_id, lat, lng)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            results.append(
                {
                    "name": name,
                    "area": row.get("Address") or row.get("Category") or "",
                    "stationCode": station_code,
                    "companyId": company_id,
                    "lat": pos.get("Latitude"),
                    "lng": pos.get("Longitude"),
                }
            )
    return results[:10]


def get_approach_stop_info(keyword: str) -> dict[str, Any] | None:
    candidates = suggest_stops(keyword)
    if not candidates:
        return None
    return candidates[0]


def resolve_approach_stop_ref(name: str, lat: Any = None, lng: Any = None) -> dict[str, str]:
    ref = {
        "name": str(name or ""),
        "stationCode": "",
        "companyId": "",
    }
    candidates = suggest_stops(name)
    if not candidates:
        return ref

    chosen = candidates[0]
    norm_name = _normalize_stop_name(name)
    exact = next((c for c in candidates if _normalize_stop_name(c.get("name", "")) == norm_name), None)
    if exact:
        chosen = exact

    try:
        if lat not in (None, "") and lng not in (None, ""):
            t_lat = float(lat)
            t_lng = float(lng)
            by_dist: list[tuple[float, dict[str, Any]]] = []
            for c in candidates:
                c_lat = c.get("lat")
                c_lng = c.get("lng")
                if c_lat in (None, "") or c_lng in (None, ""):
                    continue
                by_dist.append((_haversine_m(t_lat, t_lng, float(c_lat), float(c_lng)), c))
            if by_dist:
                by_dist.sort(key=lambda x: x[0])
                chosen = by_dist[0][1]
    except Exception:
        pass

    ref["name"] = str(chosen.get("name") or ref["name"])
    ref["stationCode"] = str(chosen.get("stationCode") or "")
    ref["companyId"] = str(chosen.get("companyId") or "")
    return ref


def fetch_approach_data(stop_code: str, company_id: str, force_refresh: bool = False, ttl_sec: int = 8) -> str:
    cache_key = _cache_key("approach_data", {"stop_code": stop_code, "company_id": company_id})
    if not force_refresh:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    payload = {
        "selectLang": "ja",
        "startStaCode": stop_code,
        "startCompId": company_id,
        "goalStaCode": "",
        "goalCompId": "",
        "listSortMode": 0,
    }
    headers = {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": "Mozilla/5.0",
    }
    _HTTP.get(f"{BASE_URL}/map/Approach", timeout=12)
    response = _HTTP.post(APPROACH_RESULT_URL, json=payload, headers=headers, timeout=15)
    response.raise_for_status()
    json_text = response.json()
    if force_refresh:
        # Keep only a very short cache window to avoid request bursts from concurrent polls.
        return _cache_set(cache_key, json_text, min(max(ttl_sec, 1), 2))
    return _cache_set(cache_key, json_text, max(ttl_sec, 1))


def fetch_approach_data_by_sid(
    station_sid: str,
    force_refresh: bool = False,
    ttl_sec: int = 8,
) -> str:
    """stationSid ベースで Approach/Result を取得する。

    `startStaCode` + `startCompId` 方式と異なり、`stationSid` を指定することで
    そののりばに停車する全社・全方面のバスをまとめて取得できる。
    """
    cache_key = _cache_key("approach_data_sid", {"station_sid": station_sid})
    if not force_refresh:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    payload = {
        "selectLang": "ja-JP",
        "stationSid": station_sid,
        "goalStaCode": "",
        "goalCompId": "",
        "listSortMode": 0,
    }
    headers = {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": "Mozilla/5.0",
        "Referer": f"{BASE_URL}/map/Approach?sid={station_sid}",
    }
    _HTTP.get(f"{BASE_URL}/map/Approach?sid={station_sid}", timeout=12)
    response = _HTTP.post(APPROACH_CHANGE_NORIBA_URL, json=payload, headers=headers, timeout=15)
    response.raise_for_status()

    # ChangeNoriba may return an escaped JSON string ("\u003cdiv ...") instead of raw HTML.
    # Decode it so the downstream HTML parser can see actual tags.
    html_text = response.text
    try:
        parsed = response.json()
        if isinstance(parsed, str):
            html_text = parsed
    except Exception:
        text = str(response.text or "").strip()
        if text.startswith('"') and text.endswith('"'):
            try:
                parsed_text = json.loads(text)
                if isinstance(parsed_text, str):
                    html_text = parsed_text
            except Exception:
                pass

    if force_refresh:
        return _cache_set(cache_key, html_text, min(max(ttl_sec, 1), 2))
    return _cache_set(cache_key, html_text, max(ttl_sec, 1))


def get_stop_sids_near(
    lat: float,
    lng: float,
    name: str,
    radius_m: int = 500,
    strict_name: bool = False,
) -> list[str]:
    """指定座標近傍の全停留所を SearchStationForMap で取得し、同名の SID リストを返す。

    同じバス停名でものりば毎に別 SID が存在するため、全のりば分をカバーするのに必要。
    WGS84 座標をそのまま渡す（座標変換をすると誤差が出るためスキップ）。
    """
    cache_key = _cache_key(
        "stop_sids_near",
        {
            "lat": round(lat, 5),
            "lng": round(lng, 5),
            "name": name,
            "r": radius_m,
            "strict": bool(strict_name),
        },
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    conv = _safe_get_json(CONVERT_TO_TOKYO_URL, {"lat": lat, "lng": lng})
    if conv and "Latitude" in conv and "Longitude" in conv:
        t_lat = float(conv.get("Latitude"))
        t_lng = float(conv.get("Longitude"))
    else:
        t_lat = lat
        t_lng = lng

    headers = {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": "Mozilla/5.0",
    }
    try:
        import requests as _req_mod
        tmp_sess = _req_mod.Session()
        response = tmp_sess.post(
            SEARCH_STATION_FOR_MAP_URL,
            json={"selectLang": "ja", "lat": t_lat, "lng": t_lng, "allowDistance": radius_m},
            headers=headers,
            timeout=15,
        )
        response.raise_for_status()
        stations = response.json() if response.text else []
    except Exception:
        return _cache_set(cache_key, [], 30)

    if not isinstance(stations, list):
        return _cache_set(cache_key, [], 30)

    base_norm_name = _normalize_stop_name_base(name)
    strict_norm_name = _normalize_stop_name(name)
    sids: list[str] = []
    seen: set[str] = set()
    for s in stations:
        sid = str(s.get("Sid") or "")
        station_name = str(s.get("Name") or "")
        base_s_name = _normalize_stop_name_base(station_name)
        strict_s_name = _normalize_stop_name(station_name)
        if sid and sid not in seen:
            if strict_name:
                # Strict mode avoids "西口" 等の別名混在を防ぎつつ、
                # "別府駅前①" のような番線バリアントは同一グループとして許可する。
                if strict_norm_name and strict_s_name and strict_norm_name == strict_s_name:
                    seen.add(sid)
                    sids.append(sid)
                    continue

                if strict_norm_name:
                    strict_base = _normalize_stop_name_base(strict_norm_name)
                    station_base = _normalize_stop_name_base(station_name)
                    if strict_base and station_base and strict_base == station_base:
                        seen.add(sid)
                        sids.append(sid)
            else:
                if len(base_norm_name) >= 2 and len(base_s_name) >= 2:
                    if base_norm_name in base_s_name or base_s_name in base_norm_name:
                        seen.add(sid)
                        sids.append(sid)

    return _cache_set(cache_key, sids, 60)


def get_station_sid_candidates(
    stop_info: dict[str, Any],
    stop_name: str,
    radius_m: int = 700,
    limit: int = 8,
) -> list[str]:
    """同名停留所ののりば違いを含めた stationSid 候補を返す。"""
    name = str(stop_name or stop_info.get("Text") or stop_info.get("Name") or "").strip()
    lat = stop_info.get("Latitude")
    lng = stop_info.get("Longitude")
    if lat in (None, ""):
        lat = stop_info.get("lat")
    if lng in (None, ""):
        lng = stop_info.get("lng")

    cache_key = _cache_key(
        "station_sid_candidates",
        {
            "name": name,
            "sid": stop_info.get("StationSid") or stop_info.get("stationSid") or "",
            "lat": lat,
            "lng": lng,
            "r": int(radius_m),
            "limit": int(limit),
        },
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    candidates: list[str] = []
    seen: set[str] = set()

    primary_sid = str(stop_info.get("StationSid") or stop_info.get("stationSid") or "").strip()
    if primary_sid:
        seen.add(primary_sid)
        candidates.append(primary_sid)

    if name and lat not in (None, "") and lng not in (None, ""):
        try:
            nearby_sids = get_stop_sids_near(float(lat), float(lng), name, radius_m=radius_m)
        except Exception:
            nearby_sids = []
        for sid in nearby_sids:
            sid_text = str(sid or "").strip()
            if not sid_text or sid_text in seen:
                continue
            seen.add(sid_text)
            candidates.append(sid_text)

    return _cache_set(cache_key, candidates[: max(limit, 1)], 60)


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371000.0
    p1 = lat1 * math.pi / 180.0
    p2 = lat2 * math.pi / 180.0
    dp = (lat2 - lat1) * math.pi / 180.0
    dl = (lng2 - lng1) * math.pi / 180.0
    a = (math.sin(dp / 2.0) ** 2
        + math.cos(p1) * math.cos(p2) * (math.sin(dl / 2.0) ** 2))
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return r * c


def nearby_stops_by_coords(lat: float, lng: float, allow_distance: int = 1200, limit: int = 8) -> list[dict[str, Any]]:
    conv = _safe_get_json(CONVERT_TO_TOKYO_URL, {"lat": lat, "lng": lng})
    t_lat = float(conv.get("Latitude"))
    t_lng = float(conv.get("Longitude"))

    headers = {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": "Mozilla/5.0",
    }
    payload = {
        "selectLang": "ja",
        "lat": t_lat,
        "lng": t_lng,
        "allowDistance": int(allow_distance),
    }
    cache_key = _cache_key("nearby_coords", {"lat": round(lat, 6), "lng": round(lng, 6), "allowDistance": int(allow_distance), "limit": limit})
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached[: max(limit, 1)]

    response = _HTTP.post(SEARCH_STATION_FOR_MAP_URL, data=json.dumps(payload), headers=headers, timeout=15)
    response.raise_for_status()
    stations = response.json() if response.text else []
    if not isinstance(stations, list):
        return []

    by_name: dict[str, dict[str, Any]] = {}
    for s in stations:
        pos = s.get("Position") or {}
        s_lat = pos.get("Latitude")
        s_lng = pos.get("Longitude")
        name = str(s.get("Name") or "").strip()
        if not name or s_lat in (None, "") or s_lng in (None, ""):
            continue
        try:
            dist = _haversine_m(t_lat, t_lng, float(s_lat), float(s_lng))
        except Exception:
            continue

        item = {
            "name": name,
            "area": "近隣",
            "lat": float(s_lat),
            "lng": float(s_lng),
            "distanceM": round(dist, 1),
        }
        prev = by_name.get(name)
        if not prev or item["distanceM"] < prev.get("distanceM", 10**9):
            by_name[name] = item

    ordered = sorted(by_name.values(), key=lambda x: x.get("distanceM", 10**9))
    _cache_set(cache_key, ordered, 20)
    return ordered[: max(limit, 1)]


def parse_approach_html(html_content: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    soup = BeautifulSoup(html_content, "html.parser")

    stop_lat = soup.find("input", id="_hdnSelectStationLatitude")
    stop_lng = soup.find("input", id="_hdnSelectStationLongitude")
    stop_coords = {
        "lat": float(stop_lat.get("value")) if stop_lat and stop_lat.get("value") else None,
        "lng": float(stop_lng.get("value")) if stop_lng and stop_lng.get("value") else None,
    }

    buses: list[dict[str, Any]] = []
    items = soup.find_all("li", class_=re.compile(r"bus-.*"))
    for idx, item in enumerate(items):
        time_td = item.find("td", class_="Time")
        destination_a = item.find("td", class_="CourseGroupName")
        delay_td = item.find("td", class_=re.compile(r"DelayTime.*"))
        location_td = item.find("td", class_="TsuukaStationName")
        line_td = item.find("td", class_="KeitouNo")

        destination_link = destination_a.find("a") if destination_a else None
        delay_inner = delay_td.find("div", class_="changeCourseInner") if delay_td else None

        bus_info: dict[str, Any] = {}
        parsed_line = ""
        parsed_destination = ""
        parsed_via = ""
        match = re.search(r"resultByCourse\((.*?)\)", str(item), re.DOTALL)
        if match:
            args = [a.strip("' \r\n\t") for a in match.group(1).split(",")]
            if len(args) >= 5:
                parsed_line = args[7] if len(args) > 7 else ""
                parsed_destination = args[8] if len(args) > 8 else ""
                parsed_via = args[9] if len(args) > 9 else ""
                bus_info = {
                    "courseGroupSid": args[0],
                    "stationSid": args[1] if len(args) > 1 else "",
                    "courseSid": args[2],
                    "companyId": args[3],
                    "busId": args[4],
                    "keiyuCd": args[5] if len(args) > 5 else "",
                    "uniqueId": args[6] if len(args) > 6 else "",
                    "line": parsed_line,
                    "destination": parsed_destination,
                    "via": parsed_via,
                }

        daiya_in = soup.find("input", id=f"_hdnDaiyaSid_{idx}")
        if not daiya_in:
            daiya_in = soup.find("input", id="_hdnSelectDaiyaSid")
        if daiya_in and bus_info is not None:
            bus_info["daiyaSid"] = daiya_in.get("value", "")
            
        daiya_id = bus_info.get("daiyaSid") or bus_info.get("uniqueId") or ""

        destination_text = destination_link.get_text(strip=True) if destination_link else ""
        if not destination_text:
            destination_text = str(parsed_destination or parsed_via or "").strip()
            
        if not destination_text and daiya_id:
            # 隠しフィールドから取得を試みる
            course_name_in = soup.find("input", id=f"_hdnApproachCourseName_{daiya_id}")
            if course_name_in and course_name_in.get("value"):
                destination_text = course_name_in.get("value")
            else:
                ikisaki_name_in = soup.find("input", id=f"_hdnApproachIkisakiName_{daiya_id}")
                if ikisaki_name_in and ikisaki_name_in.get("value"):
                    destination_text = ikisaki_name_in.get("value")

        line_text = line_td.get_text(strip=True) if line_td else ""
        if not line_text:
            line_text = str(parsed_line or "").strip()

        buses.append(
            {
                "time": time_td.get_text(strip=True) if time_td else "",
                "destination": destination_text,
                "officialRouteName": destination_text,
                "status": delay_inner.get_text(strip=True) if delay_inner else "",
                "location": location_td.get_text(strip=True) if location_td else "",
                "line": line_text,
                "busInfo": bus_info,
            }
        )

    return buses, stop_coords


def _lookup_primary_sid_pair_legacy(
    from_info: dict[str, Any],
    to_info: dict[str, Any],
    start_time: str,
) -> tuple[str | None, str | None]:
    cache_key = _cache_key(
        "sid_lookup_legacy",
        {
            "fromSid": from_info.get("StationSid"),
            "toSid": to_info.get("StationSid"),
            "fromWord": from_info.get("Text"),
            "toWord": to_info.get("Text"),
            "start": str(start_time)[:16],
        },
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    payload = {
        "selectLang": "ja",
        "startType": 2,
        "goalType": 2,
        "startWord": from_info["Text"],
        "goalWord": to_info["Text"],
        "startLatitude": from_info["Latitude"],
        "startLongitude": from_info["Longitude"],
        "goalLatitude": to_info["Latitude"],
        "goalLongitude": to_info["Longitude"],
        "searchDateTime": start_time,
        "searchKbn": 1,
        "sortType": "Time",
        "transportKbn": [1, 2, 3, 4, 5],
    }
    headers = {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"}
    try:
        response = _HTTP.post(TRANSIT_RESULT_URL, data=json.dumps(payload), headers=headers, timeout=15)
        response.raise_for_status()
        res_json = response.json()
    except Exception:
        return _cache_set(cache_key, (None, None), 10)

    if isinstance(res_json, dict):
        # Upstream may return an error object instead of the HTML fragment.
        return _cache_set(cache_key, (None, None), 10)
    if not isinstance(res_json, str):
        return _cache_set(cache_key, (None, None), 10)

    soup = BeautifulSoup(res_json, "html.parser")
    sid_input = soup.find("input", id="_hdnSID1")
    if not sid_input:
        return _cache_set(cache_key, (None, None), 10)
    sids = [str(s).strip() for s in sid_input.get("value", "").split(",") if str(s).strip()]
    if len(sids) < 2:
        return _cache_set(cache_key, (None, None), 10)
    return _cache_set(cache_key, (sids[0], sids[-1]), 20)


def get_sid_pairs_from_transit_search(
    from_info: dict[str, Any],
    to_info: dict[str, Any],
    start_time: str,
    max_pairs: int = 6,
) -> list[tuple[str, str]]:
    cache_key = _cache_key(
        "sid_lookup_pairs",
        {
            "fromSid": from_info.get("StationSid"),
            "toSid": to_info.get("StationSid"),
            "fromWord": from_info.get("Text"),
            "toWord": to_info.get("Text"),
            "start": str(start_time)[:16],
            "max": int(max_pairs),
        },
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    routes = fetch_transit_routes(from_info, to_info, start_time, sort_type="Time", search_kbn=1, ttl_sec=20)
    for route in routes:
        station_sids = [str(s).strip() for s in (route.get("stationSids") or []) if str(s).strip()]
        if len(station_sids) < 2:
            continue
        pair = (station_sids[0], station_sids[-1])
        if pair in seen:
            continue
        seen.add(pair)
        pairs.append(pair)
        if len(pairs) >= max(max_pairs, 1):
            break

    if not pairs:
        legacy_pair = _lookup_primary_sid_pair_legacy(from_info, to_info, start_time)
        if legacy_pair[0] and legacy_pair[1]:
            pairs.append((legacy_pair[0], legacy_pair[1]))

    return _cache_set(cache_key, pairs, 20)


def get_sids_from_transit_search(from_info: dict[str, Any], to_info: dict[str, Any], start_time: str) -> tuple[str | None, str | None]:
    sid_pairs = get_sid_pairs_from_transit_search(from_info, to_info, start_time, max_pairs=1)
    if not sid_pairs:
        return (None, None)
    return sid_pairs[0]


# ---------------------------------------------------------------------------
# Transit/Result パーサ（経路検索結果を HTML 1回で取り切る）
# ---------------------------------------------------------------------------


def parse_transit_result_html(html_content: str) -> list[dict[str, Any]]:
    """`Transit/Result` の HTML レスポンスから経路候補を全て抽出する。

    従来は SID だけ取り出して TimeTableAll + RouteDetail を別途叩いていたが、
    この関数は 1 回の HTML で発着時刻・系統名・運賃・停留所名を全て取得する。
    二次・三次スクレイピングが不要になる。
    """
    soup = BeautifulSoup(html_content, "html.parser")
    routes: list[dict[str, Any]] = []

    def _hv(field_id: str) -> str:
        el = soup.find("input", id=field_id)
        return str(el.get("value", "")) if el else ""

    for n in range(1, 6):
        box = soup.find("div", id=f"_divRosen{n}")
        if not box:
            break

        # サマリー行（所要時間・運賃・乗継回数）
        travel_time_min: int | None = None
        fare_yen: int | None = None
        transfer_count = 0
        bt20 = box.find("p", class_="bt20")
        if bt20:
            raw_text = bt20.get_text(" ", strip=True)
            m = re.search(r"(\d+)\s*分", raw_text)
            if m:
                travel_time_min = int(m.group(1))
            m = re.search(r"(\d+)\s*円", raw_text)
            if m:
                fare_yen = int(m.group(1))
            m = re.search(r"乗継回数.*?(\d+)", raw_text)
            if m:
                transfer_count = int(m.group(1))

        # 停留所・セグメント
        dl = box.find("dl", class_="resultRouteWrap")
        segments: list[dict[str, Any]] = []
        all_stop_names: list[str] = []
        departure_time = ""
        arrival_time = ""

        if dl:
            for dt in dl.find_all("dt", class_="stop"):
                a = dt.find("a")
                all_stop_names.append(a.get_text(strip=True) if a else "")

            for i, ul in enumerate(dl.find_all("ul")):
                time_li = ul.find("li", class_="resultRouteTime")
                if not time_li:
                    continue
                times = re.findall(r"\d{1,2}:\d{2}", time_li.get_text())
                if len(times) < 2:
                    continue
                seg_dep, seg_arr = times[0], times[-1]
                if not departure_time:
                    departure_time = seg_dep
                arrival_time = seg_arr

                company_text = ""
                line_text = ""
                line_li = ul.find("li", class_=re.compile(r"resultRouteLine"))
                if line_li:
                    bus_link = line_li.find("div", class_="busRideLink")
                    if bus_link:
                        parts = [
                            p.strip()
                            for p in bus_link.get_text("\n", strip=True).split("\n")
                            if p.strip()
                        ]
                        if len(parts) >= 2:
                            company_text, line_text = parts[0], parts[1]
                        elif parts:
                            line_text = parts[0]

                seg_fare_yen: int | None = None
                fare_li = ul.find("li", class_="resultRouteFare")
                if fare_li:
                    m2 = re.search(r"(\d+)", fare_li.get_text())
                    if m2:
                        seg_fare_yen = int(m2.group(1))

                segments.append(
                    {
                        "departureTime": seg_dep,
                        "arrivalTime": seg_arr,
                        "company": company_text,
                        "line": line_text,
                        "boardingStop": all_stop_names[i] if i < len(all_stop_names) else "",
                        "alightingStop": all_stop_names[i + 1] if i + 1 < len(all_stop_names) else "",
                        "fareYen": seg_fare_yen,
                    }
                )

        routes.append(
            {
                "routeNo": n,
                "travelTimeMin": travel_time_min,
                "fareYen": fare_yen,
                "transferCount": transfer_count,
                "departureTime": departure_time,
                "arrivalTime": arrival_time,
                "boardingStop": all_stop_names[0] if all_stop_names else "",
                "alightingStop": all_stop_names[-1] if all_stop_names else "",
                "segments": segments,
                "stationSids": [s for s in _hv(f"_hdnSID{n}").split(",") if s],
                "stationNames": [s for s in _hv(f"_hdnName{n}").split(",") if s],
                "courseSid": _hv(f"_hdnCourseSid{n}"),
                "daiyaSid": _hv(f"_hdnDaiyaSid{n}"),
                "keitouSid": _hv(f"_hdnKeitouSid{n}"),
            }
        )

    return routes


def fetch_transit_routes(
    from_info: dict[str, Any],
    to_info: dict[str, Any],
    start_time: str,
    sort_type: str = "Time",
    search_kbn: int = 1,
    ttl_sec: int = 20,
) -> list[dict[str, Any]]:
    """Transit/Result を 1 回叩き経路候補をまとめて返す。

    従来の get_sids_from_transit_search + TimeTableAll + RouteDetail
    3 連鎖スクレイピングをこの 1 呼び出しで置き換える。
    """
    cache_key = _cache_key(
        "transit_routes",
        {
            "fromSid": from_info.get("StationSid"),
            "toSid": to_info.get("StationSid"),
            "fromWord": from_info.get("Text"),
            "toWord": to_info.get("Text"),
            "start": str(start_time)[:16],
            "sort": sort_type,
            "kbn": search_kbn,
        },
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    payload = {
        "selectLang": "ja",
        "startType": 2,
        "goalType": 2,
        "startWord": from_info.get("Text", ""),
        "goalWord": to_info.get("Text", ""),
        "startLatitude": from_info.get("Latitude") or from_info.get("lat"),
        "startLongitude": from_info.get("Longitude") or from_info.get("lng"),
        "goalLatitude": to_info.get("Latitude") or to_info.get("lat"),
        "goalLongitude": to_info.get("Longitude") or to_info.get("lng"),
        "searchDateTime": start_time,
        "searchKbn": search_kbn,
        "sortType": sort_type,
        "transportKbn": [1, 2, 3, 4, 5],
    }
    headers = {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": "Mozilla/5.0",
        "Referer": f"{BASE_URL}/map/Transit",
    }
    try:
        response = _HTTP.post(
            TRANSIT_RESULT_URL, data=json.dumps(payload), headers=headers, timeout=15
        )
        response.raise_for_status()
        res = response.json()
    except Exception:
        return _cache_set(cache_key, [], min(ttl_sec, 5))

    if not isinstance(res, str):
        return _cache_set(cache_key, [], min(ttl_sec, 5))

    routes = parse_transit_result_html(res)
    return _cache_set(cache_key, routes, max(ttl_sec, 1))


def fetch_timetable_data(station_sid: str, unyou_date: str) -> str:
    cache_key = _cache_key("timetable", {"stationSid": station_sid, "date": unyou_date})
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    params = {
        "selectLang": "ja",
        "parentCompanyCode": "9000",
        "stationSid": station_sid,
        "busStopCode": "",
        "goalStationCode": "",
        "goalCompanyId": "",
        "selectUnyouDate": unyou_date,
    }
    response = _HTTP.get(TIMETABLE_DATA_URL, params=params, timeout=15)
    response.raise_for_status()
    return _cache_set(cache_key, response.json(), 60)


def parse_timetable_html(html_content: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html_content, "html.parser")
    runs: list[dict[str, Any]] = []

    for box in soup.find_all("div", class_="timeMainBox"):
        dest_cell = box.find("td", class_="timeTableTitle02")
        dest_p = dest_cell.find("p") if dest_cell else None
        destination = dest_p.get_text(strip=True) if dest_p else "Unknown"

        for row in box.find_all("tr")[2:]:
            hour_th = row.find("th", class_="timeTableDay01")
            td = row.find("td", class_="timeTableDay02")
            if not hour_th or not td:
                continue
            hour = hour_th.get_text(strip=True)

            links = td.find_all("a")
            for link in links:
                minute = link.get_text(strip=True)
                full_text = td.get_text("|", strip=True)
                parts = full_text.split("|")
                line_no = "??"
                for i, part in enumerate(parts):
                    if part == minute and i > 0 and "[" in parts[i - 1]:
                        line_no = parts[i - 1]
                        break

                match = re.search(r"ShowRouteDetail\((.*?)\)", link.get("href", ""))
                if not match:
                    continue
                args = [a.strip("'") for a in match.group(1).split(",")]
                if len(args) < 6:
                    continue

                runs.append(
                    {
                        "time": f"{hour.zfill(2)}:{minute.zfill(2)}",
                        "line": line_no,
                        "destination": destination,
                        "params": {
                            "stationSid": args[0],
                            "ikisakiSEQ": args[1],
                            "timeTableTypeCode": args[2],
                            "selectedTime": args[3],
                            "parentCompanyCode": args[4],
                            "DaiyaSid": args[5],
                        },
                    }
                )
    return runs


def get_stop_direction_options(stop_name: str, service_date: str | None = None) -> list[dict[str, Any]]:
    stop = get_approach_stop_info(stop_name)
    if not stop:
        return []

    sids: list[str] = []
    sid_points: list[tuple[Any, Any]] = []
    sid_source_stops: list[dict[str, Any]] = [stop]
    try:
        candidates = suggest_stops(stop_name)
    except Exception:
        candidates = [stop]

    norm_query = _normalize_stop_name(stop_name)
    for candidate in candidates:
        name = str(candidate.get("name") or "").strip()
        if norm_query and _normalize_stop_name(name) != norm_query:
            continue
        sid = str(candidate.get("stationSid") or candidate.get("StationSid") or "").strip()
        if sid and sid not in sids:
            sids.append(sid)
        sid_source_stops.append(candidate)
        lat = candidate.get("lat") if candidate.get("lat") not in (None, "") else candidate.get("Latitude")
        lng = candidate.get("lng") if candidate.get("lng") not in (None, "") else candidate.get("Longitude")
        if lat not in (None, "") and lng not in (None, ""):
            sid_points.append((lat, lng))

    primary_sid = str(stop.get("stationSid") or stop.get("StationSid") or "").strip()
    if primary_sid and primary_sid not in sids:
        sids.insert(0, primary_sid)

    try:
        transit_candidates = get_stop_candidates(stop_name, limit=12)
    except Exception:
        transit_candidates = []
    for candidate in transit_candidates:
        name = str(candidate.get("Text") or candidate.get("Name") or "").strip()
        if norm_query and _normalize_stop_name(name) != norm_query:
            continue
        sid = str(candidate.get("StationSid") or candidate.get("stationSid") or "").strip()
        if sid and sid not in sids:
            sids.append(sid)
        sid_source_stops.append(candidate)
        lat = candidate.get("Latitude") if candidate.get("Latitude") not in (None, "") else candidate.get("lat")
        lng = candidate.get("Longitude") if candidate.get("Longitude") not in (None, "") else candidate.get("lng")
        if lat not in (None, "") and lng not in (None, ""):
            sid_points.append((lat, lng))

    stop_lat = stop.get("lat") if stop.get("lat") not in (None, "") else stop.get("Latitude")
    stop_lng = stop.get("lng") if stop.get("lng") not in (None, "") else stop.get("Longitude")
    if stop_lat not in (None, "") and stop_lng not in (None, ""):
        sid_points.append((stop_lat, stop_lng))

    seen_near_sids = set(sids)
    for lat, lng in sid_points:
        try:
            nearby_sids = get_stop_sids_near(
                float(lat),
                float(lng),
                stop_name,
                radius_m=700,
                strict_name=True,
            )
        except Exception:
            nearby_sids = []
        for sid in nearby_sids:
            sid_text = str(sid or "").strip()
            if not sid_text or sid_text in seen_near_sids:
                continue
            seen_near_sids.add(sid_text)
            sids.append(sid_text)

    seen_candidate_sids = set(sids)
    for source_stop in sid_source_stops:
        try:
            candidate_sids = get_station_sid_candidates(source_stop, stop_name, radius_m=1000, limit=16)
        except Exception:
            candidate_sids = []
        for sid in candidate_sids:
            sid_text = str(sid or "").strip()
            if not sid_text or sid_text in seen_candidate_sids:
                continue
            seen_candidate_sids.add(sid_text)
            sids.append(sid_text)

    if not sids:
        station_code = str(stop.get("stationCode") or stop.get("StationCode") or "").strip()
        company_id = str(stop.get("companyId") or stop.get("CompanyID") or "").strip()
        if station_code and company_id:
            try:
                html = fetch_approach_data(station_code, company_id)
                buses, _ = parse_approach_html(html)
                enrich_approach_route_classes(buses, stop_name, service_date or datetime.now().strftime("%Y/%m/%d"))
                seen_fallback: set[str] = set()
                fallback_items: list[dict[str, str]] = []
                for bus in buses:
                    item = _fallback_approach_direction(bus)
                    key = item.get("key") or ""
                    if key and key not in seen_fallback:
                        seen_fallback.add(key)
                        fallback_items.append({"key": key, "label": item.get("label") or key})
                return fallback_items
            except Exception:
                return []

    date_text = service_date or datetime.now().strftime("%Y/%m/%d")
    seen: set[str] = set()
    option_map: dict[str, dict[str, Any]] = {}
    now_minutes = datetime.now().hour * 60 + datetime.now().minute

    def remember_option(key: str, label: str, run: dict[str, Any] | None = None) -> None:
        if not key:
            return
        item = option_map.setdefault(key, {"key": f"official:{key}", "label": label or key})
        if label and (not item.get("label") or item.get("label") == key):
            item["label"] = label
        if not run:
            return
        run_time = str(run.get("time") or "").strip()
        run_minutes = _to_minutes(run_time)
        if run_time and not item.get("firstTime"):
            item["firstTime"] = run_time
        if run_minutes is not None and run_minutes >= now_minutes:
            current_next = _to_minutes(str(item.get("nextTime") or ""))
            if current_next is None or run_minutes < current_next:
                item["nextTime"] = run_time
                item["nextLine"] = str(run.get("line") or "").strip()
                item["nextDestination"] = str(run.get("destination") or "").strip()

    for sid in sids:
        try:
            html = fetch_timetable_data(sid, date_text)
            runs = parse_timetable_html(html)
        except Exception:
            continue
        for run in runs:
            route_label = str(run.get("destination") or "").strip()
            normalized = _normalize_direction_key(route_label)
            if not route_label or not normalized:
                continue
            seen.add(normalized)
            remember_option(normalized, route_label, run)

    station_code = str(stop.get("stationCode") or stop.get("StationCode") or "").strip()
    company_id = str(stop.get("companyId") or stop.get("CompanyID") or "").strip()
    if station_code and company_id:
        try:
            html = fetch_approach_data(station_code, company_id)
            buses, _ = parse_approach_html(html)
        except Exception:
            buses = []
        for bus in buses:
            label = str(bus.get("officialRouteName") or bus.get("destination") or "").strip()
            key = _normalize_direction_key(label)
            if not key or key in seen:
                continue
            seen.add(key)
            remember_option(key, label, None)

    for item in option_map.values():
        if not item.get("nextTime") and item.get("firstTime"):
            item["nextTime"] = item["firstTime"]
            item["nextIsFirst"] = True

    return sorted(option_map.values(), key=lambda item: item["label"])


def _to_minutes(hhmm: str | None) -> int | None:
    if not hhmm:
        return None
    matches = re.findall(r"(\d{1,2}):(\d{2})", str(hhmm))
    if not matches:
        return None
    # Datetime text can include date-like fragments (e.g. 2026/03/24 16:30).
    # Use the last HH:mm token so we parse the actual clock time.
    h_s, m_s = matches[-1]
    h = int(h_s)
    m = int(m_s)
    if h < 0 or h > 23 or m < 0 or m > 59:
        return None
    return h * 60 + m


def _minutes_to_hhmm(total_minutes: int) -> str:
    minutes = total_minutes % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _normalize_time_text(raw: str, base_hhmm: str | None, prev_hhmm: str | None = None) -> str:
    text = (raw or "").strip()
    if not text:
        return ""

    # Already a full clock text like 16:11
    full = re.search(r"(\d{1,2}):(\d{2})", text)
    if full:
        h = int(full.group(1))
        m = int(full.group(2))
        if 0 <= h <= 23 and 0 <= m <= 59:
            return f"{h:02d}:{m:02d}"
        return ""

    # Minute-only text like 11 -> complement with base hour.
    minute_only = re.search(r"\b(\d{1,2})\b", text)
    if minute_only and base_hhmm:
        base_match = re.search(r"(\d{1,2}):(\d{2})", base_hhmm)
        if base_match:
            hour = int(base_match.group(1))
            base_min = int(base_match.group(2))
            minute = int(minute_only.group(1))
            if 0 <= minute <= 59:
                # If minute seems to wrap past the hour, step one hour forward.
                if minute < base_min:
                    hour = (hour + 1) % 24

                if prev_hhmm:
                    prev_val = _to_minutes(prev_hhmm)
                    cur_val = hour * 60 + minute
                    if prev_val is not None and cur_val < prev_val:
                        cur_val += 60
                        hour = (cur_val // 60) % 24
                        minute = cur_val % 60

                return f"{hour:02d}:{minute:02d}"

    return ""


def _normalize_stop_name(text: str) -> str:
    return re.sub(r"[\s　()（）]", "", str(text or "")).strip().lower()


def _normalize_direction_key(text: str) -> str:
    normalized = re.sub(r"[\s\u3000]", "", str(text or "")).strip().lower()
    return re.sub(r"行き先?|方面", "", normalized)


def _stop_name_equals(stop_name: str, targets: list[str]) -> bool:
    s = _normalize_stop_name(stop_name)
    if not s:
        return False
    for t in targets:
        nt = _normalize_stop_name(t)
        if not nt:
            continue
        if nt == s:
            return True
    return False


def _normalize_stop_name_base(text: str) -> str:
    s = _normalize_stop_name(text)
    # Remove circled numbers, trailing digits, and trailing '前'
    s = re.sub(r"[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]", "", s)
    s = re.sub(r"[0-9０-９]+$", "", s)
    s = re.sub(r"前$", "", s)
    return s

def _stop_name_matches(stop_name: str, targets: list[str]) -> bool:
    s = _normalize_stop_name(stop_name)
    if not s:
        return False
    for t in targets:
        nt = _normalize_stop_name(t)
        if not nt:
            continue
        if nt in s or s in nt:
            return True

    # Fallback to base name matching (e.g. "別府駅前" vs "別府駅⑤")
    s_base = _normalize_stop_name_base(s)
    if len(s_base) < 2:
        return False
        
    for t in targets:
        nt_base = _normalize_stop_name_base(t)
        if not nt_base:
            continue
        if len(nt_base) >= 2:
            if nt_base == s_base:
                return True
    return False


def _estimate_missing_stop_time_minutes(stops: list[dict[str, str]], target_index: int) -> int | None:
    prev_index: int | None = None
    prev_minutes: int | None = None
    for idx in range(target_index - 1, -1, -1):
        value = _to_minutes(stops[idx].get("time", ""))
        if value is not None:
            prev_index = idx
            prev_minutes = value
            break

    next_index: int | None = None
    next_minutes: int | None = None
    for idx in range(target_index + 1, len(stops)):
        value = _to_minutes(stops[idx].get("time", ""))
        if value is not None:
            next_index = idx
            next_minutes = value
            break

    if prev_minutes is None and next_minutes is None:
        return None

    if prev_minutes is not None and next_minutes is not None and prev_index is not None and next_index is not None:
        span = max(next_index - prev_index, 1)
        pos = max(target_index - prev_index, 1)
        delta = next_minutes - prev_minutes
        if delta < -12 * 60:
            delta += 24 * 60
        if delta > 12 * 60:
            delta -= 24 * 60
        estimated = prev_minutes + int(round(delta * (pos / span)))
        return estimated % (24 * 60)

    if prev_minutes is not None and prev_index is not None:
        gap = max(1, target_index - prev_index)
        return (prev_minutes + min(gap, 3)) % (24 * 60)

    if next_minutes is not None and next_index is not None:
        gap = max(1, next_index - target_index)
        return (next_minutes - min(gap, 3)) % (24 * 60)

    return None


def _normalize_line_token(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", str(text or "")).strip().lower()


def _fallback_arrival_from_transit(
    run: dict[str, Any],
    transit_routes: list[dict[str, Any]] | None,
    to_name: str = "",
) -> str:
    if not transit_routes:
        return ""

    run_depart = _to_minutes(run.get("time"))
    run_line = _normalize_line_token(run.get("line", ""))
    to_name_base = _normalize_stop_name_base(to_name)

    best_score: int | None = None
    best_arrival: str = ""

    for route in transit_routes:
        dep = _to_minutes(str(route.get("departureTime") or ""))
        arr = _to_minutes(str(route.get("arrivalTime") or ""))
        if dep is None or arr is None:
            continue

        score = 0
        if run_depart is not None:
            diff = abs(dep - run_depart)
            if diff > 12 * 60:
                diff = (24 * 60) - diff
            score += diff

        if run_line:
            seg_lines = [
                _normalize_line_token((seg or {}).get("line", ""))
                for seg in (route.get("segments") or [])
            ]
            non_empty_seg_lines = [sl for sl in seg_lines if sl]
            line_hit = any(sl and (sl in run_line or run_line in sl) for sl in non_empty_seg_lines)
            if non_empty_seg_lines and not line_hit:
                score += 35

        if to_name_base:
            alight = _normalize_stop_name_base(route.get("alightingStop", ""))
            if alight and (to_name_base not in alight and alight not in to_name_base):
                score += 25

        if best_score is None or score < best_score:
            best_score = score
            best_arrival = _minutes_to_hhmm(arr)

    if best_score is None:
        return ""

    return best_arrival if best_score <= 70 else ""


# ---------------------------------------------------------------------------
# 公式バス位置情報API (BusLocationByCourseBusId) — スクレイピング不使用
# ---------------------------------------------------------------------------


def fetch_bus_location(
    course_sid: str,
    company_id: str,
    bus_id: str | int,
    keiyu_cd: str = "",
    daiya_sid: str = "",
    datetime_offset: int = 50,
    ttl_sec: int = 10,
) -> list[dict[str, Any]]:
    """公式APIからリアルタイムのバス位置情報を取得する。

    バスが運行中でない場合は空リストが返る。
    キャッシュTTLはデフォルト10秒（リアルタイム性を重視）。
    """
    cache_key = _cache_key(
        "bus_location",
        {
            "courseSid": course_sid,
            "companyId": company_id,
            "busId": str(bus_id),
            "keiyuCd": keiyu_cd,
            "daiyaSid": daiya_sid,
        },
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    params: dict[str, Any] = {
        "datetime": datetime_offset,
        "courseSid": course_sid,
        "companyId": company_id,
        "busId": bus_id,
        "keiyuCd": keiyu_cd,
        "daiyaSid": daiya_sid,
        "_": int(time.time() * 1000),
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{BASE_URL}/map/Approach",
    }
    try:
        resp = _HTTP.get(BUS_LOCATION_URL, params=params, headers=headers, timeout=12)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        data = []

    result = data if isinstance(data, list) else []
    return _cache_set(cache_key, result, max(ttl_sec, 1))


def _parse_ms_date(ms_date_str: str | None) -> str:
    """'/Date(1777093423753)/' 形式をISO8601文字列に変換する。失敗時は空文字。"""
    if not ms_date_str:
        return ""
    m = re.search(r"Date\(([-\d]+)\)", str(ms_date_str))
    if not m:
        return ""
    try:
        ts = int(m.group(1))
        if ts < 0:  # 未設定値 (-62135596800000 等)
            return ""
        dt = datetime.fromtimestamp(ts / 1000.0)
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        return ""


def parse_bus_location_data(data: list[dict[str, Any]]) -> dict[str, Any]:
    """BusLocationByCourseBusId APIのレスポンスから必要な情報を抽出する。

    Returns:
        {
            "gpsLat": float | None,
            "gpsLng": float | None,
            "gpsTime": str,          # ISO8601
            "courseName": str,
            "startTime": str,
            "currentStopName": str,  # 直近の通過停留所名
            "nextStopName": str,     # 次の停留所名
            "scheduledArrival": str, # 次停留所への定刻
            "delayMinutes": int | None,
        }
    """
    if not data:
        return {}

    entry = data[0]  # 1バスにつき1エントリが基本
    pos = entry.get("Position") or {}
    daiya = entry.get("Daiya") or {}
    course = daiya.get("Course") or {}

    # GPS座標（座標系は日本測地系の場合があるが、そのまま返す）
    gps_lat: float | None = None
    gps_lng: float | None = None
    try:
        gps_lat = float(pos["Latitude"])
        gps_lng = float(pos["Longitude"])
    except (KeyError, TypeError, ValueError):
        pass

    gps_time = _parse_ms_date(entry.get("GpsTime"))

    # 現在地：最後に通過した停留所
    passages = entry.get("Passages") or []
    passed = [p for p in passages if p.get("DAPsKbnCd") == 1]  # 1=通過済み
    current_stop_name = ""
    if passed:
        last_passed = passed[-1]
        st = last_passed.get("Station") or {}
        current_stop_name = st.get("ShortName") or st.get("Name") or ""

    # 次の停留所
    upcoming = [p for p in passages if p.get("DAPsKbnCd") != 1]
    next_stop_name = ""
    scheduled_arrival = ""
    if upcoming:
        next_p = upcoming[0]
        st = next_p.get("Station") or {}
        next_stop_name = st.get("ShortName") or st.get("Name") or ""
        sched = (next_p.get("Schedule") or {}).get("ScheduledTime") or {}
        scheduled_arrival = sched.get("Value") or ""

    passage_stops: list[dict[str, Any]] = []
    last_passed_index = -1
    first_upcoming_index = -1
    for passage in passages:
        station = passage.get("Station") or {}
        schedule = (passage.get("Schedule") or {}).get("ScheduledTime") or {}
        name = station.get("ShortName") or station.get("Name") or ""
        if not name:
            continue
        passed_flag = passage.get("DAPsKbnCd") == 1
        if passed_flag:
            last_passed_index = len(passage_stops)
        elif first_upcoming_index < 0:
            first_upcoming_index = len(passage_stops)
        passage_stops.append(
            {
                "name": name,
                "time": schedule.get("Value") or "",
                "passed": passed_flag,
                "rawStatus": passage.get("DAPsKbnCd"),
            }
        )

    for idx, stop in enumerate(passage_stops):
        stop["position"] = "upcoming"
        if stop.get("passed"):
            stop["position"] = "passed"
        if idx == last_passed_index:
            stop["position"] = "current"
        elif idx == first_upcoming_index:
            stop["position"] = "next"

    return {
        "gpsLat": gps_lat,
        "gpsLng": gps_lng,
        "gpsTime": gps_time,
        "courseName": course.get("Name") or "",
        "startTime": entry.get("StartTime") or "",
        "currentStopName": current_stop_name,
        "nextStopName": next_stop_name,
        "scheduledArrival": scheduled_arrival,
        "passageStops": passage_stops,
        "delayMinutes": None,  # 遅延情報は現在APIから独立して計算困難なのでNone
    }


def get_route_details(params: dict[str, Any]) -> list[dict[str, str]]:
    cache_key = _cache_key("route_detail", params)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    response = _HTTP.get(ROUTE_DETAIL_URL, params=params, timeout=12)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    table = soup.find("table", class_="timeTable03")
    if not table:
        return _cache_set(cache_key, [], 120)

    rows = table.find_all("tr")
    base_time = str(params.get("selectedTime") or "")
    last_time = base_time if _to_minutes(base_time) is not None else None
    stops: list[dict[str, str]] = []
    for row in rows:
        name_td = row.find("td", class_=["timeTableListLeft", "timeTableListLeft_On"])
        if not name_td:
            continue
        time_tds = row.find_all("td", class_=["timeTableListCenter", "timeTableListCenter_On"])
        time_text = ""
        for td in time_tds:
            candidate = td.get_text(strip=True)
            if re.search(r"\d{1,2}:\d{2}", candidate):
                time_text = candidate
                break
        if not time_text and len(time_tds) >= 2:
            time_text = time_tds[1].get_text(strip=True)
        normalized_time = _normalize_time_text(time_text, base_time, last_time)
        if normalized_time:
            last_time = normalized_time
        stops.append(
            {
                "name": name_td.get_text(strip=True),
                "time": normalized_time,
            }
        )
    return _cache_set(cache_key, stops, 120)


def _clock_diff_minutes(a: str | None, b: str | None) -> int | None:
    am = _to_minutes(a)
    bm = _to_minutes(b)
    if am is None or bm is None:
        return None
    diff = abs(am - bm)
    return min(diff, (24 * 60) - diff)


def _approach_bus_passed(bus: dict[str, Any]) -> bool:
    text = f"{bus.get('status', '')} {bus.get('location', '')}"
    return "通過済み" in text


def _route_has_stop_after(stops: list[dict[str, str]], start_index: int, targets: list[str]) -> bool:
    for stop in stops[start_index + 1:]:
        if _stop_name_matches(stop.get("name", ""), targets):
            return True
    return False


def _route_terminal_after(stops: list[dict[str, str]], start_index: int) -> str:
    for stop in reversed(stops[start_index + 1:]):
        name = str(stop.get("name") or "").strip()
        if name:
            return name
    return ""


def _fallback_approach_direction(bus: dict[str, Any]) -> dict[str, Any]:
    bus_info = bus.get("busInfo") or {}
    route_label = str(
        bus.get("officialRouteName")
        or bus.get("destination")
        or bus_info.get("destination")
        or bus_info.get("via")
        or bus.get("line")
        or ""
    ).strip()
    return {
        "key": _normalize_stop_name(route_label) or "unknown",
        "label": route_label or "行き先確認中",
        "officialRouteName": route_label,
        "goesToOitaStation": None,
        "terminalStop": "",
        "routeStops": [],
        "targetStopIndex": -1,
    }


def _classify_approach_bus_route(
    bus: dict[str, Any],
    stop_name: str,
    service_date: str,
) -> dict[str, Any]:
    bus_info = bus.get("busInfo") or {}
    station_sid = str(bus_info.get("stationSid") or "").strip()
    if not station_sid:
        return _fallback_approach_direction(bus)

    cache_key = _cache_key(
        "approach_route_class",
        {
            "sid": station_sid,
            "line": bus.get("line") or bus_info.get("line") or "",
            "time": bus.get("time") or "",
            "daiyaSid": bus_info.get("daiyaSid") or "",
            "date": service_date,
            "stop": stop_name,
        },
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    fallback = _fallback_approach_direction(bus)
    try:
        tt_html = fetch_timetable_data(station_sid, service_date)
        runs = parse_timetable_html(tt_html)
    except Exception:
        return _cache_set(cache_key, fallback, 60)

    bus_line = _normalize_line_token(str(bus.get("line") or bus_info.get("line") or ""))
    bus_time = str(bus.get("time") or "")
    bus_daiya = str(bus_info.get("daiyaSid") or "").strip()

    best: tuple[int, dict[str, Any]] | None = None
    for run in runs:
        run_line = _normalize_line_token(str(run.get("line") or ""))
        line_match = bool(bus_line and run_line and (bus_line == run_line or bus_line in run_line or run_line in bus_line))
        run_daiya = str((run.get("params") or {}).get("DaiyaSid") or "").strip()
        daiya_match = bool(bus_daiya and run_daiya and bus_daiya == run_daiya)
        diff = _clock_diff_minutes(bus_time, str(run.get("time") or ""))

        if not daiya_match and not line_match:
            continue
        if diff is None:
            diff = 30
        if not daiya_match and diff > 20:
            continue

        score = diff
        if line_match:
            score -= 20
        if daiya_match:
            score -= 50
        if best is None or score < best[0]:
            best = (score, run)

    if not best:
        return _cache_set(cache_key, fallback, 60)

    official_route = str(best[1].get("destination") or bus.get("destination") or "").strip()
    route_stops: list[dict[str, str]] = []
    target_index = -1
    terminal_stop = ""
    try:
        route_stops = get_route_details(best[1].get("params") or {})
        stop_targets = [stop_name]
        for idx, stop in enumerate(route_stops):
            if target_index < 0 and _stop_name_matches(stop.get("name", ""), stop_targets):
                target_index = idx
        if route_stops:
            terminal_stop = str(route_stops[-1].get("name") or "").strip()
    except Exception:
        route_stops = []
    result = {
        "key": f"{_normalize_line_token(str(best[1].get('line') or bus.get('line') or ''))}:{_normalize_stop_name(official_route)}",
        "label": official_route or str(bus.get("destination") or "").strip() or "行き先確認中",
        "officialRouteName": official_route,
        "goesToOitaStation": None,
        "terminalStop": terminal_stop,
        "routeStops": route_stops,
        "targetStopIndex": target_index,
    }

    return _cache_set(cache_key, result, 300)


def enrich_approach_route_classes(
    buses: list[dict[str, Any]],
    stop_name: str,
    service_date: str,
    limit: int = 200,
) -> None:
    ordered = sorted(
        buses,
        key=lambda b: (
            1 if _approach_bus_passed(b) else 0,
            _to_minutes(str(b.get("time") or "")) or 0,
        ),
    )
    enriched = 0
    for bus in ordered:
        if enriched >= max(limit, 1):
            direction = _fallback_approach_direction(bus)
        else:
            direction = _classify_approach_bus_route(bus, stop_name, service_date)
            enriched += 1

        bus["approachDirectionKey"] = direction.get("key") or "unknown"
        bus["approachDirectionLabel"] = direction.get("label") or "行き先確認中"
        if direction.get("officialRouteName"):
            bus["officialRouteName"] = direction.get("officialRouteName")
        bus["goesToOitaStation"] = direction.get("goesToOitaStation")
        bus["terminalStop"] = direction.get("terminalStop") or ""
        bus["routeStops"] = direction.get("routeStops") or []
        bus["targetStopIndex"] = direction.get("targetStopIndex", -1)


def _collect_runs_for_sid_pair(
    from_sid: str,
    from_info: dict[str, Any],
    to_info: dict[str, Any],
    from_name: str,
    to_name: str,
    unyou_date: str,
    search_mode: str,
    query_minutes: int | None,
    limit: int,
    transit_routes: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    tt_html = fetch_timetable_data(from_sid, unyou_date)
    all_runs = sorted(parse_timetable_html(tt_html), key=lambda r: _to_minutes(r.get("time")) or 0)
    pair_results: list[dict[str, Any]] = []

    for run in all_runs:
        if len(pair_results) >= max(limit, 1):
            break

        depart_minutes = _to_minutes(run.get("time"))
        if query_minutes is not None and search_mode != "arrive":
            if depart_minutes is None or depart_minutes < query_minutes:
                continue

        stops = get_route_details(run["params"])
        from_targets = [from_name, from_info.get("Text", "")]
        to_targets = [to_name, to_info.get("Text", "")]
        from_indexes = [i for i, s in enumerate(stops) if _stop_name_equals(s.get("name", ""), from_targets)]
        if not from_indexes:
            from_indexes = [i for i, s in enumerate(stops) if _stop_name_matches(s.get("name", ""), from_targets)]

        to_indexes = [i for i, s in enumerate(stops) if _stop_name_equals(s.get("name", ""), to_targets)]
        if not to_indexes:
            to_indexes = [i for i, s in enumerate(stops) if _stop_name_matches(s.get("name", ""), to_targets)]
        if not to_indexes:
            continue

        # Only accept destination stops that appear after the boarding stop to avoid reverse-direction matches.
        selected_from_index: int | None = None
        selected_to_index: int | None = None
        candidate_to_indexes: list[int] = []
        if from_indexes:
            # Prefer the origin occurrence whose stop time is closest to the timetable departure time.
            ordered_from_indexes = from_indexes
            if depart_minutes is not None:
                ordered_from_indexes = sorted(
                    from_indexes,
                    key=lambda idx: abs((_to_minutes(stops[idx].get("time", "")) or depart_minutes) - depart_minutes),
                )

            for from_index in ordered_from_indexes:
                indexes_after_from = [idx for idx in to_indexes if idx > from_index]
                if indexes_after_from:
                    selected_from_index = from_index
                    selected_to_index = indexes_after_from[0]
                    candidate_to_indexes = indexes_after_from
                    break

            if selected_to_index is None:
                continue
        else:
            selected_to_index = to_indexes[0]
            candidate_to_indexes = to_indexes

        arrival_time = stops[selected_to_index].get("time", "")
        arrival_minutes = _to_minutes(arrival_time)
        arrival_is_estimate = False

        from_ref_minutes: int | None = None
        if selected_from_index is not None:
            from_ref_minutes = _to_minutes(stops[selected_from_index].get("time", ""))

        route_time_aligned = True
        if depart_minutes is not None and from_ref_minutes is not None:
            depart_gap = from_ref_minutes - depart_minutes
            if depart_gap < 0:
                depart_gap += 24 * 60
            # If route-detail times are far from timetable departure, treat them as non-aligned.
            if depart_gap > 30:
                route_time_aligned = False

        if not route_time_aligned:
            arrival_time = ""
            arrival_minutes = None

        # Route detail pages can contain looped/duplicated stop rows.
        # Reconstruct arrival using stop-to-stop delta from selected departure stop when possible.
        if depart_minutes is not None and candidate_to_indexes and route_time_aligned:
            best_candidate: tuple[int, int, int] | None = None
            for idx in candidate_to_indexes[:6]:
                raw_to_minutes = _to_minutes(stops[idx].get("time", ""))
                if raw_to_minutes is None:
                    continue

                if from_ref_minutes is not None:
                    # Use relative travel minutes to avoid absolute-time drift in circular routes.
                    delta = raw_to_minutes - from_ref_minutes
                    if delta < 0:
                        delta += 24 * 60
                    est_arrival_minutes = (depart_minutes + delta) % (24 * 60)
                else:
                    delta = raw_to_minutes - depart_minutes
                    if delta < 0:
                        delta += 24 * 60
                    est_arrival_minutes = raw_to_minutes

                if best_candidate is None or delta < best_candidate[0]:
                    best_candidate = (delta, idx, est_arrival_minutes)

            if best_candidate:
                delta, best_idx, best_arrival_minutes = best_candidate
                if delta <= 120:
                    selected_to_index = best_idx
                    arrival_minutes = best_arrival_minutes
                    arrival_time = _minutes_to_hhmm(best_arrival_minutes)
                else:
                    arrival_time = ""
                    arrival_minutes = None

        if arrival_minutes is None and route_time_aligned:
            estimated_minutes = _estimate_missing_stop_time_minutes(stops, selected_to_index)
            if estimated_minutes is not None and depart_minutes is not None:
                travel_delta = estimated_minutes - depart_minutes
                if travel_delta < 0:
                    travel_delta += 24 * 60
                if 0 <= travel_delta <= 180:
                    arrival_minutes = estimated_minutes
                    arrival_time = _minutes_to_hhmm(estimated_minutes)
                    arrival_is_estimate = True
            elif estimated_minutes is not None:
                arrival_minutes = estimated_minutes
                arrival_time = _minutes_to_hhmm(estimated_minutes)
                arrival_is_estimate = True

        if arrival_minutes is None:
            fallback_arrival = _fallback_arrival_from_transit(
                run,
                transit_routes,
                to_name=to_name,
            )
            if fallback_arrival:
                arrival_minutes = _to_minutes(fallback_arrival)
                arrival_time = fallback_arrival
                arrival_is_estimate = True

        if query_minutes is not None and search_mode == "arrive":
            if arrival_minutes is None or arrival_minutes > query_minutes:
                continue

        pair_results.append(
            {
                "time": run["time"],
                "line": run["line"],
                "destination": run["destination"],
                "arrivalTime": arrival_time,
                "arrivalIsEstimate": arrival_is_estimate,
                "stops": stops,
            }
        )

    return pair_results


@app.after_request
def no_cache(response):
    path = request.path
    is_cached = True
    if path.startswith("/api/stops/approach") or path.startswith("/api/bus/location"):
        response.headers["Cache-Control"] = "public, s-maxage=20, stale-while-revalidate=40"
    elif path.startswith("/api/stops/directions"):
        response.headers["Cache-Control"] = "public, s-maxage=21600, stale-while-revalidate=86400"
    elif path.startswith("/api/stops/suggest") or path.startswith("/api/stops/nearby"):
        response.headers["Cache-Control"] = "public, s-maxage=3600, stale-while-revalidate=21600"
    elif path.startswith("/api/routes/search") or path.startswith("/api/routes/transit"):
        response.headers["Cache-Control"] = "public, s-maxage=60, stale-while-revalidate=120"
    else:
        is_cached = False
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    if not is_cached:
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


@app.route("/")
def root_index():
    return send_from_directory(STITCH_DIR, "index.html")


@app.route("/api/health")
def health():
    return jsonify({"ok": True, "service": "busnavi-server"})


@app.route("/api/stops/suggest")
def api_suggest_stops():
    q = request.args.get("q", "").strip()
    if len(q) < 1:
        return jsonify({"items": []})

    items = suggest_stops(q)
    return jsonify({"items": items})


@app.route("/api/stops/nearby")
def api_nearby_stops():
    lat_q = request.args.get("lat", "").strip()
    lng_q = request.args.get("lng", "").strip()
    strict = request.args.get("strict", "0").strip() == "1"
    items: list[dict[str, Any]] = []

    if lat_q and lng_q:
        try:
            lat = float(lat_q)
            lng = float(lng_q)
            items = nearby_stops_by_coords(lat, lng, allow_distance=1500, limit=6)
        except Exception:
            items = []

    if not items and not strict:
        # Fallback when geolocation is unavailable or upstream map search fails.
        items = suggest_stops("駅")[:6]

    return jsonify({"items": items})


@app.route("/api/stops/directions")
def api_stop_directions():
    stop_name = (request.args.get("stop") or "").strip()
    service_date = (request.args.get("serviceDate") or "").strip() or None
    if not stop_name:
        return jsonify({"error": "stop is required"}), 400

    items = get_stop_direction_options(stop_name, service_date)
    return jsonify({"stop": stop_name, "items": items})


@app.route("/api/stops/approach")
def api_stop_approach():
    stop_name = (request.args.get("stop") or "").strip()
    station_sid_q = (request.args.get("stationSid") or "").strip()
    station_code_q = (request.args.get("stationCode") or "").strip()
    company_id_q = (request.args.get("companyId") or "").strip()
    track_line_q = (request.args.get("trackLine") or "").strip()
    track_dest_q = (request.args.get("trackDestination") or "").strip()
    track_time_q = (request.args.get("trackTime") or "").strip()
    try:
        gps_limit_q = int(request.args.get("gpsLimit", "4"))
    except Exception:
        gps_limit_q = 4
    gps_limit_q = max(1, min(gps_limit_q, 10))
    refresh_q = (request.args.get("refresh") or "").strip().lower()
    force_refresh = refresh_q in {"1", "true", "yes", "on"}
    if request.headers.get("x-vercel-id") and force_refresh:
        force_refresh = False

    if station_sid_q:
        stop = {
            "name": stop_name or "指定のりば",
            "stationSid": station_sid_q,
            "stationCode": station_code_q,
            "companyId": company_id_q,
            "lat": None,
            "lng": None,
        }
    elif station_code_q and company_id_q:
        stop = {
            "name": stop_name or "指定バス停",
            "stationCode": station_code_q,
            "companyId": company_id_q,
            "lat": None,
            "lng": None,
        }
    else:
        if not stop_name:
            nearby = suggest_stops("駅")
            if not nearby:
                return jsonify({"error": "バス停候補が見つかりませんでした。"}), 404
            stop = nearby[0]
        else:
            stop = get_approach_stop_info(stop_name)
            if not stop:
                return jsonify({"error": "接近情報対象のバス停が見つかりませんでした。"}), 404

    # --- 全のりばのバスを取得（stationSid ベース）---
    stop_lat = stop.get("lat")
    stop_lng = stop.get("lng")
    explicit_stop_query = bool(stop_name)
    exact_named_stops: list[dict[str, Any]] = []
    if explicit_stop_query:
        norm_query = _normalize_stop_name(stop_name)
        norm_query_base = _normalize_stop_name_base(stop_name)

        def _load_approach_candidates() -> list[dict[str, Any]]:
            try:
                return suggest_stops(stop_name)
            except Exception:
                return []

        def _load_transit_candidates() -> list[dict[str, Any]]:
            try:
                return get_stop_candidates(stop_name, limit=12)
            except Exception:
                return []

        with ThreadPoolExecutor(max_workers=2) as executor:
            approach_future = executor.submit(_load_approach_candidates)
            transit_future = executor.submit(_load_transit_candidates)
            candidates_by_name = approach_future.result()
            transit_candidates = transit_future.result()

        merged_candidates: list[dict[str, Any]] = list(candidates_by_name)
        for tc in transit_candidates:
            merged_candidates.append(
                {
                    "name": tc.get("Text") or tc.get("Name") or "",
                    "stationCode": tc.get("StationCode") or "",
                    "companyId": tc.get("CompanyId") or tc.get("CompanyID") or "",
                    "lat": tc.get("Latitude"),
                    "lng": tc.get("Longitude"),
                    "stationSid": tc.get("StationSid") or "",
                }
            )

        seen_exact_keys: set[tuple[str, str, str, str, str]] = set()
        for c in [stop] + merged_candidates:
            c_name = str(c.get("name") or c.get("Text") or "").strip()
            if not c_name:
                continue
            if norm_query:
                exact_name = _normalize_stop_name(c_name)
                if exact_name != norm_query:
                    if not norm_query_base:
                        continue
                    if _normalize_stop_name_base(c_name) != norm_query_base:
                        continue
            c_code = str(c.get("stationCode") or c.get("StationCode") or "")
            c_comp = str(c.get("companyId") or c.get("CompanyId") or c.get("CompanyID") or "")
            c_lat = str(c.get("lat") or c.get("Latitude") or "")
            c_lng = str(c.get("lng") or c.get("Longitude") or "")
            c_sid = str(c.get("stationSid") or c.get("StationSid") or "")
            exact_key = (c_code, c_comp, c_lat, c_lng, c_sid)
            if exact_key in seen_exact_keys:
                continue
            seen_exact_keys.add(exact_key)
            exact_named_stops.append(
                {
                    "name": c_name,
                    "stationCode": c_code,
                    "companyId": c_comp,
                    "stationSid": c_sid,
                    "lat": c.get("lat") if c.get("lat") not in (None, "") else c.get("Latitude"),
                    "lng": c.get("lng") if c.get("lng") not in (None, "") else c.get("Longitude"),
                }
            )
    station_code = str(stop.get("stationCode") or station_code_q or "")
    company_id = str(stop.get("companyId") or company_id_q or "")
    all_buses: list[dict[str, Any]] = []
    stop_coords: dict[str, Any] = {}
    fetched_stop_refs: set[tuple[str, str, str]] = set()

    def _append_buses_from_html(html_text: str) -> None:
        nonlocal all_buses, stop_coords
        parsed_buses, parsed_coords = parse_approach_html(html_text)
        all_buses.extend(parsed_buses)
        if (not stop_coords.get("lat")) and parsed_coords.get("lat"):
            stop_coords = parsed_coords

    def _fetch_approach_ref(ref: tuple[str, str, str]) -> tuple[tuple[str, str, str], str | None]:
        e_code, e_comp, e_sid = ref
        try:
            if e_code and e_comp:
                return ref, fetch_approach_data(e_code, e_comp, force_refresh=force_refresh)
            if e_sid:
                return ref, fetch_approach_data_by_sid(e_sid, force_refresh=force_refresh)
        except Exception:
            return ref, None
        return ref, None

    def _fetch_approach_refs(refs: list[tuple[str, str, str]]) -> None:
        unique_refs: list[tuple[str, str, str]] = []
        seen_refs: set[tuple[str, str, str]] = set()
        for ref in refs:
            if ref in fetched_stop_refs or ref in seen_refs:
                continue
            seen_refs.add(ref)
            unique_refs.append(ref)
        if not unique_refs:
            return
        max_workers = min(APPROACH_FETCH_WORKERS, len(unique_refs))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for ref_key, html_text in executor.map(_fetch_approach_ref, unique_refs):
                if not html_text:
                    continue
                _append_buses_from_html(html_text)
                fetched_stop_refs.add(ref_key)

    if station_sid_q:
        try:
            sid_html = fetch_approach_data_by_sid(station_sid_q, force_refresh=force_refresh)
            _append_buses_from_html(sid_html)
            fetched_stop_refs.add(("", "", station_sid_q))
        except Exception:
            all_buses = []

    if explicit_stop_query and exact_named_stops:
        exact_refs = [
            (
                str(exact_stop.get("stationCode") or ""),
                str(exact_stop.get("companyId") or ""),
                str(exact_stop.get("stationSid") or ""),
            )
            for exact_stop in exact_named_stops
        ]
        _fetch_approach_refs(exact_refs)

        # 同名停留所の複数のりば（同一名・別SID）を追加探索して取りこぼしを防ぐ。
        extra_sid_candidates: list[str] = []
        seen_extra_sids: set[str] = set()
        for exact_stop in exact_named_stops:
            lat = exact_stop.get("lat")
            lng = exact_stop.get("lng")
            if lat in (None, "") or lng in (None, ""):
                continue
            try:
                nearby_sids = get_stop_sids_near(
                    float(lat),
                    float(lng),
                    stop_name,
                    radius_m=450,
                    strict_name=True,
                )
            except Exception:
                nearby_sids = []
            for sid in nearby_sids:
                sid_text = str(sid or "").strip()
                if not sid_text or sid_text in seen_extra_sids:
                    continue
                seen_extra_sids.add(sid_text)
                extra_sid_candidates.append(sid_text)

        _fetch_approach_refs([("", "", sid) for sid in extra_sid_candidates])

    if not all_buses and station_code and company_id:
        try:
            html = fetch_approach_data(station_code, company_id, force_refresh=force_refresh)
            _append_buses_from_html(html)
        except Exception:
            all_buses = []

    if not all_buses and stop_lat and stop_lng:
        try:
            sids = get_stop_sids_near(
                float(stop_lat),
                float(stop_lng),
                stop.get("name", stop_name),
                strict_name=explicit_stop_query,
            )
        except Exception:
            sids = []

        _fetch_approach_refs([("", "", str(sid or "")) for sid in sids])

    if not all_buses:
        # フォールバック: 旧 stationCode + companyId 方式
        station_code = stop.get("stationCode") or station_code_q
        company_id = stop.get("companyId") or company_id_q

        if (not station_code or not company_id) and stop_name:
            fallback_stop = get_approach_stop_info(stop_name)
            if fallback_stop:
                station_code = str(fallback_stop.get("stationCode") or station_code or "")
                company_id = str(fallback_stop.get("companyId") or company_id or "")

        if not station_code or not company_id:
            if station_sid_q:
                return jsonify({
                    "stop": stop.get("name", stop_name),
                    "stopCoords": stop_coords,
                    "buses": [],
                })
            return jsonify({"error": "バス停情報が不足しています。"}), 400
        html = fetch_approach_data(station_code, company_id, force_refresh=force_refresh)
        all_buses, stop_coords = parse_approach_html(html)
    else:
        # 全のりば分をマージし重複除去（time+line+destination で判定）
        seen_keys: set[tuple[Any, ...]] = set()
        deduped: list[dict[str, Any]] = []
        for bus in all_buses:
            key = (
                bus.get("time"),
                bus.get("line"),
                bus.get("destination"),
                bus.get("status"),
            )
            if key not in seen_keys:
                seen_keys.add(key)
                deduped.append(bus)
        all_buses = sorted(deduped, key=lambda b: str(b.get("time") or ""))

    buses = all_buses

    if not stop_coords.get("lat") and stop_lat and stop_lng:
        stop_coords = {"lat": stop_lat, "lng": stop_lng}

    try:
        enrich_approach_route_classes(
            buses,
            str(stop.get("name") or stop_name or ""),
            datetime.now().strftime("%Y/%m/%d"),
        )
    except Exception:
        for bus in buses:
            direction = _fallback_approach_direction(bus)
            bus["approachDirectionKey"] = direction.get("key") or "unknown"
            bus["approachDirectionLabel"] = direction.get("label") or "行き先確認中"
            bus["goesToOitaStation"] = direction.get("goesToOitaStation")
            bus["terminalStop"] = direction.get("terminalStop") or ""

    target_line_token = _normalize_line_token(track_line_q)
    target_dest_token = _normalize_stop_name_base(track_dest_q)
    target_time_min = _to_minutes(track_time_q)
    prioritized_buses = list(buses)
    if buses and (target_line_token or target_dest_token or target_time_min is not None):
        def _bus_score(bus: dict[str, Any]) -> int:
            score = 0
            bus_line_token = _normalize_line_token(str(bus.get("line") or ""))
            if target_line_token:
                if bus_line_token:
                    if target_line_token not in bus_line_token and bus_line_token not in target_line_token:
                        score += 40
                else:
                    score += 20

            bus_dest_token = _normalize_stop_name_base(str(bus.get("destination") or ""))
            if target_dest_token:
                if bus_dest_token:
                    if target_dest_token not in bus_dest_token and bus_dest_token not in target_dest_token:
                        score += 30
                else:
                    score += 15

            bus_time_min = _to_minutes(str(bus.get("time") or ""))
            if target_time_min is not None and bus_time_min is not None:
                diff = abs(bus_time_min - target_time_min)
                if diff > 12 * 60:
                    diff = (24 * 60) - diff
                score += min(diff, 90)
            return score

        prioritized_buses = sorted(buses, key=_bus_score)

    enrich_targets = prioritized_buses[:gps_limit_q]

    def _fetch_bus_location_for_enrich(bus: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        bus_info = bus.get("busInfo") or {}
        c_sid = bus_info.get("courseSid") or ""
        c_id = bus_info.get("companyId") or ""
        b_id = bus_info.get("busId") or ""
        d_sid = bus_info.get("daiyaSid") or ""
        k_cd = bus_info.get("keiyuCd") or ""

        if not (c_sid and c_id and b_id):
            return bus, None

        try:
            loc_data = fetch_bus_location(
                course_sid=c_sid,
                company_id=c_id,
                bus_id=b_id,
                keiyu_cd=k_cd,
                daiya_sid=d_sid,
            )
            return bus, parse_bus_location_data(loc_data)
        except Exception:
            return bus, None

    # 公式APIでバス位置情報をエンリッチ（運行中のバスのみ、失敗しても既存データで返す）
    if enrich_targets:
        max_workers = min(BUS_LOCATION_FETCH_WORKERS, len(enrich_targets))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for bus, loc in executor.map(_fetch_bus_location_for_enrich, enrich_targets):
                if not loc:
                    continue
                bus["gpsLat"] = loc.get("gpsLat")
                bus["gpsLng"] = loc.get("gpsLng")
                bus["gpsTime"] = loc.get("gpsTime") or ""
                bus["currentStopName"] = loc.get("currentStopName") or ""
                bus["nextStopName"] = loc.get("nextStopName") or ""
                bus["scheduledArrival"] = loc.get("scheduledArrival") or ""
                bus["passageStops"] = loc.get("passageStops") or []

    try:
        direction_options = get_stop_direction_options(str(stop.get("name") or stop_name or ""), datetime.now().strftime("%Y/%m/%d"))
    except Exception:
        direction_options = []

    return jsonify({
        "stop": stop.get("name", stop_name),
        "stopCoords": stop_coords,
        "directionOptions": direction_options,
        "buses": buses,
    })


@app.route("/api/bus/location")
def api_bus_location():
    """公式APIから特定バスのリアルタイム位置情報を直接取得する。

    Query params:
        courseSid  (必須)
        companyId  (必須)
        busId      (必須)
        keiyuCd    (省略可)
        daiyaSid   (省略可)
        datetime   (省略可, default=50)
    """
    course_sid = (request.args.get("courseSid") or "").strip()
    company_id = (request.args.get("companyId") or "").strip()
    bus_id = (request.args.get("busId") or "").strip()
    keiyu_cd = (request.args.get("keiyuCd") or "").strip()
    daiya_sid = (request.args.get("daiyaSid") or "").strip()
    datetime_offset = int(request.args.get("datetime", 50))

    if not (course_sid and company_id and bus_id):
        return jsonify({"error": "courseSid, companyId, busId は必須パラメータです。"}), 400

    raw = fetch_bus_location(
        course_sid=course_sid,
        company_id=company_id,
        bus_id=bus_id,
        keiyu_cd=keiyu_cd,
        daiya_sid=daiya_sid,
        datetime_offset=datetime_offset,
    )
    parsed = parse_bus_location_data(raw)
    return jsonify({
        "raw": raw,
        "parsed": parsed,
    })


@app.route("/api/routes/search", methods=["POST"])
def api_search_routes():
    body = request.get_json(silent=True) or {}
    from_name = (body.get("from") or "").strip()
    to_name = (body.get("to") or "").strip()
    limit = int(body.get("limit", 5))

    if not from_name or not to_name:
        return jsonify({"error": "出発バス停と到着バス停を入力してください。"}), 400

    from_candidates = get_stop_candidates(from_name)
    to_candidates = get_stop_candidates(to_name)
    if not from_candidates or not to_candidates:
        return jsonify({"error": "バス停が見つかりませんでした。候補から選択してください。"}), 404

    query_time = body.get("queryTime") or datetime.now().strftime("%Y/%m/%d %H:%M")
    unyou_date = body.get("serviceDate") or datetime.now().strftime("%Y/%m/%d")
    search_mode = (body.get("searchMode") or "depart").strip().lower()
    query_minutes = _to_minutes(query_time)
    max_sid_pairs = max(2, min(int(body.get("maxSidPairs", 12)), 24))
    deep_transfer_search = bool(body.get("deepTransferSearch", False))

    sid_pairs: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    seen_sid_pairs: set[tuple[str, str, str]] = set()
    per_from_pairs: list[list[tuple[dict[str, Any], dict[str, Any], str]]] = []
    pair_pool_limit = max_sid_pairs * 4
    pool_count = 0
    for f in from_candidates:
        local_pairs: list[tuple[dict[str, Any], dict[str, Any], str]] = []
        primary_sids = get_station_sid_candidates(f, from_name, radius_m=700, limit=max_sid_pairs * 2)
        for t in to_candidates:
            candidate_sids = list(primary_sids)
            transit_sid_pairs = get_sid_pairs_from_transit_search(f, t, query_time, max_pairs=max_sid_pairs)
            for transit_from_sid, _ in transit_sid_pairs:
                sid_text = str(transit_from_sid or "").strip()
                if sid_text:
                    candidate_sids.append(sid_text)

            for sid in candidate_sids:
                sid_text = str(sid or "").strip()
                if not sid_text:
                    continue
                pair_key = (sid_text, str(f.get("Text") or ""), str(t.get("Text") or ""))
                if pair_key in seen_sid_pairs:
                    continue
                seen_sid_pairs.add(pair_key)
                local_pairs.append((f, t, sid_text))
                pool_count += 1
                if pool_count >= pair_pool_limit:
                    break
            if pool_count >= pair_pool_limit:
                break
        if local_pairs:
            per_from_pairs.append(local_pairs)
        if pool_count >= pair_pool_limit:
            break

    # Interleave candidates by origin stop so one similarly named stop does not monopolize the pool.
    depth = 0
    while len(sid_pairs) < max_sid_pairs:
        progressed = False
        for bucket in per_from_pairs:
            if depth < len(bucket):
                sid_pairs.append(bucket[depth])
                progressed = True
                if len(sid_pairs) >= max_sid_pairs:
                    break
        if not progressed:
            break
        depth += 1

    results: list[dict[str, Any]] = []
    transfer_hints: list[dict[str, Any]] = []
    selected_from: dict[str, Any] = from_candidates[0]
    selected_to: dict[str, Any] = to_candidates[0]
    selected_from_sid: str | None = sid_pairs[0][2] if sid_pairs else None
    transit_routes_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}

    if sid_pairs:
        selected_from = sid_pairs[0][0]
        selected_to = sid_pairs[0][1]

    aggregated_runs: list[dict[str, Any]] = []

    for from_info, to_info, from_sid in sid_pairs:
        transit_key = (
            str(from_info.get("StationSid") or from_info.get("Text") or ""),
            str(to_info.get("StationSid") or to_info.get("Text") or ""),
        )
        if transit_key not in transit_routes_cache:
            try:
                transit_routes_cache[transit_key] = fetch_transit_routes(
                    from_info,
                    to_info,
                    query_time,
                    sort_type="Time",
                    search_kbn=1,
                    ttl_sec=20,
                )
            except Exception:
                transit_routes_cache[transit_key] = []

        pair_results = _collect_runs_for_sid_pair(
            from_sid=from_sid,
            from_info=from_info,
            to_info=to_info,
            from_name=from_name,
            to_name=to_name,
            unyou_date=unyou_date,
            search_mode=search_mode,
            query_minutes=query_minutes,
            limit=limit,
            transit_routes=transit_routes_cache.get(transit_key, []),
        )

        if pair_results:
            if not aggregated_runs:
                selected_from = from_info
                selected_to = to_info
                selected_from_sid = from_sid
            aggregated_runs.extend(pair_results)

    if aggregated_runs:
        deduped_map: dict[tuple[str, str, str], dict[str, Any]] = {}
        sorted_runs = sorted(aggregated_runs, key=lambda r: _to_minutes(r.get("time")) or 0)
        for run in sorted_runs:
            run_key = (
                str(run.get("time") or ""),
                str(run.get("line") or ""),
                str(run.get("destination") or ""),
            )
            prev = deduped_map.get(run_key)
            if not prev:
                deduped_map[run_key] = run
                continue

            prev_arr = _to_minutes(str(prev.get("arrivalTime") or ""))
            cur_arr = _to_minutes(str(run.get("arrivalTime") or ""))
            if prev_arr is None and cur_arr is not None:
                deduped_map[run_key] = run
                continue
            if prev_arr is None or cur_arr is None:
                continue

            prev_est = bool(prev.get("arrivalIsEstimate"))
            cur_est = bool(run.get("arrivalIsEstimate"))
            if prev_est and not cur_est:
                deduped_map[run_key] = run
                continue
            if cur_est and not prev_est:
                continue

            dep = _to_minutes(str(run.get("time") or ""))
            if dep is None:
                continue
            prev_delta = (prev_arr - dep) % (24 * 60)
            cur_delta = (cur_arr - dep) % (24 * 60)
            if cur_delta < prev_delta:
                deduped_map[run_key] = run

        deduped_runs = list(deduped_map.values())
        results = sorted(deduped_runs, key=lambda r: _to_minutes(r.get("time")) or 0)[: max(limit, 1)]

    fallback_type = ""
    if not results:
        from_lat = selected_from.get("Latitude")
        from_lng = selected_from.get("Longitude")
        if from_lat not in (None, "") and from_lng not in (None, ""):
            try:
                nearby_items = nearby_stops_by_coords(float(from_lat), float(from_lng), allow_distance=1500, limit=8)
                transfer_deadline = time.perf_counter() + 2.5
                for near in nearby_items:
                    near_name = str(near.get("name") or "").strip()
                    if not near_name:
                        continue
                    if _stop_name_matches(near_name, [from_name, selected_from.get("Text", "")]):
                        continue

                    best_run: dict[str, Any] | None = None
                    best_from: dict[str, Any] | None = None

                    # Fast mode (default): return walking alternatives quickly.
                    # Deep mode can be enabled explicitly when detailed transfer candidates are needed.
                    if deep_transfer_search and time.perf_counter() < transfer_deadline:
                        near_candidates = get_stop_candidates(near_name, limit=1)
                        for near_from in near_candidates:
                            for t in to_candidates[:2]:
                                if time.perf_counter() >= transfer_deadline:
                                    break
                                near_sid_candidates = get_station_sid_candidates(near_from, near_name, radius_m=500, limit=4)
                                near_transit_sid_pairs = get_sid_pairs_from_transit_search(
                                    near_from,
                                    t,
                                    query_time,
                                    max_pairs=2,
                                )
                                for near_transit_sid, _ in near_transit_sid_pairs:
                                    sid_text = str(near_transit_sid or "").strip()
                                    if sid_text:
                                        near_sid_candidates.append(sid_text)

                                near_seen_sids: set[str] = set()
                                for near_sid in near_sid_candidates:
                                    near_sid_text = str(near_sid or "").strip()
                                    if not near_sid_text or near_sid_text in near_seen_sids:
                                        continue
                                    near_seen_sids.add(near_sid_text)

                                    near_transit_key = (
                                        str(near_from.get("StationSid") or near_from.get("Text") or ""),
                                        str(t.get("StationSid") or t.get("Text") or ""),
                                    )
                                    if near_transit_key not in transit_routes_cache:
                                        try:
                                            transit_routes_cache[near_transit_key] = fetch_transit_routes(
                                                near_from,
                                                t,
                                                query_time,
                                                sort_type="Time",
                                                search_kbn=1,
                                                ttl_sec=20,
                                            )
                                        except Exception:
                                            transit_routes_cache[near_transit_key] = []

                                    near_runs = _collect_runs_for_sid_pair(
                                        from_sid=near_sid_text,
                                        from_info=near_from,
                                        to_info=t,
                                        from_name=near_name,
                                        to_name=to_name,
                                        unyou_date=unyou_date,
                                        search_mode=search_mode,
                                        query_minutes=query_minutes,
                                        limit=1,
                                        transit_routes=transit_routes_cache.get(near_transit_key, []),
                                    )
                                    if near_runs:
                                        best_run = near_runs[0]
                                        best_from = near_from
                                        break

                                if best_run:
                                    break
                            if best_run or time.perf_counter() >= transfer_deadline:
                                break

                    hint_stop_name = (best_from or {}).get("Text", near_name)

                    distance_m = float(near.get("distanceM") or 0)
                    walk_minutes = max(1, int(round(distance_m / 80.0)))
                    transfer_hints.append(
                        {
                            "walkTo": hint_stop_name,
                            "walkMinutes": walk_minutes,
                            "distanceM": round(distance_m, 1),
                            "hasDirectCandidate": bool(best_run),
                            "nextBus": {
                                "time": (best_run or {}).get("time", ""),
                                "line": (best_run or {}).get("line", ""),
                                "destination": (best_run or {}).get("destination", ""),
                                "arrivalTime": (best_run or {}).get("arrivalTime", ""),
                            },
                        }
                    )
                    if len(transfer_hints) >= 3:
                        break
            except Exception:
                transfer_hints = []

    if not results and not transfer_hints:
        station_code = str(selected_from.get("StationCode") or "")
        company_id = str(selected_from.get("CompanyId") or "")
        if station_code and company_id:
            try:
                approach_html = fetch_approach_data(station_code, company_id)
                approach_buses, _ = parse_approach_html(approach_html)
                fallback_runs: list[dict[str, Any]] = []
                for bus in approach_buses:
                    bus_time = _normalize_time_text(bus.get("time", ""), "", None)
                    bus_minutes = _to_minutes(bus_time)
                    if query_minutes is not None and search_mode != "arrive":
                        if bus_minutes is None or bus_minutes < query_minutes:
                            continue
                    if query_minutes is not None and search_mode == "arrive":
                        if bus_minutes is None or bus_minutes > query_minutes:
                            continue

                    fallback_runs.append(
                        {
                            "time": bus_time or bus.get("time", ""),
                            "line": bus.get("line", ""),
                            "destination": bus.get("destination", ""),
                            "arrivalTime": "",
                            "arrivalIsEstimate": False,
                            "stops": [],
                        }
                    )

                if fallback_runs:
                    fallback_type = "approach"
                    results = sorted(fallback_runs, key=lambda r: _to_minutes(r.get("time")) or 0)[: max(limit, 1)]
            except Exception:
                pass

    if transfer_hints and not results:
        fallback_type = "transfer"

    resolved_from = from_name
    resolved_to = to_name
    selected_from_text = str(selected_from.get("Text") or "").strip()
    selected_to_text = str(selected_to.get("Text") or "").strip()
    if selected_from_text and _normalize_stop_name(selected_from_text) == _normalize_stop_name(from_name):
        resolved_from = selected_from_text
    if selected_to_text and _normalize_stop_name(selected_to_text) == _normalize_stop_name(to_name):
        resolved_to = selected_to_text

    from_stop_ref = resolve_approach_stop_ref(
        resolved_from,
        selected_from.get("Latitude"),
        selected_from.get("Longitude"),
    )
    if selected_from_sid:
        from_stop_ref["stationSid"] = selected_from_sid
    to_stop_ref = resolve_approach_stop_ref(
        resolved_to,
        selected_to.get("Latitude"),
        selected_to.get("Longitude"),
    )

    return jsonify(
        {
            "from": resolved_from,
            "to": resolved_to,
            "date": unyou_date,
            "runs": results,
            "transferHints": transfer_hints,
            "fallback": fallback_type,
            "fromStop": from_stop_ref,
            "toStop": to_stop_ref,
        }
    )


@app.route("/api/routes/transit", methods=["POST"])
def api_transit_routes():
    """Transit/Result API 経由で経路候補を取得する（連鎖スクレイピングなし版）。

    Transit/Result を 1 回呼ぶだけで発着時刻・系統・運賃・停留所が揃う。
    TimeTableAll / RouteDetail への 2 次・3 次スクレイピングが不要。

    Request body (JSON):
        from      : 出発バス停名（必須）
        to        : 到着バス停名（必須）
        queryTime : "YYYY/MM/DD HH:MM" （省略時は現在時刻）
        sortType  : "Time" | "Price" | "Transfer" （省略時は Time）
        searchKbn : 1=出発時刻指定 / 2=到着時刻指定 （省略時は 1）
    """
    body = request.get_json(silent=True) or {}
    from_name = (body.get("from") or "").strip()
    to_name = (body.get("to") or "").strip()

    if not from_name or not to_name:
        return jsonify({"error": "出発バス停と到着バス停を入力してください。"}), 400

    from_candidates = get_stop_candidates(from_name, limit=4)
    to_candidates = get_stop_candidates(to_name, limit=4)
    if not from_candidates or not to_candidates:
        return jsonify({"error": "バス停が見つかりませんでした。候補から選択してください。"}), 404

    query_time: str = body.get("queryTime") or datetime.now().strftime("%Y/%m/%d %H:%M")
    sort_type = (body.get("sortType") or "Time").strip()
    search_kbn = int(body.get("searchKbn", 1))

    routes: list[dict[str, Any]] = []
    selected_from: dict[str, Any] = from_candidates[0]
    selected_to: dict[str, Any] = to_candidates[0]

    # 候補組を試し結果が出たら打ち切る
    for f in from_candidates:
        for t in to_candidates:
            try:
                result = fetch_transit_routes(f, t, query_time, sort_type, search_kbn)
            except Exception:
                result = []
            if result:
                routes = result
                selected_from = f
                selected_to = t
                break
        if routes:
            break

    if not routes:
        return jsonify(
            {
                "error": "該当する経路が見つかりませんでした。バス停名を確認して再度お試しください。",
                "from": from_name,
                "to": to_name,
            }
        ), 404

    return jsonify(
        {
            "from": selected_from.get("Text", from_name),
            "to": selected_to.get("Text", to_name),
            "queryTime": query_time,
            "sortType": sort_type,
            "routes": routes,
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5500, debug=True)
