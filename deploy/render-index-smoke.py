"""Build representative storage and one real exposure for the isolated Render test."""

from contextlib import closing
from pathlib import Path
import sqlite3

from server.exposure_index import FOOTPRINT, SCHEMA, SCHEMA_VERSION, cap_box

ROW_COUNT = 2_000_000
KEY = ('dr1/level2/2025W18_1B/l2b-v27-2026-196/1/'
       'level2_2025W18_1B_0162_1D1_spx_l2b-v27-2026-196.fits')
REAL_FOOTPRINT = (
    (128.63450610898792, -35.58230332238156), (132.41138071133417, -37.27060720746736),
    (130.14298424991844, -40.27279633097758), (126.23043274299859, -38.4978766734843),
)


def pack(footprint):
    return FOOTPRINT.pack(*(value for point in footprint for value in point))


destination = Path('/app/data/spherex.sqlite3')
destination.parent.mkdir(parents=True, exist_ok=True)
with closing(sqlite3.connect(destination)) as db:
    db.executescript(SCHEMA)
    db.execute('PRAGMA journal_mode=OFF')
    db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
    db.execute('INSERT INTO exposure VALUES (1,?,?,?,?,?,?,?,?)', (
        KEY, 'dr1', 1, '2025W18_1B_0162_1', 60794.593497, pack(REAL_FOOTPRINT), .7455, 1.1167,
    ))
    db.execute('INSERT INTO sky VALUES (1,?,?,?,?,?,?)', cap_box(REAL_FOOTPRINT))
    footprints = []
    for ra in range(0, 360, 3):
        for dec in range(-85, 88, 3):
            if abs(ra - 127.69444) <= 5 and abs(dec + 39.1776) <= 5:
                continue
            footprint = ((ra, dec), (ra + 1, dec), (ra + 1, dec + 1), (ra, dec + 1))
            footprints.append((pack(footprint), cap_box(footprint)))
    for start in range(2, ROW_COUNT + 1, 20_000):
        rows = []
        boxes = []
        for i in range(start, min(ROW_COUNT + 1, start + 20_000)):
            footprint, bounds = footprints[(i * 7919) % len(footprints)]
            release = 'dr1' if i % 2 else 'qr3'
            detector = i % 6 + 1
            period = f'2025W{i // 38000 % 52 + 1:02d}_1B'
            obs = f'{period}_{i:07d}_1'
            key = (f'{release}/level2/{period}/l2b-v27-2026-196/{detector}/'
                   f'level2_{obs}D{detector}_spx_l2b-v27-2026-196.fits')
            rows.append((i, key, release, detector, obs, 60800 + i / 3600, footprint, .75, 1.12))
            boxes.append((i, *bounds))
        db.executemany('INSERT INTO exposure VALUES (?,?,?,?,?,?,?,?,?)', rows)
        db.executemany('INSERT INTO sky VALUES (?,?,?,?,?,?,?)', boxes)
        db.commit()
    db.execute('INSERT INTO snapshot VALUES (?,?,?)', ('RENDER SIZE TEST FIXTURE', ROW_COUNT // 2, ROW_COUNT // 2))
    db.commit()
    assert db.execute('PRAGMA quick_check').fetchone() == ('ok',)
print(f'Built Render size fixture, {ROW_COUNT:,} rows, {destination.stat().st_size:,} bytes', flush=True)
