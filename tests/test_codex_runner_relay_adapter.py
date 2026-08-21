from __future__ import annotations

import io
import json
from pathlib import Path
import unittest
from urllib.error import HTTPError

from codex_controller.runner_relay import (
    RelayPublishError,
    RelayPublisher,
    validate_internal_relay_url,
    validate_relay_auth_config,
)


class Response:
    def __init__(self, *, status: int = 202, body: bytes = b"{}") -> None:
        self.status = status
        self.body = body

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


class Opener:
    def __init__(self, *, status: int = 202, body: bytes = b"{}") -> None:
        self.status = status
        self.body = body
        self.requests: list[tuple[object, int]] = []

    def __call__(self, request: object, *, timeout: int) -> Response:
        self.requests.append((request, timeout))
        return Response(status=self.status, body=self.body)


class HttpErrorOpener:
    def __call__(self, request: object, *, timeout: int) -> Response:
        del timeout
        raise HTTPError(
            request.full_url,
            503,
            "Service Unavailable",
            {},
            io.BytesIO(b"runner_offline"),
        )


class RelayPublisherTests(unittest.TestCase):
    def test_controller_option_and_env_keep_callback_identity_separate(self) -> None:
        root = Path(__file__).resolve().parents[1] / "codex_controller"
        config = (root / "config.yaml").read_text(encoding="utf-8")
        run_script = (root / "run.sh").read_text(encoding="utf-8")
        main_source = (root / "codex_controller/main.py").read_text(encoding="utf-8")
        api_source = (root / "codex_controller/api.py").read_text(encoding="utf-8")
        self.assertIn('runner_relay_controller_api_token: ""', config)
        self.assertIn("runner_relay_controller_api_token: password", config)
        self.assertIn("CONTROLLER_RUNNER_RELAY_CONTROLLER_API_TOKEN", run_script)
        self.assertIn("local-codex-runner-relay", run_script)
        self.assertNotIn("http://codex-runner-relay:", run_script)
        self.assertIn("CONTROLLER_RUNNER_RELAY_CONTROLLER_API_TOKEN", main_source)
        self.assertIn(
            "runner_relay_controller_api_token=relay_controller_api_token",
            main_source,
        )
        self.assertIn("runner_relay_controller_api_token", api_source)
        self.assertNotIn("runner_relay_api_token=relay_api_token", main_source)

    def test_relay_auth_config_requires_distinct_complete_identities(self) -> None:
        publish_token = "p" * 32
        controller_token = "c" * 32
        self.assertEqual(validate_relay_auth_config("", "", ""), "")
        self.assertEqual(
            validate_relay_auth_config(
                "http://local-codex-runner-relay:8098/",
                publish_token,
                controller_token,
            ),
            "http://local-codex-runner-relay:8098",
        )
        for values in (
            ("http://local-codex-runner-relay:8098", publish_token, ""),
            ("http://local-codex-runner-relay:8098", "", controller_token),
            ("", publish_token, controller_token),
            ("http://local-codex-runner-relay:8098", publish_token, publish_token),
            ("http://local-codex-runner-relay:8098", "short", controller_token),
            ("http://local-codex-runner-relay:8098", publish_token, "short"),
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_relay_auth_config(*values)

    def test_internal_url_is_exact_http_addon_endpoint(self) -> None:
        self.assertEqual(
            validate_internal_relay_url("http://local-codex-runner-relay:8098/"),
            "http://local-codex-runner-relay:8098",
        )
        for value in (
            "http://codex-runner-relay:8098",
            "https://local-codex-runner-relay:8098",
            "http://local-codex-runner-relay",
            "http://local-codex-runner-relay:8099",
            "http://local-codex-runner-relay:8098/admin",
            "http://127.0.0.1:8099",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_internal_relay_url(value)

    def test_publish_uses_bearer_and_exact_runner_path(self) -> None:
        opener = Opener()
        publisher = RelayPublisher(
            "http://local-codex-runner-relay:8098",
            "t" * 32,
            opener=opener,
        )
        document = {"version": 2, "message_type": "request", "sequence": 1}
        runner_id = "RN-" + "A" * 20
        publisher.publish_request(runner_id, document)
        self.assertEqual(len(opener.requests), 1)
        request, timeout = opener.requests[0]
        self.assertEqual(
            request.full_url,
            f"http://local-codex-runner-relay:8098/internal/v1/runners/{runner_id}/request",
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer " + "t" * 32)
        self.assertEqual(json.loads(request.data), {"document": document})
        self.assertEqual(timeout, 10)

        desktop = {
            "version": 1,
            "message_type": "desktop_command",
            "runner_id": runner_id,
            "request_id": "desktop-publish-0001",
        }
        publisher.publish_desktop_command(runner_id, desktop)
        desktop_request, _desktop_timeout = opener.requests[-1]
        self.assertEqual(
            desktop_request.full_url,
            f"http://local-codex-runner-relay:8098/internal/v1/runners/{runner_id}/desktop_command",
        )
        self.assertEqual(json.loads(desktop_request.data), {"document": desktop})

    def test_non_202_and_oversized_documents_are_rejected(self) -> None:
        publisher = RelayPublisher(
            "http://local-codex-runner-relay:8098",
            "t" * 32,
            opener=Opener(status=409),
        )
        with self.assertRaises(RelayPublishError) as context:
            publisher.publish_control("RN-" + "B" * 20, {"action": "cancel"})
        self.assertEqual(context.exception.code, "relay_publish_indeterminate")
        self.assertFalse(context.exception.definitely_undelivered)

        with self.assertRaisesRegex(RuntimeError, "过大"):
            publisher.publish_request("RN-" + "C" * 20, {"value": "x" * (64 * 1024)})

    def test_runner_offline_is_the_only_confirmed_undelivered_response(self) -> None:
        runner_id = "RN-" + "D" * 20
        document = {"message_type": "request", "runner_id": runner_id}
        for opener in (
            Opener(status=503, body=b"runner_offline"),
            HttpErrorOpener(),
        ):
            publisher = RelayPublisher(
                "http://local-codex-runner-relay:8098",
                "t" * 32,
                opener=opener,
            )
            with self.subTest(opener=type(opener).__name__), self.assertRaises(
                RelayPublishError
            ) as context:
                publisher.publish_request(runner_id, document)
            self.assertEqual(context.exception.code, "runner_offline")
            self.assertTrue(context.exception.definitely_undelivered)


if __name__ == "__main__":
    unittest.main()
