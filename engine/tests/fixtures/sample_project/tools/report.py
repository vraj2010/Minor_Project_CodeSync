from app.services.parser import parse_row


def summarise(lines):
    return [parse_row(line).width for line in lines]


if __name__ == "__main__":
    print(summarise(["a,b", "c,d"]))
