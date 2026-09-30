"""Reindex one property's curated Markdown, leaving raw source documents private."""
import os
from hosting.indexing import rebuild

if __name__ == "__main__":
    result = rebuild(os.environ["TOOLKIT_PROPERTY_DIR"])
    print(f"Published property index: {result['chunks']} chunks")
