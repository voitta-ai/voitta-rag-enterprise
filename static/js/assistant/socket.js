// The assistant's streaming channel (/ws/assistant) with reconnection.
//
// Frames sent while (re)connecting are queued and flushed on open. After a
// reconnect the owner re-watches its conversation (``onOpen``) — turns run
// server-side independently of this socket, so nothing is lost while it is
// down; the owner reloads the transcript over REST and keeps listening.
// A ``4401`` close means signed out: stop reconnecting.

const WS_CLOSE_UNAUTHENTICATED = 4401;
const WS_CLOSE_FORBIDDEN_ORIGIN = 4403;
const BACKOFF_MS = [500, 1000, 2000, 4000, 8000, 15000];

export class AssistantSocket {
    constructor({ onFrame, onOpen, onStatus }) {
        this._onFrame = onFrame;
        this._onOpen = onOpen;
        this._onStatus = onStatus;
        this._ws = null;
        this._queue = [];
        this._attempt = 0;
        this._stopped = false;
        this._timer = null;
    }

    connect() {
        this._stopped = false;
        if (this._ws && this._ws.readyState <= WebSocket.OPEN) return;
        const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
        const ws = new WebSocket(`${proto}//${window.location.host}/ws/assistant`);
        this._ws = ws;
        this._onStatus?.("connecting");
        ws.addEventListener("message", (e) => {
            let frame;
            try { frame = JSON.parse(e.data); } catch { return; }
            if (frame.type === "hello") {
                this._attempt = 0;
                this._onStatus?.("open");
                const queued = this._queue.splice(0);
                this._onOpen?.();
                for (const f of queued) ws.send(JSON.stringify(f));
                return;
            }
            this._onFrame(frame);
        });
        ws.addEventListener("close", (e) => {
            if (this._ws !== ws) return;
            this._ws = null;
            if (this._stopped) return;
            if (e.code === WS_CLOSE_UNAUTHENTICATED || e.code === WS_CLOSE_FORBIDDEN_ORIGIN) {
                this._onStatus?.("closed");
                return;
            }
            this._onStatus?.("reconnecting");
            const delay = BACKOFF_MS[Math.min(this._attempt, BACKOFF_MS.length - 1)];
            this._attempt += 1;
            this._timer = setTimeout(() => this.connect(), delay);
        });
    }

    send(frame) {
        if (this._ws && this._ws.readyState === WebSocket.OPEN) {
            this._ws.send(JSON.stringify(frame));
        } else {
            this._queue.push(frame);
            this.connect();
        }
    }

    close() {
        this._stopped = true;
        clearTimeout(this._timer);
        this._queue = [];
        this._ws?.close();
        this._ws = null;
    }
}
