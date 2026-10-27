"""Patent PDF layout region detection.

The package intentionally keeps model imports lazy.  This makes the PDF and
schema layer usable for validation even when one of the optional model stacks
is not installed in the current environment.
"""

from .engine import PatentRegionLocator
from .cropper import crop_table_regions
from .schema import RawDetection

__all__ = ["PatentRegionLocator", "RawDetection", "crop_table_regions"]
