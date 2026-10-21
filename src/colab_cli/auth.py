# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import enum
import json
import logging
from importlib import resources
import os
import threading
import warnings
from typing import Optional

import google.auth
import keyring
import typer
from google.auth.transport import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

logger = logging.getLogger(__name__)


class AuthProvider(str, enum.Enum):
    """Authentication strategy for talking to the Colab backend.

    Values are the lowercase strings accepted by the global ``--auth`` flag.
    """

    OAUTH2 = "oauth2"
    ADC = "adc"


# Standard Scopes for Colab and Drive (Public Auth)
PUBLIC_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/colaboratory",
    "https://www.googleapis.com/auth/drive.file",
]


KEYRING_SERVICE_NAME = "google-colab-cli"
KEYRING_USERNAME = "oauth_token"
KEYRING_TIMEOUT_SECONDS = 30.0


def _run_with_timeout(func, *args, timeout: float):
    """Run a blocking callable in a daemon thread and raise TimeoutError if it hangs."""
    result = [None]
    error: list[Optional[BaseException]] = [None]

    def target() -> None:
        try:
            result[0] = func(*args)
        except BaseException as exc:
            error[0] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        op_name = getattr(func, "__name__", None) or repr(func)
        raise TimeoutError(f"Operation '{op_name}' timed out after {timeout:.1f}s.")
    if error[0] is not None:
        raise error[0]
    return result[0]


def _get_token_from_keyring() -> Optional[str]:
    return _run_with_timeout(
        keyring.get_password,
        KEYRING_SERVICE_NAME,
        KEYRING_USERNAME,
        timeout=KEYRING_TIMEOUT_SECONDS,
    )


def _save_token_to_keyring(token_json: str) -> None:
    _run_with_timeout(
        keyring.set_password,
        KEYRING_SERVICE_NAME,
        KEYRING_USERNAME,
        token_json,
        timeout=KEYRING_TIMEOUT_SECONDS,
    )

# Remote copy-paste OAuth flow.
#
# We deliberately do NOT use a localhost redirect (`run_local_server`) or the
# out-of-band (OOB) redirect `urn:ietf:wg:oauth:2.0:oob`. OOB was blocked by
# Google in 2022 ("The out-of-band (OOB) flow has been blocked in order to
# keep users secure") and a localhost server is environment-dependent (fails
# on headless/remote/container hosts, requires an auto-openable browser, etc.).
#
# Instead we use the same mechanism `gcloud auth application-default login`
# uses: a real registered HTTPS landing page that displays the authorization
# code for the user to copy & paste, combined with the `token_usage=remote`
# consent parameter. This works identically in local and remote environments.
#
# The landing page below is registered to Google's cloud-SDK OAuth client
# (`764086051850-...`), which is also the client shipped in
# `colab_cli/oauth_config.json`; reusing another client id with this redirect
# yields `redirect_uri_mismatch`.
REMOTE_REDIRECT_URI = "https://sdk.cloud.google.com/applicationdefaultauthcode.html"


def _run_remote_flow(client_config: dict) -> Credentials:
    """Run the remote copy-paste OAuth2 flow.

    Prints an authorization URL, waits for the user to sign in and paste back
    the authorization code shown on Google's landing page, then exchanges the
    code for credentials. See ``REMOTE_REDIRECT_URI`` for why this is preferred
    over a localhost server or the blocked OOB flow.
    """
    flow = InstalledAppFlow.from_client_config(client_config, PUBLIC_SCOPES)
    flow.redirect_uri = REMOTE_REDIRECT_URI
    auth_url, _ = flow.authorization_url(prompt="consent", token_usage="remote")

    typer.echo("\nTo authorize colab-cli, visit this URL in any browser:\n", err=True)
    typer.echo("  " + auth_url + "\n", err=True)
    typer.echo("After approving, Google will display an authorization code.", err=True)
    code = input("Enter the authorization code: ").strip()

    flow.fetch_token(code=code)
    return flow.credentials


