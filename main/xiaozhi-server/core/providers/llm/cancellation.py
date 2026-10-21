"""Session-scoped cancellation, including requests waiting for the model lock."""
import threading


class RequestTicket:
    def __init__(self):
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.response = None

    def attach(self, response):
        with self.lock:
            self.response = response
        if self.cancelled.is_set():
            self.cancel()

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            response = self.response
        if response is not None:
            # Closing an HTTP reader can block; never do it on the audio loop.
            threading.Thread(target=self._close, args=(response,), daemon=True).start()

    @staticmethod
    def _close(response):
        try:
            response.close()
        except Exception:
            pass


class SessionRequests:
    def __init__(self):
        self.lock = threading.Lock()
        self.pending = {}

    def register(self, session):
        ticket = RequestTicket()
        if session:
            with self.lock:
                self.pending.setdefault(session, set()).add(ticket)
        return ticket

    def finish(self, session, ticket):
        if session:
            with self.lock:
                requests = self.pending.get(session, set())
                requests.discard(ticket)
                if not requests:
                    self.pending.pop(session, None)

    def cancel(self, session):
        if not session:
            return
        with self.lock:
            requests = list(self.pending.get(session, ()))
        for ticket in requests:
            ticket.cancel()
