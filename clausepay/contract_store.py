"""Storage for deeds.

Code elsewhere should depend on ContractStore only. To move to Postgres,
write a PostgresContractStore that implements the same five methods.
"""
import json
import os
import tempfile
import threading
from abc import ABC, abstractmethod

from clausepay.deed import Deed, _now


class DeedNotFound(KeyError):
    pass


class ContractStore(ABC):
    @abstractmethod
    def add(self, deed: Deed) -> Deed:
        """Insert a new deed. Raises ValueError if the id already exists."""

    @abstractmethod
    def get(self, deed_id: str) -> Deed:
        """Raises DeedNotFound."""

    @abstractmethod
    def save(self, deed: Deed) -> Deed:
        """Overwrite an existing deed and bump updated_at. Raises DeedNotFound."""

    @abstractmethod
    def list(self) -> list:
        """All deeds, oldest first."""

    @abstractmethod
    def delete(self, deed_id: str) -> None:
        """Raises DeedNotFound."""


class JsonContractStore(ContractStore):
    """Keeps every deed in one JSON file, keyed by deed id."""

    def __init__(self, path="data/deeds.json"):
        self.path = path
        self._lock = threading.Lock()

    def _read(self):
        if not os.path.exists(self.path):
            return {}
        with open(self.path) as f:
            return json.load(f)

    def _write(self, data):
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        # write to a temp file then rename, so a crash never leaves half a file
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)

    def add(self, deed):
        with self._lock:
            data = self._read()
            if deed.deed_id in data:
                raise ValueError(f"{deed.deed_id} already exists")
            data[deed.deed_id] = deed.to_dict()
            self._write(data)
        return deed

    def get(self, deed_id):
        data = self._read()
        if deed_id not in data:
            raise DeedNotFound(deed_id)
        return Deed.from_dict(data[deed_id])

    def save(self, deed):
        with self._lock:
            data = self._read()
            if deed.deed_id not in data:
                raise DeedNotFound(deed.deed_id)
            deed.updated_at = _now()
            data[deed.deed_id] = deed.to_dict()
            self._write(data)
        return deed

    def list(self):
        return [Deed.from_dict(d) for d in self._read().values()]

    def delete(self, deed_id):
        with self._lock:
            data = self._read()
            if deed_id not in data:
                raise DeedNotFound(deed_id)
            del data[deed_id]
            self._write(data)
