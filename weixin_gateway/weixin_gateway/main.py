"""Weixin Gateway runtime entrypoint."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import threading

from .api import create_server
from .notification import GatewayNotificationRuntime, NotificationConfig
from .remote_work import GatewayRemoteWorkRuntime, RemoteWorkConfig
from .service import ControllerClient, GatewayService
from .store import GatewayStore, IdentityStore


async def async_main() -> None:
    data_dir = Path(os.environ.get("WEIXIN_DATA_DIR", "/data")).resolve()
    if not data_dir.is_absolute():
        raise RuntimeError("WEIXIN_DATA_DIR 必须是绝对路径")
    try:
        allowed = json.loads(os.environ.get("WEIXIN_ALLOWED_USER_IDS_JSON", "[]"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("allowed_user_ids 配置无效") from exc
    if not isinstance(allowed, list):
        raise RuntimeError("allowed_user_ids 必须是列表")

    identity_store = IdentityStore(data_dir)
    gateway_store = GatewayStore(
        os.environ.get("WEIXIN_DATABASE_PATH", data_dir / "gateway.sqlite3"),
        data_dir=data_dir,
        spool_ttl_seconds=int(os.environ.get("WEIXIN_SPOOL_TTL_SECONDS", "86400")),
    )
    controller = ControllerClient(
        os.environ.get("WEIXIN_CONTROLLER_BASE_URL", ""),
        os.environ.get("WEIXIN_CONTROLLER_API_TOKEN", ""),
    )
    bootstrap = {
        "account_id": os.environ.get("WEIXIN_ACCOUNT_ID", ""),
        "token": os.environ.get("WEIXIN_ILINK_TOKEN", ""),
        "base_url": os.environ.get("WEIXIN_ILINK_BASE_URL", "https://ilinkai.weixin.qq.com"),
        "cdn_base_url": os.environ.get("WEIXIN_CDN_BASE_URL", "https://novac2c.cdn.weixin.qq.com/c2c"),
        "user_id": os.environ.get("WEIXIN_SELF_USER_ID", ""),
        "allowed_user_ids": allowed,
        "get_updates_buf": "",
        "context_tokens": {},
    }
    service = GatewayService(
        identity_store=identity_store,
        store=gateway_store,
        controller=controller,
        bootstrap_identity=bootstrap,
        poller_enabled=os.environ.get("WEIXIN_POLLER_ENABLED", "false").lower() == "true",
        owner_pairing_enabled=os.environ.get("WEIXIN_OWNER_PAIRING_ENABLED", "false").lower() == "true",
        activation_confirmation=os.environ.get("WEIXIN_ACTIVATION_CONFIRMATION", ""),
        max_media_bytes=int(os.environ.get("WEIXIN_MAX_MEDIA_BYTES", str(20 * 1024 * 1024))),
        controller_ingress_base_url=os.environ.get("WEIXIN_CONTROLLER_INGRESS_BASE_URL", ""),
        remote_work_enabled=os.environ.get("WEIXIN_REMOTE_WORK_ENABLED", "false").lower() == "true",
        remote_work_ttl_seconds=int(os.environ.get("WEIXIN_REMOTE_WORK_TTL_SECONDS", "1800")),
        max_active_identities=int(os.environ.get("WEIXIN_MAX_ACTIVE_IDENTITIES", "5")),
    )
    await service.start()
    loop = asyncio.get_running_loop()
    notification_runtime: GatewayNotificationRuntime | None = None
    remote_work_runtime: GatewayRemoteWorkRuntime | None = None
    server = None
    try:
        if os.environ.get("WEIXIN_NOTIFICATION_BRIDGE_ENABLED", "false").lower() == "true":
            from paho.mqtt import client as mqtt

            service.notification_owner_context()
            notification_runtime = GatewayNotificationRuntime(
                NotificationConfig.from_env(),
                mqtt,
                service=service,
                loop=loop,
            )
            notification_runtime.start()
        if os.environ.get("WEIXIN_REMOTE_WORK_ENABLED", "false").lower() == "true":
            from paho.mqtt import client as mqtt

            remote_work_runtime = GatewayRemoteWorkRuntime(
                RemoteWorkConfig.from_env(),
                mqtt,
                store=gateway_store,
            )
            service.bind_remote_work_runtime(remote_work_runtime)
            remote_work_runtime.start()
        server = create_server(
            "0.0.0.0",
            8103,
            service=service,
            loop=loop,
            attachment_api_token=os.environ["WEIXIN_ATTACHMENT_API_TOKEN"],
        )
        thread = threading.Thread(target=server.serve_forever, name="weixin-ingress", daemon=True)
        thread.start()
        await asyncio.Event().wait()
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if notification_runtime is not None:
            await asyncio.to_thread(notification_runtime.stop)
        if remote_work_runtime is not None:
            await asyncio.to_thread(remote_work_runtime.stop)
        await service.stop()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
