"""The topology store: machines, links and site routes as structured facts.

``model`` names what may be stored and refuses secrets; ``store`` keeps the facts in the
control database with provenance, expiry and conflict records. See ``docs/topology.md``.
"""

from . import model, store
from .model import TOPOLOGY_WING, Observer, SecretRefused, TopologyError

__all__ = ["TOPOLOGY_WING", "Observer", "SecretRefused", "TopologyError", "model", "store"]
