import json
import subprocess
from pathlib import Path

BUNDLE_CRT_PATH = "/var/lib/wb-mqtt-alice/device_bundle.crt.pem"

def fetch_url(
    url=None,
    cert_path=BUNDLE_CRT_PATH,
    engine="ateccx08",
    key_type="ENG",
    key_id=None,
    data=None,
    headers=None,
    timeout=10,
    ):
    """
    Performs an authenticated POST request via curl with a hardware key.

    Parameters:
        url (str): Target URL.
        cert_path (str): Path to the SSL certificate.
        engine (str): Cryptographic engine.
        key_type (str): Key type.
        key_id (str): Key ID in the engine.
        data (dict): Request body as a dictionary.
        headers (dict): Additional headers.
        timeout (int): Connection timeout in seconds.

    Returns:
        dict: {
            "status_code": int,
            "data": dict | str,
            "error": str
        }
    """
        
    # Checking for certificate availability
    cert_path = Path(BUNDLE_CRT_PATH)
    if not cert_path.exists():
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

    # Create a curl command
    cmd = [
        "curl",
        "-X", "POST",
        "--cert", cert_path,
        "--engine", engine,
        "--key-type", key_type,
        "--key", key_id,
        "--tlsv1.3",
        "--connect-timeout", str(timeout),
        "--silent",
        "--write-out", "\n%{http_code}",  # Add a status code to the output
    ]

    # Add headers
    for key, value in headers.items():
        cmd.extend(["--header", f"{key}: {value}"])

    # Add JSON data and target URL
    cmd.extend(["--data", json.dumps(data), url])

    try:
        # Execute command
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        return {
            "status_code": None,
            "data": None,
            "error": f"Curl error: {e.stderr.strip() or e.stdout.strip()}",
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
    return {
        "status_code": int(status_code) if status_code.isdigit() else None,
        "data": None,
        "error": None,
    }
