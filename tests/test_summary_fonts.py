from types import SimpleNamespace

import pytest

from oskernel_agent.finals import summary_pdf


def test_font_registration_skips_unsupported_and_incomplete_candidates(tmp_path, monkeypatch):
    unsupported, incomplete, usable = [tmp_path / name for name in ("cff.ttc", "cjk-only.ttf", "complete.ttf")]
    for path in (unsupported, incomplete, usable):
        path.touch()
    monkeypatch.setattr(summary_pdf, "_font_candidates", lambda: ([unsupported, incomplete, usable], [unsupported]))
    monkeypatch.setattr(summary_pdf.pdfmetrics, "getRegisteredFontNames", lambda: [])
    registered = []
    monkeypatch.setattr(summary_pdf.pdfmetrics, "registerFont", registered.append)

    def load(name, path, **kwargs):
        if path == str(unsupported):
            raise summary_pdf.TTFError("postscript outlines are not supported")
        chars = "中文" if path == str(incomplete) else "AI0123中文"
        return SimpleNamespace(name=name, path=path, face=SimpleNamespace(charToGlyph={ord(c): 1 for c in chars}))

    monkeypatch.setattr(summary_pdf, "TTFont", load)
    assert summary_pdf._register_fonts() == ("FinalsSans", "FinalsSansBold")
    assert [font.path for font in registered] == [str(usable), str(usable)]


def test_font_registration_reports_no_compatible_font(tmp_path, monkeypatch):
    candidate = tmp_path / "unsupported.ttc"
    candidate.touch()
    monkeypatch.setattr(summary_pdf, "_font_candidates", lambda: ([candidate], []))
    monkeypatch.setattr(summary_pdf.pdfmetrics, "getRegisteredFontNames", lambda: [])

    def reject(*args, **kwargs):
        raise summary_pdf.TTFError("postscript outlines are not supported")

    monkeypatch.setattr(summary_pdf, "TTFont", reject)
    with pytest.raises(summary_pdf.SummaryPdfError, match="FINALS_CJK_FONT"):
        summary_pdf._register_fonts()
