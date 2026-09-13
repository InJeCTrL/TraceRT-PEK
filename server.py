#!/usr/bin/env python3
"""GCJ-02-only AMap route service with camera avoidance."""
import base64
import hashlib
import hmac
import json
import math
import mimetypes
import os
import random
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / 'web'
DATA_DIR = Path(os.environ.get('TRACERT_DATA_DIR', ROOT))
CAMERA_FILE = DATA_DIR / 'camera_points.json'
CAMERA_CORRECTIONS_FILE = DATA_DIR / 'camera_corrections.json'
AREA_FILE = ROOT / 'controlled_area_gcj.json'
STATUS_FILE = DATA_DIR / 'data_update_status.json'
DB_FILE = DATA_DIR / 'navigation_history.sqlite3'
ERROR_LOG = DATA_DIR / 'routing_errors.log'
HOST = os.environ.get('HOST', '0.0.0.0')
PORT = int(os.environ.get('PORT', '8765'))
AMAP_API_KEY = os.environ.get('AMAP_API_KEY', '')
AMAP_JS_KEY = os.environ.get('AMAP_JS_KEY', '')
AMAP_JS_SECURITY_CODE = os.environ.get('AMAP_JS_SECURITY_CODE', '')
AMAP_VEHICLE_PLATE = os.environ.get('AMAP_VEHICLE_PLATE', '冀X000000')
AMAP_VEHICLE_TYPE = os.environ.get('AMAP_VEHICLE_TYPE', '0')
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')
API_LIMIT = 180
CAMERA_RADIUS_M = 30
ROUTE_CACHE_TTL_S = 6 * 60 * 60
ROUTE_ALGORITHM = 'amap-static-2-v4'
GATEWAY_MARGIN_M = 600
GATEWAY_PROBE_LIMIT = 12
REQUEST_LIMIT = threading.BoundedSemaphore(2)
TRANSIENT_CODES = {'10003', '10004', '10015', '10016', '10020', '10021'}
SEARCH_CACHE, REGEOCODE_CACHE = {}, {}
DATA_LOCK, DB_LOCK, LOG_LOCK = threading.RLock(), threading.RLock(), threading.Lock()


class AmapBudgetExceeded(ValueError):
    pass


def load_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def camera_files_revision():
    source = CAMERA_FILE.stat().st_mtime_ns
    corrections = CAMERA_CORRECTIONS_FILE.stat().st_mtime_ns if CAMERA_CORRECTIONS_FILE.exists() else 0
    digest = hashlib.sha256(f'{source}:{corrections}'.encode()).digest()
    return int.from_bytes(digest[:8], 'big') & ((1 << 63) - 1)


def camera_id(name, duplicate_index=None):
    base = hashlib.sha256(name.strip().encode('utf-8')).hexdigest()[:20]
    return f'{base}-{duplicate_index}' if duplicate_index is not None else base


def load_camera_corrections():
    if not CAMERA_CORRECTIONS_FILE.exists():
        return {}
    value = load_json(CAMERA_CORRECTIONS_FILE)
    return value.get('items', {}) if isinstance(value, dict) else {}


def load_cameras():
    raw = load_json(CAMERA_FILE)
    groups = {}
    for index, camera in enumerate(raw):
        name = str(camera.get('name') or '').strip()
        groups.setdefault(name, []).append(index)
    duplicate_ranks = {}
    for name, indices in groups.items():
        if len(indices) > 1:
            ordered = sorted(indices, key=lambda index: (float(raw[index]['lon']), float(raw[index]['lat'])))
            duplicate_ranks.update({index: rank for rank, index in enumerate(ordered, 1)})
    corrections = load_camera_corrections()
    by_name = {}
    for correction in corrections.values():
        by_name.setdefault(correction.get('name'), []).append(correction)
    cameras = []
    for index, source in enumerate(raw):
        name = str(source.get('name') or '').strip()
        source_lon, source_lat = float(source['lon']), float(source['lat'])
        identifier = camera_id(name, duplicate_ranks.get(index))
        correction = corrections.get(identifier)
        if correction is None and len(groups[name]) == 1 and by_name.get(name):
            correction = min(by_name[name], key=lambda item: haversine(
                (source_lon, source_lat),
                (float(item.get('source_lon', source_lon)), float(item.get('source_lat', source_lat)))))
        camera = {'id': identifier, 'name': name, 'lon': source_lon, 'lat': source_lat,
                  'source_lon': source_lon, 'source_lat': source_lat, 'corrected': False}
        if correction is not None:
            camera.update({'lon': float(correction['lon']), 'lat': float(correction['lat']),
                           'corrected': True, 'corrected_at': correction.get('updated_at')})
        cameras.append(camera)
    return cameras


CAMERAS = []
AREA = load_json(AREA_FILE)
CAMERA_MTIME = 0


def haversine(a, b):
    lon1, lat1, lon2, lat2 = map(math.radians, (*a, *b))
    dlon, dlat = lon2-lon1, lat2-lat1
    value = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    return 12742000 * math.asin(math.sqrt(value))


def point_segment_distance(point, start, end):
    scale_x = 111320 * math.cos(math.radians(point[1]))
    px, py = point[0]*scale_x, point[1]*111320
    ax, ay = start[0]*scale_x, start[1]*111320
    bx, by = end[0]*scale_x, end[1]*111320
    dx, dy = bx-ax, by-ay
    ratio = 0 if dx == dy == 0 else max(0, min(1, ((px-ax)*dx+(py-ay)*dy)/(dx*dx+dy*dy)))
    return math.hypot(px-(ax+ratio*dx), py-(ay+ratio*dy))


def point_in_ring(point, ring):
    x, y, inside = point[0], point[1], False
    for a, b in zip(ring, ring[1:]):
        if (a[1] > y) != (b[1] > y):
            if x < (b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0]:
                inside = not inside
    return inside


def in_polygon(point, rings):
    return bool(rings and point_in_ring(point, rings[0]) and
                not any(point_in_ring(point, ring) for ring in rings[1:]))


def in_controlled_area(point):
    return in_polygon(point, AREA['sixth_ring']) or in_polygon(point, AREA['tongzhou'])


def controlled_area_distance(point):
    if in_controlled_area(point):
        return 0.0
    return min(point_segment_distance(point, a, b)
               for name in ('sixth_ring', 'tongzhou') for ring in AREA[name]
               for a, b in zip(ring, ring[1:]))


def inside_second_ring(point):
    rings = AREA.get('second_ring', [])
    return in_polygon(point, rings) or any(point_segment_distance(point, a, b) <= 35
        for ring in rings for a, b in zip(ring, ring[1:]))


