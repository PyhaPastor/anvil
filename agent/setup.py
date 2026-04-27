from setuptools import setup, find_packages
setup(
    name="anvil-agent",
    version="1.0.0",
    packages=find_packages(),
    entry_points={"console_scripts": ["anvil-agent=anvil_agent.main:main"]},
    python_requires=">=3.11",
)
