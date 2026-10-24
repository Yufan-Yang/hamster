"""The directory collector is retired; downloaded media remains in the library."""
import json

from flask import jsonify, request
from .core import app


def enabled():
    # Explicit opt-in is retained solely for isolated legacy regression fixtures.
    return bool(app.config.get('DIRECTORY_COLLECTION_ENABLED', False))


@app.before_request
def retired_directory_api():
    if not enabled() and (request.path == '/api/crawls' or request.path.startswith('/api/crawls/')):
        return jsonify(error='目录采集已下线，请在片库中搜索已保存的视频', retired=True), 410, {'Cache-Control': 'no-store'}


def clear_cache(db):
    """Migration: forget catalogue data, preserving videos, watch state and privacy."""
    from . import trash
    # An archived ID can be reused by a different video before restoration.
    # Freeze each archive's classification before removing its provenance, and
    # never restore the discarded root keys as unknown/private roots later.
    for row in db.execute('SELECT id,owner,job_id,snapshot FROM job_trash').fetchall():
        snapshot = trash._json(row['snapshot'], {})
        snapshot['_trash_private'] = trash.snapshot_private(db, row['owner'], row['job_id'], snapshot)
        snapshot['_trash_source_keys'] = []
        snapshot.pop('_trash_download_context', None)
        if str(snapshot.get('source') or '').startswith('crawl:'):
            snapshot['source'] = 'import'
        db.execute('UPDATE job_trash SET snapshot=? WHERE id=?', (json.dumps(snapshot), row['id']))
    # Every archive now owns its classification. A dangling manual ID must not
    # make an unrelated future import private merely because it reuses that ID.
    for row in db.execute('SELECT DISTINCT owner FROM job_trash').fetchall():
        owner = row['owner']
        archived = [entry['job_id'] for entry in db.execute('SELECT DISTINCT t.job_id FROM job_trash t '
                    'WHERE t.owner=? AND t.job_id IS NOT NULL AND NOT EXISTS '
                    '(SELECT 1 FROM jobs j WHERE j.id=t.job_id AND j.owner=t.owner)', (owner,))]
        trash._manual_ids(db, owner, remove=archived)
    owners = {r['owner'] for r in db.execute('SELECT owner FROM crawl_source_jobs UNION SELECT owner FROM crawls '
              "UNION SELECT owner FROM jobs WHERE source LIKE 'crawl:%'")}
    for owner in owners:
        inherited = {r[0] for r in db.execute('SELECT s.job_id FROM crawl_source_jobs s JOIN jobs j '
                     'ON j.id=s.job_id AND j.owner=s.owner WHERE s.owner=? UNION '
                     "SELECT id FROM jobs WHERE owner=? AND source LIKE 'crawl:%'", (owner, owner))}
        general = {r[0] for r in db.execute('SELECT j.job_id FROM crawl_source_jobs j JOIN crawl_sources s '
                   "ON s.owner=j.owner AND s.url_key=j.url_key WHERE j.owner=? AND s.classification='general' UNION "
                   "SELECT j.id FROM jobs j JOIN crawls c ON j.source='crawl:'||c.id AND j.owner=c.owner "
                   "WHERE j.owner=? AND c.source_classification='general' AND NOT EXISTS "
                   '(SELECT 1 FROM crawl_source_jobs s WHERE s.owner=j.owner AND s.job_id=j.id)', (owner, owner))}
        hidden = inherited - general
        if hidden:
            prefs = db.execute('SELECT ids FROM privacy WHERE owner=?', (owner,)).fetchone()
            ids = set(json.loads(prefs['ids'] or '[]')) if prefs else set()
            db.execute("INSERT INTO privacy(owner,tags,ids) VALUES(?,'[]',?) ON CONFLICT(owner) DO UPDATE SET ids=excluded.ids",
                       (owner, json.dumps(sorted(ids | hidden))))
    # Source strings can otherwise conservatively hide formerly general imports
    # once the root ledger has gone. They no longer refer to a live collector.
    db.execute("UPDATE jobs SET source='import' WHERE source LIKE 'crawl:%'")
    db.execute("DELETE FROM source_inbox_links WHERE source_kind='crawl' OR content_kind='crawl'")
    db.execute('DELETE FROM source_inbox_aliases WHERE item_id NOT IN (SELECT item_id FROM source_inbox_links)')
    db.execute('DELETE FROM source_inbox WHERE id NOT IN (SELECT item_id FROM source_inbox_links)')
    # Retired page errors must not keep scheduling automated collector repairs.
    db.execute("DELETE FROM tasks WHERE kind='heal' AND target IN (SELECT 'incident:'||id FROM incidents WHERE kind='crawl')")
    db.execute("DELETE FROM incidents WHERE kind='crawl'")
    for table in ('source_inbox_baselines', 'crawl_pages', 'crawl_results', 'crawl_aliases', 'crawl_downloads',
                  'crawl_items', 'crawl_source_jobs', 'crawl_sources', 'crawls'):
        db.execute('DELETE FROM ' + table)