def build_camera_grid(cameras, cell=.01):
    grid = {}
    for index, camera in enumerate(cameras):
        point = (float(camera['lon']), float(camera['lat']))
        grid.setdefault((math.floor(point[0]/cell), math.floor(point[1]/cell)), []).append(
            (f"camera:{camera.get('id', index)}", point, camera, CAMERA_RADIUS_M))
    return grid


CAMERA_GRID = {}


def refresh_runtime_data():
    global CAMERAS, CAMERA_GRID, CAMERA_MTIME
    with DATA_LOCK:
        revision = camera_files_revision()
        if revision != CAMERA_MTIME:
            CAMERAS = load_cameras()
            CAMERA_GRID = build_camera_grid(CAMERAS)
            CAMERA_MTIME = revision


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def save_camera_correction(identifier, lon, lat):
    global CAMERA_MTIME
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise ValueError('修正坐标无效')
    with DATA_LOCK:
        refresh_runtime_data()
        camera = next((item for item in CAMERAS if item['id'] == identifier), None)
        if camera is None:
            raise KeyError(identifier)
        value = load_json(CAMERA_CORRECTIONS_FILE) if CAMERA_CORRECTIONS_FILE.exists() else {'version': 1, 'items': {}}
        value.setdefault('items', {})[identifier] = {
            'name': camera['name'],
            'source_lon': camera['source_lon'],
            'source_lat': camera['source_lat'],
            'lon': float(lon),
            'lat': float(lat),
            'updated_at': datetime.now().astimezone().isoformat(timespec='seconds'),
        }
        atomic_json(CAMERA_CORRECTIONS_FILE, value)
        CAMERA_MTIME = 0
        refresh_runtime_data()
        return next(item for item in CAMERAS if item['id'] == identifier)


refresh_runtime_data()


def consume(stats):
    if stats['calls'] >= stats['limit']:
        raise AmapBudgetExceeded(f"高德API调用达到本次上限：{stats['limit']}次")
    stats['calls'] += 1


def amap_json(endpoint, params, stats, timeout=20):
    if not AMAP_API_KEY:
        raise ValueError('未配置高德 Web 服务 Key')
    encoded = urllib.parse.urlencode({'key': AMAP_API_KEY, 'output': 'json', **params})
    last_error = None
    for attempt in range(5):
        consume(stats)
        try:
            request = (urllib.request.Request(endpoint, data=encoded.encode(), headers={
                'User-Agent': 'TraceRT-PEK/2.0', 'Content-Type': 'application/x-www-form-urlencoded'})
                if len(encoded) > 1800 else urllib.request.Request(
                    endpoint+'?'+encoded, headers={'User-Agent': 'TraceRT-PEK/2.0'}))
            with REQUEST_LIMIT, urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.load(response)
            if data.get('status') == '1':
                return data
            last_error = ValueError(data.get('info') or '高德接口返回失败')
            if str(data.get('infocode', '')) not in TRANSIENT_CODES and 'LIMIT' not in str(data.get('info', '')):
                break
        except AmapBudgetExceeded:
            raise
        except Exception as error:
            last_error = error
        if attempt < 4:
            time.sleep(.35*(2**attempt)+random.uniform(0, .2))
    raise ValueError(str(last_error or '高德接口不可用'))


def amap_driving(start, end, avoid_points, stats, strategy='2', return_all=False):
    polygons = []
    for lon, lat in avoid_points[:100]:
        dy = CAMERA_RADIUS_M/111320
        dx = CAMERA_RADIUS_M/(111320*max(.2, math.cos(math.radians(lat))))
        polygons.append(';'.join(f'{x:.6f},{y:.6f}' for x, y in (
            (lon-dx, lat-dy), (lon+dx, lat-dy), (lon+dx, lat+dy), (lon-dx, lat+dy))))
    params = {'origin': f'{start[0]:.6f},{start[1]:.6f}',
              'destination': f'{end[0]:.6f},{end[1]:.6f}', 'strategy': strategy,
              'show_fields': 'cost,polyline,navi', 'plate': AMAP_VEHICLE_PLATE,
              'cartype': AMAP_VEHICLE_TYPE, 'alternative_route': '1' if return_all else '0'}
    if polygons:
        params['avoidpolygons'] = '|'.join(polygons)
    data = amap_json('https://restapi.amap.com/v5/direction/driving', params, stats)
    results = []
    for path in data.get('route', {}).get('paths', []):
        points, maneuvers = [], []
        for step in path.get('steps', []):
            step_points = []
            for text in step.get('polyline', '').split(';'):
                if text:
                    point = tuple(map(float, text.split(',')))
                    if not step_points or point != step_points[-1]:
                        step_points.append(point)
                    if not points or point != points[-1]:
                        points.append(point)
            navi = step.get('navi') if isinstance(step.get('navi'), dict) else {}
            action = str(navi.get('action') or navi.get('assistant_action') or '').strip()
            if step_points and action and action not in {'直行', '到达目的地'}:
                maneuvers.append({'coordinate': list(step_points[-1]), 'action': action,
                                  'road_name': str(step.get('road_name') or '')})
        if len(points) >= 2:
            results.append({'coordinates': points, 'distance_m': float(path.get('distance') or 0),
                            'duration_s': float(path.get('cost', {}).get('duration') or 0),
                            'restriction': str(path.get('restriction') or ''),
                            'maneuvers': maneuvers})
    if not results:
        raise ValueError('高德没有返回可用路线')
    return results


def route_conflicts(polyline, manual_avoid=()):
    manual = [(f'manual:{index}', tuple(map(float, point)),
               {'name': f'手动规避点{index+1}', 'lon': point[0], 'lat': point[1]}, CAMERA_RADIUS_M)
              for index, point in enumerate(manual_avoid)]
    found, progress, cell = {}, 0.0, .01
    for a, b in zip(polyline, polyline[1:]):
        pad_lat = CAMERA_RADIUS_M/111320
        pad_lon = CAMERA_RADIUS_M/(111320*max(.2, math.cos(math.radians((a[1]+b[1])/2))))
        left, right = min(a[0], b[0])-pad_lon, max(a[0], b[0])+pad_lon
        bottom, top = min(a[1], b[1])-pad_lat, max(a[1], b[1])+pad_lat
        candidates = list(manual)
        for gx in range(math.floor(left/cell), math.floor(right/cell)+1):
            for gy in range(math.floor(bottom/cell), math.floor(top/cell)+1):
                candidates.extend(CAMERA_GRID.get((gx, gy), ()))
        for key, point, info, radius in candidates:
            if key not in found and left <= point[0] <= right and bottom <= point[1] <= top:
                if point_segment_distance(point, a, b) <= radius:
                    found[key] = (point, info, progress)
        progress += haversine(a, b)
    return dict(sorted(found.items(), key=lambda item: item[1][2]))


