"""Collectors. Importing this package registers every collector in ``registry``."""

from inventory.collectors import ec2, ecs, iam, route53, s3
from inventory.collectors.base import Collector, CollectorContext, Registry, register, registry

__all__ = [
    "Collector",
    "CollectorContext",
    "Registry",
    "ec2",
    "ecs",
    "iam",
    "register",
    "registry",
    "route53",
    "s3",
]
