"""
Push notification sending module via Firebase Cloud Messaging (FCM) for asynchronous user alerts.

This module integrates with an external push notification provider by managing API keys
and device tokens stored in user profiles. It provides functions to queue and dispatch
notifications triggered by events such as generation_loop completion or gap detection,
and registers the capability with the capability_registry upon successful initialization.
"""

import json
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from threading import Lock, Thread
from typing import Any, Optional
from urllib import request, error as urllib_error
from urllib.request import Request

logger = logging.getLogger(__name__)

try:
    from capabilities import capability_registry
except ImportError:
    capability_registry = None
    logger.warning("capability_registry not available; capability will not be registered.")

try:
    from user_communication import get_user_profile, update_user_profile
except ImportError:
    logger.warning("user_communication not available; using stub implementations.")

    def get_user_profile(user_id: str) -> dict:
        return {}

    def update_user_profile(user_id: str, profile: dict) -> bool:
        return False


FCM_SEND_URL = "https://fcm.googleapis.com/fcm/send"
DEFAULT_QUEUE_WORKERS = 2
MAX_RETRY_ATTEMPTS = 3
RETRY_BACKOFF_BASE = 2.0


class NotificationPriority(Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class NotificationStatus(Enum):
    QUEUED = "queued"
    DISPATCHED = "dispatched"
    FAILED = "failed"
    RETRYING = "retrying"


@dataclass
class PushNotification:
    title: str
    body: str
    user_id: str
    priority: NotificationPriority = NotificationPriority.NORMAL
    data: dict = field(default_factory=dict)
    notification_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: NotificationStatus = field(default=NotificationStatus.QUEUED)
    attempt_count: int = 0
    created_at: float = field(default_factory=time.time)


@dataclass
class FcmConfig:
    server_key: str
    sender_id: str = ""
    timeout_seconds: int = 10


_fcm_config: Optional[FcmConfig] = None
_notification_queue: deque = deque()
_queue_lock = Lock()
_worker_threads: list = []
_is_running = False


def configure_fcm(server_key: str, sender_id: str = "", timeout_seconds: int = 10) -> bool:
    """Configure Firebase Cloud Messaging credentials."""
    global _fcm_config
    if not server_key or not isinstance(server_key, str):
        logger.error("Invalid FCM server key provided.")
        return False
    _fcm_config = FcmConfig(
        server_key=server_key,
        sender_id=sender_id,
        timeout_seconds=timeout_seconds,
    )
    logger.info("FCM configuration updated successfully.")
    return True


def get_device_token(user_id: str) -> Optional[str]:
    """Retrieve the FCM device token from a user's profile."""
    profile = get_user_profile(user_id)
    return profile.get("fcm_device_token")


def register_device_token(user_id: str, device_token: str) -> bool:
    """Store an FCM device token in the user's profile."""
    if not device_token:
        logger.warning("Empty device token provided for user %s.", user_id)
        return False
    profile = get_user_profile(user_id)
    profile["fcm_device_token"] = device_token
    success = update_user_profile(user_id, profile)
    if success:
        logger.info("Device token registered for user %s.", user_id)
    else:
        logger.warning("Failed to store device token for user %s.", user_id)
    return success


def queue_notification(
    user_id: str,
    title: str,
    body: str,
    priority: NotificationPriority = NotificationPriority.NORMAL,
    data: Optional[dict] = None,
) -> Optional[str]:
    """Queue a push notification for a user. Returns the notification_id or None on failure."""
    if not user_id or not title or not body:
        logger.error("user_id, title, and body are required to queue a notification.")
        return None
    notification = PushNotification(
        title=title,
        body=body,
        user_id=user_id,
        priority=priority,
        data=data or {},
    )
    with _queue_lock:
        _notification_queue.append(notification)
    logger.debug("Notification %s queued for user %s.", notification.notification_id, user_id)
    return notification.notification_id


def _build_fcm_payload(notification: PushNotification, device_token: str) -> dict:
    """Build the FCM API payload from a PushNotification object."""
    return {
        "to": device_token,
        "priority": notification.priority.value,
        "notification": {
            "title": notification.title,
            "body": notification.body,
        },
        "data": notification.data,
    }


def _send_fcm_request(payload: dict) -> tuple[bool, str]:
    """Send an HTTP POST request to the FCM API. Returns (success, message)."""
    if _fcm_config is None:
        return False, "FCM not configured."
    encoded_payload = json.dumps(payload).encode("utf-8")
    req = Request(
        FCM_SEND_URL,
        data=encoded_payload,
        headers={
            "Authorization": f"key={_fcm_config.server_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=_fcm_config.timeout_seconds) as response:
            response_body = json.loads(response.read().decode("utf-8"))
            if response_body.get("failure", 0) > 0:
                return False, f"FCM reported failure: {response_body}"
            return True, "OK"
    except urllib_error.HTTPError as exc:
        return False, f"HTTP error {exc.code}: {exc.reason}"
    except urllib_error.URLError as exc:
        return False, f"URL error: {exc.reason}"
    except Exception as exc:
        return False, f"Unexpected error: {exc}"


def dispatch_notification(notification: PushNotification) -> bool:
    """Attempt to dispatch a single notification with retry logic. Returns True on success."""
    device_token = get_device_token(notification.user_id)
    if not device_token:
        logger.warning(
            "No device token for user %s; cannot dispatch notification %s.",
            notification.user_id,
            notification.notification_id,
        )
        notification.status = NotificationStatus.FAILED
        return False

    for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
        notification.attempt_count = attempt
        notification.status = NotificationStatus.RETRYING if attempt > 1 else NotificationStatus.DISPATCHED
        payload = _build_fcm_payload(notification, device_token)
        success, message = _send_fcm_request(payload)
        if success:
            notification.status = NotificationStatus.DISPATCHED
            logger.info(
                "Notification %s dispatched successfully on attempt %d.",
                notification.notification_id,
                attempt,
            )
            return True
        logger.warning(
            "Notification %s failed on attempt %d: %s",
            notification.notification_id,
            attempt,
            message,
        )
        if attempt < MAX_RETRY_ATTEMPTS:
            time.sleep(RETRY_BACKOFF_BASE ** attempt)

    notification.status = NotificationStatus.FAILED
    logger.error("Notification %s permanently failed after %d attempts.", notification.notification_id, MAX_RETRY_ATTEMPTS)
    return False


def _worker_loop() -> None:
    """Worker thread loop that processes notifications from the queue."""
    while _is_running:
        notification = None
        with _queue_lock:
            if _notification_queue:
                notification = _notification_queue.popleft()
        if notification:
            dispatch_notification(notification)
        else:
            time.sleep(0.1)


def start_notification_workers(num_workers: int = DEFAULT_QUEUE_WORKERS) -> bool:
    """Start background worker threads to process the notification queue."""
    global _is_running, _worker_threads
    if _is_running:
        logger.warning("Notification workers are already running.")
        return False
    _is_running = True
    _worker_threads = []
    for i in range(num_workers):
        t = Thread(target=_worker_loop, name=f"push-notif-worker-{i}", daemon=True)
        t.start()
        _worker_threads.append(t)
    logger.info("Started %d notification worker thread(s).", num_workers)
    return True


def stop_notification_workers() -> None:
    """Stop all background notification worker threads."""
    global _is_running
    _is_running = False
    for t in _worker_threads:
        t.join(timeout=2.0)
    logger.info("All notification worker threads stopped.")


def notify_generation_loop_complete(user_id: str, loop_details: Optional[dict] = None) -> Optional[str]:
    """Queue a push notification when a generation loop completes."""
    data = loop_details or {}
    return queue_notification(
        user_id=user_id,
        title="Generation Complete",
        body="Your generation loop has finished processing.",
        priority=NotificationPriority.NORMAL,
        data=data,
    )


def notify_gap_detected(user_id: str, gap_details: Optional[dict] = None) -> Optional[str]:
    """Queue a push notification when a gap is detected."""
    data = gap_details or {}
    return queue_notification(
        user_id=user_id,
        title="Gap Detected",
        body="A gap has been detected in your data stream. Please review.",
        priority=NotificationPriority.HIGH,
        data=data,
    )


def simulate_notification(user_id: str, title: str, body: str) -> dict[str, Any]:
    """Simulate a notification dispatch without contacting FCM. Useful for integration testing."""
    notification = PushNotification(
        title=title,
        body=body,
        user_id=user_id,
        priority=NotificationPriority.NORMAL,
    )
    device_token = get_device_token(user_id)
    if not device_token:
        device_token = "simulated-token-for-testing"
    payload = _build_fcm_payload(notification, device_token)
    logger.info("Simulated notification payload: %s", json.dumps(payload, indent=2))
    notification.status = NotificationStatus.DISPATCHED
    return {
        "notification_id": notification.notification_id,
        "status": notification.status.value,
        "payload": payload,
        "simulated": True,
    }


def _run_integration_test() -> bool:
    """Run a basic integration test by simulating a notification attempt."""
    logger.info("Running push notification integration test...")
    test_user_id = "test-user-001"
    result = simulate_notification(
        user_id=test_user_id,
        title="Integration Test",
        body="This is a simulated push notification test.",
    )
    if result.get("simulated") and result.get("status") == NotificationStatus.DISPATCHED.value:
        logger.info("Push notification integration test passed.")
        return True
    logger.error("Push notification integration test failed: %s", result)
    return False


def _register_capability() -> None:
    """Register the push_notification capability with the capability_registry."""
    if capability_registry is None:
        logger.warning("Skipping capability registration; capability_registry unavailable.")
        return
    try:
        capability_registry.register(
            name="push_notification",
            description="Send push notifications via Firebase Cloud Messaging.",
            entry_points={
                "queue_notification": queue_notification,
                "dispatch_notification": dispatch_notification,
                "notify_generation_loop_complete": notify_generation_loop_complete,
                "notify_gap_detected": notify_gap_detected,
                "simulate_notification": simulate_notification,
                "register_device_token": register_device_token,
            },
        )
        logger.info("push_notification capability registered successfully.")
    except Exception as exc:
        logger.error("Failed to register push_notification capability: %s", exc)


def initialize(server_key: str = "", sender_id: str = "", num_workers: int = DEFAULT_QUEUE_WORKERS) -> bool:
    """
    Initialize the push notification module.

    Configures FCM (if credentials provided), runs an integration test,
    starts worker threads, and registers the capability.
    """
    if server_key:
        configure_fcm(server_key=server_key, sender_id=sender_id)
    test_passed = _run_integration_test()
    if test_passed:
        start_notification_workers(num_workers=num_workers)
        _register_capability()
    return test_passed


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    initialize()