def point_at_distance(polyline, target):
    travelled = 0.0
    for a, b in zip(polyline, polyline[1:]):
        length = haversine(a, b)
        if travelled+length >= target and length:
            ratio = (target-travelled)/length
            return (a[0]+(b[0]-a[0])*ratio, a[1]+(b[1]-a[1])*ratio)
        travelled += length
    return polyline[-1]


def seam_has_uturn(first, second, radius_m=800):
    """Reject split points that make either sub-route snap to the opposite carriageway."""
    join = tuple(first['coordinates'][-1])
    for route in (first, second):
        for maneuver in route.get('maneuvers', []):
            if '调头' in maneuver.get('action', ''):
                point = maneuver.get('coordinate')
                if point and haversine(join, point) <= radius_m:
                    return True
    return False


def safe_route(start, end, manual_avoid, stats, max_calls=80, depth=0, initial_avoid=None):
    """Iteratively feed every conflict on an AMap route back as avoid polygons."""
    phase_start = stats['calls']
    avoided = dict(initial_avoid or {})
    last = None
    for _ in range(14):
        if stats['calls']-phase_start >= max_calls:
            break
        options = amap_driving(start, end, [item[0] for item in avoided.values()], stats)
        evaluated = [(route_conflicts(item['coordinates'], manual_avoid), item) for item in options]
        conflicts, candidate = min(evaluated, key=lambda item: (
            len(item[0]), item[1]['duration_s'], item[1]['distance_m']))
        last = (conflicts, candidate)
        if not conflicts:
            candidate['coordinates'] = [list(point) for point in candidate['coordinates']]
            return candidate
        new_items = [(key, value) for key, value in conflicts.items() if key not in avoided]
        room = 100-len(avoided)
        if new_items and room:
            for key, value in new_items[:room]:
                avoided[key] = value
            continue
        if depth < 4:
            first_progress = next(iter(conflicts.values()))[2]
            total = sum(haversine(a, b) for a, b in zip(candidate['coordinates'], candidate['coordinates'][1:]))
            fallback = None
            for backoff in (1000, 1500, 700, 350):
                split_at = max(250, first_progress-backoff)
                if split_at >= total-250:
                    continue
                checkpoint = point_at_distance(candidate['coordinates'], split_at)
                try:
                    first = safe_route(start, checkpoint, manual_avoid, stats, max(12, max_calls//2), depth+1)
                    second = safe_route(checkpoint, end, manual_avoid, stats, max(12, max_calls//2), depth+1)
                except AmapBudgetExceeded:
                    raise
                except ValueError:
                    continue
                coordinates = first['coordinates']+second['coordinates'][1:]
                if route_conflicts(coordinates, manual_avoid):
                    continue
                combined = {'coordinates': coordinates,
                            'distance_m': sum(haversine(a, b) for a, b in zip(coordinates, coordinates[1:])),
                            'duration_s': first['duration_s']+second['duration_s'], 'restriction': '',
                            'maneuvers': first.get('maneuvers', [])+second.get('maneuvers', [])}
                if not seam_has_uturn(first, second):
                    return combined
                if fallback is None or combined['distance_m'] < fallback['distance_m']:
                    fallback = combined
            if fallback is not None:
                return fallback
        break
    names = [] if not last else [value[1].get('name', '未知点') for value in last[0].values()]
    raise ValueError('高德未找到零冲突路线'+(f"：{'、'.join(names[:3])}" if names else ''))


def safe_route_with_start_access(start, end, manual_avoid, stats, max_calls=180):
    """Retry through a nearby AMap-drivable access point when origin snapping is unstable."""
    phase_start = stats['calls']
    try:
        return safe_route(start, end, manual_avoid, stats, min(75, max_calls))
    except ValueError as direct_error:
        errors = [str(direct_error)]
    bearings = (90, 135, 180, 45, 0, 270, 225, 315)
    for radius in (88, 160, 250):
        for bearing in bearings:
            remaining = min(max_calls-(stats['calls']-phase_start), stats['limit']-stats['calls'])
            if remaining < 5:
                break
            angle = math.radians(bearing)
            checkpoint = (
                start[0]+math.sin(angle)*radius/(111320*math.cos(math.radians(start[1]))),
                start[1]+math.cos(angle)*radius/111320,
            )
            try:
                prefix = safe_route(start, checkpoint, manual_avoid, stats, min(12, remaining-2))
                if prefix['distance_m'] > 3000:
                    continue
                remaining = min(max_calls-(stats['calls']-phase_start), stats['limit']-stats['calls'])
                if remaining < 2:
                    break
                suffix = safe_route(checkpoint, end, manual_avoid, stats, min(60, remaining))
                coordinates = prefix['coordinates']+suffix['coordinates'][1:]
                if route_conflicts(coordinates, manual_avoid):
                    continue
                return {
                    'coordinates': coordinates,
                    'distance_m': prefix['distance_m']+suffix['distance_m'],
                    'duration_s': prefix['duration_s']+suffix['duration_s'],
                    'restriction': '',
                    'maneuvers': prefix.get('maneuvers', [])+suffix.get('maneuvers', []),
                    'start_access_repair': {'checkpoint': list(checkpoint), 'radius_m': radius},
                }
            except AmapBudgetExceeded:
                raise
            except ValueError as error:
                errors.append(str(error))
        if stats['calls'] >= stats['limit'] or stats['calls']-phase_start >= max_calls:
            break
    raise ValueError(errors[-1])


def gateway_candidates(point):
    candidates = []
    for area_name in ('sixth_ring', 'tongzhou'):
        for ring in AREA[area_name]:
            step = max(1, len(ring)//90)
            ranked = sorted(range(0, len(ring)-1, step), key=lambda i: haversine(point, ring[i]))[:18]
            for index in ranked:
                vertex = ring[index]
                prev, nxt = ring[(index-1) % (len(ring)-1)], ring[(index+1) % (len(ring)-1)]
                tx = (nxt[0]-prev[0])*math.cos(math.radians(vertex[1])); ty = nxt[1]-prev[1]
                length = math.hypot(tx, ty) or 1
                for nx, ny in ((-ty/length, tx/length), (ty/length, -tx/length)):
                    for offset in (900, 1400, 2200):
                        candidate = (vertex[0]+nx*offset/(111320*math.cos(math.radians(vertex[1]))),
                                     vertex[1]+ny*offset/111320)
                        if not in_controlled_area(candidate) and controlled_area_distance(candidate) >= 500:
                            candidates.append(candidate); break
    unique = {(round(p[0], 5), round(p[1], 5)): p for p in candidates}
    return sorted(unique.values(), key=lambda candidate: haversine(point, candidate))


def recent_gateway_candidates(point):
    """Prefer boundary exits that have already produced a complete safe route."""
    candidates = []
    with DB_LOCK, db() as connection:
        rows = connection.execute(
            'SELECT start_lon,start_lat,result_json FROM navigation_records '
            'WHERE success=1 AND end_lon IS NULL AND result_json IS NOT NULL '
            'ORDER BY created_at DESC LIMIT 100').fetchall()
    for row in rows:
        try:
            result = json.loads(row['result_json'])
            coordinates = result.get('coordinates') or []
            candidate = tuple(map(float, coordinates[-1]))
        except (ValueError, TypeError, IndexError, json.JSONDecodeError):
            continue
        if not in_controlled_area(candidate):
            origin = (float(row['start_lon']), float(row['start_lat']))
            candidates.append((haversine(point, origin), candidate))
    unique = {}
    for origin_distance, candidate in candidates:
        key = (round(candidate[0], 5), round(candidate[1], 5))
        if key not in unique or origin_distance < unique[key][0]:
            unique[key] = (origin_distance, candidate)
    return [item[1] for item in sorted(unique.values(), key=lambda item: (item[0], haversine(point, item[1])))]


def bearing(a, b):
    lon1, lat1, lon2, lat2 = map(math.radians, (*a, *b))
    return (math.degrees(math.atan2(math.sin(lon2-lon1)*math.cos(lat2),
        math.cos(lat1)*math.sin(lat2)-math.sin(lat1)*math.cos(lat2)*math.cos(lon2-lon1)))+360) % 360


def bearing_difference(first, second):
    return abs((first-second+180) % 360-180)


def route_piece(route, start_index, end_index):
    coordinates = [list(point) for point in route['coordinates'][start_index:end_index+1]]
    if len(coordinates) < 2:
        return None
    distance = sum(haversine(a, b) for a, b in zip(coordinates, coordinates[1:]))
    full_distance = max(1.0, float(route.get('distance_m') or distance))
    maneuvers = []
    for maneuver in route.get('maneuvers', []):
        point = maneuver.get('coordinate')
        if point and any(point_segment_distance(point, a, b) <= 45
                         for a, b in zip(coordinates, coordinates[1:])):
            maneuvers.append(maneuver)
    return {'coordinates': coordinates, 'distance_m': distance,
            'duration_s': float(route.get('duration_s') or 0)*distance/full_distance,
            'restriction': str(route.get('restriction') or ''), 'maneuvers': maneuvers}


def outbound_gateway_piece(route):
    """Keep only the AMap road prefix through the first safely-outside point."""
    coordinates = route.get('coordinates') or []
    for index, point in enumerate(coordinates[1:], 1):
        if not in_controlled_area(point) and controlled_area_distance(point) >= GATEWAY_MARGIN_M:
            return route_piece(route, 0, index)
    return None


def ordered_gateway_targets(point, direction_hint=None):
    preferred_bearing = bearing(point, direction_hint) if direction_hint else None
    raw = gateway_candidates(point)+recent_gateway_candidates(point)
    unique = {(round(candidate[0], 5), round(candidate[1], 5)): candidate for candidate in raw}
    candidates = list(unique.values())
    directional = sorted(candidates, key=lambda candidate: (
        bearing_difference(bearing(point, candidate), preferred_bearing) if preferred_bearing is not None else 0,
        haversine(point, candidate)))
    by_distance = sorted(candidates, key=lambda candidate: haversine(point, candidate))
    sectors = {}
    for candidate in by_distance:
        sectors.setdefault(int((bearing(point, candidate)+22.5)//45) % 8, candidate)
    sector_representatives = list(sectors.values())
    sector_representatives.sort(key=lambda candidate: (
        bearing_difference(bearing(point, candidate), preferred_bearing)
        if preferred_bearing is not None else haversine(point, candidate)))
    priority = ((directional[:6]+sector_representatives+directional)
                if preferred_bearing is not None else (sector_representatives+by_distance))
    selected = []
    for candidate in priority:
        if candidate in selected:
            continue
        if all(haversine(candidate, existing) >= 650 for existing in selected):
            selected.append(candidate)
        if len(selected) >= GATEWAY_PROBE_LIMIT:
            break
    if len(selected) < GATEWAY_PROBE_LIMIT:
        for candidate in priority:
            if candidate not in selected:
                selected.append(candidate)
            if len(selected) >= GATEWAY_PROBE_LIMIT:
                break
    return selected


def discover_exit_candidates(start, manual_avoid, stats, direction_hint=None):
    """Probe actual AMap roads and derive gateways from their boundary crossings."""
    errors, candidates = [], {}
    preferred_bearing = bearing(start, direction_hint) if direction_hint else None
    for target_rank, target in enumerate(ordered_gateway_targets(start, direction_hint)):
        if stats['limit']-stats['calls'] < 2:
            break
        try:
            routes = amap_driving(start, target, [], stats, return_all=True)
        except AmapBudgetExceeded as error:
            errors.append(str(error)); break
        except ValueError as error:
            errors.append(str(error)); continue
        for route in routes:
            piece = outbound_gateway_piece(route)
            if piece is None:
                continue
            gateway = tuple(piece['coordinates'][-1])
            conflicts = route_conflicts(piece['coordinates'], manual_avoid)
            first_conflict = next(iter(conflicts.values()))[2] if conflicts else piece['distance_m']
            direction_cost = (bearing_difference(bearing(start, gateway), preferred_bearing)
                              if preferred_bearing is not None else 0)
            score = (len(conflicts), direction_cost, -first_conflict, piece['distance_m'], target_rank)
            key = (round(gateway[0], 4), round(gateway[1], 4))
            candidate = {'gateway': gateway, 'route': piece, 'conflicts': conflicts, 'score': score}
            if key not in candidates or score < candidates[key]['score']:
                candidates[key] = candidate
    return sorted(candidates.values(), key=lambda item: item['score']), errors


def solve_exit_candidate(start, candidate, manual_avoid, stats, max_calls):
    if not candidate['conflicts']:
        result = candidate['route']
    else:
        result = safe_route(start, candidate['gateway'], manual_avoid, stats, max_calls,
                            initial_avoid=candidate['conflicts'])
    endpoint = tuple(result['coordinates'][-1])
    if in_controlled_area(endpoint) or controlled_area_distance(endpoint) < GATEWAY_MARGIN_M*.75:
        raise ValueError('高德路线没有到达规避区域外安全距离')
    if route_conflicts(result['coordinates'], manual_avoid):
        raise ValueError('接驳路线仍经过规避点')
    result['gateway'] = list(endpoint)
    return result


def solve_exit(start, manual_avoid, stats, direction_hint=None):
    candidates, errors = discover_exit_candidates(start, manual_avoid, stats, direction_hint)
    if not candidates:
        raise ValueError(errors[-1] if errors else '没有生成可用的区域外道路接驳点')
    for candidate in candidates:
        remaining = stats['limit']-stats['calls']
        if remaining < 2:
            errors.append(f"高德API调用达到本次上限：{stats['limit']}次")
            break
        try:
            return solve_exit_candidate(start, candidate, manual_avoid, stats, min(30, remaining))
        except AmapBudgetExceeded as error:
            errors.append(str(error)); break
        except ValueError as error:
            errors.append(str(error))
    raise ValueError(errors[-1] if errors else '候选接驳路线均会经过规避点')


def solve_entry(start, end, manual_avoid, stats):
    """Generate exits from the inside destination, then validate the inbound direction."""
    candidates, errors = discover_exit_candidates(end, manual_avoid, stats, start)
    if not candidates:
        raise ValueError(errors[-1] if errors else '没有生成可用的进城道路接驳点')
    for candidate in candidates:
        remaining = stats['limit']-stats['calls']
        if remaining < 4:
            errors.append(f"高德API调用达到本次上限：{stats['limit']}次")
            break
        try:
            outbound = solve_exit_candidate(end, candidate, manual_avoid, stats, min(24, remaining-2))
            gateway = tuple(outbound['coordinates'][-1])
            remaining = stats['limit']-stats['calls']
            if remaining < 2:
                raise AmapBudgetExceeded(f"高德API调用达到本次上限：{stats['limit']}次")
            inbound = safe_route(gateway, end, manual_avoid, stats, min(34, remaining))
            if route_conflicts(inbound['coordinates'], manual_avoid):
                raise ValueError('进城接驳路线仍经过规避点')
            inbound['gateway'] = list(gateway)
            return inbound
        except AmapBudgetExceeded as error:
            errors.append(str(error)); break
        except ValueError as error:
            errors.append(str(error))
    raise ValueError(errors[-1] if errors else '候选进城接驳路线均会经过规避点')


def gateway_label(point, stats):
    key = (round(point[0], 5), round(point[1], 5))
    if key in REGEOCODE_CACHE:
        consume(stats); return REGEOCODE_CACHE[key]
    data = amap_json('https://restapi.amap.com/v3/geocode/regeo', {
        'location': f'{point[0]:.6f},{point[1]:.6f}', 'extensions': 'base', 'radius': 500}, stats, 15)
    label = data.get('regeocode', {}).get('formatted_address') or '规避区域外安全点'
    REGEOCODE_CACHE[key] = label
    return label


def finish_result(result, stats, provider, mode, notice, area_state, transition=None):
    result.update({'provider': provider, 'mode': mode, 'notice': notice, 'area_state': area_state,
                   'amap_api_calls': stats['calls'], 'coordinate_system': 'GCJ-02',
                   'route_algorithm': ROUTE_ALGORITHM})
    if transition:
        result['area_transition'] = transition
    return result


class AlreadyOutsideError(ValueError):
    pass


def calculate_route_uncached(start, end=None, manual_avoid=()):
    refresh_runtime_data()
    if inside_second_ring(start) or (end is not None and inside_second_ring(end)):
        raise ValueError('起点或终点位于二环以内（含二环）')
    stats = {'calls': 0, 'limit': API_LIMIT}
    manual_avoid = [tuple(map(float, point)) for point in manual_avoid]
    if end is None:
        if not in_controlled_area(start):
            raise AlreadyOutsideError('起点已在规避区域外，无需一键出六环。')
        result = solve_exit(start, manual_avoid, stats)
        return finish_result(result, stats, 'amap-avoidance', 'outside',
                             '已找到从起点离开规避区域的安全路线。', 'inside-to-gateway')
    start_inside, end_inside = in_controlled_area(start), in_controlled_area(end)
    buffer_m = float(AREA.get('entry_buffer_m', 5000))
    start_area_distance = 0.0 if start_inside else controlled_area_distance(start)
    end_area_distance = 0.0 if end_inside else controlled_area_distance(end)
    start_in_buffer = not start_inside and start_area_distance <= buffer_m
    end_in_buffer = not end_inside and end_area_distance <= buffer_m
    if start_inside and end_inside:
        result = safe_route_with_start_access(start, end, manual_avoid, stats, API_LIMIT)
        return finish_result(result, stats, 'amap-avoidance', 'point',
                             '已找到不经过规避点的路线。', 'inside-to-inside')
    if start_inside and end_in_buffer:
        result = safe_route_with_start_access(start, end, manual_avoid, stats, API_LIMIT)
        result.update({'buffer_distance_m': round(end_area_distance, 1), 'entry_buffer_m': buffer_m})
        return finish_result(result, stats, 'amap-buffer-exit', 'point',
            '终点位于规避区域外5公里缓冲区，已直接规避导航至终点。', 'inside-to-buffer')
    if start_in_buffer and end_inside:
        result = safe_route_with_start_access(start, end, manual_avoid, stats, API_LIMIT)
        result.update({'buffer_distance_m': round(start_area_distance, 1), 'entry_buffer_m': buffer_m})
        return finish_result(result, stats, 'amap-buffer-entry', 'point',
            '起点位于规避区域外5公里缓冲区，已直接规避导航至目的地。', 'buffer-to-inside')
    if not start_inside and not end_inside:
        return finish_result({'coordinates': [], 'distance_m': 0, 'duration_s': 0}, stats,
            'amap-handoff', 'point', '起终点均不在六环内进京证限行区域，请使用高德导航配合六环外进京证。',
            'outside-to-outside')
    if start_inside and not end_inside:
        result = solve_exit(start, manual_avoid, stats, end)
        gateway = result['coordinates'][-1]
        transition = {'phase': 'outbound', 'gateway': gateway, 'gateway_label': gateway_label(gateway, stats),
                      'connector': [gateway, list(end)]}
        return finish_result(result, stats, 'amap-transition-outbound', 'point',
            '已找到通往规避区域外接驳点的安全路线；到达后请使用高德导航配合六环外进京证继续剩下旅程。',
            'inside-to-outside', transition)
    result = solve_entry(start, end, manual_avoid, stats)
    gateway = result['gateway']
    transition = {'phase': 'inbound', 'gateway': gateway, 'gateway_label': gateway_label(gateway, stats),
                  'connector': [list(start), gateway]}
    return finish_result(result, stats, 'amap-transition-inbound', 'point',
        '已找到规避区域外接驳点通往目的地的安全路线；请先使用高德导航配合六环外进京证导航到安全点，到达后可按照蓝线前往目的地。',
        'outside-to-inside', transition)


def amap_search(query):
    key = query.casefold().strip()
    cached = SEARCH_CACHE.get(key)
    if cached and time.time()-cached[0] < 600:
        return cached[1]
    stats = {'calls': 0, 'limit': 10}
    data = amap_json('https://restapi.amap.com/v3/assistant/inputtips', {'keywords': query, 'datatype': 'all'}, stats, 12)
    results = []
    for tip in data.get('tips', []):
        location = tip.get('location')
        if not isinstance(location, str) or ',' not in location:
            continue
        lon, lat = map(float, location.split(','))
        district = tip.get('district') or ''
        address = tip.get('address') if isinstance(tip.get('address'), str) else ''
        detail = ' · '.join(value for value in (district, address) if value)
        name = tip.get('name') or query
        results.append({'name': f'{name}，{detail}' if detail else name, 'lon': lon, 'lat': lat,
                        'coordinate_system': 'GCJ-02'})
        if len(results) >= 8: break
    SEARCH_CACHE[key] = (time.time(), results)
    return results


def update_status():
    status = load_json(STATUS_FILE) if STATUS_FILE.exists() else {}
    status.pop('map_updated_at', None)
    status.setdefault('cameras_updated_at', datetime.fromtimestamp(
        CAMERA_FILE.stat().st_mtime).astimezone().isoformat(timespec='seconds'))
    return status


def db():
    connection = sqlite3.connect(DB_FILE, timeout=15); connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with DB_LOCK, db() as connection:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('''CREATE TABLE IF NOT EXISTS navigation_records (
            id TEXT PRIMARY KEY, created_at TEXT NOT NULL, completed_at TEXT NOT NULL,
            elapsed_ms INTEGER NOT NULL, success INTEGER NOT NULL, mode TEXT NOT NULL,
            start_lon REAL, start_lat REAL, end_lon REAL, end_lat REAL,
            start_label TEXT, end_label TEXT, manual_avoid_count INTEGER NOT NULL DEFAULT 0,
            provider TEXT, distance_m REAL, duration_s REAL, amap_api_calls INTEGER,
            validation_iterations INTEGER, segment_count INTEGER, mismatch_count INTEGER NOT NULL DEFAULT 0,
            potential_stale_map INTEGER NOT NULL DEFAULT 0, patch_count INTEGER NOT NULL DEFAULT 0,
            error TEXT, client_ip TEXT, user_agent TEXT, request_json TEXT NOT NULL,
            result_json TEXT, events_json TEXT NOT NULL)''')
        connection.execute('''CREATE TABLE IF NOT EXISTS validated_route_cache (
            cache_key TEXT PRIMARY KEY, created_at REAL NOT NULL,
            camera_mtime INTEGER NOT NULL, result_json TEXT NOT NULL)''')


init_db()


def route_cache_key(start, end, manual_avoid):
    value = {
        'version': ROUTE_ALGORITHM,
        'start': [round(float(start[0]), 6), round(float(start[1]), 6)],
        'end': None if end is None else [round(float(end[0]), 6), round(float(end[1]), 6)],
        'avoid': sorted([round(float(point[0]), 6), round(float(point[1]), 6)] for point in manual_avoid),
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def cached_route_is_safe(result, manual_avoid):
    points = result.get('coordinates') or []
    if len(points) < 2:
        return result.get('area_state') == 'outside-to-outside'
    return not route_conflicts(points, manual_avoid)


def load_validated_route(start, end, manual_avoid):
    key = route_cache_key(start, end, manual_avoid)
    cutoff = time.time() - ROUTE_CACHE_TTL_S
    with DB_LOCK, db() as connection:
        row = connection.execute(
            'SELECT result_json FROM validated_route_cache '
            'WHERE cache_key=? AND camera_mtime=? AND created_at>=?',
            (key, CAMERA_MTIME, cutoff)).fetchone()
        if row:
            result = json.loads(row['result_json'])
            if cached_route_is_safe(result, manual_avoid):
                result['amap_api_calls'] = 1
                result['cache_hit'] = True
                return result

        # Backfill recent, already validated routes created before the cache table existed.
        rows = connection.execute(
            'SELECT request_json,result_json FROM navigation_records '
            'WHERE success=1 AND created_at>=? AND result_json IS NOT NULL '
            'ORDER BY created_at DESC LIMIT 200',
            (datetime.fromtimestamp(cutoff).astimezone().isoformat(timespec='milliseconds'),)).fetchall()
        for old in rows:
            payload, result = json.loads(old['request_json']), json.loads(old['result_json'])
            if result.get('route_algorithm') != ROUTE_ALGORITHM:
                continue
            old_start, old_end = payload.get('start'), payload.get('end')
            if route_cache_key(old_start, old_end, payload.get('avoid_points') or []) != key:
                continue
            if result.get('coordinate_count', len(result.get('coordinates') or [])) != len(result.get('coordinates') or []):
                continue
            if not cached_route_is_safe(result, manual_avoid):
                continue
            connection.execute(
                'INSERT OR REPLACE INTO validated_route_cache VALUES (?,?,?,?)',
                (key, time.time(), CAMERA_MTIME, json.dumps(result, ensure_ascii=False, separators=(',', ':'))))
            result['amap_api_calls'] = 1
            result['cache_hit'] = True
            return result
    return None


def store_validated_route(start, end, manual_avoid, result):
    if not cached_route_is_safe(result, manual_avoid):
        return
    key = route_cache_key(start, end, manual_avoid)
    with DB_LOCK, db() as connection:
        connection.execute(
            'INSERT OR REPLACE INTO validated_route_cache VALUES (?,?,?,?)',
            (key, time.time(), CAMERA_MTIME,
             json.dumps(result, ensure_ascii=False, separators=(',', ':'))))


def calculate_route(start, end=None, manual_avoid=()):
    refresh_runtime_data()
    if end is None and not in_controlled_area(start):
        raise AlreadyOutsideError('起点已在规避区域外，无需一键出六环。')
    manual_avoid = [tuple(map(float, point)) for point in manual_avoid]
    cached = load_validated_route(start, end, manual_avoid)
    if cached is not None:
        return cached
    result = calculate_route_uncached(start, end, manual_avoid)
    store_validated_route(start, end, manual_avoid, result)
    return result


def save_audit(payload, client_ip, user_agent, started, success, result=None, error=None):
    now = datetime.now().astimezone(); start = payload.get('start') or [None, None]; end = payload.get('end') or [None, None]
    snapshot = None
    if result:
        snapshot = {key: result.get(key) for key in ('distance_m','duration_s','provider','amap_api_calls','mode','notice','area_state','buffer_distance_m','entry_buffer_m','area_transition','coordinate_system','route_algorithm','maneuvers','cache_hit','start_access_repair') if key in result}
        points = result.get('coordinates') or []
        if len(points) > 2500:
            points = [points[round(i*(len(points)-1)/2499)] for i in range(2500)]
        snapshot.update({'coordinates': points, 'coordinate_count': len(result.get('coordinates') or [])})
    row = (f'{int(started[0]*1000)}-{uuid.uuid4().hex[:8]}', started[1], now.isoformat(timespec='milliseconds'),
        round((time.monotonic()-started[2])*1000), int(success), payload.get('mode') or 'point',
        start[0],start[1],end[0],end[1],payload.get('start_label') or '',payload.get('end_label') or '',
        len(payload.get('avoid_points') or []),(snapshot or {}).get('provider'),(snapshot or {}).get('distance_m'),
        (snapshot or {}).get('duration_s'),(snapshot or {}).get('amap_api_calls'),None,0,0,0,0,
        str(error) if error else None,client_ip,user_agent,json.dumps(payload,ensure_ascii=False,separators=(',',':')),
        json.dumps(snapshot,ensure_ascii=False,separators=(',',':')) if snapshot else None,'[]')
    with DB_LOCK, db() as connection:
        connection.execute('INSERT INTO navigation_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', row)


def record_summary(row):
    return {'id':row['id'],'created_at':row['created_at'],'completed_at':row['completed_at'],'elapsed_ms':row['elapsed_ms'],
        'success':bool(row['success']),'mode':row['mode'],'start':[row['start_lon'],row['start_lat']],
        'end':[row['end_lon'],row['end_lat']] if row['end_lon'] is not None else None,'start_label':row['start_label'],
        'end_label':row['end_label'],'manual_avoid_count':row['manual_avoid_count'],'provider':row['provider'],
        'distance_m':row['distance_m'],'duration_s':row['duration_s'],'amap_api_calls':row['amap_api_calls'],
        'error':row['error'],'client_ip':row['client_ip']}


def list_records(query):
    limit=max(1,min(200,int(query.get('limit',['50'])[0]))); offset=max(0,int(query.get('offset',['0'])[0])); clauses=[]; values=[]
    status=query.get('status',['all'])[0]
    if status in ('success','failed'): clauses.append('success=?'); values.append(1 if status=='success' else 0)
    search=query.get('q',[''])[0].strip()
    if search: clauses.append('(id LIKE ? OR start_label LIKE ? OR end_label LIKE ? OR provider LIKE ? OR error LIKE ?)'); values.extend([f'%{search}%']*5)
    where=(' WHERE '+' AND '.join(clauses)) if clauses else ''
    with DB_LOCK,db() as connection:
        total=connection.execute('SELECT COUNT(*) FROM navigation_records'+where,values).fetchone()[0]
        rows=connection.execute('SELECT * FROM navigation_records'+where+' ORDER BY created_at DESC LIMIT ? OFFSET ?',(*values,limit,offset)).fetchall()
        counts=connection.execute('SELECT COUNT(*) total,SUM(success=1) succeeded,SUM(success=0) failed FROM navigation_records').fetchone()
    return {'items':[record_summary(row) for row in rows],'total':total,'limit':limit,'offset':offset,
            'summary':{'total':int(counts['total'] or 0),'succeeded':int(counts['succeeded'] or 0),'failed':int(counts['failed'] or 0)}}


def get_record(record_id):
    with DB_LOCK,db() as connection: row=connection.execute('SELECT * FROM navigation_records WHERE id=?',(record_id,)).fetchone()
    if not row: return None
    value=record_summary(row); value.update({'user_agent':row['user_agent'],'request':json.loads(row['request_json']),
        'result':json.loads(row['result_json']) if row['result_json'] else None,'events':[]}); return value


def delete_records(record_id=None):
    with DB_LOCK,db() as connection:
        cursor=connection.execute('DELETE FROM navigation_records'+(' WHERE id=?' if record_id else ''),(record_id,) if record_id else ())
        return cursor.rowcount


def log_error(payload,error):
    with LOG_LOCK,ERROR_LOG.open('a',encoding='utf-8') as output:
        output.write(json.dumps({'time':datetime.now().astimezone().isoformat(timespec='seconds'),'payload':payload,'error':str(error)},ensure_ascii=False)+'\n')


def diagnostic_schema(connection):
    connection.execute('CREATE TABLE IF NOT EXISTS location_diagnostics (id TEXT PRIMARY KEY, received REAL, payload TEXT)')
    connection.execute('DELETE FROM location_diagnostics WHERE received < ?', (time.time()-7*86400,))


def save_location_diagnostics(payload):
    session = payload['session']
    device = payload['device']
    if not all(isinstance(v, str) and re.fullmatch(r'[a-zA-Z0-9-]{8,64}', v) for v in (session, device)):
        raise ValueError('invalid id')
    events = payload['events']
    if not isinstance(events, list) or len(events) > 60: raise ValueError('invalid events')
    # Strict allowlist: never persist arbitrary messages, coordinates or URLs.
    allowed = {'time', 'type', 'visibility', 'code', 'state', 'elapsed_ms', 'accuracy', 'sourceTimestamp'}
    clean = []
    for event in events:
        if not isinstance(event, dict): raise ValueError('invalid event')
        clean.append({k: v for k, v in event.items() if k in allowed and
                      (isinstance(v, (int, float)) or isinstance(v, str) and len(v) <= 80)})
    with DB_LOCK, db() as connection:
        diagnostic_schema(connection)
        previous = connection.execute('SELECT received,payload FROM location_diagnostics WHERE id=?', (session,)).fetchone()
        if previous and time.time()-previous[0] < 5: return {'retry': True}
        old = json.loads(previous[1])['events'] if previous else []
        record = {'session': session, 'device': device, 'browser': str(payload.get('browser', ''))[:240],
                  'received': datetime.now().astimezone().isoformat(), 'events': (old+clean)[-300:]}
        connection.execute('INSERT OR REPLACE INTO location_diagnostics VALUES (?,?,?)',
                           (session, time.time(), json.dumps(record, ensure_ascii=False)))
        connection.execute('DELETE FROM location_diagnostics WHERE id NOT IN (SELECT id FROM location_diagnostics ORDER BY received DESC LIMIT 2000)')
    return {'ok': True}


class Handler(SimpleHTTPRequestHandler):
    def send_json(self,value,status=200):
        body=json.dumps(value,ensure_ascii=False).encode(); self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def require_admin(self):
        if not ADMIN_PASSWORD:
            self.send_json({'error':'后台密码未配置'},503)
            return False
        expected='Basic '+base64.b64encode(f'{ADMIN_USERNAME}:{ADMIN_PASSWORD}'.encode()).decode()
        if hmac.compare_digest(self.headers.get('Authorization',''),expected): return True
        body='需要后台登录'.encode(); self.send_response(401); self.send_header('WWW-Authenticate','Basic realm="TraceRT-PEK Admin", charset="UTF-8"')
        self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return False
    def client_ip(self):
        value=self.headers.get('CF-Connecting-IP') or self.headers.get('X-Forwarded-For','')
        return value.split(',',1)[0].strip() or self.client_address[0]
    def do_GET(self):
        parsed=urllib.parse.urlparse(self.path)
        if parsed.path.startswith('/api/admin/'):
            if not self.require_admin(): return
            if parsed.path == '/api/admin/location-diagnostics':
                with DB_LOCK, db() as connection:
                    diagnostic_schema(connection)
                    rows = connection.execute('SELECT payload FROM location_diagnostics ORDER BY received DESC LIMIT 500').fetchall()
                return self.send_json([json.loads(row[0]) for row in rows])
            if parsed.path=='/api/admin/navigation-records': return self.send_json(list_records(urllib.parse.parse_qs(parsed.query)))
            match=re.fullmatch(r'/api/admin/navigation-records/([A-Za-z0-9-]+)',parsed.path)
            if match:
                record=get_record(match.group(1)); return self.send_json(record) if record else self.send_json({'error':'记录不存在'},404)
            return self.send_error(404)
        if parsed.path.startswith('/admin') and not self.require_admin(): return
        if parsed.path=='/api/health': refresh_runtime_data(); return self.send_json({'ok':True,'router':'amap-gcj02','coordinate_system':'GCJ-02','cameras':len(CAMERAS)})
        if parsed.path=='/api/cameras': refresh_runtime_data(); return self.send_json(CAMERAS)
        if parsed.path=='/api/update-status': return self.send_json(update_status())
        if parsed.path=='/api/area-status':
            try:
                query=urllib.parse.parse_qs(parsed.query); point=(float(query['lon'][0]),float(query['lat'][0])); inside=in_controlled_area(point)
                distance=controlled_area_distance(point); buffer_m=float(AREA.get('entry_buffer_m',5000))
                return self.send_json({'inside':inside,'outside':not inside,'distance_m':round(distance,1),'entry_buffer_m':buffer_m,
                    'in_entry_buffer':not inside and distance<=buffer_m,'coordinate_system':'GCJ-02'})
            except (KeyError,IndexError,TypeError,ValueError): return self.send_json({'error':'位置坐标无效'},400)
        if parsed.path=='/api/geocode':
            query=urllib.parse.parse_qs(parsed.query).get('q',[''])[0].strip()
            if not query: return self.send_json({'error':'缺少地址'},400)
            try: return self.send_json(amap_search(query))
            except Exception as error: return self.send_json({'error':f'地址解析失败：{error}'},502)
        path='index.html' if parsed.path=='/' else ('admin.html' if parsed.path in ('/admin','/admin/') else parsed.path.lstrip('/'))
        target=(WEB_ROOT/path).resolve()
        if WEB_ROOT not in target.parents and target!=WEB_ROOT: return self.send_error(403)
        if not target.is_file(): return self.send_error(404)
        content=target.read_bytes()
        if target.name in ('index.html', 'admin.html'):
            security = json.dumps({'securityJsCode': AMAP_JS_SECURITY_CODE}).replace('<', '\\u003c')
            content = content.decode('utf-8').replace('__AMAP_SECURITY_CONFIG__', security).replace(
                '__AMAP_JS_KEY__', urllib.parse.quote(AMAP_JS_KEY, safe='')).encode('utf-8')
        self.send_response(200); self.send_header('Content-Type',mimetypes.guess_type(target.name)[0] or 'application/octet-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Content-Length',str(len(content))); self.end_headers(); self.wfile.write(content)
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == '/api/location-diagnostics':
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 32768: return self.send_json({'error': 'payload size'}, 413)
                payload = json.loads(self.rfile.read(size))
                return self.send_json(save_location_diagnostics(payload))
            except (ValueError, TypeError, KeyError):
                return self.send_json({'error': 'invalid diagnostics'}, 400)
        correction = re.fullmatch(r'/api/cameras/([0-9a-f-]+)/correction', path)
        if correction:
            try:
                payload = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
                lon, lat = float(payload['lon']), float(payload['lat'])
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                return self.send_json({'error': str(error)}, 400)
            except KeyError:
                return self.send_json({'error': '缺少修正坐标'}, 400)
            try:
                return self.send_json(save_camera_correction(correction.group(1), lon, lat))
            except KeyError:
                return self.send_json({'error': '规避点不存在或数据已经更新'}, 404)
        if path != '/api/route': return self.send_error(404)
        try:
            payload=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0)))); start=tuple(map(float,payload['start']))
            end=None if payload.get('mode')=='outside' else tuple(map(float,payload['end'])); manual=payload.get('avoid_points') or []
        except (json.JSONDecodeError,KeyError,TypeError,ValueError) as error: return self.send_json({'error':str(error)},400)
        now=datetime.now().astimezone(); started=(now.timestamp(),now.isoformat(timespec='milliseconds'),time.monotonic())
        try:
            result=calculate_route(start,end,manual); save_audit(payload,self.client_ip(),self.headers.get('User-Agent',''),started,True,result=result); return self.send_json(result)
        except AlreadyOutsideError as error:
            return self.send_json({'error':str(error),'code':'ALREADY_OUTSIDE'},400)
        except ValueError as error:
            log_error(payload,error); save_audit(payload,self.client_ip(),self.headers.get('User-Agent',''),started,False,error=error)
            return self.send_json({'error':'未找到不经过规避点的可用路线。'},400)
        except Exception as error:
            log_error(payload,error); save_audit(payload,self.client_ip(),self.headers.get('User-Agent',''),started,False,error=error)
            return self.send_json({'error':f'后台服务异常：{error}'},500)
    def do_DELETE(self):
        parsed=urllib.parse.urlparse(self.path)
        if not parsed.path.startswith('/api/admin/') or not self.require_admin(): return
        if parsed.path=='/api/admin/navigation-records': return self.send_json({'deleted':delete_records()})
        match=re.fullmatch(r'/api/admin/navigation-records/([A-Za-z0-9-]+)',parsed.path)
        if match:
            count=delete_records(match.group(1)); return self.send_json({'deleted':count}) if count else self.send_json({'error':'记录不存在'},404)
        return self.send_error(404)
    def log_message(self,fmt,*args): print(f'{self.client_address[0]} {fmt % args}')


if __name__=='__main__':
    missing = [name for name in ('AMAP_API_KEY', 'AMAP_JS_KEY', 'AMAP_JS_SECURITY_CODE', 'ADMIN_PASSWORD')
               if not os.environ.get(name, '').strip()]
    if missing:
        raise SystemExit('Missing required environment variables: ' + ', '.join(missing))
    print(f'TraceRT-PEK GCJ-02/Amap http://{HOST}:{PORT}')
    ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
