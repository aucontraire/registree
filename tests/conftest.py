"""Shared fixtures: a synthetic project exercising every registry behavior.

The fixture project deliberately contains:

  * a Pydantic model typed only TRANSITIVELY (``Widget(BaseWidgetModel)``
    where ``BaseWidgetModel(BaseModel)``)
  * the accepted layered duplication (``Widget`` as ORM model under db/ and
    Pydantic model under models/)
  * a same-layer duplication smell (two ``Sprocket`` Pydantic models)
  * a plain class with required positional and keyword-only arguments
  * a class whose ``__init__`` soaks up ``**kwargs``
  * an aliased import (``Widget as WidgetDB``) for usage analysis
  * a tests/ directory that must be excluded unless opted in
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from registree.config import RegistreeConfig
from registree.generator import generate_registry

FIXTURE_FILES: dict[str, str] = {
    "src/widgetlib/__init__.py": "",
    "src/widgetlib/base.py": (
        "from pydantic import BaseModel\n"
        "\n"
        "\n"
        "class BaseWidgetModel(BaseModel):\n"
        '    """Shared base for domain models."""\n'
    ),
    "src/widgetlib/models/__init__.py": "",
    "src/widgetlib/models/widget.py": (
        "from widgetlib.base import BaseWidgetModel\n"
        "\n"
        "\n"
        "class Widget(BaseWidgetModel):\n"
        "    name: str\n"
        "    size: int = 1\n"
    ),
    "src/widgetlib/models/sprocket_a.py": (
        "from pydantic import BaseModel\n"
        "\n"
        "\n"
        "class Sprocket(BaseModel):\n"
        "    label: str\n"
    ),
    "src/widgetlib/models/sprocket_b.py": (
        "from pydantic import BaseModel\n"
        "\n"
        "\n"
        "class Sprocket(BaseModel):\n"
        "    tag: str\n"
    ),
    "src/widgetlib/db/__init__.py": "",
    "src/widgetlib/db/models.py": (
        "from sqlalchemy.orm import DeclarativeBase\n"
        "\n"
        "\n"
        "class Base(DeclarativeBase):\n"
        "    pass\n"
        "\n"
        "\n"
        "class Widget(Base):\n"
        '    __tablename__ = "widgets"\n'
    ),
    "src/widgetlib/gadgets.py": (
        "class Gadget:\n"
        "    def __init__(self, name: str, size: int = 2, *, flag: bool) -> None:\n"
        "        self.name = name\n"
        "\n"
        "\n"
        "class Flexible:\n"
        "    def __init__(self, **kwargs: object) -> None:\n"
        "        self.kwargs = kwargs\n"
    ),
    "src/widgetlib/services/__init__.py": "",
    "src/widgetlib/services/widget_service.py": (
        "from widgetlib.db.models import Widget as WidgetDB\n"
        "from widgetlib.models.widget import Widget\n"
        "\n"
        "\n"
        "def load(widget_id: int) -> WidgetDB:\n"
        "    row = WidgetDB()\n"
        "    return row\n"
        "\n"
        "\n"
        "def to_domain(row: WidgetDB) -> Widget:\n"
        '    return Widget(name="w")\n'
    ),
    # A client whose real method is fetch_items, so a hallucinated get_items
    # has something concrete to be caught against; plus a subclass, to prove
    # inherited methods are reported and overrides resolve to the derived one.
    "src/widgetlib/clients.py": (
        "class BaseClient:\n"
        "    def fetch_items(self, ids: list[str]) -> list[str]:\n"
        "        return []\n"
        "\n"
        "    def close(self) -> None:\n"
        "        pass\n"
        "\n"
        "\n"
        "class WidgetClient(BaseClient):\n"
        "    def __init__(self, api_key: str) -> None:\n"
        "        self.api_key = api_key\n"
        "\n"
        "    @property\n"
        "    def is_configured(self) -> bool:\n"
        "        return True\n"
        "\n"
        "    @classmethod\n"
        "    def from_env(cls) -> 'WidgetClient':\n"
        "        return cls(api_key='x')\n"
        "\n"
        "    @staticmethod\n"
        "    def normalize(value: str) -> str:\n"
        "        return value.strip()\n"
        "\n"
        "    async def refresh(self, **options: object) -> None:\n"
        "        return None\n"
        "\n"
        "    def close(self) -> None:\n"
        "        pass\n"
        "\n"
        "    def _fetch_batch(self, ids: list[str]) -> list[str]:\n"
        "        return []\n"
    ),
    "tests/__init__.py": "",
    "tests/test_sample.py": "class SampleCase:\n    pass\n",
}


@pytest.fixture
def sample_project(tmp_path: Path) -> Path:
    for relative, content in FIXTURE_FILES.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return tmp_path


@pytest.fixture
def sample_config(sample_project: Path) -> RegistreeConfig:
    return RegistreeConfig.discover(sample_project)


@pytest.fixture
def sample_registry_doc(sample_config: RegistreeConfig) -> dict[str, Any]:
    return generate_registry(sample_config)
