import asyncio

from app.models import Record, RecordError

STRICT_DEFAULT = False


def parse_row(line, sep=",", strict=False):
    def clean(value):
        return value.strip()

    if strict and not line:
        raise RecordError("empty row")
    return Record(fields=[clean(part) for part in line.split(sep)], source=line)


async def parse_stream(lines, sep=","):
    rows = []
    for line in lines:
        rows.append(parse_row(line, sep=sep))
        await asyncio.sleep(0)
    return rows


class RowReader:
    def __init__(self, sep=","):
        self.sep = sep

    def read(self, lines):
        return [parse_row(line, sep=self.sep) for line in lines]

    @staticmethod
    def supported():
        return (",", ";", "\t")
