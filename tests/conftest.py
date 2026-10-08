"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from chatbridge.config import Settings
from chatbridge.service import ImportService
from tests.fixtures import World, build_world


@pytest.fixture
def world(tmp_path: Path) -> World:
    return build_world(tmp_path)


@pytest.fixture
def service(world: World) -> ImportService:
    return ImportService(world.paths, Settings(extra_profiles=[("backup", str(world.backup_user))]))
