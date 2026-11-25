# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""Build the Lathe image in Daytona and print its name for the sandbox_image valve.

    uv run --script image/build.py [--region us-east-ash-1]

Named images are immutable: a repeat build under a name returns the old image.
The name therefore carries the date and a Dockerfile digest.
"""
import argparse
import datetime
import hashlib
import os
import pathlib
import sys
import time

import httpx

HERE = pathlib.Path(__file__).parent


def api_key() -> str:
    if key := os.environ.get("DAYTONA_API_KEY"):
        return key
    for line in (HERE.parent / ".env").read_text().splitlines():
        if line.startswith("DAYTONA_API_KEY="):
            return line.partition("=")[2].strip().strip("\"'")
    sys.exit("DAYTONA_API_KEY is not set")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="us-east-ash-1")
    args = parser.parse_args()

    dockerfile = (HERE / "Dockerfile").read_text()
    digest = hashlib.sha256(dockerfile.encode()).hexdigest()[:8]
    name = f"lathe-{datetime.date.today():%Y%m%d}-{digest}"

    with httpx.Client(base_url="https://api.daytona.com", timeout=90,
                      headers={"Authorization": f"Bearer {api_key()}"}) as client:
        response = client.post("/images", json={
            "name": name, "dockerfile": dockerfile,
            "region_id": args.region, "cpu_arch": "amd64",
        })
        if response.is_error:
            sys.exit(f"{response.status_code}: {response.text}")
        image = response.json()
        deadline = time.monotonic() + 1800
        while image["status"] not in ("ready", "failed"):
            if time.monotonic() > deadline:
                sys.exit(f"{name} is still {image['status']} after 30 minutes")
            print(f"{name}: {image['status']}", file=sys.stderr)
            started = time.monotonic()
            response = client.get(f"/images/{image['id']}",
                                  params={"wait": "ready", "timeout_ms": 30000})
            response.raise_for_status()
            image = response.json()
            time.sleep(max(0, 2 - (time.monotonic() - started)))

        if image["status"] == "failed":
            logs = client.get(f"/images/{image['id']}/build-logs-url")
            sys.exit(f"{name} failed: {image.get('error')}\n{logs.text}")
        print(f"{name}  {(image.get('bytes') or 0) / 1e6:.0f} MB  {image['id']}")


if __name__ == "__main__":
    main()
