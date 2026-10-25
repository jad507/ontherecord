"""A tiny Chrome DevTools Protocol client (standard library only) for driving the GUI headless when testing it.

    google-chrome --headless=new --remote-debugging-port=9333 --user-data-dir=SCRATCH --mute-audio \
        --autoplay-policy=no-user-gesture-required --window-size=1360,1000 about:blank

--mute-audio matters: without it a headless page plays through the machine's speakers, over whatever the person
at it is listening to (Oct 25, while Jeff was coding).

    c = CDP(9333); c.send("Page.navigate", url="http://127.0.0.1:8799/check#SET")
    c.js("expression"); c.key("KeyQ", shift=True); c.type("text"); c.shot("page.png")

Serve a scratch workspace (`ontherecord --config none --workspace DIR serve --port 8799`) with a copy of a set, so
no test answer lands in a real one. Changing only the URL's hash does not reload the page: use Page.reload.
"""
import base64, json, os, socket, struct, sys, time, urllib.request


class CDP:
    def __init__(self, port: int):
        tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
        url = next(t for t in tabs if t["type"] == "page")["webSocketDebuggerUrl"]
        host, path = url[len("ws://"):].split("/", 1)
        h, p = host.split(":")
        self.s = socket.create_connection((h, int(p)))
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall(f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                       f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.s.recv(4096)
        self.rest = buf.split(b"\r\n\r\n", 1)[1]
        self.n = 0
        self.events = []

    def _recv(self, k):
        while len(self.rest) < k:
            self.rest += self.s.recv(65536)
        out, self.rest = self.rest[:k], self.rest[k:]
        return out

    def _frame(self):
        b1, b2 = self._recv(2)
        ln = b2 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", self._recv(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self._recv(8))[0]
        return self._recv(ln)

    def send(self, method, **params):
        self.n += 1
        data = json.dumps({"id": self.n, "method": method, "params": params}).encode()
        mask = os.urandom(4)
        hdr = bytes([0x81])
        ln = len(data)
        hdr += bytes([0x80 | ln]) if ln < 126 else bytes([0x80 | 126]) + struct.pack(">H", ln) if ln < 65536 else bytes([0x80 | 127]) + struct.pack(">Q", ln)
        self.s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
        while True:
            msg = json.loads(self._frame())
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})
            self.events.append(msg)

    def js(self, expr):
        r = self.send("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"].get("exception", {}).get("description") or r["exceptionDetails"])
        return r["result"].get("value")

    KEYS = {"Space": (" ", 32), "Tab": ("Tab", 9), "Escape": ("Escape", 27), "Enter": ("Enter", 13), "Backquote": ("`", 192)}

    def key(self, code, shift=False, alt=False, ctrl=False):
        if code in self.KEYS:
            key, vk = self.KEYS[code]
        elif code.startswith("Key"):
            key, vk = (code[3:] if shift else code[3:].lower()), ord(code[3:])
        elif code.startswith("Digit"):
            key, vk = code[5:], ord(code[5:])
        else:
            key, vk = code, 0
        mods = (8 if shift else 0) | (1 if alt else 0) | (2 if ctrl else 0)
        text = key if len(key) == 1 and not (alt or ctrl) else ""
        self.send("Input.dispatchKeyEvent", type="keyDown", code=code, key=key, windowsVirtualKeyCode=vk, modifiers=mods, text=text)
        self.send("Input.dispatchKeyEvent", type="keyUp", code=code, key=key, windowsVirtualKeyCode=vk, modifiers=mods)

    def type(self, text):
        self.send("Input.insertText", text=text)

    def shot(self, path):
        d = self.send("Page.captureScreenshot", format="png")["data"]
        open(path, "wb").write(base64.b64decode(d))
