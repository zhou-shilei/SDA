"""State, nodes, and adapters for the patent dataset agent."""


def build_graph(*args, **kwargs):
    """Load LangGraph only when the orchestration graph is requested."""

    from .graph import build_graph as _build_graph

    return _build_graph(*args, **kwargs)


__all__ = ["build_graph"]
