"""Delta - Run only tests affected by code changes."""

__version__ = "0.4.31"
__author__ = "Delta Contributors"
__description__ = "Intelligently select and run only tests affected by code changes"

# Range-based storage (v2)
from .test_mapping_db_v2 import TestMappingDBV2
from .range_set import RangeSet

__all__ = [
    "TestMappingDBV2",     # New range-based storage  
    "RangeSet",            # Range compression utility
]
