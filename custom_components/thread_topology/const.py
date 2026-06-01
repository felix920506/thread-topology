"""Constants for Thread Topology integration."""

DOMAIN = "thread_topology"

# Default OTBR URL (inside HA container network)
DEFAULT_OTBR_URL = "http://core-openthread-border-router:8081"

# JSON:API media type used by the new OTBR REST interface
API_MEDIA_TYPE = "application/vnd.api+json"

# API endpoints (new OTBR REST layout, JSON:API style under /api/*)
ENDPOINT_NODE = "/api/node"
ENDPOINT_ACTIONS = "/api/actions"
ENDPOINT_DEVICES = "/api/devices"
ENDPOINT_DIAGNOSTICS = "/api/diagnostics"

# Action (task) types posted to /api/actions
TASK_UPDATE_DEVICES = "updateDeviceCollectionTask"
TASK_GET_DIAGNOSTIC = "getNetworkDiagnosticTask"

# Diagnostic TLV types requested per router to build the mesh topology
DIAGNOSTIC_TLV_TYPES = [
    "extAddress",
    "rloc16",
    "mode",
    "connectivity",
    "route64",
    "leaderData",
    "childTable",
    "childIpv6Addresses",
]

# Terminal statuses for an action/task in the queue
ACTION_TERMINAL_STATUSES = {"completed", "failed", "stopped"}

# Per-request HTTP timeout (seconds)
REQUEST_TIMEOUT = 10
# Per-task budget when running an action (seconds) and how often to poll it
ACTION_TIMEOUT = 60
ACTION_POLL_INTERVAL = 1.0

# Update interval in seconds (heavier now: discovery + per-router diagnostics)
DEFAULT_SCAN_INTERVAL = 60

# Device types
DEVICE_TYPE_ROUTER = "router"
DEVICE_TYPE_END_DEVICE = "end_device"
DEVICE_TYPE_SLEEPY_END_DEVICE = "sleepy_end_device"
DEVICE_TYPE_LEADER = "leader"

# Attributes
ATTR_EXT_ADDRESS = "ext_address"
ATTR_RLOC16 = "rloc16"
ATTR_ROLE = "role"
ATTR_LINK_QUALITY = "link_quality"
ATTR_CHILD_COUNT = "child_count"
ATTR_ROUTER_COUNT = "router_count"
ATTR_NETWORK_NAME = "network_name"
ATTR_LEADER_COST = "leader_cost"
