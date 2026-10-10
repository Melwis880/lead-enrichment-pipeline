"""Disk cache: expiry and pruning of entries that are never read again."""

import json
import time

from lead_enrich.cache import JsonDiskCache
from lead_enrich.pipeline import LLM_CACHE_PREFIX


def _age(cache: JsonDiskCache, key: str, hours: float) -> None:
    path = cache._path(key)
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["_stored_at"] = time.time() - hours * 3600
    path.write_text(json.dumps(entry), encoding="utf-8")


def test_prune_removes_raw_answers_left_by_older_versions(tmp_path):
    cache = JsonDiskCache(tmp_path, ttl_hours=720)
    cache.set("llm:old-raw-answer", {"raw": "contact jane.smith@shop.example"})
    cache.set(LLM_CACHE_PREFIX + "abc", {"enrichment": {}})

    removed = cache.prune(lambda k: k.startswith(LLM_CACHE_PREFIX))

    assert removed == 1
    assert cache.get(LLM_CACHE_PREFIX + "abc") == {"enrichment": {}}
    files = list(tmp_path.glob("*/*.json"))
    assert len(files) == 1
    assert "jane.smith" not in files[0].read_text(encoding="utf-8")


def test_prune_removes_expired_entries_and_keeps_fresh_ones(tmp_path):
    cache = JsonDiskCache(tmp_path, ttl_hours=24)
    cache.set("old", {"v": 1})
    cache.set("new", {"v": 2})
    _age(cache, "old", hours=25)

    assert cache.prune() == 1
    assert cache.get("new") == {"v": 2}
    assert not cache._path("old").exists()


def test_prune_removes_unreadable_files_and_temp_leftovers(tmp_path):
    cache = JsonDiskCache(tmp_path)
    cache.set("good", {"v": 1})
    shard = cache._path("good").parent
    (shard / "broken.json").write_text("{not json", encoding="utf-8")
    (shard / "half-written.tmp").write_text("{", encoding="utf-8")

    assert cache.prune() == 2
    assert cache.get("good") == {"v": 1}


def test_prune_on_missing_directory_is_a_no_op(tmp_path):
    assert JsonDiskCache(tmp_path / "nothing-here").prune() == 0
