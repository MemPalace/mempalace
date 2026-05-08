#!/usr/bin/env python3
import sys
sys.path.insert(0, '.')
from mempalace.config import MempalaceConfig

cfg = MempalaceConfig()
print('Config readable_extensions:', sorted(cfg.readable_extensions))
print('Contains .jsonl:', '.jsonl' in cfg.readable_extensions)
print('Contains .py:', '.py' in cfg.readable_extensions)
print('Contains .nonexistent:', '.nonexistent' in cfg.readable_extensions)
