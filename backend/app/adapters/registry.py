from __future__ import annotations

from ..config import Settings
from ..models import Platform
from .base import PublicPageAdapter
from .platforms import ADAPTER_CLASSES


def get_adapter(platform: Platform, settings: Settings) -> PublicPageAdapter:
    return ADAPTER_CLASSES[platform](settings)

