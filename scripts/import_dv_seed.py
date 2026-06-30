"""Upsert parsed DV seed records (data/dv_seed.json) into the dv_scan inventory.

Runs INSIDE the container (where the DB lives), reading /data/dv_seed.json which
the host-side ``parse_dv_seed.py`` produced. Marks rows ``source='seed'`` and
NEVER overwrites a row a live ``dovi_tool`` scan has already classified
(``source='scan'``), so the seed is a bootstrap that real detection supersedes.

Usage (from the host):
    docker compose exec -T scanhound python - < scripts/import_dv_seed.py
"""
import json

from backend.database import DatabaseManager

DBP = "/data/.local/share/scanhound/crawler.db"
SEED = "/data/dv_seed.json"

records = json.load(open(SEED, encoding="utf-8"))
dm = DatabaseManager(db_path=DBP)
with dm._lock:
    conn = dm.get_connection()
    conn.cursor().executemany(
        """
        INSERT INTO dv_scan (path, title, dv_layer, source, scanned_at, last_seen_at)
        VALUES (:path, :title, :dv_layer, 'seed', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT(path) DO UPDATE SET
            dv_layer = excluded.dv_layer,
            title = excluded.title,
            source = 'seed',
            last_seen_at = CURRENT_TIMESTAMP
        WHERE dv_scan.source != 'scan'
        """,
        records,
    )
    conn.commit()
print(f"Imported {len(records)} seed record(s)")
print("dv_scan by layer:", dm.count_dv_scans_by_layer())
dm.close()
