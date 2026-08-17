"""Minimal S-expression reader used to recover pin geometry from KiCad symbol
libraries. Only what the generators need: parse, then walk."""


def parse(text):
    """Parse an S-expression document into nested lists of tokens."""
    stack = [[]]
    index, length = 0, len(text)
    while index < length:
        char = text[index]
        if char == "(":
            node = []
            stack[-1].append(node)
            stack.append(node)
            index += 1
        elif char == ")":
            stack.pop()
            index += 1
        elif char == "\"":
            index += 1
            chunk = []
            while index < length and text[index] != "\"":
                if text[index] == "\\":
                    index += 1
                chunk.append(text[index])
                index += 1
            index += 1
            stack[-1].append("".join(chunk))
        elif char.isspace():
            index += 1
        else:
            start = index
            while index < length and not text[index].isspace() and text[index] not in "()\"":
                index += 1
            stack[-1].append(text[start:index])
    return stack[0]


def children(node, tag):
    """Direct child lists of `node` whose head token is `tag`."""
    return [item for item in node
            if isinstance(item, list) and item and item[0] == tag]


def first(node, tag):
    found = children(node, tag)
    return found[0] if found else None


def symbol_pins(library_text):
    """Map each top-level symbol name to [(number, x, y, angle, length)].

    Pin coordinates are the connection points in library space, where +Y is up.
    Sub-unit symbols are flattened because every symbol here is single-unit.
    """
    document = parse(library_text)
    root = document[0]
    result = {}
    for symbol in children(root, "symbol"):
        name = symbol[1]
        pins = []
        for unit in children(symbol, "symbol"):
            for pin in children(unit, "pin"):
                at = first(pin, "at")
                number = first(pin, "number")
                length = first(pin, "length")
                if at is None or number is None:
                    continue
                angle = float(at[3]) if len(at) > 3 else 0.0
                pins.append((number[1], float(at[1]), float(at[2]), angle,
                             float(length[1]) if length else 2.54))
        # Pinless symbols (mounting holes, graphics) are kept so callers can
        # still instantiate them.
        result[name] = pins
    return result
