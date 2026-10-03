"""技能赛训协作基础服务的服务端基础包。"""

from .custody import CustodyService
from .service import DomainService

__all__ = ["CustodyService", "DomainService"]
