"""
================================================================
  STEWARD — CONTROL PANEL
================================================================
  Every knob in the whole app lives in this file.
  Change something here and the change propagates everywhere.
  Nothing else needs editing to retune behaviour.

  NOTE ON BACKSLASHES: nothing in this file uses a Windows
  backslash literal. Drives are written as "D:" and paths are
  joined with pathlib. This is deliberate — hardcoded "D:\\"
  string literals are the single most common source of broken
  path logic on Windows. Don't reintroduce them.
================================================================
"""

# ── SANDBOX ─────────────────────────────────────────────────
# The only drive the assistant may ever touch. Everything else
# is refused before any filesystem call happens.
ALLOWED_DRIVE = "choose your drive"

# When True, the assistant can only reach inside the folders you
# added in the UI ("scope"). When False, it can reach anywhere on
# ALLOWED_DRIVE. Keep True unless you really mean it.
RESTRICT_TO_SCOPE = True

# Names the assistant may never touch, whatever the scope says.
FORBIDDEN_NAMES = {
    "pagefile.sys", "hiberfil.sys", "swapfile.sys",
    "$recycle.bin", "system volume information",
}

# Folder names skipped when walking trees (noise + huge).
SKIP_DIRS = {
    ".git", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".next", "dist", "build", ".cache", ".idea", ".vscode",
    "$recycle.bin", "system volume information",
}

# Model folders may live outside the scope but still on ALLOWED_DRIVE.
# Set False to allow loading a model from any drive (C:, E:, ...).
MODEL_PATH_DRIVE_LOCKED = True

# ── HARD LIMITS ─────────────────────────────────────────────
MAX_READ_BYTES = 2 * 1024 * 1024      # biggest file the model may read
MAX_READ_CHARS = 12_000               # chars sent to the model per file
MAX_LIST_ITEMS = 400                  # entries returned by one listing
MAX_SEARCH_RESULTS = 200
MAX_TREE_DEPTH = 4
MAX_DUPLICATE_GROUPS = 25             # groups analysed in one dup scan
MAX_FILES_PER_GROUP = 4               # files read per duplicate group

# ── AGENT ───────────────────────────────────────────────────
MAX_TOOL_STEPS = 6        # tool calls the model may chain in one turn
MAX_HISTORY_TURNS = 12    # user+assistant pairs kept in context
MAX_NEW_TOKENS = 700
TEMPERATURE = 0.25
TOP_P = 0.9
CONTEXT_TOKENS = 8192

# Tools that must never run without you clicking Confirm.
# Add a tool name here and it becomes gated automatically.
REQUIRES_CONFIRMATION = {"rename_entry", "move_entry"}

# ── ENGINE ──────────────────────────────────────────────────
# "auto" inspects the model folder: *.safetensors -> transformers,
# *.gguf -> llama.cpp. Force it with "transformers" or "llamacpp".
BACKEND = "auto"
PREFER_GPU = True
GPU_LAYERS = -1           # llama.cpp only: -1 = offload everything it can

# ── SERVER ──────────────────────────────────────────────────
HOST = "127.0.0.1"        # loopback only — not reachable from your network
PORT = 811
OPEN_BROWSER = True

# ── FILES ───────────────────────────────────────────────────
CONFIG_FILE = "steward_config.json"
AUDIT_FILE = "steward_audit.log"

# ── AUDIT ───────────────────────────────────────────────────
AUDIT_ENABLED = True
AUDIT_ECHO_TO_CONSOLE = False

# ── SELF-CHECK ──────────────────────────────────────────────
# At boot, Steward greps its own source for delete calls and
# refuses to start if it finds any. This is what makes "it cannot
# delete" a property of the code rather than a promise in a README.
ENFORCE_NO_DELETE_SELFCHECK = False
BANNED_CALLS = (
    "os.remove", "os.unlink", "os.rmdir", "os.removedirs",
    "shutil.rmtree", ".unlink(", "send2trash", "rmtree",
)

# ── UI TEXT ─────────────────────────────────────────────────
APP_NAME = "Steward"
APP_TAGLINE = "Local AI file assistant"
