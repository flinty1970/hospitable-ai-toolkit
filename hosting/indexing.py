"""Build immutable property indexes; publish only completed generations."""
import fcntl
import hashlib
import json
import os
import re
import uuid
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

COLLECTION = "property_knowledge"
MODEL = "all-MiniLM-L6-v2"


def markdown_chunks(root, allow_empty=False):
    docs = Path(root) / "docs"
    if docs.is_symlink() or Path(root).resolve() not in docs.resolve().parents:
        raise ValueError("Curated document directory escapes its property")
    result = []
    for path in sorted(docs.rglob("*.md")):
        if path.is_symlink() or docs.resolve() not in path.resolve().parents:
            raise ValueError("Curated document escapes its property")
        text = path.read_text(encoding="utf-8").strip()
        for start in range(0, len(text), 1100):
            chunk = text[start:start + 1300].strip()
            if chunk:
                source = str(path.relative_to(docs))
                identity = hashlib.sha256((source + ":" + str(start) + ":" + chunk).encode()).hexdigest()
                result.append((identity, chunk, {"source": source, "guest_safe": True}))
    if not result and not allow_empty:
        raise ValueError("No curated Markdown; existing index was preserved")
    return result


@lru_cache(maxsize=1)
def embedding():
    from chromadb.utils import embedding_functions
    return embedding_functions.SentenceTransformerEmbeddingFunction(model_name=MODEL)


@contextmanager
def index_lock(root, exclusive, name="index.lock"):
    state = Path(root) / "state"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / name).open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield


def active_index(root):
    index = Path(root) / "index"
    manifest = index / "current.json"
    if not manifest.exists():
        return None
    data = json.loads(manifest.read_text())
    generation = data["generation"]
    if data.get("schema") != 1 or not re.fullmatch(r"[0-9a-f]{32}", generation):
        raise ValueError("Invalid index manifest")
    path = (index / generation).resolve()
    if index.resolve() not in path.parents or not path.is_dir():
        raise ValueError("Index generation escapes its property or is missing")
    return path


def rebuild(root, builder=None, allow_empty=False):
    root = Path(root)
    chunks = markdown_chunks(root, allow_empty=allow_empty)
    with index_lock(root, True, "index-build.lock"):
        generation = uuid.uuid4().hex
        target = root / "index" / generation
        target.mkdir(parents=True, mode=0o700)
        if builder is None:
            def builder(path, records):
                import chromadb
                from chromadb.config import Settings
                client = chromadb.PersistentClient(path=str(path), settings=Settings(anonymized_telemetry=False))
                collection = client.create_collection(COLLECTION, embedding_function=embedding())
                for start in range(0, len(records), 100):
                    batch = records[start:start + 100]
                    collection.add(ids=[x[0] for x in batch], documents=[x[1] for x in batch], metadatas=[x[2] for x in batch])
                if collection.count() != len(records):
                    raise ValueError("Incomplete index")
        builder(target, chunks)
        manifest = {"schema": 1, "generation": generation, "chunks": len(chunks), "embedding_model": MODEL}
        temporary = root / "index/current.json.tmp"
        with temporary.open("w") as file:
            json.dump(manifest, file)
            file.flush()
            os.fsync(file.fileno())
        with index_lock(root, True):
            os.replace(temporary, root / "index/current.json")
        return manifest


def retrieve(root, message):
    import chromadb
    from chromadb.config import Settings
    with index_lock(root, False):
        target = active_index(root)
        if target is None:
            return []
        client = chromadb.PersistentClient(path=str(target), settings=Settings(anonymized_telemetry=False))
        collection = client.get_collection(COLLECTION, embedding_function=embedding())
        if not collection.count():
            return []
        hits = collection.query(query_texts=[message], n_results=min(4, collection.count()), where={"guest_safe": True})
        return [{"text": text, "source": metadata} for text, metadata in zip(hits["documents"][0], hits["metadatas"][0])]
