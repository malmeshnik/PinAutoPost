import os
import uuid

import requests


def main():
    webhook_url = os.getenv("PUBLISH_URL")
    image_url = os.getenv("PUBLISH_IMAGE_URL")
    board_id = os.getenv("PUBLISH_BOARD_ID")
    board_name = os.getenv("PUBLISH_BOARD_NAME")
    if not webhook_url or not image_url or not (board_id or board_name):
        raise SystemExit(
            "Set PUBLISH_URL, PUBLISH_IMAGE_URL, and either "
            "PUBLISH_BOARD_ID or PUBLISH_BOARD_NAME."
        )
    idempotency_key = os.getenv("PUBLISH_IDEMPOTENCY_KEY", str(uuid.uuid4()))

    payload = {
        "title": os.getenv("PUBLISH_TITLE", ""),
        "description": os.getenv("PUBLISH_DESCRIPTION", ""),
        "image_url": image_url,
    }
    if board_id:
        payload["board_id"] = board_id
    else:
        payload["board_name"] = board_name

    response = requests.post(
        webhook_url,
        json=payload,
        headers={
            "Accept": "application/json",
            "Idempotency-Key": idempotency_key,
        },
        timeout=65,
    )
    print(f"Idempotency-Key: {idempotency_key}")
    print(f"Status: {response.status_code}")
    print(response.text)


if __name__ == "__main__":
    main()
