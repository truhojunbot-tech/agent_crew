"""Writable subprocess stdin for dispatcher prompt assertions."""


class CapturedStdin:
    def __init__(self):
        self.data = bytearray()

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
