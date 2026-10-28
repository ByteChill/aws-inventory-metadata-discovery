from __future__ import annotations

import pytest

from inventory.aws import ClientFactory, EndpointResolver
from inventory.config import get_common_settings
from tests.helpers import load_fixture


@pytest.mark.parametrize("case", load_fixture("foundation/endpoint_cases.json"))
def test_endpoint_resolution(settings_env: dict[str, str], case: dict[str, object]) -> None:
    resolver = EndpointResolver(get_common_settings())
    service, region = str(case["service"]), str(case["region"])

    assert resolver.api_region(region) == case["api_region"]
    # only cross-region endpoints are configured; everything else uses the default DNS
    assert resolver.endpoint_url(service, region) == case["endpoint_url"]


def test_aws_endpoint_url_overrides_everything(
    settings_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INVENTORY_AWS_ENDPOINT_URL", "http://localhost:5000")
    get_common_settings.cache_clear()
    resolver = EndpointResolver(get_common_settings())

    assert resolver.endpoint_url("iam", "global") == "http://localhost:5000"


def test_client_factory_routes_global_to_us_east_1(settings_env: dict[str, str]) -> None:
    factory = ClientFactory(EndpointResolver(get_common_settings()))

    global_client = factory.client("ec2", "global")
    regional_client = factory.client("ec2", "eu-west-1")

    assert global_client.meta.region_name == "us-east-1"
    assert regional_client.meta.endpoint_url == (
        "https://vpce-0a1-ec2.ec2.eu-west-1.vpce.amazonaws.com"
    )
