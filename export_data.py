"""Export application state for migration; no credentials are copied."""
import argparse
from pathlib import Path
import shutil
import sqlite3

ROOT = Path(__file__).resolve().parent


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('destination', type=Path)
    destination = parser.parse_args().destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    for name in ('camera_points.json', 'data_update_status.json', 'camera_corrections.json'):
        if (ROOT / name).exists():
            shutil.copyfile(ROOT / name, destination / name)
    source = ROOT / 'navigation_history.sqlite3'
    if source.exists():
        with sqlite3.connect(f'{source.as_uri()}?mode=ro', uri=True) as src:
            with sqlite3.connect(destination / source.name) as dst:
                src.backup(dst)
    print(f'Application data exported to {destination}; credentials excluded')
