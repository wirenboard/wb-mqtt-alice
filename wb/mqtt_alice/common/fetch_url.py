import json
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Optional

BUNDLE_CRT_PATH = "/var/lib/wb-mqtt-alice/device_bundle.crt.pem"

# Backstop for the whole curl run: --max-time caps one attempt, but retries
# multiply it, and without this the caller can hang forever on a stalled upstream
SUBPROCESS_TIMEOUT_S = 90


def fetch_url(
    url=None,
    method="POST",
    cert_path=BUNDLE_CRT_PATH,
    engine="ateccx08",
    key_type="ENG",
    key_id=None,
    data=None,
    headers=None,
    timeout=10,
    retry_opts: Optional[Iterable[str]] = None,
):
    """
    Performs an authenticated HTTP request via curl with a hardware key

    Parameters:
        url (str): Target URL.
        method (str): HTTP method.
        cert_path (str): Path to the SSL certificate.
        engine (str): Cryptographic engine.
        key_type (str): Key type.
        key_id (str): Key ID in the engine.
        data (dict): Request body as a dictionary.
        headers (dict): Additional headers.
        timeout (int): Connect and total transfer timeout of one attempt, seconds.

    Returns:
        dict: {
            "status_code": int,
            "data": dict | str,
            "error": str
        }
    """

    # Checking for certificate availability
    cert_path_obj = Path(cert_path)
    if not cert_path_obj.exists():
        return {
            "status_code": None,
            "data": None,
            "error": f"Certificate file not found: {cert_path}",
        }

    # Prepare data and headers
    if data is None:
        data = {"controller_version": "8.5"}  # Default value
    if headers is None:
        headers = {"Content-Type": "application/json"}

    if not retry_opts:
        retry_opts = [
            "--retry",
            "5",
            "--retry-delay",
            "2",
            "--retry-all-errors",
        ]

    # Create a curl command
    cmd = [
        "curl",
        "-X",
        method.upper(),
        *retry_opts,
        "--cert",
        str(cert_path_obj),
        "--engine",
        engine,
        "--key-type",
        key_type,
        "--key",
        key_id,
        "--tlsv1.3",
        "--connect-timeout",
        str(timeout),
        "--max-time",
        str(timeout),
        "--silent",
        "--write-out",
        "\n%{http_code}",  # Add a status code to the output
    ]

    # Add headers
    for key, value in headers.items():
        cmd.extend(["--header", f"{key}: {value}"])

    # Add JSON data and target URL
    if method.upper() != "GET":
        cmd.extend(["--data", json.dumps(data)])
    cmd.append(url)

    try:
        # Execute command
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return {
            "status_code": None,
            "data": None,
            "error": f"Curl timed out after {SUBPROCESS_TIMEOUT_S}s",
        }
    except OSError as e:
        return {
            "status_code": None,
            "data": None,
            "error": f"Curl error: {e}",
        }

    # Split response and status code
    output = result.stdout.strip()
    if "\n" in output:
        response_data, status_code = output.rsplit("\n", 1)
    else:
        response_data, status_code = "", output

    # Parse JSON
    json_data = None
    if response_data:
        try:
            json_data = json.loads(response_data)
        except json.JSONDecodeError:
            json_data = response_data

    return {
        "status_code": int(status_code) if status_code.isdigit() else None,
        "data": json_data,
        "error": None,
    }
