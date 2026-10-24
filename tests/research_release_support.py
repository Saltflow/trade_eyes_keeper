"""Versioned inputs for offline research tests, independent of local run archives."""

from pathlib import Path

ROOT = Path(__file__).parent / "fixtures/research_release"
SOURCES = [ROOT / "peer/完整分析.md", ROOT / "stage3/模型输入.md"]


def use_fixture_market(monkeypatch, inputs):
    original = inputs.latest_market

    def latest(sources, companies=()):
        if any(
            Path(source).resolve().is_relative_to(ROOT.resolve()) for source in sources
        ):
            with monkeypatch.context() as patch:
                patch.setattr(inputs, "REPO", ROOT)
                return original(sources, companies)
        return original(sources, companies)

    monkeypatch.setattr(inputs, "latest_market", latest)
