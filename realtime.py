import json
import logging
import threading

logger = logging.getLogger(__name__)
_clients = {}
_clients_lock = threading.Lock()


def add_client(user_id, websocket):
    with _clients_lock:
        _clients.setdefault(user_id, set()).add(websocket)


def remove_client(user_id, websocket):
    with _clients_lock:
        clients = _clients.get(user_id)
        if clients is None:
            return
        clients.discard(websocket)
        if not clients:
            _clients.pop(user_id, None)


def send_to_user(user_id, event):
    payload = json.dumps(event)
    with _clients_lock:
        clients = tuple(_clients.get(user_id, ()))
    disconnected = []
    for websocket in clients:
        try:
            websocket.send(payload)
        except Exception:
            logger.warning(
                "Removing disconnected realtime chat client for user %s",
                user_id,
                exc_info=True,
            )
            disconnected.append(websocket)
    for websocket in disconnected:
        remove_client(user_id, websocket)


def connected_client_count(user_id):
    with _clients_lock:
        return len(_clients.get(user_id, ()))
