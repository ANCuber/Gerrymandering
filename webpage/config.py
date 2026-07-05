DATABASE = 'voting_game.db'

TOTAL_ALLOWANCE = 100
MAX_BALLOTS_PER_CELL_PER_USER = 20

# Choose the board layout:
# - 'standard' for the original symmetric hex board
# - 'border_asymmetric' for a mild border-only variation
GRID_MAP = 'border_asymmetric'

ADMIN_USERNAME = 'admin'
ADMIN_PASSWORD = 'admin'
ADMIN_COLOR = '#29292e'

USER_REGISTRY = {
    "group1": {"secret": "group1", "color": "#ff4757"},
    "group2": {"secret": "group2", "color": "#2ed573"},
    "group3": {"secret": "group3", "color": "#1e90ff"},
    "group4": {"secret": "group4", "color": "#ffa502"},
    "group5": {"secret": "group5", "color": "#9b59b6"},
    "group6": {"secret": "group6", "color": "#1abc9c"},
    "group7": {"secret": "group7", "color": "#fd79a8"},
    "group8": {"secret": "group8", "color": "#f1c40f"},
    "group9": {"secret": "group9", "color": "#e67e22"},
    "group10": {"secret": "group10", "color": "#6c5ce7"}
}

# How many groups (from the start of USER_REGISTRY) are attending the game.
# Set this to an integer to limit attendees. Default is all groups.
GROUP_COUNT = 10


def get_active_user_registry():
    """Return a dict of the first GROUP_COUNT entries from USER_REGISTRY."""
    try:
        n = int(GROUP_COUNT)
    except Exception:
        n = len(USER_REGISTRY)
    keys = list(USER_REGISTRY.keys())
    n = max(0, min(n, len(keys)))
    return {k: USER_REGISTRY[k] for k in keys[:n]}
