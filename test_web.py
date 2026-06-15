"""Web 接口冒烟测试 — pytest"""
import json
import urllib.request

import pytest

BASE = "http://127.0.0.1:8080"
pytestmark = pytest.mark.skip(reason="需要先启动 web 服务: python monitor.py web")


def test_dashboard():
    r = urllib.request.urlopen(f"{BASE}/")
    assert r.status == 200
    assert len(r.read()) > 0


def test_api_stats():
    r = urllib.request.urlopen(f"{BASE}/api/stats")
    assert r.status == 200
    data = json.loads(r.read())
    assert isinstance(data, dict)


def test_videos_page():
    r = urllib.request.urlopen(f"{BASE}/videos")
    assert r.status == 200


def test_api_trends():
    r = urllib.request.urlopen(f"{BASE}/api/trends/1")
    assert r.status == 200
    data = json.loads(r.read())
    assert "datasets" in data
    assert "labels" in data
