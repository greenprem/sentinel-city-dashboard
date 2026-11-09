"""Central settings. Override any of these with environment variables."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
BOUNDARY_DIR = DATA_DIR / "boundaries"
RESULTS_DIR = Path(os.environ.get("SC_RESULTS_DIR", ROOT / "results"))
CACHE_DIR = Path(os.environ.get("SC_CACHE_DIR", ROOT / ".cache"))

# Free, credential-free Sentinel-2 L2A sources.
STAC_URL = os.environ.get("SC_STAC_URL", "https://earth-search.aws.element84.com/v1")
STAC_COLLECTION = "sentinel-2-l2a"
# Public bucket behind Earth Search; used directly when the STAC API is unreachable.
S3_COG_BASE = "https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs"

NOMINATIM_URL = os.environ.get("SC_NOMINATIM_URL", "https://nominatim.openstreetmap.org")
OVERPASS_URL = os.environ.get("SC_OVERPASS_URL", "https://overpass-api.de/api/interpreter")
USER_AGENT = os.environ.get("SC_USER_AGENT", "sentinel-city-dashboard/0.1 (research prototype)")

# Analysis defaults
DEFAULT_RESOLUTION = int(os.environ.get("SC_RESOLUTION", 20))  # metres; 10 is sharper but ~4x slower
DEFAULT_WINDOW_DAYS = 60          # look back this many days for a composite
MAX_SCENE_CLOUD = 60              # tile-level cloud % ceiling for candidate scenes
MAX_COMPOSITE_DATES = 6           # most acquisition dates blended into one composite
TARGET_CLEAR_OBS = 3              # stop adding dates once most pixels have this many clear looks
GRID_CELL_M = 2000                # side of a grid cell for the grid zoning

os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.TIF")
os.environ.setdefault("GDAL_HTTP_MULTIRANGE", "YES")
os.environ.setdefault("GDAL_HTTP_MERGE_CONSECUTIVE_RANGES", "YES")
os.environ.setdefault("GDAL_HTTP_MAX_RETRY", "4")
os.environ.setdefault("GDAL_HTTP_RETRY_DELAY", "1")
os.environ.setdefault("VSI_CACHE", "TRUE")
