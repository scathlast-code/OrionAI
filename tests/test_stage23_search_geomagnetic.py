import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plugins.newsroom.desk import DEFAULT_TOPICS, source_search_queries, google_news_url

def test_geomagnetic_order():
    keys = [x[0] for x in DEFAULT_TOPICS]
    assert keys.index('astronomy') < keys.index('geomagnetic') < keys.index('weather')
    assert 'astro' not in keys

def test_scoped_query():
    assert source_search_queries('https://www.example.com/news', 'сонячна активність') == ['site:example.com сонячна активність']
    assert 'site%3Aexample.com' in google_news_url(source_search_queries('https://example.com')[0])

def test_invalid_source():
    import pytest
    with pytest.raises(ValueError):
        source_search_queries('not-a-url')
