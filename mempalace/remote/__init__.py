"""Remote sync — push/pull palace wings to/from S3-compatible object storage."""

from .push import push_wing
from .pull import pull_wing

__all__ = ["push_wing", "pull_wing"]
