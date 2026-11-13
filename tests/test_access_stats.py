"""Drawer read counts kept beside the palace (``mempalace.access_stats``)."""

from mempalace import access_stats


def test_access_stats_count_every_read(tmp_path):
    palace = str(tmp_path)
    access_stats.record_access(palace, ["d1", "d2", "d1"])
    access_stats.record_access(palace, ["d1"])
    stats = access_stats.access_for(palace, ["d1", "d2", "d3"])
    assert stats["d1"]["retrieval_count"] == 2
    assert stats["d2"]["retrieval_count"] == 1
    assert "d3" not in stats


def test_access_stats_never_create_a_palace(tmp_path):
    missing = str(tmp_path / "nope")
    access_stats.record_access(missing, ["d1"])
    assert access_stats.access_for(missing, ["d1"]) == {}
    assert not (tmp_path / "nope").exists()
