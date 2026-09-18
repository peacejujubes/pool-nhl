"""Static NHL team metadata: display name (FR) and a primary brand color,
used only for small accent bars/badges in the UI — nothing fetched live."""

TEAM_INFO = {
    "ANA": ("Ducks d'Anaheim", "#B5985A"),
    "BOS": ("Bruins de Boston", "#FFB81C"),
    "BUF": ("Sabres de Buffalo", "#002654"),
    "CGY": ("Flames de Calgary", "#C8102E"),
    "CAR": ("Hurricanes de la Caroline", "#CC0000"),
    "CHI": ("Blackhawks de Chicago", "#CF0A2C"),
    "COL": ("Avalanche du Colorado", "#6F263D"),
    "CBJ": ("Blue Jackets de Columbus", "#002654"),
    "DAL": ("Stars de Dallas", "#006847"),
    "DET": ("Red Wings de Détroit", "#CE1126"),
    "EDM": ("Oilers d'Edmonton", "#FF4C00"),
    "FLA": ("Panthers de la Floride", "#C8102E"),
    "LAK": ("Kings de Los Angeles", "#111111"),
    "MIN": ("Wild du Minnesota", "#154734"),
    "MTL": ("Canadiens de Montréal", "#AF1E2D"),
    "NSH": ("Predators de Nashville", "#FFB81C"),
    "NJD": ("Devils du New Jersey", "#CE1126"),
    "NYI": ("Islanders de New York", "#00539B"),
    "NYR": ("Rangers de New York", "#0038A8"),
    "OTT": ("Sénateurs d'Ottawa", "#C52032"),
    "PHI": ("Flyers de Philadelphie", "#F74902"),
    "PIT": ("Penguins de Pittsburgh", "#FCB514"),
    "SJS": ("Sharks de San Jose", "#006D75"),
    "SEA": ("Kraken de Seattle", "#001628"),
    "STL": ("Blues de St. Louis", "#002F87"),
    "TBL": ("Lightning de Tampa Bay", "#002868"),
    "TOR": ("Maple Leafs de Toronto", "#00205B"),
    "UTA": ("Mammoth de l'Utah", "#69B3E7"),
    "VAN": ("Canucks de Vancouver", "#00205B"),
    "VGK": ("Golden Knights de Vegas", "#B4975A"),
    "WSH": ("Capitals de Washington", "#C8102E"),
    "WPG": ("Jets de Winnipeg", "#041E42"),
}


def team_color(code: str) -> str:
    return TEAM_INFO.get(code, ("", "#5b6b7c"))[1]


def team_name(code: str) -> str:
    info = TEAM_INFO.get(code)
    return info[0] if info else code
