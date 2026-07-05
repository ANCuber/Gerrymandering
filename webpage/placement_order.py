import json

from config import GRID_MAP

HEX_DIRECTIONS = [(1, 0), (1, -1), (0, -1), (-1, 0), (-1, 1), (0, 1)]
ROW_LENGTHS = [8, 9, 10, 11, 12, 13, 14, 15, 14, 13, 12, 11, 10, 9, 8]
BORDER_ASYMMETRIC_ROW_LENGTHS = [8, 9, 10, 11, 12, 13, 14, 15, 14, 13, 12, 11, 10, 9, 8]
BLOCKED_CELLS = {
    'standard': set(),
    'border_asymmetric': {
        'R2C3', 'R3C7', 'R4C2', 'R5C10', 'R6C5',
        'R8C12', 'R9C4', 'R10C8', 'R11C6', 'R12C13',
        'R13C9', 'R14C2', 'R15C7',
        'R1C6', 'R2C8', 'R3C2', 'R4C9', 'R5C4',
        'R7C14', 'R8C3', 'R9C12', 'R11C10', 'R12C2',
        'R14C8'
    },
}


def get_row_lengths(mode=None):
    mode_name = (mode or GRID_MAP or 'standard').lower()
    if mode_name == 'border_asymmetric':
        return BORDER_ASYMMETRIC_ROW_LENGTHS
    return ROW_LENGTHS


def get_blocked_cells(mode=None):
    mode_name = (mode or GRID_MAP or 'standard').lower()
    return set(BLOCKED_CELLS.get(mode_name, set()))


def is_cell_blocked(cell_id, mode=None):
    return cell_id in get_blocked_cells(mode)


def rotate_left(coord):
    x, y = coord
    return (x + y, -x)


def reflect(coord):
    x, y = coord
    return (x, -x - y)


def canonical_shape(shape):
    def normalize(coords):
        xs = [x for x, y in coords]
        ys = [y for x, y in coords]
        min_x = min(xs)
        min_y = min(ys)
        translated = sorted((x - min_x, y - min_y) for x, y in coords)
        return tuple(translated)

    orientations = []
    current = shape
    for _ in range(6):
        orientations.append(normalize(current))
        current = {rotate_left(c) for c in current}

    reflected = {reflect(c) for c in shape}
    current = reflected
    for _ in range(6):
        orientations.append(normalize(current))
        current = {rotate_left(c) for c in current}

    return tuple(min(orientations))


def expand_polyhexes(shapes):
    next_shapes = set()
    for shape in shapes:
        for x, y in shape:
            for dx, dy in HEX_DIRECTIONS:
                neighbor = (x + dx, y + dy)
                if neighbor not in shape:
                    new_shape = set(shape)
                    new_shape.add(neighbor)
                    next_shapes.add(canonical_shape(new_shape))
    return next_shapes


def generate_free_pentahexes():
    shapes = {canonical_shape({(0, 0)})}
    for _ in range(1, 5):
        shapes = expand_polyhexes(shapes)
    free_shapes = sorted(shapes)
    return free_shapes


def rotate_shape(coords, orientation):
    rotated = set(coords)
    for _ in range(orientation % 6):
        rotated = {rotate_left(c) for c in rotated}
    return rotated


def compute_bounding_box_area(shape):
    xs = [x for x, _ in shape]
    ys = [y for _, y in shape]
    return (max(xs) - min(xs) + 1) * (max(ys) - min(ys) + 1)


def is_symmetric_shape(shape):
    normalized = canonical_shape(shape)

    # rotation symmetry
    rotated = set(shape)
    for _ in range(1, 6):
        rotated = {rotate_left(c) for c in rotated}
        if canonical_shape(rotated) == normalized:
            return True

    # reflection symmetry
    reflected = {reflect(c) for c in shape}
    if canonical_shape(reflected) == normalized:
        return True
    rotated = set(reflected)
    for _ in range(1, 6):
        rotated = {rotate_left(c) for c in rotated}
        if canonical_shape(rotated) == normalized:
            return True

    return False


def is_connected_shape(shape):
    cells = set(shape)
    if not cells:
        return False

    visited = {next(iter(cells))}
    frontier = [next(iter(cells))]
    while frontier:
        cell = frontier.pop()
        for neighbor in [(cell[0] + dx, cell[1] + dy) for dx, dy in HEX_DIRECTIONS]:
            if neighbor in cells and neighbor not in visited:
                visited.add(neighbor)
                frontier.append(neighbor)
    return len(visited) == len(cells)


def get_board_cells():
    cells = []
    row_lengths = get_row_lengths()
    for r, length in enumerate(row_lengths, start=1):
        y = r - 8
        x_min = max(-7, 1 - r)
        for c in range(1, length + 1):
            x = x_min + c - 1
            cells.append((r, c, x, y))
    return cells

_ALL_CELLS = get_board_cells()
_CELL_ID_BY_AXIAL = {(x, y): f'R{r}C{c}' for r, c, x, y in _ALL_CELLS}
_AXIAL_BY_CELL_ID = {f'R{r}C{c}': (x, y) for r, c, x, y in _ALL_CELLS}


def axial_to_cell_id(x, y):
    return _CELL_ID_BY_AXIAL.get((x, y))


def cell_id_to_axial(cell_id):
    return _AXIAL_BY_CELL_ID.get(cell_id)


def generate_shape_catalog():
    free_shapes = generate_free_pentahexes()
    connected_shapes = [shape for shape in free_shapes if is_connected_shape(shape)]
    symmetric_shapes = [shape for shape in connected_shapes if is_symmetric_shape(shape)]
    symmetric_shapes.sort(key=lambda shape: (compute_bounding_box_area(shape), tuple(shape)))
    selected_shapes = symmetric_shapes[:12]
    catalog = []
    for index, shape in enumerate(selected_shapes, start=1):
        shape_id = f'P{index}'
        catalog.append({'id': shape_id, 'coords': list(shape)})
    return catalog

SHAPE_CATALOG = generate_shape_catalog()
SHAPE_ID_MAP = {shape['id']: shape for shape in SHAPE_CATALOG}
SHAPE_IDS = [shape['id'] for shape in SHAPE_CATALOG]


def placement_cells(shape_id, anchor_cell_id, orientation=0):
    if shape_id not in SHAPE_ID_MAP:
        return []
    anchor = cell_id_to_axial(anchor_cell_id)
    if anchor is None:
        return []
    rotated = rotate_shape(SHAPE_ID_MAP[shape_id]['coords'], orientation)
    cells = []
    for x, y in rotated:
        target = (anchor[0] + x, anchor[1] + y)
        target_id = axial_to_cell_id(*target)
        if target_id is None:
            return []
        cells.append(target_id)
    return cells


def compute_allocation_variance(user_votes):
    row_lengths = get_row_lengths()
    cell_ids = [cell_id for _, _, cell_id in sorted(
        [(r, c, f'R{r}C{c}') for r, length in enumerate(row_lengths, start=1) for c in range(1, length + 1)],
        key=lambda t: (t[0], t[1])
    )]
    counts = [user_votes.get(cell_id, 0) for cell_id in cell_ids]
    if not counts:
        return 0.0
    mean = sum(counts) / len(counts)
    variance = sum((count - mean) ** 2 for count in counts) / len(counts)
    return variance


def order_users_by_variance(user_votes_map):
    scored = []
    for username, votes in user_votes_map.items():
        variance = compute_allocation_variance(votes)
        scored.append((variance, username))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [username for _, username in scored]


def get_shape_choices():
    return [{'id': shape['id'], 'label': shape['id']} for shape in SHAPE_CATALOG]
