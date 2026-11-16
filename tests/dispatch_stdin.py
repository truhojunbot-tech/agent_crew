"""Captured subprocess stdin for dispatcher prompt assertions.

Since #1026 the dispatcher hands the child an open prompt file as ``stdin``;
pass the spawn's ``stdin`` kwarg so the prompt is read from it at spawn time.
"""


class CapturedStdin:
    def __init__(self, source=None):
        self.data = bytearray()
        if source is not None and hasattr(source, "read"):
            self.data.extend(source.read())

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass

    def text(self):
        return self.data.decode()
