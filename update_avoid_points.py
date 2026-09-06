#!/usr/bin/env python3
"""Refresh category-filtered camera data in source-native GCJ-02 coordinates."""
import argparse
import json
import os
import re
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
SOURCE_URL = 'https://www.jinjing365.com/index.asp'
DATA_DIR = Path(os.environ.get('TRACERT_DATA_DIR', ROOT))
POINTS_FILE = DATA_DIR / 'camera_points.json'
STATUS_FILE = DATA_DIR / 'data_update_status.json'


def atomic_text(path, text):
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(text, encoding='utf-8')
    temporary.replace(path)


def fetch_source():
    error = None
    for attempt in range(5):
        try:
            request = urllib.request.Request(SOURCE_URL, headers={'User-Agent': 'TraceRT-PEK/2.0'})
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode('utf-8', errors='replace')
        except Exception as caught:
            error = caught
            time.sleep(2**attempt)
    raise RuntimeError(f'探头数据下载失败：{error}')


def parse_points(html):
    raw = re.findall(r"name:\s*'([^']+)'\s*,\s*position:\s*\[([0-9.]+),([0-9.]+)\]\s*,\s*aa:'([^']+)'", html)
    points, seen = [], set()
    for name, lon, lat, category in raw:
        if category == '6':
            continue
        key = (round(float(lon), 7), round(float(lat), 7), name)
        if key in seen:
            continue
        seen.add(key)
        points.append({'name': name, 'lon': float(lon), 'lat': float(lat)})
    if len(raw) < 1000 or len(points) < 500:
        raise RuntimeError(f'探头数量异常：原始{len(raw)}，有效{len(points)}')
    return points


def source_update_time(html):
    match = re.search(r'更新时间[\s\S]{0,300}?(20\d\d)-(\d\d)-(\d\d)-(\d\d)[：:](\d\d)[：:](\d\d)', html)
    if not match:
        raise RuntimeError('数据源页面缺少可识别的更新时间')
    return datetime(*map(int, match.groups()), tzinfo=ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--local-html', type=Path)
    args = parser.parse_args()
    html = args.local_html.read_text(encoding='utf-8') if args.local_html else fetch_source()
    points = parse_points(html)
    old = json.loads(POINTS_FILE.read_text(encoding='utf-8')) if POINTS_FILE.exists() else []
    if old and not args.force and not .65 <= len(points)/len(old) <= 1.35:
        raise RuntimeError(f'探头数量变化过大：{len(old)} -> {len(points)}')
    serialized = json.dumps(points, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    old_serialized = json.dumps(old, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    if serialized == old_serialized:
        print('camera data unchanged')
        return
    updated_at = source_update_time(html)
    atomic_text(POINTS_FILE, json.dumps(points, ensure_ascii=False, indent=2)+'\n')
    status = json.loads(STATUS_FILE.read_text(encoding='utf-8')) if STATUS_FILE.exists() else {}
    status.pop('map_updated_at', None)
    status['cameras_updated_at'] = updated_at
    atomic_text(STATUS_FILE, json.dumps(status, ensure_ascii=False, indent=2)+'\n')
    print(f'published cameras={len(points)} coordinate_system=GCJ-02')


if __name__ == '__main__':
    main()
