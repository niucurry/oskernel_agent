from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_mobile_detail_is_visible_and_scrolled():
    app = (ROOT / "frontend/src/App.vue").read_text(encoding="utf-8")
    css = (ROOT / "frontend/src/styles.css").read_text(encoding="utf-8")
    assert 'ref="detailPane"' in app and "scrollIntoView" in app
    assert "@media (max-width: 768px)" in css
    assert "100dvh" in css and "order: -1" in css
