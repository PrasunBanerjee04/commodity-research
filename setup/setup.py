"""Setuptools package locations for the repository layout."""
from setuptools import find_packages, setup

setup(
    packages=(find_packages("../src")
              + ["comm_research.infra"]
              + ["comm_research.infra." + name for name in find_packages("../infra") if name not in ("tests", "datalake") and not name.startswith(("tests.", "datalake."))]
              + ["comm_research.setup"]),
    package_dir={"": "../src", "comm_research.infra": "../infra", "comm_research.setup": "."},
)
