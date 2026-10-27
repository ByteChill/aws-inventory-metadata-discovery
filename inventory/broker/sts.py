"""AssumeRoleWithSAML with broker error mapping."""

from __future__ import annotations

from botocore.exceptions import BotoCoreError, ClientError

from inventory.aws import AwsClient
from inventory.broker import BrokerError, SamlExpiredError
from inventory.broker.saml import RolePair, SamlAssertion
from inventory.errors import ACCESS_DENIED_CODES, THROTTLE_CODES
from inventory.models import AwsCredentials

EXPIRED_ASSERTION_CODES = frozenset({"ExpiredTokenException", "InvalidIdentityToken"})


class StsAssumer:
    def __init__(self, sts_client: AwsClient, session_duration_seconds: int = 3600) -> None:
        self._sts = sts_client
        self._duration = session_duration_seconds

    def assume(self, assertion: SamlAssertion, pair: RolePair) -> AwsCredentials:
        duration = min(self._duration, assertion.session_duration or self._duration)
        try:
            response = self._sts.assume_role_with_saml(
                RoleArn=pair.role_arn,
                PrincipalArn=pair.principal_arn,
                SAMLAssertion=assertion.raw_b64,
                DurationSeconds=duration,
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in EXPIRED_ASSERTION_CODES:
                raise SamlExpiredError(code) from exc
            if code in ACCESS_DENIED_CODES:
                raise BrokerError("ACCESS_DENIED", f"STS denied {pair.role_arn}") from exc
            if code in THROTTLE_CODES:
                raise BrokerError("THROTTLED", "STS throttled AssumeRoleWithSAML") from exc
            raise BrokerError("INTERNAL", f"STS error {code}") from exc
        except BotoCoreError as exc:
            raise BrokerError("INTERNAL", f"STS call failed: {type(exc).__name__}") from exc

        creds = response["Credentials"]
        return AwsCredentials(
            access_key_id=creds["AccessKeyId"],
            secret_access_key=creds["SecretAccessKey"],
            session_token=creds["SessionToken"],
            expiration=creds["Expiration"],
        )
