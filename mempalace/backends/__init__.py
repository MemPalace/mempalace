from typing import Protocol, runtime_checkable


@runtime_checkable
class VectorCollection(Protocol):
    def add(self, documents: list[str], ids: list[str], metadatas: list[dict]) -> None: ...

    def upsert(self, ids: list[str], documents: list[str], metadatas: list[dict]) -> None: ...

    def delete(self, ids: list[str]) -> None: ...

    def query(
        self,
        query_texts: list[str],
        n_results: int,
        where: dict | None = None,
        include: list[str] | None = None,
    ) -> dict: ...

    def get(
        self,
        where: dict | None = None,
        limit: int | None = None,
        offset: int | None = None,
        include: list[str] | None = None,
        ids: list[str] | None = None,
    ) -> dict: ...

    def count(self) -> int: ...
