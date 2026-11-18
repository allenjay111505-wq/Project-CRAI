import json
import logging
import threading

logger = logging.getLogger(__name__)
_call_clients = {}
_call_clients_lock = threading.Lock()


def add_call_client(request_id, user_id, websocket):
    with _call_clients_lock:
        clients = _call_clients.setdefault(request_id, set())
        existing_peers = tuple(
            (peer_id, peer_socket)
            for peer_id, peer_socket in clients
            if peer_id != user_id
        )
        clients.add((user_id, websocket))
    for peer_id, peer_socket in existing_peers:
        try:
            peer_socket.send(
                json.dumps({"type": "peer-joined", "user_id": user_id})
            )
        except Exception:
            logger.warning(
                "Could not notify call participant %s that a peer joined",
                peer_id,
                exc_info=True,
            )
    return bool(existing_peers)


def remove_call_client(request_id, user_id, websocket):
    with _call_clients_lock:
        clients = _call_clients.get(request_id)
        if clients is None:
            return
        clients.discard((user_id, websocket))
        if not clients:
            _call_clients.pop(request_id, None)


def send_call_signal(request_id, sender_id, recipient_id, event):
    payload = json.dumps(event)
    with _call_clients_lock:
        clients = tuple(_call_clients.get(request_id, ()))
    for user_id, websocket in clients:
        if user_id != recipient_id or user_id == sender_id:
            continue
        try:
            websocket.send(payload)
        except Exception:
            logger.warning(
                "Could not relay call signaling to user %s",
                user_id,
                exc_info=True,
            )
            remove_call_client(request_id, user_id, websocket)
