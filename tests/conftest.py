import os


# Paper sessions under pytest must not download history, headlines, or company
# facts unless a test asks for that fetch directly.
os.environ.setdefault("ATLAS_AUTO_TRAIN", "false")
os.environ.setdefault("ATLAS_COMPANY_FACTS", "false")
os.environ.setdefault("ATLAS_BEHAVIOR", "false")
