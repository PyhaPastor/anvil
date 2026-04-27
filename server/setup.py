from setuptools import setup, find_packages
setup(
    name="anvil-server",
    version="1.0.0",
    packages=find_packages(),
    entry_points={"console_scripts": ["anvil-server=anvil_server.main:app"]},
    python_requires=">=3.11",
)
