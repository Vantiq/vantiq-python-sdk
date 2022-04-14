import pathlib
from setuptools import setup

# The directory containing this file
HERE = pathlib.Path(__file__).parent

# The text of the README file
README = (HERE / "README.md").read_text()

# Configure the package contents
setup(
    name="vantiq-sdk",
    version="0.9.0",
    description="SDK for working with the Vantiq system",
    long_description=README,
    long_description_content_type="text/markdown",
    url="https://github.com/vantiq/vantiq-python-sdk",
    author="Vantiq, Inc",
    author_email="fcarter@vantiq.com",
    license="MIT",
    classifiers=[
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
    ],
    packages=["vantiq"],
    package_dir={
        'vantiq': 'src/main/python/vantiq',
    },
    include_package_data=True,
    python_reuires='>=3.8',
    install_requires=[
        "aiohttp>=3.8",
        "websockets>=10.2"
    ],
    entry_points={},
)
