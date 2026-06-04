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

# Diagnostic TLV types requested per router to build the mesh topology.
# NOTE: the route table TLV is named "route" on the OTBR REST API (the standard
# "route64" name is rejected with a 422). Only the TLVs the topology builder
# actually consumes are requested.
DIAGNOSTIC_TLV_TYPES = [
    "extAddress",
    "rloc16",
    "mode",
    "connectivity",
    "route",
    "leaderData",
    "childTable",
    # "children" returns each child's extAddress + rloc16 (so children can be
    # matched to Home Assistant Matter devices and named).
    "children",
    # vendor info, used to name devices not known to Home Assistant Matter
    # (e.g. standalone OpenThread border routers). NOTE: vendorVersion /
    # threadStackVersion are rejected (422) on some builds, so are not requested.
    "vendorName",
    "vendorModel",
]

# Terminal statuses for an action/task in the queue
ACTION_TERMINAL_STATUSES = {"completed", "failed", "stopped"}

# Per-request HTTP timeout (seconds)
REQUEST_TIMEOUT = 10
# Per-task budget when running an action (seconds) and how often to poll it
ACTION_TIMEOUT = 60
ACTION_POLL_INTERVAL = 1.0

# updateDeviceCollectionTask required attributes (all four are mandatory)
# maxAge caps how stale a cached device entry may be before OTBR re-queries it:
# OTBR is free to return cached results for any entry younger than maxAge instead
# of actively crawling the mesh. Set to 0 so every poll forces a real crawl rather
# than replaying the cache (otherwise the topology goes stale / drops live nodes).
DISCOVERY_MAX_AGE = 0
DISCOVERY_MAX_RETRIES = 5
DISCOVERY_DEVICE_COUNT = 64

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
