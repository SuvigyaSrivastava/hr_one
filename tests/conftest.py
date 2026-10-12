"""Shared fixtures for the live-API tests. They talk to a running server (BASE_URL, default :8000)
and use only employee codes EMP9xxx, which are wiped before and after each test module."""
import os
import pathlib
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")


@pytest.fixture(scope="session")
def mongo_db():
    c = MongoClient(os.environ["MONGO_URI"], tz_aware=True)
    return c[os.getenv("MONGO_DB", "attendance_db")]


@pytest.fixture(autouse=True)
def clean(mongo_db):
    def wipe():
        mongo_db.employees.delete_many({"emp_code": {"$regex": "^EMP9"}})
        mongo_db.attendance_logs.delete_many({"emp_code": {"$regex": "^EMP9"}})
    wipe()
    yield
    wipe()


@pytest.fixture(scope="session")
def api():
    with httpx.Client(base_url=BASE_URL, timeout=30) as c:
        yield c


@pytest.fixture
def parallel():
    """parallel(fn, n) -> list of n results, all calls fired as close together as possible."""
    def run(fn, n):
        with ThreadPoolExecutor(max_workers=n) as ex:
            return list(ex.map(lambda _: fn(), range(n)))
    return run
