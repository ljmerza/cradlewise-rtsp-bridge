"""AWS Cognito authentication for the Cradlewise API."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import boto3
from botocore.credentials import Credentials
from pycognito import Cognito

from .bootstrap import AppConfig
from .exceptions import CradlewiseAuthError

_LOGGER = logging.getLogger(__name__)


@dataclass
class CradlewiseCredentials:
    """Holds both Cognito and IAM credentials."""

    cognito: Cognito
    aws: Credentials

    @property
    def access_key(self) -> str:
        return self.aws.access_key

    @property
    def secret_key(self) -> str:
        return self.aws.secret_key

    @property
    def session_token(self) -> str:
        return self.aws.token


class CradlewiseAuth:
    """Handles Cognito SRP authentication and IAM credential exchange."""

    def __init__(self, email: str, password: str, app_config: AppConfig) -> None:
        self._email = email
        self._password = password
        self._config = app_config
        self._credentials: CradlewiseCredentials | None = None

    @property
    def email(self) -> str:
        return self._email

    @property
    def credentials(self) -> CradlewiseCredentials | None:
        return self._credentials

    @property
    def app_config(self) -> AppConfig:
        return self._config

    async def authenticate(self) -> CradlewiseCredentials:
        """Authenticate with Cognito and obtain AWS IAM credentials."""
        try:
            cognito = await asyncio.to_thread(self._cognito_auth)
            aws_creds = await asyncio.to_thread(self._exchange_for_iam, cognito)
            self._credentials = CradlewiseCredentials(cognito=cognito, aws=aws_creds)
            return self._credentials
        except Exception as err:
            raise CradlewiseAuthError(f"Authentication failed: {err}") from err

    async def ensure_valid(self) -> CradlewiseCredentials:
        """Ensure credentials are valid, refreshing if needed."""
        if self._credentials is None:
            return await self.authenticate()

        try:
            await asyncio.to_thread(self._credentials.cognito.check_token)
            aws_creds = await asyncio.to_thread(
                self._exchange_for_iam, self._credentials.cognito
            )
            self._credentials = CradlewiseCredentials(
                cognito=self._credentials.cognito, aws=aws_creds
            )
            return self._credentials
        except Exception:
            return await self.authenticate()

    def _cognito_auth(self) -> Cognito:
        """Perform Cognito SRP authentication (blocking)."""
        cognito = Cognito(
            self._config.cognito_user_pool_id,
            self._config.cognito_app_client_id,
            client_secret=self._config.cognito_app_client_secret,
            username=self._email,
        )
        cognito.authenticate(password=self._password)
        return cognito

    def _exchange_for_iam(self, cognito: Cognito) -> Credentials:
        """Exchange Cognito ID token for AWS IAM credentials (blocking)."""
        region = self._config.cognito_region
        client = boto3.client("cognito-identity", region_name=region)
        provider_key = (
            f"cognito-idp.{region}.amazonaws.com/{self._config.cognito_user_pool_id}"
        )

        identity_response = client.get_id(
            IdentityPoolId=self._config.cognito_identity_pool_id,
            Logins={provider_key: cognito.id_token},
        )

        credentials_response = client.get_credentials_for_identity(
            IdentityId=identity_response["IdentityId"],
            Logins={provider_key: cognito.id_token},
        )

        creds = credentials_response["Credentials"]
        return Credentials(
            access_key=creds["AccessKeyId"],
            secret_key=creds["SecretKey"],
            token=creds["SessionToken"],
        )