def _get_google_auth_credentials(config_path: str) -> Credentials:
    """
    Retrieves credentials using standard public OAuth2 flow.
    """
    client_config = None
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            client_config = json.load(f)
    else:
        # Last resort: try inlined config
        try:
            config_resource = resources.files("colab_cli").joinpath("oauth_config.json")
            if config_resource.is_file():
                client_config = json.loads(config_resource.read_text())
        except Exception as e:
            logger.debug(f"Failed to load inlined config: {e}")

    if not client_config:
        raise FileNotFoundError(
            f"Client OAuth config not found at {config_path} and no inlined config available. "
            "Please provide a valid path via -c/--client-oauth-config."
        )

    creds = None

    try:
        token_json = _get_token_from_keyring()
        if token_json:
            creds = Credentials.from_authorized_user_info(
                json.loads(token_json), PUBLIC_SCOPES
            )
    except Exception as e:
        logger.warning(f"Failed to load token from keyring: {e}")

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                logger.warning(f"Failed to refresh token: {e}")
                creds = None

        if not creds:
            creds = _run_remote_flow(client_config)

        # Save the credentials for the next run
        try:
            _save_token_to_keyring(creds.to_json())
        except Exception as e:
            logger.warning(f"Failed to save token to keyring: {e}")

    return creds


def _get_adc_credentials() -> Credentials:
    """Retrieves credentials using Google Application Default Credentials.

    Honors the standard ADC discovery chain (``GOOGLE_APPLICATION_CREDENTIALS``,
    ``gcloud auth application-default login``, GCE/GKE metadata server, etc.).

    The RuntimeService at colab.pa.googleapis.com requires the `colaboratory`
    scope. Most ADC credential types (service accounts, GCE/GKE, impersonated)
    support `with_scopes`; user credentials minted by
    `gcloud auth application-default login` do not. For the latter, the user
    must run `gcloud auth application-default login` with
    `--scopes=openid,https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/colaboratory`
    (`openid` and `cloud-platform` are required by `gcloud` itself; `userinfo.email`
    is required by the session backend; `colaboratory` is retained for Colab APIs).
    """
    # `google.auth._default` emits a UserWarning when ADC user credentials
    # don't have a quota project pinned ("Your application has authenticated
    # using end user credentials from Google Cloud SDK without a quota
    # project. You might receive a 'quota exceeded' or 'API not enabled'
    # error.").
    #
    # That warning shows up on every single `colab` invocation under ADC,
    # which is pure noise. Filter it, but keep the scope as tight as possible:
    # only this exact message, only during this one call.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"Your application has authenticated using end user credentials.*",
            category=UserWarning,
        )
        creds, _ = google.auth.default(scopes=list(PUBLIC_SCOPES))

    if not creds.valid:
        from google.auth import compute_engine

        if isinstance(creds, compute_engine.Credentials):
            creds = None
        else:
            logger.warning("Failed to obtain valid ADC credentials.")
            try:
                logger.warning("Trying to refresh ADC credentials")
                creds.refresh(Request())
            except Exception as e:
                logger.warning(f"Failed to refresh token: {e}")
                creds = None

    if not creds:
        typer.echo(
            "No valid default credentials found. To authenticate, run:\n\n"
            "  gcloud auth application-default login \\\n"
            "      --scopes=openid,"
            "https://www.googleapis.com/auth/cloud-platform,"
            "https://www.googleapis.com/auth/userinfo.email,"
            "https://www.googleapis.com/auth/colaboratory\n",
            err=True,
        )
        exit(1)

    # Some credential subclasses ignore the `scopes=` kwarg in `default()`
    # (e.g. user creds), so re-apply via `with_scopes` when supported.
    if getattr(creds, "requires_scopes", False):
        try:
            creds = creds.with_scopes(list(PUBLIC_SCOPES))
        except Exception as e:  # NotImplementedError for non-scopable creds.
            logger.debug(f"Could not augment ADC scopes via with_scopes: {e}")
    return creds


def get_credentials(
    config_path: Optional[str] = None,
    provider: AuthProvider = AuthProvider.OAUTH2,
) -> requests.AuthorizedSession:
    """Unified entry point for retrieving an authorized session.

    Args:
      config_path: Path to the OAuth2 client config JSON. Only consulted when
        ``provider`` is ``OAUTH2``.
      provider: Which authentication strategy to use.
    """
    if provider == AuthProvider.OAUTH2:
        if not config_path:
            config_path = os.path.expanduser("~/.colab-cli-oauth-config.json")
        creds = _get_google_auth_credentials(config_path)
    elif provider == AuthProvider.ADC:
        creds = _get_adc_credentials()
    else:
        raise ValueError(f"Unknown auth provider: {provider!r}")

    return requests.AuthorizedSession(creds)
