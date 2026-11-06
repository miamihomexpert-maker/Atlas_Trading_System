import os


# Paper sessions under pytest must not download history or headlines unless a
# test asks for that fetch directly.
os.environ.setdefault("ATLAS_AUTO_TRAIN", "false")
