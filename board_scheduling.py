"""Select a free execution identity for one agent role."""


def availableLane(lanes, busy, preferred=None):
    if lanes and lanes[0] not in busy:
        return lanes[0]
    if preferred in lanes and preferred not in busy:
        return preferred
    return next((lane for lane in lanes if lane not in busy), None)
