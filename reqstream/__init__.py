"""reqstream — streaming ingestion and ranking over open-requisition data."""

__version__ = "0.1.0"

from . import ingest, gate, rank, verify          # noqa: F401
from .ingest import Corpus, IngestError           # noqa: F401
from .gate import Gate, GateReport                # noqa: F401

__all__ = ["ingest", "gate", "rank", "verify",
           "Corpus", "IngestError", "Gate", "GateReport"]
