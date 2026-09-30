from dataclasses import dataclass, field

DEFAULT_SEPARATOR = ","


@dataclass
class Record:
    fields: list[str] = field(default_factory=list)
    source: str = ""

    def column(self, index):
        return self.fields[index]

    @property
    def width(self):
        return len(self.fields)


class RecordError(Exception):
    pass
