"""Content-addressed, bounded annual-report research orchestration."""

from .models import ResearchSpec

__all__ = [
    "ResearchSpec",
    "cancel_research",
    "get_status",
    "resume_research",
    "run_research",
    "start_research",
]


def __getattr__(name):
    # The financial domain implementation stays in one package. Merely using
    # its contracts or read-only caches must not construct model/network ports.
    if name in __all__ and name != "ResearchSpec":
        from . import engine

        return getattr(engine, name)
    raise AttributeError(name)
