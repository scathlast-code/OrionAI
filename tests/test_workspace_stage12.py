from pathlib import Path

def test_workspace_is_real_page():
    page = (Path(__file__).resolve().parents[1] / 'sticker_webapp' / 'dashboard.html').read_text()
    for item in ('/api/notifications', '/api/notifications/quiet', '/api/notifications/sleep', '/index.html?draft=', 'data-page', 'aria-label'):
        assert item in page

def test_bot_has_workspace_command():
    bot = (Path(__file__).resolve().parents[1] / 'bot.py').read_text()
    assert 'CommandHandler("workspace"' in bot and 'cmd_workspace' in bot
    assert 'ORION_MINIAPP_URL' in bot
