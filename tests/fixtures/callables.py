def plain(flag, items):
    if flag and items:
        return len(items)
    return 0


async def async_work(value):
    while value:
        value -= 1
    return value


class Worker:
    def run(self, value):
        try:
            return value if value else 0
        except ValueError:
            return 0


def outer(values):
    def inner(value):
        if value:
            return value
        return 0

    return [inner(item) for item in values if item]
